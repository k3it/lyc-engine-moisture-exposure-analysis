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

2. **Air-exchange inertia — modeled as TWO PARALLEL PATHS, not one lag.** The crankcase
   is not open to the atmosphere; outside air reaches it through the breather tube (the
   one genuinely open path), past the rings to the exhaust (tortuous), or through a
   filtered-and-often-plugged intake (nearly sealed). So the **interior water-vapour
   content is a low-pass-filtered version of ambient.** We filter the transported
   quantity — vapour pressure — not dew point or absolute humidity, because vapour
   pressure (∝ mixing ratio) is what actually moves through the openings independent of
   local temperature.

   A *single* time constant cannot represent the exchange, because two physically
   distinct processes act on very different timescales:

   - **Slow bulk path (`tau_bulk`, default 24 h).** Diffusion plus mean thermal
     breathing: the metal swings a few °C → ΔV/V ~1 % → only ~1–2 % of crankcase volume
     is exchanged per day; molecular diffusion up the ~0.7 m breather is multi-week.
     This is always on and sets the quiet-weather floor. (1 day is the lower end of the
     methodology's old `~1 day–1 week` estimate; the upper end is rejected empirically —
     a multi-day bulk lag makes the interior *retain* humidity for days and, with no
     condensation sink modeled, over-condenses to tens of grams, which is not credible.)
   - **Fast event path (`tau_event`, default 1.5 h), gated on rising ambient vapour
     pressure.** When a moist air mass moves in, wind and barometric swings flush the
     open breather within an hour or two — exactly the moment a cold cam (lagging at the
     prior temperature) meets fresh humid air and dews up. The gate is the rising-vapour
     rate `(d e_ext/dt)_+` normalized by `event_ref` (default 1.0 hPa/h, ≈ the 95th
     percentile of moist-front rates in the KMRB record); it is **0 while ambient dries**
     (no fast ingress) and saturates at 1 during a strong front:

         g(t)  = clip( (d e_ext/dt)_+ / event_ref , 0, 1 )
         k(t)  = dt/tau_bulk + (dt/tau_event)·g(t)
         e_int = e_int + k(t)·(e_ext_prev − e_int)

   **Why this matters:** a single 24 h lag averages frontal condensation away entirely —
   on the KMRB cowl record it predicts **0 wet-hours all year**, because condensation
   needs the moisture to outrun the cold metal (`tau_air < tau_metal = 8 h`) and 24 h is
   3× too slow. Adding the event path (bulk unchanged at 24 h) recovers **~17 realistic
   sub-dew hours/yr**, concentrated in **Feb–Mar** (cold winter metal meeting the first
   warm humid spring air) — physically where stored-engine condensation actually occurs.
   The instant-ingress bound (`k = ∞`) is 48 h, so the two-path realistic number sits
   sensibly between the dead single-lag floor and that ceiling.

## Condensation criterion

Condensation onto the cam occurs when the **interior vapour pressure exceeds the
saturation vapour pressure at the (lagged) metal temperature**:

    e_int(t) > e_sat(T_metal(t))   ⇔   Td_int(t) > T_metal(t)

`model.analyze()` reports this two ways:
- **realistic** — using `e_int` (interior, two-path exchange).
- **upper bound** — using `e_ext` (instant air exchange, `k = ∞`). The old naive number,
  useful as a conservative "how damp has it been" index.

## Film mass balance (persistence / the drying tail)

Sub-dew-point hours undercount the damage: a condensed film does not vanish when the
metal warms back up — it evaporates only as fast as the vapour-pressure deficit
allows, and spring air stays humid. We integrate a film budget per unit area:

    film += h_m · (AH_int − AH_sat(T_metal)) · dt      (condense if +, evaporate if −)
    film clamped to [0, FILM_CAP]                       (gravity drainage off lobes)

with mass-transfer coefficient `h_m ≈ 0.003 m/s` (enclosed natural convection,
Chilton–Colburn from h ≈ 3–5 W/m²K). **Film-present hours** (film > ~monolayer) is
the corrosion-relevant time-of-wetness; it exceeds sub-dew-point hours by the drying
tail.

**Drying is asymmetric (`dry_factor`, default 0.3).** Condensed water does not just
re-evaporate the way it arrived. Water is denser than oil and immiscible — oil cannot
dissolve it — so a droplet that drains off a cam lobe into a low spot or the oil sits
*below* the oil film and is largely cut off from the breather vapour path; it leaves
mainly when a flight boils it out, not when ambient air dries. We model this by
evaporating the film at `dry_factor ×` the condensation rate (drying ~3× slower than
wetting). On the KMRB record this raises annual time-of-wetness from 22.8 h (symmetric)
to **31.9 h (+40 %)** — the persistence of drained-but-not-evaporated water. We do *not*
add a separate "grams trapped in oil" figure: the integrated mass is <1 g and a scary
water number would destroy credibility (see below).

## Honest condensed mass

