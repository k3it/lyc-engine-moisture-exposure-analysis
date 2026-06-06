"""Moisture-specific diagnostics, parallel to the temperature ones.
Generates: dew-point predicted-vs-actual (clear & overcast weeks), RH residual vs
hour-of-day, dew-point & RH residual vs cloud fraction, and a monthly seasonal
residual chart that exposes the winter condensation sink + near-saturation RH bias."""
from __future__ import annotations
import sys, os, json
import numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
sys.path.insert(0, os.path.dirname(__file__))
from overlay import load_all
from cowl_io import engine_run_mask
from gapfill import fit_transfer, synthesize_cowl, solar_elevation_deg, KMRB_LAT, KMRB_LON
from model import esat_hpa, dewpoint_c
from charts import INK, RUST, STEEL, TEAL, AMBER, GRAY, _style, _clean, _header
from diagnostics import masked_cowl, pick_weeks

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
REPORTS = os.path.abspath(os.path.join(DATA, "..", "reports"))
GRID = "10min"

def _truth_td(cowl):
    c = masked_cowl(cowl)[["Tc", "RH"]].resample(GRID).mean()
    c["Td"] = dewpoint_c(esat_hpa(c["Tc"].values) * np.clip(c["RH"].values, 1, 100)/100)
    return c

def aligned(cowl, metar, params):
    syn = synthesize_cowl(metar, params, GRID)
    syn["Td"] = dewpoint_c(esat_hpa(syn["Tc"].values) * np.clip(syn["RH"].values, 1, 100)/100)
    c = _truth_td(cowl)
    cf = metar["cloud"].resample(GRID).mean().interpolate(limit=12) if "cloud" in metar else None
    j = pd.DataFrame({"Td_p": syn["Td"], "Td_t": c["Td"],
                      "RH_p": syn["RH"], "RH_t": c["RH"]})
    if cf is not None:
        j["cloud"] = cf
    j = j.dropna(subset=["Td_p", "Td_t", "RH_p", "RH_t"])
    j["dres"] = j["Td_p"] - j["Td_t"]
    j["rres"] = j["RH_p"] - j["RH_t"]
    j["hour"] = j.index.hour
    j["el"] = solar_elevation_deg(j.index, KMRB_LAT, KMRB_LON)
    return j

def dewpt_predvactual(cowl, metar, params, w0, label, cf_val, out):
    _style()
    syn = synthesize_cowl(metar.loc[w0:w0+pd.Timedelta(days=7)], params, GRID)
    syn["Td"] = dewpoint_c(esat_hpa(syn["Tc"].values) * np.clip(syn["RH"].values, 1, 100)/100)
    c = _truth_td(cowl)["Td"].loc[w0:w0+pd.Timedelta(days=7)]
    j = pd.DataFrame({"pred": syn["Td"], "true": c}).dropna()
    fig, ax = plt.subplots(figsize=(11, 4.6)); t = j.index
    ax.plot(t, j["true"], color=TEAL, lw=2.0, label="Actual cowl dew point")
    ax.plot(t, j["pred"], color=RUST, lw=1.6, ls=(0, (4, 2)), label="Predicted (station→cowl)")
    ax.set_ylabel("Dew point (°C)"); _clean(ax)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    rmse = float(np.sqrt(np.mean((j["pred"]-j["true"])**2)))
    ax.legend(frameon=False, fontsize=9, loc="upper right")
    _header(fig, f"Predicted vs actual cowl dew point — {label} week",
            f"{w0:%Y-%m-%d}…+7d · mean cloud {cf_val:.2f} · RMSE {rmse:.2f}°C · near pass-through (vapor not stored)")
    plt.tight_layout(rect=[0, 0, 1, 0.88]); plt.savefig(out, dpi=160); plt.close()
    return out, rmse

def rh_resid_hour(j, out):
    _style()
    fig, ax = plt.subplots(figsize=(9, 4.4))
    g = j.groupby("hour")["rres"]; mean, std = g.mean(), g.std(); h = mean.index.values
    ax.axhline(0, color=INK, lw=0.8)
    ax.fill_between(h, mean-std, mean+std, color=AMBER, alpha=0.18, label="±1σ")
    ax.plot(h, mean.values, color=RUST, lw=2.2, marker="o", ms=4, label="Mean RH residual")
    ax.set_xlabel("Hour of day (UTC)"); ax.set_ylabel("Predicted − actual RH (%)"); _clean(ax)
    ax.set_xticks(range(0, 24, 2)); ax.legend(frameon=False, fontsize=9)
    _header(fig, "RH residual by hour of day",
            "Diurnal RH error inherited from the temperature reconstruction (RH = f(T, Td))")
    plt.tight_layout(rect=[0, 0, 1, 0.88]); plt.savefig(out, dpi=160); plt.close()
    return out

