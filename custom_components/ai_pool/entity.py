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
        # Named explicitly, which knowingly departs from the has-entity-name
        # convention. `Entity.name` returns `_attr_name` verbatim; composing it
        # with the device name happens later, in `_async_calculate_state`, and
        # only for the friendly name in the state machine. So the usual
        # has_entity_name + `_attr_name = None` pairing leaves `entity.name` as
        # None - and the tts manager reads `entity.name` directly, refusing an
        # engine whose name is not set. The alternative that satisfies both, a
        # translation key, would name this entity twice over: the device is the
        # pool and carries exactly one primary entity, so "TTS Pool" would
        # present itself as "TTS Pool <something>".
        self._attr_name = entry.title
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="AI Pool",
            model=f"{pool.pool_type} pool",
            entry_type=None,
        )
