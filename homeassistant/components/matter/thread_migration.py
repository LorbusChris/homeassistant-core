"""Migrate commissioned Thread devices onto another Thread network.

A Thread Matter device carries its network in the Network Commissioning
cluster, so it can be moved to a different network without touching its
fabric membership: stage the new dataset under an armed fail-safe, connect,
and complete. The fail-safe is the safety net — a device that cannot reach
the new network reverts to its previous one by itself when the fail-safe
expires without CommissioningComplete.
"""

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from chip.clusters import Objects as clusters
from chip.clusters.Types import NullValue
from matter_server.common.errors import MatterError
from python_otbr_api import tlv_parser

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, LOGGER
from .thread_border_router import check_commissioning_response

if TYPE_CHECKING:
    from matter_server.client import MatterClient
    from matter_server.client.models.node import MatterEndpoint, MatterNode

# The fail-safe must outlive staging, the attach to the new network, the
# controller re-resolving the device there, and CommissioningComplete —
# and a sleepy device may take many minutes over each step, so use the
# whole window the spec allows.
FAILSAFE_EXPIRY_S = 900

# How long CommissioningComplete is retried while the device re-attaches,
# and how long to pause between attempts. The deadline stays clear of the
# fail-safe expiry so a failure report is unambiguous about the outcome.
COMPLETE_DEADLINE_S = 840
COMPLETE_RETRY_DELAY_S = 15

_THREAD_FEATURE = clusters.NetworkCommissioning.Bitmaps.Feature.kThreadNetworkInterface

# Least-disruptive-first: end devices depend on parents in the network they
# are leaving, so they go before the routers they depend on; the old leader
# causes the biggest upheaval and goes last.
_ROLE = clusters.ThreadNetworkDiagnostics.Enums.RoutingRoleEnum
MIGRATION_ORDER: dict[int, int] = {
    _ROLE.kSleepyEndDevice: 0,
    _ROLE.kEndDevice: 0,
    _ROLE.kReed: 1,
    _ROLE.kRouter: 2,
    _ROLE.kLeader: 3,
}


def get_thread_network_endpoint(node: MatterNode) -> MatterEndpoint | None:
    """Return the endpoint carrying the device's Thread network, if any.

    That is the root endpoint with a Network Commissioning cluster whose
    feature map declares the Thread interface, plus the Thread diagnostics
    that report which network the device is on.
    """
    endpoint = node.endpoints.get(0)
    if endpoint is None:
        return None
    if not endpoint.has_cluster(
        clusters.NetworkCommissioning
    ) or not endpoint.has_cluster(clusters.ThreadNetworkDiagnostics):
        return None
    feature_map = endpoint.get_attribute_value(
        None, clusters.NetworkCommissioning.Attributes.FeatureMap
    )
    if not feature_map or not feature_map & _THREAD_FEATURE:
        return None
    return endpoint


def get_extended_pan_id(endpoint: MatterEndpoint) -> int | None:
    """Return the extended PAN ID of the network the device is on."""
    value = endpoint.get_attribute_value(
        None, clusters.ThreadNetworkDiagnostics.Attributes.ExtendedPanId
    )
    if value in (None, NullValue):
        return None
    return int(value)


def get_routing_role(endpoint: MatterEndpoint) -> int:
    """Return the device's Thread routing role, defaulting to end device."""
    value = endpoint.get_attribute_value(
        None, clusters.ThreadNetworkDiagnostics.Attributes.RoutingRole
    )
    if value in (None, NullValue):
        return _ROLE.kEndDevice
    return int(value)


def network_id_from_dataset(dataset: bytes) -> bytes:
    """Return the network ID (the extended PAN ID) of a dataset."""
    try:
        entries = tlv_parser.parse_tlv(dataset.hex())
        ext_pan_id = entries[tlv_parser.MeshcopTLVType.EXTPANID].data
    except (KeyError, ValueError, tlv_parser.TLVError) as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="dataset_missing_extpanid"
        ) from err
    return bytes(ext_pan_id)


def check_network_response(
    response: Any, step: str, ok: tuple[int, ...] = (0,)
) -> None:
    """Raise when a network commissioning command reports failure.

    The commands answer with a networking status inside the response rather
    than an interaction status, so a failed step otherwise reads as success.
    """
    if isinstance(response, dict):
        status = response.get("networkingStatus")
        text = response.get("debugText") or ""
    else:
        status = getattr(response, "networkingStatus", None)
        text = getattr(response, "debugText", None) or ""
    if status is None or status in ok:
        return
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="device_command_failed",
        translation_placeholders={
            "details": f"{step} failed with networking status {status} {text}".strip()
        },
    )


async def async_get_live_extended_pan_id(
    matter_client: MatterClient, node: MatterNode
) -> int | None:
    """Read the extended PAN ID from the device rather than the cache.

    Thread diagnostics are lazily reported, so the cache can keep naming a
    network the device has long left; the read also refreshes the cache.
    Returns None when the device does not answer or reports null.
    """
    path = f"0/{clusters.ThreadNetworkDiagnostics.id}/{clusters.ThreadNetworkDiagnostics.Attributes.ExtendedPanId.attribute_id}"
    try:
        values = await matter_client.read_attribute(node.node_id, path)
    except MatterError:
        return None
    value = values.get(path) if isinstance(values, dict) else None
    if value in (None, NullValue):
        return None
    return int(value)


