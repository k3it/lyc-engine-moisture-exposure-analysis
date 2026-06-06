"""Headline deliverable: backtest + full transfer fit, masking engine runs.
Writes data/kmrb_cowl_transfer.json and prints the summary."""
from __future__ import annotations
import sys, os, json
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(__file__))
from overlay import load_all
from cowl_io import engine_run_mask
from gapfill import backtest, fit_transfer
from model import FLIGHT_TEMP_C

DATA = os.path.join(os.path.dirname(__file__), "..", "data")

def masked_cowl(cowl):
    """Drop engine-run + cooldown samples used for the station->cowl regression."""
    m = engine_run_mask(cowl)
    return cowl[~m]

def coverage(cowl, metar):
    span_h = (cowl.index.max() - cowl.index.min()).total_seconds()/3600
    # station obs gaps > 3h
    gaps = metar.index.to_series().diff().dt.total_seconds()/3600
    big = gaps[gaps > 3]
    return {
        "cowl_span_days": round(span_h/24, 1),
        "cowl_samples": int(len(cowl)),
        "metar_obs": int(len(metar)),
        "metar_median_interval_min": round(float(gaps.median()*60), 1),
        "metar_gaps_over_3h": int(len(big)),
        "metar_max_gap_h": round(float(gaps.max()), 1) if len(gaps) else None,
    }

if __name__ == "__main__":
    cowl, metar = load_all()
    cm = masked_cowl(cowl)
    print("Coverage:", json.dumps(coverage(cowl, metar), indent=2))
    print(f"Engine-run mask removed {len(cowl)-len(cm)} samples "
          f"({100*(1-len(cm)/len(cowl)):.1f}%); fitting on {len(cm)}.\n")

    grid = "10min"
    bt = backtest(cm, metar, grid=grid, test_frac=0.3)
    full = fit_transfer(cm, metar, grid=grid)

    out = {"summary": full["summary"], "oos": bt["oos"],
           "temperature": full["temperature"], "moisture": full["moisture"],
           "grid": full["grid"], "n": full["n"], "lat": full["lat"], "lon": full["lon"],
           "engine_mask": {"flight_temp_c": FLIGHT_TEMP_C, "cooldown_h": 4.0,
                           "masked_samples": int(len(cowl)-len(cm))},
           "coverage": coverage(cowl, metar)}
    path = os.path.join(DATA, "kmrb_cowl_transfer.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2)

    print("=== FULL-OVERLAP FIT (summary) ===")
    for k, v in full["summary"].items():
        print(f"  {k:22s}: {v}")
    print("\n=== OUT-OF-SAMPLE (last 30% held out) ===")
    for k, v in bt["oos"].items():
        print(f"  {k:22s}: {v}")
    print(f"\nWrote {os.path.abspath(path)}")
