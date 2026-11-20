"""Move commissioned Thread devices onto another Thread network.

A Thread device keeps its network in the Network Commissioning cluster, so it
moves without leaving the fabric: stage the new dataset under an armed
fail-safe, connect, and complete. A device that cannot reach the new network
returns to its previous one when the fail-safe expires.
"""

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from chip.clusters import Objects as clusters
from chip.clusters.Types import NullValue
from matter_server.client.exceptions import MatterClientException
from matter_server.common.errors import MatterError
from matter_server.common.helpers.util import create_attribute_path
from python_otbr_api import tlv_parser

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import DOMAIN, LOGGER
from .thread_border_router import check_commissioning_response

if TYPE_CHECKING:
    from matter_server.client import MatterClient
    from matter_server.client.models.node import MatterEndpoint, MatterNode

# Covers staging, the attach, the controller finding the device again and
# CommissioningComplete, each of which a sleepy device can take minutes over:
# the longest fail-safe the spec allows.
FAIL_SAFE_EXPIRY = 900  # seconds
# Ends before the fail-safe does, so a reported failure is a device that
# returns to its previous network.
COMPLETE_DEADLINE = 840  # seconds
COMPLETE_RETRY_DELAY = 15  # seconds

MIGRATED = "migrated"
ALREADY_ON_NETWORK = "already_on_network"

_THREAD_FEATURE = clusters.NetworkCommissioning.Bitmaps.Feature.kThreadNetworkInterface
_STATUS = clusters.NetworkCommissioning.Enums.NetworkCommissioningStatusEnum
_ROLE = clusters.ThreadNetworkDiagnostics.Enums.RoutingRoleEnum

# End devices depend on routers of the network they leave, so they go first;
# the leader goes last.
_MIGRATION_ORDER: dict[int, int] = {
    _ROLE.kSleepyEndDevice: 0,
    _ROLE.kEndDevice: 0,
    _ROLE.kReed: 1,
    _ROLE.kRouter: 2,
    _ROLE.kLeader: 3,
}

_EXTENDED_PAN_ID_PATH = create_attribute_path(
    0,
    clusters.ThreadNetworkDiagnostics.id,
    clusters.ThreadNetworkDiagnostics.Attributes.ExtendedPanId.attribute_id,
)


def get_thread_network_endpoint(node: MatterNode) -> MatterEndpoint | None:
    """Return the root endpoint if it carries the device's Thread network."""
    endpoint = node.endpoints.get(0)
    if (
        endpoint is None
        or not endpoint.has_cluster(clusters.NetworkCommissioning)
        or not endpoint.has_cluster(clusters.ThreadNetworkDiagnostics)
    ):
        return None
    feature_map = endpoint.get_attribute_value(
        None, clusters.NetworkCommissioning.Attributes.FeatureMap
    )
    if not isinstance(feature_map, int) or not feature_map & _THREAD_FEATURE:
        return None
    return endpoint


def _network_id(value: Any) -> bytes | None:
    """Return an extended PAN ID as a network ID; None when absent, null or out of range."""
    if not isinstance(value, int) or not 0 <= value < 1 << 64:
        return None
    return value.to_bytes(8, "big")


def get_network_id(endpoint: MatterEndpoint) -> bytes | None:
    """Return the network ID of the network the device last reported."""
    return _network_id(
        endpoint.get_attribute_value(
            None, clusters.ThreadNetworkDiagnostics.Attributes.ExtendedPanId
        )
    )


def get_migration_order(endpoint: MatterEndpoint) -> int:
    """Return when to move the device among others: end devices first."""
    role = endpoint.get_attribute_value(
        None, clusters.ThreadNetworkDiagnostics.Attributes.RoutingRole
    )
    if role in (None, NullValue):
        return 0
    return _MIGRATION_ORDER.get(role, 1)


def network_id_from_dataset(dataset: bytes) -> bytes:
    """Return the network ID, the extended PAN ID, of a dataset."""
    try:
        entries = tlv_parser.parse_tlv(dataset.hex())
        return bytes(entries[tlv_parser.MeshcopTLVType.EXTPANID].data)
    except (KeyError, ValueError, tlv_parser.TLVError) as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="dataset_missing_extpanid"
        ) from err


def _migration_failed(error: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="thread_migration_failed",
        translation_placeholders={"error": error},
    )


