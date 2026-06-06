# Cowl ↔ KMRB station transfer fit — gap-fill calibration

**Engine:** Lycoming O-320, X-Sense cowl sensor · **Station:** KMRB ASOS (Eastern WV Regional, Martinsburg)
**Period:** 2025-06-04 → 2026-06-04 (365 days, 1-min cowl, hourly station) · **Fit grid:** 10 min
**Generated:** 2026-06-05 · physics from the `engine-moisture-analysis` skill (`model.py` / `gapfill.py`), unmodified.

This calibrates a station→cowl transfer function so that when the cowl feed drops, the
Home-Assistant monitor can synthesize a *buffered* cowl estimate from KMRB METAR instead of
feeding raw outside air into the corrosion model. Saved coefficients: [`data/kmrb_cowl_transfer.json`](../data/kmrb_cowl_transfer.json).

---

## Headline

| Term | Fitted | Physical meaning | Check |
|---|---|---|---|
| **Thermal lag** | **6 h** | Hangar + cowl thermal inertia (first-order low-pass τ) | ✓ hours |
| **Thermal damping** | **0.899** | Coefficient on the *lagged* station signal | ✓ 0–1 |
| **Solar gain** | **+4.3 °C** | South-door greenhouse at peak clear sun | ✓ > 0 |
| **Radiative cooling** | **+0.11 °C** | Clear-night sky cooling | ⚠️ ≈ 0 (expected < 0 — see below) |
| Temp RMSE / R² | 2.88 °C / 0.91 | In-sample temperature fit | — |
| **Moisture lag** | **1.5 h** | Dew-point tracks ambient with little delay | ✓ |
| **Moisture damping** | **0.972** | Dew point barely buffered (vapor is conserved/leaky) | ✓ |
| Dew-point RMSE | 1.92 °C | In-sample moisture fit | — |

### Out-of-sample (last 30 % held out — chronological, ≈ mid-Feb → Jun 2026)

| Metric | Value |
|---|---|
| Temp reconstruction RMSE | **3.05 °C** (bias **+1.38 °C**) |
| RH reconstruction RMSE | **8.5 %** (bias **−5.1 %**) |
| n (10-min samples scored) | 15,333 |

**Bottom line for gap-fill:** a synthesized cowl temperature good to ~3 °C and RH to ~8–9 %
out-of-sample. Usable as a *buffered* fallback — markedly better than feeding raw station air
(which would be off by the full ~5 °C night offset + the damped diurnal swing) — but not a
precision substitute for the real sensor. RH error is the one to watch near saturation (below).

---

## What the coefficients say

### Hangar inertia — the dominant effect
The 6-hour first-order lag is the headline. A 24-h diurnal cycle through a 6-h low-pass is
attenuated to ≈ 0.54 of its amplitude *before* the 0.899 damping coefficient is even applied,
so the **net cowl diurnal swing is only ~48 % of the station's**. That matches the overlays:
in October the station swings 6→25 °C (19 °C) while the cowl swings 13→25 °C (12 °C); in August
the station swings 19→34 °C while the cowl only does 24.5→33 °C. The hangar is a heavy thermal
flywheel — it lops the bottom off every cold night and rounds over every hot afternoon.

### Greenhouse — real and seasonally stable
`solar_gain = +4.3 °C` at peak clear sun, and it barely moves across the year
(first-half 4.44, second-half 4.34). This is the south-facing metal door loading the hangar
interior on sunny days. **Note the subtlety:** the *raw* daytime "cowl − ambient" average is
small and even slightly negative at peak summer sun (−0.35 °C, Aug 15–18) because the thermal
lag makes the cowl trail the fast summer upswing. The regression correctly separates the lag
from the greenhouse, recovering a clear positive solar term once the lag absorbs the delay.

### Night cooling — undetectable, and that's physical
`radiative_cool = +0.11 °C` — essentially zero, and it fails the expected-negative sign.
This is **not a fitting bug.** With a 6-h thermal mass the cowl barely cools overnight at all
(you can see the nearly-flat night curve in both overlays), so there is almost no thermal budget
for clear-vs-overcast sky condition to modulate. Three independent lines of evidence:

1. The **first-half/second-half drift** flips it (+0.25 → −0.05) — i.e. it's noise around zero,
   not a stable physical coefficient.
2. **Residual-vs-hour-of-day is dead flat** (±0.3 °C across all 24 h) — no systematic
   night/day bias left unmodeled.
3. **Residual-vs-cloud at night has no negative slope at low cloud** — there is no leftover
   clear-night cooling signal for the term to have captured.

So the hangar's thermal mass **swamps** radiative cooling; dropping the term would not hurt the
fit. (Kept for now since it's harmless and the skill's machinery expects it.)

