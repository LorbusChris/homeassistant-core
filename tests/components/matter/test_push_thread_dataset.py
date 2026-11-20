"""Test handing a Thread network to a Matter border router."""

from base64 import b64encode
from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock, patch

from chip.clusters import Objects as clusters
from chip.clusters.Types import NullValue
from matter_server.client.exceptions import NotConnected
from matter_server.common.errors import UnknownError
import pytest

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.thread import async_add_dataset, dataset_store
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .common import set_node_attribute_and_notify, setup_integration_with_node_fixture

from tests.components.thread import DATASET_1

DATASET = bytes.fromhex("0e080000000000010000")
UNPROVISIONED = {"1/1106/4": NullValue}


@pytest.fixture(autouse=True)
def short_border_agent_discovery_timeout() -> Generator[None]:
    """Skip the store's border agent discovery wait."""
    with patch.object(dataset_store, "BORDER_AGENT_DISCOVERY_TIMEOUT", 0):
        yield


@pytest.fixture(autouse=True)
def mock_thread_discovery(mock_async_zeroconf: MagicMock) -> MagicMock:
    """Adding a dataset starts Thread discovery, which would open a real socket."""
    return mock_async_zeroconf


@pytest.fixture(name="preferred_network")
async def preferred_network_fixture(hass: HomeAssistant) -> None:
    """Have DATASET_1 as the preferred network."""
    await async_add_dataset(hass, "test", DATASET_1)
    store = await dataset_store.async_get_store(hass)
    store.preferred_dataset = next(iter(store.datasets))


def _device_id(hass: HomeAssistant) -> str:
    """Return the device id of the only Matter node."""
    return next(
        device.id
        for device in dr.async_get(hass).devices
        if any(identifier[0] == "matter" for identifier in device.identifiers)
    )


def _entity_id(hass: HomeAssistant, key: str) -> str:
    """Return the entity id of the entity with this schema key."""
    return next(
        entry.entity_id
        for entry in er.async_get(hass).entities.values()
        if entry.platform == "matter" and key in entry.unique_id
    )


def _commands(matter_client: MagicMock) -> list[Any]:
    """Return the commands sent to the device."""
    return [
        call.kwargs["command"]
        for call in matter_client.send_device_command.call_args_list
    ]


async def _push(hass: HomeAssistant, **data: str) -> None:
    """Call the push action for the border router."""
    await hass.services.async_call(
        "matter",
        "push_thread_dataset",
        {"device_id": _device_id(hass), **data},
        blocking=True,
    )


async def _get(hass: HomeAssistant) -> dict[str, Any]:
    """Call the get action for the border router."""
    response = await hass.services.async_call(
        "matter",
        "get_thread_dataset",
        {"device_id": _device_id(hass)},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    return response


async def test_provisioned_router_gets_pending_dataset(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A router with a network receives the dataset as pending."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )

    await _push(hass, dataset=DATASET.hex())

    commands = _commands(matter_client)
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
        hass, "thread_border_router", matter_client, override_attributes=UNPROVISIONED
    )

    await _push(hass, dataset=DATASET.hex())

    commands = _commands(matter_client)
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


async def test_the_preferred_network_is_pushed_by_default(
    hass: HomeAssistant, matter_client: MagicMock, preferred_network: None
) -> None:
    """Without a dataset, the preferred network is pushed."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )

    await _push(hass)

    assert _commands(matter_client)[0].pendingDataset == bytes.fromhex(DATASET_1)


async def test_no_dataset_and_no_preferred_network(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """Without a dataset or a preferred network, nothing is sent."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )

    with pytest.raises(ServiceValidationError) as exc_info:
        await _push(hass)

    assert exc_info.value.translation_key == "no_preferred_dataset"
    assert not matter_client.send_device_command.called


@pytest.mark.parametrize("dataset", ["not hex", ""])
async def test_invalid_dataset_is_rejected(
    hass: HomeAssistant, matter_client: MagicMock, dataset: str
) -> None:
    """A dataset that is not hex TLVs is rejected before anything is sent."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )

    with pytest.raises(ServiceValidationError) as exc_info:
        await _push(hass, dataset=dataset)

    assert exc_info.value.translation_key == "invalid_dataset"
    assert not matter_client.send_device_command.called


async def test_failed_adoption_disarms_the_failsafe(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """When the dataset write fails, the fail-safe is disarmed right away."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client, override_attributes=UNPROVISIONED
    )

    async def fail_dataset(command: Any, **kwargs: Any) -> None:
        if isinstance(
            command,
            clusters.ThreadBorderRouterManagement.Commands.SetActiveDatasetRequest,
        ):
            raise UnknownError("busy")

    matter_client.send_device_command.side_effect = fail_dataset

    with pytest.raises(HomeAssistantError) as exc_info:
        await _push(hass, dataset=DATASET.hex())

    assert exc_info.value.translation_key == "thread_dataset_push_failed"
    assert exc_info.value.translation_placeholders == {"error": "busy"}
    commands = _commands(matter_client)
    assert isinstance(commands[-1], clusters.GeneralCommissioning.Commands.ArmFailSafe)
    assert commands[-1].expiryLengthSeconds == 0


