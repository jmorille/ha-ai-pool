"""AI Pool: route AI calls across several providers with quota-aware failover."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_POOL_TYPE
from .pool import AIPool

_LOGGER = logging.getLogger(__name__)

type AIPoolConfigEntry = ConfigEntry[AIPool]

# The platform a pool publishes on depends on what kind of pool it is; the
# sensor platform is always added so member health is visible on a dashboard.
POOL_PLATFORM: dict[str, Platform] = {
    "ai_task": Platform.AI_TASK,
    "conversation": Platform.CONVERSATION,
    "tts": Platform.TTS,
    "stt": Platform.STT,
}


def _platforms(entry: ConfigEntry) -> list[Platform]:
    """Platforms to load for this entry."""
    pool_type = {**entry.data, **entry.options}[CONF_POOL_TYPE]
    return [POOL_PLATFORM[pool_type], Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: AIPoolConfigEntry) -> bool:
    """Set up a pool from a config entry."""
    pool = AIPool(hass, entry)
    await pool.async_setup()
    entry.runtime_data = pool

    await hass.config_entries.async_forward_entry_setups(entry, _platforms(entry))
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AIPoolConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, _platforms(entry))


async def async_reload_entry(hass: HomeAssistant, entry: AIPoolConfigEntry) -> None:
    """Reload when the options change.

    Members and strategy are read live from the entry, but the pool type
    decides which platform is loaded, so a full reload is the honest response.
    """
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete persisted counters along with the entry."""
    pool = AIPool(hass, entry)
    await pool.async_remove()
