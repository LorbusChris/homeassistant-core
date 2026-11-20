"""Wi-Fi credentials of Matter network infrastructure managers.

A manager with the Wi-Fi Network Management cluster hands the passphrase of
the network it runs to a fabric member with the Manage privilege. The user
imports it with the manager's button, for the Matter server to commission
Wi-Fi devices onto that network.
"""

from base64 import b64decode
from typing import TYPE_CHECKING, Any

from chip.clusters import Objects as clusters
from matter_server.client.exceptions import MatterClientException
from matter_server.common.errors import MatterError

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    from matter_server.client import MatterClient
    from matter_server.client.models.node import MatterEndpoint


def _passphrase_from_response(response: Any) -> str:
    """Return the passphrase of a NetworkPassphraseResponse; raise ValueError without one."""
    raw = response.get("passphrase") if isinstance(response, dict) else None
    if not isinstance(raw, str) or not (passphrase := b64decode(raw, validate=True)):
        raise ValueError("response carries no passphrase")
    return passphrase.decode()


async def async_import_credentials(
    matter_client: MatterClient, endpoint: MatterEndpoint
) -> None:
    """Hand the Wi-Fi network a manager runs to the Matter server."""
    ssid = endpoint.get_attribute_value(
        None, clusters.WiFiNetworkManagement.Attributes.Ssid
    )
    # Null while sharing is off or the manager runs no network.
    if not isinstance(ssid, bytes) or not ssid:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="wifi_network_not_shared"
        )
    try:
        network = ssid.decode()
        response: Any = await matter_client.send_device_command(
            node_id=endpoint.node.node_id,
            endpoint_id=endpoint.endpoint_id,
            command=clusters.WiFiNetworkManagement.Commands.NetworkPassphraseRequest(),
        )
        passphrase = _passphrase_from_response(response)
        await matter_client.set_wifi_credentials(ssid=network, credentials=passphrase)
    except ValueError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="wifi_credentials_unusable"
        ) from err
    except (MatterError, MatterClientException) as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="wifi_credentials_import_failed",
            translation_placeholders={"error": str(err) or err.__class__.__name__},
        ) from err

    LOGGER.debug(
        "Imported Wi-Fi credentials from node %s endpoint %s",
        endpoint.node.node_id,
        endpoint.endpoint_id,
    )
