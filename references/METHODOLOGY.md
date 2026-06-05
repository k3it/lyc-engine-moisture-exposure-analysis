# Methodology — engine internal-moisture / cam-corrosion exposure

This documents the physics behind `scripts/model.py`. Read it when you need to
explain a result, justify a constant, change an assumption, or defend the model.

## What we are estimating

Corrosion of a stored piston aero-engine camshaft and lifters is driven by
**time-of-wetness** — how long the steel holds a liquid film — not by the total
grams of water. The model converts a stream of `(time, temperature, RH)` readings
from a sensor in the engine bay into the hours the cam spends wet, plus an honest
(small) condensed-mass figure.

## Two inertias — both matter

A naive model compares the bay dew point directly to the bay temperature. That is
wrong in two ways, both of which we correct with first-order lags:

1. **Metal thermal inertia (`tau_metal`, default 8 h).** The cam/lifters are steel
   buried in a large aluminium case; they lag air temperature. C ≈ 85 kJ/K for
   ~116 kg of mixed metal; cowled natural convection hA ≈ 4–10 W/K, so
   τ ≈ 2.5–6 h for the outer case and **8–10 h for the buried cam** (the
   conservative worst case used by default). The cam warming slowest is exactly
   why it is the corrosion casualty.

2. **Air-exchange inertia (`tau_air`, default 24 h).** The crankcase is not open to
   the atmosphere. Outside air reaches it only through the breather tube (the one
   genuinely open path), past the rings to the exhaust (tortuous), or through a
   filtered-and-often-plugged intake (nearly sealed). So the **interior water-vapour
   content is a heavily low-pass-filtered version of ambient.** We filter the
   transported quantity — vapour pressure — not dew point or absolute humidity,
   because vapour pressure (∝ mixing ratio) is what actually moves through the
   openings independent of local temperature.

   Physical scale of `tau_air`: bulk thermal breathing exchanges only ~1–2 % of
   crankcase volume per day (the metal swings just a few °C → ΔV/V ~1 %); molecular
   diffusion up a ~0.7 m breather tube is a multi-week process; convective exchange
   through the breather is the wildcard. Realistic `tau_air` is **~1 day to 1 week**.

## Condensation criterion

Condensation onto the cam occurs when the **interior vapour pressure exceeds the
saturation vapour pressure at the (lagged) metal temperature**:

    e_int(t) > e_sat(T_metal(t))   ⇔   Td_int(t) > T_metal(t)

`model.analyze()` reports this two ways:
- **realistic** — using `e_int` (interior, lagged by `tau_air`).
- **upper bound** — using `e_ext` (instant air exchange, `tau_air = 0`). This is the
  old naive number and is useful as a conservative "how damp has it been" index.

## Film mass balance (persistence / the drying tail)

Sub-dew-point hours undercount the damage: a condensed film does not vanish when the
metal warms back up — it evaporates only as fast as the vapour-pressure deficit
allows, and spring air stays humid. We integrate a film budget per unit area:

    film += h_m · (AH_int − AH_sat(T_metal)) · dt      (condense if +, evaporate if −)
    film clamped to [0, FILM_CAP]                       (gravity drainage off lobes)

with mass-transfer coefficient `h_m ≈ 0.003 m/s` (enclosed natural convection,
Chilton–Colburn from h ≈ 3–5 W/m²K). **Film-present hours** (film > ~monolayer) is
the corrosion-relevant time-of-wetness; it exceeds sub-dew-point hours by the drying
tail (≈ +17 % over a year, up to +60 % within a single humid event).

## Honest condensed mass

Mass is reported (`condensed_mass_g`) for completeness but is deliberately not the
headline: integrated condensation flux × internal wetted area (~0.3 m²) yields
**grams or less** over a damp spell, and **near zero** once `tau_air` is realistic.
A one-shot combustion residual of **~1 g** is trapped at each shutdown
(`combustion_water_per_shutdown_g`), independent of breathing. Any alert copy should
lead with wet-hours, never inflate the water figure (e.g. "half an ounce" ≈ 14 g is
physically impossible here and would destroy the tool's credibility).

## Flight detection and the "since last flight" clock

Cowl air above `flight_temp_c` (40 °C) means the engine ran. Each run **resets the
cumulative clock** and **refreshes the oil's corrosion-inhibitor film** (the polar
additive re-forms on hot shutdown). The monitoring headline is therefore exposure
*since the last flight*.

## Known limitations / caveats

- **The oil is an internal moisture source the bay sensor cannot see.** Water
  dissolved in the oil sets a humidity floor inside the case independent of ambient;
  air-exchange inertia filters *outside* moisture but does nothing about water already
  in the oil. This is why the dominant real mitigation is **flying to full operating
  temperature to boil water out of the oil**, not ambient humidity control.
- `tau_metal` and `tau_air` are modeled, not measured. A lifter-boss temperature
  probe and a crankcase RH sensor would replace them directly.
- The film model omits the condensation sink on interior humidity, so realistic
  numbers are conservative (true exposure is if anything lower).
- The combustion water fraction (15 % rich) is an upper bound; blow-by dilutes it.

## Default constants (see `model.Params`)

| Constant | Default | Meaning |
|---|---|---|
| `tau_metal_s` | 8 h | cam thermal time constant |
| `tau_air_s` | 24 h | interior air-exchange time constant |
| `hm` | 0.003 m/s | mass-transfer coefficient |
| `film_cap` | 15 g/m² | drainage-limited max film |
| `wet_area_m2` | 0.30 m² | internal wetted steel area |
| `crankcase_v` | 0.010 m³ | crankcase free-gas volume |
| `flight_temp_c` | 40 °C | run-detection threshold |
