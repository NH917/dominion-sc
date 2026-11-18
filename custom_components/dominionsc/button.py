"""Button platform for Dominion SC Energy."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    SERVICE_REWRITE_STATISTICS,
    SERVICE_RUN_BACKFILL,
    SERVICE_SYNC_ELECTRICITY_CATCHUP,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Dominion SC button entities."""
    async_add_entities([
        DominionSCBackfillButton(hass, entry),
        DominionSCElectricityCatchupButton(hass, entry),
        DominionSCRewriteStatisticsButton(hass, entry),
    ])


class DominionSCBackfillButton(ButtonEntity):
    """Button to run one backfill cycle via integration service."""

    _attr_has_entity_name = True
    _attr_name = "Run Backfill"
    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.entry_id}_run_backfill"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self) -> None:
        """Trigger one-cycle backfill using integration service."""
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_RUN_BACKFILL,
            {ATTR_CONFIG_ENTRY_ID: self._entry_id, "overwrite": False},
            blocking=True,
        )


class DominionSCElectricityCatchupButton(ButtonEntity):
    """Button to sync electricity from install date to now (no deep re-backfill)."""

    _attr_has_entity_name = True
    _attr_name = "Sync Electricity Since Install"
    _attr_icon = "mdi:flash-clock"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.entry_id}_sync_electricity_catchup"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self) -> None:
        """Trigger install→now electricity catch-up."""
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_SYNC_ELECTRICITY_CATCHUP,
            {
                ATTR_CONFIG_ENTRY_ID: self._entry_id,
                "full_since_install": True,
            },
            blocking=True,
        )


class DominionSCRewriteStatisticsButton(ButtonEntity):
    """Button to trigger full historical statistics rewrite."""

    _attr_has_entity_name = True
    _attr_name = "Cleanup External Statistics"
    _attr_icon = "mdi:database-refresh"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.entry_id}_rewrite_statistics"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self) -> None:
        """Trigger statistics rewrite using integration service."""
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_REWRITE_STATISTICS,
            {ATTR_CONFIG_ENTRY_ID: self._entry_id},
            blocking=True,
        )
