"""Import Thread operational datasets from Matter border routers.

A router that implements the Network Infrastructure Manager device type carries
the Thread Border Router Management cluster, which lets an authorised member of
the fabric read the active operational dataset over Matter. That replaces
reading the same credentials from a vendor specific API such as the OpenThread
Border Router REST interface.
"""

from base64 import b64decode
import contextlib
from typing import TYPE_CHECKING, Any

from chip.clusters import Objects as clusters
from chip.clusters.Types import NullValue
from matter_server.client.models import device_types
from matter_server.common.errors import MatterError
from matter_server.common.helpers.util import create_attribute_path

from homeassistant.components.thread import async_add_dataset
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    from matter_server.client import MatterClient
    from matter_server.client.models.node import MatterEndpoint, MatterNode

# A border router may present either device type; both carry the TBRM cluster.
BORDER_ROUTER_DEVICE_TYPES = {
    device_types.NetworkInfrastructureManager.device_type,
    device_types.ThreadBorderRouter.device_type,
}


def get_active_dataset_timestamp_path(endpoint: MatterEndpoint) -> str:
    """Return the attribute path of ActiveDatasetTimestamp on this endpoint."""
    return create_attribute_path(
        endpoint.endpoint_id,
        clusters.ThreadBorderRouterManagement.id,
        clusters.ThreadBorderRouterManagement.Attributes.ActiveDatasetTimestamp.attribute_id,
    )


def get_border_router_endpoints(node: MatterNode) -> list[MatterEndpoint]:
    """Return the endpoints of a node that expose a Thread border router."""
    return [
        endpoint
        for endpoint in node.endpoints.values()
        if endpoint.has_cluster(clusters.ThreadBorderRouterManagement)
        and any(
            device_type.device_type in BORDER_ROUTER_DEVICE_TYPES
            for device_type in endpoint.device_types
        )
    ]


def get_extended_address(endpoint: MatterEndpoint) -> Any:
    """Return the border router's Thread extended address attribute value.

    NullValue when the Thread stack is not running; None when the attribute
    is absent.
    """
    return endpoint.get_attribute_value(
        None, clusters.ThreadNetworkDiagnostics.Attributes.ExtAddress
    )


def get_active_dataset_timestamp(endpoint: MatterEndpoint) -> int | None:
    """Return the active dataset timestamp, which changes when the dataset does."""
    timestamp: int | None = endpoint.get_attribute_value(
        None, clusters.ThreadBorderRouterManagement.Attributes.ActiveDatasetTimestamp
    )
    return timestamp


def _dataset_from_response(response: Any) -> bytes:
    """Return the dataset carried by a DatasetResponse.

    The Matter client hands back command responses as plain dicts, with octet
    strings base64 encoded rather than as bytes, so the payload cannot be read
    off the response as an attribute. Object and bytes forms are still accepted
    so this keeps working if that representation changes.

    Raises ValueError for a response that carries no readable dataset: a
    malformed reply must not be mistaken for an unprovisioned border router,
    or the import would be considered done and not tried again.
    """
    if isinstance(response, dict):
        raw = response.get("dataset")
    else:
        raw = getattr(response, "dataset", None)
    if isinstance(raw, str):
        return b64decode(raw, validate=True)
    if isinstance(raw, bytes):
        return raw
    raise ValueError("response carries no dataset payload")


async def async_import_dataset(
    hass: HomeAssistant, matter_client: MatterClient, endpoint: MatterEndpoint
) -> None:
    """Read the active dataset from a border router and add it to the store.

    The dataset is only reachable by command; the identifiers used to mark the
    preferred border agent are plain attributes on the same endpoint.
    """
    response: Any = await matter_client.send_device_command(
        node_id=endpoint.node.node_id,
        endpoint_id=endpoint.endpoint_id,
        command=clusters.ThreadBorderRouterManagement.Commands.GetActiveDatasetRequest(),
    )
    dataset = _dataset_from_response(response)

    if not dataset:
        # A border router that has not formed or joined a network answers with
        # an empty dataset, which the dataset store would reject for lacking an
        # active timestamp.
        LOGGER.debug(
            "Border router on node %s endpoint %s has no active dataset",
            endpoint.node.node_id,
            endpoint.endpoint_id,
        )
        return

    border_agent_id: bytes | None = endpoint.get_attribute_value(
        None, clusters.ThreadBorderRouterManagement.Attributes.BorderAgentID
    )
    ext_address: int | None = get_extended_address(endpoint)

    # The store refuses a preferred border agent ID that is not accompanied by an
    # extended address, so only mark a preference when both are known. A border
    # router with no Thread stack running reports ExtAddress as NullValue rather
    # than omitting it, which is not falsy and must be excluded explicitly.
    preferred: dict[str, str] = {}
    if border_agent_id and ext_address not in (None, NullValue):
        preferred = {
            "preferred_border_agent_id": border_agent_id.hex(),
            "preferred_extended_address": ext_address.to_bytes(8, "big").hex(),
        }

    await async_add_dataset(hass, DOMAIN, dataset.hex(), **preferred)

    LOGGER.debug(
        "Imported Thread dataset from node %s endpoint %s",
        endpoint.node.node_id,
        endpoint.endpoint_id,
    )


