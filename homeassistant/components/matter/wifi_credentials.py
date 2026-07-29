"""Import Wi-Fi credentials shared by Matter network infrastructure managers."""

from __future__ import annotations

from base64 import b64decode
from typing import TYPE_CHECKING, Any

from chip.clusters import Objects as clusters
from chip.clusters.Types import NullValue
from matter_server.client.models import device_types
from matter_server.common.helpers.util import create_attribute_path

from .const import LOGGER

if TYPE_CHECKING:
    from matter_server.client import MatterClient
    from matter_server.client.models.node import MatterEndpoint, MatterNode


# The cluster is part of the network infrastructure manager device type; a
# plain accessory has no business handing out the network's credentials.
CREDENTIAL_SOURCE_DEVICE_TYPES = {
    device_types.NetworkInfrastructureManager.device_type,
}


def get_wifi_endpoints(node: MatterNode) -> list[MatterEndpoint]:
    """Return the endpoints of a node that share Wi-Fi credentials."""
    return [
        endpoint
        for endpoint in node.endpoints.values()
        if endpoint.has_cluster(clusters.WiFiNetworkManagement)
        and any(
            device_type.device_type in CREDENTIAL_SOURCE_DEVICE_TYPES
            for device_type in endpoint.device_types
        )
    ]


def get_passphrase_surrogate_path(endpoint: MatterEndpoint) -> str:
    """Return the attribute path signalling a changed passphrase."""
    return create_attribute_path(
        endpoint.endpoint_id,
        clusters.WiFiNetworkManagement.id,
        clusters.WiFiNetworkManagement.Attributes.PassphraseSurrogate.attribute_id,
    )


def get_passphrase_surrogate(endpoint: MatterEndpoint) -> int | None:
    """Return the surrogate identifying the currently shared passphrase.

    The surrogate changes whenever the passphrase does. It exists so that a
    client can tell a changed credential from an unchanged one without asking
    for the passphrase itself, which is a privileged operation.
    """
    surrogate: int | None = endpoint.get_attribute_value(
        None, clusters.WiFiNetworkManagement.Attributes.PassphraseSurrogate
    )
    return surrogate


def get_ssid(endpoint: MatterEndpoint) -> bytes | None:
    """Return the SSID shared by this endpoint, if any."""
    ssid: bytes | None = endpoint.get_attribute_value(
        None, clusters.WiFiNetworkManagement.Attributes.Ssid
    )
    if ssid in (None, NullValue):
        return None
    return ssid


def _passphrase_from_response(response: Any) -> bytes | None:
    """Return the passphrase from a NetworkPassphraseResponse.

    The Matter client hands back command responses as plain dicts, with octet
    strings base64 encoded rather than as bytes. Object and bytes forms are
    still accepted so this keeps working if that representation changes.
    """
    if isinstance(response, dict):
        raw = response.get("passphrase")
    else:
        raw = getattr(response, "passphrase", None)
    if isinstance(raw, str):
        try:
            return b64decode(raw)
        except ValueError:
            LOGGER.debug("Network manager returned an undecodable passphrase")
            return None
    if isinstance(raw, bytes):
        return raw
    return None


async def async_import_credentials(
    matter_client: MatterClient, endpoint: MatterEndpoint
) -> None:
    """Store the Wi-Fi credentials this endpoint shares for commissioning.

    A network infrastructure manager offers the credentials of the network it
    runs, so a Wi-Fi device can be commissioned onto it without asking the
    user for a password they have already given the router.
    """
    ssid = get_ssid(endpoint)
    if not ssid:
        # Sharing is off, or the manager runs no access point of its own.
        LOGGER.debug(
            "Node %s endpoint %s shares no Wi-Fi credentials",
            endpoint.node.node_id,
            endpoint.endpoint_id,
        )
        return

    response: Any = await matter_client.send_device_command(
        node_id=endpoint.node.node_id,
        endpoint_id=endpoint.endpoint_id,
        command=clusters.WiFiNetworkManagement.Commands.NetworkPassphraseRequest(),
    )
    passphrase = _passphrase_from_response(response)

    if not passphrase:
        LOGGER.debug(
            "Node %s endpoint %s returned no passphrase",
            endpoint.node.node_id,
            endpoint.endpoint_id,
        )
        return

    await matter_client.set_wifi_credentials(
        ssid=ssid.decode("utf-8", "replace"),
        credentials=passphrase.decode("utf-8", "replace"),
    )

    LOGGER.debug(
        "Imported Wi-Fi credentials from node %s endpoint %s",
        endpoint.node.node_id,
        endpoint.endpoint_id,
    )
