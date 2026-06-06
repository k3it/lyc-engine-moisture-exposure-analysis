# Home Assistant: engine-moisture monitor + "want to go flying?" nudge

Turns the on-demand moisture model (`../scripts/model.py`) into a live Home
Assistant automation. On a schedule it pulls your X-Sense cowl temp/humidity from
HA's recorder, runs the **canonical** physics model over a trailing multi-day
window (needed to spin up the 8 h metal lag and the 24 h / 1.5 h interior-air
lags), publishes cam-wetness sensors, and — when your cam has sipped enough water
since the last flight *and* there's a calm VFR window soon — sends a friendly
Telegram nudge with charts.

The hot-run signature is already in the model: cowl air `> flight_temp_c` (40 °C)
marks an engine run, and the tally is measured **since that run**, so it reads as
"what your cam has accumulated since you last flew."

## How the model stays in sync (no copies)

`../scripts/model.py` and `../scripts/charts.py` stay the **single source of
truth**. The app does **not** copy them — `moisture_monitor.py` adds `scripts/` to
`sys.path` and imports them. So on the HA box you deploy the **whole repo**, and a
`git pull` updates the physics; AppDaemon hot-reloads the app and picks it up.

If you can't place the repo so that `scripts/` sits at `<repo>/scripts` relative to
the app, set the env var `LYC_SCRIPTS_DIR` to the directory containing `model.py`.

## Files

```
homeassistant/
  appdaemon/apps/
    apps.yaml            # the adjustable config (sensors, airport, params, thresholds)
    moisture_monitor.py  # the AppDaemon app (orchestration) + pure, testable helpers
    weather.py           # TAF (aviationweather.gov) + NWS multi-day VFR/calm windows
  test_local.py          # offline end-to-end check against the repo's real CSV
  README.md
```

## One-time setup

1. **Deploy the repo to HA.** Clone/copy this whole repo onto the HA host, e.g. to
   `/config/lyc-engine-moisture-exposure-analysis`. (Git pull here = model updates.)

2. **AppDaemon add-on.** Install the *AppDaemon* add-on. In its add-on config add
   the Python deps and point it at the app dir:
   ```yaml
   # AppDaemon add-on configuration
   python_packages:
     - pandas
     - numpy
     - matplotlib
   # 'requests' isn't needed: weather.py uses stdlib urllib.
   ```
   Then in `appdaemon/appdaemon.yaml` set the apps directory to this repo's app
   folder (or symlink/copy `apps.yaml` + the two `.py` files into your existing
   AppDaemon `apps/` dir — but prefer pointing at the repo so the model stays
   linked):
   ```yaml
   appdaemon:
     app_dir: /config/lyc-engine-moisture-exposure-analysis/homeassistant/appdaemon/apps
     # ...your latitude/longitude/time_zone/plugins as usual...
   ```
   > If your AppDaemon already has an `app_dir` you don't want to change, instead
   > set `LYC_SCRIPTS_DIR` in the add-on environment to
   > `/config/lyc-engine-moisture-exposure-analysis/scripts` and copy the three
   > files in `homeassistant/appdaemon/apps/` into your apps dir.

3. **Google Generative AI (Gemini).** Settings → Devices & Services → Add
   Integration → *Google Generative AI*. Paste your Gemini key. Store it as a
   secret rather than inline:
   ```yaml
   # secrets.yaml
   gemini_api_key: <your-key>
   ```
   ⚠️ Rotate the key you pasted into chat earlier once this is wired.
   The app calls the service `google_generative_ai_conversation/generate_content`.
   If your HA version exposes a different service id, set `gemini_service:` in
   `apps.yaml`. (If composition fails for any reason, the app sends a templated
   message instead — it never goes silent.)

