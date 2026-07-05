---
name: engine-moisture-analysis
description: >
  Compute internal-moisture and camshaft-corrosion exposure for a stored piston
  aircraft engine from temperature/humidity sensor data (e.g. an X-Sense logger in
  the cowl). Use this whenever the user wants to analyze engine-bay temp/RH logs,
  estimate condensation or time-of-wetness on the cam/lifters, detect condensation
  events, track moisture exposure since the last flight, decide whether storage
  corrosion is a concern, or power a live monitor/alert. Trigger on mentions of
  cam/camshaft corrosion, condensation in a crankcase, dew point on engine metal,
  time-of-wetness, hangar humidity, engine preservation/dehydrator/desiccant
  breather decisions, or "should I fly to dry the engine out" — even if the user
  doesn't name the model explicitly. Produces a deterministic JSON summary, charts,
  and (optionally) a report; the same model backs both on-demand analysis and the
  live alerting Worker.
---

# Engine internal-moisture / cam-corrosion exposure

This skill is the **single deterministic source of truth** for turning engine-bay
temperature and humidity readings into camshaft corrosion-exposure numbers. There is
no language model in the calculation — every figure is physics. The same
`scripts/model.py` is meant to be imported by a live monitoring backend (a scheduled
Home Assistant integration that alerts via Telegram) and run on-demand here for deep 
analysis and reports. Keep the model as the one reviewed implementation both consume.

Read `references/METHODOLOGY.md` before changing any constant or explaining a result —
it documents the two-inertia model and why each assumption holds.

## Presenting results — READ FIRST

These rules are mandatory for every run, including light / non-thinking models. Follow
them literally. They keep the tool credible: it reports physics, not opinions.

1. **Report only what the scripts output.** Every number must come straight from the
   `model.py` JSON or `gapfill.py` output. If a value is not in the script output, do not
   state it. No guessing, no estimating, no rounding into new claims, no filler.
2. **If you ran two scenarios and use AWOS data, label them and keep them separate. Never mix or average
   them:**
   - **Hangared** — hangar inertia fit applied (`gapfill` station→cowl transfer; the
     hangar buffers outside air).
   - **Tiedown** — no hangar buffering.
   - **Sensor data** - no gap-fill, just the raw sensor export (the cowl sensor is the best available data, so
     present it as its own scenario).
   Put each scenario's numbers under its own clear label. Do not imply one when the other
   was computed.
3. **Give no maintenance or service advice. NEVER recommend a ground run.** For any
   "should I fly / how often should I fly" question, do not answer it yourself — refer the
   user to Lycoming's official position:
   https://www.lycoming.com/content/frequency-flight-and-its-affect-engine with the caveat that the 
   model's data-aware grounding caution is a more nuanced take on their blanket "fly monthly" guidance. 
   If the user asks for interpretation, tell them to re-run this skill with the latest model 
   with **extended thinking and high effort enabled** — interpreting these numbers wrong 
   can mislead an owner about their engine.
4. **Do not add your own (LLM) conclusions or interpretation** unless the user explicitly
   asks for them. Present the script's numbers and charts and stop. If the user does ask for
   interpretation, tell them to re-run this skill with the latest model with **extended
   thinking and high effort enabled** — interpreting these numbers wrong can mislead an
   owner about their engine.
5. **Stay on the script output.** No climate analysis, no physics derivations, no weather
   speculation unless the user explicitly requests it.

## The model in one paragraph

