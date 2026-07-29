"""Test importing Wi-Fi credentials from Matter network managers."""

from base64 import b64encode
from unittest.mock import MagicMock

from chip.clusters import Objects as clusters
from chip.clusters.Types import NullValue
from matter_server.common.models import EventType
import pytest

from homeassistant.core import HomeAssistant

from .common import (
    set_node_attribute,
    setup_integration_with_node_fixture,
    trigger_subscription_callback,
)

SSID = "TestNetwork"
PASSPHRASE = "correct horse battery staple"

# Wi-Fi Network Management attributes on the manager endpoint.
SSID_ATTRIBUTE = 0
SURROGATE_ATTRIBUTE = 1


@pytest.fixture(name="passphrase_response")
def passphrase_response_fixture(matter_client: MagicMock) -> dict[str, str]:
    """Make NetworkPassphraseRequest return a passphrase.

    The Matter client returns command responses as dicts with octet strings
    base64 encoded, which is what a real network manager produces.
    """
    response = {"passphrase": b64encode(PASSPHRASE.encode()).decode()}
    matter_client.send_device_command.return_value = response
    return response


def passphrase_requests(matter_client: MagicMock) -> int:
    """Return how often the passphrase was asked for."""
    return sum(
        isinstance(
            call.kwargs.get("command"),
            clusters.WiFiNetworkManagement.Commands.NetworkPassphraseRequest,
        )
        for call in matter_client.send_device_command.call_args_list
    )


async def test_credentials_imported_from_network_manager(
    hass: HomeAssistant, matter_client: MagicMock, passphrase_response: dict[str, str]
) -> None:
    """A network manager's Wi-Fi credentials are handed to the Matter server."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )
    await hass.async_block_till_done()

    assert passphrase_requests(matter_client) == 1
    matter_client.set_wifi_credentials.assert_awaited_with(
        ssid=SSID, credentials=PASSPHRASE
    )


async def test_credentials_not_imported_without_ssid(
    hass: HomeAssistant, matter_client: MagicMock, passphrase_response: dict[str, str]
) -> None:
    """Sharing switched off reads as a null SSID and must not be pursued."""
    await setup_integration_with_node_fixture(
        hass,
        "thread_border_router",
        matter_client,
        override_attributes={"1/1105/0": NullValue},
    )
    await hass.async_block_till_done()

    # The passphrase is privileged; it must not be requested with nothing shared.
    assert passphrase_requests(matter_client) == 0
    matter_client.set_wifi_credentials.assert_not_awaited()


async def test_empty_passphrase_is_not_imported(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A manager that answers without a passphrase is skipped."""
    matter_client.send_device_command.return_value = {"passphrase": ""}

    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )
    await hass.async_block_till_done()

    matter_client.set_wifi_credentials.assert_not_awaited()


async def test_plain_node_is_ignored(
    hass: HomeAssistant, matter_client: MagicMock, passphrase_response: dict[str, str]
) -> None:
    """A node without the cluster never triggers a passphrase read."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)
    await hass.async_block_till_done()

    assert passphrase_requests(matter_client) == 0
    matter_client.set_wifi_credentials.assert_not_awaited()


async def test_passphrase_not_reread_when_surrogate_unchanged(
    hass: HomeAssistant, matter_client: MagicMock, passphrase_response: dict[str, str]
) -> None:
    """The surrogate exists so the privileged read only happens on a change."""
    node = await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )
    await hass.async_block_till_done()

    assert passphrase_requests(matter_client) == 1

    await trigger_subscription_callback(
        hass, matter_client, EventType.NODE_UPDATED, node
    )
    await hass.async_block_till_done()

    assert passphrase_requests(matter_client) == 1


async def test_changed_surrogate_triggers_reimport(
    hass: HomeAssistant, matter_client: MagicMock, passphrase_response: dict[str, str]
) -> None:
    """A password changed on the router bumps the surrogate and is picked up."""
    node = await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )
    await hass.async_block_till_done()

    assert passphrase_requests(matter_client) == 1

    new_passphrase = "hunter2hunter2"
    matter_client.send_device_command.return_value = {
        "passphrase": b64encode(new_passphrase.encode()).decode()
    }
    set_node_attribute(node, 1, 1105, SURROGATE_ATTRIBUTE, 1785200999999)
    await trigger_subscription_callback(
        hass,
        matter_client,
        EventType.ATTRIBUTE_UPDATED,
        data=(node.node_id, "1/1105/1", 1785200999999),
    )
    await hass.async_block_till_done()

    assert passphrase_requests(matter_client) == 2
    matter_client.set_wifi_credentials.assert_awaited_with(
        ssid=SSID, credentials=new_passphrase
    )
