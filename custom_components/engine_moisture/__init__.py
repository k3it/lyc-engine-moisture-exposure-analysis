"""Engine Moisture Monitor — a Home Assistant integration that hosts the canonical
engine-moisture physics model (repo scripts/) and turns the X-Sense cowl temp/RH
history into cam-wetness sensors plus an optional "want to go flying?" Telegram nudge.

Schedule, parameters and the manual trigger are all GUI-adjustable (Options + the
engine_moisture.run_now service); run history is the native entity History/Logbook and
automation Traces. The physics stay in scripts/model.py (single source of truth) — this
package imports them, never copies them.
"""
from __future__ import annotations

import logging
import os
import sys

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_CHART_DAYS,
    ATTR_FORCE,
    CONF_SCRIPTS_DIR,
    DOMAIN,
    PLATFORMS,
    SERVICE_RUN_NOW,
)
from .coordinator import EngineMoistureCoordinator

_LOGGER = logging.getLogger(__name__)

# headless matplotlib for chart rendering inside HA (set before charts import)
os.environ.setdefault("MPLBACKEND", "Agg")


def _resolve_scripts_dir(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Find the repo's scripts/ dir (the one holding model.py). Tries, in order: the
    configured option, the LYC_SCRIPTS_DIR env var, then common deploy locations under
    the HA config dir. Raises ConfigEntryNotReady if model.py can't be found."""
    candidates: list[str] = []
    configured = entry.data.get(CONF_SCRIPTS_DIR) or entry.options.get(CONF_SCRIPTS_DIR)
    if configured:
        candidates.append(configured)
    if os.environ.get("LYC_SCRIPTS_DIR"):
        candidates.append(os.environ["LYC_SCRIPTS_DIR"])
    candidates.append(
        hass.config.path("lyc-engine-moisture-exposure-analysis", "scripts"))
    candidates.append(hass.config.path("engine-moisture", "scripts"))
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, "model.py")):
            return c
    raise ConfigEntryNotReady(
        "Could not find scripts/model.py. Deploy the repo (so scripts/ sits beside the "
        "config dir) or set the 'scripts_dir' option / LYC_SCRIPTS_DIR env var. Tried: "
        + ", ".join(str(c) for c in candidates))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Engine Moisture Monitor from a config entry."""
    scripts_dir = await hass.async_add_executor_job(_resolve_scripts_dir, hass, entry)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    coordinator = EngineMoistureCoordinator(hass, entry, scripts_dir)
    await coordinator.async_load_state()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN):
            hass.services.async_remove(DOMAIN, SERVICE_RUN_NOW)
    return unload_ok


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change (picks up new schedule/params)."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register engine_moisture.run_now once (manual trigger, optional force)."""
    if hass.services.has_service(DOMAIN, SERVICE_RUN_NOW):
        return

    async def _handle_run_now(call: ServiceCall) -> None:
        force = bool(call.data.get(ATTR_FORCE, False))
        chart_days = call.data.get(ATTR_CHART_DAYS)
        coordinators = list(hass.data.get(DOMAIN, {}).values())
        for coordinator in coordinators:
            await coordinator.async_run_now(force=force, chart_history_days=chart_days)

    hass.services.async_register(
        DOMAIN,
        SERVICE_RUN_NOW,
        _handle_run_now,
        schema=vol.Schema({
            vol.Optional(ATTR_FORCE, default=False): cv.boolean,
            vol.Optional(ATTR_CHART_DAYS): vol.All(
                vol.Coerce(int), vol.Range(min=7, max=180)),
        }),
    )
