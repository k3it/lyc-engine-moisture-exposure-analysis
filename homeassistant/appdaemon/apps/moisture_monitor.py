"""
AppDaemon app: live engine-moisture monitor + "want to go flying?" nudge.

It pulls the X-Sense temp/RH history from Home Assistant's recorder, runs the
*canonical* physics model (scripts/model.py - imported, never copied, so editing
the model keeps this in sync), publishes cam-wetness sensors back to HA, and -
when the cam has sipped enough water since the last flight AND there's a calm VFR
window soon - composes a friendly Telegram nudge with charts.

The hot-run signature (cowl air > flight_temp_c) is already what model.py uses to
mark a flight; since_last_flight() slices the series there, so the running tally
means "what your cam has accumulated since you last flew". We persist the last
flight across runs so the tally survives even when it scrolls out of the window.

Design note: everything except the MoistureMonitor class is a plain function with
no AppDaemon/HA dependency, so the whole pipeline is exercised offline by
test_local.py. The AppDaemon import is guarded so this module imports anywhere.
"""
from __future__ import annotations
import os, sys, json, datetime as dt
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


# --------------------------------------------------------------------------------
# Locate the canonical model. scripts/ stays the single source of truth; we add it
# to sys.path rather than copying, so a `git pull` on the HA box updates the model
# and AppDaemon hot-reloads. Override with LYC_SCRIPTS_DIR if deployed elsewhere.
# --------------------------------------------------------------------------------
def _locate_scripts():
    here = Path(__file__).resolve()
    candidates = []
    env = os.environ.get("LYC_SCRIPTS_DIR")
    if env:
        candidates.append(Path(env))
    # repo layout: <root>/homeassistant/appdaemon/apps/moisture_monitor.py
    candidates.append(here.parents[3] / "scripts")
    candidates.append(here.parent / "scripts")  # flat fallback
    for c in candidates:
        if (c / "model.py").exists():
            if str(c) not in sys.path:
                sys.path.insert(0, str(c))
            return str(c)
    raise ImportError(
        "Could not find scripts/model.py. Deploy the whole repo and/or set "
        "LYC_SCRIPTS_DIR to the directory containing model.py."
    )


_SCRIPTS_DIR = _locate_scripts()
import glob as _glob  # noqa: E402
import pandas as pd  # noqa: E402  (after path setup is harmless; pandas is independent)
from model import (  # noqa: E402
    Params, regrid, analyze, episodes, since_last_flight, grounding_caution,
    load_csv,
)

# weather.py lives next to this file
sys.path.insert(0, str(Path(__file__).resolve().parent))
import weather  # noqa: E402


# --------------------------------------------------------------------------------
# Config defaults (mirror model.py constants; overridden by apps.yaml)
# --------------------------------------------------------------------------------
DEFAULTS = {
    "timezone": "America/New_York",
    "airport_icao": "KMRB",
    "latitude": 39.40,
    "longitude": -77.98,
    "tau_metal_h": 8.0,
    "tau_bulk_h": 24.0,
    "tau_event_h": 1.5,
    "dry_factor": 0.3,
    # run detection: absolute hot OR a rapid engine-heat rise (catches cooler/short runs)
    "flight_temp_c": 40.0,
    "flight_rise_c": 8.0,            # cowl rise (°C) over the window that signals a run
    "flight_rise_window_min": 10,
    "flight_run_floor_c": 32.0,      # the run's peak must clear this
    "flight_peak_window_min": 60,
    "flight_debounce_h": 6.0,
    "wet_caution_hours": 8.0,
    "flight_limit_days": 30,
    "temp_unit": "F",
    "run_every_minutes": 60,
    "window_days": 14,
    "max_window_days": 60,
    "spinup_days": 5,
    # exposure thresholds (since last flight) that earn a nudge — ANY of these fires
    "close_call_margin_c": 2.0,     # metal within this of the interior dew point = "close call"
    "alert_wet_hours": 2.0,         # hours the cam was at/below the dew point (condensing)
    "alert_close_call_hours": 20.0, # hours within close_call_margin_c of the dew point
    "alert_film_hours": 4.0,        # hours of persistent film (the conservative metric)
    "alert_cooldown_hours": 48,
    "forecast_horizon_days": 7,   # LLM looks for a window up to a week out (no further)
    "quiet_hours": [22, 7],
    "www_dir": "/homeassistant/www/moisture",
    "chart_url_base": "/local/moisture",   # /local maps to <config>/www
    # HA 2026.x AI Task (Gemini). Older builds used google_generative_ai_conversation.
    "ai_task_service": "ai_task/generate_data",
    "ai_task_entity": "ai_task.google_ai_task",
    "backfill_csv_glob": None,             # optional X-Sense CSV export(s) to seed history
}