async def test_a_failed_disarm_keeps_the_write_error(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A disarm that fails too does not hide why the adoption failed."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client, override_attributes=UNPROVISIONED
    )
    matter_client.send_device_command.side_effect = [
        None,
        UnknownError("busy"),
        NotConnected(),
    ]

    with pytest.raises(HomeAssistantError) as exc_info:
        await _push(hass, dataset=DATASET.hex())

    assert exc_info.value.translation_placeholders == {"error": "busy"}


@pytest.mark.parametrize(
    ("responses", "error"),
    [
        ([{"errorCode": 4, "debugText": "busy"}], "ArmFailSafe returned error 4 busy"),
        ([None, None, {"errorCode": 1}], "CommissioningComplete returned error 1"),
    ],
)
async def test_a_failed_commissioning_step_is_reported(
    hass: HomeAssistant,
    matter_client: MagicMock,
    responses: list[dict[str, Any] | None],
    error: str,
) -> None:
    """An error code inside a commissioning response fails the push."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client, override_attributes=UNPROVISIONED
    )
    matter_client.send_device_command.side_effect = responses

    with pytest.raises(HomeAssistantError) as exc_info:
        await _push(hass, dataset=DATASET.hex())

    assert exc_info.value.translation_key == "thread_dataset_push_failed"
    assert exc_info.value.translation_placeholders == {"error": error}
    # A failed arm is not followed by a dataset write.
    assert len(_commands(matter_client)) == len(responses)


async def test_a_failed_pending_write_is_reported(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A pending dataset the server fails to write fails the push."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )
    matter_client.send_device_command.side_effect = NotConnected()

    with pytest.raises(HomeAssistantError) as exc_info:
        await _push(hass, dataset=DATASET.hex())

    assert exc_info.value.translation_placeholders == {"error": "NotConnected"}


async def test_unknown_device_is_rejected(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A stale device ID is user input, not a runtime failure."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )

    with pytest.raises(ServiceValidationError) as exc_info:
        await hass.services.async_call(
            "matter",
            "push_thread_dataset",
            {"device_id": "no-such-device", "dataset": DATASET.hex()},
            blocking=True,
        )

    assert exc_info.value.translation_key == "invalid_device"


async def test_a_device_without_a_border_router_is_rejected(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """Only a border router can be handed a network."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)

    with pytest.raises(ServiceValidationError) as exc_info:
        await _push(hass, dataset=DATASET.hex())

    assert exc_info.value.translation_key == "not_a_border_router"
    assert not matter_client.send_device_command.called


