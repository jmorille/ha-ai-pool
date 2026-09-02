"""Shared base for pool entities."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .pool import AIPool


class AIPoolEntity(Entity):
    """Common identity and device grouping for every pool entity."""

    _attr_should_poll = False

    def __init__(self, pool: AIPool, entry: ConfigEntry) -> None:
        """Initialise identity from the config entry."""
        self.pool = pool
        self._entry = entry
        self._attr_unique_id = entry.entry_id
        # Named explicitly rather than borrowing the device name through
        # has_entity_name: the tts manager reads entity.name directly and
        # refuses an engine whose name is None, which is what the device-name
        # convention leaves it as.
        self._attr_name = entry.title
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="AI Pool",
            model=f"{pool.pool_type} pool",
            entry_type=None,
        )