# --------------------------------------------------------------------------------
# Pure pipeline helpers (no HA dependency)
# --------------------------------------------------------------------------------
def _local_tz(name):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return dt.timezone.utc


def hist_to_series(states, tz_name):
    """Recorder state list -> pandas Series indexed by NAIVE LOCAL time.

    `states` is a list of dicts with 'state' and 'last_changed'/'last_updated'
    (the shape AppDaemon's get_history returns per entity)."""
    rows = []
    for s in states or []:
        v = s.get("state")
        if v in (None, "unknown", "unavailable", "none", ""):
            continue
        try:
            val = float(v)
        except (TypeError, ValueError):
            continue
        ts = s.get("last_changed") or s.get("last_updated")
        if ts:
            rows.append((ts, val))
    if not rows:
        return pd.Series(dtype=float)
    idx = pd.to_datetime([r[0] for r in rows], utc=True, errors="coerce")
    ser = pd.Series([r[1] for r in rows], index=idx).dropna()
    ser = ser[~ser.index.duplicated(keep="last")].sort_index()
    ser.index = ser.index.tz_convert(_tz_str(tz_name)).tz_localize(None)
    return ser


def _tz_str(name):
    # pandas accepts the IANA string directly
    return name


def load_backfill_csv(glob_pattern):
    """Load + concatenate X-Sense CSV export(s) -> frame [Tc(°C), RH(%)] in naive
    LOCAL time (the index the exports already use). Returns None if nothing matches.
    Lets the very first runs have full-resolution history before the recorder has
    accumulated a long window; live recorder data overrides it where they overlap."""
    if not glob_pattern:
        return None
    paths = sorted(_glob.glob(glob_pattern))
    if not paths:
        return None
    frames = [load_csv(p) for p in paths]          # load_csv already does F->C
    df = pd.concat(frames)
    return df[~df.index.duplicated(keep="first")].sort_index()


def build_frame(temp_states, rh_states, tz_name, temp_unit="F", backfill_df=None,
                max_days=None):
    """Recorder histories (+ optional CSV backfill) -> regridded 1-min frame.

    The recorder series take precedence where they overlap the backfill, so live
    data wins and the CSV only fills the older tail. `max_days` trims the combined
    frame to the most recent N days before regridding, keeping each cycle fast even
    when a multi-month CSV is supplied for backfill."""
    t = hist_to_series(temp_states, tz_name)
    h = hist_to_series(rh_states, tz_name)
    live = None
    if not (t.empty or h.empty):
        tc = (t - 32.0) * 5.0 / 9.0 if str(temp_unit).upper().startswith("F") else t
        live = pd.concat([tc.rename("Tc"), h.rename("RH")], axis=1)
    if backfill_df is not None and len(backfill_df):
        bf = backfill_df[["Tc", "RH"]]
        df = pd.concat([bf, live]) if live is not None else bf
        df = df[~df.index.duplicated(keep="last")].sort_index()  # live (last) wins
    elif live is not None:
        df = live.sort_index()
    else:
        return None
    if max_days:
        cutoff = df.index.max() - pd.Timedelta(days=max_days)
        df = df.loc[df.index >= cutoff]
    g = regrid(df)
    return g if len(g) else None


def make_params(cfg):
    p = Params(
        tau_metal_s=cfg["tau_metal_h"] * 3600,
        tau_bulk_s=cfg["tau_bulk_h"] * 3600,
        tau_event_s=cfg["tau_event_h"] * 3600,
        dry_factor=cfg["dry_factor"],
        flight_temp_c=cfg["flight_temp_c"],
    )
    # optional run-detection overrides (rapid-rise); fall back to model defaults
    for k in ("flight_rise_c", "flight_rise_window_min", "flight_run_floor_c",
              "flight_peak_window_min", "flight_debounce_h"):
        if cfg.get(k) is not None:
            setattr(p, k, cfg[k])
    return p


