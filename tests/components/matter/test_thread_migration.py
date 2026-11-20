"""Test moving Matter Thread devices onto another Thread network."""

from typing import Any
from unittest.mock import MagicMock, patch

from chip.clusters import Objects as clusters
from chip.clusters.Types import NullValue
from matter_server.common.errors import MatterError
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr

from .common import (
    _setup_integration_with_nodes,
    create_node_from_fixture,
    setup_integration_with_node_fixture,
)

# Extended PAN ID 1111111122222222, the network the devices are moved to
DATASET = bytes.fromhex(
    "0e080000000003e90000000300000f35060004001fffe002081111111122222222"
)
TARGET_ID = bytes.fromhex("1111111122222222")
# The network of the eve_contact_sensor fixture
PREVIOUS_ID = bytes.fromhex("52fe7495632d4fbb")
CONTACT_SENSOR = 9
ENERGY_PLUG = 61

Commands = clusters.NetworkCommissioning.Commands
Commissioning = clusters.GeneralCommissioning.Commands


def _device_id(hass: HomeAssistant, node_id: int) -> str:
    """Return the device id of a node."""
    return next(
        device.id
        for device in dr.async_get(hass).devices
        if any(
            domain == "matter" and f"-{node_id:016X}-" in value
            for domain, value in device.identifiers
        )
    )


def _commands(matter_client: MagicMock) -> list[Any]:
    """Return the commands sent to devices."""
    return [
        call.kwargs["command"]
        for call in matter_client.send_device_command.call_args_list
    ]


def _disarmed(matter_client: MagicMock) -> bool:
    """Return whether a fail-safe was disarmed."""
    return any(
        isinstance(command, Commissioning.ArmFailSafe)
        and command.expiryLengthSeconds == 0
        for command in _commands(matter_client)
    )


@pytest.fixture(name="healthy_devices")
def healthy_devices_fixture(matter_client: MagicMock) -> MagicMock:
    """Answer every command the way a healthy device does."""

    async def respond(command: Any, **kwargs: Any) -> dict[str, int]:
        if isinstance(
            command, (Commissioning.ArmFailSafe, Commissioning.CommissioningComplete)
        ):
            return {"errorCode": 0}
        return {"networkingStatus": 0}

    matter_client.send_device_command.side_effect = respond
    return respond


def _answer(matter_client: MagicMock, healthy: Any, answers: dict[type, Any]) -> None:
    """Answer the given command types differently, the rest healthily."""

    async def respond(command: Any, **kwargs: Any) -> Any:
        for command_type, answer in answers.items():
            if isinstance(command, command_type):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return await healthy(command=command, **kwargs)

    matter_client.send_device_command.side_effect = respond


async def _migrate(hass: HomeAssistant, node_id: int = CONTACT_SENSOR) -> Any:
    """Call the single device migration."""
    return await hass.services.async_call(
        "matter",
        "migrate_thread_device",
        {"device_id": _device_id(hass, node_id), "dataset": DATASET.hex()},
        blocking=True,
        return_response=True,
    )


async def _migrate_fleet(hass: HomeAssistant, return_response: bool = True) -> Any:
    """Call the fleet migration."""
    return await hass.services.async_call(
        "matter",
        "migrate_thread_fleet",
        {"dataset": DATASET.hex()},
        blocking=True,
        return_response=return_response,
    )