async def test_get_thread_dataset_returns_active(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """The get action returns the active dataset and no pending one."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )
    matter_client.send_device_command.return_value = {
        "dataset": b64encode(DATASET).decode()
    }

    assert await _get(hass) == {
        "active_dataset": DATASET.hex(),
        "active_dataset_timestamp": 1,
        "pending_dataset": None,
        "pending_dataset_timestamp": None,
    }
    # The pending dataset does not exist, so only the active one is read.
    commands = _commands(matter_client)
    assert len(commands) == 1
    assert isinstance(
        commands[0],
        clusters.ThreadBorderRouterManagement.Commands.GetActiveDatasetRequest,
    )


async def test_get_thread_dataset_returns_pending(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """While a migration is scheduled, the pending dataset is read too."""
    await setup_integration_with_node_fixture(
        hass,
        "thread_border_router",
        matter_client,
        override_attributes={"1/1106/4": NullValue, "1/1106/5": 2},
    )
    matter_client.send_device_command.return_value = {
        "dataset": b64encode(DATASET).decode()
    }

    assert await _get(hass) == {
        "active_dataset": None,
        "active_dataset_timestamp": None,
        "pending_dataset": DATASET.hex(),
        "pending_dataset_timestamp": 2,
    }
    commands = _commands(matter_client)
    assert len(commands) == 1
    assert isinstance(
        commands[0],
        clusters.ThreadBorderRouterManagement.Commands.GetPendingDatasetRequest,
    )


@pytest.mark.parametrize(
    ("side_effect", "translation_key"),
    [
        (NotConnected(), "thread_dataset_read_failed"),
        ([{"dataset": None}], "thread_dataset_unusable"),
    ],
)
async def test_a_failed_get_is_reported(
    hass: HomeAssistant,
    matter_client: MagicMock,
    side_effect: Exception | list[dict[str, None]],
    translation_key: str,
) -> None:
    """A read the server fails, or an unusable answer, fails the get action."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )
    matter_client.send_device_command.side_effect = side_effect

    with pytest.raises(HomeAssistantError) as exc_info:
        await _get(hass)

    assert exc_info.value.translation_key == translation_key


async def test_the_adopt_button_is_disabled_by_default(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, matter_client: MagicMock
) -> None:
    """Pressing the button reconfigures a network, so it starts disabled."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )

    entry = entity_registry.async_get(_entity_id(hass, "ThreadNetworkAdoptButton"))
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_the_adopt_button_pushes_the_preferred_network(
    hass: HomeAssistant, matter_client: MagicMock, preferred_network: None
) -> None:
    """The button adopts an unprovisioned router into the preferred network."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client, override_attributes=UNPROVISIONED
    )

    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: _entity_id(hass, "ThreadNetworkAdoptButton")},
        blocking=True,
    )

    assert _commands(matter_client)[1].activeDataset == bytes.fromhex(DATASET_1)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_the_adopt_button_without_a_preferred_network(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """Without a preferred network, the press fails and nothing is sent."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )

    with pytest.raises(HomeAssistantError) as exc_info:
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: _entity_id(hass, "ThreadNetworkAdoptButton")},
            blocking=True,
        )

    assert exc_info.value.translation_key == "no_preferred_dataset"
    assert not matter_client.send_device_command.called


async def test_migration_pending_sensor(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """The sensor is on while the router has a pending dataset."""
    node = await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )
    entity_id = _entity_id(hass, "ThreadMigrationPending")
    assert hass.states.get(entity_id).state == STATE_OFF

    await set_node_attribute_and_notify(
        hass, matter_client, node, endpoint=1, cluster_id=1106, attribute_id=5, value=2
    )
    assert hass.states.get(entity_id).state == STATE_ON

    await set_node_attribute_and_notify(
        hass,
        matter_client,
        node,
        endpoint=1,
        cluster_id=1106,
        attribute_id=5,
        value=NullValue,
    )
    assert hass.states.get(entity_id).state == STATE_OFF
