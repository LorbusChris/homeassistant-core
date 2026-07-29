"""Services for Matter devices."""

from typing import Any

from chip.clusters.Types import NullValue
from matter_server.common.errors import MatterError
import voluptuous as vol

from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.components.thread import async_get_preferred_dataset
from homeassistant.components.water_heater import DOMAIN as WATER_HEATER_DOMAIN
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    service,
)

from .adapter import MatterAdapter
from .const import (
    ATTR_CREDENTIAL_DATA,
    ATTR_CREDENTIAL_INDEX,
    ATTR_CREDENTIAL_RULE,
    ATTR_CREDENTIAL_TYPE,
    ATTR_USER_INDEX,
    ATTR_USER_NAME,
    ATTR_USER_STATUS,
    ATTR_USER_TYPE,
    CLEAR_ALL_INDEX,
    CONF_WIFI_CREDENTIALS_SOURCE,
    CREDENTIAL_RULE_REVERSE_MAP,
    CREDENTIAL_TYPE_REVERSE_MAP,
    DOMAIN,
    ID_TYPE_DEVICE_ID,
    LOGGER,
    SERVICE_CREDENTIAL_TYPES,
    USER_TYPE_REVERSE_MAP,
)
from .helpers import MissingNode, get_device_id, get_matter, node_from_ha_device_id
from .thread_border_router import (
    async_push_dataset,
    async_read_dataset,
    get_active_dataset_timestamp,
    get_border_router_endpoints,
    get_pending_dataset_timestamp,
)
from .thread_migration import (
    MIGRATION_ORDER,
    async_migrate_device,
    get_extended_pan_id,
    get_routing_role,
    get_thread_network_endpoint,
    network_id_from_dataset,
)
from .wifi_credentials import (
    async_import_credentials,
    async_set_manual_credentials,
    get_wifi_endpoints,
)

ATTR_DURATION = "duration"
ATTR_EMERGENCY_BOOST = "emergency_boost"
ATTR_TEMPORARY_SETPOINT = "temporary_setpoint"

SERVICE_WATER_HEATER_BOOST = "water_heater_boost"

ATTR_SSID = "ssid"
ATTR_PASSWORD = "password"

SERVICE_SET_WIFI_CREDENTIALS = "set_wifi_credentials"
SERVICE_IMPORT_WIFI_CREDENTIALS = "import_wifi_credentials"
SERVICE_PUSH_THREAD_DATASET = "push_thread_dataset"
SERVICE_GET_THREAD_DATASET = "get_thread_dataset"
SERVICE_MIGRATE_THREAD_DEVICE = "migrate_thread_device"
SERVICE_MIGRATE_THREAD_FLEET = "migrate_thread_fleet"

ATTR_DATASET = "dataset"


def _get_matter(call: ServiceCall) -> MatterAdapter:
    """Return the Matter adapter or explain why there is none."""
    try:
        return get_matter(call.hass)
    except IndexError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="matter_not_set_up"
        ) from err


async def _async_set_wifi_credentials(call: ServiceCall) -> None:
    """Store the Wi-Fi credentials handed to devices during commissioning.

    This sets the network the Matter controller puts new Wi-Fi devices on;
    it does not, and cannot, change any access point configuration. A
    network manager device normally supplies these credentials by itself,
    so this action is for overriding that choice or for use without one.
    """
    matter = _get_matter(call)
    try:
        await async_set_manual_credentials(
            call.hass, matter, call.data[ATTR_SSID], call.data[ATTR_PASSWORD]
        )
    except MatterError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="device_command_failed",
            translation_placeholders={"details": str(err)},
        ) from err


