"""Services for Matter devices."""

from collections.abc import Callable
from typing import Any, cast

from chip.clusters.Types import NullValue
from matter_server.client.exceptions import MatterClientException
from matter_server.client.models.node import MatterEndpoint, MatterNode
from matter_server.common.errors import MatterError
from matter_server.common.models import ServerInfoMessage
import probatio

from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.components.thread import async_get_preferred_dataset
from homeassistant.components.water_heater import DOMAIN as WATER_HEATER_DOMAIN
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    service,
)

from .adapter import MatterAdapter
from .const import (
    CLEAR_ALL_INDEX,
    CREDENTIAL_RULE_REVERSE_MAP,
    CREDENTIAL_TYPE_REVERSE_MAP,
    DOMAIN,
    ID_TYPE_DEVICE_ID,
    SERVICE_CREDENTIAL_TYPES,
    USER_TYPE_REVERSE_MAP,
    MatterService,
    MatterServiceArgument,
)
from .helpers import MissingNode, get_device_id, get_matter, node_from_ha_device_id
from .thread_border_router import (
    async_push_dataset,
    async_read_dataset,
    get_active_dataset_timestamp,
    get_border_router_endpoints,
    get_pending_dataset_timestamp,
)


def _get_matter(call: ServiceCall) -> MatterAdapter:
    """Return the Matter adapter; raise when Matter is not loaded."""
    try:
        return get_matter(call.hass)
    except IndexError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="matter_not_set_up"
        ) from err


async def _async_set_wifi_credentials(call: ServiceCall) -> None:
    """Set the Wi-Fi network the Matter server commissions devices onto."""
    matter = _get_matter(call)
    try:
        await matter.matter_client.set_wifi_credentials(
            ssid=call.data[MatterServiceArgument.SSID],
            credentials=call.data[MatterServiceArgument.PASSWORD],
        )
    except (MatterError, MatterClientException) as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="wifi_credentials_set_failed",
            translation_placeholders={"error": str(err) or err.__class__.__name__},
        ) from err


def _resolve_device(
    call: ServiceCall, matter: MatterAdapter
) -> tuple[MatterNode, Callable[[MatterEndpoint], bool]]:
    """Return the node of the chosen device and a test for its own endpoints.

    The test tells the endpoints the device stands for from those of bridged
    devices on the same node.
    """
    device_id = call.data[ATTR_DEVICE_ID]
    try:
        node = node_from_ha_device_id(call.hass, device_id)
    except MissingNode:
        node = None
    if node is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_device",
            translation_placeholders={"device_id": device_id},
        )
    device = dr.async_get(call.hass).async_get(device_id, include_child_devices=False)
    assert device is not None
    identifiers = {value for domain, value in device.identifiers if domain == DOMAIN}
    server_info = cast(ServerInfoMessage, matter.matter_client.server_info)

    def is_own(endpoint: MatterEndpoint) -> bool:
        identifier = f"{ID_TYPE_DEVICE_ID}_{get_device_id(server_info, endpoint)}"
        return identifier in identifiers

    return node, is_own


def _resolve_border_router(call: ServiceCall, matter: MatterAdapter) -> MatterEndpoint:
    """Return the border router endpoint of the chosen device."""
    node, is_own = _resolve_device(call, matter)
    endpoints = [e for e in get_border_router_endpoints(node) if is_own(e)]
    if len(endpoints) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_a_border_router"
        )
    return endpoints[0]


async def _dataset_from_call(call: ServiceCall) -> bytes:
    """Return the dataset of the call, by default the preferred network."""
    if (dataset_hex := call.data.get(MatterServiceArgument.DATASET)) is None:
        if (dataset_hex := await async_get_preferred_dataset(call.hass)) is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_preferred_dataset"
            )
    try:
        dataset = bytes.fromhex(dataset_hex)
    except ValueError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_dataset"
        ) from err
    if not dataset:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_dataset"
        )
    return dataset


async def _async_push_thread_dataset(call: ServiceCall) -> None:
    """Hand a dataset, by default the preferred network, to a border router."""
    matter = _get_matter(call)
    endpoint = _resolve_border_router(call, matter)
    dataset = await _dataset_from_call(call)
    await async_push_dataset(matter.matter_client, endpoint, dataset)


