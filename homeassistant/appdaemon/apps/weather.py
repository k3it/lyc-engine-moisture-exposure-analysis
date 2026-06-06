"""
Aviation weather lookahead for the "want to go flying?" nudge.

Two sources, both free and keyless:
  * TAF (aviationweather.gov) - terminal forecast, ~24-30 h, authoritative for the
    near term (wind/gust, visibility, ceiling -> VFR or not).
  * NWS hourly forecast (api.weather.gov) - several days out, used to spot a calm,
    fair-weather window on an upcoming weekend afternoon.

The job here is not a flight briefing - it is to answer "is there an obviously nice,
calm, VFR window in the next few days, ideally a weekend afternoon?" and to hand back
a short human summary the message-composer (Gemini) can weave in.

No AppDaemon / Home Assistant dependency: importable and runnable standalone
    python weather.py KMRB 39.40 -77.98
so it can be tested before it ever runs inside HA.
"""
from __future__ import annotations
import json, re, datetime as dt
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - very old python
    ZoneInfo = None

# --- VFR / "nice day" thresholds (conservative; this is a nudge, not a clearance) ---
VFR_CEILING_FT = 3000      # broken/overcast base at/above this counts as VFR ceiling
VFR_VIS_SM     = 5.0       # statute miles
CALM_WIND_KT   = 12        # sustained wind at/below this is "calm enough" to enjoy
CALM_GUST_KT   = 16        # gusts at/below this
GOOD_POP_PCT   = 25        # max probability-of-precip for a "go" window
DAY_START_H    = 9         # local hours we consider "flyable daylight"
DAY_END_H      = 18
AFTERNOON      = range(12, 18)

_UA = "lyc-moisture-monitor (github.com/k3it/lyc-engine-moisture-exposure-analysis)"
_GOOD_SKY = ("clear", "sunny", "fair", "mostly clear", "mostly sunny",
             "partly cloudy", "partly sunny", "few clouds")
_BAD_SKY  = ("rain", "shower", "thunder", "tstorm", "storm", "fog", "mist",
             "drizzle", "snow", "sleet", "ice", "freezing", "haze", "smoke")


# ----------------------------- http helper --------------------------------------
def _get_json(url, timeout=15):
    req = Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _local(tz_name):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    return dt.timezone.utc


# ----------------------------- TAF ----------------------------------------------
def fetch_taf(icao, timeout=15):
    """Return the parsed TAF JSON for one station, or None on failure."""
    url = f"https://aviationweather.gov/api/data/taf?ids={icao}&format=json"
    try:
        data = _get_json(url, timeout)
        return data[0] if isinstance(data, list) and data else (data or None)
    except (URLError, HTTPError, ValueError, IndexError, KeyError):
        return None


def _cloud_ceiling_ft(clouds):
    """Lowest broken/overcast base in feet, or None (treat as unlimited)."""
    if not clouds:
        return None
    bases = []
    for c in clouds:
        cover = (c.get("cover") or "").upper()
        base = c.get("base")
        if cover in ("BKN", "OVC", "VV") and base is not None:
            bases.append(float(base))
    return min(bases) if bases else None


def taf_summary(taf):
    """One-line near-term VFR/calm read from the TAF, e.g. 'TAF: VFR, calm'."""
    if not taf:
        return "TAF unavailable"
    fcsts = taf.get("fcsts") or taf.get("forecast") or []
    vfr = calm = True
    worst_vis = 99.0
    max_wind = 0
    for f in fcsts[:4]:  # first few forecast groups ~ next ~12-18 h
        vis = f.get("visib")
        if isinstance(vis, str):
            vis = 6.0 if "6+" in vis or "P6" in vis else _num(vis)
        if vis is not None:
            worst_vis = min(worst_vis, float(vis))
        ceil = _cloud_ceiling_ft(f.get("clouds"))
        wspd = _num(f.get("wspd")) or 0
        wgst = _num(f.get("wgst")) or 0
        max_wind = max(max_wind, wspd, wgst)
        if (ceil is not None and ceil < VFR_CEILING_FT) or (vis is not None and float(vis) < VFR_VIS_SM):
            vfr = False
        if wspd > CALM_WIND_KT or wgst > CALM_GUST_KT:
            calm = False
    tag = "VFR" if vfr else "not VFR"
    wind = "calm" if calm else f"breezy (~{int(max_wind)} kt)"
    return f"TAF: {tag}, {wind}"