def _check_network_response(response: Any, command: str, *accepted: int) -> None:
    """Raise when a Network Commissioning command reports failure.

    The status is in the response, so a failed step reads as success
    otherwise.
    """
    status = response.get("networkingStatus") if isinstance(response, dict) else None
    if status is not None and status not in (_STATUS.kSuccess, *accepted):
        detail = f"{command} returned status {status} {response.get('debugText') or ''}"
        raise _migration_failed(detail.strip())


async def _async_read_network_id(
    matter_client: MatterClient, node: MatterNode
) -> bytes | None:
    """Read the network ID from the device; None when it gives none."""
    try:
        values = await matter_client.read_attribute(node.node_id, _EXTENDED_PAN_ID_PATH)
    except MatterError, MatterClientException:
        return None
    return _network_id(
        values.get(_EXTENDED_PAN_ID_PATH) if isinstance(values, dict) else None
    )


async def async_migrate_device(
    matter_client: MatterClient, endpoint: MatterEndpoint, dataset: bytes
) -> str:
    """Move a Thread device onto the network of the dataset.

    Returns MIGRATED or ALREADY_ON_NETWORK. A device that fails after it was
    told to connect returns to its previous network when the fail-safe
    expires.
    """
    node = endpoint.node
    target = network_id_from_dataset(dataset)
    previous = get_network_id(endpoint)
    if previous == target:
        return ALREADY_ON_NETWORK
    # Thread diagnostics are reported lazily, so the cache can name a network
    # the device has left.
    if (live := await _async_read_network_id(matter_client, node)) is not None:
        if live == target:
            return ALREADY_ON_NETWORK
        previous = live

    commissioning = clusters.GeneralCommissioning.Commands
    network = clusters.NetworkCommissioning.Commands

    async def send(command: Any) -> Any:
        try:
            return await matter_client.send_device_command(
                node_id=node.node_id, endpoint_id=0, command=command
            )
        except (MatterError, MatterClientException) as err:
            raise _migration_failed(str(err) or err.__class__.__name__) from err

    response = await send(
        commissioning.ArmFailSafe(expiryLengthSeconds=FAIL_SAFE_EXPIRY, breadcrumb=1)
    )
    check_commissioning_response(response, "ArmFailSafe", "thread_migration_failed")
    try:
        if previous is not None:
            # A dataset for another network is refused while the old entry
            # exists. An entry already gone is a rerun.
            response = await send(
                network.RemoveNetwork(networkID=previous, breadcrumb=2)
            )
            _check_network_response(
                response, "RemoveNetwork", _STATUS.kNetworkIDNotFound
            )
        response = await send(
            network.AddOrUpdateThreadNetwork(operationalDataset=dataset, breadcrumb=3)
        )
        _check_network_response(response, "AddOrUpdateThreadNetwork")
        try:
            response = await send(
                network.ConnectNetwork(networkID=target, breadcrumb=4)
            )
        except HomeAssistantError:
            # The device detaches as it acts on this, so the answer may be lost.
            LOGGER.debug("No answer to ConnectNetwork from node %s", node.node_id)
        else:
            _check_network_response(response, "ConnectNetwork")
    except HomeAssistantError:
        # The device has not left its network: revert it now.
        with contextlib.suppress(HomeAssistantError):
            await send(commissioning.ArmFailSafe(expiryLengthSeconds=0))
        raise

    # The device is attaching to the new network. It is not disarmed from here
    # on: the fail-safe is what takes it back if it cannot be reached there.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + COMPLETE_DEADLINE
    while True:
        try:
            response = await matter_client.send_device_command(
                node_id=node.node_id,
                endpoint_id=0,
                command=commissioning.CommissioningComplete(),
            )
        except (MatterError, MatterClientException) as err:
            if loop.time() >= deadline:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="thread_migration_unreachable",
                    translation_placeholders={
                        "error": str(err) or err.__class__.__name__
                    },
                ) from err
            # Makes the controller look up the device's new address.
            with contextlib.suppress(MatterError, MatterClientException):
                await matter_client.ping_node(node.node_id)
            await asyncio.sleep(COMPLETE_RETRY_DELAY)
            continue
        check_commissioning_response(
            response, "CommissioningComplete", "thread_migration_failed"
        )
        return MIGRATED
