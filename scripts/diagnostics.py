"""Diagnostics + drift check for the cowl<->station transfer fit.
Generates: clear-week & overcast-week predicted-vs-actual, residual vs hour-of-day,
residual vs cloud fraction (day + night), cowl-vs-station scatter w/ fitted line.
Also runs a first-half vs second-half coefficient drift check."""
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
from charts import PAPER, INK, RUST, STEEL, TEAL, AMBER, GRAY, GRID, _style, _clean, _header

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
GRID = "10min"

def masked_cowl(cowl):
    return cowl[~engine_run_mask(cowl)]

def aligned_pred(cowl, metar, params):
    """Predicted cowl Tc/RH (synth) joined to masked truth on the 10-min grid,
    with cloud fraction + solar elevation attached."""
    syn = synthesize_cowl(metar, params, GRID)
    truth = masked_cowl(cowl)[["Tc", "RH"]].resample(GRID).mean()
    cf = metar["cloud"].resample(GRID).mean().interpolate(limit=12) if "cloud" in metar else None
    j = pd.DataFrame({
        "Tc_pred": syn["Tc"], "RH_pred": syn["RH"],
        "Tc_true": truth["Tc"], "RH_true": truth["RH"],
    })
    if cf is not None:
        j["cloud"] = cf
    j = j.dropna(subset=["Tc_pred", "Tc_true"])
    j["resid"] = j["Tc_pred"] - j["Tc_true"]
    j["hour"] = j.index.hour
    j["el"] = solar_elevation_deg(j.index, KMRB_LAT, KMRB_LON)
    return j

def pick_weeks(cowl, metar):
    """Pick the clearest and most-overcast 7-day window with cowl coverage + no engine runs."""
    cm = pd.Series(engine_run_mask(cowl), index=cowl.index)
    cf = metar["cloud"].resample("D").mean()
    cov = masked_cowl(cowl)["Tc"].resample("D").count()
    cand = []
    lo = max(cowl.index.min(), metar.index.min()).normalize()
    hi = min(cowl.index.max(), metar.index.max())
    d = lo
    while d + pd.Timedelta(days=7) < hi:
        w0, w1 = d, d + pd.Timedelta(days=7)
        run = cm.loc[w0:w1].mean()
        wcf = cf.loc[w0:w1].mean()
        wcov = cov.loc[w0:w1].sum()
        if run == 0 and wcov > 6*100 and metar["cloud"].loc[w0:w1].count() > 100:
            cand.append((w0, wcf))
        d += pd.Timedelta(days=2)
    cand.sort(key=lambda x: x[1])
    clear = cand[0][0]
    overcast = cand[-1][0]
    return (clear, cand[0][1]), (overcast, cand[-1][1])

def predvactual_chart(cowl, metar, params, w0, label, cf_val, out):
    _style()
    syn = synthesize_cowl(metar.loc[w0:w0+pd.Timedelta(days=7)], params, GRID)
    truth = masked_cowl(cowl)["Tc"].loc[w0:w0+pd.Timedelta(days=7)].resample(GRID).mean()
    j = pd.DataFrame({"pred": syn["Tc"], "true": truth}).dropna()
    fig, ax = plt.subplots(figsize=(11, 4.6)); t = j.index
    el = solar_elevation_deg(t, KMRB_LAT, KMRB_LON)
    ax.fill_between(t, 0, 1, where=el < 0, transform=ax.get_xaxis_transform(),
                    color=STEEL, alpha=0.06, step="mid")
    ax.plot(t, j["true"], color=STEEL, lw=2.0, label="Actual cowl temp")
    ax.plot(t, j["pred"], color=RUST, lw=1.6, ls=(0, (4, 2)), label="Predicted (station→cowl)")
    ax.set_ylabel("Temperature (°C)"); _clean(ax)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    rmse = float(np.sqrt(np.mean((j["pred"]-j["true"])**2)))
    ax.legend(frameon=False, fontsize=9, loc="upper right")
    _header(fig, f"Predicted vs actual cowl temp — {label} week",
            f"{w0:%Y-%m-%d}…+7d · mean cloud frac {cf_val:.2f} · in-window RMSE {rmse:.2f}°C · night shaded")
    plt.tight_layout(rect=[0, 0, 1, 0.88]); plt.savefig(out, dpi=160); plt.close()
    return out, rmse

def residual_hour_chart(j, out):
    _style()
    fig, ax = plt.subplots(figsize=(9, 4.4))
    g = j.groupby("hour")["resid"]
    mean, std = g.mean(), g.std()
    h = mean.index.values
    ax.axhline(0, color=INK, lw=0.8)
    ax.fill_between(h, mean-std, mean+std, color=TEAL, alpha=0.2, label="±1σ")
    ax.plot(h, mean.values, color=STEEL, lw=2.2, marker="o", ms=4, label="Mean residual")
    ax.set_xlabel("Hour of day (UTC)"); ax.set_ylabel("Predicted − actual (°C)"); _clean(ax)
    ax.set_xticks(range(0, 24, 2))
    ax.legend(frameon=False, fontsize=9)
    _header(fig, "Temperature residual by hour of day",
            "Flat ≈ radiative terms capturing the diurnal shape; a sine wave ≈ unmodeled day/night bias")
    plt.tight_layout(rect=[0, 0, 1, 0.88]); plt.savefig(out, dpi=160); plt.close()
    return out

