"""Test migrating Thread devices onto another Thread network."""

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

# EXTPANID 1111111122222222 — the network the devices are asked to join.
DATASET = bytes.fromhex(
    "0e080000000003e90000000300000f35060004001fffe002081111111122222222"
)
TARGET_ID = bytes.fromhex("1111111122222222")
# The eve_contact_sensor fixture reports ExtendedPanId 5980345540157460411.
OLD_ID = bytes.fromhex("52fe7495632d4fbb")


def device_id_for_node(hass: HomeAssistant, node_id: int) -> str:
    """Return the HA device id of the device standing for a node."""
    device_registry = dr.async_get(hass)
    return next(
        device.id
        for device in device_registry.devices.values()
        if any(
            identifier[0] == "matter" and f"-{node_id:016X}-" in identifier[1]
            for identifier in device.identifiers
        )
    )


def sent_commands(matter_client: MagicMock) -> list:
    """Return the commands sent to devices."""
    return [
        c.kwargs["command"] for c in matter_client.send_device_command.call_args_list
    ]


def respond_ok(matter_client: MagicMock) -> None:
    """Answer every command the way a healthy device would."""

    async def responder(node_id, endpoint_id, command, **kwargs):
        if isinstance(
            command,
            (
                clusters.GeneralCommissioning.Commands.ArmFailSafe,
                clusters.GeneralCommissioning.Commands.CommissioningComplete,
            ),
        ):
            return {"errorCode": 0}
        return {"networkingStatus": 0}

    matter_client.send_device_command.reset_mock(return_value=True, side_effect=True)
    matter_client.send_device_command.side_effect = responder


async def migrate(hass: HomeAssistant, device_id: str) -> dict:
    """Invoke the single-device migration action."""
    return await hass.services.async_call(
        "matter",
        "migrate_thread_device",
        {"device_id": device_id, "dataset": DATASET.hex()},
        blocking=True,
        return_response=True,
    )


