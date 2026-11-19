"""Button platform for Dominion SC Energy."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    COORDINATOR,
    DOMAIN,
    SERVICE_REWRITE_STATISTICS,
    SERVICE_RUN_BACKFILL,
    SERVICE_SYNC_ELECTRICITY_CATCHUP,
)
from .coordinator import DominionSCCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Dominion SC button entities."""
    coordinator: DominionSCCoordinator = hass.data[DOMAIN][entry.entry_id][COORDINATOR]
    async_add_entities([
        DominionSCBackfillButton(coordinator, entry),
        DominionSCElectricityCatchupButton(coordinator, entry),
        DominionSCRewriteStatisticsButton(coordinator, entry),
    ])


class DominionSCBackfillButton(CoordinatorEntity[DominionSCCoordinator], ButtonEntity):
    """Button to backfill one billing cycle (daily + hourly + monthly)."""

    _attr_has_entity_name = True
    _attr_name = "Run Backfill"
    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.entry_id}_run_backfill"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Unavailable while any backfill is running so it cannot be pressed again."""
        if self.coordinator.backfill_in_progress:
            return False
        return super().available

    async def async_press(self) -> None:
        """Trigger one-cycle backfill using integration service."""
        if self.coordinator.backfill_in_progress:
            return
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_RUN_BACKFILL,
            {ATTR_CONFIG_ENTRY_ID: self._entry_id, "overwrite": False},
            blocking=True,
        )


class DominionSCElectricityCatchupButton(CoordinatorEntity[DominionSCCoordinator], ButtonEntity):
    """Button to sync electricity for the open billing cycle (daily + hourly)."""

    _attr_has_entity_name = True
    _attr_name = "Sync Current Cycle"
    _attr_icon = "mdi:flash-clock"

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
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
        """Trigger current-billing-cycle electricity sync."""
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_SYNC_ELECTRICITY_CATCHUP,
            {ATTR_CONFIG_ENTRY_ID: self._entry_id},
            blocking=True,
        )


class DominionSCRewriteStatisticsButton(CoordinatorEntity[DominionSCCoordinator], ButtonEntity):
    """Button to trigger full historical statistics rewrite."""

    _attr_has_entity_name = True
    _attr_name = "Cleanup External Statistics"
    _attr_icon = "mdi:database-refresh"

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
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
