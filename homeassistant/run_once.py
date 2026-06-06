"""
Run ONE moisture-monitor cycle against a live Home Assistant over the REST API.

This is a CLI/dev tool: it reuses the same canonical pipeline the custom integration
uses (scripts/pipeline.py: build_frame / run_model / reconcile_last_flight /
decide_alert / compose) but talks to HA through ha_client.HAClient over REST, so it can
drive a live HA from a laptop for testing without installing the integration.

Usage:
    set HA_URL=http://homeassistant:8123   (default)
    set HA_TOKEN=<long-lived token>
    python homeassistant/run_once.py --force [--no-send] [--serve-charts]

  --force        ignore thresholds/quiet-hours/cooldown and send a nudge now (test)
  --no-send      do everything except sending Telegram (dry run)
  --serve-charts briefly serve chart PNGs over LAN HTTP so HA can fetch+attach them
"""
from __future__ import annotations
import os, sys, json, time, socket, argparse, datetime as dt, threading, tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "scripts"))  # canonical model + pipeline + weather + charts
sys.path.insert(0, str(HERE))              # ha_client

import pipeline as mm
import weather
from ha_client import HAClient

# --- config for THIS deployment (discovered from the live HA) ---
CONFIG = dict(mm.DEFAULTS, **{
    "temp_entity": "sensor.engine_thermo_hygrometer_temperature",
    "humidity_entity": "sensor.engine_thermo_hygrometer_humidity",
    "temp_unit": "F",
    "airport_icao": "KMRB",
    "latitude": 39.40,
    "longitude": -77.98,
    "timezone": "America/New_York",
    # seed history from the repo's X-Sense exports until the recorder accumulates
    "backfill_csv_glob": str(REPO / "data" / "Engine Thermo-hygrometer_*.csv"),
    "www_dir": tempfile.mkdtemp(prefix="moisture_charts_"),
    "telegram_target": None,   # broadcast to configured chats
})
STATE_PATH = HERE / "run_state.json"


def fetch_history_best(client, entity, days):
    """HA's history endpoint returns inconsistent (sometimes empty/boundary-only)
    results depending on how far `start` predates the retained data. Union several
    window sizes and dedupe by timestamp so we reliably capture all dense recent
    points regardless of retention quirks."""
    merged = {}
    for d in sorted({min(days, x) for x in (1, 2, 4, 7, 14, 30, days)}):
        for s in client.get_history(entity, d):
            ts = s.get("last_changed") or s.get("last_updated")
            if ts and s.get("state") not in (None, "unknown", "unavailable", ""):
                merged[ts] = s
    pts = [merged[k] for k in sorted(merged)]
    return pts, len(pts)


def load_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {"last_flight": None, "last_alert_ts": None}


def save_state(s):
    STATE_PATH.write_text(json.dumps(s, indent=2))


