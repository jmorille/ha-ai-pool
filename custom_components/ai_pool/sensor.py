"""Diagnostic sensors exposing per-member pool health."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .pool import AIPool

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one usage sensor per configured member."""
    pool: AIPool = entry.runtime_data
    async_add_entities(
        AIPoolMemberSensor(pool, entry, member.entity_id) for member in pool.members
    )


class AIPoolMemberSensor(SensorEntity):
    """Calls served today by one pool member.

    Registered as a diagnostic sensor: it describes the integration's own
    behaviour rather than anything about the home.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "calls"

    def __init__(self, pool: AIPool, entry: ConfigEntry, member_id: str) -> None:
        """Initialise a sensor bound to one member."""
        self._pool = pool
        self._entry = entry
        self._member_id = member_id
        self._attr_unique_id = f"{entry.entry_id}_{member_id}"
        self._attr_translation_key = "member_calls"
        self._attr_translation_placeholders = {"member": member_id}
        self._attr_name = member_id
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to pool state changes."""
        self.async_on_remove(self._pool.async_add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        """Refresh when the pool records a call or a failure."""
        self.async_write_ha_state()

    def _row(self) -> dict[str, Any] | None:
        """Locate this member in the pool snapshot."""
        for row in self._pool.snapshot():
            if row["entity_id"] == self._member_id:
                return row
        return None

    @property
    def native_value(self) -> int | None:
        """Calls served today."""
        row = self._row()
        return None if row is None else row["calls_today"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Health detail for dashboards and troubleshooting."""
        row = self._row()
        if row is None:
            return {}
        return {
            "status": row["status"],
            "failures_today": row["failures_today"],
            "daily_limit": row["daily_limit"],
            "remaining": row["remaining"],
            "weight": row["weight"],
            "cooldown_until": row["cooldown_until"],
            "last_error": row["last_error"],
            "last_success": row["last_success"],
        }
