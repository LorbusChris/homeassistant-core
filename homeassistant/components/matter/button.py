"""Matter Button platform."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, override

from chip.clusters import Objects as clusters
from matter_server.client.models import device_types
from matter_server.common.custom_clusters import HeimanCluster

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_WIFI_CREDENTIALS_SOURCE
from .entity import MatterEntity, MatterEntityDescription
from .helpers import MatterConfigEntry, get_matter
from .models import MatterDiscoverySchema
from .wifi_credentials import async_import_credentials


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MatterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Matter Button platform."""
    matter = config_entry.runtime_data.adapter
    matter.register_platform_handler(Platform.BUTTON, async_add_entities)


@dataclass(frozen=True, kw_only=True)
class MatterButtonEntityDescription(ButtonEntityDescription, MatterEntityDescription):
    """Describe Matter Button entities."""

    command: Callable[[], Any] | None = None


class MatterCommandButton(MatterEntity, ButtonEntity):
    """Representation of a Matter Button entity."""

    entity_description: MatterButtonEntityDescription

    @override
    async def async_press(self) -> None:
        """Handle the button press leveraging a Matter command."""
        if TYPE_CHECKING:
            assert self.entity_description.command is not None
        await self.send_device_command(self.entity_description.command())


class MatterImportWifiCredentialsButton(MatterEntity, ButtonEntity):
    """Import the Wi-Fi credentials this network manager shares."""

    entity_description: MatterButtonEntityDescription

    @override
    async def async_press(self) -> None:
        """Lift any manual override and read this router's credentials."""
        matter = get_matter(self.hass)
        entry = matter.config_entry
        if CONF_WIFI_CREDENTIALS_SOURCE in entry.options:
            options = dict(entry.options)
            options.pop(CONF_WIFI_CREDENTIALS_SOURCE)
            self.hass.config_entries.async_update_entry(entry, options=options)
        await async_import_credentials(self.matter_client, self._endpoint)


