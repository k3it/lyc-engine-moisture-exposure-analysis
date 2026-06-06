"""Constants for the Engine Moisture Monitor integration.

The physics + pipeline live in the repo's scripts/ (the single source of truth); this
integration only hosts them in Home Assistant. OPTION_DEFAULTS below mirrors the
GUI-exposed subset of scripts/pipeline.DEFAULTS so the config/options forms are
self-contained (the flow runs before scripts/ is on sys.path). pipeline.DEFAULTS is the
runtime backstop for anything not surfaced here.
"""
from __future__ import annotations

DOMAIN = "engine_moisture"
PLATFORMS = ["sensor", "binary_sensor"]

# ---- initial config (identity of this deployment) ----
CONF_TEMP_ENTITY = "temp_entity"
CONF_HUMIDITY_ENTITY = "humidity_entity"
CONF_TEMP_UNIT = "temp_unit"
CONF_AIRPORT = "airport_icao"
CONF_LAT = "latitude"
CONF_LON = "longitude"
CONF_TZ = "timezone"
CONF_SCRIPTS_DIR = "scripts_dir"

# ---- service ----
SERVICE_RUN_NOW = "run_now"
ATTR_FORCE = "force"

# ---- GUI-adjustable options (keys match scripts/pipeline cfg) ----
# Quiet hours are split into two numeric fields for the GUI and reassembled into the
# [start, end] list the pipeline expects.
CONF_QUIET_START = "quiet_start"
CONF_QUIET_END = "quiet_end"

OPTION_DEFAULTS = {
    # schedule
    "run_every_minutes": 60,
    # model parameters
    "tau_metal_h": 8.0,
    "tau_bulk_h": 24.0,
    "tau_event_h": 1.5,
    "dry_factor": 0.3,
    "flight_temp_c": 40.0,
    "flight_rise_c": 8.0,
    "flight_rise_window_min": 10,
    "flight_run_floor_c": 32.0,
    "flight_peak_window_min": 60,
    "flight_debounce_h": 6.0,
    # trailing window / spin-up
    "window_days": 14,
    "max_window_days": 60,
    "spinup_days": 5,
    # alert thresholds (since last flight) — ANY fires
    "close_call_margin_c": 2.0,
    "alert_wet_hours": 2.0,
    "alert_close_call_hours": 20.0,
    "alert_film_hours": 4.0,
    "alert_cooldown_hours": 48,
    # grounding caution
    "wet_caution_hours": 8.0,
    "flight_limit_days": 30,
    # weather / message
    "forecast_horizon_days": 7,
    CONF_QUIET_START: 22,
    CONF_QUIET_END: 7,
    "chart_history_days": 75,
    # LLM flying-window prompt; blank = use the built-in default. {icao} is substituted.
    "window_prompt": "",
    # notification plumbing
    "telegram_target": "",
    "ai_task_entity": "ai_task.google_ai_task",
    "www_dir": "/homeassistant/www/moisture",
    "backfill_csv_glob": "",
}