async def _async_import_wifi_credentials(call: ServiceCall) -> None:
    """Return to the Wi-Fi credentials shared by the network manager.

    Lifts the override left by the set_wifi_credentials action and imports
    the credentials the network's routers currently offer.
    """
    matter = _get_matter(call)
    entry = matter.config_entry
    if CONF_WIFI_CREDENTIALS_SOURCE in entry.options:
        options = dict(entry.options)
        options.pop(CONF_WIFI_CREDENTIALS_SOURCE)
        call.hass.config_entries.async_update_entry(entry, options=options)
    imported = 0
    failed = 0
    for node in matter.matter_client.get_nodes():
        for endpoint in get_wifi_endpoints(node):
            try:
                await async_import_credentials(matter.matter_client, endpoint)
            except MatterError as err:
                # One unreachable router must not block importing from the
                # others; the failure only matters if nobody answered.
                failed += 1
                LOGGER.warning(
                    "Could not import Wi-Fi credentials from node %s: %s",
                    node.node_id,
                    err,
                )
            else:
                imported += 1
    if failed and not imported:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="wifi_import_failed"
        )


async def _dataset_from_call(call: ServiceCall) -> bytes:
    """Return the Thread dataset named by the call, or the preferred one."""
    if dataset_hex := call.data.get(ATTR_DATASET):
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
    preferred = await async_get_preferred_dataset(call.hass)
    if preferred is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_preferred_dataset"
        )
    return bytes.fromhex(preferred)


def _resolve_thread_device(call: ServiceCall, matter: MatterAdapter) -> Any:
    """Return the migratable Thread node the chosen device stands for.

    Border routers are refused: they are the network, not a device on it,
    and are managed through Push Thread network instead. So is picking a
    bridged accessory of a hub, which must not reconfigure the hub.
    """
    device_id = call.data[ATTR_DEVICE_ID]
    try:
        node = node_from_ha_device_id(call.hass, device_id)
    except MissingNode as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_device",
            translation_placeholders={"device_id": device_id},
        ) from err
    if node is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_device",
            translation_placeholders={"device_id": device_id},
        )
    endpoint = get_thread_network_endpoint(node)
    if endpoint is None or get_border_router_endpoints(node):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_a_thread_device"
        )
    device_entry = dr.async_get(call.hass).async_get(device_id)
    assert device_entry is not None  # node resolution above already found it
    server_info = matter.matter_client.server_info
    device_identifiers = {
        identifier[1]
        for identifier in device_entry.identifiers
        if identifier[0] == DOMAIN
    }
    if (
        f"{ID_TYPE_DEVICE_ID}_{get_device_id(server_info, endpoint)}"
        not in device_identifiers
    ):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_a_thread_device"
        )
    return node


async def _async_migrate_thread_device(call: ServiceCall) -> dict[str, Any]:
    """Move one commissioned Thread device onto another Thread network."""
    matter = _get_matter(call)
    node = _resolve_thread_device(call, matter)
    dataset = await _dataset_from_call(call)
    result = await async_migrate_device(matter.matter_client, node, dataset)
    return {"result": result}


async def _async_migrate_thread_fleet(call: ServiceCall) -> dict[str, Any]:
    """Move every commissioned Thread device onto another Thread network.

    Devices already on the network are skipped, border routers are left
    alone, and end devices go before the routers they depend on. The action
    is idempotent: running it again picks up the stragglers.
    """
    matter = _get_matter(call)
    dataset = await _dataset_from_call(call)
    target_id = network_id_from_dataset(dataset)

    candidates = []
    migrated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for node in matter.matter_client.get_nodes():
        endpoint = get_thread_network_endpoint(node)
        if endpoint is None or get_border_router_endpoints(node):
            continue
        entry = {"name": node.name, "node_id": node.node_id}
        old_pan = get_extended_pan_id(endpoint)
        if old_pan is not None and old_pan.to_bytes(8, "big") == target_id:
            skipped.append(entry)
        elif not node.available:
            failed.append({**entry, "reason": "offline"})
        else:
            candidates.append(
                (MIGRATION_ORDER.get(get_routing_role(endpoint), 1), node)
            )

    candidates.sort(key=lambda item: (item[0], item[1].node_id))
    total = len(candidates)
    for index, (_, node) in enumerate(candidates, start=1):
        entry = {"name": node.name, "node_id": node.node_id}
        LOGGER.info(
            "Migrating %s (node %s, %d of %d) to the new Thread network",
            node.name,
            node.node_id,
            index,
            total,
        )
        try:
            result = await async_migrate_device(matter.matter_client, node, dataset)
        except HomeAssistantError as err:
            LOGGER.warning(
                "Could not migrate %s (node %s): %s", node.name, node.node_id, err
            )
            failed.append({**entry, "reason": str(err)})
        else:
            if result == "migrated":
                migrated.append(entry)
            else:
                skipped.append(entry)
    return {"migrated": migrated, "skipped": skipped, "failed": failed}


