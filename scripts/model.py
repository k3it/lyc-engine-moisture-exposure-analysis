"""
Engine internal-moisture / cam-corrosion exposure model.

Deterministic physics core. NO language model in the hot path: every number here
is computed from sensor readings (timestamp, temperature, relative humidity).

Two inertias are modeled, both of which matter:
  1. METAL thermal inertia  - the cam/lifter steel lags air temperature (tau_metal).
  2. AIR-EXCHANGE inertia    - the crankcase only breathes through restricted paths
                               (breather tube dominant; intake plugged/filtered;
                               exhaust tortuous), so interior humidity is a heavily
                               low-pass-filtered version of ambient (tau_air).

Condensation onto the cam occurs when the interior water-vapour pressure exceeds
the saturation pressure at the (lagged) metal temperature. Wet metal -> corrosion;
the standard metric is TIME-OF-WETNESS (hours the surface holds a film), not grams.

See references/METHODOLOGY.md for the derivation, constants, and caveats.
"""
from __future__ import annotations
import numpy as np, pandas as pd
from dataclasses import dataclass, asdict

# ----------------------------- constants / defaults -----------------------------
MAGNUS_A, MAGNUS_B, ES0 = 17.625, 243.04, 6.112      # Magnus coeffs; es0 in hPa
TAU_METAL_S   = 8 * 3600     # cam/lifter thermal time constant (buried, conservative)
TAU_AIR_S     = 24 * 3600    # interior air-exchange time constant (breather-dominated)
HM            = 0.003        # mass-transfer coefficient, enclosed nat. convection (m/s)
FILM_CAP_GM2  = 15.0         # max film before gravity drainage off lobes (g/m^2)
WET_AREA_M2   = 0.30         # internal wetted steel (cam+lifters+lower case) (m^2)
CRANKCASE_V   = 0.010        # crankcase free-gas volume (m^3)
FLIGHT_TEMP_C = 40.0         # cowl-air temp above this => engine was run (resets clock)
RICH_H2O_FRAC = 0.15         # rich-shutdown exhaust water fraction (vol) - informational

# ----------------------------- psychrometrics -----------------------------------
def esat_hpa(t_c):
    """Saturation vapour pressure (hPa) over water, Magnus."""
    return ES0 * np.exp(MAGNUS_A * t_c / (MAGNUS_B + t_c))

def vapour_pressure_hpa(t_c, rh_pct):
    return (np.clip(rh_pct, 0, 100) / 100.0) * esat_hpa(t_c)

def dewpoint_c(e_hpa):
    e = np.clip(e_hpa, 1e-6, None)
    l = np.log(e / ES0)
    return MAGNUS_B * l / (MAGNUS_A - l)

def abs_humidity_gm3(t_c, rh_pct):
    """Absolute humidity (g/m^3)."""
    return esat_hpa(t_c) * (np.clip(rh_pct, 0, 100) / 100.0) * 100.0 * 2.1674 / (273.15 + t_c)

# ----------------------------- core filters --------------------------------------
def first_order_lag(x, tau_s, dt_s=60.0):
    """First-order low-pass. Returns x unchanged if tau<=0."""
    x = np.asarray(x, float)
    if tau_s <= 0:
        return x.copy()
    y = np.empty_like(x); y[0] = x[0]; k = dt_s / tau_s
    for i in range(1, len(x)):
        y[i] = y[i-1] + k * (x[i-1] - y[i-1])
    return y

# ----------------------------- data loading --------------------------------------
def load_csv(path):
    """Load an X-Sense / thermo-hygrometer CSV export.
    Expected columns (order-insensitive): time, temperature (F), relative humidity (%).
    """
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    tcol = next(c for c in df.columns if "time" in c)
    fcol = next(c for c in df.columns if "fahrenheit" in c or c.startswith("temp"))
    hcol = next(c for c in df.columns if "humid" in c or "rh" in c)
    out = pd.DataFrame({
        "time": pd.to_datetime(df[tcol]),
        "Tc": (pd.to_numeric(df[fcol], errors="coerce") - 32) * 5/9,
        "RH": pd.to_numeric(df[hcol], errors="coerce"),
    }).dropna().sort_values("time").set_index("time")
    return out