def check_commissioning_response(response: Any, step: str) -> None:
    """Raise when a commissioning command reports failure in its payload.

    ArmFailSafe and CommissioningComplete answer with an error code inside
    the response rather than an interaction status, so a failed step
    otherwise reads as success.
    """
    if isinstance(response, dict):
        code = response.get("errorCode")
        text = response.get("debugText") or ""
    else:
        code = getattr(response, "errorCode", None)
        text = getattr(response, "debugText", None) or ""
    if code:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="device_command_failed",
            translation_placeholders={
                "details": f"{step} failed with error {code} {text}".strip()
            },
        )


async def async_push_dataset(
    matter_client: MatterClient, endpoint: MatterEndpoint, dataset: bytes
) -> None:
    """Hand a Thread network to the border router behind this endpoint.

    An unprovisioned border router is adopted under the protection of the
    device's fail-safe: if anything goes wrong it reverts to having no
    network rather than being left half-configured. A provisioned one is
    handed the dataset as a pending migration, which its network applies
    through Thread's own delay mechanism.
    """
    node_id = endpoint.node.node_id

    async def send(endpoint_id: int, command: Any) -> Any:
        try:
            return await matter_client.send_device_command(
                node_id=node_id, endpoint_id=endpoint_id, command=command
            )
        except MatterError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="device_command_failed",
                translation_placeholders={"details": str(err)},
            ) from err

    # The attribute reads null while the router has no network; the client
    # hands that back as its NullValue sentinel, not as None.
    if get_active_dataset_timestamp(endpoint) in (None, NullValue):
        response = await send(
            0,
            clusters.GeneralCommissioning.Commands.ArmFailSafe(expiryLengthSeconds=120),
        )
        check_commissioning_response(response, "Arming the fail-safe")
        try:
            await send(
                endpoint.endpoint_id,
                clusters.ThreadBorderRouterManagement.Commands.SetActiveDatasetRequest(
                    activeDataset=dataset
                ),
            )
        except Exception:
            # Disarm so the router does not sit in a half-open fail-safe for
            # two minutes; it reverts to unprovisioned either way. Best
            # effort: the interesting error is the one being re-raised.
            with contextlib.suppress(Exception):
                await matter_client.send_device_command(
                    node_id=node_id,
                    endpoint_id=0,
                    command=clusters.GeneralCommissioning.Commands.ArmFailSafe(
                        expiryLengthSeconds=0
                    ),
                )
            raise
        response = await send(
            0, clusters.GeneralCommissioning.Commands.CommissioningComplete()
        )
        check_commissioning_response(response, "Completing the adoption")
    else:
        await send(
            endpoint.endpoint_id,
            clusters.ThreadBorderRouterManagement.Commands.SetPendingDatasetRequest(
                pendingDataset=dataset
            ),
        )


def get_pending_dataset_timestamp(endpoint: MatterEndpoint) -> int | None:
    """Return the pending dataset timestamp, set while a migration is scheduled."""
    timestamp: int | None = endpoint.get_attribute_value(
        None, clusters.ThreadBorderRouterManagement.Attributes.PendingDatasetTimestamp
    )
    return timestamp


async def async_read_dataset(
    matter_client: MatterClient, endpoint: MatterEndpoint, pending: bool = False
) -> bytes | None:
    """Read the active or pending operational dataset from a border router.

    Returns None while the respective dataset does not exist, so the
    privileged read is only made when there is something to fetch.
    """
    timestamp = (
        get_pending_dataset_timestamp(endpoint)
        if pending
        else get_active_dataset_timestamp(endpoint)
    )
    if timestamp in (None, NullValue):
        return None
    command: Any
    if pending:
        command = (
            clusters.ThreadBorderRouterManagement.Commands.GetPendingDatasetRequest()
        )
    else:
        command = (
            clusters.ThreadBorderRouterManagement.Commands.GetActiveDatasetRequest()
        )
    try:
        response = await matter_client.send_device_command(
            node_id=endpoint.node.node_id,
            endpoint_id=endpoint.endpoint_id,
            command=command,
        )
    except MatterError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="device_command_failed",
            translation_placeholders={"details": str(err)},
        ) from err
    return _dataset_from_response(response)
