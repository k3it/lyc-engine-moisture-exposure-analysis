"""
Gap-fill fallback + hangar transfer-function backtest.

When the cowl sensor feed drops, we don't feed raw nearest-station METAR into the
corrosion model: the T-hangar + cowl BUFFER the outside air (thermal inertia +
amplitude damping), AND modulate it via radiation -- a south-facing metal-door hangar
runs WARMER than ambient on sunny days (greenhouse) and the skin radiates to a clear
sky at night (sheltered interior still cools). So the transfer is not a simple lag; it
depends on sky condition. We fit that transfer once (the backtest), then push live
METAR (with sky cover) through it to synthesize a buffered cowl estimate during gaps.

Transported / driving variables:
  - DEW POINT for moisture (roughly conserved; interior tracks exterior with a lag).
  - TEMPERATURE: lagged ambient + a SOLAR-GAIN term (clear-sky solar elevation x
    clear-fraction = greenhouse) + a RADIATIVE-COOLING term (night x clear-fraction).
RH is reconstructed from synthesized T and Td.

Cloud cover: ASOS reports OBSERVED sky condition (skyc1/2/3 = CLR/FEW/SCT/BKN/OVC via
the station ceilometer) -- a measurement, not a forecast. We map it to an effective
cloud fraction. (The smooth "% sky cover" in NOAA point forecasts is a *forecast*
product; for backtesting historical correlation the observed ASOS codes are correct.)
Solar elevation is computed from lat/lon + time -- no external data needed.

Historical METAR/ASOS for the one-time fit (free, Iowa State Mesonet ASOS archive):
  Use mesonet_url(station, start, end) for the EXACT download link (includes sky cover).
  If Claude is blocked from downloading, fetch_metar_archive() raises MetarDownloadBlocked
  carrying that link to hand to the user; the saved CSV is read with load_metar_csv().
Live METAR for production gap-fill:
  https://aviationweather.gov/api/data/metar?ids=KMRB&format=json   (temp_c, dewp_c, clouds[])
"""
from __future__ import annotations
import numpy as np, pandas as pd
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from model import esat_hpa, dewpoint_c, first_order_lag

TAU_CANDIDATES_H = [0.5, 1, 1.5, 2, 3, 4, 6, 8, 12]
KMRB_LAT, KMRB_LON = 39.40, -77.98          # Eastern WV Regional (Martinsburg)

# ASOS sky-condition code -> effective cloud fraction (okta midpoints)
CLOUD_FRAC = {"CLR": 0.0, "SKC": 0.0, "NSC": 0.0, "NCD": 0.0,
              "FEW": 0.19, "SCT": 0.44, "BKN": 0.75, "OVC": 1.0, "VV": 1.0}

# ----------------------------- data fetch ----------------------------------------
MESONET_ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

def _norm_station(station):
    s = station.strip().upper()
    return s[1:] if len(s) == 4 and s.startswith("K") else s

def mesonet_url(station, start, end, data=("tmpf", "dwpf", "skyc1", "skyc2", "skyc3")):
    """EXACT Iowa Mesonet ASOS download URL (comma CSV) for a station and period,
    including observed sky-cover columns. station may be ICAO (KMRB) or 3-char (MRB)."""
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    parts = [f"station={_norm_station(station)}"]
    parts += [f"data={d}" for d in data]
    parts += [
        f"year1={s.year}", f"month1={s.month}", f"day1={s.day}",
        f"year2={e.year}", f"month2={e.month}", f"day2={e.day}",
        "tz=Etc/UTC", "format=onlycomma", "latlon=no", "elev=no",
        "missing=M", "trace=T", "direct=no", "report_type=3", "report_type=4",
    ]
    return MESONET_ASOS + "?" + "&".join(parts)

def _cloud_fraction(df):
    """Max mapped coverage across reported sky layers -> fraction in [0,1]."""
    cols = [c for c in df.columns if c.startswith("skyc")]
    if not cols:
        return None
    frac = np.zeros(len(df))
    for c in cols:
        m = df[c].astype(str).str.upper().str.strip().map(CLOUD_FRAC).fillna(0.0).values
        frac = np.maximum(frac, m)
    return frac

