"""DataUpdateCoordinator: pulls recorder history, runs the canonical model off the
event loop, persists the last-flight / last-alert state, and sends the flying nudge.

All heavy imports (pandas, the model, charts, weather) are deferred into the executor
job so nothing blocks the event loop and they resolve only after scripts/ is on
sys.path (added in __init__)."""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

from homeassistant.components.recorder import get_instance, history
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AIRPORT,
    CONF_HUMIDITY_ENTITY,
    CONF_LAT,
    CONF_LON,
    CONF_QUIET_END,
    CONF_QUIET_START,
    CONF_TEMP_ENTITY,
    CONF_TEMP_UNIT,
    CONF_TZ,
    DOMAIN,
    OPTION_DEFAULTS,
)

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1


class EngineMoistureCoordinator(DataUpdateCoordinator):
    """Coordinates a periodic moisture-model cycle for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, scripts_dir: str) -> None:
        # _build_cfg() (below) needs self.hass/self.entry, but DataUpdateCoordinator
        # only sets self.hass in its __init__ — which we can't call first because it
        # needs the update_interval that _build_cfg computes. Set them up front.
        self.hass = hass
        self.entry = entry
        self.scripts_dir = scripts_dir
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._state: dict[str, Any] = {"last_flight": None, "last_alert_ts": None}
        self.last_run: dt.datetime | None = None
        cfg = self._build_cfg()
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=dt.timedelta(minutes=int(cfg["run_every_minutes"])),
        )

    # ---- config ----
    def _build_cfg(self) -> dict[str, Any]:
        """Merge pipeline defaults <- GUI option defaults <- entry data/options.
        pipeline.DEFAULTS is imported lazily (scripts/ is on sys.path by call time)."""
        try:
            import pipeline  # noqa: PLC0415  (scripts/ on sys.path)

            base = dict(pipeline.DEFAULTS)
        except Exception:  # pragma: no cover - first import guard
            base = {}
        cfg = {**base, **OPTION_DEFAULTS, **self.entry.data, **self.entry.options}
        # reassemble the quiet-hours list the pipeline expects
        cfg["quiet_hours"] = [
            int(cfg.get(CONF_QUIET_START, 22)),
            int(cfg.get(CONF_QUIET_END, 7)),
        ]
        # normalize empty-string GUI fields to None/sane values
        if not cfg.get("telegram_target"):
            cfg["telegram_target"] = None
        if not cfg.get("backfill_csv_glob"):
            cfg["backfill_csv_glob"] = None
        cfg.setdefault(CONF_TZ, self.hass.config.time_zone)
        # The chart file must live under an allowlist_external_dirs path for
        # telegram_bot/send_photo. Default to <config>/www/moisture (i.e. /config/www/..,
        # which HA allowlists by default and serves at /local). Empty or the old
        # hard-coded default both fall through to this derived path.
        www = cfg.get("www_dir")
        if not www or www == "/homeassistant/www/moisture":
            cfg["www_dir"] = self.hass.config.path("www", "moisture")
        return cfg

    # ---- state persistence ----
    async def async_load_state(self) -> None:
        data = await self._store.async_load()
        if isinstance(data, dict):
            self._state.update(data)

    async def _async_save_state(self) -> None:
        await self._store.async_save(self._state)

    # ---- public trigger ----
    async def async_run_now(self, force: bool = False) -> None:
        """Manual trigger (engine_moisture.run_now). force bypasses thresholds/quiet/cooldown."""
        self._force_alert = force
        await self.async_refresh()

    # ---- main cycle ----
    async def _async_update_data(self) -> dict[str, Any]:
        force = getattr(self, "_force_alert", False)
        self._force_alert = False
        cfg = self._build_cfg()

        now_local = dt_util.now().replace(tzinfo=None)
        fetch_days = self._fetch_days(cfg, now_local)

        temp_states = await self._recorder_history(cfg[CONF_TEMP_ENTITY], fetch_days)
        rh_states = await self._recorder_history(cfg[CONF_HUMIDITY_ENTITY], fetch_days)

        try:
            out = await self.hass.async_add_executor_job(
                self._compute, cfg, temp_states, rh_states, dict(self._state), now_local
            )
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"moisture model failed: {err}") from err

        if out is None:
            raise UpdateFailed("no usable sensor history this cycle")

        # commit reconciled state
        if out["is_new_flight"]:
            self._state["last_alert_ts"] = None
        self._state["last_flight"] = out["last_flight"]
        await self._async_save_state()

        fire = out["fire"] or force
        if fire:
            sent = await self._send_alert(cfg, out, now_local)
            if sent:  # only record an alert that actually went out
                self._state["last_alert_ts"] = now_local.isoformat()
                await self._async_save_state()

        self.last_run = dt_util.now()
        data = out["data"]
        data["fired"] = fire
        data["last_alert"] = self._state["last_alert_ts"]
        return data

    def _fetch_days(self, cfg: dict[str, Any], now_local: dt.datetime) -> int:
        """Trailing history to pull: enough to cover since-last-flight + spin-up, and
        always at least the chart's history window so the alert chart shows context."""
        base = int(cfg["window_days"])
        lf = self._state.get("last_flight")
        if lf:
            try:
                grounded = (now_local - dt.datetime.fromisoformat(lf)).days
                base = min(int(cfg["max_window_days"]),
                           max(int(cfg["window_days"]), grounded + int(cfg["spinup_days"])))
            except ValueError:
                pass
        return max(base, int(cfg["chart_history_days"]) + int(cfg["spinup_days"]))

    async def _recorder_history(self, entity_id: str, days: int) -> list[dict]:
        """Fetch recorder state changes for one entity, reduced to the plain dict shape
        pipeline.hist_to_series expects ({'state', 'last_changed'})."""
        start = dt_util.utcnow() - dt.timedelta(days=days)

        def _fetch():
            return history.state_changes_during_period(
                self.hass, start, None, entity_id,
                include_start_time_state=True, no_attributes=True)

        result = await get_instance(self.hass).async_add_executor_job(_fetch)
        states = result.get(entity_id, []) if result else []
        return [
            {"state": s.state, "last_changed": s.last_changed.isoformat()}
            for s in states
        ]

    # ---- blocking compute (executor) ----
    def _compute(self, cfg, temp_states, rh_states, state, now_local):
        import pipeline as pl  # scripts/ on sys.path

        backfill = pl.load_backfill_csv(cfg.get("backfill_csv_glob"))
        g = pl.build_frame(
            temp_states, rh_states, cfg[CONF_TZ], cfg[CONF_TEMP_UNIT],
            backfill_df=backfill,
            max_days=int(cfg["chart_history_days"]) + int(cfg["spinup_days"]))
        if g is None:
            return None

        res, series = pl.run_model(g, cfg)
        last_flight, is_new = pl.reconcile_last_flight(res, series, state.get("last_flight"), cfg)
        nw = pl.near_wet_stats(series, res, cfg["close_call_margin_c"])
        fire, reason = pl.decide_alert(nw, res["since_last_flight"], state, cfg, now_local)

        slf = res["since_last_flight"]
        gc = res["grounding_caution"]
        latest = slf.get("latest", {})
        data = {
            "film_hours_since_flight": round(float(slf.get("film_hours", 0)), 1),
            "days_since_flight": slf.get("days"),
            "wet_hours_realistic": slf.get("wet_hours_realistic"),
            "ambient_damp_hours_ub": slf.get("ambient_damp_hours_ub"),
            "sub_dew_h": nw["sub_dew_h"],
            "close_call_h": nw["close_call_h"],
            "peak_gap_c": nw["peak_gap_c"],
            "last_flight": last_flight,
            "flight_count": res.get("flight_count"),
            "latest_temp_c": latest.get("Tc"),
            "latest_rh_pct": latest.get("RH"),
            "latest_reading": latest.get("time"),
            "grounding_caution": bool(gc.get("caution")),
            "gc_reason": gc.get("reason"),
            "gc_wet_hours": gc.get("wet_hours_since_flight"),
            "gc_days_grounded": gc.get("days_grounded"),
            "alert_reason": reason,
        }
        # keep res/series for the alert path (same process; not stored on the entity)
        return {
            "data": data, "res": res, "series": series, "nw": nw,
            "last_flight": last_flight, "is_new_flight": is_new, "fire": fire,
        }

    # ---- alerting ----
    async def _send_alert(self, cfg, out, now_local) -> bool:
        # weather lookahead (blocking urllib) + chart render in the executor
        weather_info = await self.hass.async_add_executor_job(
            self._assess_weather, cfg)
        window_line = await self._predict_window(cfg, weather_info)
        chart_path = await self.hass.async_add_executor_job(
            self._render_chart, cfg, out["series"], out["res"], out["nw"])
        moisture_line = await self.hass.async_add_executor_job(
            self._moisture_line, cfg, out["res"], out["series"], out["nw"])

        import pipeline as pl
        text = pl.assemble_message(moisture_line, window_line)
        sent = await self._send_telegram(cfg, text, chart_path)
        if sent:
            _LOGGER.info("Engine moisture nudge sent (%s)", out["data"].get("alert_reason"))
        return sent

    def _assess_weather(self, cfg):
        import weather
        return weather.assess(
            cfg[CONF_AIRPORT], cfg[CONF_LAT], cfg[CONF_LON],
            cfg[CONF_TZ], int(cfg["forecast_horizon_days"]))

    def _moisture_line(self, cfg, res, series, nw):
        import pipeline as pl
        return pl.moisture_status_line(res, series, cfg["close_call_margin_c"], nw)

    def _render_chart(self, cfg, series, res, nw):
        try:
            import charts as ch
            os.makedirs(cfg["www_dir"], exist_ok=True)
            path = os.path.join(
                cfg["www_dir"], f"summary_{dt.datetime.now():%Y%m%d_%H%M}.png")
            return ch.summary_chart(
                series, res, path, margin_c=nw["margin_c"],
                history_days=int(cfg["chart_history_days"]))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("summary chart failed: %s", err)
            return None

    async def _predict_window(self, cfg, weather_info) -> str:
        """Gemini predicts ONLY the next flying window; deterministic fallback otherwise."""
        import pipeline as pl
        custom = (cfg.get("window_prompt") or "").strip()
        if custom:
            brief = weather_info.get("forecast_brief") or weather_info.get("summary") or ""
            prompt = custom.replace("{icao}", str(cfg[CONF_AIRPORT])) + "\n\nForecast:\n" + str(brief)
        else:
            prompt = pl.build_window_prompt(weather_info, cfg[CONF_AIRPORT])
        domain, _, service = str(cfg["ai_task_service"]).partition("/")
        service = service or "generate_data"
        try:
            resp = await self.hass.services.async_call(
                domain or "ai_task", service,
                {"task_name": "flying_window", "instructions": prompt,
                 "entity_id": cfg["ai_task_entity"]},
                blocking=True, return_response=True)
            text = pl.extract_llm_text(resp)
            if text:
                return text
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("AI Task window predict failed: %s", err)
        return pl.window_fallback(weather_info)

    async def _send_telegram(self, cfg, text, chart_path) -> bool:
        """Send the nudge. Try the chart photo first; on any failure fall back to a
        text-only message so a chart/allowlist problem never silences the alert.
        Returns True only if something was actually delivered."""
        target = cfg.get("telegram_target")
        base = {"target": target} if target else {}
        if chart_path:
            try:
                await self.hass.services.async_call(
                    "telegram_bot", "send_photo",
                    {"file": chart_path, "caption": text, **base}, blocking=True)
                return True
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("send_photo failed (%s); falling back to text", err)
        try:
            await self.hass.services.async_call(
                "telegram_bot", "send_message", {"message": text, **base}, blocking=True)
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("telegram send failed: %s", err)
            return False
