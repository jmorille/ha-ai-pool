"""A problem sensor for a pool that cannot serve.

The failure this answers is the quiet one. When every member refuses, the call
raises and whatever asked for it stops - an announcement simply never plays,
and the only trace is in the automation trace. One entity that says "this pool
has nothing healthy left" turns that into something a dashboard shows and an
automation can notify about.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, STATUS_HEALTHY
from .pool import AIPool

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the pool's problem sensor."""
    pool: AIPool = entry.runtime_data
    async_add_entities([AIPoolNoHealthyMemberSensor(pool, entry)])


class AIPoolNoHealthyMemberSensor(BinarySensorEntity):
    """On when no member of the pool is in a state to serve."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "no_healthy_member"
    # Polled, unlike the metric sensors: this one has to be right even when
    # nothing is calling the pool. Two of the three ways it changes - a
    # cooldown expiring, a member entity going unavailable - happen without the
    # pool being involved at all, so there is no event to listen for.
    _attr_should_poll = True

    def __init__(self, pool: AIPool, entry: ConfigEntry) -> None:
        """Bind the sensor to its pool."""
        self._pool = pool
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_no_healthy_member"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
        )

    async def async_added_to_hass(self) -> None:
        """Also refresh immediately when the pool records a failure."""
        self.async_on_remove(self._pool.async_add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        """Refresh without waiting for the next poll."""
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool | None:
        """Whether the pool has no healthy member left.

        An empty pool reports unknown rather than a problem: nothing is broken,
        it was simply never given anything to route to.
        """
        members = self._pool.members
        if not members:
            return None
        return not any(
            self._pool.member_status(member) == STATUS_HEALTHY for member in members
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Which members are in what state, so the cause is visible here."""
        statuses = {
            member.entity_id: self._pool.member_status(member)
            for member in self._pool.members
        }
        healthy = [
            entity_id
            for entity_id, status in statuses.items()
            if status == STATUS_HEALTHY
        ]
        return {
            "members_total": len(statuses),
            "members_healthy": len(healthy),
            "healthy": healthy,
            "statuses": statuses,
        }