def load_metar_csv(path):
    """Read a Mesonet 'onlycomma' CSV -> DataFrame[T, Td, (cloud)] in C, UTC-indexed."""
    df = pd.read_csv(path, na_values=["M", "T", ""])
    df.columns = [c.strip().lower() for c in df.columns]
    tcol = next(c for c in df.columns if "valid" in c or "time" in c)
    df[tcol] = pd.to_datetime(df[tcol])
    df = df.set_index(tcol)
    out = pd.DataFrame({
        "T": (pd.to_numeric(df["tmpf"], errors="coerce") - 32) * 5/9,
        "Td": (pd.to_numeric(df["dwpf"], errors="coerce") - 32) * 5/9,
    })
    cf = _cloud_fraction(df)
    if cf is not None:
        out["cloud"] = cf
    out = out.dropna(subset=["T", "Td"]).sort_index()
    out.index.name = "valid"
    return out

class MetarDownloadBlocked(RuntimeError):
    """Raised when Claude can't fetch directly; carries the manual download URL."""
    def __init__(self, url):
        self.url = url
        super().__init__(
            "Couldn't download ASOS data directly (network blocked). "
            "Ask the user to open this link in a browser, save the CSV, and provide it; "
            "then load it with load_metar_csv(path):\n  " + url)

def fetch_metar_archive(station, start, end):
    """Try to download historical ASOS (with sky cover) for [start, end]. On ANY network
    failure, raise MetarDownloadBlocked with the exact mesonet_url() link."""
    import urllib.request, io
    url = mesonet_url(station, start, end)
    try:
        raw = urllib.request.urlopen(url, timeout=60).read().decode()
    except Exception:
        raise MetarDownloadBlocked(url)
    if not raw.strip() or "station" not in raw.splitlines()[0].lower():
        raise MetarDownloadBlocked(url)
    return load_metar_csv(io.StringIO(raw))

def parse_live_metar_json(js):
    """aviationweather.gov metar json -> dict with T, Td (C), cloud fraction."""
    r = js[0] if isinstance(js, list) else js
    frac = 0.0
    for layer in r.get("clouds", []) or []:
        frac = max(frac, CLOUD_FRAC.get(str(layer.get("cover", "")).upper(), 0.0))
    return {"time": r.get("reportTime") or r.get("obsTime"),
            "T": r["temp"], "Td": r["dewp"], "cloud": frac}

# ----------------------------- solar geometry ------------------------------------
def solar_elevation_deg(idx_utc, lat=KMRB_LAT, lon=KMRB_LON):
    """Approx solar elevation (deg) from UTC time + lat/lon. No external data."""
    idx = pd.DatetimeIndex(idx_utc)
    doy = idx.dayofyear + (idx.hour + idx.minute/60)/24.0
    decl = 23.45 * np.sin(np.radians(360*(284+doy)/365.0))
    B = np.radians(360*(doy-81)/364.0)
    eot = 9.87*np.sin(2*B) - 7.53*np.cos(B) - 1.5*np.sin(B)          # minutes
    solar_time = (idx.hour + idx.minute/60) + (4*lon + eot)/60.0     # hours
    ha = np.radians(15*(solar_time - 12))                            # hour angle
    el = np.degrees(np.arcsin(
        np.sin(np.radians(lat))*np.sin(np.radians(decl)) +
        np.cos(np.radians(lat))*np.cos(np.radians(decl))*np.cos(ha)))
    return el

def _radiative_features(idx, cloud, lat, lon):
    """Return (solar_gain, radiative_cool) regressors. cloud may be None -> treated clear."""
    el = solar_elevation_deg(idx, lat, lon)
    sun = np.clip(np.sin(np.radians(el)), 0, None)      # clear-sky daytime proxy
    night = (el < 0).astype(float)
    clear = 1.0 - (np.zeros(len(idx)) if cloud is None else np.asarray(cloud))
    return sun * clear, night * clear

# ----------------------------- alignment -----------------------------------------
def _align(cowl, metar, grid="10min"):
    c = cowl.copy()
    if "Td" not in c:
        c["Td"] = dewpoint_c(esat_hpa(c["Tc"].values) * np.clip(c["RH"].values, 1, 100)/100)
    cg = c[["Tc", "Td"]].resample(grid).mean()
    mcols = ["T", "Td"] + (["cloud"] if "cloud" in metar else [])
    mg = metar[mcols].resample(grid).mean().interpolate(limit=12)
    j = cg.join(mg, how="inner").dropna()
    return j