### Moisture — nearly a pass-through with a short lag
Dew point tracks ambient with only a 1.5-h lag and 0.972 damping: water vapor is roughly
conserved and the cowl/hangar leaks freely, so interior dew point ≈ exterior dew point shifted
by ~90 min. RH is then reconstructed from synthesized T and Td — which is why RH error inflates
when temperature error is largest (RH is steep near saturation).

---

---

## Humidity — does it behave like temperature?

Short answer: **the *driving* terms do not carry over, and that's physically correct — but
humidity inherits temperature's effects through RH, and it has two structures of its own that the
linear model can't see.** Empirical residual analysis (dew point and RH, full year):

### Vapor is a near-pass-through, not a flywheel
Unlike heat, the hangar does not *store* water vapor, so there is no greenhouse/radiative analog
to fit — and the data agrees:

| Moisture residual structure | Magnitude | vs temperature |
|---|---|---|
| Day vs night dew-point resid | −0.32 °C / +0.30 °C | tiny (temp night offset was ±5 °C) |
| Correlation with cloud fraction | 0.15 | weak — **no greenhouse/radiative term needed** |
| Dew-point RMSE (clear / overcast week) | 0.98 / 0.86 °C | dew point predicts *better* than temperature |

The dew-point fit is a short 1.5-h lag at 0.972 damping — essentially ambient dew point shifted
~90 min. See [`diag_moist_predvactual_clear.png`](diag_moist_predvactual_clear.png) and
[`diag_moist_resid_cloud.png`](diag_moist_resid_cloud.png) (flat across cloud except a small
heavy-overcast uptick where rain/saturation adds vapor).

### Temperature reaches humidity through RH
RH is reconstructed as `RH = f(synth T, synth Td)`, so all of the temperature behavior — the warm
cowl, the damped swing — propagates into RH. This is why the **RH residual carries a mild diurnal
wave (±2.5 %)** ([`diag_rh_resid_hour.png`](diag_rh_resid_hour.png)) even though the *temperature*
residual was dead flat: RH amplifies small temperature errors. Net RH RMSE is 7.7 % in-sample,
8.5 % OOS.

### Two moisture-specific effects the linear model misses
1. **Winter condensation/frost sink.** Dew-point residual swings **positive in Dec/Jan (+0.65 to
   +0.9 °C)** — the model *over-predicts* moisture in winter
   ([`diag_moist_resid_season.png`](diag_moist_resid_season.png)). Fingerprint of vapor being
   pulled out onto cold surfaces (the exact corrosion process), which a linear lag cannot represent.
2. **Near-saturation RH runs low — and it errs optimistic.** In the corrosion-relevant regime
   (actual RH > 85 %, 2.5 % of the year), synthesized **RH reads −5.4 % below actual** (driven by
   the temperature warm-bias inflating the denominator `esat(T)`). So during gap-fill the
   synthesized humidity is *least conservative exactly when wetness risk is highest.*

**Recommendation for the HA monitor:** when running on synthesized (gap-fill) data near
saturation, apply a small conservative RH nudge (≈ +5 %) or raise a "low-confidence near
saturation" flag, so the fallback does not under-call time-of-wetness. Stats:
[`data/moisture_diag.json`](../data/moisture_diag.json).

---

## Validation / acceptance

| Criterion | Result |
|---|---|
| `solar_gain > 0` | ✓ +4.3 °C |
| `radiative_cool < 0` | ✗ +0.11 (≈0; argued physical above) |
| `0 < thermal_damping < 1` | ✓ 0.899 |
| lag in hours | ✓ 6 h (temp), 1.5 h (moisture) |
| Coefficients stable across held-out season | ✓ lag 6↔6 h, damping 0.86↔0.83, **solar_gain 4.44↔4.34**; only the ≈0 radiative term flips |
| OOS temp RMSE useful | ✓ 3.05 °C |
| OOS RH RMSE useful | ⚠️ 8.5 % — usable but flag near saturation |

### Live validation (worked example)
A live spot-check on **2026-06-05 19:50 EDT**: the fitted transfer was driven with the recent
KMRB history (to settle the 6-h lag) and the final synthesized value compared against the actual
cowl sensor.

| Channel | Station input | Predicted cowl | Actual cowl | Error |
|---|---|---|---|---|
| Temperature | 82.0 °F (27.8 °C) | 82.7 °F (28.2 °C) | **83.1 °F (28.4 °C)** | −0.4 °F / −0.2 °C |
| Humidity | 51 % RH (ambient) | 42 % RH | **42.4 % RH** | −0.4 % RH |

