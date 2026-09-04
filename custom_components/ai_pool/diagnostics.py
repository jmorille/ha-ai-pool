"""Diagnostics support for AI Pool."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .pool import AIPool


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Nothing is redacted because a pool stores no credentials: it references
    member entities, whose own integrations hold the keys.
    """
    pool: AIPool = entry.runtime_data
    return {
        "config": {**entry.data, **entry.options},
        "strategy": pool.strategy,
        "max_attempts": pool.max_attempts,
        "cooldown_seconds": pool.cooldown.total_seconds(),
        "cursor": pool.store.state.cursor,
        "members": [view.as_dict() for view in pool.snapshot()],
    }
