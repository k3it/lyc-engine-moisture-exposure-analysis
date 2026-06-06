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

def summary_chart(series, res, out, margin_c=2.0, history_days=75, days=None):
    """ONE at-a-glance chart for the Telegram nudge: cam metal vs interior dew point
    over the last `history_days` (default ~2.5 months for context), with condensing and
    'close-call' (within margin_c) periods shaded. Detected flights are marked with
    vertical lines, and the cumulative wet+close-hours line RESETS to zero at each flight
    -- so the purple sawtooth shows how exposure built up and was purged each time the
    engine ran, making the current since-last-flight climb easy to read against the past.

    `history_days` controls the visible window; `days` is a deprecated alias kept for
    older callers (when given it overrides history_days)."""
    _style()
    win = days if days is not None else history_days
    s = series.loc[series.index.max() - pd.Timedelta(days=win):] if win else series
    t = s.index
    gap = (s["Td_int"] - s["Tm"])
    cond = (gap > 0).values                       # at/below dew point -> condensing
    close = ((gap > -margin_c) & (gap <= 0)).values   # within margin, not yet crossing
    m = int(round(margin_c))

    # flight reset points inside the visible window (model exposes all run starts)
    flights = res.get("flights") or ([res["last_flight"]] if res.get("last_flight") else [])
    f_ts = [pd.Timestamp(f) for f in flights if f]
    f_ts = [f for f in f_ts if t[0] <= f <= t[-1]]
    reset_pos = sorted({int(t.searchsorted(f)) for f in f_ts if 0 <= t.searchsorted(f) < len(t)})

    EXPO = "#6d28d9"   # violet, distinct from the steel/teal temperature traces
    fig, ax = plt.subplots(figsize=(9.6, 4.8))
    ax.plot(t, s["Tm"], color=STEEL, lw=1.8, label="Cam metal temp")
    ax.plot(t, s["Td_int"], color=TEAL, lw=1.4, ls=(0, (5, 2)), label="Interior dew point")
    lo, hi = ax.get_ylim()
    ax.fill_between(t, lo, hi, where=cond, color=RUST, alpha=0.32,
                    label="At/below dew point (condensing)")
    ax.fill_between(t, lo, hi, where=close & ~cond, color=AMBER, alpha=0.22,
                    label=f"Within {m} °C (close call)")
    # flight markers (tally reset points)
    for j, fi in enumerate(reset_pos):
        ax.axvline(t[fi], color=STEEL, lw=1.1, ls=(0, (2, 2)), alpha=0.55,
                   label="Flight (tally reset)" if j == 0 else None)
    ax.set_ylim(lo, hi); ax.set_ylabel("Temperature (°C)"); _clean(ax)

    # cumulative wet+close hours, reset to 0 at each flight -> sawtooth across history
    wetmin = (cond | close).astype(float) / 60.0
    expo = np.empty(len(wetmin)); acc = 0.0
    resets = set(reset_pos)
    for i in range(len(wetmin)):
        if i in resets:
            acc = 0.0
        acc += wetmin[i]
        expo[i] = acc
    ax2 = ax.twinx()
    ax2.plot(t, expo, color=EXPO, lw=1.6, label="Wet + close hours (resets each flight)")
    ax2.set_ylabel("Wet + close hours since flight", color=EXPO)
    ax2.set_ylim(bottom=0); ax2.tick_params(colors=EXPO); ax2.spines["top"].set_visible(False)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, frameon=False, fontsize=8.0, loc="upper left", ncol=2)
    slf = res.get("since_last_flight", {})
    # since-last-flight tally (headline) computed over the post-flight slice
    lf = res.get("last_flight")
    sl = s.loc[lf:] if lf else s
    glf = (sl["Td_int"] - sl["Tm"])
    sub_h = round(float((glf > 0).sum()) / 60, 1)
    close_h = round(float(((glf > -margin_c) & (glf <= 0)).sum()) / 60, 1)
    span_days = max(1, int(round((t[-1] - t[0]).total_seconds() / 86400))) if len(t) else 0
    _header(fig, f"Cam moisture — last {span_days} days",
            f"Since last flight: {slf.get('days')} d  ·  {sub_h} h at/below dew point  ·  "
            f"{close_h} h within {m} °C  ·  {len(f_ts)} flights shown")
    plt.tight_layout(rect=[0, 0, 1, 0.86]); plt.savefig(out, dpi=160); plt.close()
    return out