def lan_ip(target_host):
    """Best-effort local IP that HA would see us on."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((target_host, 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def serve_dir(directory, port=8770):
    import http.server, functools
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    httpd = http.server.HTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def predict_window(client, cfg, weather_info):
    """Gemini predicts ONLY the next flying window (not the moisture)."""
    prompt = mm.build_window_prompt(weather_info, cfg["airport_icao"])
    try:
        resp = client.call_service(
            "ai_task", "generate_data",
            {"task_name": "flying_window", "instructions": prompt,
             "entity_id": cfg["ai_task_entity"]}, return_response=True)
        sr = resp.get("service_response") if isinstance(resp, dict) else resp
        text = mm.extract_llm_text(sr)
        if text:
            return text, "gemini(ai_task)"
    except Exception as e:
        print(f"  ! AI Task window predict failed: {e}")
    return mm.window_fallback(weather_info), "fallback"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-send", action="store_true")
    ap.add_argument("--serve-charts", action="store_true")
    a = ap.parse_args()

    url = os.environ.get("HA_URL", "http://homeassistant:8123")
    token = os.environ.get("HA_TOKEN")
    if not token:
        sys.exit("Set HA_TOKEN (long-lived access token).")
    client = HAClient(url, token, timeout=90)   # AI Task (Gemini) can be slow
    cfg = CONFIG
    tz = mm._local_tz(cfg["timezone"])
    now_local = dt.datetime.now(tz).replace(tzinfo=None)
    state = load_state()

    # dynamic window
    win_days = cfg["window_days"]
    if state.get("last_flight"):
        try:
            grounded = (now_local - dt.datetime.fromisoformat(state["last_flight"])).days
            win_days = min(cfg["max_window_days"], max(cfg["window_days"], grounded + cfg["spinup_days"]))
        except ValueError:
            pass

    print(f"HA: {url}  window={win_days}d")
    temp_hist, tw = fetch_history_best(client, cfg["temp_entity"], win_days)
    rh_hist, hw = fetch_history_best(client, cfg["humidity_entity"], win_days)
    print(f"  recorder points: temp={len(temp_hist)} rh={len(rh_hist)}")
    backfill = mm.load_backfill_csv(cfg.get("backfill_csv_glob"))
    print(f"  backfill rows: {0 if backfill is None else len(backfill)}")
    g = mm.build_frame(temp_hist, rh_hist, cfg["timezone"], cfg["temp_unit"],
                       backfill_df=backfill,
                       max_days=cfg["max_window_days"] + cfg["spinup_days"])
    if g is None:
        sys.exit("No usable history (recorder empty and no backfill).")
    print(f"  model frame: {len(g)} min  {g.index.min()} -> {g.index.max()}")

    res, series = mm.run_model(g, cfg)
    last_flight, is_new = mm.reconcile_last_flight(res, series, state.get("last_flight"), cfg)
    if is_new:
        print(f"  NEW hot run at {last_flight} -> tally reset")
        state["last_alert_ts"] = None
    state["last_flight"] = last_flight
    slf = res["since_last_flight"]
    gc = res["grounding_caution"]
    print(f"  flights={res['flight_count']} last_flight={last_flight}")
    print(f"  since flight: days={slf['days']} film_h={slf['film_hours']} "
          f"damp_h={slf['ambient_damp_hours_ub']}  caution={gc['caution']}")

    # publish sensors
    latest = slf.get("latest", {})
    client.set_state("sensor.cam_film_hours_since_flight", round(slf["film_hours"], 1), {
        "unit_of_measurement": "h", "icon": "mdi:water-percent",
        "friendly_name": "Cam wet-hours since last flight",
        "days_since_flight": slf["days"], "wet_hours_realistic": slf["wet_hours_realistic"],
        "ambient_damp_hours_ub": slf["ambient_damp_hours_ub"], "last_flight": last_flight,
        "latest_temp_c": latest.get("Tc"), "latest_rh_pct": latest.get("RH")})
    client.set_state("sensor.cam_days_since_flight", slf["days"],
                     {"unit_of_measurement": "d", "icon": "mdi:calendar-clock",
                      "friendly_name": "Days since last flight"})
    client.set_state("sensor.cam_last_flight", last_flight or "unknown",
                     {"icon": "mdi:airplane-takeoff", "friendly_name": "Last engine run",
                      "flight_count": res["flight_count"]})
    client.set_state("binary_sensor.cam_grounding_caution",
                     "on" if gc["caution"] else "off",
                     {"device_class": "problem", "friendly_name": "Cam grounding caution",
                      "reason": gc.get("reason")})
    print("  published sensors: sensor.cam_film_hours_since_flight, "
          "sensor.cam_days_since_flight, sensor.cam_last_flight, "
          "binary_sensor.cam_grounding_caution")

    # deterministic moisture exposure (close calls included) drives the alert
    nw = mm.near_wet_stats(series, res, cfg["close_call_margin_c"])
    print(f"  exposure since flight: sub_dew={nw['sub_dew_h']}h "
          f"close_call={nw['close_call_h']}h (within {nw['margin_c']}°C) "
          f"peak_gap={nw['peak_gap_c']}°C")
    fire, reason = mm.decide_alert(nw, slf, state, cfg, now_local)
    if a.force:
        fire, reason = True, "forced"
    print(f"  alert: {fire} ({reason})")

    if fire and not a.no_send:
        # weather only fetched when we're actually sending
        weather_info = weather.assess(cfg["airport_icao"], cfg["latitude"],
                                      cfg["longitude"], cfg["timezone"],
                                      cfg["forecast_horizon_days"])
        moisture_line = mm.moisture_status_line(res, series, cfg["close_call_margin_c"], nw)
        window_line, src = predict_window(client, cfg, weather_info)
        text = mm.assemble_message(moisture_line, window_line)
        print(f"  moisture (deterministic): {moisture_line}")
        print(f"  window [{src}]: {window_line}")

        # single summary chart
        import charts as ch
        out = Path(cfg["www_dir"])
        chart_file = None
        try:
            ch.summary_chart(series, res, str(out / "summary.png"),
                             margin_c=cfg["close_call_margin_c"])
            chart_file = "summary.png"
        except Exception as e:
            print(f"  ! summary chart failed: {e}")

        # ONE telegram message: chart photo with the full text as its caption
        sent = False
        if a.serve_charts and chart_file:
            host = url.split("//")[-1].split(":")[0].split("/")[0]
            ip = lan_ip(host)
            if ip:
                httpd, port = serve_dir(str(out))
                print(f"  serving chart at http://{ip}:{port}/{chart_file}")
                try:
                    client.call_service("telegram_bot", "send_photo",
                        {k: v for k, v in {"url": f"http://{ip}:{port}/{chart_file}",
                         "caption": text, "target": cfg["telegram_target"]}.items()
                         if v is not None})
                    sent = True
                except Exception as e:
                    print(f"  ! send_photo failed: {e}")
                time.sleep(3); httpd.shutdown()
        if not sent:
            client.call_service("telegram_bot", "send_message",
                {k: v for k, v in {"message": text,
                 "target": cfg["telegram_target"]}.items() if v is not None})
            print(f"  sent text-only (chart at {out/'summary.png'} "
                  f"{'— use --serve-charts to attach' if not a.serve_charts else ''})")

    save_state(state)
    print("done.")


if __name__ == "__main__":
    main()
