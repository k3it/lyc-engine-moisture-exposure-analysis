# lyc-engine-moisture-exposure-analysis

A Claude Skill (and standalone Python model) that turns engine-bay temperature/humidity
logs into **camshaft corrosion-exposure** numbers for a stored piston aircraft engine —
e.g. a Lycoming O-320 in a T-hangar with an X-Sense sensor in the cowl.

The physics is fully deterministic (no LLM in the calculation). The same model backs
both on-demand analysis/reporting and a live Home-Assistant monitor that can nudge you
to go fly when it's been damp.

## Why time-of-wetness, not "grams of water"

Corrosion is driven by how long the cam/lifter steel holds a liquid film. 
The model computes that from the measured air, accounting for two
inertias that a naive dew-point comparison would miss:

1. **Metal thermal inertia** (`tau_metal` ≈ 8 h) — the buried cam lags air temperature.
2. **Air-exchange inertia — two parallel paths.** The crankcase only breathes through
   restricted paths, so interior humidity lags ambient. A *slow bulk* path
   (`tau_bulk` ≈ 1 day: diffusion + mean thermal breathing) sets the quiet-weather floor;
   a *fast event* path (`tau_event` ≈ 1.5 h, gated on **rising ambient vapour pressure**)
   captures the breather flushing when a moist air mass moves in onto a still-cold cam.
   A single lag averages those frontal condensation events away (it predicts *zero*
   wet-hours on the KMRB record); the event path recovers them — ~17 realistic sub-dew
   hours/yr, concentrated in late winter/early spring.

A film mass-balance adds the post-event **drying tail**, with **asymmetric drying**
(`dry_factor` ≈ 0.3): water that drains toward the immiscible, denser oil re-evaporates
far slower than it condensed. Each hot run resets the exposure clock and refreshes the
oil's corrosion-inhibitor film; a **conditional grounding caution** flags a month on the
ground only when it followed real wetting (improving on Lycoming's blanket "fly often, every 
week, month" in any geography).

See [`references/METHODOLOGY.md`](references/METHODOLOGY.md) for the full derivation,
constants, and caveats (including the oil-borne moisture reservoir the bay sensor can't
see — which is why flying to temperature is the real mitigation).

## Quick start

```bash
pip install pandas numpy matplotlib
python scripts/model.py your_sensor_export.csv --tau-bulk-h 24 --tau-event-h 1.5 --json
```

The CSV needs three columns (matched loosely): a time column, temperature (°F), and
relative humidity (%). Output is a JSON summary: sub-dew-point hours (**realistic** and
**upper-bound**), film-hours, an honest condensed-mass figure, flight detection, wet
episodes, and exposure **since the last flight**.

```python
from scripts.model import (load_csv, regrid, analyze, episodes,
                           since_last_flight, grounding_caution, Params)
from scripts.charts import event_chart, seasonal_chart, dewpoint_divergence_chart

g = regrid(load_csv("export.csv"))
res, series = analyze(g, Params())          # two-path ingress + asymmetric drying by default
res["episodes"] = episodes(series)
res["since_last_flight"] = since_last_flight(series, res)
res["grounding_caution"] = grounding_caution(res["since_last_flight"])
dewpoint_divergence_chart(series, "2026-03-16T12:00", "alert.png")
```

## Sensor gap-fill (nearest-station fallback)

When the cowl feed drops, don't feed raw METAR in — the hangar buffers the outside air.
`scripts/gapfill.py` fits a **station→cowl transfer function** (thermal lag, amplitude
damping = sheltering, a solar-gain/greenhouse term for south-facing metal doors, a
clear-night radiative-cooling term, and moisture lag), then synthesizes a buffered cowl
estimate from live METAR during gaps.

Cloud cover comes from **observed ASOS sky-condition codes** (CLR/FEW/SCT/BKN/OVC),
mapped to an effective cloud fraction; solar elevation is computed from lat/lon. Build
the exact historical-data download link with:

```python
from scripts.gapfill import mesonet_url, load_metar_csv, backtest
mesonet_url("KMRB", "2025-06-04", "2026-06-05")   # Iowa Mesonet ASOS, comma CSV
metar = load_metar_csv("downloaded.csv")
print(backtest(cowl, metar)["oos"])               # out-of-sample fit quality
```

If direct download is blocked (sandboxed Claude), `fetch_metar_archive()` raises with
that URL so it can be handed to the user to download in a browser.

**The live monitor applies this automatically.** Each cycle, `pipeline.apply_gapfill()`
detects data holes and a stale tail (sensor offline > `gapfill_stale_min`, default
90 min), pulls station METAR for the outage (Iowa Mesonet archive + the
aviationweather.gov live cache), pushes it through the saved transfer fit
([`data/kmrb_cowl_transfer.json`](data/kmrb_cowl_transfer.json)) and splices the
buffered estimate in, flagged `estimated=True`. Synthesized spans are shaded on the
alert chart, noted in the Telegram copy, counted toward the exposure tally (with a
conservative near-saturation RH bump — see the
[backtest report](reports/cowl_station_backtest.md)), and surfaced as a
`Cowl sensor offline` problem entity in Home Assistant.

## Files

| File | Purpose |
|---|---|
| `SKILL.md` | Skill manifest + usage instructions |
| `scripts/model.py` | Deterministic core: psychrometrics, dual inertia, film budget, flight detection |
| `scripts/charts.py` | Event, seasonal, and dew-point-divergence charts |
| `scripts/gapfill.py` | Station→cowl transfer fit (cloud/solar), backtest, gap synthesis |
| `references/METHODOLOGY.md` | Derivation, constants, caveats |

## Install as a Claude Skill

Package with the skill tooling, or copy `SKILL.md` + `scripts/` + `references/` into your
skills directory. In Claude Code / Cowork it triggers on engine-bay humidity, cam
corrosion, condensation, time-of-wetness, and engine-preservation questions.

## Caveats

- `tau_metal`, `tau_bulk`, and `tau_event` are modeled, not measured; a lifter-boss probe
  + crankcase RH sensor would replace them directly.
- The bay sensor cannot see water dissolved in the oil — an internal humidity floor that
  only running the engine clears.
- Flights are not distinguished from ground runs (the cowl sensor can't confirm oil
  temperature, and is hung by hand after shutdown); the model trusts a diligent pilot to
  fly long enough to boil moisture out.
- Weather/VFR outputs in any downstream monitor are advisory; defer to a real preflight
  briefing.

## License

MIT — see [`LICENSE`](LICENSE).

*Not affiliated with Lycoming or Textron. Use at your own risk; this is an engineering
estimate, not maintenance authority.*
