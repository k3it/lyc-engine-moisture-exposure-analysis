"""
Offline end-to-end check for the Home Assistant moisture monitor.

Runs the *exact* app pipeline (build_frame -> run_model -> reconcile_last_flight ->
decide_alert -> charts -> message) against the repo's real X-Sense CSV, with the
network/LLM/HA pieces stubbed, and asserts:
  * the model parity: feeding the data through the recorder-shaped path
    (build_frame) gives the same numbers as a plain regrid() of the CSV;
  * sensors' worth of metrics are produced;
  * charts render to PNG;
  * the message composer + fallback work.

This validates everything except the live HA/Telegram/Gemini calls. Run:
    python homeassistant/test_local.py [days]
"""
import os, sys, glob, tempfile
from pathlib import Path

try:                       # make emoji/em-dash safe on Windows consoles
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "scripts"))

import pandas as pd
from model import load_csv, regrid
from cowl_io import to_utc
import pipeline as mm


def newest_csv():
    files = sorted(glob.glob(str(REPO / "data" /
                   "Engine Thermo-hygrometer_Export Data_*.csv")))
    if not files:
        raise SystemExit("No X-Sense CSV found under data/")
    return files[-1]


def approx(a, b, tol=0.5):
    return abs((a or 0) - (b or 0)) <= tol


def gapfill_check():
    """Offline check of the sensor gap-fill fallback (no CSV, no network).

    Builds a synthetic cowl year-fraction, fits an (identity-ish) station->cowl
    transfer on it, punches a 12 h hole in the middle and cuts the last 24 h off
    (the 'sensor stopped reporting' case), then asserts apply_gapfill() detects both
    gaps, splices the synthesized estimate back in flagged estimated=True, and that
    the reconstruction tracks the truth. Station fetch is bypassed via station_df."""
    import datetime as dt
    import json
    import numpy as np
    from gapfill import fit_transfer
    from model import esat_hpa, dewpoint_c

    print("\n[gap-fill fallback: synthetic end-to-end]")
    # --- synthetic truth on a naive-UTC 1-min grid (14 days) ---
    idx = pd.date_range("2026-06-15", periods=14 * 24 * 60, freq="1min")
    hours = (idx - idx[0]).total_seconds() / 3600
    tc = 22 + 6 * np.sin(2 * np.pi * (hours - 9) / 24)
    rh = 70 + 12 * np.sin(2 * np.pi * (hours - 3) / 24)
    truth_utc = pd.DataFrame({"Tc": tc, "RH": rh}, index=idx)

    # station = hourly obs of the same air (identity transfer; the real fit quality
    # is validated by the backtest — this exercises the plumbing)
    td = dewpoint_c(esat_hpa(truth_utc["Tc"].values)
                    * np.clip(truth_utc["RH"].values, 1, 100) / 100)
    metar = pd.DataFrame({"T": truth_utc["Tc"].values, "Td": td},
                         index=idx).resample("1h").mean()
    params = fit_transfer(truth_utc, metar)
    pfile = Path(tempfile.mkdtemp(prefix="moisture_gapfill_")) / "transfer.json"
    pfile.write_text(json.dumps(params))

    # --- local-time frame with a 12 h hole and a 24 h stale tail ---
    tz = "America/New_York"
    local = truth_utc.copy()
    local.index = local.index.tz_localize("UTC").tz_convert(tz).tz_localize(None)
    now_local = local.index[-1]
    hole_s = local.index[0] + pd.Timedelta(days=6)
    hole_e = hole_s + pd.Timedelta(hours=12)
    holed = local[~((local.index >= hole_s) & (local.index <= hole_e))]
    holed = holed[holed.index <= now_local - pd.Timedelta(hours=24)]

    cfg = dict(mm.DEFAULTS, timezone=tz, transfer_params_path=str(pfile))
    gaps = mm.find_sensor_gaps(holed, now_local, cfg["gapfill_stale_min"])
    assert len(gaps) == 2, f"expected hole + stale tail, got {gaps}"

    filled, info = mm.apply_gapfill(holed, cfg, now_local, station_df=metar)
    print(f"  gaps={info['gaps']}")
    print(f"  filled_minutes={info['filled_minutes']} stale={info['stale']} "
          f"error={info['error']}")
    assert info["error"] is None, f"gap-fill errored: {info['error']}"
    assert info["stale"] is True, "stale tail not flagged"
    assert info["filled_minutes"] >= 0.8 * 36 * 60, "gaps not substantially filled"
    est = filled["estimated"]
    assert not est[est.index < hole_s].iloc[:-1].any(), "real rows flagged estimated"
    assert est[(est.index > hole_s) & (est.index < hole_e)].all(), "hole rows not flagged"

    # reconstruction should track the truth closely (identity transfer)
    hole_syn = filled.loc[hole_s:hole_e, "Tc"]
    hole_truth = local.loc[hole_syn.index, "Tc"]
    rmse = float(np.sqrt(((hole_syn - hole_truth) ** 2).mean()))
    print(f"  hole reconstruction Tc RMSE={rmse:.2f} °C")
    assert rmse < 1.5, f"synthesized cowl temp off by {rmse:.2f} °C"

    # model runs on the filled frame; the estimated mask reaches the chart
    res, series = mm.run_model(filled, cfg)
    assert "estimated" in series.columns and series["estimated"].any()
    note = mm.gapfill_note(info)
    print(f"  note: {note}")
    assert note and "estimated" in note
    import charts as ch
    out = Path(tempfile.mkdtemp(prefix="moisture_gapfill_")) / "summary_gapfill.png"
    p = ch.summary_chart(series, res, str(out), history_days=14)
    assert os.path.getsize(p) > 1000
    print(f"  chart with estimated shading: {p}")
    print("  gap-fill fallback OK")


