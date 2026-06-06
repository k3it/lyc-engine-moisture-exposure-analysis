"""Chart generation for the engine-moisture skill. Cohesive steel-and-rust palette.
Produces PNGs suitable for embedding in a report or attaching to a Telegram alert.

Usage (from analyze() output):
    from charts import event_chart, seasonal_chart, dewpoint_divergence_chart
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np, pandas as pd

PAPER="#f7f4ee"; INK="#2a2622"; RUST="#c2410c"; STEEL="#2f4858"
TEAL="#3b6978"; AMBER="#d98324"; GRAY="#a89f90"; GRID="#d9d2c5"

def _style():
    plt.rcParams.update({
        "figure.facecolor":PAPER,"axes.facecolor":PAPER,"savefig.facecolor":PAPER,
        "axes.edgecolor":INK,"axes.labelcolor":INK,"text.color":INK,
        "xtick.color":INK,"ytick.color":INK,"axes.linewidth":0.8,"font.size":11,
        "axes.grid":True,"grid.color":GRID,"grid.linewidth":0.7,"font.family":"DejaVu Sans"})

def _clean(ax):
    for s in ("top","right"): ax.spines[s].set_visible(False)
    ax.tick_params(length=0)

def _header(fig, title, sub):
    fig.text(0.04,0.955,title,fontsize=14,fontweight="bold",color=INK)
    fig.text(0.04,0.90,sub,fontsize=9.8,color="#6b6358")

def event_chart(series, center_time, out, hours=18):
    """Anatomy of a single condensation event: air/metal/dewpoint + film + shaded phases."""
    _style()
    c = pd.Timestamp(center_time)
    e = series.loc[c - pd.Timedelta(hours=hours): c + pd.Timedelta(hours=hours)]
    fig, ax = plt.subplots(figsize=(9,4.6)); t = e.index
    ax.plot(t, e["Tc"], color=GRAY, lw=1.6, label="Cowl air temp")
    ax.plot(t, e["Tm"], color=STEEL, lw=2.4, label="Cam metal temp")
    ax.plot(t, e["Td_int"], color=TEAL, lw=1.8, ls=(0,(5,2)), label="Interior dew point")
    cond = (e["Tm"] < e["Td_int"]).values
    film = (e["film"] > 0.1).values
    lo, hi = ax.get_ylim()
    ax.fill_between(t, lo, hi, where=cond, color=RUST, alpha=0.16, label="Condensing")
    ax.fill_between(t, lo, hi, where=film & ~cond, color=AMBER, alpha=0.18, label="Drying tail")
    ax.set_ylim(lo, hi); ax.set_ylabel("Temperature (°C)"); _clean(ax)
    ax2 = ax.twinx(); ax2.fill_between(t, 0, e["film"], color=RUST, alpha=0.28)
    ax2.plot(t, e["film"], color=RUST, lw=1.2); ax2.set_ylabel("Condensed film (g/m²)", color=RUST)
    ax2.tick_params(colors=RUST); ax2.spines["top"].set_visible(False)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d\n%H:%M"))
    ax.legend(frameon=False, fontsize=8.8, loc="upper right")
    _header(fig, "Condensation event", "Warm humid air over the cold-soaked cam, with the drying tail")
    plt.tight_layout(rect=[0,0,1,0.86]); plt.savefig(out, dpi=160); plt.close()
    return out

def seasonal_chart(series, out):
    """Monthly film-hours, split sub-dew-point vs evaporation tail."""
    _style()
    wf = (series["film"] > 0.1).astype(float)
    sub = (series["Tm"] < series["Td_int"]).astype(float)
    m = pd.DataFrame({"film":wf,"sub":sub}, index=series.index).resample("ME").sum()/60
    m = m[(m["film"]>0.02)|(m["sub"]>0.02)]
    if len(m)==0:
        m = pd.DataFrame({"film":[0],"sub":[0]}, index=[series.index[-1]])
    fig, ax = plt.subplots(figsize=(9,4.3)); x = np.arange(len(m))
    tail = np.clip(m["film"].values - m["sub"].values, 0, None)
    ax.bar(x, m["sub"].values, color=STEEL, width=0.62, label="Sub-dew-point")
    ax.bar(x, tail, bottom=m["sub"].values, color=RUST, width=0.62, label="Evaporation tail")
    ax.set_xticks(x); ax.set_xticklabels([d.strftime("%b\n%y") for d in m.index], fontsize=9)
    ax.set_ylabel("Cam wet-hours per month"); _clean(ax)
    ax.legend(frameon=False, fontsize=9.5, loc="upper left")
    _header(fig, "Seasonal wetting profile", "When the cam actually holds a condensed film")
    plt.tight_layout(rect=[0,0,1,0.86]); plt.savefig(out, dpi=160); plt.close()
    return out

def dewpoint_divergence_chart(series, center_time, out, hours=18):
    """Shows WHY restricted breathing protects: exterior vs interior dew point during an event."""
    _style()
    c = pd.Timestamp(center_time)
    e = series.loc[c - pd.Timedelta(hours=hours): c + pd.Timedelta(hours=hours)]
    fig, ax = plt.subplots(figsize=(9,4.2)); t = e.index
    ax.plot(t, e["Td_ext"], color=AMBER, lw=2.0, label="Exterior dew point (cowl)")
    ax.plot(t, e["Td_int"], color=TEAL, lw=2.0, ls=(0,(5,2)), label="Interior dew point (lagged)")
    ax.plot(t, e["Tm"], color=STEEL, lw=2.4, label="Cam metal temp")
    ax.fill_between(t, e["Tm"], e["Td_ext"], where=(e["Td_ext"]>e["Tm"]),
                    color=RUST, alpha=0.12)
    ax.set_ylabel("Temperature (°C)"); _clean(ax)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d\n%H:%M"))
    ax.legend(frameon=False, fontsize=9.3, loc="best")
    _header(fig, "Why restricted breathing protects",
            "Outside humidity spikes; the interior lags and never reaches the cam in time")
    plt.tight_layout(rect=[0,0,1,0.86]); plt.savefig(out, dpi=160); plt.close()
    return out

def summary_chart(series, res, out, margin_c=2.0, days=None):
    """ONE at-a-glance chart of cam moisture since the last flight: metal vs interior
    dew point, with condensing and 'close-call' (within margin_c) periods shaded, plus
    a cumulative wet+close hours line. Built for the Telegram nudge."""
    _style()
    lf = res.get("last_flight")
    s = series.loc[lf:] if lf else series
    if days:
        s = s.loc[s.index.max() - pd.Timedelta(days=days):]
    t = s.index
    gap = (s["Td_int"] - s["Tm"])
    cond = (gap > 0).values                       # at/below dew point -> condensing
    close = ((gap > -margin_c) & (gap <= 0)).values   # within margin, not yet crossing
    m = int(round(margin_c))

    EXPO = "#6d28d9"   # violet, distinct from the steel/teal temperature traces
    fig, ax = plt.subplots(figsize=(9, 4.6))
    ax.plot(t, s["Tm"], color=STEEL, lw=2.0, label="Cam metal temp")
    ax.plot(t, s["Td_int"], color=TEAL, lw=1.5, ls=(0, (5, 2)), label="Interior dew point")
    lo, hi = ax.get_ylim()
    ax.fill_between(t, lo, hi, where=cond, color=RUST, alpha=0.32,
                    label="At/below dew point (condensing)")
    ax.fill_between(t, lo, hi, where=close & ~cond, color=AMBER, alpha=0.22,
                    label=f"Within {m} °C (close call)")
    ax.set_ylim(lo, hi); ax.set_ylabel("Temperature (°C)"); _clean(ax)

    expo = np.cumsum((cond | close).astype(float)) / 60.0   # cumulative hours
    ax2 = ax.twinx()
    ax2.plot(t, expo, color=EXPO, lw=1.6, label="Cumulative wet + close hours")
    ax2.set_ylabel("Cumulative wet + close hours", color=EXPO)
    ax2.tick_params(colors=EXPO); ax2.spines["top"].set_visible(False)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, frameon=False, fontsize=8.2, loc="upper left", ncol=2)
    slf = res.get("since_last_flight", {})
    sub_h = round(float(cond.sum()) / 60, 1)
    close_h = round(float((close & ~cond).sum()) / 60, 1)
    _header(fig, "Cam moisture since last flight",
            f"{slf.get('days')} d grounded  ·  {sub_h} h at/below dew point  ·  "
            f"{close_h} h within {m} °C")
    plt.tight_layout(rect=[0, 0, 1, 0.86]); plt.savefig(out, dpi=160); plt.close()
    return out