4. **Telegram.** You already have `telegram_bot` configured. Put your chat id in
   AppDaemon's `secrets.yaml` as `telegram_chat_id` (referenced by `apps.yaml`).
   Charts are written to `www_dir` (default `/homeassistant/www/moisture`) and sent
   with `telegram_bot/send_photo`; make sure that path is allowed for the bot
   (it's under `<config>/www`, served at `/local/...`).

5. **Find your X-Sense entity ids.** Developer Tools → States, filter for your
   sensor, or query the API with the long-lived token:
   ```bash
   curl -s -H "Authorization: Bearer <TOKEN>" \
     http://homeassistant:8123/api/states | \
     python -c "import sys,json;[print(s['entity_id'],'=',s['state'],s['attributes'].get('unit_of_measurement','')) for s in json.load(sys.stdin) if 'temp' in s['entity_id'] or 'humid' in s['entity_id']]"
   ```
   Put the temperature and humidity entity ids into `apps.yaml` (`temp_entity`,
   `humidity_entity`) and set `temp_unit` to `F` or `C` to match.

## Configuration (`apps.yaml`)

Everything tunable lives in `appdaemon/apps/apps.yaml`. Key knobs:

| Setting | Meaning |
|---|---|
| `temp_entity` / `humidity_entity` / `temp_unit` | your X-Sense sensors + their unit |
| `airport_icao`, `latitude`, `longitude`, `timezone` | home field for TAF/MOS + window logic |
| `tau_metal_h`, `tau_bulk_h`, `tau_event_h`, `dry_factor`, `flight_temp_c` | model parameters (defaults match `model.py`) |
| `run_every_minutes` | how often the cycle runs (default 60) |
| `window_days` / `max_window_days` / `spinup_days` | trailing history pulled (auto-extends to cover the last flight) |
| `alert_film_hours` | cam wet-hours since last flight that earns a nudge |
| `alert_cooldown_hours` | min time between nudges |
| `forecast_horizon_days` | how far out to look for a calm VFR window |
| `quiet_hours` | `[start, end]` local hours with no pushes |
| `telegram_target`, `www_dir`, `gemini_service` | notification plumbing |
| `manual_trigger` (optional) | an `input_boolean` you flip to force a run |

## What it publishes back to HA

- `sensor.cam_film_hours_since_flight` — wet-hours since last flight (state), with
  attributes: days since flight, realistic/ambient-damp hours, last flight, latest
  reading.
- `sensor.cam_days_since_flight`
- `sensor.cam_last_flight` (+ `flight_count`)
- `binary_sensor.cam_grounding_caution` (+ `reason`) — the data-aware "fly monthly"
  caution (fires only when ≥8 h of wetting *and* ≥30 days grounded).

## Alert logic

A nudge fires only when **all** hold:
- `film_hours_since_flight ≥ alert_film_hours`,
- a calm + VFR window exists within `forecast_horizon_days` (weekend afternoons
  rank highest),
- it's outside `quiet_hours`, and
- the last nudge was more than `alert_cooldown_hours` ago.

A new hot run (cowl `> flight_temp_c`) resets the tally **and** clears the cooldown.

## Testing

**Offline (no HA needed)** — validates the whole pipeline against your real CSV:
```bash
python homeassistant/test_local.py 170     # use a winter-inclusive window
```
Checks recorder-path ↔ model parity, the hot-run reset semantics, the alert
decision, chart rendering, and the message composer/fallback.

**Weather only** (hits live NWS/aviationweather):
```bash
python homeassistant/appdaemon/apps/weather.py KMRB 39.40 -77.98
```

**In HA**
1. Watch the AppDaemon log for `MoistureMonitor up. Model from .../scripts.`
2. Confirm the `cam_*` entities appear and update each cycle.
3. Force one end-to-end alert: fire the event `lyc_moisture_run` with
   `{"force": true}` from Developer Tools → Events, or flip the optional
   `manual_trigger` input_boolean. Confirm the Telegram message + 3 charts arrive.
4. To verify the reset: after a real flight (or by lowering `flight_temp_c`
   briefly) confirm `sensor.cam_last_flight` advances and the tally drops to ~0.
```
event: lyc_moisture_run
event_data:
  force: true
```
