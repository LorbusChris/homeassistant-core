"""Test the Wi-Fi credentials import from Matter network managers."""

from base64 import b64encode
from unittest.mock import MagicMock

from chip.clusters import Objects as clusters
from chip.clusters.Types import NullValue
from matter_server.client.exceptions import NotConnected
from matter_server.client.models import device_types
from matter_server.common.errors import NodeNotReady, UnknownError
import pytest

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .common import setup_integration_with_node_fixture

SSID = "TestNetwork"
PASSPHRASE = "correct horse battery staple"


def _response(passphrase: bytes) -> dict[str, str]:
    """Return a NetworkPassphraseResponse as the client delivers it."""
    return {"passphrase": b64encode(passphrase).decode()}


def _passphrase_reads(matter_client: MagicMock) -> int:
    """Return how often the passphrase was requested."""
    return sum(
        isinstance(
            call.kwargs.get("command"),
            clusters.WiFiNetworkManagement.Commands.NetworkPassphraseRequest,
        )
        for call in matter_client.send_device_command.call_args_list
    )


def _import_button(hass: HomeAssistant) -> str | None:
    """Return the entity id of the import button, if there is one."""
    return next(
        (
            entry.entity_id
            for entry in er.async_get(hass).entities.values()
            if entry.platform == "matter"
            and "WifiCredentialsImportButton" in entry.unique_id
        ),
        None,
    )


async def _press_import(hass: HomeAssistant) -> None:
    """Press the import button."""
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: _import_button(hass)},
        blocking=True,
    )


@pytest.fixture(name="passphrase_response")
def passphrase_response_fixture(matter_client: MagicMock) -> dict[str, str]:
    """Make NetworkPassphraseRequest return PASSPHRASE."""
    response = _response(PASSPHRASE.encode())
    matter_client.send_device_command.return_value = response
    return response


async def test_manager_is_not_read_without_a_press(
    hass: HomeAssistant, matter_client: MagicMock, passphrase_response: dict[str, str]
) -> None:
    """The passphrase is privileged and only read when the user asks."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )
    await hass.async_block_till_done()

    assert _passphrase_reads(matter_client) == 0
    matter_client.set_wifi_credentials.assert_not_awaited()


async def test_pressing_the_button_imports_the_credentials(
    hass: HomeAssistant, matter_client: MagicMock, passphrase_response: dict[str, str]
) -> None:
    """The button reads the passphrase and hands the network to the server."""
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )

    await _press_import(hass)

    assert _passphrase_reads(matter_client) == 1
    assert matter_client.send_device_command.call_args.kwargs["node_id"] == 90
    assert matter_client.send_device_command.call_args.kwargs["endpoint_id"] == 1
    matter_client.set_wifi_credentials.assert_awaited_once_with(
        ssid=SSID, credentials=PASSPHRASE
    )


async def test_no_import_button_on_a_thread_border_router(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """Only a network infrastructure manager shares its Wi-Fi network."""
    await setup_integration_with_node_fixture(
        hass,
        "thread_border_router",
        matter_client,
        override_attributes={
            "1/29/0": [{"0": device_types.ThreadBorderRouter.device_type, "1": 1}]
        },
    )

    assert _import_button(hass) is None


async def test_no_import_button_without_the_cluster(
    hass: HomeAssistant, matter_client: MagicMock
) -> None:
    """A node without the Wi-Fi Network Management cluster has no import button."""
    await setup_integration_with_node_fixture(hass, "eve_contact_sensor", matter_client)

    assert _import_button(hass) is None


@pytest.mark.parametrize("ssid", [NullValue, ""])
async def test_a_manager_sharing_no_network_is_not_read(
    hass: HomeAssistant,
    matter_client: MagicMock,
    passphrase_response: dict[str, str],
    ssid: object,
) -> None:
    """Sharing switched off reads as no SSID; the press fails without a read."""
    await setup_integration_with_node_fixture(
        hass,
        "thread_border_router",
        matter_client,
        override_attributes={"1/1105/0": ssid},
    )

    with pytest.raises(HomeAssistantError) as exc_info:
        await _press_import(hass)

    assert exc_info.value.translation_key == "wifi_network_not_shared"
    assert _passphrase_reads(matter_client) == 0
    matter_client.set_wifi_credentials.assert_not_awaited()


@pytest.mark.parametrize(
    "response",
    [
        None,
        {"passphrase": None},
        {"passphrase": ""},
        {"passphrase": "%%%not-base64%%%"},
        _response(b"\xff\xfe"),
    ],
)
async def test_an_unusable_response_fails_the_press(
    hass: HomeAssistant,
    matter_client: MagicMock,
    response: dict[str, str | None] | None,
) -> None:
    """A response without a readable passphrase is reported, nothing imported."""
    matter_client.send_device_command.return_value = response
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )

    with pytest.raises(HomeAssistantError) as exc_info:
        await _press_import(hass)

    assert exc_info.value.translation_key == "wifi_credentials_unusable"
    matter_client.set_wifi_credentials.assert_not_awaited()


async def test_an_ssid_that_is_not_text_is_not_read(
    hass: HomeAssistant, matter_client: MagicMock, passphrase_response: dict[str, str]
) -> None:
    """The Matter server takes the SSID as text; another one fails the press."""
    await setup_integration_with_node_fixture(
        hass,
        "thread_border_router",
        matter_client,
        override_attributes={"1/1105/0": b64encode(b"\xff\xfe").decode()},
    )

    with pytest.raises(HomeAssistantError) as exc_info:
        await _press_import(hass)

    assert exc_info.value.translation_key == "wifi_credentials_unusable"
    assert _passphrase_reads(matter_client) == 0


@pytest.mark.parametrize(
    ("error", "placeholder"),
    [
        (NodeNotReady("node not ready"), "node not ready"),
        (UnknownError(), "UnknownError"),
        (NotConnected(), "NotConnected"),
    ],
)
async def test_a_failed_read_fails_the_press(
    hass: HomeAssistant,
    matter_client: MagicMock,
    error: Exception,
    placeholder: str,
) -> None:
    """A read the server fails is reported to the user, with the server's reason."""
    matter_client.send_device_command.side_effect = error
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )

    with pytest.raises(HomeAssistantError) as exc_info:
        await _press_import(hass)

    assert exc_info.value.translation_key == "wifi_credentials_import_failed"
    assert exc_info.value.translation_placeholders == {"error": placeholder}
    matter_client.set_wifi_credentials.assert_not_awaited()


async def test_a_failed_store_fails_the_press(
    hass: HomeAssistant, matter_client: MagicMock, passphrase_response: dict[str, str]
) -> None:
    """Credentials the server does not store are reported to the user."""
    matter_client.set_wifi_credentials.side_effect = UnknownError("store failed")
    await setup_integration_with_node_fixture(
        hass, "thread_border_router", matter_client
    )

    with pytest.raises(HomeAssistantError) as exc_info:
        await _press_import(hass)

    assert exc_info.value.translation_key == "wifi_credentials_import_failed"
    assert exc_info.value.translation_placeholders == {"error": "store failed"}