# Discovery schema(s) to map Matter Attributes to HA entities
DISCOVERY_SCHEMAS = [
    MatterDiscoverySchema(
        platform=Platform.BUTTON,
        entity_description=MatterButtonEntityDescription(
            key="ImportWifiCredentialsButton",
            translation_key="import_wifi_credentials",
            entity_category=EntityCategory.CONFIG,
        ),
        entity_class=MatterImportWifiCredentialsButton,
        required_attributes=(clusters.WiFiNetworkManagement.Attributes.Ssid,),
        device_type=(device_types.NetworkInfrastructureManager,),
        # Null just means credential sharing is switched off right now.
        allow_none_value=True,
    ),
    MatterDiscoverySchema(
        platform=Platform.BUTTON,
        entity_description=MatterButtonEntityDescription(
            key="IdentifyButton",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=ButtonDeviceClass.IDENTIFY,
            command=lambda: clusters.Identify.Commands.Identify(identifyTime=15),
        ),
        entity_class=MatterCommandButton,
        required_attributes=(clusters.Identify.Attributes.IdentifyType,),
        value_is_not=clusters.Identify.Enums.IdentifyTypeEnum.kNone,
        allow_multi=True,
    ),
    MatterDiscoverySchema(
        platform=Platform.BUTTON,
        entity_description=MatterButtonEntityDescription(
            key="OperationalStatePauseButton",
            translation_key="pause",
            command=clusters.OperationalState.Commands.Pause,
        ),
        entity_class=MatterCommandButton,
        required_attributes=(clusters.OperationalState.Attributes.AcceptedCommandList,),
        value_contains=clusters.OperationalState.Commands.Pause.command_id,
        allow_multi=True,
    ),
    MatterDiscoverySchema(
        platform=Platform.BUTTON,
        entity_description=MatterButtonEntityDescription(
            key="OperationalStateResumeButton",
            translation_key="resume",
            command=clusters.OperationalState.Commands.Resume,
        ),
        entity_class=MatterCommandButton,
        required_attributes=(clusters.OperationalState.Attributes.AcceptedCommandList,),
        value_contains=clusters.OperationalState.Commands.Resume.command_id,
        allow_multi=True,
    ),
    MatterDiscoverySchema(
        platform=Platform.BUTTON,
        entity_description=MatterButtonEntityDescription(
            key="OperationalStateStartButton",
            translation_key="start",
            command=clusters.OperationalState.Commands.Start,
        ),
        entity_class=MatterCommandButton,
        required_attributes=(clusters.OperationalState.Attributes.AcceptedCommandList,),
        value_contains=clusters.OperationalState.Commands.Start.command_id,
        allow_multi=True,
    ),
    MatterDiscoverySchema(
        platform=Platform.BUTTON,
        entity_description=MatterButtonEntityDescription(
            key="OperationalStateStopButton",
            translation_key="stop",
            command=clusters.OperationalState.Commands.Stop,
        ),
        entity_class=MatterCommandButton,
        required_attributes=(clusters.OperationalState.Attributes.AcceptedCommandList,),
        value_contains=clusters.OperationalState.Commands.Stop.command_id,
        allow_multi=True,
    ),
    MatterDiscoverySchema(
        platform=Platform.BUTTON,
        entity_description=MatterButtonEntityDescription(
            key="HepaFilterMonitoringResetButton",
            translation_key="reset_filter_condition",
            command=clusters.HepaFilterMonitoring.Commands.ResetCondition,
        ),
        entity_class=MatterCommandButton,
        required_attributes=(
            clusters.HepaFilterMonitoring.Attributes.AcceptedCommandList,
        ),
        value_contains=clusters.HepaFilterMonitoring.Commands.ResetCondition.command_id,
        allow_multi=True,
    ),
    MatterDiscoverySchema(
        platform=Platform.BUTTON,
        entity_description=MatterButtonEntityDescription(
            key="ActivatedCarbonFilterMonitoringResetButton",
            translation_key="reset_filter_condition",
            command=clusters.ActivatedCarbonFilterMonitoring.Commands.ResetCondition,
        ),
        entity_class=MatterCommandButton,
        required_attributes=(
            clusters.ActivatedCarbonFilterMonitoring.Attributes.AcceptedCommandList,
        ),
        value_contains=clusters.ActivatedCarbonFilterMonitoring.Commands.ResetCondition.command_id,
        allow_multi=True,
    ),
    MatterDiscoverySchema(
        platform=Platform.BUTTON,
        entity_description=MatterButtonEntityDescription(
            key="SmokeCoAlarmSelfTestRequest",
            translation_key="self_test_request",
            entity_category=EntityCategory.DIAGNOSTIC,
            command=clusters.SmokeCoAlarm.Commands.SelfTestRequest,
        ),
        entity_class=MatterCommandButton,
        required_attributes=(clusters.SmokeCoAlarm.Attributes.AcceptedCommandList,),
        value_contains=clusters.SmokeCoAlarm.Commands.SelfTestRequest.command_id,
    ),
    MatterDiscoverySchema(
        platform=Platform.BUTTON,
        entity_description=MatterButtonEntityDescription(
            key="WaterHeaterManagementCancelBoost",
            translation_key="cancel_boost",
            command=clusters.WaterHeaterManagement.Commands.CancelBoost,
        ),
        entity_class=MatterCommandButton,
        required_attributes=(
            clusters.WaterHeaterManagement.Attributes.AcceptedCommandList,
        ),
        value_contains=clusters.WaterHeaterManagement.Commands.CancelBoost.command_id,
        allow_multi=True,  # Also used in water_heater
    ),
    MatterDiscoverySchema(
        platform=Platform.BUTTON,
        entity_description=MatterButtonEntityDescription(
            key="HeimanSmokeCoAlarmTemporaryMuteRequest",
            translation_key="temporary_mute_request",
            command=HeimanCluster.Commands.MutingSensor,
        ),
        entity_class=MatterCommandButton,
        required_attributes=(HeimanCluster.Attributes.AcceptedCommandList,),
        value_contains=HeimanCluster.Commands.MutingSensor.command_id,
    ),
]
