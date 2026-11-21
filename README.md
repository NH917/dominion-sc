# Dominion Energy SC – Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A custom Home Assistant integration for **Dominion Energy South Carolina** that provides energy consumption and cost tracking for the [Energy Dashboard](https://www.home-assistant.io/docs/energy/).

## Features

- **Electric & Gas consumption** sensors with full historical backfill
- **Electric & Gas cost** sensors
- **Energy Dashboard compatible** — external statistics for **Hourly**, **Daily**, and **Monthly** views
- **Historical statistics import** — backfilled data appears at the correct local (Eastern) dates
- **Automatic incremental polling** — interval data ingestion with deduplication
- **Lag-tolerant daily reconciliation** — handles Dominion's reporting delay
- **Billing cycle backfill** — daily + hourly + monthly per cycle; one cycle per poll, or Run Backfill All on demand
- **One-time install backfill** — drains the historical queue once after setup, then syncs the open cycle
- **Sync Current Cycle** — button/service to refresh the open billing period (daily + hourly through today)
- **2FA support** — SMS and email verification during setup
- **Persistent state** — survives restarts without data loss or double-counting
- **Busy-safe jobs** — backfill / sync / cleanup buttons are unavailable while any sync job is running
- **Null-safe zeros** — placeholder / unknown zero rows are not written into ledgers or statistics

## ⚠️ Upgrade Notice — Energy Dashboard Reconfiguration Required

> **If you are upgrading from release `2026.05.03.04` or earlier**, you must update your Energy Dashboard configuration.
>
> This version switches historical statistics from entity-based (`sensor.*`) IDs to **external statistics** (`dominionsc:*`) to fix a recurring issue where large negative values appeared on the Energy Dashboard after restarts.
>
> **What to do:**
> 1. Go to **Settings → Dashboards → Energy**
> 2. Remove the existing Dominion consumption/cost entities from your grid configuration
> 3. Re-add them by selecting the new external statistics (e.g. **"Dominion SC Daily Electric"**, **"Dominion SC Hourly Electric"**, **"Dominion SC Gas Cost"**)
> 4. These appear under the "External statistics" section in the entity picker
>
> The new external statistics are not affected by HA's recorder auto-compilation and will not produce negative deltas after restarts.

## Important updates (2026-04-03)

- Default SSL verification is now enabled. The integration will verify HTTPS certificates by default (the `verify_ssl` option defaults to `true` for new installs). If you previously disabled certificate verification, reconfigure the integration to re-enable it for that entry.
- The following settings can now be configured during the initial setup (and during reconfigure):
   - Poll interval (minutes)
   - Backfill cycles target
   - Daily lookback days

For existing Home Assistant entries, change these values via **Settings → Devices & Services → Dominion SC Energy → Reconfigure** or via the **Configure** (Options) dialog.

## Requirements