# ----------------------------- NWS multi-day ------------------------------------
def fetch_nws_hourly(lat, lon, timeout=15):
    """Return NWS hourly forecast periods (list) for a lat/lon, or [] on failure."""
    try:
        pts = _get_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", timeout)
        url = pts["properties"]["forecastHourly"]
        fc = _get_json(url, timeout)
        return fc["properties"]["periods"]
    except (URLError, HTTPError, ValueError, KeyError, TypeError):
        return []


def _num(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"-?\d+(\.\d+)?", str(s))
    return float(m.group()) if m else None


def _hour_is_good(p):
    """Is a single NWS hourly period calm + fair + dry?"""
    wind = _num(p.get("windSpeed")) or 0          # mph
    pop = p.get("probabilityOfPrecipitation") or {}
    pop_v = pop.get("value") if isinstance(pop, dict) else _num(pop)
    pop_v = 0 if pop_v is None else pop_v
    short = (p.get("shortForecast") or "").lower()
    bad = any(b in short for b in _BAD_SKY)
    good_sky = any(g in short for g in _GOOD_SKY)
    calm = wind <= CALM_WIND_KT * 1.15            # mph vs kt, generous
    return (not bad) and good_sky and calm and pop_v <= GOOD_POP_PCT


def find_windows(periods, tz_name, horizon_days, now=None):
    """Group consecutive good daytime hours into candidate flying windows."""
    tz = _local(tz_name)
    now = now or dt.datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    horizon = now + dt.timedelta(days=horizon_days)
    windows, cur = [], None
    for p in periods:
        try:
            t = dt.datetime.fromisoformat(p["startTime"]).astimezone(tz)
        except Exception:
            continue
        if t < now or t > horizon:
            continue
        daytime = DAY_START_H <= t.hour < DAY_END_H
        good = daytime and _hour_is_good(p)
        if good:
            if cur is None:
                cur = {"start": t, "end": t + dt.timedelta(hours=1)}
            else:
                cur["end"] = t + dt.timedelta(hours=1)
        elif cur is not None:
            windows.append(cur); cur = None
    if cur is not None:
        windows.append(cur)
    # keep windows of at least 2 h
    return [w for w in windows if (w["end"] - w["start"]) >= dt.timedelta(hours=2)]


def _score(w):
    """Higher = nicer. Favor weekends and afternoons and longer windows."""
    start = w["start"]
    hours = (w["end"] - start).total_seconds() / 3600
    score = min(hours, 6)                       # up to +6 for duration
    if start.weekday() >= 5:                    # Sat/Sun
        score += 4
    if start.weekday() == 4:                    # Friday gets a small nudge
        score += 1
    if any(h in AFTERNOON for h in range(start.hour, min(w["end"].hour or 24, 24))):
        score += 2
    return score


def _phrase(w):
    """e.g. 'Sat afternoon'. Prefers 'afternoon' when the window covers it,
    since that's the prime flying slot the nudge is aiming at."""
    s, e = w["start"], w["end"]
    dow = s.strftime("%a")
    covers = set(range(s.hour, e.hour))
    if covers & set(AFTERNOON):
        part = "afternoon"
    elif e.hour <= 12:
        part = "morning"
    elif s.hour >= 17:
        part = "evening"
    else:
        part = "midday"
    return f"{dow} {part}"


