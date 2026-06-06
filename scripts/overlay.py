"""3-day cowl-vs-station temperature overlay + timezone/solar sanity checks.
Run BEFORE fitting to eyeball alignment (gotcha #1)."""
from __future__ import annotations
import sys, os, glob
import numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
sys.path.insert(0, os.path.dirname(__file__))
from cowl_io import load_cowl_concat, to_utc, engine_run_mask
from gapfill import load_metar_csv, solar_elevation_deg, KMRB_LAT, KMRB_LON
from charts import PAPER, INK, RUST, STEEL, TEAL, AMBER, GRAY, GRID, _style, _clean, _header

DATA = os.path.join(os.path.dirname(__file__), "..", "data")

def load_all():
    paths = sorted(glob.glob(os.path.join(DATA, "Engine Thermo-hygrometer_Export Data_*.csv")))
    cowl = to_utc(load_cowl_concat(paths))
    metar = load_metar_csv(os.path.join(DATA, "kmrb-awos-data-12m.csv"))
    return cowl, metar

def pick_window(cowl, metar, days=3):
    """Auto-pick a clean engine-free window with full station coverage."""
    mask = engine_run_mask(cowl)
    cm = pd.Series(mask, index=cowl.index)
    # candidate starts: walk overlap in 1-day steps, score by (no engine run, station coverage)
    lo = max(cowl.index.min(), metar.index.min())
    hi = min(cowl.index.max(), metar.index.max())
    best = None
    d = lo.normalize() + pd.Timedelta(days=1)
    while d + pd.Timedelta(days=days) < hi:
        w0, w1 = d, d + pd.Timedelta(days=days)
        run = cm.loc[w0:w1].mean()
        mcov = len(metar.loc[w0:w1])  # ~hourly -> expect ~24*days
        if run == 0 and mcov >= 20*days:
            # prefer a window in autumn (clear, strong diurnal) -> score by month closeness to Oct
            score = -abs((w0.month - 10))
            if best is None or score > best[0]:
                best = (score, w0, w1)
        d += pd.Timedelta(days=1)
    return best[1], best[2]

def overlay(cowl, metar, w0, w1, out):
    _style()
    c = cowl["Tc"].loc[w0:w1].resample("10min").mean()
    m = metar["T"].loc[w0:w1].resample("10min").mean().interpolate(limit=12)
    j = pd.DataFrame({"c": c, "m": m}).dropna(subset=["c"])  # common 10-min grid
    c, m = j["c"], j["m"]
    el = solar_elevation_deg(c.index, KMRB_LAT, KMRB_LON)
    fig, ax = plt.subplots(figsize=(11, 4.8)); t = c.index
    # shade night (solar elev < 0)
    night = el < 0
    ax.fill_between(t, 0, 1, where=night, transform=ax.get_xaxis_transform(),
                    color=STEEL, alpha=0.07, step="mid")
    ax.plot(t, m, color=AMBER, lw=1.8, label="KMRB station air temp (UTC)")
    ax.plot(t, c, color=STEEL, lw=2.0, label="Cowl air temp (local→UTC)")
    # mark solar noon (max elevation) each day
    for day, g in pd.Series(el, index=t).groupby(t.normalize()):
        noon = g.idxmax()
        ax.axvline(noon, color=RUST, lw=0.9, ls=(0, (3, 3)), alpha=0.7)
    ax.axvline(t[0], color=RUST, lw=0.9, ls=(0, (3, 3)), alpha=0.7, label="Solar noon")
    ax.set_ylabel("Temperature (°C)"); _clean(ax)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d\n%H:%M"))
    ax.legend(frameon=False, fontsize=9, loc="upper right", ncol=3)
    _header(fig, "Cowl vs station — 3-day alignment check",
            f"{w0:%Y-%m-%d} → {w1:%Y-%m-%d} UTC · night shaded · red = solar noon (~17:00 UTC at KMRB)")
    plt.tight_layout(rect=[0, 0, 1, 0.88]); plt.savefig(out, dpi=160); plt.close()
    return out

def sanity(cowl, metar):
    """Gotcha #1 checks: daytime cowl-minus-ambient sign; solar-noon peak hour."""
    mask = engine_run_mask(cowl)
    c = cowl[~mask]["Tc"].resample("10min").mean()
    m = metar["T"].resample("10min").mean().interpolate(limit=12)
    j = pd.DataFrame({"Tc": c, "T": m}).dropna()
    el = solar_elevation_deg(j.index, KMRB_LAT, KMRB_LON)
    day = el > 10
    night = el < -5
    diff = j["Tc"] - j["T"]
    # solar elevation should peak ~17:00 UTC at KMRB (lon -77.98 -> ~ -5.2h)
    el_s = pd.Series(el, index=j.index)
    peak_hour = el_s.groupby(el_s.index.floor("D")).idxmax()
    peak_utc_hours = pd.DatetimeIndex(peak_hour.values).hour + pd.DatetimeIndex(peak_hour.values).minute/60
    return {
        "day_cowl_minus_ambient_C": round(float(diff[day].mean()), 2),
        "night_cowl_minus_ambient_C": round(float(diff[night].mean()), 2),
        "solar_noon_utc_hour_median": round(float(np.median(peak_utc_hours)), 2),
        "n_day": int(day.sum()), "n_night": int(night.sum()),
        "overlap_start": str(j.index.min()), "overlap_end": str(j.index.max()),
        "cowl_station_corr": round(float(j["Tc"].corr(j["T"])), 3),
    }

if __name__ == "__main__":
    cowl, metar = load_all()
    print("Cowl  :", cowl.index.min(), "->", cowl.index.max(), "n=", len(cowl))
    print("METAR :", metar.index.min(), "->", metar.index.max(), "n=", len(metar),
          "cloud" if "cloud" in metar else "(no cloud)")
    s = sanity(cowl, metar)
    print("\nSanity (gotcha #1):")
    for k, v in s.items(): print(f"  {k:32s}: {v}")
    if len(sys.argv) > 1:
        w0 = pd.Timestamp(sys.argv[1]); w1 = w0 + pd.Timedelta(days=3)
    else:
        w0, w1 = pick_window(cowl, metar)
    print(f"\nOverlay window: {w0} -> {w1}")
    out = os.path.join(DATA, "..", "reports", "overlay_3day.png")
    overlay(cowl, metar, w0, w1, os.path.abspath(out))
    print("Wrote", os.path.abspath(out))
