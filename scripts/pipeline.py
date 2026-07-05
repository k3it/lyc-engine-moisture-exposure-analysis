"""
Pure, HA-agnostic orchestration pipeline around the canonical model.

Everything here is a plain function with no Home Assistant / AppDaemon dependency, so
the whole monitoring pipeline can be exercised offline (see homeassistant/test_local.py)
and reused by every host: the custom_components/engine_moisture integration and the
run_once.py CLI both import these helpers. The physics live in model.py (the single
source of truth); this module only shapes inputs, runs the model, and turns the result
into the monitor's headline numbers, alert decision, and message text.
"""
from __future__ import annotations
import glob as _glob
import datetime as dt

import pandas as pd

from model import (
    Params, regrid, analyze, episodes, since_last_flight, grounding_caution, load_csv,
)

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


# --------------------------------------------------------------------------------
# Config defaults (mirror model.py constants; hosts override via apps.yaml / Options)
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
    "chart_history_days": 75,     # how much past history the alert chart shows (~2.5 mo)
    "www_dir": "/homeassistant/www/moisture",
    "chart_url_base": "/local/moisture",   # /local maps to <config>/www
    # HA 2026.x AI Task (Gemini). Older builds used google_generative_ai_conversation.
    "ai_task_service": "ai_task/generate_data",
    "ai_task_entity": "ai_task.google_ai_task",
    "backfill_csv_glob": None,             # optional X-Sense CSV export(s) to seed history
    # ---- sensor gap-fill fallback (stale/offline cowl feed -> station transfer model,
    #      see SKILL.md 'Sensor gap-fill fallback' and reports/cowl_station_backtest.md) ----
    "gapfill_enabled": True,
    "gapfill_stale_min": 90,        # a data hole / stale tail longer than this gets filled
    "gapfill_spinup_h": 24,         # extra station history before a gap to settle the lags
    "gapfill_rh_margin_pct": 5.0,   # conservative RH bump on synthesized near-saturation air
    "transfer_params_path": None,   # saved fit_transfer JSON; hosts derive <repo>/data/... if unset
    "backup_airport_icao": None,    # last-resort nearby AWOS when the primary has no data too
}