@pytest.mark.usefixtures("healthy_devices")
async def test_device_is_migrated(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """The device is moved under an armed fail-safe."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)

    assert await _migrate(hass) == {"result": "migrated"}

    commands = _commands(matter_client)
    assert [type(command) for command in commands] == [
        Commissioning.ArmFailSafe,
        Commands.RemoveNetwork,
        Commands.AddOrUpdateThreadNetwork,
        Commands.ConnectNetwork,
        Commissioning.CommissioningComplete,
    ]
    assert commands[0].expiryLengthSeconds == 900
    assert commands[1].networkID == PREVIOUS_ID
    assert commands[2].operationalDataset == DATASET
    assert commands[3].networkID == TARGET_ID


@pytest.mark.usefixtures("healthy_devices")
async def test_a_stale_cache_is_corrected_by_a_live_read(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A device whose cached network is stale is not reconfigured."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)
    matter_client.read_attribute.return_value = {
        "0/53/4": int.from_bytes(TARGET_ID, "big")
    }

    assert await _migrate(hass) == {"result": "already_on_network"}
    assert not matter_client.send_device_command.called


@pytest.mark.usefixtures("healthy_devices")
async def test_the_live_network_is_removed(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """The network the device reports live is the one removed."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)
    matter_client.read_attribute.return_value = {"0/53/4": 0x0102030405060708}

    assert await _migrate(hass) == {"result": "migrated"}
    assert _commands(matter_client)[1].networkID == bytes.fromhex("0102030405060708")


@pytest.mark.usefixtures("healthy_devices")
async def test_a_failed_live_read_falls_back_to_the_cache(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A device that does not answer the read is moved from its cached network."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)
    matter_client.read_attribute.side_effect = MatterError("timeout")

    assert await _migrate(hass) == {"result": "migrated"}
    assert _commands(matter_client)[1].networkID == PREVIOUS_ID


@pytest.mark.usefixtures("healthy_devices")
async def test_a_device_already_on_the_network(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A device already on the target network is left untouched."""
    await setup_integration_with_node_fixture(
        hass,
        "eve_contact_sensor",
        matter_client,
        override_attributes={"0/53/4": int.from_bytes(TARGET_ID, "big")},
    )

    assert await _migrate(hass) == {"result": "already_on_network"}
    assert not matter_client.send_device_command.called


@pytest.mark.usefixtures("healthy_devices")
async def test_a_border_router_is_refused(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A border router is the network, not a device on it."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )

    with pytest.raises(ServiceValidationError) as exc_info:
        await _migrate(hass, 90)

    assert exc_info.value.translation_key == "not_a_thread_device"
    assert not matter_client.send_device_command.called


@pytest.mark.usefixtures("healthy_devices")
async def test_an_unknown_previous_network_is_not_removed(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """Without a known previous network there is nothing to remove."""
    await setup_integration_with_node_fixture(
        hass,
        "eve_contact_sensor",
        matter_client,
        override_attributes={"0/53/4": NullValue},
    )

    assert await _migrate(hass) == {"result": "migrated"}
    assert not any(
        isinstance(command, Commands.RemoveNetwork)
        for command in _commands(matter_client)
    )


async def test_a_network_already_removed_is_a_rerun(
    hass: HomeAssistant, matter_client: MagicMock, healthy_devices: Any
) -> None:
    """A previous network the device no longer holds does not stop the move."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)
    not_found = clusters.NetworkCommissioning.Enums.NetworkCommissioningStatusEnum.kNetworkIDNotFound
    _answer(
        matter_client,
        healthy_devices,
        {Commands.RemoveNetwork: {"networkingStatus": not_found}},
    )

    assert await _migrate(hass) == {"result": "migrated"}


async def test_a_lost_connect_answer_is_survived(
    hass: HomeAssistant, matter_client: MagicMock, healthy_devices: Any
) -> None:
    """The device detaches on ConnectNetwork, so its answer may never come."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)
    not_yet = iter([MatterError("not reachable yet")])

    async def respond(command: Any, **kwargs: Any) -> Any:
        if isinstance(command, Commands.ConnectNetwork):
            raise MatterError("switching networks")
        if isinstance(command, Commissioning.CommissioningComplete) and (
            error := next(not_yet, None)
        ):
            raise error
        return await healthy_devices(command=command, **kwargs)

    matter_client.send_device_command.side_effect = respond

    with patch(
        "homeassistant.components.matter.thread_migration.COMPLETE_RETRY_DELAY", 0
    ):
        assert await _migrate(hass) == {"result": "migrated"}

    # The controller is made to look the device up on the new network.
    matter_client.ping_node.assert_awaited_once_with(CONTACT_SENSOR)
    assert not _disarmed(matter_client)


@pytest.mark.parametrize(
    "answers",
    [
        {Commands.ConnectNetwork: {"networkingStatus": 9, "debugText": "no"}},
        {Commands.AddOrUpdateThreadNetwork: {"networkingStatus": 2}},
        {Commands.RemoveNetwork: MatterError("busy")},
    ],
)
async def test_a_refusal_before_the_switch_disarms(
    hass: HomeAssistant,
    matter_client: MagicMock,
    healthy_devices: Any,
    answers: dict[type, Any],
) -> None:
    """A step refused before the device left its network disarms the fail-safe."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)
    _answer(matter_client, healthy_devices, answers)

    with pytest.raises(HomeAssistantError) as exc_info:
        await _migrate(hass)

    assert exc_info.value.translation_key == "thread_migration_failed"
    command = _commands(matter_client)[-1]
    assert isinstance(command, Commissioning.ArmFailSafe)
    assert command.expiryLengthSeconds == 0


async def test_a_failed_arm_sends_nothing_else(
    hass: HomeAssistant, matter_client: MagicMock, healthy_devices: Any
) -> None:
    """A fail-safe the device does not arm stops the move."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)
    _answer(
        matter_client,
        healthy_devices,
        {Commissioning.ArmFailSafe: {"errorCode": 4, "debugText": "busy"}},
    )

    with pytest.raises(HomeAssistantError) as exc_info:
        await _migrate(hass)

    assert exc_info.value.translation_placeholders == {
        "error": "ArmFailSafe returned error 4 busy"
    }
    assert len(_commands(matter_client)) == 1


async def test_an_unreachable_device_is_reported_not_disarmed(
    hass: HomeAssistant, matter_client: MagicMock, healthy_devices: Any
) -> None:
    """After ConnectNetwork, the fail-safe takes a lost device back."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)
    _answer(
        matter_client,
        healthy_devices,
        {Commissioning.CommissioningComplete: MatterError("gone")},
    )

    with (
        patch("homeassistant.components.matter.thread_migration.COMPLETE_DEADLINE", 0),
        pytest.raises(HomeAssistantError) as exc_info,
    ):
        await _migrate(hass)

    assert exc_info.value.translation_key == "thread_migration_unreachable"
    assert not _disarmed(matter_client)


@pytest.mark.usefixtures("healthy_devices")
async def test_a_dataset_without_a_network_id_is_rejected(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A dataset that names no network is rejected before any command."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)

    with pytest.raises(ServiceValidationError) as exc_info:
        await hass.services.async_call(
            "matter",
            "migrate_thread_device",
            {
                "device_id": _device_id(hass, CONTACT_SENSOR),
                "dataset": "0e080000000003e90000",
            },
            blocking=True,
            return_response=True,
        )

    assert exc_info.value.translation_key == "dataset_missing_extpanid"
    assert not matter_client.send_device_command.called


@pytest.mark.usefixtures("healthy_devices")
async def test_the_fleet_is_moved_end_devices_first(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """End devices go first and border routers are left alone."""
    await _setup_integration_with_nodes(
        hass,
        matter_client,
        [
            create_node_from_fixture("thread_border_router", override_serial=True),
            create_node_from_fixture("eve_energy_plug", override_serial=True),
            create_node_from_fixture("eve_contact_sensor", override_serial=True),
        ],
    )

    response = await _migrate_fleet(hass)

    # The contact sensor is a sleepy end device, the plug a router.
    assert [entry["node_id"] for entry in response["migrated"]] == [
        CONTACT_SENSOR,
        ENERGY_PLUG,
    ]
    assert response["skipped"] == []
    assert response["failed"] == []
    arms = [
        command
        for command in _commands(matter_client)
        if isinstance(command, Commissioning.ArmFailSafe)
    ]
    assert len(arms) == 2


@pytest.mark.usefixtures("healthy_devices")
async def test_the_fleet_skips_moved_and_offline_devices(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """Devices on the network are skipped, offline ones reported."""
    moved = create_node_from_fixture(
        "eve_contact_sensor", {"0/53/4": int.from_bytes(TARGET_ID, "big")}
    )
    offline = create_node_from_fixture("eve_energy_plug", override_serial=True)
    offline.node_data.available = False
    await _setup_integration_with_nodes(hass, matter_client, [moved, offline])

    response = await _migrate_fleet(hass)

    assert [entry["node_id"] for entry in response["skipped"]] == [CONTACT_SENSOR]
    assert response["failed"] == [
        {"name": offline.name, "node_id": ENERGY_PLUG, "reason": "offline"}
    ]
    assert not matter_client.send_device_command.called


async def test_the_fleet_continues_after_a_failure(
    hass: HomeAssistant, matter_client: MagicMock, healthy_devices: Any
) -> None:
    """A device that fails is reported and does not stop the others."""
    await _setup_integration_with_nodes(
        hass,
        matter_client,
        [
            create_node_from_fixture("eve_energy_plug", override_serial=True),
            create_node_from_fixture("eve_contact_sensor", override_serial=True),
        ],
    )

    async def respond(command: Any, node_id: int, **kwargs: Any) -> Any:
        if node_id == CONTACT_SENSOR and isinstance(command, Commissioning.ArmFailSafe):
            return {"errorCode": 4}
        return await healthy_devices(command=command, node_id=node_id, **kwargs)

    matter_client.send_device_command.side_effect = respond

    response = await _migrate_fleet(hass)

    assert [entry["node_id"] for entry in response["migrated"]] == [ENERGY_PLUG]
    assert [entry["node_id"] for entry in response["failed"]] == [CONTACT_SENSOR]

    # Without the response, the failure is the action's.
    with pytest.raises(HomeAssistantError) as exc_info:
        await _migrate_fleet(hass, return_response=False)

    assert exc_info.value.translation_key == "thread_migration_incomplete"


@pytest.mark.usefixtures("healthy_devices")
async def test_a_device_without_a_thread_interface_is_refused(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A device whose network interface is not Thread cannot be moved."""
    await setup_integration_with_node_fixture(
        hass,
        "eve_contact_sensor",
        matter_client,
        # Wi-Fi network interface
        override_attributes={"0/49/65532": 1},
    )

    with pytest.raises(ServiceValidationError) as exc_info:
        await _migrate(hass)

    assert exc_info.value.translation_key == "not_a_thread_device"


@pytest.mark.usefixtures("healthy_devices")
async def test_the_leader_is_moved_last(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A device without a reported role goes before the leader."""
    leader = clusters.ThreadNetworkDiagnostics.Enums.RoutingRoleEnum.kLeader
    await _setup_integration_with_nodes(
        hass,
        matter_client,
        [
            create_node_from_fixture(
                "eve_contact_sensor", {"0/53/1": leader}, override_serial=True
            ),
            create_node_from_fixture(
                "eve_energy_plug", {"0/53/1": NullValue}, override_serial=True
            ),
        ],
    )

    response = await _migrate_fleet(hass)

    assert [entry["node_id"] for entry in response["migrated"]] == [
        ENERGY_PLUG,
        CONTACT_SENSOR,
    ]