def from_records(records):
    """Build the frame from live readings: list of dicts with keys
    time (ISO str or datetime), and EITHER tc OR tf, plus rh."""
    rows = []
    for r in records:
        tc = r["tc"] if "tc" in r else (r["tf"] - 32) * 5/9
        rows.append({"time": pd.to_datetime(r["time"]), "Tc": tc, "RH": r["rh"]})
    return pd.DataFrame(rows).dropna().sort_values("time").set_index("time")

def regrid(df, max_gap_min=60):
    """Resample to a clean 1-minute grid; interpolate only across short gaps."""
    g = df[["Tc", "RH"]].resample("1min").mean().interpolate(limit=max_gap_min).dropna()
    return g

# ----------------------------- the analysis --------------------------------------
@dataclass
class Params:
    tau_metal_s: float = TAU_METAL_S
    tau_air_s:   float = TAU_AIR_S
    hm:          float = HM
    film_cap:    float = FILM_CAP_GM2
    wet_area_m2: float = WET_AREA_M2
    flight_temp_c: float = FLIGHT_TEMP_C

def analyze(g: pd.DataFrame, p: Params = Params()):
    """Run the full model on a regridded frame. Returns a dict of results plus
    per-minute series (as a DataFrame) for charting."""
    T  = g["Tc"].values
    RH = np.clip(g["RH"].values, 1, 100)
    dt = 60.0

    # metal temperature (thermal inertia)
    Tm = first_order_lag(T, p.tau_metal_s, dt)
    # interior vapour pressure (air-exchange inertia on the transported quantity)
    e_ext = vapour_pressure_hpa(T, RH)
    e_int = first_order_lag(e_ext, p.tau_air_s, dt)
    e_int_inst = e_ext                     # tau_air = 0 upper bound ("how damp ambient is")

    es_m = esat_hpa(Tm)                     # saturation vp at metal temp
    wet_real = e_int > es_m                 # realistic: interior moisture reaches cam
    wet_ub   = e_int_inst > es_m            # upper bound: instant air exchange

    # film mass balance (per unit area) using realistic interior vapour
    ah_int = e_int / esat_hpa(T) * abs_humidity_gm3(T, 100.0)   # interior abs hum (g/m^3)
    ah_sat_m = abs_humidity_gm3(Tm, 100.0)
    drive = ah_int - ah_sat_m               # >0 condense, <0 evaporate (g/m^3)
    flux = p.hm * drive * dt                # g/m^2 per minute (signed)
    film = np.zeros_like(T)
    cond_mass_gm2 = 0.0
    for i in range(1, len(T)):
        film[i] = min(p.film_cap, max(0.0, film[i-1] + flux[i]))
        if flux[i] > 0:
            cond_mass_gm2 += flux[i]
    film_present = film > 0.1

    # flight / run detection -> reset points
    ran = T > p.flight_temp_c
    flight_starts = g.index[ran & ~np.roll(ran, 1)]

    res = {
        "span": (g.index.min().isoformat(), g.index.max().isoformat()),
        "minutes": int(len(g)),
        "sub_dew_hours_realistic": float(wet_real.sum() / 60),
        "sub_dew_hours_upper_bound": float(wet_ub.sum() / 60),
        "film_hours": float(film_present.sum() / 60),
        "tow_pct_realistic": float(wet_real.mean() * 100),
        "condensed_mass_g": float(cond_mass_gm2 * p.wet_area_m2),  # honest, small
        "peak_film_gm2": float(film.max()),
        "flight_count": int(len(flight_starts)),
        "last_flight": flight_starts[-1].isoformat() if len(flight_starts) else None,
        "params": asdict(p),
    }

    series = pd.DataFrame({
        "Tc": T, "Tm": Tm, "RH": RH,
        "Td_ext": dewpoint_c(e_ext), "Td_int": dewpoint_c(e_int),
        "film": film, "wet_real": wet_real, "wet_ub": wet_ub,
    }, index=g.index)
    return res, series