def main(days=21):
    gapfill_check()                        # synthetic; runs even without a CSV export
    cfg = dict(mm.DEFAULTS)
    csv = newest_csv()
    print(f"Using {os.path.basename(csv)}  (last {days} days)")

    # --- load + slice to keep the offline run quick ---
    df = load_csv(csv)                      # naive LOCAL index, Tc(°C), RH(%)
    cutoff = df.index.max() - pd.Timedelta(days=days)
    df = df.loc[df.index >= cutoff]
    if len(df) < 100:
        raise SystemExit("Not enough rows in slice; pick more --days")

    # --- path 1: plain CSV -> regrid -> model (the canonical reference) ---
    g1 = regrid(df)
    res1, series1 = mm.run_model(g1, cfg)
    slf1 = res1["since_last_flight"]
    print("\n[reference: regrid(CSV)]")
    print(f"  span {res1['span'][0]} -> {res1['span'][1]}  ({res1['minutes']} min)")
    print(f"  film_hours={res1['film_hours']:.1f}  flights={res1['flight_count']}  "
          f"last_flight={res1['last_flight']}")
    print(f"  since_last_flight: days={slf1['days']} film_h={slf1['film_hours']} "
          f"damp_h={slf1['ambient_damp_hours_ub']}")

    # --- path 2: recorder-shaped states -> build_frame -> model ---
    utc = to_utc(df)                        # naive UTC, Tc/RH (mirrors prod tz logic)
    idx = utc.index.tz_localize("UTC")
    tf = utc["Tc"].values * 9.0 / 5.0 + 32.0   # back to °F as HA would store it
    temp_states = [{"state": f"{v:.1f}", "last_changed": t.isoformat()}
                   for t, v in zip(idx, tf)]
    rh_states = [{"state": f"{v:.1f}", "last_changed": t.isoformat()}
                 for t, v in zip(idx, utc["RH"].values)]
    g2 = mm.build_frame(temp_states, rh_states, cfg["timezone"], "F")
    assert g2 is not None and len(g2) > 0, "build_frame produced nothing"
    res2, series2 = mm.run_model(g2, cfg)
    print("\n[app path: build_frame(recorder states)]")
    print(f"  film_hours={res2['film_hours']:.1f}  flights={res2['flight_count']}  "
          f"last_flight={res2['last_flight']}")

    # --- parity: the two paths should agree closely ---
    assert approx(res1["film_hours"], res2["film_hours"], tol=1.0), \
        f"film_hours mismatch: {res1['film_hours']} vs {res2['film_hours']}"
    assert res1["flight_count"] == res2["flight_count"], \
        f"flight_count mismatch: {res1['flight_count']} vs {res2['flight_count']}"
    print("  parity OK (film_hours & flight_count match the reference)")

    # --- reconcile_last_flight: stored older flight is carried; new run resets ---
    lf, is_new = mm.reconcile_last_flight(res2, series2, None, cfg)
    print(f"\n[reconcile] first-seen last_flight={lf} is_new={is_new}")
    assert is_new is False, "first-ever detection should not count as a reset"

    model_lf = res2.get("last_flight")
    if model_lf:
        # a) model sees a flight newer than what we stored -> NEW run (reset)
        older = "2000-01-01T00:00:00"
        lf2, new2 = mm.reconcile_last_flight(dict(res2), series2, older, cfg)
        assert new2 is True and lf2 == model_lf, "newer hot run should reset the tally"
        # b) flight scrolled out of the window (grounded > max_window_days): model
        #    sees none, but we carry the stored flight and recompute over the window.
        carried = (series2.index[0] - pd.Timedelta(days=10)).isoformat()
        r3 = dict(res2); r3["last_flight"] = None
        r3["since_last_flight"] = dict(res2["since_last_flight"])
        lf3, new3 = mm.reconcile_last_flight(r3, series2, carried, cfg)
        assert new3 is False and lf3 == carried, "carried-forward flight should be kept"
        assert r3["since_last_flight"]["days"] is not None, "tally should recompute"
        print("  reset semantics OK (newer run resets; scrolled-out flight carried)")

    # --- deterministic moisture exposure (close calls) ---
    nw = mm.near_wet_stats(series2, res2, cfg["close_call_margin_c"])
    print(f"\n[near_wet] sub_dew={nw['sub_dew_h']}h close_call={nw['close_call_h']}h "
          f"peak_gap={nw['peak_gap_c']}°C")
    moisture_line = mm.moisture_status_line(res2, series2, cfg["close_call_margin_c"], nw)
    print(f"[moisture line] {moisture_line}")
    assert "since last flight" in moisture_line

    # --- alert decision (fires on exposure, weather is NOT a gate) ---
    import datetime as dt
    now = dt.datetime(2026, 6, 5, 14, 0)   # afternoon, outside quiet hours
    cfg_fire = dict(cfg, alert_wet_hours=0.0, alert_close_call_hours=0.0)
    fire, reason = mm.decide_alert(nw, res2["since_last_flight"],
                                   {"last_alert_ts": None}, cfg_fire, now)
    print(f"[decide_alert] fire={fire} reason={reason}")
    assert fire, f"expected alert to fire, got: {reason}"

    # --- Gemini does ONLY the window; moisture is never sent to it ---
    weather_info = {
        "best_window": {"phrase": "Sat afternoon", "is_weekend": True, "hours": 4.0},
        "forecast_brief": "TAF: VFR, calm\nSat Jun 6: wind 4-8 kt, Sunny, precip 0%",
        "taf": "TAF: VFR, calm",
    }
    wprompt = mm.build_window_prompt(weather_info)
    assert "moisture" not in wprompt.lower().split("do not mention")[0] or "do NOT mention" in wprompt
    assert "flying window" in wprompt.lower()
    window_line = mm.window_fallback(weather_info)
    msg = mm.assemble_message(moisture_line, window_line)
    print(f"\n[assembled message]\n{msg}")
    assert "Sat afternoon" in msg and moisture_line in msg

    # --- single summary chart renders ---
    import charts as ch
    out = Path(tempfile.mkdtemp(prefix="moisture_charts_"))
    p = ch.summary_chart(series2, res2, str(out / "summary.png"),
                         margin_c=cfg["close_call_margin_c"])
    assert os.path.getsize(p) > 1000, f"summary chart looks empty: {p}"
    print(f"\n[chart] wrote summary chart to {p}")

    print("\nALL CHECKS PASSED ✅")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 21)
