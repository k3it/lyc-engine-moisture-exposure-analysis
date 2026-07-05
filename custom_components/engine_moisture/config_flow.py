"""Config + Options flow for Engine Moisture Monitor.

Initial config captures the deployment identity (sensors, airport, scripts path).
The Options flow exposes every tunable — schedule, model parameters, alert thresholds —
as a single GUI form, which is the whole point of the refactor.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_AIRPORT,
    CONF_HUMIDITY_ENTITY,
    CONF_LAT,
    CONF_LON,
    CONF_QUIET_END,
    CONF_QUIET_START,
    CONF_SCRIPTS_DIR,
    CONF_TEMP_ENTITY,
    CONF_TEMP_UNIT,
    CONF_TZ,
    DOMAIN,
    OPTION_DEFAULTS,
)

_SENSOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor"))
_UNIT = selector.SelectSelector(
    selector.SelectSelectorConfig(options=["F", "C"], translation_key="temp_unit"))


def _num(minv=None, maxv=None, step=0.1, mode="box", unit=None):
    # NumberSelectorConfig is a TypedDict — build it as a dict, don't set attributes.
    conf: dict[str, Any] = {"step": step, "mode": mode}
    if minv is not None:
        conf["min"] = minv
    if maxv is not None:
        conf["max"] = maxv
    if unit is not None:
        conf["unit_of_measurement"] = unit
    return selector.NumberSelector(conf)


def _options_schema(current: dict[str, Any]) -> vol.Schema:
    """Build the Options form, defaulting each field to the current value or default."""
    def d(key):
        return current.get(key, OPTION_DEFAULTS[key])

    return vol.Schema({
        # schedule
        vol.Required("run_every_minutes", default=d("run_every_minutes")): _num(5, 1440, 5, unit="min"),
        # model parameters
        vol.Required("tau_metal_h", default=d("tau_metal_h")): _num(0.5, 48, 0.5, unit="h"),
        vol.Required("tau_bulk_h", default=d("tau_bulk_h")): _num(1, 96, 0.5, unit="h"),
        vol.Required("tau_event_h", default=d("tau_event_h")): _num(0.1, 24, 0.1, unit="h"),
        vol.Required("dry_factor", default=d("dry_factor")): _num(0.05, 1.0, 0.05),
        vol.Required("flight_temp_c", default=d("flight_temp_c")): _num(20, 80, 1, unit="°C"),
        vol.Required("flight_rise_c", default=d("flight_rise_c")): _num(2, 30, 1, unit="°C"),
        vol.Required("flight_rise_window_min", default=d("flight_rise_window_min")): _num(2, 60, 1, unit="min"),
        vol.Required("flight_run_floor_c", default=d("flight_run_floor_c")): _num(15, 60, 1, unit="°C"),
        vol.Required("flight_peak_window_min", default=d("flight_peak_window_min")): _num(5, 180, 5, unit="min"),
        vol.Required("flight_debounce_h", default=d("flight_debounce_h")): _num(0.5, 24, 0.5, unit="h"),
        # trailing window / spin-up
        vol.Required("window_days", default=d("window_days")): _num(1, 120, 1, unit="d"),
        vol.Required("max_window_days", default=d("max_window_days")): _num(7, 180, 1, unit="d"),
        vol.Required("spinup_days", default=d("spinup_days")): _num(0, 30, 1, unit="d"),
        # alert thresholds
        vol.Required("close_call_margin_c", default=d("close_call_margin_c")): _num(0.5, 10, 0.5, unit="°C"),
        vol.Required("alert_wet_hours", default=d("alert_wet_hours")): _num(0, 240, 0.5, unit="h"),
        vol.Required("alert_close_call_hours", default=d("alert_close_call_hours")): _num(0, 500, 1, unit="h"),
        vol.Required("alert_film_hours", default=d("alert_film_hours")): _num(0, 240, 0.5, unit="h"),
        vol.Required("alert_cooldown_hours", default=d("alert_cooldown_hours")): _num(0, 336, 1, unit="h"),
        # grounding caution
        vol.Required("wet_caution_hours", default=d("wet_caution_hours")): _num(0, 240, 0.5, unit="h"),
        vol.Required("flight_limit_days", default=d("flight_limit_days")): _num(1, 365, 1, unit="d"),
        # sensor gap-fill fallback
        vol.Required("gapfill_enabled", default=d("gapfill_enabled")): selector.BooleanSelector(),
        vol.Required("gapfill_stale_min", default=d("gapfill_stale_min")): _num(61, 1440, 1, unit="min"),
        vol.Optional("transfer_params_path", default=d("transfer_params_path")): selector.TextSelector(),
        # weather / chart / message
        vol.Required("forecast_horizon_days", default=d("forecast_horizon_days")): _num(1, 7, 1, unit="d"),
        vol.Required("chart_history_days", default=d("chart_history_days")): _num(7, 180, 1, unit="d"),
        vol.Required(CONF_QUIET_START, default=d(CONF_QUIET_START)): _num(0, 23, 1, unit="h"),
        vol.Required(CONF_QUIET_END, default=d(CONF_QUIET_END)): _num(0, 23, 1, unit="h"),
        # notification plumbing
        vol.Optional("telegram_target", default=d("telegram_target")): selector.TextSelector(),
        vol.Optional("ai_task_entity", default=d("ai_task_entity")): selector.TextSelector(),
        vol.Optional("www_dir", default=d("www_dir")): selector.TextSelector(),
        vol.Optional("backfill_csv_glob", default=d("backfill_csv_glob")): selector.TextSelector(),
        vol.Optional("window_prompt", default=d("window_prompt")): selector.TextSelector(
            selector.TextSelectorConfig(multiline=True)),
    })


class EngineMoistureConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            return self.async_create_entry(title="Engine Moisture Monitor", data=user_input)

        schema = vol.Schema({
            vol.Required(CONF_TEMP_ENTITY): _SENSOR,
            vol.Required(CONF_HUMIDITY_ENTITY): _SENSOR,
            vol.Required(CONF_TEMP_UNIT, default="F"): _UNIT,
            vol.Required(CONF_AIRPORT, default="KMRB"): selector.TextSelector(),
            vol.Required(CONF_LAT, default=self.hass.config.latitude): _num(-90, 90, "any"),
            vol.Required(CONF_LON, default=self.hass.config.longitude): _num(-180, 180, "any"),
            vol.Required(CONF_TZ, default=self.hass.config.time_zone): selector.TextSelector(),
            vol.Optional(CONF_SCRIPTS_DIR, default=""): selector.TextSelector(),
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EngineMoistureOptionsFlow()


class EngineMoistureOptionsFlow(OptionsFlow):
    """GUI form for all tunables."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=_options_schema(current))