async def test_device_is_migrated(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """The full command sequence runs and reports the migration."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)
    await hass.async_block_till_done()
    device_id = device_id_for_node(hass, 9)
    respond_ok(matter_client)

    response = await migrate(hass, device_id)

    assert response == {"result": "migrated"}
    commands = sent_commands(matter_client)
    assert len(commands) == 5
    assert isinstance(commands[0], clusters.GeneralCommissioning.Commands.ArmFailSafe)
    assert commands[0].expiryLengthSeconds == 900
    assert isinstance(commands[1], clusters.NetworkCommissioning.Commands.RemoveNetwork)
    assert commands[1].networkID == OLD_ID
    assert isinstance(
        commands[2], clusters.NetworkCommissioning.Commands.AddOrUpdateThreadNetwork
    )
    assert commands[2].operationalDataset == DATASET
    assert isinstance(
        commands[3], clusters.NetworkCommissioning.Commands.ConnectNetwork
    )
    assert commands[3].networkID == TARGET_ID
    assert isinstance(
        commands[4], clusters.GeneralCommissioning.Commands.CommissioningComplete
    )


async def test_stale_cache_is_corrected_by_live_read(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A device whose cache lies about its network is not reconfigured."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)
    await hass.async_block_till_done()
    device_id = device_id_for_node(hass, 9)
    respond_ok(matter_client)
    # The cache says the old network, the device says the target.
    matter_client.read_attribute.reset_mock(return_value=True, side_effect=True)
    matter_client.read_attribute.return_value = {
        "0/53/4": int.from_bytes(TARGET_ID, "big")
    }

    response = await migrate(hass, device_id)

    assert response == {"result": "already_on_network"}
    assert not matter_client.send_device_command.called


async def test_device_already_on_network(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A device already on the target network is left untouched."""
    await setup_integration_with_node_fixture(
        hass,
        "eve_contact_sensor",
        matter_client,
        override_attributes={"0/53/4": int.from_bytes(TARGET_ID, "big")},
    )
    await hass.async_block_till_done()
    device_id = device_id_for_node(hass, 9)
    respond_ok(matter_client)

    response = await migrate(hass, device_id)

    assert response == {"result": "already_on_network"}
    assert not matter_client.send_device_command.called


async def test_border_router_is_refused(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A border router is the network, not a device to move onto one."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )
    await hass.async_block_till_done()
    device_id = device_id_for_node(hass, 90)
    respond_ok(matter_client)

    with pytest.raises(ServiceValidationError):
        await migrate(hass, device_id)
    assert not matter_client.send_device_command.called


async def test_unknown_old_network_skips_remove(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """Without a known previous network there is nothing to remove."""
    await setup_integration_with_node_fixture(
        hass,
        "eve_contact_sensor",
        matter_client,
        override_attributes={"0/53/4": NullValue},
    )
    await hass.async_block_till_done()
    device_id = device_id_for_node(hass, 9)
    respond_ok(matter_client)

    response = await migrate(hass, device_id)

    assert response == {"result": "migrated"}
    assert not any(
        isinstance(c, clusters.NetworkCommissioning.Commands.RemoveNetwork)
        for c in sent_commands(matter_client)
    )


async def test_lost_connect_response_is_survived(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """The device detaches on ConnectNetwork; a lost answer is not a failure."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)
    await hass.async_block_till_done()
    device_id = device_id_for_node(hass, 9)
    respond_ok(matter_client)
    inner = matter_client.send_device_command.side_effect
    complete_failures = iter([True])

    async def responder(node_id, endpoint_id, command, **kwargs):
        if isinstance(command, clusters.NetworkCommissioning.Commands.ConnectNetwork):
            raise MatterError("device is switching networks")
        if isinstance(
            command, clusters.GeneralCommissioning.Commands.CommissioningComplete
        ) and next(complete_failures, False):
            raise MatterError("not reachable yet")
        return await inner(node_id, endpoint_id, command, **kwargs)

    matter_client.send_device_command.side_effect = responder

    with patch(
        "homeassistant.components.matter.thread_migration.COMPLETE_RETRY_DELAY_S", 0
    ):
        response = await migrate(hass, device_id)

    assert response == {"result": "migrated"}


async def test_connect_refusal_disarms(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """An explicit ConnectNetwork refusal disarms; the device never left."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)
    await hass.async_block_till_done()
    device_id = device_id_for_node(hass, 9)
    respond_ok(matter_client)
    inner = matter_client.send_device_command.side_effect

    async def responder(node_id, endpoint_id, command, **kwargs):
        if isinstance(command, clusters.NetworkCommissioning.Commands.ConnectNetwork):
            return {"networkingStatus": 9, "errorValue": -5}
        return await inner(node_id, endpoint_id, command, **kwargs)

    matter_client.send_device_command.side_effect = responder

    with pytest.raises(HomeAssistantError):
        await migrate(hass, device_id)
    commands = sent_commands(matter_client)
    assert isinstance(commands[-1], clusters.GeneralCommissioning.Commands.ArmFailSafe)
    assert commands[-1].expiryLengthSeconds == 0


async def test_staging_refusal_disarms(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A refused AddOrUpdateThreadNetwork disarms the fail-safe."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)
    await hass.async_block_till_done()
    device_id = device_id_for_node(hass, 9)
    respond_ok(matter_client)
    inner = matter_client.send_device_command.side_effect

    async def responder(node_id, endpoint_id, command, **kwargs):
        if isinstance(
            command, clusters.NetworkCommissioning.Commands.AddOrUpdateThreadNetwork
        ):
            return {"networkingStatus": 2}
        return await inner(node_id, endpoint_id, command, **kwargs)

    matter_client.send_device_command.side_effect = responder

    with pytest.raises(HomeAssistantError):
        await migrate(hass, device_id)
    commands = sent_commands(matter_client)
    assert isinstance(commands[-1], clusters.GeneralCommissioning.Commands.ArmFailSafe)
    assert commands[-1].expiryLengthSeconds == 0


async def test_unreachable_after_connect_reports_revert(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """After ConnectNetwork a dead device is reported, never disarmed."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)
    await hass.async_block_till_done()
    device_id = device_id_for_node(hass, 9)
    respond_ok(matter_client)
    inner = matter_client.send_device_command.side_effect

    async def responder(node_id, endpoint_id, command, **kwargs):
        if isinstance(
            command, clusters.GeneralCommissioning.Commands.CommissioningComplete
        ):
            raise MatterError("gone")
        return await inner(node_id, endpoint_id, command, **kwargs)

    matter_client.send_device_command.side_effect = responder

    with (
        patch(
            "homeassistant.components.matter.thread_migration.COMPLETE_RETRY_DELAY_S", 0
        ),
        patch(
            "homeassistant.components.matter.thread_migration.COMPLETE_DEADLINE_S", 0
        ),
        pytest.raises(HomeAssistantError) as excinfo,
    ):
        await migrate(hass, device_id)

    assert "revert" in (excinfo.value.translation_placeholders or {}).get("details", "")
    assert not any(
        isinstance(c, clusters.GeneralCommissioning.Commands.ArmFailSafe)
        and c.expiryLengthSeconds == 0
        for c in sent_commands(matter_client)
    )


async def test_fleet_orders_and_reports(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """End devices go first, border routers are excluded, results are bucketed."""
    nodes = [
        create_node_from_fixture("thread_border_router", override_serial=True),
        create_node_from_fixture("eve_energy_plug", override_serial=True),
        create_node_from_fixture("eve_contact_sensor", override_serial=True),
    ]
    await _setup_integration_with_nodes(hass, matter_client, nodes)
    await hass.async_block_till_done()
    respond_ok(matter_client)

    response = await hass.services.async_call(
        "matter",
        "migrate_thread_fleet",
        {"dataset": DATASET.hex()},
        blocking=True,
        return_response=True,
    )

    migrated_ids = [entry["node_id"] for entry in response["migrated"]]
    # Contact sensor (sleepy end device, node 9) before energy plug (router, 61).
    assert migrated_ids == [9, 61]
    assert response["failed"] == []
    assert all(entry["node_id"] != 90 for entry in response["skipped"])
    # Both devices went through the full sequence.
    arm_commands = [
        c
        for c in sent_commands(matter_client)
        if isinstance(c, clusters.GeneralCommissioning.Commands.ArmFailSafe)
    ]
    assert len(arm_commands) == 2


async def test_fleet_continues_after_failure(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """One failing device is reported and does not stop the fleet."""
    nodes = [
        create_node_from_fixture("eve_energy_plug", override_serial=True),
        create_node_from_fixture("eve_contact_sensor", override_serial=True),
    ]
    await _setup_integration_with_nodes(hass, matter_client, nodes)
    await hass.async_block_till_done()
    respond_ok(matter_client)
    inner = matter_client.send_device_command.side_effect

    async def responder(node_id, endpoint_id, command, **kwargs):
        if node_id == 9 and isinstance(
            command, clusters.GeneralCommissioning.Commands.ArmFailSafe
        ):
            return {"errorCode": 4, "debugText": "busy"}
        return await inner(node_id, endpoint_id, command, **kwargs)

    matter_client.send_device_command.side_effect = responder

    response = await hass.services.async_call(
        "matter",
        "migrate_thread_fleet",
        {"dataset": DATASET.hex()},
        blocking=True,
        return_response=True,
    )

    assert [entry["node_id"] for entry in response["migrated"]] == [61]
    assert [entry["node_id"] for entry in response["failed"]] == [9]


async def test_dataset_without_extpanid_is_rejected(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A dataset that names no network is rejected before any command."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)
    await hass.async_block_till_done()
    device_id = device_id_for_node(hass, 9)
    respond_ok(matter_client)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "matter",
            "migrate_thread_device",
            {"device_id": device_id, "dataset": "0e080000000003e90000"},
            blocking=True,
            return_response=True,
        )
    assert not matter_client.send_device_command.called
