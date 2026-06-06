"""
Engine internal-moisture / cam-corrosion exposure model.

Deterministic physics core. NO language model in the hot path: every number here
is computed from sensor readings (timestamp, temperature, relative humidity).

Two inertias are modeled, both of which matter:
  1. METAL thermal inertia  - the cam/lifter steel lags air temperature (tau_metal).
  2. AIR-EXCHANGE inertia    - the crankcase breathes through restricted paths, so
                               interior humidity lags ambient. This is modeled as TWO
                               PARALLEL PATHS, not one lag:
                                 * slow BULK   (tau_bulk ~ days): diffusion + mean
                                   thermal breathing; always on.
                                 * fast EVENT  (tau_event ~ 1-2 h): wind/barometric
                                   flushing of the open breather when a moist air mass
                                   moves in. Gated on RISING ambient vapour pressure
                                   (moist air arriving) - the condition under which a
                                   cold cam meets fresh humid air and dews up. A single
                                   lag cannot represent both timescales; the event path
                                   is what lets the model SEE a frontal condensation
                                   event instead of averaging it away.

Condensation onto the cam occurs when the interior water-vapour pressure exceeds
the saturation pressure at the (lagged) metal temperature. Wet metal -> corrosion;
the standard metric is TIME-OF-WETNESS (hours the surface holds a film), not grams.
Drying is ASYMMETRIC: water that drains off a lobe toward the oil (immiscible, denser)
re-evaporates far slower than it condensed, so the film budget evaporates at
dry_factor x the condensation rate.

See references/METHODOLOGY.md for the derivation, constants, and caveats.
"""
from __future__ import annotations
import numpy as np, pandas as pd
from dataclasses import dataclass, asdict

# ----------------------------- constants / defaults -----------------------------
MAGNUS_A, MAGNUS_B, ES0 = 17.625, 243.04, 6.112      # Magnus coeffs; es0 in hPa
TAU_METAL_S   = 8 * 3600     # cam/lifter thermal time constant (buried, conservative)
# --- two-path interior air exchange (replaces the single tau_air low-pass) ---
TAU_BULK_S    = 24 * 3600    # slow path: diffusion + mean thermal breathing (~1 day floor)
TAU_EVENT_S   = 1.5 * 3600   # fast path: frontal/wind breather flush (hours)
EVENT_REF_HPA_H = 1.0        # rising-vapour rate that fully opens the event path (hPa/h)
TAU_AIR_S     = 24 * 3600    # LEGACY single-tau low-pass (only if Params.two_path=False)
DRY_FACTOR    = 0.3          # film evaporation rate / condensation rate (<1, asymmetric)
HM            = 0.003        # mass-transfer coefficient, enclosed nat. convection (m/s)
FILM_CAP_GM2  = 15.0         # max film before gravity drainage off lobes (g/m^2)
WET_AREA_M2   = 0.30         # internal wetted steel (cam+lifters+lower case) (m^2)
CRANKCASE_V   = 0.010        # crankcase free-gas volume (m^3)
FLIGHT_TEMP_C = 40.0         # cowl-air temp above this => engine was run (resets clock)
# A run also shows as a RAPID cowl-air rise (engine heat), which catches shorter/cooler
# runs that never reach FLIGHT_TEMP_C. Solar heating creeps ~0.2 C/10 min; a run slams
# the cowl up many degrees in minutes, so a rise gate separates the two cleanly.
FLIGHT_RISE_C          = 8.0  # cowl rise over the window that signals a run
FLIGHT_RISE_WINDOW_MIN = 10   # minutes over which the rise is measured
FLIGHT_RUN_FLOOR_C     = 32.0 # the run's PEAK must clear this (else it's not a hot run)
FLIGHT_PEAK_WINDOW_MIN = 60   # the peak may come up to this long after the rise onset
FLIGHT_DEBOUNCE_H      = 6.0  # collapse run-start edges within this into one flight
RICH_H2O_FRAC = 0.15         # rich-shutdown exhaust water fraction (vol) - informational
# Conditional grounding caution (improves on Lycoming's blanket 'fly monthly'):
FLIGHT_LIMIT_D  = 30         # days grounded that count as 'a month on the ground'
WET_CAUTION_H   = 8.0        # time-of-wetness since last flight that makes grounding matter

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