def residual_cloud_chart(j, out):
    _style()
    fig, ax = plt.subplots(figsize=(9, 4.4))
    bins = np.linspace(0, 1, 6)
    jb = j.dropna(subset=["cloud"]).copy()
    jb["cb"] = pd.cut(jb["cloud"], bins, include_lowest=True)
    for sub, color, lab in [(jb[jb["el"] > 5], AMBER, "Day (el>5°)"),
                            (jb[jb["el"] < -5], STEEL, "Night (el<−5°)")]:
        g = sub.groupby("cb", observed=True)["resid"]
        x = [iv.mid for iv in g.mean().index]
        ax.plot(x, g.mean().values, color=color, lw=2.2, marker="o", ms=5, label=lab)
    ax.axhline(0, color=INK, lw=0.8)
    ax.set_xlabel("Observed cloud fraction (ASOS)"); ax.set_ylabel("Predicted − actual (°C)"); _clean(ax)
    ax.legend(frameon=False, fontsize=9)
    _header(fig, "Temperature residual vs cloud fraction",
            "Night slope vs cloud ≈ residual radiative-cooling signal the fit didn't absorb")
    plt.tight_layout(rect=[0, 0, 1, 0.88]); plt.savefig(out, dpi=160); plt.close()
    return out

def scatter_chart(cowl, metar, params, out):
    _style()
    # cowl Tc vs station T, with the fitted lag+terms line is multi-d; show raw scatter
    # plus the synthesized prediction as the "fitted" relationship.
    cm = masked_cowl(cowl)["Tc"].resample(GRID).mean()
    st = metar["T"].resample(GRID).mean().interpolate(limit=12)
    syn = synthesize_cowl(metar, params, GRID)["Tc"]
    j = pd.DataFrame({"station": st, "cowl": cm, "pred": syn}).dropna()
    fig, ax = plt.subplots(figsize=(6.6, 6.2))
    ax.scatter(j["station"], j["cowl"], s=2, color=GRAY, alpha=0.18, label="Cowl vs station (raw)")
    lim = [min(j["station"].min(), j["cowl"].min())-1, max(j["station"].max(), j["cowl"].max())+1]
    ax.plot(lim, lim, color=INK, lw=1.0, ls=(0, (3, 3)), label="1:1")
    # fitted relationship: predicted cowl vs station (sorted)
    s = j.sort_values("station")
    ax.scatter(s["station"], s["pred"], s=2, color=RUST, alpha=0.25, label="Fitted (predicted cowl)")
    ax.set_xlim(lim); ax.set_ylim(lim)
    ax.set_xlabel("KMRB station air temp (°C)"); ax.set_ylabel("Cowl air temp (°C)"); _clean(ax)
    ax.legend(frameon=False, fontsize=9, loc="upper left", markerscale=4)
    r = float(j["station"].corr(j["cowl"]))
    _header(fig, "Cowl vs station scatter",
            f"r={r:.3f} · flatter-than-1:1 cloud = damping (cowl warmer when station cold, cooler when station hot)")
    plt.tight_layout(rect=[0, 0, 1, 0.90]); plt.savefig(out, dpi=160); plt.close()
    return out

def drift_check(cowl, metar):
    """Fit on first-half vs second-half; compare key coefficients."""
    cm = masked_cowl(cowl)
    mid = cm.index.min() + (cm.index.max()-cm.index.min())/2
    a = fit_transfer(cm.loc[:mid], metar, GRID)["summary"]
    b = fit_transfer(cm.loc[mid:], metar, GRID)["summary"]
    keys = ["thermal_lag_h", "thermal_damping", "solar_gain_C", "radiative_cool_C",
            "temp_rmse_C", "temp_r2", "moisture_lag_h", "moisture_damping", "dewpt_rmse_C"]
    return {"split_at": str(mid), "first_half": {k: a[k] for k in keys},
            "second_half": {k: b[k] for k in keys}}

if __name__ == "__main__":
    cowl, metar = load_all()
    cm = masked_cowl(cowl)
    params = fit_transfer(cm, metar, GRID)
    j = aligned_pred(cowl, metar, params)

    (cw, ccf), (ow, ocf) = pick_weeks(cowl, metar)
    outs = {}
    outs["clear"], rmse_clear = predvactual_chart(cowl, metar, params, cw, "clear-sky", ccf,
                                    os.path.abspath(os.path.join(DATA, "..", "reports", "diag_predvactual_clear.png")))
    outs["overcast"], rmse_oc = predvactual_chart(cowl, metar, params, ow, "overcast", ocf,
                                    os.path.abspath(os.path.join(DATA, "..", "reports", "diag_predvactual_overcast.png")))
    outs["resid_hour"] = residual_hour_chart(j, os.path.abspath(os.path.join(DATA, "..", "reports", "diag_resid_hour.png")))
    outs["resid_cloud"] = residual_cloud_chart(j, os.path.abspath(os.path.join(DATA, "..", "reports", "diag_resid_cloud.png")))
    outs["scatter"] = scatter_chart(cowl, metar, params, os.path.abspath(os.path.join(DATA, "..", "reports", "diag_scatter.png")))

    drift = drift_check(cowl, metar)
    with open(os.path.join(DATA, "drift_check.json"), "w") as f:
        json.dump(drift, f, indent=2)

    print("Charts written:")
    for k, v in outs.items(): print(f"  {k:10s}: {v}")
    print(f"\nClear week {cw:%Y-%m-%d} cloud={ccf:.2f} RMSE={rmse_clear:.2f}°C")
    print(f"Overcast wk {ow:%Y-%m-%d} cloud={ocf:.2f} RMSE={rmse_oc:.2f}°C")
    print("\n=== DRIFT CHECK (first half vs second half) ===")
    print(f"split at {drift['split_at']}")
    for k in drift["first_half"]:
        a, b = drift["first_half"][k], drift["second_half"][k]
        print(f"  {k:20s}: {a:>8}  |  {b:>8}")
