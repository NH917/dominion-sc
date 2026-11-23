"""Data coordinator for Dominion SC integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
import logging
import re
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

# Dominion SC Bidgely timestamps are Eastern local wall-clock values.
_DEFAULT_UTILITY_TZ = ZoneInfo("America/New_York")

from .const import (
    CONF_BACKFILL_CYCLES_TARGET,
    CONF_DAILY_ELECTRIC_CATCHUP,
    CONF_DAILY_LOOKBACK_DAYS,
    CONF_ENABLE_DAILY_STATISTICS,
    CONF_ENABLE_HOURLY_STATISTICS,
    CONF_ENABLE_MONTHLY_STATISTICS,
    CONF_INTERVAL_LOOKBACK_HOURS,
    CONF_PASSWORD,
    CONF_POLL_MINUTES,
    CONF_TFA_TOKEN,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DEFAULT_BACKFILL_CYCLES_TARGET,
    DEFAULT_CATCHUP_HOURLY_DAYS,
    DEFAULT_DAILY_ELECTRIC_CATCHUP,
    DEFAULT_DAILY_LOOKBACK_DAYS,
    DEFAULT_ENABLE_DAILY_STATISTICS,
    DEFAULT_ENABLE_HOURLY_STATISTICS,
    DEFAULT_ENABLE_MONTHLY_STATISTICS,
    DEFAULT_INTERVAL_LOOKBACK_HOURS,
    DEFAULT_POLL_MINUTES,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    STORE_KEY_PREFIX,
    STORE_VERSION,
    TOTAL_ELECTRIC_COST,
    TOTAL_ELECTRIC_KWH,
    TOTAL_GAS_COST,
    TOTAL_GAS_FT3,
)
from .dominion_sc_client import DominionSCClient

_LOGGER = logging.getLogger(__name__)

# Bidgely mode=day rows have been seen without ``consumption``; accept aliases.
_USAGE_VALUE_KEYS = ("consumption", "usage", "value", "consumptionValue", "kwh")
_COST_VALUE_KEYS = ("cost", "costValue", "amount")
_HOURLY_BACKFILL_MAX_EMPTY_ATTEMPTS = 5


@dataclass(frozen=True)
class BillingCycle:
    start: date
    end: date

    @property
    def key(self) -> str:
        return f"{self.start.isoformat()}|{self.end.isoformat()}"


class DominionSCCoordinator(DataUpdateCoordinator[dict[str, float]]):
    """Coordinate Dominion SC data fetches and persistent synthetic totals."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.config_entry = entry

        poll_minutes = int(entry.options.get(CONF_POLL_MINUTES, entry.data.get(CONF_POLL_MINUTES, DEFAULT_POLL_MINUTES)))
        update_interval = timedelta(minutes=max(1, poll_minutes))

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=update_interval,
        )

        verify_ssl = bool(entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL))
        self._client = DominionSCClient(verify_ssl=verify_ssl)
        self._store = Store[dict[str, Any]](hass, STORE_VERSION, f"{STORE_KEY_PREFIX}{entry.entry_id}")
        self._state: dict[str, Any] = self._default_state()
        # Prevent overlapping long jobs (backfill / catchup / stats rewrite).
        self._backfill_busy = False

    async def async_setup(self) -> None:
        """Load persisted state before first refresh."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self._state = self._merge_state(stored)
        # Recompute totals from any persisted ledgers if totals appear missing
        # or were not written by older state formats. This ensures the
        # synthetic cumulative totals are available immediately after a
        # restart and avoid negative Energy Dashboard deltas when recorder
        # statistics exist but in-memory totals are zero.
        try:
            self._recompute_totals_from_ledgers()
        except Exception:  # defensive: do not let a recompute break startup
            _LOGGER.debug("Could not recompute totals from ledgers on setup", exc_info=True)

        # Stamp install time once so catch-up can bound itself to "since integrated"
        self._ensure_installed_at()

        # Upgrades from older builds: if historical cycles were already drained,
        # do not re-run the one-time initial backfill on next startup.
        if not self._state.get("initial_backfill_done"):
            completed = self._state.get("backfill", {}).get("completed_cycles") or []
            missing = self._state.get("backfill", {}).get("missing_cycles") or []
            if completed and not missing:
                self._state["initial_backfill_done"] = True

        # One-time: Bidgely midnight intervalEnd was stored as the next calendar
        # day and stats used UTC midnight (Energy bars at ~7–8pm prior evening).
        self._migrate_day_ahead_and_utc_stats()

        # Ensure backfill cycles are properly initialized after setup
        self._initialize_backfill_cycles()
        
    @property
    def totals(self) -> dict[str, float]:
        return self._state["totals"]

    @property
    def backfill(self) -> dict[str, Any]:
        return self._state["backfill"]

    @property
    def backfill_in_progress(self) -> bool:
        """True while any exclusive sync job is running (backfill, catchup, rewrite)."""
        return bool(getattr(self, "_backfill_busy", False))

    def _try_begin_backfill(self, reason: str) -> bool:
        """Acquire the exclusive sync-job lock. Returns False if already running."""
        if self._backfill_busy:
            _LOGGER.warning(
                "Sync job already in progress for entry=%s; ignoring %s",
                self.config_entry.entry_id,
                reason,
            )
            return False
        self._backfill_busy = True
        # Notify button entities so they become unavailable.
        try:
            self._publish_coordinator_data()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Could not publish sync-job-busy state", exc_info=True)
        return True

    def _end_backfill(self) -> None:
        """Release the exclusive sync-job lock and refresh entities."""
        self._backfill_busy = False
        try:
            self._publish_coordinator_data()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Could not publish sync-job-idle state", exc_info=True)

    @property
    def account_summary(self) -> dict[str, Any]:
        """Return the latest fetched account/billing payload.

        This value is populated during updates and may be an empty dict
        if no data has been fetched yet.
        """
        return self._state.get("account_summary", {}) or {}

    @property
    def bill_projection(self) -> dict[str, Any]:
        """Return the latest fetched bill projection payload."""
        return self._state.get("bill_projection", {}) or {}

    @property
    def current_bill_summary(self) -> dict[str, Any]:
        """Return the latest summarized current bill payload (bill_summary)."""
        return self._state.get("current_bill_summary", {}) or {}

    @property
    def current_daily_usage(self) -> dict[str, Any]:
        """Return the latest fetched current daily usage payload."""
        return self._state.get("current_daily_usage", {}) or {}

    @property
    def backfill_cycles_target(self) -> int:
        """Return the currently configured backfill cycles target."""
        return int(
            self.config_entry.options.get(
                CONF_BACKFILL_CYCLES_TARGET,
                self.config_entry.data.get(CONF_BACKFILL_CYCLES_TARGET, DEFAULT_BACKFILL_CYCLES_TARGET),
            )
        )

    def _publish_coordinator_data(self) -> None:
        """Push totals + backfill remaining so diagnostic sensors always refresh."""
        payload = dict(self.totals)
        payload["backfill_remaining"] = self.backfill_remaining_count
        payload["backfill_pending_hourly"] = len(self._pending_hourly_cycles())
        try:
            self.async_set_updated_data(payload)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Could not publish coordinator data", exc_info=True)

    @property
    def backfill_remaining_count(self) -> int:
        """Daily-incomplete + hourly-pending cycle count for the Remaining sensor."""
        return len(self.backfill_summary.get("remaining_cycles", []))

    @property
    def backfill_summary(self) -> dict[str, Any]:
        """Compute a consistent backfill summary based on current target.

        Returns a dict with:
        - target: configured backfill cycles target
        - eligible_keys: cycle keys eligible for the current target
        - completed_cycles: all-time completed daily cycle keys (persistent)
        - completed_in_scope: completed daily cycles in the current eligible set
        - incomplete_cycles: eligible daily cycles not yet completed
        - pending_hourly_cycles: daily-complete cycles still missing hourly intervals
        - hourly_exhausted_cycles: pending hourly cycles that hit the empty-attempt cap
          (still counted in remaining; scheduled polls stop retrying them)
        - remaining_cycles: daily incomplete + pending hourly (what the Remaining sensor shows)
        """
        target = self.backfill_cycles_target
        eligible = self._build_recent_monthly_cycles(
            now_date=self._utility_today(), target=target
        )
        eligible_keys = [c.key for c in eligible]
        all_completed = set(self.backfill.get("completed_cycles", []))

        completed_in_scope = [k for k in eligible_keys if k in all_completed]
        incomplete = [k for k in eligible_keys if k not in all_completed]
        # Newest-first pending hourly from ledger coverage (not just a sticky flag).
        pending_hourly = [c.key for c in self._pending_hourly_cycles()]
        # Remaining work: unfinished daily cycles + daily-done cycles awaiting hourly.
        # These sets are disjoint (hourly pending requires daily completed).
        remaining = list(incomplete) + [
            k for k in pending_hourly if k not in incomplete
        ]

        return {
            "target": target,
            "eligible_keys": eligible_keys,
            "completed_cycles": sorted(all_completed),
            "completed_in_scope": completed_in_scope,
            "incomplete_cycles": incomplete,
            "pending_hourly_cycles": pending_hourly,
            "hourly_exhausted_cycles": [k for k in pending_hourly if self._hourly_exhausted(k)],
            "remaining_cycles": remaining,
        }

    @property
    def current_billing_cycle(self) -> BillingCycle:
        """Return current billing cycle using fetched projection/current payloads.

        Prefer `bill_projection.billStartDateFormatted` / `billEndDateFormatted` (ISO)
        or `billStartDate` / `billEndDate` (human) when available. Fall back to
        current_daily_usage fields, then to month-start -> today.
        """
        bp = self._state.get("bill_projection") or {}
        start = None
        end = None

        def _try_parse(s: str) -> date | None:
            if not s:
                return None
            # Try ISO first
            try:
                return date.fromisoformat(s)
            except Exception:
                pass
            # Try common human-readable format like 'Mar 9, 2026'
            try:
                return datetime.strptime(s, "%b %d, %Y").date()
            except Exception:
                return None

        # Try bill_projection first
        if isinstance(bp, dict):
            sd = bp.get("billStartDateFormatted") or bp.get("billStartDate")
            ed = bp.get("billEndDateFormatted") or bp.get("billEndDate")
            start = _try_parse(sd) if sd else None
            end = _try_parse(ed) if ed else None

        # Debug: log what we found in bill_projection
        if _LOGGER.isEnabledFor(logging.DEBUG):
            try:
                _LOGGER.debug(
                    "Billing cycle source: bill_projection sd=%s ed=%s parsed_start=%s parsed_end=%s",
                    bp.get("billStartDateFormatted") or bp.get("billStartDate"),
                    bp.get("billEndDateFormatted") or bp.get("billEndDate"),
                    start.isoformat() if start else None,
                    end.isoformat() if end else None,
                )
            except Exception:
                _LOGGER.debug("Billing cycle source: bill_projection present but could not log values")

        # Fallback: current_daily_usage payload
        if (not start or not end):
            cd = self._state.get("current_daily_usage") or {}
            sd = cd.get("billStartDateFormatted") or cd.get("billStartDate")
            ed = cd.get("billEndDateFormatted") or cd.get("billEndDate")
            if not start and sd:
                start = _try_parse(sd)
            if not end and ed:
                end = _try_parse(ed)

        # Debug: log what we found in current_daily_usage
        if _LOGGER.isEnabledFor(logging.DEBUG):
            try:
                _LOGGER.debug(
                    "Billing cycle source: current_daily_usage sd=%s ed=%s parsed_start=%s parsed_end=%s",
                    cd.get("billStartDateFormatted") or cd.get("billStartDate"),
                    cd.get("billEndDateFormatted") or cd.get("billEndDate"),
                    start.isoformat() if start else None,
                    end.isoformat() if end else None,
                )
            except Exception:
                _LOGGER.debug("Billing cycle source: current_daily_usage present but could not log values")

        if start and end:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Resolved current billing cycle: start=%s end=%s",
                    start.isoformat(), end.isoformat(),
                )
            return BillingCycle(start=start, end=end)

        # Final fallback: month start to today (utility calendar)
        now = self._utility_today()
        start = date(now.year, now.month, 1)
        return BillingCycle(start=start, end=now)

    @property
    def last_sync(self) -> str | None:
        """Return ISO 8601 timestamp string for last successful sync, or None."""
        return self._state.get("last_sync")

    async def async_run_backfill_all(self, overwrite: bool = False) -> None:
        """Manually process all incomplete backfill cycles."""
        if not self._try_begin_backfill("manual full backfill"):
            return
        try:
            await self._run_backfill_all_unlocked(overwrite=overwrite)
        finally:
            self._end_backfill()

    async def _run_backfill_all_unlocked(self, *, overwrite: bool = False) -> None:
        """Full backfill body (caller must hold the busy lock)."""
        _LOGGER.debug(
            "Manual full backfill requested: entry=%s overwrite=%s",
            self.config_entry.entry_id,
            overwrite,
        )
        await self._ensure_authenticated()

        if not self._state["backfill"].get("missing_cycles"):
            self._initialize_backfill_cycles()

        while self._state["backfill"].get("missing_cycles"):
            await self._process_backfill(overwrite=overwrite, allow_initialize_missing=False)

        for cycle in self._pending_hourly_cycles():
            await self._backfill_hourly_for_cycle(cycle, overwrite=overwrite)

        await self._sync_external_statistics(force_rewrite=True)
        await self._sync_hourly_external_statistics(force_rewrite=True)
        await self._sync_monthly_external_statistics(force_rewrite=True)
        self._apply_monotonic_guard()
        self._set_last_sync()
        await self._save_state()
        self._publish_coordinator_data()
        _LOGGER.info(
            "Manual full backfill finished: entry=%s remaining=%s pending_hourly=%s",
            self.config_entry.entry_id,
            self.backfill_remaining_count,
            len(self._pending_hourly_cycles()),
        )

    async def async_rewrite_statistics(self) -> None:
        """Clear legacy/external statistic IDs, then force-rewrite all series from ledgers."""
        if not self._try_begin_backfill("manual statistics rewrite"):
            return
        try:
            await self._rewrite_statistics_unlocked()
        finally:
            self._end_backfill()

    async def _rewrite_statistics_unlocked(self) -> None:
        """Statistics cleanup/rewrite body (caller must hold the busy lock)."""
        _LOGGER.info(
            "Manual statistics rewrite requested: entry=%s",
            self.config_entry.entry_id,
        )
        await self._clear_external_statistics()
        await self._sync_external_statistics(force_rewrite=True)
        await self._sync_hourly_external_statistics(force_rewrite=True)
        await self._sync_monthly_external_statistics(force_rewrite=True)
        self._set_last_sync()
        await self._save_state()
        self._publish_coordinator_data()
        _LOGGER.info(
            "Manual statistics rewrite finished: entry=%s",
            self.config_entry.entry_id,
        )

    async def async_maybe_run_initial_backfill(self) -> None:
        """On first integration, drain the historical month-cycle backfill queue once.

        Subsequent catch-up (button / daily) syncs the open billing cycle or
        watermark window and does not re-queue completed historical cycles.
        """
        self._ensure_installed_at()
        if self._state.get("initial_backfill_done"):
            _LOGGER.debug(
                "Initial backfill already completed for entry=%s",
                self.config_entry.entry_id,
            )
            return
        if not self._try_begin_backfill("initial historical backfill"):
            return

        _LOGGER.info(
            "Starting one-time initial historical backfill for entry=%s",
            self.config_entry.entry_id,
        )
        try:
            await self._run_backfill_all_unlocked(overwrite=False)
            # Fill the open billing cycle (daily + hourly) after historical months.
            # Already holding the busy lock — call the unlocked body directly.
            await self._sync_current_cycle_unlocked()
            self._state["initial_backfill_done"] = True
            await self._save_state()
            _LOGGER.info(
                "Initial historical backfill finished for entry=%s",
                self.config_entry.entry_id,
            )
        except Exception as err:  # pylint: disable=broad-except
            # Leave initial_backfill_done=False so the next daily/poll can retry.
            _LOGGER.warning(
                "Initial backfill failed for entry=%s: %s",
                self.config_entry.entry_id,
                err,
            )
        finally:
            self._end_backfill()

    async def async_sync_current_cycle(self) -> None:
        """Fill electric + gas usage/cost for the open billing cycle (daily + hourly).

        Uses ``current_billing_cycle`` (bill projection / current usage), clamped
        to today so a future bill-end date does not request unfinished days.

        Also covers the current calendar month from the 1st when that is earlier
        than the billing-cycle start, so Energy's open month is not left empty.
        Daily rows come from Bidgely (with per-day gap fill) plus Dominion's
        Fusion ``GetAccountDailyUsageDetails`` portal payload.
        """
        if not self._try_begin_backfill("manual sync current cycle"):
            return
        try:
            await self._sync_current_cycle_unlocked()
        finally:
            self._end_backfill()

    async def _sync_current_cycle_unlocked(self) -> None:
        """Current-cycle sync body (caller must hold the busy lock)."""
        await self._ensure_authenticated()
        self._ensure_installed_at()

        # Refresh cycle bounds before sync — cached projection can be stale.
        try:
            bill_projection = await self.hass.async_add_executor_job(
                self._client.get_bill_projection
            )
            self._state["bill_projection"] = bill_projection
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Sync current cycle: could not refresh bill_projection: %s", err)

        today = self._utility_today()
        cycle = self.current_billing_cycle
        month_start = date(today.year, today.month, 1)
        # Prefer the earlier of billing-cycle start and calendar month start so
        # days from the 1st are not skipped when the bill period began mid-month.
        start = min(cycle.start, month_start)
        # Always sync through today (not a stale billEndDate stuck on yesterday).
        end = today
        if start > end:
            start = month_start if month_start <= end else end

        _LOGGER.info(
            "Sync current cycle: entry=%s start=%s end=%s (cycle=%s..%s)",
            self.config_entry.entry_id,
            start.isoformat(),
            end.isoformat(),
            cycle.start.isoformat(),
            cycle.end.isoformat(),
        )

        await self._catchup_electric_daily(start, end)
        utility_tz = self._local_tz()
        start_dt = datetime.combine(start, time.min, tzinfo=utility_tz)
        end_dt = datetime.combine(end, time.max, tzinfo=utility_tz)
        await self._store_interval_window(
            start_dt, end_dt, electric_only=False, overwrite=True
        )
        await self._monthly_reconcile()
        # Rewrite daily stats so newly filled earlier-month days replace a
        # watermark that previously only had yesterday.
        await self._sync_external_statistics(force_rewrite=True)
        await self._sync_hourly_external_statistics(force_rewrite=True)
        await self._sync_monthly_external_statistics(force_rewrite=False)
        self._apply_monotonic_guard()

        catchup = self._state.setdefault(
            "electric_catchup",
            {"last_run": None, "watermark_date": None},
        )
        catchup["last_run"] = self._utility_now().isoformat()
        catchup["watermark_date"] = today.isoformat()
        self._set_last_sync()
        await self._save_state()
        self._publish_coordinator_data()
        _LOGGER.debug(
            "Sync current cycle finished: entry=%s watermark=%s",
            self.config_entry.entry_id,
            catchup["watermark_date"],
        )

    async def async_sync_electricity_catchup(self, *, full_since_install: bool = False) -> None:
        """Incremental electricity catch-up used by the daily schedule.

        ``full_since_install=True``: daily rows from installed_at → today, plus a
        capped hourly window.
        ``full_since_install=False`` (default): from catch-up watermark minus
        lookback → today.
        """
        if not self._try_begin_backfill("scheduled electricity catch-up"):
            return
        try:
            await self._sync_electricity_catchup_unlocked(
                full_since_install=full_since_install
            )
        finally:
            self._end_backfill()

    async def _sync_electricity_catchup_unlocked(
        self, *, full_since_install: bool = False
    ) -> None:
        """Scheduled catch-up body (caller must hold the busy lock)."""
        await self._ensure_authenticated()
        self._ensure_installed_at()

        today = self._utility_today()
        installed = self._installed_at_date()

        lookback = self._daily_lookback_days()

        catchup = self._state.setdefault(
            "electric_catchup",
            {"last_run": None, "watermark_date": None},
        )
        if full_since_install:
            start = installed
        else:
            watermark_raw = catchup.get("watermark_date")
            try:
                watermark = date.fromisoformat(str(watermark_raw)) if watermark_raw else installed
            except ValueError:
                watermark = installed
            start = max(installed, watermark - timedelta(days=lookback))

        if start > today:
            start = today

        _LOGGER.info(
            "Electricity catch-up: entry=%s full_since_install=%s start=%s end=%s",
            self.config_entry.entry_id,
            full_since_install,
            start.isoformat(),
            today.isoformat(),
        )

        await self._catchup_electric_daily(start, today)
        await self._catchup_electric_intervals(start, today)
        await self._monthly_reconcile()
        # Force hourly rewrite so Dominion SC Hourly appears in the Energy picker
        # after catch-up (metadata is only registered once points are imported).
        await self._sync_external_statistics(force_rewrite=False)
        await self._sync_hourly_external_statistics(force_rewrite=True)
        await self._sync_monthly_external_statistics(force_rewrite=False)
        self._apply_monotonic_guard()

        catchup["last_run"] = self._utility_now().isoformat()
        catchup["watermark_date"] = today.isoformat()
        self._set_last_sync()
        await self._save_state()
        self._publish_coordinator_data()
        _LOGGER.debug(
            "Electricity catch-up finished: entry=%s watermark=%s",
            self.config_entry.entry_id,
            catchup["watermark_date"],
        )

    async def async_recalculate_backfill_target(self, new_target: int) -> None:
        """Recalculate missing backfill cycles when the target changes.

        If the target increased, newly eligible cycles that are not already
        completed are added to missing_cycles.  If decreased, cycles beyond
        the new target that have not yet been backfilled are removed.
        """
        old_target = int(
            self.config_entry.data.get(
                CONF_BACKFILL_CYCLES_TARGET, DEFAULT_BACKFILL_CYCLES_TARGET
            )
        )
        # Also check what was previously stored in state
        state_target = self._state.get("backfill_cycles_target", old_target)
        if new_target == state_target:
            _LOGGER.debug("Backfill target unchanged at %d", state_target)
            return

        _LOGGER.info(
            "Backfill target changed: %d -> %d, recalculating missing cycles",
            state_target,
            new_target,
        )

        # Compute eligible cycles for the new target
        all_eligible = self._build_recent_monthly_cycles(
            now_date=self._utility_today(), target=new_target
        )
        all_eligible_keys = [c.key for c in all_eligible]

        completed = set(self._state["backfill"].get("completed_cycles", []))
        new_missing = [k for k in all_eligible_keys if k not in completed]

        old_missing = self._state["backfill"].get("missing_cycles", [])
        _LOGGER.debug(
            "Backfill recalc: eligible=%d, completed=%d, "
            "old_missing=%d, new_missing=%d",
            len(all_eligible_keys),
            len(completed),
            len(old_missing),
            len(new_missing),
        )

        self._state["backfill"]["missing_cycles"] = new_missing
        self._state["backfill_cycles_target"] = new_target
        await self._save_state()
        self.async_set_updated_data(dict(self.totals))

    async def _async_update_data(self) -> dict[str, float]:
        try:
            await self._ensure_authenticated()
            # Fetch account and billing data used by informational sensors.
            # Use the executor because the client uses blocking requests.
            try:
                account_summary = await self.hass.async_add_executor_job(self._client.get_account_summary)
                self._state["account_summary"] = account_summary
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("Could not fetch account_summary: %s", err)

            try:
                current_daily = await self.hass.async_add_executor_job(self._client.get_current_daily_usage)
                # store entire current_daily payload and the summarized bill_summary
                self._state["current_daily_usage"] = current_daily
                self._state["current_bill_summary"] = current_daily.get("bill_summary", {})
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("Could not fetch current_daily_usage: %s", err)

            try:
                bill_projection = await self.hass.async_add_executor_job(self._client.get_bill_projection)
                self._state["bill_projection"] = bill_projection
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("Could not fetch bill_projection: %s", err)
            utility_now = self._utility_now()
            await self._process_intervals(utility_now)
            await self._daily_reconcile(utility_now.date())
            await self._monthly_reconcile()
            # Process at most one missing backfill cycle per scheduled update
            # (initial install also drains the full queue via async_maybe_run_initial_backfill).
            # Hold the busy lock so buttons stay unavailable during the job.
            if self._try_begin_backfill("scheduled one-cycle backfill"):
                try:
                    await self._process_backfill(overwrite=False, retry_exhausted_hourly=False)
                finally:
                    self._end_backfill()
            # else: a manual/initial backfill is already running; skip this poll's cycle
            # One-time migration: clear auto-compiled recorder rows that
            # conflict with our imported statistics after state_class removal.
            force_rewrite = self._check_statistics_migration()
            # One-time fix: re-create external statistics with proper unit_class
            force_rewrite = self._check_unit_class_fix() or force_rewrite
            force_rewrite = bool(self._state.pop("_force_stats_rewrite", False)) or force_rewrite
            await self._sync_external_statistics(force_rewrite=force_rewrite)
            await self._sync_hourly_external_statistics(force_rewrite=force_rewrite)
            await self._sync_monthly_external_statistics(force_rewrite=force_rewrite)
            # Cadence:
            # - hourly/daily series: commit each completed hour
            # - monthly series: commit only after a month ends
            # - once-per-day catch-up still fills gaps since install watermark
            await self._maybe_hourly_electric_catchup()
            await self._maybe_monthly_electric_catchup()
            await self._maybe_daily_electric_catchup()
            self._apply_monotonic_guard()
            # Update last_sync timestamp for scheduled/automatic update
            self._set_last_sync()
            await self._save_state()
            return dict(self.totals)
        except Exception as err:  # pylint: disable=broad-except
            raise UpdateFailed(str(err)) from err

    async def _clear_external_statistics(self) -> None:
        """Clear all legacy statistic IDs (both old dominionsc:entry_* and sensor.* formats)."""
        try:
            from homeassistant.components.recorder import get_instance  # pylint: disable=import-outside-toplevel
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Recorder unavailable while clearing external stats: %s", err)
            return

        statistic_ids = [
            stat_id
            for stat_id in (
                self.get_legacy_external_statistic_id(TOTAL_ELECTRIC_KWH),
                self.get_legacy_external_statistic_id(TOTAL_GAS_FT3),
                self.get_statistic_id(TOTAL_ELECTRIC_KWH),
                self.get_statistic_id(TOTAL_GAS_FT3),
                self.get_statistic_id(TOTAL_ELECTRIC_COST),
                self.get_statistic_id(TOTAL_GAS_COST),
                self.get_hourly_statistic_id(TOTAL_ELECTRIC_KWH),
                self.get_hourly_statistic_id(TOTAL_GAS_FT3),
                self.get_hourly_statistic_id(TOTAL_ELECTRIC_COST),
                self.get_hourly_statistic_id(TOTAL_GAS_COST),
                self.get_monthly_statistic_id(TOTAL_ELECTRIC_KWH),
                self.get_monthly_statistic_id(TOTAL_GAS_FT3),
                self.get_monthly_statistic_id(TOTAL_ELECTRIC_COST),
                self.get_monthly_statistic_id(TOTAL_GAS_COST),
            )
            if stat_id
        ]
        # Also clear old sensor.* based statistic IDs that conflicted with
        # the recorder's auto-compilation (the previous architecture).
        statistic_ids.extend(self.get_legacy_sensor_statistic_ids())

        if not statistic_ids:
            return

        recorder = get_instance(self.hass)
        recorder.async_clear_statistics(statistic_ids)
        _LOGGER.info("Cleared legacy/external statistic IDs: %s", statistic_ids)

    def get_statistic_id(self, total_key: str) -> str | None:
        """Return external daily statistic_id for Energy Dashboard historical data.

        Uses the 'dominionsc' source prefix so these statistics are completely
        separate from the HA recorder's auto-compiled entity statistics.  This
        avoids conflicts where the recorder writes sum=0 rows after restarts
        that create negative deltas in the Energy Dashboard.

        External statistics appear in the Energy Dashboard picker under
        'External statistics' and can be selected for grid/gas/cost tracking.
        """
        mapping = {
            TOTAL_ELECTRIC_KWH: f"{DOMAIN}:electric_cumulative_consumption",
            TOTAL_GAS_FT3: f"{DOMAIN}:gas_cumulative_consumption",
            TOTAL_ELECTRIC_COST: f"{DOMAIN}:electric_cumulative_cost",
            TOTAL_GAS_COST: f"{DOMAIN}:gas_cumulative_cost",
        }
        return mapping.get(total_key)

    def get_hourly_statistic_id(self, total_key: str) -> str | None:
        """Return external hourly statistic_id (interval data aggregated to hours)."""
        mapping = {
            TOTAL_ELECTRIC_KWH: f"{DOMAIN}:electric_hourly_consumption",
            TOTAL_GAS_FT3: f"{DOMAIN}:gas_hourly_consumption",
            TOTAL_ELECTRIC_COST: f"{DOMAIN}:electric_hourly_cost",
            TOTAL_GAS_COST: f"{DOMAIN}:gas_hourly_cost",
        }
        return mapping.get(total_key)

    def get_monthly_statistic_id(self, total_key: str) -> str | None:
        """Return external monthly statistic_id (Bidgely mode=year buckets)."""
        mapping = {
            TOTAL_ELECTRIC_KWH: f"{DOMAIN}:electric_monthly_consumption",
            TOTAL_GAS_FT3: f"{DOMAIN}:gas_monthly_consumption",
            TOTAL_ELECTRIC_COST: f"{DOMAIN}:electric_monthly_cost",
            TOTAL_GAS_COST: f"{DOMAIN}:gas_monthly_cost",
        }
        return mapping.get(total_key)

    def get_legacy_external_statistic_id(self, total_key: str) -> str | None:
        """Return previous custom external statistic_id used by older integration versions."""
        entry_id = re.sub(r"[^a-z0-9_]", "_", str(self.config_entry.entry_id).lower())
        mapping = {
            TOTAL_ELECTRIC_KWH: f"{DOMAIN}:{entry_id}_electric_consumption",
            TOTAL_GAS_FT3: f"{DOMAIN}:{entry_id}_gas_consumption",
        }
        return mapping.get(total_key)

    def get_legacy_sensor_statistic_ids(self) -> list[str]:
        """Return previous sensor.* statistic_ids that conflicted with recorder.

        These were used before switching to external statistics and caused
        negative deltas because the HA recorder also auto-compiled into them.
        """
        base = "dominion_sc_energy"
        return [
            f"sensor.{base}_electric_cumulative_consumption",
            f"sensor.{base}_gas_cumulative_consumption",
            f"sensor.{base}_electric_cumulative_cost",
            f"sensor.{base}_gas_cumulative_cost",
        ]

    def _option_bool(self, key: str, default: bool) -> bool:
        """Read a boolean from options, falling back to entry data then default."""
        return bool(
            self.config_entry.options.get(
                key,
                self.config_entry.data.get(key, default),
            )
        )

    def _hourly_statistics_enabled(self) -> bool:
        return self._option_bool(
            CONF_ENABLE_HOURLY_STATISTICS, DEFAULT_ENABLE_HOURLY_STATISTICS
        )

    def _daily_statistics_enabled(self) -> bool:
        return self._option_bool(
            CONF_ENABLE_DAILY_STATISTICS, DEFAULT_ENABLE_DAILY_STATISTICS
        )

    def _monthly_statistics_enabled(self) -> bool:
        return self._option_bool(
            CONF_ENABLE_MONTHLY_STATISTICS, DEFAULT_ENABLE_MONTHLY_STATISTICS
        )

    async def _sync_external_statistics(self, *, force_rewrite: bool = False) -> None:
        """Import historical daily usage into external dominionsc:* statistics with day timestamps.

        Uses external statistics (source='dominionsc') to avoid conflicts with
        the HA recorder's auto-compiled entity statistics. The recorder compiles
        hourly statistics for sensors with state_class and can reset sum=0 after
        restarts; external statistics are completely independent of that process.
        """
        if not self._daily_statistics_enabled():
            _LOGGER.debug("Daily statistics sync skipped: disabled in options")
            return
        try:
            from homeassistant.components.recorder import get_instance  # pylint: disable=import-outside-toplevel
            from homeassistant.components.recorder.statistics import (  # pylint: disable=import-outside-toplevel
                StatisticData,
                StatisticMetaData,
                StatisticMeanType,
                async_add_external_statistics,
            )
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Recorder statistics import unavailable: %s", err)
            return

        statistics_state = self._state.setdefault("statistics_import", {"electric": [], "gas": []})
        fuel_config: list[tuple[str, str, str, str, str | None]] = [
            # (series_key, total_key, unit, ledger_name, unit_class)
            ("electric", TOTAL_ELECTRIC_KWH, "kWh", "daily_ledger", "energy"),
            ("gas", TOTAL_GAS_FT3, "ft³", "daily_ledger", "volume"),
            ("electric_cost", TOTAL_ELECTRIC_COST, "$", "daily_cost_ledger", None),
            ("gas_cost", TOTAL_GAS_COST, "$", "daily_cost_ledger", None),
        ]

        for series_key, total_key, unit, ledger_name, unit_class in fuel_config:
            # For cost series, the ledger keys use "electric|date" / "gas|date" (no "_cost" prefix)
            fuel_prefix = series_key.replace("_cost", "")
            imported_days_raw = statistics_state.setdefault(series_key, [])
            imported_days = set(imported_days_raw if isinstance(imported_days_raw, list) else [])

            daily_points: list[tuple[date, float]] = []
            today = self._utility_today()

            for key, value in self._state.get(ledger_name, {}).items():
                if not isinstance(key, str) or not key.startswith(f"{fuel_prefix}|"):
                    continue
                day_str = key.split("|", 1)[1]
                try:
                    day = date.fromisoformat(day_str)
                except ValueError:
                    continue

                consumption = max(float(value), 0.0) if value is not None else 0.0

                # Skip future dates. Today is allowed so the daily Energy series
                # can update at each completed hour (progressive in-day total).
                if day > today:
                    _LOGGER.debug(
                        "Statistics sync filtering future date: %s=%s (%s)",
                        key, consumption, series_key,
                    )
                    continue

                # Zeros stay null — never import a 0 statistic point.
                if consumption == 0.0:
                    _LOGGER.debug(
                        "Statistics sync filtering zero row (null): %s (%s)",
                        key,
                        series_key,
                    )
                    continue

                daily_points.append((day, consumption))

            if not daily_points:
                _LOGGER.debug("Statistics sync skipped for %s: no daily points", series_key)
                continue

            daily_points.sort(key=lambda item: item[0])
            # Build a set of available day strings from the ledger points so
            # we can detect when previously imported days are missing from
            # the current ledger. If any previously imported day is not
            # present, force a rewrite to avoid importing a smaller
            # cumulative sequence (which would create negative deltas).
            point_day_set = {d.isoformat() for d, _ in daily_points}
            running_sum = 0.0
            to_import: list[StatisticData] = []

            statistic_id = self.get_statistic_id(total_key)
            if not statistic_id:
                continue

            _LOGGER.debug(
                "Statistics sync start: series=%s force_rewrite=%s points=%s imported_days=%s statistic_id=%s",
                series_key,
                force_rewrite,
                len(daily_points),
                len(imported_days),
                statistic_id,
            )

            rewrite_for_fuel = force_rewrite
            if not rewrite_for_fuel and imported_days:
                try:
                    max_imported_day = max(date.fromisoformat(day_key) for day_key in imported_days)
                except ValueError:
                    max_imported_day = None
                if max_imported_day is not None:
                    has_older_new_day = any(
                        point_day < max_imported_day and point_day.isoformat() not in imported_days
                        for point_day, _ in daily_points
                    )
                    if has_older_new_day:
                        rewrite_for_fuel = True
                        _LOGGER.info(
                            "Statistics sync forcing rewrite for %s due to newly discovered older days",
                            series_key,
                        )
                # If any previously imported day is missing from the ledger
                # we must force a rewrite. This can happen after state restore
                # / reboot if the ledger was changed or filtered differently
                # and helps avoid importing cumulative values that are lower
                # than what was previously written to recorder.
                if any(day_key not in point_day_set for day_key in imported_days):
                    rewrite_for_fuel = True
                    _LOGGER.info(
                        "Statistics sync forcing rewrite for %s because previously imported days are missing from ledger",
                        series_key,
                    )

            # Detect cumulative sum drift: if ledger values for previously-
            # imported days changed (e.g. daily_reconcile overwrites with
            # updated API data), the running_sum at max_imported_day will
            # differ from what was written to the recorder last time. Appending
            # new points with shifted sums creates negative deltas in the
            # Energy Dashboard. Force a rewrite to correct the full series.
            if not rewrite_for_fuel and imported_days:
                max_imported_iso = max(imported_days)
                precompute_sum = 0.0
                for day, consumption in daily_points:
                    precompute_sum += consumption
                    if day.isoformat() == max_imported_iso:
                        break
                stored_sum_key = f"{series_key}_sum_at_max"
                stored_max_sum = statistics_state.get(stored_sum_key)
                if stored_max_sum is not None and abs(precompute_sum - stored_max_sum) > 0.001:
                    rewrite_for_fuel = True
                    _LOGGER.info(
                        "Statistics sync forcing rewrite for %s: cumulative sum at max imported day "
                        "changed (stored=%.3f current=%.3f) — ledger values were updated overnight",
                        series_key, stored_max_sum, precompute_sum,
                    )

            if rewrite_for_fuel:
                imported_days.clear()
                recorder = get_instance(self.hass)
                # NOTE: Despite the async-style name, this is not an awaitable coroutine
                # in current HA recorder API. Do not add `await` here.
                recorder.async_clear_statistics([statistic_id])
                _LOGGER.info("Cleared existing statistics for %s", statistic_id)

            for day, consumption in daily_points:
                running_sum += consumption
                day_key = day.isoformat()
                if day_key in imported_days:
                    continue
                to_import.append(
                    StatisticData(
                        start=self._local_midnight(day),
                        state=running_sum,
                        sum=running_sum,
                    )
                )
                imported_days.add(day_key)

            if not to_import:
                # Even when nothing new to import, update the stored sum so
                # drift detection stays accurate as ledger values settle.
                if imported_days:
                    statistics_state[f"{series_key}_sum_at_max"] = running_sum
                _LOGGER.debug("Statistics sync no-op for %s: nothing new to import", series_key)
                continue

            # Label explicitly as Daily so Energy Dashboard shows Hourly/Daily/Monthly.
            friendly_label = series_key.replace("_", " ").title()
            metadata = StatisticMetaData(
                has_mean=False,
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"Dominion SC Daily {friendly_label}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_class=unit_class,
                unit_of_measurement=unit,
            )

            # NOTE: Despite the async-style name, these are not awaitable coroutines
            # in current HA recorder API. Do not add `await` here.
            # For external statistics, always use async_add_external_statistics.
            # The rewrite path already cleared existing rows above via
            # recorder.async_clear_statistics, so this inserts fresh data.
            async_add_external_statistics(self.hass, metadata, to_import)

            statistics_state[series_key] = sorted(imported_days)
            # Store the running_sum at the max imported day so we can detect
            # cumulative sum drift on the next run (ledger value changes that
            # would produce negative deltas if we just append).
            statistics_state[f"{series_key}_sum_at_max"] = running_sum
            _LOGGER.debug(
                "Statistics sync complete: series=%s mode=%s imported=%s tracked_days=%s sum_at_max=%.3f",
                series_key,
                "rewrite" if rewrite_for_fuel else "append",
                len(to_import),
                len(statistics_state[series_key]),
                running_sum,
            )

    async def _sync_hourly_external_statistics(self, *, force_rewrite: bool = False) -> None:
        """Import interval ledger rows as hour-aligned external statistics."""
        if not self._hourly_statistics_enabled():
            _LOGGER.debug("Hourly statistics sync skipped: disabled in options")
            return
        try:
            from homeassistant.components.recorder import get_instance  # pylint: disable=import-outside-toplevel
            from homeassistant.components.recorder.statistics import (  # pylint: disable=import-outside-toplevel
                StatisticData,
                StatisticMetaData,
                StatisticMeanType,
                async_add_external_statistics,
            )
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Recorder hourly statistics import unavailable: %s", err)
            return

        statistics_state = self._state.setdefault("hourly_statistics_import", {})
        fuel_config: list[tuple[str, str, str, str, str | None]] = [
            ("electric", TOTAL_ELECTRIC_KWH, "kWh", "interval_ledger", "energy"),
            ("gas", TOTAL_GAS_FT3, "ft³", "interval_ledger", "volume"),
            ("electric_cost", TOTAL_ELECTRIC_COST, "$", "interval_cost_ledger", None),
            ("gas_cost", TOTAL_GAS_COST, "$", "interval_cost_ledger", None),
        ]

        # Only commit completed hours — never the in-progress hour (utility local).
        local_tz = self._local_tz()
        now_local = self._utility_now()
        current_hour_start = now_local.replace(minute=0, second=0, microsecond=0)

        for series_key, total_key, unit, ledger_name, unit_class in fuel_config:
            fuel_prefix = series_key.replace("_cost", "")
            statistic_id = self.get_hourly_statistic_id(total_key)
            if not statistic_id:
                continue

            hourly_points = self._aggregate_interval_ledger_to_hours(
                self._state.get(ledger_name, {}) or {},
                fuel_prefix=fuel_prefix,
                tz=local_tz,
            )
            completed_points = [
                (hour_start, consumption)
                for hour_start, consumption in hourly_points
                if hour_start < current_hour_start and consumption > 0.0
            ]
            if not completed_points:
                _LOGGER.info(
                    "Hourly statistics sync skipped for %s: no completed hours yet "
                    "(interval_ledger keys=%s). Energy picker will not list this "
                    "series until at least one hour is imported.",
                    series_key,
                    sum(
                        1
                        for k in (self._state.get(ledger_name, {}) or {})
                        if isinstance(k, str) and k.startswith(f"{fuel_prefix}|")
                    ),
                )
                continue

            imported_raw = statistics_state.setdefault(series_key, [])
            imported_hours = set(imported_raw if isinstance(imported_raw, list) else [])
            point_hour_set = {dt.isoformat() for dt, _ in completed_points}
            rewrite_for_fuel = force_rewrite
            if not rewrite_for_fuel and imported_hours:
                if any(key not in point_hour_set for key in imported_hours):
                    rewrite_for_fuel = True
                else:
                    max_imported = max(imported_hours)
                    precompute_sum = 0.0
                    for hour_start, consumption in completed_points:
                        precompute_sum += consumption
                        if hour_start.isoformat() == max_imported:
                            break
                    stored_max = statistics_state.get(f"{series_key}_sum_at_max")
                    if stored_max is not None and abs(precompute_sum - float(stored_max)) > 0.001:
                        rewrite_for_fuel = True

            if rewrite_for_fuel:
                imported_hours.clear()
                get_instance(self.hass).async_clear_statistics([statistic_id])
                _LOGGER.info("Cleared existing hourly statistics for %s", statistic_id)

            running_sum = 0.0
            to_import: list[StatisticData] = []
            for hour_start, consumption in completed_points:
                running_sum += consumption
                hour_key = hour_start.isoformat()
                if hour_key in imported_hours:
                    continue
                to_import.append(
                    StatisticData(
                        start=hour_start,
                        state=running_sum,
                        sum=running_sum,
                    )
                )
                imported_hours.add(hour_key)

            if not to_import:
                if imported_hours:
                    statistics_state[f"{series_key}_sum_at_max"] = running_sum
                continue

            friendly_label = series_key.replace("_", " ").title()
            metadata = StatisticMetaData(
                has_mean=False,
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"Dominion SC Hourly {friendly_label}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_class=unit_class,
                unit_of_measurement=unit,
            )
            async_add_external_statistics(self.hass, metadata, to_import)
            statistics_state[series_key] = sorted(imported_hours)
            statistics_state[f"{series_key}_sum_at_max"] = running_sum
            _LOGGER.info(
                "Hourly statistics sync complete: series=%s imported=%s tracked=%s id=%s",
                series_key,
                len(to_import),
                len(statistics_state[series_key]),
                statistic_id,
            )

    async def _sync_monthly_external_statistics(self, *, force_rewrite: bool = False) -> None:
        """Import monthly ledger rows into external monthly statistics."""
        if not self._monthly_statistics_enabled():
            _LOGGER.debug("Monthly statistics sync skipped: disabled in options")
            return
        try:
            from homeassistant.components.recorder import get_instance  # pylint: disable=import-outside-toplevel
            from homeassistant.components.recorder.statistics import (  # pylint: disable=import-outside-toplevel
                StatisticData,
                StatisticMetaData,
                StatisticMeanType,
                async_add_external_statistics,
            )
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Recorder monthly statistics import unavailable: %s", err)
            return

        statistics_state = self._state.setdefault("monthly_statistics_import", {})
        fuel_config: list[tuple[str, str, str, str, str | None]] = [
            ("electric", TOTAL_ELECTRIC_KWH, "kWh", "monthly_ledger", "energy"),
            ("gas", TOTAL_GAS_FT3, "ft³", "monthly_ledger", "volume"),
            ("electric_cost", TOTAL_ELECTRIC_COST, "$", "monthly_cost_ledger", None),
            ("gas_cost", TOTAL_GAS_COST, "$", "monthly_cost_ledger", None),
        ]
        utility_today = self._utility_today()
        now_month = date(utility_today.year, utility_today.month, 1)

        for series_key, total_key, unit, ledger_name, unit_class in fuel_config:
            fuel_prefix = series_key.replace("_cost", "")
            statistic_id = self.get_monthly_statistic_id(total_key)
            if not statistic_id:
                continue

            monthly_points: list[tuple[date, float]] = []
            for key, value in (self._state.get(ledger_name, {}) or {}).items():
                if not isinstance(key, str) or not key.startswith(f"{fuel_prefix}|"):
                    continue
                month_str = key.split("|", 1)[1]
                try:
                    year_s, month_s = month_str.split("-", 1)
                    month_start = date(int(year_s), int(month_s), 1)
                except (ValueError, TypeError):
                    continue
                # Skip the in-progress month — usage/cost are still changing.
                if month_start >= now_month:
                    continue
                consumption = max(float(value or 0.0), 0.0)
                # Zeros stay null — no monthly statistic entry for 0.
                if consumption == 0.0:
                    continue
                monthly_points.append((month_start, consumption))

            if not monthly_points:
                _LOGGER.debug("Monthly statistics sync skipped for %s: no points", series_key)
                continue

            monthly_points.sort(key=lambda item: item[0])
            imported_raw = statistics_state.setdefault(series_key, [])
            imported_months = set(imported_raw if isinstance(imported_raw, list) else [])
            point_month_set = {d.isoformat() for d, _ in monthly_points}
            rewrite_for_fuel = force_rewrite
            if not rewrite_for_fuel and imported_months:
                if any(key not in point_month_set for key in imported_months):
                    rewrite_for_fuel = True
                else:
                    max_imported = max(imported_months)
                    precompute_sum = 0.0
                    for month_start, consumption in monthly_points:
                        precompute_sum += consumption
                        if month_start.isoformat() == max_imported:
                            break
                    stored_max = statistics_state.get(f"{series_key}_sum_at_max")
                    if stored_max is not None and abs(precompute_sum - float(stored_max)) > 0.001:
                        rewrite_for_fuel = True

            if rewrite_for_fuel:
                imported_months.clear()
                get_instance(self.hass).async_clear_statistics([statistic_id])
                _LOGGER.info("Cleared existing monthly statistics for %s", statistic_id)

            running_sum = 0.0
            to_import: list[StatisticData] = []
            for month_start, consumption in monthly_points:
                running_sum += consumption
                month_key = month_start.isoformat()
                if month_key in imported_months:
                    continue
                to_import.append(
                    StatisticData(
                        start=self._local_midnight(month_start),
                        state=running_sum,
                        sum=running_sum,
                    )
                )
                imported_months.add(month_key)

            if not to_import:
                if imported_months:
                    statistics_state[f"{series_key}_sum_at_max"] = running_sum
                continue

            metadata = StatisticMetaData(
                has_mean=False,
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"Dominion SC Monthly {series_key.replace('_', ' ').title()}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_class=unit_class,
                unit_of_measurement=unit,
            )
            async_add_external_statistics(self.hass, metadata, to_import)
            statistics_state[series_key] = sorted(imported_months)
            statistics_state[f"{series_key}_sum_at_max"] = running_sum
            _LOGGER.debug(
                "Monthly statistics sync complete: series=%s imported=%s tracked=%s",
                series_key,
                len(to_import),
                len(statistics_state[series_key]),
            )

    def _local_tz(self) -> ZoneInfo:
        """Dominion Bidgely wall-clock timezone (America/New_York).

        Always Eastern — not Home Assistant's configured timezone — so hour and
        calendar-day keys match the utility portal even when the HA host is UTC
        or another zone.
        """
        return _DEFAULT_UTILITY_TZ

    def _utility_now(self) -> datetime:
        """Current time in the Dominion utility timezone."""
        return datetime.now(self._local_tz())

    def _utility_today(self) -> date:
        """Current calendar day in the Dominion utility timezone."""
        return self._utility_now().date()

    def _local_midnight(self, day: date) -> datetime:
        """Start of calendar day in utility local time (Energy day alignment)."""
        return datetime.combine(day, time.min, tzinfo=self._local_tz())

    @classmethod
    def _parse_interval_end_datetime(
        cls,
        interval_end: str | int | float,
        *,
        tz: ZoneInfo | None = None,
    ) -> datetime | None:
        """Parse Bidgely intervalEndDate as utility-local wall clock."""
        local_tz = tz or _DEFAULT_UTILITY_TZ
        if isinstance(interval_end, (int, float)):
            ts = float(interval_end)
            if ts > 10**11:
                ts /= 1000.0
            # Bidgely epoch timestamps for daily/monthly buckets are often UTC midnight
            # (e.g. 2026-11-21 00:00:00Z). Converting straight to America/New_York
            # shifts them to 19:00/20:00 the previous day.
            dt_utc = datetime.fromtimestamp(ts, tz=UTC)
            if dt_utc.hour == 0 and dt_utc.minute == 0 and dt_utc.second == 0:
                return datetime.combine(dt_utc.date(), time.min, tzinfo=local_tz)
            return dt_utc.astimezone(local_tz)

        text = str(interval_end).strip()
        if not text or text.lower() == "none":
            return None
        # Epoch seconds / millis encoded as a numeric string.
        if text.lstrip("-").isdigit():
            ts = int(text)
            if abs(ts) > 10**11:
                ts //= 1000
            dt_utc = datetime.fromtimestamp(ts, tz=UTC)
            if dt_utc.hour == 0 and dt_utc.minute == 0 and dt_utc.second == 0:
                return datetime.combine(dt_utc.date(), time.min, tzinfo=local_tz)
            return dt_utc.astimezone(local_tz)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                return parsed.replace(tzinfo=local_tz)
            except ValueError:
                continue
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=local_tz)
        return parsed.astimezone(local_tz)

    @classmethod
    def _day_key_from_bidgely_interval(
        cls,
        *,
        interval_start: Any = None,
        interval_end: Any = None,
        tz: ZoneInfo | None = None,
    ) -> str | None:
        """Map Bidgely interval bounds to the portal calendar day.

        Bidgely daily buckets commonly use an exclusive midnight ``intervalEnd``
        (e.g. usage for Nov 15 ends at ``2026-11-16 00:00:00``). Using the end
        date token alone stores the next day — one day ahead of the portal.
        Prefer ``intervalStart`` when present; otherwise treat exact midnight
        ends as belonging to the previous calendar day.
        """
        local_tz = tz or _DEFAULT_UTILITY_TZ
        if interval_start not in (None, ""):
            parsed_start = cls._parse_interval_end_datetime(str(interval_start), tz=local_tz)
            if parsed_start is not None:
                return parsed_start.date().isoformat()

        if interval_end in (None, ""):
            return None
        parsed_end = cls._parse_interval_end_datetime(str(interval_end), tz=local_tz)
        if parsed_end is None:
            text = str(interval_end).strip()
            return text.split(" ")[0] if text else None

        if (
            parsed_end.hour == 0
            and parsed_end.minute == 0
            and parsed_end.second == 0
            and parsed_end.microsecond == 0
        ):
            return (parsed_end.date() - timedelta(days=1)).isoformat()
        return parsed_end.date().isoformat()

    def _migrate_day_ahead_and_utc_stats(self) -> None:
        """Shift historical daily ledger keys back one day and force stats rewrite."""
        if self._state.get("day_key_midnight_fix_applied"):
            return

        shifted = 0
        for ledger_name in ("daily_ledger", "daily_cost_ledger"):
            old_ledger = self._state.get(ledger_name, {}) or {}
            if not isinstance(old_ledger, dict) or not old_ledger:
                continue
            new_ledger: dict[str, Any] = {}
            for key, value in old_ledger.items():
                if not isinstance(key, str) or "|" not in key:
                    new_ledger[key] = value
                    continue
                fuel, day_str = key.split("|", 1)
                try:
                    day = date.fromisoformat(day_str) - timedelta(days=1)
                except ValueError:
                    new_ledger[key] = value
                    continue
                new_key = f"{fuel}|{day.isoformat()}"
                # Prefer the shifted key; if collision, keep the larger value.
                if new_key in new_ledger:
                    new_ledger[new_key] = max(float(new_ledger[new_key] or 0.0), float(value or 0.0))
                else:
                    new_ledger[new_key] = value
                shifted += 1
            self._state[ledger_name] = new_ledger

        # Reset import tracking so local-midnight timestamps replace UTC points.
        for stats_key in (
            "statistics_import",
            "hourly_statistics_import",
            "monthly_statistics_import",
        ):
            self._state[stats_key] = {}

        self._state["day_key_midnight_fix_applied"] = True
        self._state["_force_stats_rewrite"] = True
        try:
            self._recompute_totals_from_ledgers()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Could not recompute totals after day-key migration", exc_info=True)
        _LOGGER.info(
            "Applied day-ahead ledger fix: shifted=%s daily keys; stats rewrite queued",
            shifted,
        )

    @classmethod
    def _aggregate_interval_ledger_to_hours(
        cls,
        ledger: dict[str, Any],
        *,
        fuel_prefix: str,
        tz: ZoneInfo | None = None,
    ) -> list[tuple[datetime, float]]:
        """Bucket sub-hourly interval rows into hour-start cumulative-ready points."""
        local_tz = tz or _DEFAULT_UTILITY_TZ
        buckets: dict[datetime, float] = {}
        for key, value in ledger.items():
            if not isinstance(key, str) or not key.startswith(f"{fuel_prefix}|"):
                continue
            interval_end = key.split("|", 1)[1]
            parsed = cls._parse_interval_end_datetime(interval_end, tz=local_tz)
            if parsed is None:
                continue
            # Interval end belongs to the preceding hour bucket when exact hour.
            # Example: 01:00:00 closes the 00:00 hour; 00:15 closes the 00:00 hour.
            if parsed.minute == 0 and parsed.second == 0 and parsed.microsecond == 0:
                hour_start = (parsed - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            else:
                hour_start = parsed.replace(minute=0, second=0, microsecond=0)
            buckets[hour_start] = buckets.get(hour_start, 0.0) + max(float(value or 0.0), 0.0)

        return sorted(buckets.items(), key=lambda item: item[0])

    async def _ensure_authenticated(self) -> None:
        username = self.config_entry.data.get(CONF_USERNAME)
        password = self.config_entry.data.get(CONF_PASSWORD)
        stored_tfa_token = self.config_entry.data.get(CONF_TFA_TOKEN)
        if not username or not password:
            raise UpdateFailed("Missing username/password in config entry")

        _LOGGER.debug(
            "Authenticating entry=%s with stored_tfa_token=%s",
            self.config_entry.entry_id,
            bool(stored_tfa_token),
        )

        if stored_tfa_token:
            self._client.tfa_token = str(stored_tfa_token)

        try:
            await self.hass.async_add_executor_job(self._client.login, username, password)
        except Exception as err:  # pylint: disable=broad-except
            raise UpdateFailed(f"Authentication failed: {err}") from err

        refreshed_token = self._client.tfa_token
        if refreshed_token and refreshed_token != stored_tfa_token:
            new_data = dict(self.config_entry.data)
            new_data[CONF_TFA_TOKEN] = refreshed_token
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            _LOGGER.info("Updated stored tfa token for entry=%s", self.config_entry.entry_id)

    async def _process_intervals(self, now_dt: datetime) -> None:
        """Fetch Bidgely mode=day intervals and persist usage/cost ledgers.

        Interval rows are recorded for hourly external statistics only. They do
        not increment ``totals`` — those come from the daily ledger so Energy
        Dashboard cumulative sensors are not double-counted.
        """
        lookback_hours = int(
            self.config_entry.options.get(
                CONF_INTERVAL_LOOKBACK_HOURS,
                self.config_entry.data.get(
                    CONF_INTERVAL_LOOKBACK_HOURS, DEFAULT_INTERVAL_LOOKBACK_HOURS
                ),
            )
        )
        start = now_dt - timedelta(hours=max(1, lookback_hours))
        await self._store_interval_window(start, now_dt, electric_only=False)

    async def _maybe_hourly_electric_catchup(self) -> None:
        """Commit completed-hour electricity into hourly + progressive daily series.

        Runs once per completed hour (poll may be every 15m; watermark prevents
        repeats). Monthly series are intentionally not updated here.
        """
        if not self._hourly_statistics_enabled() and not self._daily_statistics_enabled():
            return
        if not self._state.get("initial_backfill_done"):
            return

        local_tz = self._local_tz()
        now = self._utility_now()
        current_hour_start = now.replace(minute=0, second=0, microsecond=0)
        last_completed = current_hour_start - timedelta(hours=1)
        catchup = self._state.setdefault(
            "electric_catchup",
            {"last_run": None, "watermark_date": None, "last_hour_watermark": None},
        )
        if catchup.get("last_hour_watermark") == last_completed.isoformat():
            return

        lookback_hours = int(
            self.config_entry.options.get(
                CONF_INTERVAL_LOOKBACK_HOURS,
                self.config_entry.data.get(
                    CONF_INTERVAL_LOOKBACK_HOURS, DEFAULT_INTERVAL_LOOKBACK_HOURS
                ),
            )
        )
        window_start = last_completed - timedelta(hours=max(1, lookback_hours - 1))
        try:
            await self._store_interval_window(
                window_start,
                now,
                electric_only=True,
                overwrite=True,
            )
            self._rollup_today_daily_from_completed_hours()
            # Exclusive midnight end (D+1 00:00) closes D's 23:00 hour. Once that
            # hour completes, refresh D from mode=month so the daily series gets
            # the full portal day — not a short interval lookback overwrite.
            if last_completed.hour == 23:
                completed_day = last_completed.date()
                await self._catchup_electric_daily(completed_day, completed_day)
            if self._hourly_statistics_enabled():
                await self._sync_hourly_external_statistics(force_rewrite=False)
            if self._daily_statistics_enabled():
                await self._sync_external_statistics(force_rewrite=False)
            catchup["last_hour_watermark"] = last_completed.isoformat()
            _LOGGER.info(
                "Hourly electricity catch-up committed hour ending %s",
                current_hour_start.isoformat(),
            )
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Hourly electricity catch-up failed: %s", err)

    async def _maybe_monthly_electric_catchup(self) -> None:
        """When the calendar month rolls, import the completed prior month once."""
        if not self._monthly_statistics_enabled():
            return
        if not self._state.get("initial_backfill_done"):
            return

        now = self._utility_today()
        current_month = date(now.year, now.month, 1)
        catchup = self._state.setdefault(
            "electric_catchup",
            {"last_run": None, "watermark_date": None, "last_month_watermark": None},
        )
        last_month_raw = catchup.get("last_month_watermark")
        try:
            last_month = date.fromisoformat(str(last_month_raw)) if last_month_raw else None
        except ValueError:
            last_month = None

        # First run: stamp current month without forcing a sync (prior months
        # already come from monthly reconcile / initial backfill).
        if last_month is None:
            catchup["last_month_watermark"] = current_month.isoformat()
            return
        if last_month >= current_month:
            return

        try:
            await self._monthly_reconcile()
            await self._sync_monthly_external_statistics(force_rewrite=False)
            catchup["last_month_watermark"] = current_month.isoformat()
            _LOGGER.info(
                "Monthly electricity catch-up imported completed month before %s",
                current_month.isoformat(),
            )
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Monthly electricity catch-up failed: %s", err)

    def _rollup_today_daily_from_completed_hours(self) -> None:
        """Set today's daily electric ledger from sum of completed hours so far."""
        local_tz = self._local_tz()
        now_local = self._utility_now()
        today = now_local.date()
        current_hour_start = now_local.replace(minute=0, second=0, microsecond=0)

        usage_hours = self._aggregate_interval_ledger_to_hours(
            self._state.get("interval_ledger", {}) or {},
            fuel_prefix="electric",
            tz=local_tz,
        )
        cost_hours = self._aggregate_interval_ledger_to_hours(
            self._state.get("interval_cost_ledger", {}) or {},
            fuel_prefix="electric",
            tz=local_tz,
        )
        usage_sum = sum(
            value
            for hour_start, value in usage_hours
            if hour_start.date() == today and hour_start < current_hour_start
        )
        cost_sum = sum(
            value
            for hour_start, value in cost_hours
            if hour_start.date() == today and hour_start < current_hour_start
        )
        day_key = today.isoformat()
        self._upsert_daily(f"electric|{day_key}", usage_sum, True, TOTAL_ELECTRIC_KWH)
        self._upsert_daily_cost(f"electric|{day_key}", cost_sum, True, TOTAL_ELECTRIC_COST)

    async def _maybe_daily_electric_catchup(self) -> None:
        """Run incremental electricity catch-up at most once per local calendar day."""
        enabled = self._option_bool(
            CONF_DAILY_ELECTRIC_CATCHUP, DEFAULT_DAILY_ELECTRIC_CATCHUP
        )
        if not enabled:
            return

        # Prefer finishing the one-time historical backfill before catch-up loops.
        if not self._state.get("initial_backfill_done"):
            self.hass.async_create_task(self.async_maybe_run_initial_backfill())
            return

        catchup = self._state.setdefault(
            "electric_catchup",
            {"last_run": None, "watermark_date": None},
        )
        today = self._utility_today()
        last_run_raw = catchup.get("last_run")
        if last_run_raw:
            try:
                last_run_day = datetime.fromisoformat(str(last_run_raw)).date()
            except ValueError:
                last_run_day = None
            if last_run_day == today:
                return

        try:
            await self.async_sync_electricity_catchup(full_since_install=False)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Daily electricity catch-up failed: %s", err)

    async def _catchup_electric_daily(self, start: date, end: date) -> None:
        """Upsert electric + gas daily usage/cost for [start, end].

        Merges Bidgely ``mode=month`` rows with Dominion Fusion portal daily
        usage so the open month/cycle is not stuck on a single recent day when
        Bidgely returns a sparse chart payload. Gas uses the same lookback
        rules as ``_daily_reconcile`` (zero gas+cost in-window = placeholder).
        """
        rows = self._merge_daily_rows_by_date(
            await self._fetch_daily_rows(start, end, gap_fill=True),
            await self._fetch_portal_daily_rows(start, end),
        )
        lookback_cutoff = self._daily_lookback_cutoff(end)
        applied_electric = 0
        applied_gas = 0
        for row in rows:
            if self._is_future_placeholder(row, end):
                continue
            if self._should_skip_daily_row(row, end, is_backfill=False):
                continue
            day_key = str(row["date"])
            try:
                row_date = date.fromisoformat(day_key)
            except (ValueError, TypeError):
                continue
            if row_date < start or row_date > end:
                continue

            e_kwh = float(row["electric_usage_kwh"])
            e_cost = float(row["electric_cost"])
            g_ft3 = float(row["gas_usage_ccf"]) * 100.0
            g_cost = float(row["gas_cost"])
            in_lookback = row_date >= lookback_cutoff

            if not (in_lookback and e_kwh == 0.0):
                self._upsert_daily(f"electric|{day_key}", e_kwh, True, TOTAL_ELECTRIC_KWH)
                applied_electric += 1
            if not (in_lookback and e_cost == 0.0):
                self._upsert_daily_cost(f"electric|{day_key}", e_cost, True, TOTAL_ELECTRIC_COST)

            # Gas: skip zero consumption+cost inside lookback (lag placeholder).
            # Outside lookback, store whatever the portal/Bidgely returned.
            if in_lookback and g_ft3 == 0.0 and g_cost == 0.0:
                continue
            self._upsert_daily(f"gas|{day_key}", g_ft3, True, TOTAL_GAS_FT3)
            self._upsert_daily_cost(f"gas|{day_key}", g_cost, True, TOTAL_GAS_COST)
            if g_ft3:
                applied_gas += 1

        _LOGGER.info(
            "Daily catch-up applied electric=%s gas=%s range=%s..%s",
            applied_electric,
            applied_gas,
            start.isoformat(),
            end.isoformat(),
        )

    async def _catchup_electric_intervals(self, start: date, end: date) -> None:
        """Fetch a capped hourly window ending at ``end`` (electric only)."""
        hourly_days = min(
            DEFAULT_CATCHUP_HOURLY_DAYS,
            max(1, (end - start).days + 1),
        )
        window_start_date = max(start, end - timedelta(days=hourly_days - 1))
        utility_tz = self._local_tz()
        start_dt = datetime.combine(window_start_date, time.min, tzinfo=utility_tz)
        end_dt = datetime.combine(end, time.max, tzinfo=utility_tz)
        await self._store_interval_window(
            start_dt, end_dt, electric_only=True, overwrite=True
        )

    async def _fetch_interval_payload(
        self,
        measurement_type: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        """Fetch Bidgely mode=day rows covering ``start``..``end``.

        Portal curls use ``start=0`` with an explicit ``end``; try that first
        and keep only rows inside the requested calendar range. Multi-day
        windows often return empty for Dominion SC, so any day not covered by
        the portal-shaped response is then fetched one calendar day at a time.

        A failed request only loses its own rows; the error is re-raised only
        when every request failed, so one bad day cannot discard a whole month.
        """
        utility_tz = self._local_tz()
        start_local = start.astimezone(utility_tz) if start.tzinfo else start.replace(tzinfo=utility_tz)
        end_local = end.astimezone(utility_tz) if end.tzinfo else end.replace(tzinfo=utility_tz)
        first = start_local.date()
        last = end_local.date()
        today = self._utility_today()
        requests = 0
        last_error: Exception | None = None

        requests += 1
        try:
            portal_payload = await self.hass.async_add_executor_job(
                lambda: self._client.get_hourly_usage(
                    measurement_type=measurement_type,
                    day_start=0,
                    day_end=end_local,
                    locale="en_US",
                    skip_ongoing_cycle=False,
                )
            )
        except Exception as err:  # pylint: disable=broad-except
            last_error = err
            portal_payload = None
            _LOGGER.warning(
                "mode=day %s start=0 end=%s failed: %s", measurement_type, end_local.isoformat(), err
            )
        failures = 1 if last_error is not None else 0
        merged = self._interval_rows_in_date_range(portal_payload, first, last)
        rows_per_day: dict[str, int] = {}
        for row in merged:
            day_key = self._interval_row_day_key(row)
            if day_key is not None:
                rows_per_day[day_key] = rows_per_day.get(day_key, 0) + 1
        # A midnight-ending row maps to the prior day; only skip per-day fetches
        # for days with (at least) a full DST-short day of hourly rows.
        covered_days = {day_key for day_key, count in rows_per_day.items() if count >= 23}
        _LOGGER.debug(
            "mode=day %s start=0 end=%s rows_in_range=%s covered_days=%s",
            measurement_type,
            end_local.isoformat(),
            len(merged),
            len(covered_days),
        )

        day = first
        while day <= last:
            if day.isoformat() in covered_days:
                day += timedelta(days=1)
                continue
            day_start = datetime.combine(day, time.min, tzinfo=utility_tz)
            day_end = datetime.combine(day, time(23, 59, 59), tzinfo=utility_tz)
            # Today is the ongoing cycle — include completed hours so far.
            skip_ongoing = False
            requests += 1
            try:
                payload = await self.hass.async_add_executor_job(
                    lambda d0=day_start, d1=day_end, skip=skip_ongoing: self._client.get_hourly_usage(
                        measurement_type=measurement_type,
                        day_start=d0,
                        day_end=d1,
                        locale="en_US",
                        skip_ongoing_cycle=skip,
                    )
                )
            except Exception as err:  # pylint: disable=broad-except
                failures += 1
                last_error = err
                _LOGGER.warning(
                    "mode=day %s day=%s failed: %s", measurement_type, day.isoformat(), err
                )
                day += timedelta(days=1)
                continue
            rows = self._interval_rows_in_date_range(payload, first, last)
            _LOGGER.debug(
                "mode=day %s day=%s rows=%s",
                measurement_type,
                day.isoformat(),
                len(rows),
            )
            merged.extend(rows)
            day += timedelta(days=1)

        _LOGGER.info(
            "mode=day %s range=%s..%s rows=%s (start=0 covered %s days)",
            measurement_type,
            first.isoformat(),
            last.isoformat(),
            len(merged),
            len(covered_days),
        )
        if last_error is not None and failures == requests:
            raise last_error

        # Dedupe by interval end so overlapping day windows cannot inflate usage.
        deduped: dict[str, dict[str, Any]] = {}
        for row in merged:
            if not isinstance(row, dict):
                continue
            key = row.get("intervalEndDate") or row.get("intervalEnd")
            if key in (None, ""):
                continue
            deduped[str(key)] = row
        return {"payload": {"usageChartDataList": list(deduped.values())}}

    async def _store_interval_window(
        self,
        start: datetime,
        end: datetime,
        *,
        electric_only: bool,
        overwrite: bool = False,
    ) -> int:
        """Persist Bidgely mode=day interval rows into interval ledgers.

        Returns the number of fetched rows with positive electric usage
        (whether or not they were already in the ledger).
        """
        electric_payload = await self._fetch_interval_payload("ELECTRIC", start, end)
        if electric_only:
            rows = self._merge_usage_rows(
                electric_rows=self._parse_usage_rows(electric_payload, fuel="electric"),
                gas_rows=[],
            )
        else:
            # Electric-only accounts (or a gas outage) must not discard electric rows.
            try:
                gas_payload = await self._fetch_interval_payload("GAS", start, end)
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning(
                    "Gas interval fetch failed for %s..%s; storing electric only: %s",
                    start.isoformat(),
                    end.isoformat(),
                    err,
                )
                gas_payload = {}
                # Skip the gas ledger writes so overwrite cannot pop stored gas keys.
                electric_only = True
            rows = self._merge_usage_rows(
                electric_rows=self._parse_usage_rows(electric_payload, fuel="electric"),
                gas_rows=self._parse_usage_rows(gas_payload, fuel="gas"),
            )

        self._state["stats"]["raw_interval_rows"] += len(rows)
        stored = 0
        positive_electric = 0
        for row in rows:
            interval_end = str(row["interval_end"])
            e_key = f"electric|{interval_end}"
            g_key = f"gas|{interval_end}"

            if float(row["electric_usage_kwh"]) > 0.0:
                positive_electric += 1
            if self._store_interval_value(
                self._state["interval_ledger"],
                e_key,
                float(row["electric_usage_kwh"]),
                overwrite,
                count_accepted=True,
            ):
                stored += 1
            self._store_interval_value(
                self._state["interval_cost_ledger"],
                e_key,
                float(row["electric_cost"]),
                overwrite,
            )

            if electric_only:
                continue

            self._store_interval_value(
                self._state["interval_ledger"],
                g_key,
                float(row["gas_usage_ccf"]) * 100.0,
                overwrite,
            )
            self._store_interval_value(
                self._state["interval_cost_ledger"],
                g_key,
                float(row["gas_cost"]),
                overwrite,
            )

        _LOGGER.info(
            "Interval window stored: rows=%s positive_electric=%s newly_stored=%s overwrite=%s range=%s..%s",
            len(rows),
            positive_electric,
            stored,
            overwrite,
            start.isoformat(),
            end.isoformat(),
        )
        return positive_electric

    def _ensure_installed_at(self) -> None:
        """Persist the first-seen integration timestamp once."""
        if self._state.get("installed_at"):
            return
        self._state["installed_at"] = datetime.now().astimezone().isoformat()

    def _installed_at_date(self) -> date:
        """Return the calendar date the integration was first set up."""
        self._ensure_installed_at()
        raw = self._state.get("installed_at")
        try:
            return datetime.fromisoformat(str(raw)).date()
        except (TypeError, ValueError):
            return datetime.now().date().replace(day=1)

    @staticmethod
    def _is_future_placeholder(row: dict[str, float | str], today: date) -> bool:
        """Return True if the row is a zero-value placeholder for a future date."""
        try:
            row_date = date.fromisoformat(str(row["date"]))
        except (ValueError, KeyError):
            return False
        if row_date <= today:
            return False
        # Future date: skip only if all usage/cost values are zero
        usage_sum = (
            abs(float(row.get("electric_usage_kwh", 0)))
            + abs(float(row.get("gas_usage_ccf", 0)))
            + abs(float(row.get("electric_cost", 0)))
            + abs(float(row.get("gas_cost", 0)))
        )
        return usage_sum == 0.0

    def _should_skip_daily_row(
        self,
        row: dict[str, float | str],
        today: date,
        *,
        is_backfill: bool = False,
    ) -> bool:
        """Return True if a daily row should be skipped (not upserted into ledger).

        Filtering rules:
        - Today and future dates: always skip (data is never available yet).
        - Recent dates within ``daily_lookback_days`` of today: skip the whole
          row only when ALL fuels/costs are null/zero (Dominion lag placeholder).
          Rows with any real value are kept; per-fuel zero filtering happens at
          the upsert call sites in ``_daily_reconcile`` / catch-up.
        - Finalized backfill rows: skip only when ALL values are zero (API gap);
          trust zero gas / zero cost on otherwise populated days.

        Args:
            row: Merged daily usage row with electric_usage_kwh, gas_usage_ccf,
                 electric_cost, gas_cost, and date fields.
            today: Current utility-local date.
            is_backfill: True if this row comes from a completed billing cycle
                         (finalized data).

        Returns:
            True if the row should be skipped entirely.
        """
        try:
            row_date = date.fromisoformat(str(row["date"]))
        except (ValueError, KeyError):
            return False

        # Always skip today and future dates — data is never ready yet
        if row_date >= today:
            _LOGGER.debug(
                "Skipping row for today/future date: %s", row["date"],
            )
            return True

        e_kwh = float(row.get("electric_usage_kwh", 0) or 0)
        g_ccf = float(row.get("gas_usage_ccf", 0) or 0)
        e_cost = float(row.get("electric_cost", 0) or 0)
        g_cost = float(row.get("gas_cost", 0) or 0)

        all_zero = (e_kwh == 0.0 and g_ccf == 0.0 and e_cost == 0.0 and g_cost == 0.0)

        if not is_backfill:
            # Recent data within lookback window: skip only if everything is
            # null/zero — this means Dominion hasn't delivered data yet.
            # If at least one fuel has data, keep the row (the other fuel
            # may legitimately be zero).
            lookback_cutoff = self._daily_lookback_cutoff(today)
            if row_date >= lookback_cutoff and all_zero:
                _LOGGER.debug(
                    "Skipping recent all-zero row (data may arrive later): %s",
                    row["date"],
                )
                return True
            # NOTE: Per-fuel zero filtering (e.g. skip zero electric but keep
            # gas) is handled at the individual _upsert_daily call sites in
            # _daily_reconcile and catch-up, not here.
        else:
            # Backfill / finalized data: trust zero for gas and cost,
            # but skip if ALL values are zero (likely a data gap in API)
            if all_zero:
                _LOGGER.debug(
                    "Skipping finalized all-zero row (likely data gap): %s",
                    row["date"],
                )
                return True

        return False

    def _daily_lookback_days(self) -> int:
        """Configured lag-tolerance window (at least 1 day)."""
        return max(
            1,
            int(
                self.config_entry.options.get(
                    CONF_DAILY_LOOKBACK_DAYS,
                    self.config_entry.data.get(
                        CONF_DAILY_LOOKBACK_DAYS, DEFAULT_DAILY_LOOKBACK_DAYS
                    ),
                )
            ),
        )

    def _daily_lookback_cutoff(self, today: date) -> date:
        """Oldest date still treated as late-arriving for zero placeholders."""
        return today - timedelta(days=self._daily_lookback_days())

    async def _daily_reconcile(self, now_date: date) -> None:
        """Reconcile daily ledgers for the open billing cycle.

        Fetches the full current billing cycle (not just the lookback window).
        ``daily_lookback_days`` only controls which recent zeros are treated as
        late-arriving placeholders and skipped until real values appear.
        """
        lookback = self._daily_lookback_days()
        cycle = self.current_billing_cycle
        # Clamp end to today — billEndDate can sit in the future.
        start = cycle.start
        end = min(cycle.end, now_date)
        if start > end:
            start = end
        _LOGGER.debug(
            "Daily reconcile using billing cycle: start=%s end=%s "
            "(lookback placeholder window=%s days, cutoff=%s)",
            start.isoformat(),
            end.isoformat(),
            lookback,
            self._daily_lookback_cutoff(now_date).isoformat(),
        )
        rows = await self._fetch_daily_rows(start, end)

        lookback_cutoff = self._daily_lookback_cutoff(now_date)

        for row in rows:
            if self._is_future_placeholder(row, now_date):
                _LOGGER.debug("Skipping future zero-value placeholder row: %s", row.get("date"))
                continue
            if self._should_skip_daily_row(row, now_date, is_backfill=False):
                continue
            day_key = row["date"]

            # --- Per-fuel zero filtering within the lookback window ---
            # Dominion lags ~2 days; zeros within the window are placeholders.
            # Filter per-fuel so real data for one fuel isn't blocked by
            # the other fuel's placeholder zero.
            try:
                row_date = date.fromisoformat(str(day_key))
            except (ValueError, TypeError):
                row_date = None
            in_lookback = row_date is not None and row_date >= lookback_cutoff

            e_kwh = float(row["electric_usage_kwh"])
            g_ft3 = float(row["gas_usage_ccf"]) * 100.0
            e_cost = float(row["electric_cost"])
            g_cost = float(row["gas_cost"])

            # Electric consumption: skip zero within lookback (lag placeholder)
            if in_lookback and e_kwh == 0.0:
                _LOGGER.debug(
                    "Daily reconcile: skipping zero electric consumption for %s (lookback placeholder)",
                    day_key,
                )
            else:
                self._upsert_daily(f"electric|{day_key}", e_kwh, True, TOTAL_ELECTRIC_KWH)

            # Electric cost: skip zero within lookback (lag placeholder)
            if in_lookback and e_cost == 0.0:
                _LOGGER.debug(
                    "Daily reconcile: skipping zero electric cost for %s (lookback placeholder)",
                    day_key,
                )
            else:
                self._upsert_daily_cost(f"electric|{day_key}", e_cost, True, TOTAL_ELECTRIC_COST)

            # Gas: skip only when BOTH usage and cost are zero inside lookback
            # (zero gas alone can be legitimate; both-zero is usually a placeholder).
            if in_lookback and g_ft3 == 0.0 and g_cost == 0.0:
                _LOGGER.debug(
                    "Daily reconcile: skipping zero gas consumption+cost for %s (lookback placeholder)",
                    day_key,
                )
            else:
                self._upsert_daily(f"gas|{day_key}", g_ft3, True, TOTAL_GAS_FT3)
                self._upsert_daily_cost(f"gas|{day_key}", g_cost, True, TOTAL_GAS_COST)

    async def _monthly_reconcile(self) -> None:
        """Fetch Bidgely mode=year monthly usage/cost and upsert monthly ledgers.

        Monthly buckets are recorded separately from daily totals so Energy
        Dashboard cumulative sensors stay sourced from the daily ledger only.
        """
        rows = await self._fetch_monthly_rows()
        if not rows:
            _LOGGER.debug("Monthly reconcile: no rows returned")
            return

        for row in rows:
            month_key = str(row.get("month") or "")
            if not month_key:
                continue
            e_kwh = float(row["electric_usage_kwh"])
            g_ft3 = float(row["gas_usage_ccf"]) * 100.0
            e_cost = float(row["electric_cost"])
            g_cost = float(row["gas_cost"])
            self._upsert_monthly(f"electric|{month_key}", e_kwh, overwrite=True)
            self._upsert_monthly(f"gas|{month_key}", g_ft3, overwrite=True)
            self._upsert_monthly_cost(f"electric|{month_key}", e_cost, overwrite=True)
            self._upsert_monthly_cost(f"gas|{month_key}", g_cost, overwrite=True)

        _LOGGER.debug("Monthly reconcile stored %s month rows", len(rows))

    async def _fetch_monthly_rows(self) -> list[dict[str, float | str]]:
        """Fetch monthly usage/cost for electric and gas via Bidgely mode=year."""
        electric_payload = await self.hass.async_add_executor_job(
            lambda: self._client.get_monthly_usage(
                measurement_type="ELECTRIC",
                start=0,
                include_ongoing=True,
                locale="en_US",
            )
        )
        gas_payload = await self.hass.async_add_executor_job(
            lambda: self._client.get_monthly_usage(
                measurement_type="GAS",
                start=0,
                include_ongoing=True,
                locale="en_US",
            )
        )
        return self._merge_monthly_rows(
            electric_rows=self._parse_monthly_rows(electric_payload, fuel="electric"),
            gas_rows=self._parse_monthly_rows(gas_payload, fuel="gas"),
        )

    @classmethod
    def _parse_monthly_rows(cls, payload: Any, fuel: str) -> list[dict[str, float | str]]:
        rows: list[dict[str, float | str]] = []
        chart_rows: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            chart_rows = (payload.get("payload") or {}).get("usageChartDataList") or []

        for row in chart_rows:
            interval_end = row.get("intervalEndDate") or row.get("intervalEnd")
            month_key = cls._month_key_from_interval_end(str(interval_end) if interval_end else "")
            if not month_key:
                continue
            consumption = max(float(row.get("consumption") or 0.0), 0.0)
            cost = max(float(row.get("cost") or 0.0), 0.0)
            rows.append(
                {
                    "month": month_key,
                    "electric_usage_kwh": consumption if fuel == "electric" else 0.0,
                    "gas_usage_ccf": consumption if fuel == "gas" else 0.0,
                    "electric_cost": cost if fuel == "electric" else 0.0,
                    "gas_cost": cost if fuel == "gas" else 0.0,
                }
            )
        return rows

    @classmethod
    def _month_key_from_interval_end(cls, interval_end: str) -> str | None:
        """Derive YYYY-MM from a Bidgely monthly interval end value."""
        parsed = cls._parse_interval_end_datetime(interval_end)
        if parsed is not None:
            day = parsed.date()
            # Exclusive midnight ends belong to the previous calendar day/month.
            if (
                parsed.hour == 0
                and parsed.minute == 0
                and parsed.second == 0
                and parsed.microsecond == 0
            ):
                day = day - timedelta(days=1)
            return f"{day.year:04d}-{day.month:02d}"
        text = str(interval_end).strip()
        if len(text) >= 7 and text[4] == "-":
            candidate = text[:7]
            try:
                year_s, month_s = candidate.split("-", 1)
                month_i = int(month_s)
                if 1 <= month_i <= 12:
                    return f"{int(year_s):04d}-{month_i:02d}"
            except ValueError:
                return None
        return None

    @staticmethod
    def _merge_monthly_rows(
        electric_rows: list[dict[str, float | str]],
        gas_rows: list[dict[str, float | str]],
    ) -> list[dict[str, float | str]]:
        merged: dict[str, dict[str, float | str]] = {}

        def _upsert(row: dict[str, float | str]) -> None:
            key = str(row["month"])
            if key not in merged:
                merged[key] = {
                    "month": key,
                    "electric_usage_kwh": 0.0,
                    "gas_usage_ccf": 0.0,
                    "electric_cost": 0.0,
                    "gas_cost": 0.0,
                }
            merged_row = merged[key]
            merged_row["electric_usage_kwh"] = float(merged_row["electric_usage_kwh"]) + float(
                row["electric_usage_kwh"]
            )
            merged_row["gas_usage_ccf"] = float(merged_row["gas_usage_ccf"]) + float(row["gas_usage_ccf"])
            merged_row["electric_cost"] = float(merged_row["electric_cost"]) + float(row["electric_cost"])
            merged_row["gas_cost"] = float(merged_row["gas_cost"]) + float(row["gas_cost"])

        for row in electric_rows:
            _upsert(row)
        for row in gas_rows:
            _upsert(row)
        return [merged[key] for key in sorted(merged.keys())]

    async def _backfill_hourly_and_monthly_for_cycle(
        self,
        cycle: BillingCycle,
        *,
        overwrite: bool,
    ) -> None:
        """Fetch Bidgely intervals + monthly buckets covering one billing cycle."""
        await self._backfill_hourly_for_cycle(cycle, overwrite=overwrite)
        try:
            await self._monthly_reconcile()
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning(
                "Backfill monthly reconcile failed for cycle %s: %s",
                cycle.key,
                err,
                exc_info=True,
            )

    async def _backfill_hourly_for_cycle(
        self,
        cycle: BillingCycle,
        *,
        overwrite: bool,
    ) -> int:
        """Fill interval_ledger for one cycle and track hourly completion.

        A cycle is hourly-complete only while interval_ledger holds positive
        electric intervals inside it. Empty fetches count toward
        ``_HOURLY_BACKFILL_MAX_EMPTY_ATTEMPTS`` but never complete the cycle:
        exhausted cycles stay pending (reported as ``hourly_exhausted_cycles``)
        because Bidgely interval retention is shorter than daily retention.
        Fetch errors are not counted as empty attempts.
        """
        utility_tz = self._local_tz()
        start_dt = datetime.combine(cycle.start, time.min, tzinfo=utility_tz)
        end_dt = datetime.combine(cycle.end, time.max, tzinfo=utility_tz)
        positive = 0
        failed = False
        try:
            positive = await self._store_interval_window(
                start_dt,
                end_dt,
                electric_only=False,
                overwrite=overwrite,
            )
        except Exception as err:  # pylint: disable=broad-except
            failed = True
            _LOGGER.warning(
                "Backfill hourly intervals failed for cycle %s: %s",
                cycle.key,
                err,
                exc_info=True,
            )

        backfill = self._state["backfill"]
        hourly_completed = backfill.setdefault("hourly_completed_cycles", [])
        attempts = backfill.setdefault("hourly_empty_attempts", {})
        if self._cycle_has_hourly_data(cycle):
            attempts.pop(cycle.key, None)
            if cycle.key not in hourly_completed:
                hourly_completed.append(cycle.key)
            _LOGGER.info(
                "Backfill hourly cycle %s complete: positive_electric_intervals=%s",
                cycle.key,
                positive,
            )
            return positive

        if cycle.key in hourly_completed:
            hourly_completed.remove(cycle.key)
        if failed:
            return 0
        attempt_n = int(attempts.get(cycle.key, 0)) + 1
        attempts[cycle.key] = attempt_n
        _LOGGER.warning(
            "Backfill hourly cycle %s returned no electric intervals (attempt %s/%s)%s",
            cycle.key,
            attempt_n,
            _HOURLY_BACKFILL_MAX_EMPTY_ATTEMPTS,
            "; exhausted, still pending (Run Backfill All keeps retrying)"
            if attempt_n >= _HOURLY_BACKFILL_MAX_EMPTY_ATTEMPTS
            else "",
        )
        return 0

    @classmethod
    def _interval_ledger_day(cls, interval_end: str) -> date | None:
        """Portal day for an interval_ledger ``YYYY-MM-DD HH:MM:SS`` key suffix.

        A midnight end closes the previous day (same rule as
        ``_day_key_from_bidgely_interval``).
        """
        try:
            day = date.fromisoformat(interval_end[:10])
        except ValueError:
            day_key = cls._day_key_from_bidgely_interval(interval_end=interval_end)
            try:
                return date.fromisoformat(day_key) if day_key else None
            except ValueError:
                return None
        if interval_end[11:19] in ("00:00:00", "00:00"):
            return day - timedelta(days=1)
        return day

    def _electric_interval_days(self) -> set[date]:
        """Days with at least one positive electric interval in interval_ledger."""
        days: set[date] = set()
        for key, value in (self._state.get("interval_ledger", {}) or {}).items():
            if not isinstance(key, str) or not key.startswith("electric|"):
                continue
            try:
                if float(value or 0.0) <= 0.0:
                    continue
            except (TypeError, ValueError):
                continue
            day = self._interval_ledger_day(key.split("|", 1)[1])
            if day is not None:
                days.add(day)
        return days

    def _cycle_has_hourly_data(
        self, cycle: BillingCycle, days: set[date] | None = None
    ) -> bool:
        """True when interval_ledger has any positive electric row in the cycle."""
        if days is None:
            days = self._electric_interval_days()
        return any(cycle.start <= day <= cycle.end for day in days)

    @staticmethod
    def _cycle_from_key(key: str) -> BillingCycle | None:
        try:
            start_s, end_s = key.split("|")
            return BillingCycle(start=date.fromisoformat(start_s), end=date.fromisoformat(end_s))
        except ValueError:
            return None

    def _reconcile_hourly_completion_flags(self, days: set[date] | None = None) -> None:
        """Drop hourly_completed flags that have no interval ledger coverage."""
        backfill = self._state.setdefault("backfill", {})
        hourly_completed = list(backfill.get("hourly_completed_cycles", []) or [])
        if not hourly_completed:
            return
        if days is None:
            days = self._electric_interval_days()
        kept = [
            key
            for key in hourly_completed
            if (cycle := self._cycle_from_key(key)) is not None
            and self._cycle_has_hourly_data(cycle, days)
        ]
        if kept != hourly_completed:
            backfill["hourly_completed_cycles"] = kept
            _LOGGER.info(
                "Reconciled hourly_completed_cycles: %s -> %s (dropped flags without ledger data)",
                len(hourly_completed),
                len(kept),
            )

    def _hourly_exhausted(self, cycle_key: str) -> bool:
        attempts = (self._state.get("backfill", {}) or {}).get("hourly_empty_attempts") or {}
        return int(attempts.get(cycle_key, 0)) >= _HOURLY_BACKFILL_MAX_EMPTY_ATTEMPTS

    def _pending_hourly_cycles(self, *, include_exhausted: bool = True) -> list[BillingCycle]:
        """Daily-complete cycles that still lack hourly interval ledger data.

        Uses the interval ledger as source of truth (not only a sticky flag), so
        Remaining stays > 0 until hours are actually stored. Newest first.
        ``include_exhausted=False`` drops cycles that already hit the empty-attempt
        cap (used by scheduled polls so they do not refetch a month every poll).
        """
        days = self._electric_interval_days()
        self._reconcile_hourly_completion_flags(days)
        backfill = self._state.get("backfill", {}) or {}
        completed = set(backfill.get("completed_cycles", []) or [])
        pending: list[BillingCycle] = []
        for key in sorted(completed, reverse=True):
            cycle = self._cycle_from_key(key)
            if cycle is None or self._cycle_has_hourly_data(cycle, days):
                continue
            if not include_exhausted and self._hourly_exhausted(key):
                continue
            pending.append(cycle)
        return pending

    async def _process_hourly_only_backfill(
        self, overwrite: bool, *, include_exhausted: bool = True
    ) -> bool:
        """Backfill intervals for one pending cycle; return False if none pending.

        Picks the pending cycle with the fewest empty attempts (newest first on
        ties) so one unfillable cycle cannot block every later press.
        """
        pending = self._pending_hourly_cycles(include_exhausted=include_exhausted)
        if not pending:
            return False
        attempts = self._state["backfill"].get("hourly_empty_attempts") or {}
        cycle = min(pending, key=lambda c: int(attempts.get(c.key, 0)))
        _LOGGER.info(
            "Backfill: daily cycles complete; filling hourly intervals for %s (%s pending)",
            cycle.key,
            len(pending),
        )
        await self._backfill_hourly_for_cycle(cycle, overwrite=overwrite)
        return True

    async def _process_backfill(
        self,
        overwrite: bool,
        cycle_key: str | None = None,
        allow_initialize_missing: bool = True,
        retry_exhausted_hourly: bool = True,
    ) -> None:
        """Process one backfill cycle: daily + hourly intervals + monthly.

        allow_initialize_missing controls whether the missing_cycles list may be
        lazily repopulated when empty. Scheduled automatic updates should allow
        initialization; manual service/button invocations will pass
        allow_initialize_missing=False so they can early-exit when there are no
        incomplete cycles to process.
        retry_exhausted_hourly=False skips hourly-pending cycles that already hit
        the empty-attempt cap (scheduled polls); manual runs keep retrying them.
        """
        if allow_initialize_missing:
            self._initialize_backfill_cycles()
        else:
            backfill_state = self._state.get("backfill", {})
            missing = backfill_state.get("missing_cycles", []) if isinstance(backfill_state, dict) else []
            if not missing and not cycle_key:
                if await self._process_hourly_only_backfill(
                    overwrite, include_exhausted=retry_exhausted_hourly
                ):
                    return
                _LOGGER.warning(
                    "Manual backfill requested but no incomplete backfill cycles to process for entry=%s",
                    self.config_entry.entry_id,
                )
                return

        cycle = self._pick_cycle_for_backfill(overwrite=overwrite, cycle_key=cycle_key)
        if cycle is None:
            if not await self._process_hourly_only_backfill(
                overwrite, include_exhausted=retry_exhausted_hourly
            ):
                _LOGGER.debug("Backfill skipped: no eligible cycle (overwrite=%s)", overwrite)
            return

        _LOGGER.debug("Backfill cycle selected: %s (overwrite=%s)", cycle.key, overwrite)

        # Intervals + monthly are independent of the daily fetch; run them first
        # so an empty daily response cannot skip hourly backfill.
        await self._backfill_hourly_and_monthly_for_cycle(cycle, overwrite=overwrite)

        rows = await self._fetch_daily_rows(cycle.start, cycle.end)
        in_range_rows = self._filter_rows_to_date_range(rows, cycle.start, cycle.end)
        if not in_range_rows:
            # Do not permanently complete empty cycles — Bidgely often returns the
            # wrong billing window when skip_ongoing is forced. Retry later.
            attempts = self._state.setdefault("backfill_empty_attempts", {})
            attempt_n = int(attempts.get(cycle.key, 0)) + 1
            attempts[cycle.key] = attempt_n
            _LOGGER.warning(
                "Backfill cycle %s returned no in-range rows (raw=%s attempt=%s/%s)",
                cycle.key,
                len(rows),
                attempt_n,
                5,
            )
            if attempt_n >= 5:
                backfill = self._state["backfill"]
                if cycle.key in backfill["missing_cycles"]:
                    backfill["missing_cycles"].remove(cycle.key)
                if cycle.key not in backfill["completed_cycles"]:
                    backfill["completed_cycles"].append(cycle.key)
                    backfill["cycles_completed"] += 1
                _LOGGER.warning(
                    "Backfill cycle %s marked complete after %s empty attempts",
                    cycle.key,
                    attempt_n,
                )
            return

        today = self._utility_today()
        _LOGGER.debug(
            "Backfill cycle %s processing %s in-range daily rows (raw=%s)",
            cycle.key,
            len(in_range_rows),
            len(rows),
        )

        for row in in_range_rows:
            if self._is_future_placeholder(row, today):
                _LOGGER.debug("Backfill skipping future zero-value placeholder row: %s", row.get("date"))
                continue
            if self._should_skip_daily_row(row, today, is_backfill=True):
                continue
            day_key = row["date"]

            e_kwh = float(row["electric_usage_kwh"])
            g_ft3 = float(row["gas_usage_ccf"]) * 100.0
            e_cost = float(row["electric_cost"])
            g_cost = float(row["gas_cost"])

            # --- Per-fuel zero filtering for finalized/backfill cycles ---
            # For finalized data, zero electric consumption is suspect
            # (true zero essentially impossible — HA itself draws power).
            # Trust zero for gas (legitimate in summer) and all cost series.
            if e_kwh == 0.0:
                _LOGGER.debug(
                    "Backfill: skipping zero electric consumption for %s "
                    "(finalized cycle — suspect placeholder)",
                    day_key,
                )
            else:
                self._upsert_daily(f"electric|{day_key}", e_kwh, overwrite, TOTAL_ELECTRIC_KWH)

            # Electric cost: skip if electric consumption was zero (same placeholder)
            if e_kwh == 0.0 and e_cost == 0.0:
                _LOGGER.debug(
                    "Backfill: skipping zero electric cost for %s (consumption also zero)",
                    day_key,
                )
            else:
                self._upsert_daily_cost(f"electric|{day_key}", e_cost, overwrite, TOTAL_ELECTRIC_COST)

            # Gas: trust zero consumption and cost (legitimate in summer)
            self._upsert_daily(f"gas|{day_key}", g_ft3, overwrite, TOTAL_GAS_FT3)
            self._upsert_daily_cost(f"gas|{day_key}", g_cost, overwrite, TOTAL_GAS_COST)

        backfill = self._state["backfill"]
        if cycle.key in backfill["missing_cycles"]:
            backfill["missing_cycles"].remove(cycle.key)
        if cycle.key not in backfill["completed_cycles"]:
            backfill["completed_cycles"].append(cycle.key)
            backfill["cycles_completed"] += 1
        # Clear empty-attempt counter on success
        self._state.setdefault("backfill_empty_attempts", {}).pop(cycle.key, None)
        _LOGGER.info(
            "Backfill cycle complete: %s daily+hourly+monthly cycles_completed=%s missing_remaining=%s",
            cycle.key,
            backfill["cycles_completed"],
            len(backfill["missing_cycles"]),
        )

    async def _fetch_daily_rows(
        self,
        start: date,
        end: date,
        *,
        gap_fill: bool = False,
    ) -> list[dict[str, float | str]]:
        """Fetch Bidgely mode=month daily rows for an explicit date range.

        Always prefer ``current_cycle=True`` (skip_ongoing_cycle=False) so the
        Bidgely ``start``/``end`` window is honored. Forcing
        ``current_cycle=False`` on historical calendar months often returns the
        prior *billing* cycle (or empty), which does not match Oct 1–31 style
        backfill windows and caused months to be marked complete with no data.

        If the preferred fetch is empty, retry once with ``current_cycle=False``.

        When ``gap_fill=True`` (Sync Current Cycle / electric catch-up only),
        missing calendar days are requested one day at a time. Left off for
        scheduled poll reconcile so a sparse response cannot spam Bidgely.
        """
        utility_tz = self._local_tz()
        start_dt = datetime.combine(start, time.min, tzinfo=utility_tz)
        end_dt = datetime.combine(end, time.max, tzinfo=utility_tz)

        rows = await self._fetch_daily_rows_with_flag(
            start_dt, end_dt, current_cycle=True
        )
        if not rows and end < self._utility_today():
            _LOGGER.debug(
                "Daily fetch empty for %s..%s with current_cycle=True; retrying current_cycle=False",
                start.isoformat(),
                end.isoformat(),
            )
            rows = await self._fetch_daily_rows_with_flag(
                start_dt, end_dt, current_cycle=False
            )

        if not gap_fill:
            return self._filter_rows_to_date_range(rows, start, end)

        covered: set[date] = set()
        for row in rows:
            try:
                covered.add(date.fromisoformat(str(row["date"])))
            except (ValueError, TypeError, KeyError):
                continue

        today = self._utility_today()
        expected_days = max(0, (min(end, today - timedelta(days=1)) - start).days + 1)
        if expected_days > 1 and len(covered) < expected_days:
            _LOGGER.info(
                "Daily fetch sparse for %s..%s: covered=%s expected~%s; gap-filling per day",
                start.isoformat(),
                end.isoformat(),
                len(covered),
                expected_days,
            )
            day = start
            while day <= end:
                if day >= today:
                    break
                if day in covered:
                    day += timedelta(days=1)
                    continue
                day_start = datetime.combine(day, time.min, tzinfo=utility_tz)
                day_end = datetime.combine(day, time.max, tzinfo=utility_tz)
                day_rows = await self._fetch_daily_rows_with_flag(
                    day_start, day_end, current_cycle=True
                )
                if not day_rows:
                    day_rows = await self._fetch_daily_rows_with_flag(
                        day_start, day_end, current_cycle=False
                    )
                for row in day_rows:
                    try:
                        row_day = date.fromisoformat(str(row["date"]))
                    except (ValueError, TypeError, KeyError):
                        continue
                    if row_day != day:
                        continue
                    rows.append(row)
                    covered.add(day)
                day += timedelta(days=1)

        return self._filter_rows_to_date_range(rows, start, end)

    async def _fetch_portal_daily_rows(
        self, start: date, end: date
    ) -> list[dict[str, float | str]]:
        """Fetch Dominion Fusion current-period daily rows for ``[start, end]``.

        This is the portal chart behind Detailed Energy Usage — the natural
        source for the open billing cycle / current calendar month. Bidgely
        ``mode=month`` alone often only returns the latest finalized day.
        """
        rows: list[dict[str, float | str]] = []

        async def _load(start_date: date | None, end_date: date | None) -> list[dict[str, Any]]:
            payload = await self.hass.async_add_executor_job(
                lambda: self._client.get_current_daily_usage(
                    usage_type="BOTH",
                    start_date=start_date,
                    end_date=end_date,
                )
            )
            if isinstance(payload, dict):
                self._state["current_daily_usage"] = payload
                self._state["current_bill_summary"] = payload.get("bill_summary", {})
                return list(payload.get("daily_records") or [])
            return []

        try:
            records = await _load(start, end)
            if not records:
                # Portal default (1900-01-01) returns the open billing period.
                records = await _load(None, None)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning(
                "Portal daily usage fetch failed for %s..%s: %s",
                start.isoformat(),
                end.isoformat(),
                err,
            )
            return rows

        for rec in records:
            if not isinstance(rec, dict):
                continue
            day = self._parse_portal_date_label(str(rec.get("date_label") or ""), ref=end)
            if day is None or day < start or day > end:
                continue
            e_kwh = max(float(rec.get("electric_usage") or 0.0), 0.0)
            e_cost = max(float(rec.get("electric_cost") or 0.0), 0.0)
            g_usage = max(float(rec.get("gas_usage") or 0.0), 0.0)
            g_cost = max(float(rec.get("gas_cost") or 0.0), 0.0)
            rows.append(
                {
                    "interval_end": f"{day.isoformat()} 23:59:59",
                    "date": day.isoformat(),
                    "electric_usage_kwh": e_kwh,
                    "gas_usage_ccf": g_usage,
                    "electric_cost": e_cost,
                    "gas_cost": g_cost,
                }
            )

        _LOGGER.info(
            "Portal daily usage rows=%s for %s..%s",
            len(rows),
            start.isoformat(),
            end.isoformat(),
        )
        return rows

    @staticmethod
    def _parse_portal_date_label(label: str, *, ref: date) -> date | None:
        """Parse Fusion ``xAxisLabelForTotal`` date tokens into a calendar day."""
        text = str(label or "").strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        for fmt in ("%b %d", "%B %d", "%m/%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                day = date(ref.year, parsed.month, parsed.day)
                # Dec labels near a January ref belong to the prior year.
                if day.month - ref.month > 6:
                    day = date(ref.year - 1, day.month, day.day)
                elif ref.month - day.month > 6:
                    day = date(ref.year + 1, day.month, day.day)
                return day
            except ValueError:
                continue
        return None

    @staticmethod
    def _merge_daily_rows_by_date(
        primary: list[dict[str, float | str]],
        secondary: list[dict[str, float | str]],
    ) -> list[dict[str, float | str]]:
        """Merge daily rows by calendar date; prefer non-zero electric values."""
        merged: dict[str, dict[str, float | str]] = {}

        def _upsert(row: dict[str, float | str], *, prefer_incoming: bool) -> None:
            day_key = str(row.get("date") or "")
            if not day_key:
                return
            if day_key not in merged:
                merged[day_key] = {
                    "interval_end": row.get("interval_end") or f"{day_key} 23:59:59",
                    "date": day_key,
                    "electric_usage_kwh": float(row.get("electric_usage_kwh") or 0.0),
                    "gas_usage_ccf": float(row.get("gas_usage_ccf") or 0.0),
                    "electric_cost": float(row.get("electric_cost") or 0.0),
                    "gas_cost": float(row.get("gas_cost") or 0.0),
                }
                return
            existing = merged[day_key]
            for field in (
                "electric_usage_kwh",
                "gas_usage_ccf",
                "electric_cost",
                "gas_cost",
            ):
                incoming = float(row.get(field) or 0.0)
                current = float(existing.get(field) or 0.0)
                if prefer_incoming and incoming > 0.0:
                    existing[field] = incoming
                elif current == 0.0 and incoming > 0.0:
                    existing[field] = incoming

        for row in primary:
            _upsert(row, prefer_incoming=False)
        for row in secondary:
            # Portal values win when Bidgely left a zero / missing day.
            _upsert(row, prefer_incoming=True)
        return [merged[key] for key in sorted(merged.keys())]

    async def _fetch_daily_rows_with_flag(
        self,
        start_dt: datetime,
        end_dt: datetime,
        *,
        current_cycle: bool,
    ) -> list[dict[str, float | str]]:
        electric_payload = await self.hass.async_add_executor_job(
            lambda: self._client.get_daily_usage(
                measurement_type="ELECTRIC",
                current_cycle=current_cycle,
                start=start_dt,
                end=end_dt,
                locale="en_US",
            )
        )
        gas_payload = await self.hass.async_add_executor_job(
            lambda: self._client.get_daily_usage(
                measurement_type="GAS",
                current_cycle=current_cycle,
                start=start_dt,
                end=end_dt,
                locale="en_US",
            )
        )

        try:
            e_rows = (
                (electric_payload.get("payload") or {}).get("usageChartDataList") or []
                if isinstance(electric_payload, dict)
                else []
            )
            g_rows = (
                (gas_payload.get("payload") or {}).get("usageChartDataList") or []
                if isinstance(gas_payload, dict)
                else []
            )
            _LOGGER.debug(
                "Daily fetch: start=%s end=%s current_cycle=%s electric_rows=%d gas_rows=%d",
                start_dt.date().isoformat(),
                end_dt.date().isoformat(),
                current_cycle,
                len(e_rows),
                len(g_rows),
            )
        except Exception:  # Defensive: logging must not break processing
            _LOGGER.debug(
                "Daily fetch: start=%s end=%s current_cycle=%s (could not compute row counts)",
                start_dt.date().isoformat(),
                end_dt.date().isoformat(),
                current_cycle,
            )

        return self._merge_usage_rows(
            electric_rows=self._parse_usage_rows(electric_payload, fuel="electric"),
            gas_rows=self._parse_usage_rows(gas_payload, fuel="gas"),
        )

    @staticmethod
    def _filter_rows_to_date_range(
        rows: list[dict[str, float | str]],
        start: date,
        end: date,
    ) -> list[dict[str, float | str]]:
        """Keep only rows whose parsed calendar day falls inside [start, end]."""
        filtered: list[dict[str, float | str]] = []
        for row in rows:
            try:
                row_date = date.fromisoformat(str(row["date"]))
            except (ValueError, TypeError, KeyError):
                continue
            if start <= row_date <= end:
                filtered.append(row)
        return filtered

    @staticmethod
    def _first_numeric(row: dict[str, Any], keys: tuple[str, ...]) -> float:
        """Return the first parseable non-null value among ``keys`` (clamped ≥ 0)."""
        for key in keys:
            raw = row.get(key)
            if raw in (None, ""):
                continue
            try:
                return max(float(raw), 0.0)
            except (TypeError, ValueError):
                continue
        return 0.0

    @classmethod
    def _interval_row_day_key(cls, row: dict[str, Any]) -> str | None:
        """Portal calendar day for a raw Bidgely usageChartDataList row."""
        interval_end = row.get("intervalEndDate") or row.get("intervalEnd")
        if interval_end in (None, ""):
            return None
        return cls._day_key_from_bidgely_interval(
            interval_start=row.get("intervalStartDate") or row.get("intervalStart"),
            interval_end=interval_end,
        )

    @classmethod
    def _interval_rows_in_date_range(
        cls,
        payload: Any,
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        """Raw usageChartDataList rows whose portal day falls inside [start, end]."""
        rows = (
            ((payload or {}).get("payload") or {}).get("usageChartDataList") or []
            if isinstance(payload, dict)
            else []
        )
        if not isinstance(rows, list):
            return []
        kept: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            day_key = cls._interval_row_day_key(row)
            if day_key is None:
                continue
            try:
                row_day = date.fromisoformat(day_key)
            except ValueError:
                continue
            if start <= row_day <= end:
                kept.append(row)
        return kept

    @classmethod
    def _parse_usage_rows(cls, payload: Any, fuel: str) -> list[dict[str, float | str]]:
        rows: list[dict[str, float | str]] = []
        chart_rows: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            chart_rows = (payload.get("payload") or {}).get("usageChartDataList") or []

        for row in chart_rows:
            interval_start = row.get("intervalStartDate") or row.get("intervalStart")
            interval_end = row.get("intervalEndDate") or row.get("intervalEnd")
            if interval_end in (None, ""):
                continue
            parsed_end = cls._parse_interval_end_datetime(interval_end)
            if parsed_end is None:
                continue
            # Normalize ledger keys to a parseable wall-clock string.
            interval_end_s = parsed_end.strftime("%Y-%m-%d %H:%M:%S")
            day_key = cls._day_key_from_bidgely_interval(
                interval_start=interval_start,
                interval_end=interval_end_s,
            )
            if not day_key:
                continue
            consumption = cls._first_numeric(row, _USAGE_VALUE_KEYS)
            cost = cls._first_numeric(row, _COST_VALUE_KEYS)

            rows.append(
                {
                    "interval_end": interval_end_s,
                    "date": day_key,
                    "electric_usage_kwh": consumption if fuel == "electric" else 0.0,
                    "gas_usage_ccf": consumption if fuel == "gas" else 0.0,
                    "electric_cost": cost if fuel == "electric" else 0.0,
                    "gas_cost": cost if fuel == "gas" else 0.0,
                }
            )

        return rows

    @staticmethod
    def _merge_usage_rows(
        electric_rows: list[dict[str, float | str]],
        gas_rows: list[dict[str, float | str]],
    ) -> list[dict[str, float | str]]:
        merged: dict[str, dict[str, float | str]] = {}

        def _upsert(row: dict[str, float | str]) -> None:
            key = str(row["interval_end"])
            if key not in merged:
                merged[key] = {
                    "interval_end": row["interval_end"],
                    "date": row["date"],
                    "electric_usage_kwh": 0.0,
                    "gas_usage_ccf": 0.0,
                    "electric_cost": 0.0,
                    "gas_cost": 0.0,
                }
            merged_row = merged[key]
            merged_row["electric_usage_kwh"] = float(merged_row["electric_usage_kwh"]) + float(row["electric_usage_kwh"])
            merged_row["gas_usage_ccf"] = float(merged_row["gas_usage_ccf"]) + float(row["gas_usage_ccf"])
            merged_row["electric_cost"] = float(merged_row["electric_cost"]) + float(row["electric_cost"])
            merged_row["gas_cost"] = float(merged_row["gas_cost"]) + float(row["gas_cost"])

        for row in electric_rows:
            _upsert(row)
        for row in gas_rows:
            _upsert(row)

        return [merged[key] for key in sorted(merged.keys())]

    def _initialize_backfill_cycles(self) -> None:
        backfill = self._state["backfill"]

        target = int(
            self.config_entry.options.get(
                CONF_BACKFILL_CYCLES_TARGET,
                self.config_entry.data.get(CONF_BACKFILL_CYCLES_TARGET, DEFAULT_BACKFILL_CYCLES_TARGET),
            )
        )
        # Always regenerate from current target (don't return early if cycles exist)
        # This ensures that when user updates target or config is refreshed,
        # the cycle list is regenerated and old/stale cycles are replaced
        # Same utility-calendar "today" as backfill_summary, so a UTC host cannot
        # queue the still-open month (or drop the just-closed one) at month end.
        cycles = self._build_recent_monthly_cycles(now_date=self._utility_today(), target=target)
        eligible_keys = [cycle.key for cycle in cycles]
        completed = set(backfill.get("completed_cycles", []))

        # Only add cycles that haven't already been completed
        backfill["missing_cycles"] = [k for k in eligible_keys if k not in completed]
        _LOGGER.debug(
            "Initialized backfill cycles: eligible=%d completed=%d missing=%d target=%d",
            len(eligible_keys),
            len(completed),
            len(backfill["missing_cycles"]),
            target,
        )
        if backfill["missing_cycles"]:
            _LOGGER.debug(
                "First 3 missing cycles (oldest to newest): %s",
                backfill["missing_cycles"][:3],
            )

    @staticmethod
    def _build_recent_monthly_cycles(now_date: date, target: int) -> list[BillingCycle]:
        """Build a list of recent complete calendar month cycles.
        
        Backfill targets completed past cycles only. The current month is handled
        by regular polling, so it's excluded from the backfill list.
        """
        cycles: list[BillingCycle] = []
        if target <= 0:
            return cycles

        # Start from the first day of the previous month (exclude current month)
        month_start = date(now_date.year, now_date.month, 1)
        current = month_start - timedelta(days=1)  # Last day of previous month

        while len(cycles) < target:
            cycle_start = date(current.year, current.month, 1)
            cycle_end = date(current.year, current.month, current.day)
            cycles.append(BillingCycle(start=cycle_start, end=cycle_end))
            current = cycle_start - timedelta(days=1)

        cycles.reverse()
        return cycles

    def _pick_cycle_for_backfill(self, overwrite: bool, cycle_key: str | None) -> BillingCycle | None:
        backfill = self._state["backfill"]

        if cycle_key:
            start_s, end_s = cycle_key.split("|")
            return BillingCycle(start=date.fromisoformat(start_s), end=date.fromisoformat(end_s))

        if overwrite and backfill["completed_cycles"]:
            start_s, end_s = backfill["completed_cycles"][0].split("|")
            return BillingCycle(start=date.fromisoformat(start_s), end=date.fromisoformat(end_s))

        if not backfill["missing_cycles"]:
            return None

        start_s, end_s = backfill["missing_cycles"][0].split("|")
        return BillingCycle(start=date.fromisoformat(start_s), end=date.fromisoformat(end_s))

    def _check_statistics_migration(self) -> bool:
        """Return True (force rewrite) once to migrate from sensor.* to external statistics.

        Previous versions imported statistics into sensor.* statistic_ids with
        source='recorder'. This conflicted with the HA recorder's auto-compiled
        hourly statistics, creating negative deltas after restarts. The migration
        clears old sensor.* rows and reimports into dominionsc:* external IDs.
        """
        if self._state.get("external_statistics_migration_done"):
            return False
        _LOGGER.info(
            "One-time statistics migration: switching from sensor.* to "
            "dominionsc:* external statistics (clears old conflicting rows)"
        )
        # Clear old sensor.* statistics that conflicted with recorder
        try:
            from homeassistant.components.recorder import get_instance  # pylint: disable=import-outside-toplevel
            recorder = get_instance(self.hass)
            legacy_ids = self.get_legacy_sensor_statistic_ids()
            if legacy_ids:
                recorder.async_clear_statistics(legacy_ids)
                _LOGGER.info("Migration: cleared old sensor.* statistic IDs: %s", legacy_ids)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Migration: could not clear old sensor.* stats", exc_info=True)
        # Reset imported_days tracking so all data is reimported into new IDs
        stats_state = self._state.get("statistics_import", {})
        for key in list(stats_state.keys()):
            if not key.endswith("_sum_at_max"):
                stats_state[key] = []
            else:
                del stats_state[key]
        self._state["external_statistics_migration_done"] = True
        return True

    def _check_unit_class_fix(self) -> bool:
        """Return True (force rewrite) once to fix unit_class=None in external statistics.

        Earlier versions created external statistics metadata without unit_class,
        which prevents the Energy Dashboard picker from discovering them.  Clear
        existing external statistics so they get re-created with proper unit_class
        (energy/volume/monetary) on the next sync.
        """
        if self._state.get("unit_class_fix_applied"):
            return False
        _LOGGER.info(
            "One-time unit_class fix: clearing external statistics to "
            "re-create with proper unit_class for Energy Dashboard discovery"
        )
        try:
            from homeassistant.components.recorder import get_instance  # pylint: disable=import-outside-toplevel
            recorder = get_instance(self.hass)
            ext_ids = [
                self.get_statistic_id(TOTAL_ELECTRIC_KWH),
                self.get_statistic_id(TOTAL_GAS_FT3),
                self.get_statistic_id(TOTAL_ELECTRIC_COST),
                self.get_statistic_id(TOTAL_GAS_COST),
            ]
            ext_ids = [sid for sid in ext_ids if sid]
            if ext_ids:
                recorder.async_clear_statistics(ext_ids)  # do not await
                _LOGGER.info("unit_class fix: cleared external statistic IDs: %s", ext_ids)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("unit_class fix: could not clear external stats", exc_info=True)
        # Reset imported_days so all data is reimported with correct metadata
        stats_state = self._state.get("statistics_import", {})
        for key in list(stats_state.keys()):
            if not key.endswith("_sum_at_max"):
                stats_state[key] = []
            else:
                del stats_state[key]
        self._state["unit_class_fix_applied"] = True
        return True

    def _store_interval_value(
        self,
        ledger: dict[str, Any],
        key: str,
        value: float,
        overwrite: bool,
        *,
        count_accepted: bool = False,
    ) -> bool:
        """Store a positive interval value; zeros stay null (no entry)."""
        value = max(float(value), 0.0)
        if value == 0.0:
            if overwrite:
                ledger.pop(key, None)
            return False
        if key not in ledger or overwrite:
            if count_accepted and key not in ledger:
                self._state["stats"]["accepted_interval_rows"] += 1
            ledger[key] = value
            return True
        return False

    def _upsert_daily(self, key: str, value: float, overwrite: bool, total_key: str) -> None:
        value = max(float(value), 0.0)
        ledger = self._state["daily_ledger"]
        # Zeros stay null — do not create (or keep) a 0 ledger entry.
        if value == 0.0:
            if overwrite and key in ledger:
                old = float(ledger.pop(key))
                self._state["totals"][total_key] -= old
            return
        if key not in ledger:
            ledger[key] = value
            self._state["totals"][total_key] += value
            return
        if overwrite:
            old = float(ledger[key])
            ledger[key] = value
            self._state["totals"][total_key] += value - old

    def _upsert_daily_cost(self, key: str, value: float, overwrite: bool, total_key: str) -> None:
        value = max(float(value), 0.0)
        ledger = self._state["daily_cost_ledger"]
        if value == 0.0:
            if overwrite and key in ledger:
                old = float(ledger.pop(key))
                self._state["totals"][total_key] -= old
            return
        if key not in ledger:
            ledger[key] = value
            self._state["totals"][total_key] += value
            return
        if overwrite:
            old = float(ledger[key])
            ledger[key] = value
            self._state["totals"][total_key] += value - old

    def _upsert_monthly(self, key: str, value: float, *, overwrite: bool) -> None:
        value = max(float(value), 0.0)
        ledger = self._state["monthly_ledger"]
        if value == 0.0:
            if overwrite:
                ledger.pop(key, None)
            return
        if key not in ledger or overwrite:
            ledger[key] = value

    def _upsert_monthly_cost(self, key: str, value: float, *, overwrite: bool) -> None:
        value = max(float(value), 0.0)
        ledger = self._state["monthly_cost_ledger"]
        if value == 0.0:
            if overwrite:
                ledger.pop(key, None)
            return
        if key not in ledger or overwrite:
            ledger[key] = value

    def _apply_monotonic_guard(self) -> None:
        totals = self._state["totals"]
        last = self._state["last_totals"]
        for key in (TOTAL_ELECTRIC_KWH, TOTAL_GAS_FT3, TOTAL_ELECTRIC_COST, TOTAL_GAS_COST):
            totals[key] = max(float(totals[key]), float(last.get(key, 0.0)))
            last[key] = float(totals[key])

    def _set_last_sync(self) -> None:
        """Set the last_sync timestamp in persistent state to now (ISO 8601)."""
        try:
            # Prefer timezone-aware ISO format
            self._state["last_sync"] = datetime.now().astimezone().isoformat()
        except Exception:
            # Fallback to naive ISO format
            self._state["last_sync"] = datetime.now().isoformat()

    async def _save_state(self) -> None:
        await self._store.async_save(self._state)

    def _default_state(self) -> dict[str, Any]:
        return {
            "interval_ledger": {},
            "interval_cost_ledger": {},
            "daily_ledger": {},
            "daily_cost_ledger": {},
            "monthly_ledger": {},
            "monthly_cost_ledger": {},
            "backfill": {
                "cycles_completed": 0,
                "missing_cycles": [],
                "completed_cycles": [],
            },
            "totals": {
                TOTAL_ELECTRIC_KWH: 0.0,
                TOTAL_GAS_FT3: 0.0,
                TOTAL_ELECTRIC_COST: 0.0,
                TOTAL_GAS_COST: 0.0,
            },
            "last_totals": {
                TOTAL_ELECTRIC_KWH: 0.0,
                TOTAL_GAS_FT3: 0.0,
                TOTAL_ELECTRIC_COST: 0.0,
                TOTAL_GAS_COST: 0.0,
            },
            "stats": {
                "raw_interval_rows": 0,
                "accepted_interval_rows": 0,
            },
            "statistics_import": {
                "electric": [],
                "gas": [],
            },
            "hourly_statistics_import": {},
            "monthly_statistics_import": {},
            "installed_at": None,
            "initial_backfill_done": False,
            "day_key_midnight_fix_applied": False,
            "electric_catchup": {
                "last_run": None,
                "watermark_date": None,
            },
            # ISO 8601 timestamp of the last successful sync/update
            "last_sync": None,
            "statistics_rewrite_once_done": False,
            # Latest fetched account/billing payloads (for informational sensors)
            "account_summary": {},
            "bill_projection": {},
            "current_daily_usage": {},
            "current_bill_summary": {},
        }

    def _recompute_totals_from_ledgers(self) -> None:
        """Recompute totals from persisted daily ledgers.

        Totals are sourced only from daily usage/cost ledgers. Interval and
        monthly ledgers are finer/coarser views of the same energy and must
        not be added again (that previously double-counted vs daily).
        """
        totals = {
            TOTAL_ELECTRIC_KWH: 0.0,
            TOTAL_GAS_FT3: 0.0,
            TOTAL_ELECTRIC_COST: 0.0,
            TOTAL_GAS_COST: 0.0,
        }

        for key, val in (self._state.get("daily_ledger", {}) or {}).items():
            try:
                if isinstance(key, str) and key.startswith("electric|"):
                    totals[TOTAL_ELECTRIC_KWH] += float(val or 0.0)
                elif isinstance(key, str) and key.startswith("gas|"):
                    totals[TOTAL_GAS_FT3] += float(val or 0.0)
            except Exception:
                continue

        for key, val in (self._state.get("daily_cost_ledger", {}) or {}).items():
            try:
                if isinstance(key, str) and key.startswith("electric|"):
                    totals[TOTAL_ELECTRIC_COST] += float(val or 0.0)
                elif isinstance(key, str) and key.startswith("gas|"):
                    totals[TOTAL_GAS_COST] += float(val or 0.0)
            except Exception:
                continue

        # Respect monotonicity vs any recorded last_totals
        last = self._state.get("last_totals", {}) or {}
        for k in list(totals.keys()):
            totals[k] = max(float(totals[k]), float(last.get(k, 0.0)))

        self._state["totals"] = totals
        # Also set last_totals so the monotonic guard uses this baseline
        self._state["last_totals"] = {k: float(v) for k, v in totals.items()}

    def _merge_state(self, stored: dict[str, Any]) -> dict[str, Any]:
        merged = self._default_state()
        merge_dicts = {
            "interval_ledger",
            "interval_cost_ledger",
            "daily_ledger",
            "daily_cost_ledger",
            "monthly_ledger",
            "monthly_cost_ledger",
            "totals",
            "last_totals",
            "stats",
            "backfill",
            "statistics_import",
            "hourly_statistics_import",
            "monthly_statistics_import",
            "electric_catchup",
        }
        for key, value in stored.items():
            if key in merge_dicts and isinstance(value, dict):
                merged[key].update(value)
            else:
                merged[key] = value
        merged["statistics_rewrite_once_done"] = bool(merged.get("statistics_rewrite_once_done", False))
        merged["initial_backfill_done"] = bool(merged.get("initial_backfill_done", False))
        return merged
