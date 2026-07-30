"""Test handing a Thread network to a Matter border router."""

from base64 import b64encode
from unittest.mock import MagicMock

from chip.clusters import Objects as clusters
from chip.clusters.Types import NullValue
from matter_server.common.errors import MatterError
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .common import setup_integration_with_node_fixture

DATASET = bytes.fromhex("0e080000000000010000")


def get_border_router_device(hass: HomeAssistant) -> dr.DeviceEntry:
    """Return the device entry of the border router node."""
    device_registry = dr.async_get(hass)
    return next(
        device
        for device in device_registry.devices.values()
        if any(identifier[0] == "matter" for identifier in device.identifiers)
    )


def entity_id_by_key(hass: HomeAssistant, key: str) -> str:
    """Return the entity id of the discovered entity with this schema key."""
    entity_registry = er.async_get(hass)
    return next(
        entry.entity_id
        for entry in entity_registry.entities.values()
        if key in entry.unique_id
    )


def sent_commands(matter_client: MagicMock) -> list:
    """Return the commands sent to the device."""
    return [
        c.kwargs["command"] for c in matter_client.send_device_command.call_args_list
    ]


async def test_provisioned_router_gets_pending_dataset(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A router with a running network receives the dataset as a migration."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )
    await hass.async_block_till_done()
    matter_client.send_device_command.reset_mock(return_value=True, side_effect=True)
    matter_client.send_device_command.return_value = None

    await hass.services.async_call(
        "matter",
        "push_thread_dataset",
        {
            "device_id": get_border_router_device(hass).id,
            "dataset": DATASET.hex(),
        },
        blocking=True,
    )

    commands = sent_commands(matter_client)
    assert len(commands) == 1
    assert isinstance(
        commands[0],
        clusters.ThreadBorderRouterManagement.Commands.SetPendingDatasetRequest,
    )
    assert commands[0].pendingDataset == DATASET


async def test_unprovisioned_router_is_adopted_under_failsafe(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A router without a network is provisioned inside a fail-safe window."""
    await setup_integration_with_node_fixture(
        hass,
        "thread_border_router",
        matter_client,
        override_attributes={"1/1106/4": NullValue},
    )
    await hass.async_block_till_done()
    matter_client.send_device_command.reset_mock(return_value=True, side_effect=True)
    matter_client.send_device_command.return_value = None

    await hass.services.async_call(
        "matter",
        "push_thread_dataset",
        {
            "device_id": get_border_router_device(hass).id,
            "dataset": DATASET.hex(),
        },
        blocking=True,
    )

    commands = sent_commands(matter_client)
    assert len(commands) == 3
    assert isinstance(commands[0], clusters.GeneralCommissioning.Commands.ArmFailSafe)
    assert commands[0].expiryLengthSeconds == 120
    assert isinstance(
        commands[1],
        clusters.ThreadBorderRouterManagement.Commands.SetActiveDatasetRequest,
    )
    assert commands[1].activeDataset == DATASET
    assert isinstance(
        commands[2], clusters.GeneralCommissioning.Commands.CommissioningComplete
    )


async def test_failed_adoption_disarms_the_failsafe(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """When provisioning fails, the fail-safe is disarmed right away."""
    await setup_integration_with_node_fixture(
        hass,
        "thread_border_router",
        matter_client,
        override_attributes={"1/1106/4": NullValue},
    )
    await hass.async_block_till_done()
    matter_client.send_device_command.reset_mock(return_value=True, side_effect=True)
    matter_client.send_device_command.return_value = None

    async def fail_dataset(node_id, endpoint_id, command, **kwargs):
        if isinstance(
            command,
            clusters.ThreadBorderRouterManagement.Commands.SetActiveDatasetRequest,
        ):
            raise MatterError("busy")

    matter_client.send_device_command.side_effect = fail_dataset

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "matter",
            "push_thread_dataset",
            {
                "device_id": get_border_router_device(hass).id,
                "dataset": DATASET.hex(),
            },
            blocking=True,
        )

    commands = sent_commands(matter_client)
    assert isinstance(commands[-1], clusters.GeneralCommissioning.Commands.ArmFailSafe)
    assert commands[-1].expiryLengthSeconds == 0


async def test_invalid_dataset_is_rejected(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A dataset that is not hex TLVs is rejected before anything is sent."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )
    await hass.async_block_till_done()
    matter_client.send_device_command.reset_mock(return_value=True, side_effect=True)
    matter_client.send_device_command.return_value = None

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "matter",
            "push_thread_dataset",
            {
                "device_id": get_border_router_device(hass).id,
                "dataset": "not hex",
            },
            blocking=True,
        )
    assert not matter_client.send_device_command.called


async def test_failed_commissioning_step_is_reported(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """An error code inside the ArmFailSafe response surfaces as a failure."""
    await setup_integration_with_node_fixture(
        hass,
        "thread_border_router",
        matter_client,
        override_attributes={"1/1106/4": NullValue},
    )
    await hass.async_block_till_done()
    matter_client.send_device_command.reset_mock(return_value=True, side_effect=True)
    matter_client.send_device_command.return_value = {
        "errorCode": 1,
        "debugText": "busy",
    }

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "matter",
            "push_thread_dataset",
            {
                "device_id": get_border_router_device(hass).id,
                "dataset": DATASET.hex(),
            },
            blocking=True,
        )

    # The failed arm must not be followed by a dataset write.
    commands = sent_commands(matter_client)
    assert not any(
        isinstance(
            c, clusters.ThreadBorderRouterManagement.Commands.SetActiveDatasetRequest
        )
        for c in commands
    )


async def test_unknown_device_is_rejected(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A stale device ID is user input, not a runtime failure."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "matter",
            "push_thread_dataset",
            {"device_id": "no-such-device", "dataset": DATASET.hex()},
            blocking=True,
        )


async def test_get_thread_dataset_returns_active(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """The get action returns the active dataset and no pending one."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )
    await hass.async_block_till_done()
    matter_client.send_device_command.reset_mock(return_value=True, side_effect=True)
    matter_client.send_device_command.return_value = {
        "dataset": b64encode(DATASET).decode()
    }

    response = await hass.services.async_call(
        "matter",
        "get_thread_dataset",
        {"device_id": get_border_router_device(hass).id},
        blocking=True,
        return_response=True,
    )

    assert response == {
        "active_dataset": DATASET.hex(),
        "pending_dataset": None,
        "active_dataset_timestamp": 1,
        "pending_dataset_timestamp": None,
    }
    # Only the active dataset was fetched; the pending one does not exist.
    commands = sent_commands(matter_client)
    assert len(commands) == 1
    assert isinstance(
        commands[0],
        clusters.ThreadBorderRouterManagement.Commands.GetActiveDatasetRequest,
    )


async def test_import_wifi_credentials_button(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """The device page button imports this router's credentials."""
    matter_client.send_device_command.return_value = {
        "passphrase": b64encode(b"correct horse battery staple").decode()
    }
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )
    await hass.async_block_till_done()
    matter_client.set_wifi_credentials.reset_mock()

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id_by_key(hass, "ImportWifiCredentialsButton")},
        blocking=True,
    )

    matter_client.set_wifi_credentials.assert_awaited_with(
        ssid="TestNetwork", credentials="correct horse battery staple"
    )


async def test_migration_pending_sensor(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """The migration sensor reads off while no migration is scheduled."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )
    await hass.async_block_till_done()

    state = hass.states.get(entity_id_by_key(hass, "ThreadMigrationPending"))
    assert state is not None
    assert state.state == "off"