def run_model(g, cfg):
    """Full model pass; returns (res, series). Mirrors model.py's CLI assembly."""
    res, series = analyze(g, make_params(cfg))
    res["episodes"] = episodes(series)
    res["since_last_flight"] = since_last_flight(series, res)
    res["grounding_caution"] = grounding_caution(
        res["since_last_flight"],
        wet_caution_h=cfg["wet_caution_hours"],
        flight_limit_d=cfg["flight_limit_days"],
    )
    return res, series


def reconcile_last_flight(res, series, stored_last_flight, cfg):
    """Carry the last flight across runs and detect a NEW hot run.

    Returns (last_flight_iso, is_new_flight). If the model saw a newer flight than
    we had stored, that's a new run (reset point). If the model saw none (flight
    scrolled out of the window) but we have one stored, keep it and recompute the
    since-last-flight tally from what's available."""
    model_lf = res.get("last_flight")
    is_new = False
    if model_lf and (stored_last_flight is None or model_lf > stored_last_flight):
        last_flight = model_lf
        is_new = stored_last_flight is not None  # first-ever detection isn't a "reset"
    elif stored_last_flight:
        last_flight = stored_last_flight
        if model_lf != stored_last_flight:
            # recompute tally against the carried-forward flight time
            res["last_flight"] = stored_last_flight
            res["since_last_flight"] = since_last_flight(series, res)
            res["grounding_caution"] = grounding_caution(
                res["since_last_flight"],
                wet_caution_h=cfg["wet_caution_hours"],
                flight_limit_d=cfg["flight_limit_days"],
            )
    else:
        last_flight = model_lf
    res["last_flight"] = last_flight
    return last_flight, is_new


def in_quiet_hours(now_local, quiet):
    qs, qe = quiet
    h = now_local.hour
    return (h >= qs or h < qe) if qs > qe else (qs <= h < qe)


def near_wet_stats(series, res, margin_c=2.0):
    """Deterministic condensation exposure SINCE the last flight, including the
    'close calls' the LLM must not paper over: hours the cam metal sat at/below the
    interior dew point, and hours it came within margin_c of it without crossing."""
    lf = res.get("last_flight")
    s = series.loc[lf:] if lf else series
    gap = (s["Td_int"] - s["Tm"])           # >0 => condensing; near 0 => close call
    sub = gap > 0
    close = (gap > -margin_c) & (gap <= 0)
    peak = float(gap.max()) if len(gap) else 0.0
    return {
        "margin_c": margin_c,
        "sub_dew_h": round(float(sub.sum()) / 60, 1),
        "close_call_h": round(float(close.sum()) / 60, 1),
        "peak_gap_c": round(peak, 2),       # >0: crossed the dew point by this; <=0: closest approach
    }


def moisture_status_line(res, series, margin_c=2.0, nw=None):
    """The deterministic 'warning' — computed from the model, NOT the LLM."""
    slf = res["since_last_flight"]
    nw = nw or near_wet_stats(series, res, margin_c)
    latest = slf.get("latest", {})
    bits = [f"{slf.get('days')} d since last flight."]
    m = int(round(margin_c))
    if nw["sub_dew_h"] >= 0.1:
        tail = (f", and within {m} °C for {nw['close_call_h']} h"
                if nw["close_call_h"] > 0 else "")
        bits.append(f"Cam was at/below the dew point for {nw['sub_dew_h']} h{tail} "
                    f"(peak +{nw['peak_gap_c']:.1f} °C over).")
    elif nw["close_call_h"] >= 0.5:
        bits.append(f"No condensation, but the cam came within {m} °C of the dew "
                    f"point for {nw['close_call_h']} h (closest {abs(nw['peak_gap_c']):.1f} °C).")
    else:
        bits.append("Cam stayed comfortably dry.")
    if slf.get("film_hours", 0) >= 0.1:
        bits.append(f"Persistent film {slf['film_hours']} h.")
    if latest.get("Tc") is not None:
        bits.append(f"Latest {latest['Tc']} °C / {latest.get('RH')}% RH.")
    return " ".join(bits)