# --------------------------------------------------------------------------------
# Pure pipeline helpers
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
    (the shape AppDaemon's get_history and the recorder both reduce to)."""
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
    ser.index = ser.index.tz_convert(tz_name).tz_localize(None)
    return ser


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


def find_sensor_gaps(g, now_local, stale_min):
    """Sensor outages in a 1-min regridded frame: internal holes (regrid only bridges
    short gaps; longer ones drop out of the index) plus the STALE TAIL between the last
    reading and now_local — the 'sensor stopped reporting' case. Returns a list of
    (start, end) naive-local Timestamps, each spanning more than stale_min minutes."""
    if g is None or not len(g):
        return []
    step = pd.Timedelta(minutes=1)
    thresh = pd.Timedelta(minutes=stale_min)
    gaps = []
    deltas = g.index.to_series().diff()
    for ts, d in deltas[deltas > thresh].items():
        gaps.append((ts - d + step, ts - step))
    end = pd.Timestamp(now_local).floor("min")
    if end - g.index[-1] > thresh:
        gaps.append((g.index[-1] + step, end))
    return gaps


def apply_gapfill(g, cfg, now_local, station_df=None, backup_station_df=None):
    """Replace sensor outages with the station->cowl transfer model.

    Detects data holes and a stale tail in the regridded frame, pulls station METAR
    covering them (Iowa Mesonet archive + aviationweather.gov live cache), pushes it
    through the fitted hangar transfer (gapfill.synthesize_cowl) and splices the
    BUFFERED estimate into the frame, marked estimated=True. Synthesized RH gets a
    conservative +gapfill_rh_margin_pct bump near saturation, where the backtest showed
    the reconstruction reads ~5% low (reports/cowl_station_backtest.md).

    LAST RESORT — backup station: when the primary AWOS (airport_icao) also has no data
    over part of the outage (its ASOS feed was down too), and backup_airport_icao is set,
    the spans the primary couldn't cover are retried against that ONE nearby station.
    The backup only supplies ambient T/Td/cloud; the transfer fit (hangar lag, greenhouse,
    solar geometry) is unchanged, so the backup must be close enough that its weather
    stands in for the primary's. Only one backup is attempted.

    The fill can only reach as far as some station has obs — if neither the primary nor
    the backup covers a span it stays empty. That partial coverage is reported, not
    hidden: info['unfilled_minutes'] counts the gap minutes no estimate could cover,
    info['warning'] is set when that exceeds the stale threshold, and info['sources']
    carries each station/source's outcome.

    Returns (frame, info). Never raises: on any failure the original frame comes back
    with info['error'] set, so a broken fallback cannot take the monitor down with it.
    station_df / backup_station_df: pre-fetched history (naive-UTC [T, Td, cloud]) for
    tests/CLI, bypassing the network for the primary / backup respectively.
    """
    info = {"filled_minutes": 0, "unfilled_minutes": 0, "gaps": [], "stale": False,
            "error": None, "warning": None, "source": None, "sources": {},
            "backup_source": None, "backup_filled_minutes": 0}
    out = g.copy()
    out["estimated"] = False
    if not len(g) or not cfg.get("gapfill_enabled", True):
        return out, info
    try:
        stale_min = int(cfg.get("gapfill_stale_min", 90))
        gaps = find_sensor_gaps(g, now_local, stale_min)
        if not gaps:
            return out, info
        info["gaps"] = [(s.isoformat(), e.isoformat()) for s, e in gaps]
        info["stale"] = gaps[-1][1] >= pd.Timestamp(now_local).floor("min")

        import gapfill as gf  # sibling module; hosts put scripts/ on sys.path

        params = gf.load_transfer_params(cfg.get("transfer_params_path") or "")
        if params is None:
            info["error"] = ("no station->cowl transfer params "
                             "(fit one and set transfer_params_path)")
            return out, info

        tz = _local_tz(cfg["timezone"])

        def to_utc(ts):
            return (pd.Timestamp(ts)
                    .tz_localize(tz, ambiguous=True, nonexistent="shift_forward")
                    .tz_convert("UTC").tz_localize(None))

        spin = pd.Timedelta(hours=float(cfg.get("gapfill_spinup_h", 24)))
        rh_margin = float(cfg.get("gapfill_rh_margin_pct", 5.0))

        def _station_estimate(station, prefetched, start_utc, end_utc):
            """Fetch a station's obs and turn them into a local-time, 1-min, RH-nudged
            cowl estimate through the (hangar) transfer. Returns (frame|None, last_ob)."""
            skey = str(station).upper()
            metar = prefetched if prefetched is not None else gf.fetch_station_history(
                station, start_utc, end_utc,
                status=info["sources"].setdefault(skey, {}))
            if metar is None or not len(metar):
                return None, None
            syn = gf.synthesize_cowl(metar, params)      # 10-min frame, naive-UTC index
            syn.index = syn.index.tz_localize("UTC").tz_convert(tz).tz_localize(None)
            syn = syn[~syn.index.duplicated(keep="last")].sort_index()
            syn1 = syn[["Tc", "RH"]].resample("1min").mean().interpolate(limit=15)
            near = syn1["RH"] >= 80.0
            syn1.loc[near, "RH"] = (syn1.loc[near, "RH"] + rh_margin).clip(upper=100.0)
            return syn1, metar.index.max()

        def _splice(frame, syn1, spans):
            """Add syn1's estimate over `spans` into `frame` for minutes not already
            present (real data and earlier fills win). Returns (frame, minutes_added)."""
            if syn1 is None:
                return frame, 0
            parts = [syn1.loc[s:e] for s, e in spans]
            fill = pd.concat(parts).dropna() if parts else syn1.iloc[:0]
            fill = fill[~fill.index.isin(frame.index)]
            if not len(fill):
                return frame, 0
            fill = fill.copy()
            fill["estimated"] = True
            return pd.concat([frame, fill]).sort_index(), int(len(fill))

        # --- primary station over the whole outage ---
        prim, prim_last = _station_estimate(
            cfg["airport_icao"], station_df,
            to_utc(gaps[0][0]) - spin, to_utc(gaps[-1][1]))
        if prim_last is not None:
            info["station_last_ob_utc"] = prim_last.isoformat()
        out, added = _splice(out, prim, gaps)
        info["filled_minutes"] += added

        # --- backup station (last resort) for spans the primary couldn't cover ---
        backup = str(cfg.get("backup_airport_icao") or "").strip()
        remaining = find_sensor_gaps(out, now_local, stale_min)
        if backup and remaining and backup.upper() != str(cfg["airport_icao"]).upper():
            bstart = to_utc(min(s for s, _ in remaining)) - spin
            bend = to_utc(max(e for _, e in remaining))
            bfill, blast = _station_estimate(backup, backup_station_df, bstart, bend)
            out, badded = _splice(out, bfill, remaining)
            if badded:
                info["backup_source"] = backup
                info["backup_filled_minutes"] = badded
                info["filled_minutes"] += badded
                if blast is not None:
                    info["backup_last_ob_utc"] = blast.isoformat()

        if not info["filled_minutes"]:
            info["error"] = ("no station data available for the gap window; sources: "
                             f"{info['sources']}")
            return g.assign(estimated=False), info

        info["source"] = str(cfg["airport_icao"])
        # honest coverage accounting: an outage at every station leaves part of the gap
        # unfillable — surface that instead of implying a complete fill
        want = int(sum((e - s).total_seconds() / 60 + 1 for s, e in gaps))
        info["unfilled_minutes"] = max(0, want - info["filled_minutes"])
        if info["unfilled_minutes"] > stale_min:
            tried = "/".join(info["sources"].keys()) or info["source"]
            info["warning"] = (
                f"station data covered only part of the sensor gap "
                f"({round(info['unfilled_minutes'] / 60, 1)} h unfilled; last primary ob "
                f"{info.get('station_last_ob_utc')}Z; stations tried: {tried})")
        return out, info
    except Exception as err:  # noqa: BLE001 - fallback must never kill the cycle
        info["error"] = f"{type(err).__name__}: {err}"
        return g.assign(estimated=False), info


def gapfill_note(info):
    """One short alert-copy sentence when part of the tally is synthesized, or None."""
    if not info or not info.get("filled_minutes"):
        return None
    h = round(info["filled_minutes"] / 60, 1)
    src = info.get("source") or "station"
    if info.get("backup_source"):
        bh = round(info.get("backup_filled_minutes", 0) / 60, 1)
        src = f"{src} (+ backup {info['backup_source']} {bh} h)"
    tail = " Cowl sensor is currently offline." if info.get("stale") else ""
    if info.get("warning"):
        uh = round(info.get("unfilled_minutes", 0) / 60, 1)
        tail += (f" {uh} h of the outage had no station data either — "
                 f"exposure there is UNKNOWN, not zero.")
    return (f"⚠️ {h} h estimated from {src} via the hangar transfer fit "
            f"(cowl sensor gap).{tail}")


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
    if "estimated" in g.columns:   # carry the gap-fill provenance mask for charts/copy
        series["estimated"] = g["estimated"].astype(bool).values
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
