"""
Minimal Home Assistant REST client (stdlib only).

Works two ways with the same code:
  * from a laptop / standalone:  HAClient("http://homeassistant:8123", "<long-lived-token>")
  * inside a HA add-on:          HAClient("http://supervisor/core", os.environ["SUPERVISOR_TOKEN"])

So the engine can be tested live over the API now and shipped as an add-on later
without changing the HA-facing code.
"""
from __future__ import annotations
import json, datetime as dt
import urllib.request
import urllib.parse


class HAClient:
    def __init__(self, base_url, token, timeout=45):
        self.base = base_url.rstrip("/")
        self.tok = token
        self.timeout = timeout

    def _req(self, method, path, data=None):
        url = self.base + path
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(
            url, data=body, method=method,
            headers={"Authorization": "Bearer " + self.tok,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else None

    # --- reads ---
    def get_history(self, entity_id, days):
        """Return the recorder state list for one entity over the last `days`."""
        start = (dt.datetime.utcnow() - dt.timedelta(days=days)
                 ).strftime("%Y-%m-%dT%H:%M:%S")
        q = urllib.parse.urlencode({"filter_entity_id": entity_id})
        path = f"/api/history/period/{start}?{q}&minimal_response&no_attributes"
        data = self._req("GET", path)
        if data and isinstance(data, list) and data and isinstance(data[0], list):
            return data[0]
        return data or []

    def get_config(self):
        return self._req("GET", "/api/config")

    # --- writes ---
    def set_state(self, entity_id, state, attributes=None):
        return self._req("POST", f"/api/states/{entity_id}",
                         {"state": state, "attributes": attributes or {}})

    def call_service(self, domain, service, data=None, return_response=False):
        path = f"/api/services/{domain}/{service}"
        if return_response:
            path += "?return_response"
        return self._req("POST", path, data or {})
