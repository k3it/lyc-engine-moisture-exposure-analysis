# Engine Moisture Monitor — Home Assistant integration

A native Home Assistant integration that hosts the canonical engine-moisture physics
model (`../../scripts/model.py`) and turns your X-Sense cowl temp/RH history into
cam-wetness sensors plus an optional "want to go flying?" Telegram nudge.

Unlike the old AppDaemon app, **everything is GUI-driven**:

- **Schedule** → the `Run cycle every (minutes)` option (or call the service from any
  automation/dashboard button).
- **Parameters** → one Options form (Settings → Devices & Services → Engine Moisture
  Monitor → **Configure**): model τ's, flight-detection, alert thresholds, quiet hours,
  chart history, notification plumbing.
- **Manual trigger** → the `engine_moisture.run_now` service (with optional `force` to
  send the nudge regardless of thresholds/quiet-hours/cooldown).
- **Run history** → native entity **History/Logbook** + automation **Traces**, plus a
  diagnostic *Last model run* sensor.

The physics stay the single source of truth in `scripts/` — this integration imports
them, never copies them. A `git pull` on the repo updates the model.

## Install

1. **Deploy the repo on the HA box** (for `scripts/`), e.g. clone to
   `/config/lyc-engine-moisture-exposure-analysis` (the integration auto-detects this
   path; otherwise set the `scripts_dir` option or `LYC_SCRIPTS_DIR`).
2. **Copy the integration** `custom_components/engine_moisture/` into
   `/config/custom_components/engine_moisture/` (symlink to the repo copy if you prefer
   `git pull` to update both).
3. **Restart Home Assistant.** On first load it installs `pandas`, `numpy`, `matplotlib`
   (declared in `manifest.json`) — the first restart may take a few minutes.
4. **Add it:** Settings → Devices & Services → **Add Integration** → *Engine Moisture
   Monitor*. Pick your cowl temperature and humidity sensors, unit, home airport, and
   lat/lon/timezone.
5. **Tune it:** open **Configure** to adjust schedule, parameters, and alert thresholds.

## Notifications (optional, for the nudge)

- **Telegram:** have the `telegram_bot` integration set up; put your chat id in the
  `Telegram chat id` option (blank = broadcast to configured chats).
- **Flying-window text (Gemini):** add the *Google Generative AI* / AI Task integration;
  the default AI Task entity is `ai_task.google_ai_task` (change via the option). If it's
  unavailable the nudge falls back to a deterministic window line — it never goes silent.
- **Charts** are written to the `Chart output directory` (default
  `/homeassistant/www/moisture`) and attached to the Telegram message. The alert chart
  shows ~2.5 months of history with each detected flight marked and the wet+close-hours
  line reset at every flight, so you can see how exposure built up and was purged.

## Sensor gap-fill fallback (stale/offline cowl feed)

If the cowl sensor stops reporting (lost Wi-Fi, dead battery, cloud outage), the model
does **not** freeze on the last reading. Each cycle it looks for data holes and a stale
tail (no data for more than `Gap-fill: sensor stale after`, default 90 min), pulls the
airport station's METAR for the outage (Iowa Mesonet archive + aviationweather.gov live
cache), pushes it through the fitted **station→cowl transfer** (hangar thermal lag,
damping, solar gain — see `../../reports/cowl_station_backtest.md`), and splices the
buffered estimate in, marked estimated. Estimated spans are shaded gray on the alert
chart and called out in the Telegram text, and the exposure tally keeps counting through
the outage.

The transfer fit is read from `Gap-fill: station→cowl transfer fit JSON` (blank =
`<repo>/data/<icao>_cowl_transfer.json`, which ships fitted for KMRB). For a different
station/hangar, fit your own with `scripts/gapfill.py` (`fit_transfer`/`backtest`) and
point the option at the saved JSON.

## Entities

- `sensor.*_cam_wet_hours_since_last_flight` (film hours; attrs: days, realistic/UB
  hours, sub-dew/close-call hours, last flight, latest reading)
- `sensor.*_days_since_last_flight`
- `sensor.*_last_engine_run` (+ `flight_count`)
- `sensor.*_last_model_run` (diagnostic; + `last_alert`)
- `binary_sensor.*_cam_grounding_caution` (data-aware "fly monthly"; attrs: reason, wet
  hours, days grounded)
- `binary_sensor.*_cowl_sensor_offline` (on while running on the station gap-fill
  estimate; attrs: gap-filled hours, last real reading, gap-fill error if any)

## Test it

Fire the service from Developer Tools → Actions:

```yaml
action: engine_moisture.run_now
data:
  force: true
```

Confirm the sensors update and (if Telegram is configured) one message with the summary
chart arrives. For offline/CLI testing without HA, see `../../homeassistant/`.