def daily_digest(periods, tz_name, days, now=None):
    """Per-day daytime summary for the LLM to reason about flying windows.
    NWS hourly only spans a few days but goes well past the ~24 h TAF horizon."""
    tz = _local(tz_name)
    now = now or dt.datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    horizon = now + dt.timedelta(days=days)
    byday = {}
    for p in periods:
        try:
            t = dt.datetime.fromisoformat(p["startTime"]).astimezone(tz)
        except Exception:
            continue
        if t < now or t > horizon or not (DAY_START_H <= t.hour < DAY_END_H):
            continue
        wind_mph = _num(p.get("windSpeed")) or 0
        wind_kt = wind_mph * 0.869
        pop = p.get("probabilityOfPrecipitation") or {}
        pop_v = pop.get("value") if isinstance(pop, dict) else _num(pop)
        d = byday.setdefault(t.date(), {"winds": [], "pop": 0, "skies": []})
        d["winds"].append(wind_kt)
        d["pop"] = max(d["pop"], pop_v or 0)
        d["skies"].append((p.get("shortForecast") or "").strip())
    out = []
    for date in sorted(byday)[:days]:
        d = byday[date]
        wlo, whi = (min(d["winds"]), max(d["winds"])) if d["winds"] else (0, 0)
        # most common short-forecast phrase of the day
        sky = max(set(d["skies"]), key=d["skies"].count) if d["skies"] else "?"
        out.append({
            "day": (date.strftime("%a %b ") + str(date.day)) if hasattr(date, "strftime") else str(date),
            "wind_kt": f"{round(wlo)}-{round(whi)}",
            "max_pop_pct": round(d["pop"]),
            "sky": sky,
        })
    return out


# ----------------------------- top-level assessment -----------------------------
def assess(icao, lat, lon, tz_name="America/New_York", horizon_days=4, now=None):
    """Return {best_window, windows, summary, taf} for the message-composer.

    best_window is None when nothing calm+VFR shows up in the horizon.
    summary is a short human string, e.g.
        'TAF: VFR, calm. Calm VFR windows: Fri afternoon, Sat afternoon, Sun afternoon.'
    """
    taf = fetch_taf(icao)
    periods = fetch_nws_hourly(lat, lon)
    windows = find_windows(periods, tz_name, horizon_days, now=now)
    windows.sort(key=_score, reverse=True)
    daily = daily_digest(periods, tz_name, horizon_days, now=now)

    taf_s = taf_summary(taf)
    # A compact, source-of-truth forecast brief the LLM reasons over (TAF is only
    # ~24 h; the daily lines extend the picture). Includes raw TAF text if present.
    raw_taf = (taf or {}).get("rawTAF") or (taf or {}).get("raw_text") or ""
    brief_lines = [f"{d['day']}: wind {d['wind_kt']} kt, {d['sky']}, precip {d['max_pop_pct']}%"
                   for d in daily]
    forecast_brief = (f"{taf_s}\nNear-term TAF: {raw_taf}\n"
                      f"Daytime outlook (KMRB area):\n  " + "\n  ".join(brief_lines)
                      if brief_lines else taf_s)
    if windows:
        # de-dup phrases in chronological order for the summary
        chron = sorted(windows, key=lambda w: w["start"])
        seen, phrases = set(), []
        for w in chron:
            ph = _phrase(w)
            if ph not in seen:
                seen.add(ph); phrases.append(ph)
        summary = f"{taf_s}. Calm VFR windows: " + ", ".join(phrases[:5]) + "."
        best = windows[0]
        best_window = {
            "start": best["start"].isoformat(),
            "end": best["end"].isoformat(),
            "phrase": _phrase(best),
            "is_weekend": best["start"].weekday() >= 5,
            "hours": round((best["end"] - best["start"]).total_seconds() / 3600, 1),
        }
    else:
        summary = f"{taf_s}. No clearly calm VFR window found in the next {horizon_days} days."
        best_window = None

    return {
        "best_window": best_window,
        "windows": [
            {"start": w["start"].isoformat(), "end": w["end"].isoformat(),
             "phrase": _phrase(w)}
            for w in sorted(windows, key=lambda w: w["start"])
        ],
        "summary": summary,
        "taf": taf_s,
        "daily": daily,
        "forecast_brief": forecast_brief,
    }


if __name__ == "__main__":
    import sys
    icao = sys.argv[1] if len(sys.argv) > 1 else "KMRB"
    lat = float(sys.argv[2]) if len(sys.argv) > 2 else 39.40
    lon = float(sys.argv[3]) if len(sys.argv) > 3 else -77.98
    out = assess(icao, lat, lon)
    print(json.dumps(out, indent=2))