async def async_migrate_device(
    matter_client: MatterClient, node: MatterNode, dataset: bytes
) -> str:
    """Move a commissioned Thread device onto the network in the dataset.

    Returns "migrated" or "already_on_network". Raises HomeAssistantError
    when the device could not be moved; unless the failure happened before
    the device was told to connect, it reverts to its previous network by
    itself when the fail-safe expires.
    """
    endpoint = get_thread_network_endpoint(node)
    if TYPE_CHECKING:
        assert endpoint is not None  # callers resolve the endpoint first

    target_id = network_id_from_dataset(dataset)
    old_pan = get_extended_pan_id(endpoint)
    if old_pan is not None and old_pan.to_bytes(8, "big") == target_id:
        return "already_on_network"

    # The cache says the device is elsewhere; make sure the device agrees
    # before reconfiguring it, since diagnostics are lazily reported and
    # may still name a network it has long left.
    live_pan = await async_get_live_extended_pan_id(matter_client, node)
    if live_pan is not None:
        if live_pan.to_bytes(8, "big") == target_id:
            return "already_on_network"
        old_pan = live_pan

    node_id = node.node_id

    async def send(command: Any) -> Any:
        try:
            return await matter_client.send_device_command(
                node_id=node_id, endpoint_id=0, command=command
            )
        except MatterError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="device_command_failed",
                translation_placeholders={"details": str(err)},
            ) from err

    async def disarm() -> None:
        # Best effort: the interesting error is the one being re-raised.
        try:
            await matter_client.send_device_command(
                node_id=node_id,
                endpoint_id=0,
                command=clusters.GeneralCommissioning.Commands.ArmFailSafe(
                    expiryLengthSeconds=0
                ),
            )
        except MatterError:
            LOGGER.debug("Could not disarm the fail-safe on node %s", node_id)

    response = await send(
        clusters.GeneralCommissioning.Commands.ArmFailSafe(
            expiryLengthSeconds=FAILSAFE_EXPIRY_S, breadcrumb=1
        )
    )
    check_commissioning_response(response, "Arming the fail-safe")

    try:
        if old_pan is not None:
            # The device only carries one network, and it refuses to stage
            # a dataset for a different one while the old entry is present.
            # A missing entry is fine: a re-run after an aborted attempt.
            response = await send(
                clusters.NetworkCommissioning.Commands.RemoveNetwork(
                    networkID=old_pan.to_bytes(8, "big"), breadcrumb=2
                )
            )
            check_network_response(
                response,
                "Removing the previous network",
                ok=(
                    0,
                    clusters.NetworkCommissioning.Enums.NetworkCommissioningStatusEnum.kNetworkIDNotFound,
                ),
            )

        response = await send(
            clusters.NetworkCommissioning.Commands.AddOrUpdateThreadNetwork(
                operationalDataset=dataset, breadcrumb=3
            )
        )
        check_network_response(response, "Staging the new network")
    except HomeAssistantError:
        await disarm()
        raise

    # The device detaches as soon as it acts on this, so a lost response is
    # expected and not a failure; only an explicit refusal is.
    try:
        response = await send(
            clusters.NetworkCommissioning.Commands.ConnectNetwork(
                networkID=target_id, breadcrumb=4
            )
        )
    except HomeAssistantError:
        LOGGER.debug(
            "No answer to ConnectNetwork from node %s; it is likely switching",
            node_id,
        )
    else:
        try:
            check_network_response(response, "Connecting to the new network")
        except HomeAssistantError:
            # Refused outright: the device never left its network.
            await disarm()
            raise

    # From here the device is attaching to the new network and must be
    # reached there. Failing is reported, never disarmed: the armed
    # fail-safe is what brings the device back to its previous network.
    deadline = asyncio.get_running_loop().time() + COMPLETE_DEADLINE_S
    while True:
        try:
            response = await matter_client.send_device_command(
                node_id=node_id,
                endpoint_id=0,
                command=clusters.GeneralCommissioning.Commands.CommissioningComplete(),
            )
        except MatterError as err:
            if asyncio.get_running_loop().time() >= deadline:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="device_command_failed",
                    translation_placeholders={
                        "details": (
                            f"the device did not answer on the new network within "
                            f"{COMPLETE_DEADLINE_S}s ({err}); it will revert to its "
                            "previous network when the fail-safe expires"
                        )
                    },
                ) from err
            # Nudge the controller into re-resolving the device's new address.
            with contextlib.suppress(MatterError):
                await matter_client.ping_node(node_id)
            await asyncio.sleep(COMPLETE_RETRY_DELAY_S)
            continue
        check_commissioning_response(response, "Completing the migration")
        return "migrated"