def episodes(series, min_hours=0.5, min_rh=60):
    """List discrete wet (film-present) episodes with persistence breakdown."""
    wf = series["film"] > 0.1
    grp = (wf != wf.shift()).cumsum()
    out = []
    for _, idx in wf[wf].groupby(grp[wf]).groups.items():
        seg = series.loc[idx[0]:idx[-1]]
        total_h = (idx[-1] - idx[0]).total_seconds() / 3600
        sub_h = (seg["Tm"] < seg["Td_int"]).sum() / 60
        if total_h >= min_hours and seg["RH"].mean() >= min_rh:
            out.append({
                "start": idx[0].isoformat(),
                "total_h": round(total_h, 1),
                "subdew_h": round(sub_h, 1),
                "tail_h": round(total_h - sub_h, 1),
                "rh_mean": round(float(seg["RH"].mean())),
                "peak_film_gm2": round(float(seg["film"].max()), 1),
            })
    return out

def since_last_flight(series, res):
    """Cumulative exposure since the last detected hot run - the monitoring headline."""
    lf = res["last_flight"]
    s = series.loc[lf:] if lf else series
    wet_real = s["wet_real"].sum() / 60
    film_h = (s["film"] > 0.1).sum() / 60
    # 'ambient dampness' upper-bound index drives the social nudge; realistic drives honesty
    return {
        "since": lf,
        "days": round((s.index[-1] - s.index[0]).total_seconds() / 86400, 1),
        "wet_hours_realistic": round(float(wet_real), 1),
        "ambient_damp_hours_ub": round(float(s["wet_ub"].sum() / 60), 1),
        "film_hours": round(float(film_h), 1),
        "latest": {"time": s.index[-1].isoformat(),
                   "Tc": round(float(s["Tc"].iloc[-1]), 1),
                   "RH": round(float(s["RH"].iloc[-1]))},
    }

def combustion_water_per_shutdown_g(volume_l=5.24, shutdown_c=90, cool_c=10):
    """Informational: one-shot trapped combustion water that can condense on cooldown."""
    R, P = 8.314, 101325.0
    n_tot = P * (volume_l/1000) / (R * (273.15 + shutdown_c))
    n_h2o = n_tot * RICH_H2O_FRAC
    water_g = n_h2o * 18.0
    # vapour that can remain at cool_c in same volume
    e = esat_hpa(cool_c) * 100
    n_vap = e * (volume_l/1000) / (R * (273.15 + cool_c))
    return max(0.0, water_g - n_vap * 18.0)

# ----------------------------- CLI ----------------------------------------------
if __name__ == "__main__":
    import argparse, json, sys
    ap = argparse.ArgumentParser(description="Engine cam-moisture exposure model")
    ap.add_argument("csv", help="X-Sense CSV export (time, temp F, RH %)")
    ap.add_argument("--tau-metal-h", type=float, default=TAU_METAL_S/3600)
    ap.add_argument("--tau-air-h", type=float, default=TAU_AIR_S/3600)
    ap.add_argument("--json", action="store_true", help="emit JSON only")
    a = ap.parse_args()
    p = Params(tau_metal_s=a.tau_metal_h*3600, tau_air_s=a.tau_air_h*3600)
    g = regrid(load_csv(a.csv))
    res, series = analyze(g, p)
    res["episodes"] = episodes(series)
    res["since_last_flight"] = since_last_flight(series, res)
    res["combustion_water_per_shutdown_g"] = round(combustion_water_per_shutdown_g(), 2)
    if a.json:
        print(json.dumps(res, indent=2)); sys.exit(0)
    print(f"Span {res['span'][0]} -> {res['span'][1]}  ({res['minutes']} min)")
    print(f"Sub-dew-point hours : realistic {res['sub_dew_hours_realistic']:.1f}"
          f"  |  upper bound {res['sub_dew_hours_upper_bound']:.1f}")
    print(f"Film-present hours  : {res['film_hours']:.1f}")
    print(f"Condensed mass est. : {res['condensed_mass_g']:.2f} g  (honestly small)")
    print(f"Flights detected    : {res['flight_count']}  last {res['last_flight']}")
    slf = res["since_last_flight"]
    print(f"Since last flight   : {slf['days']} d, realistic wet {slf['wet_hours_realistic']} h, "
          f"ambient-damp {slf['ambient_damp_hours_ub']} h")
    print(f"Episodes (>=0.5h)   : {len(res['episodes'])}")