def decide_alert(nw, slf, state, cfg, now_local):
    """Fire on real moisture exposure (any threshold). Weather is NOT a gate here —
    Gemini predicts the flying window in the message. Returns (fire, reason)."""
    wet = nw["sub_dew_h"] >= cfg["alert_wet_hours"]
    close = nw["close_call_h"] >= cfg["alert_close_call_hours"]
    film = slf.get("film_hours", 0) >= cfg["alert_film_hours"]
    if not (wet or close or film):
        return False, "below exposure thresholds"
    if in_quiet_hours(now_local, cfg["quiet_hours"]):
        return False, "quiet hours"
    last = state.get("last_alert_ts")
    if last:
        try:
            last_dt = dt.datetime.fromisoformat(last)
            if (now_local - last_dt) < dt.timedelta(hours=cfg["alert_cooldown_hours"]):
                return False, "within alert cooldown"
        except ValueError:
            pass
    why = []
    if wet:
        why.append(f"{nw['sub_dew_h']}h sub-dew")
    if close:
        why.append(f"{nw['close_call_h']}h close-call")
    if film:
        why.append(f"{slf['film_hours']}h film")
    return True, "fire: " + ", ".join(why)


def pick_event_center(series, res):
    """Most recent meaningful condensation moment to center charts on."""
    eps = res.get("episodes") or []
    if eps:
        return eps[-1]["start"]
    cond = series["Tm"] < series["Td_int"]
    if bool(cond.any()):
        return series.index[cond][-1].isoformat()
    return series.index[-1].isoformat()


def build_window_prompt(weather_info, icao="KMRB"):
    """Gemini's ONLY job: predict the next good flying window from the forecast.
    It must not touch the moisture interpretation (that's deterministic)."""
    brief = weather_info.get("forecast_brief") or weather_info.get("summary") or ""
    return (
        f"You are a CFI-minded weather assistant helping a pilot pick the next good "
        f"day to fly a light piston aircraft from {icao}. Using the forecast data "
        f"below (TAF from aviationweather.gov plus a multi-day outlook), identify the "
        f"NEXT good flying window within the next 7 DAYS. Do NOT speculate beyond 7 "
        f"days — the forecast becomes unreliable. Reply with ONE short, friendly "
        f"sentence naming the day and rough time of day.\n"
        f"Ideal conditions: no thunderstorms, no frontal passage, ceilings above "
        f"2000 ft AGL, and sustained wind 10 kt or less. In summer, early morning or "
        f"evening are usually calmest and smoothest — prefer those — but watch for "
        f"afternoon/evening thunderstorms. Be honest: never call a breezy, stormy, or "
        f"low-ceiling period 'calm' or 'perfect'; if nothing in the next 7 days "
        f"clearly fits, say conditions look unsettled and to watch for the next calm "
        f"day. Do NOT mention engine moisture, condensation, or the cam.\n\n"
        f"Forecast:\n" + str(brief)
    )


def window_fallback(weather_info):
    """Deterministic next-window line if Gemini is unavailable."""
    bw = weather_info.get("best_window")
    if bw:
        return f"Next likely calm VFR window: {bw['phrase']}."
    return ("Winds look unsettled the next few days — watch for the next calm VFR "
            "day. " + (weather_info.get("taf") or ""))


def assemble_message(moisture_line, window_line):
    """One Telegram message: deterministic moisture status + Gemini flying window."""
    return f"🛩️ Lycoming moisture watch\n\n{moisture_line}\n\n✈️ {window_line}"


def extract_llm_text(resp):
    """Pull the generated text out of whatever shape the service returns."""
    if resp is None:
        return None
    if isinstance(resp, str):
        return resp.strip() or None
    if isinstance(resp, dict):
        # ai_task.generate_data -> {"data": "..."}; older shapes use text/response.
        for key in ("data", "text", "response", "speech", "plain", "result"):
            if key in resp:
                got = extract_llm_text(resp[key])
                if got:
                    return got
        # last resort: first string value found, ignoring ids
        for k, v in resp.items():
            if k in ("conversation_id", "id"):
                continue
            got = extract_llm_text(v)
            if got:
                return got
    if isinstance(resp, (list, tuple)) and resp:
        return extract_llm_text(resp[0])
    return None