def _resolve_border_router(call: ServiceCall, matter: MatterAdapter) -> Any:
    """Return the border router endpoint the chosen device stands for.

    The chosen device must itself be the border router: a node can carry
    bridged accessories whose devices resolve to the same node, and
    configuring the hub because one of its lights was selected would touch
    hardware the user never pointed at.
    """
    device_id = call.data[ATTR_DEVICE_ID]
    try:
        node = node_from_ha_device_id(call.hass, device_id)
    except MissingNode as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_device",
            translation_placeholders={"device_id": device_id},
        ) from err
    if node is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_device",
            translation_placeholders={"device_id": device_id},
        )
    device_entry = dr.async_get(call.hass).async_get(device_id)
    assert device_entry is not None  # node resolution above already found it
    server_info = matter.matter_client.server_info
    device_identifiers = {
        identifier[1]
        for identifier in device_entry.identifiers
        if identifier[0] == DOMAIN
    }
    endpoints = [
        endpoint
        for endpoint in get_border_router_endpoints(node)
        if f"{ID_TYPE_DEVICE_ID}_{get_device_id(server_info, endpoint)}"
        in device_identifiers
    ]
    if len(endpoints) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_a_border_router"
        )
    return endpoints[0]


async def _async_push_thread_dataset(call: ServiceCall) -> None:
    """Hand a Thread network to a border router.

    An unprovisioned border router is adopted into the network under the
    protection of the device's fail-safe: if anything goes wrong the router
    reverts to having no network rather than being left half-configured. A
    provisioned one is handed the dataset as a pending migration, which its
    network applies through Thread's own delay mechanism.
    """
    matter = _get_matter(call)
    endpoint = _resolve_border_router(call, matter)

    dataset = await _dataset_from_call(call)

    await async_push_dataset(matter.matter_client, endpoint, dataset)


