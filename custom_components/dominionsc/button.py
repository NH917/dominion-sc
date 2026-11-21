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
    SERVICE_RUN_BACKFILL_ALL,
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
        DominionSCBackfillAllButton(coordinator, entry),
        DominionSCElectricityCatchupButton(coordinator, entry),
        DominionSCRewriteStatisticsButton(coordinator, entry),
    ])


class DominionSCJobButton(CoordinatorEntity[DominionSCCoordinator], ButtonEntity):
    """Shared unavailable/guard behavior while any sync job is running."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Dominion SC Energy",
            manufacturer="Dominion Energy South Carolina",
            model="Utility Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Unavailable while any exclusive sync job is running."""
        if self.coordinator.backfill_in_progress:
            return False
        return super().available

    def _job_blocked(self) -> bool:
        return bool(self.coordinator.backfill_in_progress)


class DominionSCBackfillAllButton(DominionSCJobButton):
    """Button to drain all incomplete cycles up to the configured target."""

    _attr_name = "Run Backfill All"
    _attr_icon = "mdi:playlist-play"

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_run_backfill_all"

    async def async_press(self) -> None:
        """Trigger full backfill for every incomplete configured cycle."""
        if self._job_blocked():
            return
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_RUN_BACKFILL_ALL,
            {ATTR_CONFIG_ENTRY_ID: self._entry_id, "overwrite": False},
            blocking=True,
        )


class DominionSCElectricityCatchupButton(DominionSCJobButton):
    """Button to sync electric + gas for the open billing cycle (daily + hourly)."""

    _attr_name = "Sync Current Cycle"
    _attr_icon = "mdi:flash-clock"

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_sync_electricity_catchup"

    async def async_press(self) -> None:
        """Trigger current-billing-cycle electricity sync."""
        if self._job_blocked():
            return
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_SYNC_ELECTRICITY_CATCHUP,
            {ATTR_CONFIG_ENTRY_ID: self._entry_id},
            blocking=True,
        )


class DominionSCRewriteStatisticsButton(DominionSCJobButton):
    """Button to trigger full historical statistics rewrite."""

    _attr_name = "Cleanup External Statistics"
    _attr_icon = "mdi:database-refresh"

    def __init__(self, coordinator: DominionSCCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_rewrite_statistics"

    async def async_press(self) -> None:
        """Trigger statistics rewrite using integration service."""
        if self._job_blocked():
            return
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_REWRITE_STATISTICS,
            {ATTR_CONFIG_ENTRY_ID: self._entry_id},
            blocking=True,
        )