def two_path_eint(e_ext, dt_s=60.0, tau_bulk_s=TAU_BULK_S, tau_event_s=TAU_EVENT_S,
                  event_ref_hpa_h=EVENT_REF_HPA_H, smooth_s=1800.0):
    """Interior vapour pressure via TWO parallel ingress paths (see module header).

    A slow bulk path (always on) plus a fast event path that opens only while ambient
    vapour pressure is RISING - i.e. a moist air mass is moving in, the moment a cold
    cam is exposed to fresh humid air. The gate is the rising-vapour rate normalized by
    event_ref; it is 0 when ambient is drying (no fast ingress) and saturates at 1
    during a strong moist front.

        g(t)   = clip( (d e_ext/dt)_+ / event_ref , 0, 1 )
        k(t)   = dt/tau_bulk + (dt/tau_event) * g(t)
        e_int += k(t) * (e_ext_prev - e_int)

    Reduces to the slow bulk lag when nothing is changing; approaches instant ingress
    during a front. The d/dt is taken on a lightly smoothed e_ext to suppress sensor
    jitter. Returns e_int (hPa), same length as e_ext.
    """
    e = np.asarray(e_ext, float)
    de = np.gradient(first_order_lag(e, smooth_s, dt_s)) * (3600.0 / dt_s)  # hPa/h
    gate = np.clip(de / event_ref_hpa_h, 0.0, 1.0)
    k_b, k_e = dt_s / tau_bulk_s, dt_s / tau_event_s
    out = np.empty_like(e); out[0] = e[0]
    for i in range(1, len(e)):
        k = min(1.0, k_b + k_e * gate[i])      # clamp for numerical stability
        out[i] = out[i-1] + k * (e[i-1] - out[i-1])
    return out

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
    tau_bulk_s:  float = TAU_BULK_S        # two-path: slow bulk ingress (days)
    tau_event_s: float = TAU_EVENT_S       # two-path: fast frontal-flush ingress (hours)
    event_ref_hpa_h: float = EVENT_REF_HPA_H
    dry_factor:  float = DRY_FACTOR        # asymmetric film drying (<1)
    hm:          float = HM
    film_cap:    float = FILM_CAP_GM2
    wet_area_m2: float = WET_AREA_M2
    flight_temp_c: float = FLIGHT_TEMP_C
    flight_rise_c: float = FLIGHT_RISE_C            # rapid-rise run detection
    flight_rise_window_min: int = FLIGHT_RISE_WINDOW_MIN
    flight_run_floor_c: float = FLIGHT_RUN_FLOOR_C
    flight_peak_window_min: int = FLIGHT_PEAK_WINDOW_MIN
    flight_debounce_h: float = FLIGHT_DEBOUNCE_H
    two_path:    bool = True               # False -> legacy single-tau low-pass
    tau_air_s:   float = TAU_AIR_S         # legacy single-tau (used iff two_path=False)

def detect_flight_starts(g, p: "Params"):
    """Return run-start timestamps. A run is flagged when cowl air is either above
    flight_temp_c (absolute) OR rises faster than flight_rise_c over the rise window
    while above flight_run_floor_c (the engine-heat signature). Edges within
    flight_debounce_h are collapsed so one run counts once. Assumes a 1-min grid."""
    T = g["Tc"].values
    idx = g.index
    n = len(T)
    if n == 0:
        return []
    w = max(1, int(p.flight_rise_window_min))
    rise = np.zeros(n)
    if n > w:
        rise[w:] = T[w:] - T[:-w]          # cowl rise over the window (deg C / window)
    # the run's PEAK can arrive after the steep rise, so gate the rise on the
    # forward-looking max temperature clearing the floor (not the instantaneous T).
    sT = pd.Series(T, index=idx)
    peak_ahead = sT[::-1].rolling(f"{int(p.flight_peak_window_min)}min",
                                  min_periods=1).max()[::-1].values
    hot = T > p.flight_temp_c
    rapid = (rise > p.flight_rise_c) & (peak_ahead > p.flight_run_floor_c)
    flag = hot | rapid
    prev = np.concatenate(([False], flag[:-1]))
    edges = idx[flag & ~prev]
    deb = pd.Timedelta(hours=p.flight_debounce_h)
    out, last = [], None
    for ts in edges:
        if last is None or (ts - last) >= deb:
            out.append(ts); last = ts
    return out

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
    if p.two_path:
        e_int = two_path_eint(e_ext, dt, p.tau_bulk_s, p.tau_event_s, p.event_ref_hpa_h)
    else:
        e_int = first_order_lag(e_ext, p.tau_air_s, dt)
    e_int_inst = e_ext                     # instant ingress upper bound ("how damp ambient is")

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
        # asymmetric drying: water drained toward the oil evaporates slower than it condensed
        f = flux[i] if flux[i] >= 0 else flux[i] * p.dry_factor
        film[i] = min(p.film_cap, max(0.0, film[i-1] + f))
        if flux[i] > 0:
            cond_mass_gm2 += flux[i]
    film_present = film > 0.1

    # flight / run detection -> reset points (absolute hot OR rapid engine-driven rise)
    flight_starts = detect_flight_starts(g, p)

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
        "flights": [ts.isoformat() for ts in flight_starts],   # all run-start points
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

