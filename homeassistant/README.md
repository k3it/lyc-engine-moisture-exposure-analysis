# Home Assistant — CLI / developer tools

> **The live Home Assistant deployment is now a native custom integration:**
> [`custom_components/engine_moisture/`](../custom_components/engine_moisture/). Install
> and configure it from there — schedule, parameters, manual trigger, and run history are
> all GUI-driven. The old AppDaemon app has been retired.

This folder keeps the **off-HA tools** that share the same canonical pipeline
(`../scripts/pipeline.py` + `../scripts/model.py`), useful for development and validation:

| File | What it does |
|---|---|
| `test_local.py` | Offline end-to-end + recorder↔regrid **parity** check against the repo's real X-Sense CSV. No HA, no network. |
| `run_once.py` | Run ONE monitor cycle against a live HA over the REST API (publishes the `cam_*` sensors, optionally sends the nudge). Handy for testing before/without the integration. |
| `ha_client.py` | Minimal stdlib HA REST client used by `run_once.py`. |

## Offline test (no HA needed)

```bash
python homeassistant/test_local.py 60     # winter-inclusive window
```
Validates build_frame ↔ regrid parity, the hot-run reset semantics, the alert decision,
the summary chart, and the message composer/fallback.

## One live cycle over REST (optional)

```bash
set HA_URL=http://homeassistant:8123
set HA_TOKEN=<long-lived token>
python homeassistant/run_once.py --force [--no-send] [--serve-charts]
```
- `--force` ignore thresholds/quiet-hours/cooldown and send a nudge now
- `--no-send` do everything except sending Telegram (dry run)
- `--serve-charts` briefly serve the chart PNG over LAN so HA can fetch + attach it

Both tools import the same `pipeline`/`model`/`charts`/`weather` modules the integration
uses, so a green `test_local.py` is a good proxy for the integration's compute path.

`run_state.json` is this tool's local cycle state (gitignored).