async def _async_get_thread_dataset(call: ServiceCall) -> ServiceResponse:
    """Return a border router's active dataset, and its pending one.

    The datasets hold the network key, so only those whose timestamp says
    they exist are read.
    """
    matter = _get_matter(call)
    endpoint = _resolve_border_router(call, matter)
    response: dict[str, Any] = {}
    for kind, timestamp in (
        ("active", get_active_dataset_timestamp(endpoint)),
        ("pending", get_pending_dataset_timestamp(endpoint)),
    ):
        dataset = b""
        if timestamp in (None, NullValue):
            timestamp = None
        else:
            try:
                dataset = await async_read_dataset(
                    matter.matter_client, endpoint, pending=kind == "pending"
                )
            except (MatterError, MatterClientException) as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="thread_dataset_read_failed",
                    translation_placeholders={
                        "error": str(err) or err.__class__.__name__
                    },
                ) from err
            except ValueError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="thread_dataset_unusable",
                ) from err
        response[f"{kind}_dataset"] = dataset.hex() or None
        response[f"{kind}_dataset_timestamp"] = timestamp
    return response


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Matter services."""

    hass.services.async_register(
        DOMAIN,
        MatterService.SET_WIFI_CREDENTIALS,
        _async_set_wifi_credentials,
        schema=probatio.Schema(
            {
                probatio.Required(MatterServiceArgument.SSID): cv.string,
                probatio.Required(MatterServiceArgument.PASSWORD): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        MatterService.PUSH_THREAD_DATASET,
        _async_push_thread_dataset,
        schema=probatio.Schema(
            {
                probatio.Required(ATTR_DEVICE_ID): cv.string,
                probatio.Optional(MatterServiceArgument.DATASET): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        MatterService.GET_THREAD_DATASET,
        _async_get_thread_dataset,
        schema=probatio.Schema({probatio.Required(ATTR_DEVICE_ID): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        MatterService.WATER_HEATER_BOOST,
        entity_domain=WATER_HEATER_DOMAIN,
        schema={
            # duration >=1
            probatio.Required(MatterServiceArgument.DURATION): probatio.All(
                probatio.Coerce(int), probatio.Range(min=1)
            ),
            probatio.Optional(MatterServiceArgument.EMERGENCY_BOOST): cv.boolean,
            probatio.Optional(MatterServiceArgument.TEMPORARY_SETPOINT): probatio.All(
                probatio.Coerce(int), probatio.Range(min=30, max=65)
            ),
        },
        func="async_set_boost",
    )

    # Lock services - Full user CRUD
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        MatterService.SET_LOCK_USER,
        entity_domain=LOCK_DOMAIN,
        schema={
            probatio.Optional(MatterServiceArgument.USER_INDEX): probatio.All(
                probatio.Coerce(int), probatio.Range(min=1)
            ),
            probatio.Optional(MatterServiceArgument.USER_NAME): probatio.Any(str, None),
            probatio.Optional(MatterServiceArgument.USER_TYPE): probatio.In(
                USER_TYPE_REVERSE_MAP.keys()
            ),
            probatio.Optional(MatterServiceArgument.CREDENTIAL_RULE): probatio.In(
                CREDENTIAL_RULE_REVERSE_MAP.keys()
            ),
        },
        func="async_set_lock_user",
    )

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        MatterService.CLEAR_LOCK_USER,
        entity_domain=LOCK_DOMAIN,
        schema={
            probatio.Required(MatterServiceArgument.USER_INDEX): probatio.All(
                probatio.Coerce(int),
                probatio.Any(probatio.Range(min=1), CLEAR_ALL_INDEX),
            ),
        },
        func="async_clear_lock_user",
    )

    # Lock services - Query operations
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        MatterService.GET_LOCK_INFO,
        entity_domain=LOCK_DOMAIN,
        schema={},
        func="async_get_lock_info",
        supports_response=SupportsResponse.ONLY,
    )

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        MatterService.GET_LOCK_USERS,
        entity_domain=LOCK_DOMAIN,
        schema={},
        func="async_get_lock_users",
        supports_response=SupportsResponse.ONLY,
    )

    # Lock services - Credential management
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        MatterService.SET_LOCK_CREDENTIAL,
        entity_domain=LOCK_DOMAIN,
        schema={
            probatio.Required(MatterServiceArgument.CREDENTIAL_TYPE): probatio.In(
                SERVICE_CREDENTIAL_TYPES
            ),
            probatio.Required(MatterServiceArgument.CREDENTIAL_DATA): cv.string,
            probatio.Optional(MatterServiceArgument.CREDENTIAL_INDEX): probatio.All(
                probatio.Coerce(int), probatio.Range(min=0)
            ),
            probatio.Optional(MatterServiceArgument.USER_INDEX): probatio.All(
                probatio.Coerce(int), probatio.Range(min=1)
            ),
            probatio.Optional(MatterServiceArgument.USER_STATUS): probatio.In(
                ["occupied_enabled", "occupied_disabled"]
            ),
            probatio.Optional(MatterServiceArgument.USER_TYPE): probatio.In(
                USER_TYPE_REVERSE_MAP.keys()
            ),
        },
        func="async_set_lock_credential",
        supports_response=SupportsResponse.ONLY,
    )

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        MatterService.CLEAR_LOCK_CREDENTIAL,
        entity_domain=LOCK_DOMAIN,
        schema={
            probatio.Required(MatterServiceArgument.CREDENTIAL_TYPE): probatio.In(
                SERVICE_CREDENTIAL_TYPES
            ),
            probatio.Required(MatterServiceArgument.CREDENTIAL_INDEX): probatio.All(
                probatio.Coerce(int), probatio.Range(min=0)
            ),
        },
        func="async_clear_lock_credential",
    )

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        MatterService.GET_LOCK_CREDENTIAL_STATUS,
        entity_domain=LOCK_DOMAIN,
        schema={
            probatio.Required(MatterServiceArgument.CREDENTIAL_TYPE): probatio.In(
                CREDENTIAL_TYPE_REVERSE_MAP.keys()
            ),
            probatio.Required(MatterServiceArgument.CREDENTIAL_INDEX): probatio.All(
                probatio.Coerce(int), probatio.Range(min=0)
            ),
        },
        func="async_get_lock_credential_status",
        supports_response=SupportsResponse.ONLY,
    )