- Home Assistant **2026.4.0** or later
- A [Dominion Energy South Carolina](https://www.dominionenergy.com/south-carolina) online account
- HACS (for easy installation) or manual installation

## Home Assistant Compatibility

This integration is designed for Home Assistant **2026.4.0** and later versions. It uses modern Home Assistant patterns and APIs including:
- DataUpdateCoordinator for polling
- ConfigEntry and options flow for configuration
- Store for persistent state management
- SensorEntityDescription for sensor metadata
- Long-term / external statistics integration with recorder

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant.
2. Go to **Integrations** → **⋮ (menu)** → **Custom repositories**.
3. Add this repository URL and select **Integration** as the category:
   ```
   https://github.com/nh917/dominion-sc
   ```
4. Click **Add**, then find **Dominion SC Energy** in the HACS store and click **Download**.
5. Restart Home Assistant.

### Manual

1. Copy the `custom_components/dominionsc` folder into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## Setup

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **Dominion SC Energy**.
3. Enter your Dominion Energy credentials:
   - **Username**
   - **Password**
4. If your account requires two-factor authentication:
   - Select your preferred 2FA method (SMS or email).
   - Enter the verification code when received.
5. Configure options:
   - **Poll interval** (minutes) — how often to check for new data (default: `15`)
   - **Backfill cycles target** — how many previous billing cycles to backfill (default: `6`)
   - **Daily lookback days** — lag-tolerant reconciliation window (default: `5`)

After the first successful refresh, the integration runs a **one-time historical backfill** (all incomplete cycles in the target window), then **Sync Current Cycle** for the open billing period.

## Entities

After setup, the integration creates a **Dominion SC Energy** device with the following entities:

### Energy Sensors (live state)

| Entity | Unit | State Class | Description |
|--------|------|-------------|-------------|
| Electric Cumulative Consumption | kWh | `total_increasing` | Running total of electric usage |
| Gas Cumulative Consumption | ft³ | `total_increasing` | Running total of gas usage |
| Electric Cumulative Cost | $ | `total` | Running total of electric cost |
| Gas Cumulative Cost | $ | `total` | Running total of gas cost |

Use these for live cards/automations. For the Energy Dashboard, prefer the **external statistics** below.

### External statistics (Energy Dashboard)

Published as `dominionsc:*` and shown in the Energy picker (when the matching option is enabled):

| Name pattern | Granularity | Typical use |
|--------------|-------------|-------------|
| Dominion SC Hourly … | Hour | Energy Dashboard **Hour** view |
| Dominion SC Daily … | Day | Energy Dashboard **Day** view |
| Dominion SC Monthly … | Month | Energy Dashboard **Month** view |

Series cover electric/gas consumption and cost. Day boundaries use **America/New_York** (Dominion Bidgely wall clock), not the HA host timezone.

### Diagnostic Sensors

| Entity | State | Attributes | Description |
|--------|-------|------------|-------------|
| Backfill Cycles | Configured target | `completed_count`, `incomplete_count`, `pending_hourly_count`, `remaining_count`, cycle lists (`YYYY-MMM`) | Overview of backfill progress |
| Backfill Cycles Remaining | Integer | `daily_incomplete_count`, `pending_hourly_count`, `pending_hourly_cycles`, `remaining_cycles`, `hourly_exhausted_*` | Work left: unfinished **daily** cycles **plus** daily-complete cycles still missing **hourly** data |
| Current Billing Cycle | `MMM D - MMM D` (e.g. `Mar 9 - Apr 4`) | `start`, `end` (ISO dates) | Open billing period from bill projection / current usage |

**Remaining** only drops when a cycle has daily data **and** real hourly interval coverage in the ledger. Empty Bidgely hourly responses do not mark a cycle done.

### Buttons

| Entity | Icon | Description |
|--------|------|-------------|
| Run Backfill All | `mdi:playlist-play` | Drains **all** incomplete cycles up to the configured backfill-cycles target (through current), then refreshes Energy statistics. |
| Sync Current Cycle | `mdi:flash-clock` | Refreshes electric + gas for the **open billing cycle** (daily rows + hourly intervals through today). Does not re-run deep historical backfill. |
| Cleanup External Statistics | `mdi:database-refresh` | Clears legacy/`dominionsc:*` external statistics and force-rewrites historical series from ledgers. |

All three buttons are **unavailable** while any of these jobs (or the scheduled/initial backfill) is already running, so they cannot overlap or re-enter themselves.

### Account & Billing Informational Sensors

These additional informational sensors expose account and billing details (useful in dashboards or automations). They are created on the same `Dominion SC Energy` device and are diagnostic/monetary where appropriate.

| Entity | Unit / Type | Description |
|--------|------------:|-------------|
| Last Payment | $ | Most recent payment amount (USD) |
| Account Balance | $ | Current account balance; textual values like `Bill Paid` are reported as `$0.00` |
| Current Cost | $ | Current accumulated cost for billing period (from projection API) |
| Projected Price | $ | Projected total price for the current billing period |
| Days Left | integer | Days remaining in the billing cycle (from projection API) |
| Electric Charges | $ | Electric charge subtotal for the current billing period (daily bill summary) |
| Gas Charges | $ | Gas charge subtotal for the current billing period (daily bill summary) |
| Electric Other Charges | $ | Other electric charges (fees, taxes) in the current bill summary |
| Gas Other Charges | $ | Other gas charges in the current bill summary |
| Total Charges | $ | Combined total bill amount (from daily bill summary) |

These sensors read Dominion's `account_summary`, `bill_projection`, and `current_daily_usage` payloads and perform lightweight parsing (money strings like `$333.19`, and dates such as `Mar 9, 2026`). The integration treats textual `Bill Paid` values as `$0.00` to keep monetary sensors numeric for dashboards and automations.

## Energy Dashboard Configuration

1. Go to **Settings → Dashboards → Energy**.
2. Under **Electricity** / **Gas**, add the Dominion **external statistics** (Hourly / Daily / Monthly as needed), for example:
   - Grid consumption: **Dominion SC Daily Electric** or **Dominion SC Hourly Electric**
   - Cost: matching **Dominion SC … Cost** series
3. Ensure the matching options are enabled (**Enable hourly / daily / monthly statistics**) under Configure.

> **Note:** After initial setup or a fresh hourly import, it may take a few minutes for new series to appear in the Energy picker. Use **Sync Current Cycle** or **Run Backfill All** if Hourly is missing after install.

## Services

### `dominionsc.run_backfill_all`

Process **all** incomplete billing cycles (daily + hourly + monthly) and refresh Energy external statistics.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `config_entry_id` | string | _(all entries)_ | Target a specific config entry |
| `overwrite` | boolean | `false` | If `true`, re-imports all incomplete cycles and replaces prior stored values |

### `dominionsc.sync_electricity_catchup`

**Sync current cycle** — fill electricity usage/cost for the open billing cycle (daily rows and hourly intervals through today). Does not re-run deep historical backfill.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `config_entry_id` | string | _(all entries)_ | Target a specific config entry |

### `dominionsc.rewrite_statistics`

Clears legacy external statistics (`dominionsc:*` IDs) and force-rewrites historical series.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `config_entry_id` | string | _(all entries)_ | Target a specific config entry |

## How It Works

### Data Flow

```
Dominion / Bidgely API
    │
    ├─ Interval (mode=day) ─────→ Interval ledger → Hourly external stats
    │
    ├─ Daily usage (mode=month) → Daily ledger → Daily external stats
    │
    ├─ Monthly (mode=year) ─────→ Monthly buckets → Monthly external stats
    │
    └─ Billing cycle backfill ──→ Daily + hourly + monthly for each cycle
                                         │
                                         ▼
                                  Synthetic cumulative totals
                                  (monotonic, never decrease)
                                         │
                                    ┌────┴────┐
                                    ▼         ▼
                              HA Sensors   Recorder / Energy
                              (live state)  (external LTS)
```

### Authentication

- On first setup, the config flow handles login and optional 2FA.
- Credentials are stored in the config entry.
- The coordinator re-authenticates automatically on each poll using stored credentials.
- Auth state stays in Home Assistant `ConfigEntry` / `Store` persistence — no cookie files on disk.
- If a refreshed 2FA token is returned, it is persisted back to the config entry.

### Backfill Strategy

- **Install:** one-time drain of all incomplete cycles in the target window, then Sync Current Cycle.
- **Each scheduled poll:** at most **one** missing billing cycle (daily + hourly + monthly).
- Cycles are processed oldest-first within the configured **Backfill cycles target**.
- **Run Backfill All** drains every incomplete cycle in one press; it is disabled while busy.
- **Backfill Cycles Remaining** counts daily-incomplete cycles **and** cycles that still need hourly interval data.
- With `overwrite: true`, existing cycle data is replaced (useful for correcting stale data).
- Empty hourly API responses leave the cycle pending (Remaining stays > 0) instead of marking it complete.

### Catch-up / current cycle

- **Sync Current Cycle** (button / `sync_electricity_catchup`) uses the open billing window from bill projection / current usage, clamped to today.
- Optional **Daily electricity catch-up** (Configure) runs once per local day from the catch-up watermark (lookback window) without deep historical backfill.
- Scheduled hourly/monthly catch-up commits completed hours and finished months into Energy series when enabled.

### Statistics Import

- Historical data is imported into Home Assistant long-term / external statistics at Eastern local midnights (and hour ends for interval data).
- The integration uses `async_import_statistics` / `async_add_external_statistics` (not awaitable despite the `async_` prefix) and `recorder.async_clear_statistics` for cleanup rewrites.
- Future-dated placeholder rows (zero-value days beyond today) are filtered out.
- If newly backfilled days are older than previously imported days, the integration triggers a full rewrite for that series to keep cumulative sums continuous.

Additional import/filtering behavior

- Daily reconcile fetches the **full open billing cycle**. The configured **Daily lookback days** only marks recent zeros as late-arriving placeholders (skipped until real values arrive); it does not shrink the fetch window.
- For finalized/backfill cycles, zero electric consumption for a full day is treated as suspicious and skipped; zero gas or zero cost on finalized days is trusted as valid.
- Filtering runs before ledger upsert and before building `StatisticData`, so nulls parsed as `0.0` do not create premature zero-value statistics.

### Units & Conversion

- **Electric**: reported in `kWh` (no conversion needed).
- **Gas**: Dominion reports in `CCF`; converted to `ft³` (`CCF × 100`) for Home Assistant's gas device class.
- Monetary sensors use `state_class=total` (not `total_increasing`).

## Debugging

Enable debug logging by adding this to `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.dominionsc: debug
```

Then restart Home Assistant. Debug output covers:
- Authentication state and token refresh
- Backfill cycle selection, remaining/pending hourly, and row counts
- Daily reconciliation window
- Current-cycle sync ranges
- Statistics sync (import counts, rewrite triggers, future-date filtering)
- Future placeholder row skipping

### Fusion daily-usage probe

To check whether Dominion's Fusion API provides historical daily usage for an
account, run the probe launcher from the repository root:

- Windows PowerShell: `.\diagnostics\probe_fusion_daily_usage.ps1`
- Linux/macOS: `bash diagnostics/probe_fusion_daily_usage.sh`

The launcher prompts for Dominion credentials, creates a dedicated
`.venv-fusion-probe` environment, and installs the probe's dependencies on
first run. It then requests about two years of daily usage by default and
prints only response dates, fuel flags, and record counts. It does not write
credentials or raw usage values to disk. The account may request a 2FA code
during login.

Share only the printed summary. Do not share credentials, `.env`, cookies, or
verification codes. Optional `--start-date YYYY-MM-DD` and `--end-date
YYYY-MM-DD` arguments can be passed through the launcher to test a different
period.

## Options (Reconfigurable)

After setup, you can adjust options via **Settings → Devices & Services → Dominion SC Energy → Configure**:

- **Poll interval** (minutes)
- **Backfill cycles target** — raising this queues newly eligible incomplete cycles
- **Daily lookback days** — lag-tolerance window for zero placeholders (default `5`); reconcile still covers the open billing cycle
- **Enable hourly statistics** — publish Dominion SC Hourly series for Energy
- **Enable daily statistics** — publish Dominion SC Daily series for Energy
- **Enable monthly statistics** — publish Dominion SC Monthly series for Energy
- **Daily electricity catch-up** — once per day, refresh from the catch-up watermark

Changes take effect on the next poll cycle (backfill target changes recalculate the missing queue immediately).

## Troubleshooting

### "No statistics available" in Energy Dashboard
- Wait 5–10 minutes after initial setup for the recorder to compile statistics.
- Verify the sensor state is not `unavailable` or `unknown`.
- Confirm hourly/daily/monthly options are enabled, then press **Sync Current Cycle** or **Run Backfill All**.

### Hourly missing from the Energy picker
- Enable **Enable hourly statistics**.
- Press **Sync Current Cycle** (open period) or **Run Backfill All** until **Backfill Cycles Remaining** reaches `0`.
- Check attributes on Remaining for `pending_hourly_cycles`.

### Remaining stays at 0 while hourly still looks incomplete
- Reload the integration after updating. Remaining includes pending hourly work; empty API responses no longer mark hourly complete.

### "Verification code was not accepted"
- Ensure you entered the code without leading/trailing spaces.
- If the code was valid, this may indicate a post-auth session issue. Check HA logs for the actual error.

### Sensors show data only from today
- Run **Cleanup External Statistics** to force-rewrite historical data.
- Verify backfill cycles have completed (Backfill Cycles / Remaining diagnostics).

### Negative values in statistics
- This can occur if backfill cycles were imported out of order. Run **Cleanup External Statistics** to rebuild the cumulative series.

### Day bars appear one day ahead
- Statistics use Eastern local midnight. After upgrading, reload once so any day-key migration can run; use Cleanup External Statistics if old UTC-aligned points remain.

## License

[MIT](LICENSE)