def moist_resid_cloud(j, out):
    _style()
    fig, ax = plt.subplots(figsize=(9, 4.4))
    ax2 = ax.twinx()
    bins = np.linspace(0, 1, 6)
    jb = j.dropna(subset=["cloud"]).copy()
    jb["cb"] = pd.cut(jb["cloud"], bins, include_lowest=True)
    gd = jb.groupby("cb", observed=True)["dres"].mean()
    gr = jb.groupby("cb", observed=True)["rres"].mean()
    x = [iv.mid for iv in gd.index]
    l1, = ax.plot(x, gd.values, color=TEAL, lw=2.2, marker="o", ms=5, label="Dew-point resid (°C)")
    l2, = ax2.plot(x, gr.values, color=RUST, lw=2.2, marker="s", ms=5, label="RH resid (%)")
    ax.axhline(0, color=INK, lw=0.8)
    ax.set_xlabel("Observed cloud fraction (ASOS)")
    ax.set_ylabel("Dew-point resid (°C)", color=TEAL); ax2.set_ylabel("RH resid (%)", color=RUST)
    ax.tick_params(axis="y", colors=TEAL); ax2.tick_params(axis="y", colors=RUST)
    _clean(ax); ax2.spines["top"].set_visible(False)
    ax.legend(handles=[l1, l2], frameon=False, fontsize=9, loc="upper left")
    _header(fig, "Moisture residual vs cloud fraction",
            "Weak slope ⇒ vapor needs no greenhouse/radiative term (unlike temperature)")
    plt.tight_layout(rect=[0, 0, 1, 0.88]); plt.savefig(out, dpi=160); plt.close()
    return out

def moist_resid_season(j, out):
    _style()
    fig, ax = plt.subplots(figsize=(9.5, 4.6)); ax2 = ax.twinx()
    g = j.copy(); g["mon"] = g.index.month
    dTd = g.groupby("mon")["dres"].mean()
    dRH = g.groupby("mon")["rres"].mean()
    months = list(dTd.index); xlab = [pd.Timestamp(2025, m, 1).strftime("%b") for m in months]
    x = np.arange(len(months))
    ax.bar(x-0.2, dTd.values, width=0.4, color=TEAL, label="Dew-point resid (°C)")
    ax2.bar(x+0.2, dRH.values, width=0.4, color=RUST, alpha=0.85, label="RH resid (%)")
    ax.axhline(0, color=INK, lw=0.8)
    ax.set_xticks(x); ax.set_xticklabels(xlab)
    ax.set_ylabel("Dew-point resid (°C)", color=TEAL); ax2.set_ylabel("RH resid (%)", color=RUST)
    ax.tick_params(axis="y", colors=TEAL); ax2.tick_params(axis="y", colors=RUST)
    _clean(ax); ax2.spines["top"].set_visible(False)
    h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1+h2, l1+l2, frameon=False, fontsize=9, loc="upper center", ncol=2)
    _header(fig, "Moisture residual by month — the structure the linear model misses",
            "Positive dew-point resid in winter ⇒ model over-predicts moisture (condensation/frost sink)")
    plt.tight_layout(rect=[0, 0, 1, 0.88]); plt.savefig(out, dpi=160); plt.close()
    return out

def near_saturation_stats(j):
    hi = j["RH_t"] > 85
    return {
        "n_near_sat": int(hi.sum()), "pct": round(100*float(hi.mean()), 1),
        "rh_resid_mean": round(float(j["rres"][hi].mean()), 1),
        "rh_resid_std": round(float(j["rres"][hi].std()), 1),
        "dewpt_resid_mean": round(float(j["dres"][hi].mean()), 2),
        "rh_resid_overall": round(float(j["rres"].mean()), 1),
        "dewpt_rmse": round(float(np.sqrt((j["dres"]**2).mean())), 2),
        "rh_rmse": round(float(np.sqrt((j["rres"]**2).mean())), 1),
        "day_dres": round(float(j["dres"][j.el > 5].mean()), 2),
        "night_dres": round(float(j["dres"][j.el < -5].mean()), 2),
        "corr_dres_cloud": round(float(j["dres"].corr(j["cloud"])), 3) if "cloud" in j else None,
    }

if __name__ == "__main__":
    cowl, metar = load_all()
    cm = masked_cowl(cowl)
    params = fit_transfer(cm, metar, GRID)
    j = aligned(cowl, metar, params)
    (cw, ccf), (ow, ocf) = pick_weeks(cowl, metar)

    outs = {}
    outs["dewpt_clear"], r1 = dewpt_predvactual(cowl, metar, params, cw, "clear-sky", ccf,
                              os.path.join(REPORTS, "diag_moist_predvactual_clear.png"))
    outs["dewpt_overcast"], r2 = dewpt_predvactual(cowl, metar, params, ow, "overcast", ocf,
                              os.path.join(REPORTS, "diag_moist_predvactual_overcast.png"))
    outs["rh_hour"] = rh_resid_hour(j, os.path.join(REPORTS, "diag_rh_resid_hour.png"))
    outs["moist_cloud"] = moist_resid_cloud(j, os.path.join(REPORTS, "diag_moist_resid_cloud.png"))
    outs["moist_season"] = moist_resid_season(j, os.path.join(REPORTS, "diag_moist_resid_season.png"))

    stats = near_saturation_stats(j)
    with open(os.path.join(DATA, "moisture_diag.json"), "w") as f:
        json.dump(stats, f, indent=2)

    print("Moisture charts:")
    for k, v in outs.items(): print(f"  {k:16s}: {v}")
    print(f"\nDew-point RMSE clear {r1:.2f}°C  overcast {r2:.2f}°C")
    print("\nMoisture stats:", json.dumps(stats, indent=2))