def _grid_dt_s(idx):
    return float(pd.Series(idx).diff().dt.total_seconds().median())

# ----------------------------- the fit -------------------------------------------
def _fit_temperature(j, lat, lon):
    """Tc ~ a*lag(Tm,tau) + g*solar_gain + r*radiative_cool + b. Grid-search tau."""
    dt = _grid_dt_s(j.index)
    cloud = j["cloud"].values if "cloud" in j else None
    sg, rc = _radiative_features(j.index, cloud, lat, lon)
    y = j["Tc"].values
    best = None
    for tau_h in TAU_CANDIDATES_H:
        lag = first_order_lag(j["T"].values, tau_h*3600, dt)
        X = np.column_stack([lag, sg, rc, np.ones_like(lag)])
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ coef
        rmse = float(np.sqrt(np.mean(resid**2)))
        if best is None or rmse < best["rmse"]:
            ss = np.sum(resid**2); tot = np.sum((y-y.mean())**2)
            best = {"tau_h": tau_h, "coef": coef.tolist(), "rmse": rmse,
                    "r2": float(1-ss/tot), "has_cloud": cloud is not None}
    return best

def fit_transfer(cowl, metar, grid="10min", lat=KMRB_LAT, lon=KMRB_LON):
    """Fit station->cowl transfer (temperature w/ solar+cloud, moisture w/ lag).
    Returns params dict -- the hangar inertia/sheltering/greenhouse characterization."""
    # explicit suffixes to avoid Td collision (cowl vs metar)
    c = cowl.copy()
    if "Td" not in c:
        c["Td"] = dewpoint_c(esat_hpa(c["Tc"].values) * np.clip(c["RH"].values, 1, 100)/100)
    cg = c[["Tc", "Td"]].resample(grid).mean().rename(columns={"Td": "Tdc"})
    mcols = ["T", "Td"] + (["cloud"] if "cloud" in metar else [])
    mg = metar[mcols].resample(grid).mean().interpolate(limit=12).rename(columns={"Td": "Tdm"})
    j = cg.join(mg, how="inner").dropna()
    if len(j) < 50:
        raise ValueError(f"Too little overlap to fit ({len(j)} points). Check timeframes/timezones.")

    temp = _fit_temperature(j, lat, lon)

    dt = _grid_dt_s(j.index)
    best_m = None
    for tau_h in TAU_CANDIDATES_H:
        lag = first_order_lag(j["Tdm"].values, tau_h*3600, dt)
        X = np.column_stack([lag, np.ones_like(lag)])
        coef, *_ = np.linalg.lstsq(X, j["Tdc"].values, rcond=None)
        resid = j["Tdc"].values - X @ coef
        rmse = float(np.sqrt(np.mean(resid**2)))
        if best_m is None or rmse < best_m["rmse"]:
            ss = np.sum(resid**2); tot = np.sum((j["Tdc"].values-j["Tdc"].mean())**2)
            best_m = {"tau_h": tau_h, "coef": coef.tolist(), "rmse": rmse,
                      "r2": float(1-ss/tot)}

    tc = temp["coef"]
    return {"grid": grid, "n": int(len(j)), "lat": lat, "lon": lon,
            "temperature": temp, "moisture": best_m,
            "summary": {
                "thermal_lag_h": temp["tau_h"],
                "thermal_damping": round(tc[0], 3),
                "solar_gain_C": round(tc[1], 2),       # greenhouse: warmer by day if clear
                "radiative_cool_C": round(tc[2], 2),   # clear-night cooling (expect <0)
                "temp_rmse_C": round(temp["rmse"], 2), "temp_r2": round(temp["r2"], 3),
                "cloud_used": temp["has_cloud"],
                "moisture_lag_h": best_m["tau_h"], "moisture_damping": round(best_m["coef"][0], 3),
                "dewpt_rmse_C": round(best_m["rmse"], 2)}}