def grounding_caution(slf, wet_caution_h=WET_CAUTION_H, flight_limit_d=FLIGHT_LIMIT_D):
    """A condensation-aware improvement on Lycoming's blanket 'fly at least monthly'.

    Lycoming's rule is a worst-case, geography-blind guideline written for pilots with
    no condensation data: it assumes any month grounded is risky. With actual exposure
    tracked we can do better - a quiet month in a dry spell does NOT warrant a nag. The
    caution fires only when a month on the ground FOLLOWED real wetting:

        caution  <=>  (time-of-wetness since last flight >= wet_caution_h)
                 AND  (days since last flight       >= flight_limit_d)

    This is the 'fly smarter, not just more often' case: a timely flight right after a
    condensation spell purges the water before it sits. (Oil-acid aging is a separate
    calendar matter and is left to the pilot's own oil-change schedule - not modeled.)

    slf: the since_last_flight() result.
    """
    days = slf.get("days")
    wet  = slf.get("film_hours")            # time-of-wetness since last flight
    grounded = days is not None and days >= flight_limit_d
    wetted   = wet is not None and wet >= wet_caution_h
    fire = bool(grounded and wetted)
    return {
        "caution": fire,
        "days_grounded": days,
        "wet_hours_since_flight": wet,
        "flight_limit_days": flight_limit_d,
        "wet_caution_hours": wet_caution_h,
        "reason": (f"{wet:.1f} h of wetting then {days:.0f} d grounded "
                   f"(no purge flight)") if fire else None,
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
    ap.add_argument("--tau-bulk-h", type=float, default=TAU_BULK_S/3600,
                    help="slow bulk-ingress time constant (hours)")
    ap.add_argument("--tau-event-h", type=float, default=TAU_EVENT_S/3600,
                    help="fast frontal-flush ingress time constant (hours)")
    ap.add_argument("--dry-factor", type=float, default=DRY_FACTOR,
                    help="film evaporation/condensation rate ratio (<1)")
    ap.add_argument("--single-tau-h", type=float, default=None,
                    help="use the legacy single-tau low-pass instead of two-path")
    ap.add_argument("--json", action="store_true", help="emit JSON only")
    a = ap.parse_args()
    if a.single_tau_h is not None:
        p = Params(tau_metal_s=a.tau_metal_h*3600, two_path=False,
                   tau_air_s=a.single_tau_h*3600, dry_factor=a.dry_factor)
    else:
        p = Params(tau_metal_s=a.tau_metal_h*3600, tau_bulk_s=a.tau_bulk_h*3600,
                   tau_event_s=a.tau_event_h*3600, dry_factor=a.dry_factor)
    g = regrid(load_csv(a.csv))
    res, series = analyze(g, p)
    res["episodes"] = episodes(series)
    res["since_last_flight"] = since_last_flight(series, res)
    res["grounding_caution"] = grounding_caution(res["since_last_flight"])
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
          f"film(wetness) {slf['film_hours']} h, ambient-damp {slf['ambient_damp_hours_ub']} h")
    gc = res["grounding_caution"]
    print(f"Grounding caution   : {'YES - ' + gc['reason'] if gc['caution'] else 'no'}")
    print(f"Episodes (>=0.5h)   : {len(res['episodes'])}")
