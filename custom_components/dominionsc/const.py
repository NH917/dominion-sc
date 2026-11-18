"""Constants for Dominion SC Energy integration."""
DOMAIN = "dominionsc"

PLATFORMS = ["sensor", "button"]

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_VERIFY_SSL = "verify_ssl"
CONF_POLL_MINUTES = "poll_minutes"
CONF_BACKFILL_CYCLES_TARGET = "backfill_cycles_target"
CONF_DAILY_LOOKBACK_DAYS = "daily_lookback_days"
CONF_INTERVAL_LOOKBACK_HOURS = "interval_lookback_hours"
CONF_DAILY_ELECTRIC_CATCHUP = "daily_electric_catchup"
CONF_TFA_TOKEN = "tfa_token"

DEFAULT_VERIFY_SSL = True
DEFAULT_POLL_MINUTES = 15
DEFAULT_BACKFILL_CYCLES_TARGET = 6
DEFAULT_DAILY_LOOKBACK_DAYS = 5
# How far back each poll reaches for Bidgely mode=day (interval/hourly) rows.
DEFAULT_INTERVAL_LOOKBACK_HOURS = 48
DEFAULT_DAILY_ELECTRIC_CATCHUP = True
# Cap hourly API catch-up window so button/daily runs do not hammer Bidgely.
DEFAULT_CATCHUP_HOURLY_DAYS = 7

SERVICE_RUN_BACKFILL = "run_backfill"
SERVICE_RUN_BACKFILL_ALL = "run_backfill_all"
SERVICE_REWRITE_STATISTICS = "rewrite_statistics"
SERVICE_SYNC_ELECTRICITY_CATCHUP = "sync_electricity_catchup"

STORE_VERSION = 1
STORE_KEY_PREFIX = f"{DOMAIN}_state_"

COORDINATOR = "coordinator"

TOTAL_ELECTRIC_KWH = "electric_kwh_total"
TOTAL_GAS_FT3 = "gas_ft3_total"
TOTAL_ELECTRIC_COST = "electric_cost_total"
TOTAL_GAS_COST = "gas_cost_total"