Corrosion is driven by **time-of-wetness**, not grams of water. The cam/lifter steel
lags air temperature (metal thermal inertia, `tau_metal` ≈ 8 h). The crankcase only
breathes through restricted paths, so interior humidity lags ambient via **two parallel
paths**: a slow bulk path (`tau_bulk` ≈ 1 day, always on) plus a fast event path
(`tau_event` ≈ 1.5 h) that opens on **rising ambient vapour pressure** — moist air
flushing the breather onto a still-cold cam. A single lag averages those frontal
condensation events away; the event path is what lets the model see them. Condensation
occurs when interior vapour pressure exceeds saturation at the metal temperature. A film
mass-balance adds the post-event drying tail, with **asymmetric drying** (`dry_factor`
≈ 0.3 — water draining toward the immiscible oil re-evaporates slowly). Each hot run
resets the clock and refreshes the oil's inhibitor film; a **grounding caution** fires
only when a month grounded *followed real wetting* (a data-aware take on Lycoming's
blanket "fly monthly"). Flights are not distinguished from ground runs (KISS — the cowl
sensor can't confirm oil temperature).

## How to use it

### 1. Analyze a sensor export (most common)

```bash
python scripts/model.py /path/to/xsense_export.csv --tau-bulk-h 24 --tau-event-h 1.5 --json
```

The CSV needs three columns (names are matched loosely): a time column, temperature
in °F, and relative humidity in %. Output is a JSON summary with sub-dew-point hours
(**realistic** and **upper-bound**), film-hours, an honest condensed-mass figure,
flight detection, episodes, the grounding caution, and exposure **since the last flight**.

Always report **two numbers**: the realistic figure (two-path interior exchange) and
the upper bound (instant ingress, `k = ∞`). The gap between them is the protection the
restricted breathing provides — present it, don't hide it.

If you also run a gap-fill / synthesized path, label the scenarios separately —
**Hangared** (hangar inertia fit applied) vs **Tiedown** (no hangar buffering) — and keep
their numbers apart. Never blend them. (See "Presenting results — READ FIRST".)

### 2. Programmatic / from live readings

```python
from scripts.model import (from_records, regrid, analyze, episodes,
                           since_last_flight, grounding_caution, Params)
g = regrid(from_records(readings))         # readings: [{"time":..., "tf":..., "rh":...}, ...]
res, series = analyze(g, Params())         # two-path ingress + asymmetric drying by default
res["episodes"] = episodes(series)
res["since_last_flight"] = since_last_flight(series, res)
res["grounding_caution"] = grounding_caution(res["since_last_flight"])
```

### 3. Charts (for a report or a Telegram attachment)

```python
from scripts.charts import event_chart, seasonal_chart, dewpoint_divergence_chart
event_chart(series, center_time="2026-03-16T12:00", out="event.png")
seasonal_chart(series, out="seasonal.png")
dewpoint_divergence_chart(series, center_time="2026-03-16T12:00", out="divergence.png")
```

`dewpoint_divergence_chart` is the one to attach to an alert: it visually shows the
exterior humidity spiking while the lagged interior never reaches the cold cam.

### 4. Full written report

For a polished deliverable, generate the three charts, then assemble an HTML report
(steel-and-rust palette) and render to PDF with headless Chromium (embed fonts so it
is self-contained). Lead with the realistic/upper-bound pair, the seasonal profile,
the per-event persistence table, and the oil-reservoir caveat.

## Honesty rules for any alert or summary copy

These protect the tool's credibility — a nag-bot that cries wolf gets muted. They are in
addition to the mandatory rules in "Presenting results — READ FIRST" above.

- **Never give service advice and never recommend a ground run.** Route any "should I fly /
  how often" question to Lycoming's official position:
  https://www.lycoming.com/content/frequency-flight-and-its-affect-engine
- **Lead with wet-hours / film-hours, not water mass.** The realistic condensed mass
  is grams or less; never state ounces. "Your cam has logged ~6 damp-hours since you
  last flew" is credible; "breathed in half an ounce of water" is not.
- **Drive the social nudge off the ambient-damp index + days-since-flight**, and keep
  any moisture claim honestly small. The point is "it's been humid and you haven't
  flown — go fly," not a false corrosion scare.
- **Frame any weather/VFR suggestion as advisory flavor**, never a substitute for a
  real preflight weather briefing.
- **Surface the oil caveat when relevant**: ambient analysis can't see water dissolved
  in the oil; flying to temperature is what manages that.

## Sensor gap-fill fallback (feed drops)

If the cowl sensor drops out (e.g. lost connectivity through a humid spell), do NOT
feed raw nearest-station METAR into the model — the hangar/cowl buffers the outside
air, so raw METAR overstates swings and over-alerts. Instead use `scripts/gapfill.py`:

1. **Once**, fit the station→cowl transfer function on overlapping history
   (`fit_transfer` / `backtest`). The fitted params ARE the hangar's behavior: thermal
   lag (h), amplitude damping (<1 = sheltered), a **solar-gain term** (south-facing
   metal doors → warmer than ambient on clear days, the greenhouse effect), a
   **radiative-cooling term** (clear-night cooling, expect negative), moisture lag, and
   out-of-sample reconstruction RMSE. Sky cover is taken from observed ASOS codes and
   solar elevation is computed from lat/lon — both modulate the radiative terms.
2. **During a gap**, push live station METAR through that transfer (`synthesize_cowl`)
   to produce a buffered cowl estimate, mark those minutes `estimated=True`, and treat
   the resulting exposure as lower-confidence (widen alert thresholds or annotate).

The live monitor does this automatically: `pipeline.apply_gapfill()` detects holes and
a stale tail each cycle, fetches the station obs (`fetch_station_history`), synthesizes
through the saved fit (`data/<icao>_cowl_transfer.json`), and splices the estimate in
flagged `estimated=True` — shaded on the alert chart and noted in the alert copy.

The fitted transfer represents a **hangared** aircraft (the hangar buffers outside air).
If you present a buffered (hangared) result alongside an unbuffered (tiedown) one, label
each scenario clearly and keep the numbers separate — see "Presenting results — READ FIRST".

```bash
# estimator sanity check (no internet needed):
python scripts/gapfill.py export.csv
```

**If Claude can't download the ASOS data directly** (sandboxed/blocked network),
do NOT give up — offer the user the exact link to download it themselves:

1. Build the precise URL for their station and chosen period:
   ```python
   from scripts.gapfill import mesonet_url
   mesonet_url("KMRB", "2025-06-04", "2026-06-04")   # ICAO or 3-char id both fine
   ```
   `fetch_metar_archive()` already raises `MetarDownloadBlocked` carrying this URL on
   any network failure — surface that URL to the user verbatim.
2. Tell the user to open it in a browser (it returns a comma CSV) and provide the file.
3. Load it and proceed with the fit:
   ```python
   from scripts.gapfill import load_metar_csv, fit_transfer, backtest
   metar = load_metar_csv("/path/to/downloaded.csv")
   print(backtest(cowl, metar)["oos"])      # out-of-sample fit quality
   ```

The real fit needs historical ASOS; sources are documented at the top of
`gapfill.py` (Iowa Mesonet archive for the fit, aviationweather.gov for live METAR).

## Files

- `scripts/model.py` — deterministic core (psychrometrics, dual inertia, film budget,
  flight detection, since-last-flight, combustion estimate). Import it; don't reimplement.
- `scripts/charts.py` — event, seasonal, and dew-point-divergence charts.
- `scripts/gapfill.py` — station→cowl transfer fit, backtest, and gap synthesis.
- `references/METHODOLOGY.md` — derivation, constants, caveats. Read before editing.
