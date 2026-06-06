"""
Loading + timezone reconciliation for the cowl<->station backtest.

Keeps ALL physics in model.py / gapfill.py; this module only does I/O plumbing:
  - concatenate the two X-Sense half-year exports, dedupe the boundary overlap
  - localize the cowl LOCAL-time index (America/New_York, DST-aware) -> naive UTC
    so it aligns with the Mesonet METAR (naive UTC) and gapfill.solar_elevation_deg
    (which assumes a UTC index).
  - build an engine-run mask (+cooldown) so hot-run periods don't corrupt the
    station->cowl regression. Reuses the corrosion model's FLIGHT_TEMP_C threshold.
"""
from __future__ import annotations
import sys, os, glob
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(__file__))
from model import load_csv, FLIGHT_TEMP_C

COWL_TZ = "America/New_York"

def load_cowl_concat(paths):
    """Load + concat X-Sense exports, dedupe overlapping/identical timestamps.
    Returns a frame [Tc, RH] indexed by NAIVE LOCAL time, ascending."""
    frames = [load_csv(p) for p in paths]
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    return df

def to_utc(df_local, tz=COWL_TZ):
    """Localize a naive-LOCAL-time frame to UTC, return NAIVE-UTC indexed frame.
    DST: ambiguous (fall-back repeat) inferred from order; nonexistent (spring
    skip) shifted forward."""
    idx = df_local.index
    try:
        loc = idx.tz_localize(tz, ambiguous="infer", nonexistent="shift_forward")
    except Exception:
        # infer can fail if the repeated fall-back hour isn't cleanly ordered;
        # fall back to marking the ambiguous hour as DST (True) deterministically.
        loc = idx.tz_localize(tz, ambiguous=True, nonexistent="shift_forward")
    utc = loc.tz_convert("UTC").tz_localize(None)
    out = df_local.copy()
    out.index = utc
    out = out[~out.index.duplicated(keep="first")].sort_index()
    out.index.name = "time"
    return out

def engine_run_mask(cowl_utc, flight_temp_c=FLIGHT_TEMP_C, cooldown_h=4.0):
    """Boolean mask (True = EXCLUDE) for engine-run spikes + cooldown tail.
    A run is any sample with Tc > flight_temp_c; the mask extends cooldown_h
    forward in time from each such sample so the post-shutdown soak (still
    engine-driven, not weather) is also removed."""
    hot = cowl_utc["Tc"].values > flight_temp_c
    # forward-extend each hot sample by cooldown using a time-aware rolling max
    s = pd.Series(hot.astype(float), index=cowl_utc.index)
    # rolling window looking BACK cooldown_h: if any hot sample within the prior
    # cooldown window, this sample is in a cooldown tail.
    win = f"{int(cooldown_h*60)}min"
    cooled = s.rolling(win).max().fillna(0.0).values > 0
    return cooled  # includes the hot samples themselves

if __name__ == "__main__":
    paths = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..", "data",
                   "Engine Thermo-hygrometer_Export Data_*.csv")))
    print("Files:")
    for p in paths: print("  ", os.path.basename(p))
    local = load_cowl_concat(paths)
    print(f"\nConcatenated (local): {local.index.min()} -> {local.index.max()}  "
          f"n={len(local)}")
    utc = to_utc(local)
    print(f"UTC reconciled      : {utc.index.min()} -> {utc.index.max()}  n={len(utc)}")
    mask = engine_run_mask(utc)
    print(f"Engine-run+cooldown masked: {mask.sum()} samples "
          f"({100*mask.mean():.1f}%); hot(>{FLIGHT_TEMP_C}C): {(utc['Tc']>FLIGHT_TEMP_C).sum()}")