# --------------------------------------------------------------------------------
# AppDaemon orchestration (guarded import so the helpers above stay importable)
# --------------------------------------------------------------------------------
try:
    import appdaemon.plugins.hass.hassapi as hass
    _BASE = hass.Hass
except Exception:  # pragma: no cover - not running under AppDaemon
    _BASE = object


class MoistureMonitor(_BASE):
    # ---- lifecycle ----
    def initialize(self):
        self.cfg = {**DEFAULTS, **{k: v for k, v in self.args.items()
                                   if k not in ("module", "class")}}
        self.tz = _local_tz(self.cfg["timezone"])
        self.state_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "moisture_state.json")
        self.state = self._load_state()
        os.makedirs(self.cfg["www_dir"], exist_ok=True)

        interval = int(self.cfg["run_every_minutes"]) * 60
        self.run_every(self.run_cycle, "now+30", interval)
        self.listen_event(self.on_manual, "lyc_moisture_run")
        trig = self.cfg.get("manual_trigger")
        if trig:
            self.listen_state(self.on_manual_state, trig, new="on")
        self.log(f"MoistureMonitor up. Model from {_SCRIPTS_DIR}. "
                 f"Cycle every {self.cfg['run_every_minutes']} min.")

    def on_manual(self, event_name, data, kwargs):
        self.log("Manual run requested via event.")
        self.run_cycle({"force": bool((data or {}).get("force"))})

    def on_manual_state(self, entity, attribute, old, new, kwargs):
        self.log("Manual run requested via input_boolean.")
        self.run_cycle({})

    # ---- main cycle ----
    def run_cycle(self, kwargs):
        try:
            self._cycle(force=bool(kwargs.get("force")))
        except Exception as e:
            self.error(f"moisture cycle failed: {e}", level="ERROR")
            raise

    def _cycle(self, force=False):
        cfg = self.cfg
        now_local = dt.datetime.now(self.tz).replace(tzinfo=None)

        # dynamic window: enough to cover since-last-flight + lag spin-up
        stored_lf = self.state.get("last_flight")
        win_days = cfg["window_days"]
        if stored_lf:
            try:
                lf_dt = dt.datetime.fromisoformat(stored_lf)
                grounded_days = (now_local - lf_dt).days
                win_days = min(cfg["max_window_days"],
                               max(cfg["window_days"], grounded_days + cfg["spinup_days"]))
            except ValueError:
                pass

        temp_hist = self._history(cfg["temp_entity"], win_days)
        rh_hist = self._history(cfg["humidity_entity"], win_days)
        backfill = load_backfill_csv(cfg.get("backfill_csv_glob"))
        g = build_frame(temp_hist, rh_hist, cfg["timezone"], cfg["temp_unit"],
                        backfill_df=backfill,
                        max_days=cfg["max_window_days"] + cfg["spinup_days"])
        if g is None:
            self.log("No usable sensor history this cycle; skipping.")
            return

        res, series = run_model(g, cfg)
        last_flight, is_new_flight = reconcile_last_flight(
            res, series, stored_lf, cfg)

        if is_new_flight:
            self.log(f"New hot run detected at {last_flight}; tally reset.")
            self.state["last_alert_ts"] = None
        self.state["last_flight"] = last_flight
        self._save_state()

        self._publish_sensors(res)

        nw = near_wet_stats(series, res, cfg["close_call_margin_c"])
        fire, reason = decide_alert(nw, res["since_last_flight"], self.state, cfg, now_local)
        if force:
            fire, reason = True, "forced"
        self.log(f"Alert decision: {fire} ({reason}). "
                 f"sub_dew={nw['sub_dew_h']}h close_call={nw['close_call_h']}h "
                 f"film={res['since_last_flight'].get('film_hours')}h")
        if fire:
            weather_info = weather.assess(
                cfg["airport_icao"], cfg["latitude"], cfg["longitude"],
                cfg["timezone"], cfg["forecast_horizon_days"])
            self._send_alert(res, series, nw, weather_info)
            self.state["last_alert_ts"] = now_local.isoformat()
            self._save_state()

    # ---- HA I/O ----
    def _history(self, entity_id, days):
        try:
            data = self.get_history(entity_id=entity_id, days=days)
        except Exception as e:
            self.error(f"get_history failed for {entity_id}: {e}", level="WARNING")
            return []
        if data and isinstance(data[0], list):  # list-of-lists shape
            return data[0]
        return data or []

    def _publish_sensors(self, res):
        slf = res["since_last_flight"]
        gc = res["grounding_caution"]
        latest = slf.get("latest", {})
        self.set_state(
            "sensor.cam_film_hours_since_flight",
            state=round(slf.get("film_hours", 0), 1),
            attributes={
                "unit_of_measurement": "h",
                "friendly_name": "Cam wet-hours since last flight",
                "icon": "mdi:water-percent",
                "days_since_flight": slf.get("days"),
                "wet_hours_realistic": slf.get("wet_hours_realistic"),
                "ambient_damp_hours_ub": slf.get("ambient_damp_hours_ub"),
                "last_flight": res.get("last_flight"),
                "latest_temp_c": latest.get("Tc"),
                "latest_rh_pct": latest.get("RH"),
                "latest_reading": latest.get("time"),
            })
        self.set_state(
            "sensor.cam_days_since_flight",
            state=slf.get("days"),
            attributes={"unit_of_measurement": "d",
                        "friendly_name": "Days since last flight",
                        "icon": "mdi:calendar-clock"})
        self.set_state(
            "sensor.cam_last_flight",
            state=res.get("last_flight") or "unknown",
            attributes={"friendly_name": "Last engine run",
                        "icon": "mdi:airplane-takeoff",
                        "flight_count": res.get("flight_count")})
        self.set_state(
            "binary_sensor.cam_grounding_caution",
            state="on" if gc.get("caution") else "off",
            attributes={"device_class": "problem",
                        "friendly_name": "Cam grounding caution",
                        "reason": gc.get("reason"),
                        "wet_hours_since_flight": gc.get("wet_hours_since_flight"),
                        "days_grounded": gc.get("days_grounded")})

    def _send_alert(self, res, series, nw, weather_info):
        cfg = self.cfg
        # deterministic moisture status (model) + Gemini-predicted flying window
        moisture_line = moisture_status_line(res, series, cfg["close_call_margin_c"], nw)
        window_line = self._window(weather_info)
        text = assemble_message(moisture_line, window_line)

        # single summary chart, attached to the one message as its caption
        chart = self._make_chart(series, res, nw)
        target = cfg.get("telegram_target")
        try:
            if chart:
                kw = {"file": chart, "caption": text}
                if target:
                    kw["target"] = target
                self.call_service("telegram_bot/send_photo", **kw)
            else:
                kw = {"message": text}
                if target:
                    kw["target"] = target
                self.call_service("telegram_bot/send_message", **kw)
        except Exception as e:
            self.error(f"telegram send failed: {e}", level="ERROR")
        self.log("Sent flying nudge (1 message, summary chart).")

    def _window(self, weather_info):
        """Gemini predicts ONLY the next flying window; never the moisture."""
        prompt = build_window_prompt(weather_info, self.cfg["airport_icao"])
        svc = self.cfg["ai_task_service"]
        try:
            resp = self.call_service(
                svc, task_name="flying_window", instructions=prompt,
                entity_id=self.cfg["ai_task_entity"], return_result=True)
            text = extract_llm_text(resp)
            if text:
                return text
        except Exception as e:
            self.error(f"LLM window predict failed ({svc}): {e}", level="WARNING")
        return window_fallback(weather_info)

    def _make_chart(self, series, res, nw):
        try:
            import charts as ch  # from scripts/, on sys.path
        except Exception as e:
            self.error(f"charts import failed: {e}", level="WARNING")
            return None
        path = os.path.join(self.cfg["www_dir"],
                            f"summary_{dt.datetime.now():%Y%m%d_%H%M}.png")
        try:
            return ch.summary_chart(series, res, path, margin_c=nw["margin_c"])
        except Exception as e:
            self.error(f"summary chart failed: {e}", level="WARNING")
            return None

    # ---- state persistence ----
    def _load_state(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"last_flight": None, "last_alert_ts": None}

    def _save_state(self):
        try:
            with open(self.state_path, "w") as f:
                json.dump(self.state, f, indent=2)
        except OSError as e:
            self.error(f"could not persist state: {e}", level="WARNING")