# ----------------------------- synthesis (production gap-fill) --------------------
def synthesize_cowl(metar, params, grid="10min"):
    """Push METAR (with sky cover) through the fitted transfer -> estimated cowl Tc, RH."""
    dt = pd.Timedelta(grid).total_seconds()
    mcols = ["T", "Td"] + (["cloud"] if "cloud" in metar else [])
    mg = metar[mcols].resample(grid).mean().interpolate(limit=12).dropna(subset=["T", "Td"])
    cloud = mg["cloud"].values if "cloud" in mg else None
    sg, rc = _radiative_features(mg.index, cloud, params["lat"], params["lon"])
    tc = params["temperature"]["coef"]
    lagT = first_order_lag(mg["T"].values, params["temperature"]["tau_h"]*3600, dt)
    Tc = tc[0]*lagT + tc[1]*sg + tc[2]*rc + tc[3]
    mc = params["moisture"]["coef"]
    lagTd = first_order_lag(mg["Td"].values, params["moisture"]["tau_h"]*3600, dt)
    Tdc = mc[0]*lagTd + mc[1]
    RH = np.clip(100*esat_hpa(Tdc)/esat_hpa(Tc), 1, 100)
    return pd.DataFrame({"Tc": Tc, "RH": RH, "estimated": True}, index=mg.index)

# ----------------------------- backtest ------------------------------------------
def backtest(cowl, metar, grid="10min", test_frac=0.3, lat=KMRB_LAT, lon=KMRB_LON):
    """Time-split fit/evaluate: fit on the first (1-test_frac), score synthesis on the
    held-out tail. Reports out-of-sample reconstruction error for cowl T and RH."""
    cutoff = cowl.index.min() + (cowl.index.max()-cowl.index.min())*(1-test_frac)
    params = fit_transfer(cowl.loc[:cutoff], metar, grid, lat, lon)
    test_metar = metar.loc[cutoff:]
    syn = synthesize_cowl(test_metar, params, grid)
    truth = cowl[["Tc", "RH"]].resample(grid).mean().reindex(syn.index).dropna()
    m = syn.join(truth, how="inner", lsuffix="_syn", rsuffix="_true").dropna()
    t_rmse = float(np.sqrt(np.mean((m["Tc_syn"]-m["Tc_true"])**2)))
    rh_rmse = float(np.sqrt(np.mean((m["RH_syn"]-m["RH_true"])**2)))
    return {"params": params,
            "oos": {"n": int(len(m)), "temp_rmse_C": round(t_rmse, 2),
                    "rh_rmse_pct": round(rh_rmse, 1),
                    "temp_bias_C": round(float((m["Tc_syn"]-m["Tc_true"]).mean()), 2),
                    "rh_bias_pct": round(float((m["RH_syn"]-m["RH_true"]).mean()), 1)}}

# ----------------------------- estimator self-test (no internet) ------------------
def _self_test_synthetic(cowl):
    """Validate the ESTIMATOR: plant a known transfer (lag+damping+solar gain), build a
    fake station series, confirm fit_transfer recovers the right sign/scale."""
    c = cowl[["Tc", "RH"]].resample("10min").mean().dropna().iloc[:5000].copy()
    c["Td"] = dewpoint_c(esat_hpa(c["Tc"].values)*np.clip(c["RH"].values,1,100)/100)
    sg, rc = _radiative_features(c.index, None, KMRB_LAT, KMRB_LON)
    station_T = c["Tc"].values - 2.0*sg            # strip planted +2C clear-day gain
    daily = pd.Series(station_T, index=c.index).rolling(144, center=True, min_periods=1).mean().values
    station_T = daily + (station_T-daily)/0.6       # un-damp (planted damping 0.6)
    metar = pd.DataFrame({"T": station_T, "Td": c["Td"].values}, index=c.index)
    p = fit_transfer(c, metar, "10min")
    return p["summary"], {"planted_thermal_damping": 0.6, "planted_solar_gain_C": 2.0}

if __name__ == "__main__":
    from model import load_csv
    cowl = load_csv(sys.argv[1])
    got, planted = _self_test_synthetic(cowl)
    print("Estimator self-test (synthetic planted transfer):")
    print("  planted  :", planted)
    print("  recovered:", {k: got[k] for k in
          ["thermal_damping", "solar_gain_C", "thermal_lag_h", "temp_rmse_C"]})
