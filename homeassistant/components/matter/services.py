"""Services for Matter devices."""

from matter_server.common.errors import MatterError
import voluptuous as vol

from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.components.water_heater import DOMAIN as WATER_HEATER_DOMAIN
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, service

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
    LOGGER,
    SERVICE_CREDENTIAL_TYPES,
    USER_TYPE_REVERSE_MAP,
)
from .helpers import get_matter
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