Mass is reported (`condensed_mass_g`) for completeness but is deliberately not the
headline: integrated condensation flux × internal wetted area (~0.3 m²) yields about
**10 g over a full year** with the two-path model (and is wildly over-stated by any
multi-day `tau_bulk` — 5-day bulk gives 136 g, the artifact that pins `tau_bulk` to the
~1-day floor). A one-shot combustion residual of **~1 g** is trapped at each shutdown
(`combustion_water_per_shutdown_g`), independent of breathing. Any alert copy should
lead with wet-hours, never inflate the water figure (e.g. "half an ounce" ≈ 14 g is
physically misleading here and would destroy the tool's credibility).

## Flight detection and the "since last flight" clock

Cowl air above `flight_temp_c` (40 °C) means the engine ran. Each run **resets the
cumulative clock** and **refreshes the oil's corrosion-inhibitor film** (the polar
additive re-forms on hot shutdown). The monitoring headline is therefore exposure
*since the last flight*.

**We deliberately do NOT try to distinguish flights from ground runs (KISS).** It is
tempting to gate the reset on a "real" warm-up (Lycoming: water leaves the oil only at
~165 °F oil temp, which a brief ground run cannot reach). But (a) the cowl sensor reads
*air*, not oil, and in this installation peaks at only ~50 °C even on a 4½-hour flight —
it is range-/placement-limited and cannot confirm oil temperature; and (b) the sensor
is hung by hand *after* shutdown, so a genuine flight can register as a short or absent
spike (land, button up later). Duration-gating would therefore mostly produce *false
negatives on real flights* — worse than trusting the run. We instead assume a diligent
pilot flies long enough to boil moisture out (the article's own guidance); the rare
deliberate ground-run-and-walk-away is out of scope.

**Conditional grounding caution (`grounding_caution`) — improving on Lycoming.** The
frequency-of-flight article says "fly at least ~monthly," but that is a worst-case,
geography-blind rule for pilots with *no* condensation data. With exposure tracked we do
better: a quiet month in a dry spell earns no nag. The caution fires only when a month
on the ground **followed real wetting** —

    caution ⇔ (time-of-wetness since last flight ≥ WET_CAUTION_H, default 8 h)
            ∧ (days since last flight             ≥ FLIGHT_LIMIT_D, default 30 d)

i.e. the "fly *smarter*, not just more often" case — a timely flight right after a
condensation spell purges the water before it sits. Oil-acid aging is a separate
*calendar* process (combustion water → acid over months); the 4-month oil change remains
the pilot's own backstop and is intentionally **not modeled** (it needs a manual
oil-change date and adds no analytic value here).

## Known limitations / caveats

- **The oil is an internal moisture source the bay sensor cannot see.** Water
  dissolved in the oil sets a humidity floor inside the case independent of ambient;
  air-exchange inertia filters *outside* moisture but does nothing about water already
  in the oil. This is why the dominant real mitigation is **flying to full operating
  temperature to boil water out of the oil**, not ambient humidity control.
- `tau_metal`, `tau_bulk`, `tau_event` are modeled, not measured. A lifter-boss
  temperature probe and a crankcase RH sensor would replace them directly.
- The `tau_event` gate (rising vapour pressure) is a *proxy* for breather flushing
  during frontal/windy weather; it is not a measured flow. `event_ref` is calibrated to
  the KMRB rising-vapour distribution, not derived from first principles.
- The film model omits the condensation sink on interior humidity, so realistic
  numbers are conservative — and this is exactly why `tau_bulk` is held at the ~1-day
  floor: a slower bulk lag retains interior humidity for days and, lacking the sink,
  over-condenses (the 136 g artifact).
- The combustion water fraction (15 % rich) is an upper bound; blow-by dilutes it.

## Default constants (see `model.Params`)

| Constant | Default | Meaning |
|---|---|---|
| `tau_metal_s` | 8 h | cam thermal time constant |
| `tau_bulk_s` | 24 h | slow bulk-ingress time constant (always on) |
| `tau_event_s` | 1.5 h | fast frontal-flush ingress, gated on rising vapour |
| `event_ref_hpa_h` | 1.0 hPa/h | rising-vapour rate that fully opens the event path |
| `dry_factor` | 0.3 | film evaporation/condensation rate ratio (asymmetric drying) |
| `hm` | 0.003 m/s | mass-transfer coefficient |
| `film_cap` | 15 g/m² | drainage-limited max film |
| `wet_area_m2` | 0.30 m² | internal wetted steel area |
| `crankcase_v` | 0.010 m³ | crankcase free-gas volume |
| `flight_temp_c` | 40 °C | run-detection threshold |
| `tau_air_s` | 24 h | legacy single-tau lag (only if `two_path=False`) |

The `## What we are estimating` and condensation-criterion sections above are unchanged
by the two-path / asymmetric-drying update; only the air-exchange model and the drying
tail were revised. A conditional grounding caution and an explicit no-ground-run-gating
decision were added (see "Flight detection").