Both channels landed well inside sensor noise and far better than the OOS RMSE — **but this was a
calm, clear, stable evening, the model's easiest regime**, and a single draw can land anywhere in
the ±3 °C distribution. The value of the check is directional: the cowl ran +1.1 °F *above* ambient
(thermal inertia) and *drier* than ambient (42.4 % vs 51 %), both correctly predicted in sign and
rough magnitude. Do not read a fair-weather 0.4 °F hit as the expected winter/frontal accuracy.

### Seasonal drift detail
Fit quality degrades in the colder/spring half (temp RMSE 2.06 → 3.27 °C, dew-point RMSE
0.93 → 2.52 °C) and the OOS reconstruction carries a **+1.38 °C warm bias** in the held-out
spring. The *shape* coefficients (lag, damping, greenhouse) are stable; it's the absolute fit
that loosens in winter/spring — likely sharper frontal passages and snow-cover/low-sun-angle
radiative regimes the single-station linear transfer can't fully track. For production the warm
bias is small relative to the gap-fill use case, but a season-aware bias offset is an option.

---

## Charts

| File | Shows |
|---|---|
| [`overlay_3day.png`](overlay_3day.png) | Oct 1–4 alignment check — phase lock at solar noon, strong night damping |
| [`overlay_3day_august.png`](overlay_3day_august.png) | Aug 15–18 — lag-dominated summer upswing; cloud-event smoothing |
| [`diag_predvactual_clear.png`](diag_predvactual_clear.png) | Clear week (2025-09-30, cloud 0.03): predicted vs actual, RMSE 1.92 °C |
| [`diag_predvactual_overcast.png`](diag_predvactual_overcast.png) | Overcast week (2026-05-20, cloud 0.82): RMSE 1.21 °C |
| [`diag_resid_hour.png`](diag_resid_hour.png) | Residual vs hour-of-day — flat ≈ diurnal shape fully captured |
| [`diag_resid_cloud.png`](diag_resid_cloud.png) | Residual vs cloud fraction (day/night) — no leftover radiative signal |
| [`diag_scatter.png`](diag_scatter.png) | Cowl-vs-station scatter (r=0.929), flatter-than-1:1 = damping |
| [`diag_moist_predvactual_clear.png`](diag_moist_predvactual_clear.png) | Dew point predicted vs actual, clear week (RMSE 0.98 °C) |
| [`diag_moist_predvactual_overcast.png`](diag_moist_predvactual_overcast.png) | Dew point predicted vs actual, overcast week (RMSE 0.86 °C) |
| [`diag_rh_resid_hour.png`](diag_rh_resid_hour.png) | RH residual by hour — mild diurnal wave inherited from temperature |
| [`diag_moist_resid_cloud.png`](diag_moist_resid_cloud.png) | Dew-point & RH residual vs cloud — weak slope (no vapor flywheel) |
| [`diag_moist_resid_season.png`](diag_moist_resid_season.png) | Monthly moisture residual — winter condensation/frost sink |

---

## Coverage & method notes

- **Cowl:** two X-Sense half-year exports concatenated, boundary overlap de-duplicated →
  520,721 1-min samples, full 365-day span, no long internal gaps.
- **Timezone (gotcha #1):** cowl timestamps are local `America/New_York` (DST-aware); localized
  → UTC before fitting. Verified: solar-noon median **17.17 UTC** (expected ~17:00 at KMRB),
  daytime cowl−ambient positive. No 4–5 h phase error.
- **Engine-run mask (gotcha #2):** samples with cowl T > 40 °C (`FLIGHT_TEMP_C`) plus a 4-h
  cooldown tail removed from the fit — 6,363 samples (1.2 %). These are engine-driven, not
  weather, and would corrupt the station→cowl regression.
- **Station:** 10,427 hourly obs; median interval 60 min; 24 gaps > 3 h (max 50 h). Cloud
  fraction from **observed ASOS sky-condition codes** (skyc1/2/3), not forecast sky cover.
  No interpolation across long gaps; station was interpolated only up to 12×10-min within the
  fit grid.

## Limitations

- Single-station linear transfer; one micro-site (one hangar, one cowl position).
- `radiative_cool` is below the noise floor here — do not read physical meaning into its sign.
- Winter/spring fit is looser (≈ +1.4 °C warm bias OOS); consider a season-aware offset if the
  gap-fill is used heavily in those months.
- RH is derived from synthesized T and Td, so RH error is largest exactly where it matters most
  for corrosion (cold, near-saturated air): synthesized RH runs ~5 % *low* above 85 % actual RH.
  Treat synthesized RH as indicative, not authoritative, and consider a conservative near-saturation
  nudge/flag in the monitor (see Humidity section).
- Moisture is modeled as lagged dew point only; the winter condensation/frost sink (vapor removed
  onto cold surfaces) is not represented, so synthesized winter moisture skews slightly high.
- **This is an engineering estimate for gap-fill, not maintenance authority.**