async def _async_get_thread_dataset(call: ServiceCall) -> dict[str, Any]:
    """Read the Thread network configuration of a border router.

    Returns the active dataset, and the pending one while a migration is
    scheduled; a completed migration clears the pending dataset, so its
    disappearance is how a caller observes the switch has happened. The
    datasets contain the network key, which is why reading them is a
    privileged operation on the device.
    """
    matter = _get_matter(call)
    endpoint = _resolve_border_router(call, matter)

    def _timestamp(value: int | None) -> int | None:
        return None if value in (None, NullValue) else value

    active = await async_read_dataset(matter.matter_client, endpoint)
    pending = await async_read_dataset(matter.matter_client, endpoint, pending=True)
    return {
        "active_dataset": active.hex() if active else None,
        "pending_dataset": pending.hex() if pending else None,
        "active_dataset_timestamp": _timestamp(get_active_dataset_timestamp(endpoint)),
        "pending_dataset_timestamp": _timestamp(
            get_pending_dataset_timestamp(endpoint)
        ),
    }


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Matter services."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_WIFI_CREDENTIALS,
        _async_set_wifi_credentials,
        schema=vol.Schema(
            {
                vol.Required(ATTR_SSID): cv.string,
                vol.Required(ATTR_PASSWORD): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_WIFI_CREDENTIALS,
        _async_import_wifi_credentials,
        schema=vol.Schema({}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PUSH_THREAD_DATASET,
        _async_push_thread_dataset,
        schema=vol.Schema(
            {
                vol.Required(ATTR_DEVICE_ID): cv.string,
                vol.Optional(ATTR_DATASET): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_THREAD_DATASET,
        _async_get_thread_dataset,
        schema=vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_MIGRATE_THREAD_DEVICE,
        _async_migrate_thread_device,
        schema=vol.Schema(
            {
                vol.Required(ATTR_DEVICE_ID): cv.string,
                vol.Optional(ATTR_DATASET): cv.string,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_MIGRATE_THREAD_FLEET,
        _async_migrate_thread_fleet,
        schema=vol.Schema({vol.Optional(ATTR_DATASET): cv.string}),
        supports_response=SupportsResponse.OPTIONAL,
    )

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_WATER_HEATER_BOOST,
        entity_domain=WATER_HEATER_DOMAIN,
        schema={
            # duration >=1
            vol.Required(ATTR_DURATION): vol.All(vol.Coerce(int), vol.Range(min=1)),
            vol.Optional(ATTR_EMERGENCY_BOOST): cv.boolean,
            vol.Optional(ATTR_TEMPORARY_SETPOINT): vol.All(
                vol.Coerce(int), vol.Range(min=30, max=65)
            ),
        },
        func="async_set_boost",
    )

    # Lock services - Full user CRUD
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        "set_lock_user",
        entity_domain=LOCK_DOMAIN,
        schema={
            vol.Optional(ATTR_USER_INDEX): vol.All(vol.Coerce(int), vol.Range(min=1)),
            vol.Optional(ATTR_USER_NAME): vol.Any(str, None),
            vol.Optional(ATTR_USER_TYPE): vol.In(USER_TYPE_REVERSE_MAP.keys()),
            vol.Optional(ATTR_CREDENTIAL_RULE): vol.In(
                CREDENTIAL_RULE_REVERSE_MAP.keys()
            ),
        },
        func="async_set_lock_user",
    )

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        "clear_lock_user",
        entity_domain=LOCK_DOMAIN,
        schema={
            vol.Required(ATTR_USER_INDEX): vol.All(
                vol.Coerce(int),
                vol.Any(vol.Range(min=1), CLEAR_ALL_INDEX),
            ),
        },
        func="async_clear_lock_user",
    )

    # Lock services - Query operations
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        "get_lock_info",
        entity_domain=LOCK_DOMAIN,
        schema={},
        func="async_get_lock_info",
        supports_response=SupportsResponse.ONLY,
    )

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        "get_lock_users",
        entity_domain=LOCK_DOMAIN,
        schema={},
        func="async_get_lock_users",
        supports_response=SupportsResponse.ONLY,
    )

    # Lock services - Credential management
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        "set_lock_credential",
        entity_domain=LOCK_DOMAIN,
        schema={
            vol.Required(ATTR_CREDENTIAL_TYPE): vol.In(SERVICE_CREDENTIAL_TYPES),
            vol.Required(ATTR_CREDENTIAL_DATA): str,
            vol.Optional(ATTR_CREDENTIAL_INDEX): vol.All(
                vol.Coerce(int), vol.Range(min=0)
            ),
            vol.Optional(ATTR_USER_INDEX): vol.All(vol.Coerce(int), vol.Range(min=1)),
            vol.Optional(ATTR_USER_STATUS): vol.In(
                ["occupied_enabled", "occupied_disabled"]
            ),
            vol.Optional(ATTR_USER_TYPE): vol.In(USER_TYPE_REVERSE_MAP.keys()),
        },
        func="async_set_lock_credential",
        supports_response=SupportsResponse.ONLY,
    )

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        "clear_lock_credential",
        entity_domain=LOCK_DOMAIN,
        schema={
            vol.Required(ATTR_CREDENTIAL_TYPE): vol.In(SERVICE_CREDENTIAL_TYPES),
            vol.Required(ATTR_CREDENTIAL_INDEX): vol.All(
                vol.Coerce(int), vol.Range(min=0)
            ),
        },
        func="async_clear_lock_credential",
    )

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        "get_lock_credential_status",
        entity_domain=LOCK_DOMAIN,
        schema={
            vol.Required(ATTR_CREDENTIAL_TYPE): vol.In(
                CREDENTIAL_TYPE_REVERSE_MAP.keys()
            ),
            vol.Required(ATTR_CREDENTIAL_INDEX): vol.All(
                vol.Coerce(int), vol.Range(min=0)
            ),
        },
        func="async_get_lock_credential_status",
        supports_response=SupportsResponse.ONLY,
    )
