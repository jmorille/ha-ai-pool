"""Diagnostic sensors exposing pool health and routing metrics.

Three sensors, each answering a different question:

* calls per member    - who is doing the work, and against which allowance
* latency per member  - who answers fast enough to deserve going first
* fallback rate       - whether the configured preference order is any good

The first two are per member; the third is the pool's own, because no
per-member counter can show that a request needed a second member.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .pool import AIPool

PARALLEL_UPDATES = 0


@callback
def _member_label(hass: HomeAssistant, member_id: str) -> str:
    """Human name for a member entity, falling back to its entity id.

    The entity id reads badly in a sensor name ("TTS Pool tts.google_ai_tts"),
    and a pool with three sensors per member makes that noise three times
    worse.
    """
    if (state := hass.states.get(member_id)) and (
        name := state.attributes.get("friendly_name")
    ):
        return str(name)
    if (entry := er.async_get(hass).async_get(member_id)) and (
        name := entry.name or entry.original_name
    ):
        return str(name)
    return member_id


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the per-member sensors plus the pool's routing sensor."""
    pool: AIPool = entry.runtime_data
    entities: list[SensorEntity] = [AIPoolFallbackSensor(pool, entry)]
    for member in pool.members:
        label = _member_label(hass, member.entity_id)
        entities.append(AIPoolCallsSensor(pool, entry, member.entity_id, label))
        entities.append(AIPoolLatencySensor(pool, entry, member.entity_id, label))
    async_add_entities(entities)


class AIPoolSensor(SensorEntity):
    """Shared plumbing: device identity and refresh on pool activity."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, pool: AIPool, entry: ConfigEntry) -> None:
        """Bind the sensor to its pool."""
        self._pool = pool
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to pool state changes."""
        self.async_on_remove(self._pool.async_add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        """Refresh when the pool records a call or a failure."""
        self.async_write_ha_state()


class AIPoolMemberSensor(AIPoolSensor):
    """Base for sensors describing one member."""

    def __init__(
        self, pool: AIPool, entry: ConfigEntry, member_id: str, label: str
    ) -> None:
        """Bind the sensor to one member of the pool."""
        super().__init__(pool, entry)
        self._member_id = member_id
        self._attr_translation_placeholders = {"member": label}

    def _row(self) -> dict[str, Any] | None:
        """Locate this member in the pool snapshot."""
        for row in self._pool.snapshot():
            if row["entity_id"] == self._member_id:
                return row
        return None


class AIPoolCallsSensor(AIPoolMemberSensor):
    """Calls served today by one pool member."""

    _attr_translation_key = "member_calls"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "calls"

    def __init__(
        self, pool: AIPool, entry: ConfigEntry, member_id: str, label: str
    ) -> None:
        """Bind the sensor to one member of the pool."""
        super().__init__(pool, entry, member_id, label)
        self._attr_unique_id = f"{entry.entry_id}_{member_id}"

    @property
    def native_value(self) -> int | None:
        """Calls served today."""
        row = self._row()
        return None if row is None else row["calls_today"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Health and failure detail for dashboards and troubleshooting."""
        row = self._row()
        if row is None:
            return {}
        attributes = {
            "status": row["status"],
            # Which engine is really behind this member. Two members sharing
            # one is the failure the pool cannot route around, and a dashboard
            # is where that is spotted.
            "model": row["model"],
            "failures_today": row["failures_today"],
            "success_rate": row["success_rate"],
            "daily_limit": row["daily_limit"],
            "remaining": row["remaining"],
            "weight": row["weight"],
            "cooldown_until": row["cooldown_until"],
            # Consecutive capacity refusals, which is what makes each cooldown
            # longer than the last.
            "cooldown_strikes": row["cooldown_strikes"],
            "last_error": row["last_error"],
            "last_success": row["last_success"],
            # Rate-limit tracking. Providers meter requests, and a refusal is
            # a request: requests_today is therefore the pessimistic reading of
            # the same allowance that calls_today reads optimistically. The
            # provider's own counter sits between the two and is not visible
            # from here.
            "requests_today": row["requests_today"],
            "rpd_remaining": row["rpd_remaining"],
            "requests_last_minute": row["requests_last_minute"],
            "rpm_limit": row["rpm_limit"],
            "rpm_remaining": row["rpm_remaining"],
            # Characters, not tokens: no token count reaches the integration.
            # Roughly four characters per token is the usual rule of thumb.
            "input_chars_today": row["input_chars_today"],
            "input_chars_last_minute": row["input_chars_last_minute"],
        }
        # One key per observed failure kind rather than a nested dict, so each
        # is usable on its own in a template or a dashboard card.
        for kind, count in row["failures_by_kind"].items():
            attributes[f"failures_{kind}"] = count
        return attributes


class AIPoolLatencySensor(AIPoolMemberSensor):
    """How long this member's last successful call took.

    A measurement rather than a total, so the recorder keeps long-term
    statistics and the numbers can be charted side by side: that comparison is
    the whole point, and it replaces benchmarking providers by hand.
    """

    _attr_translation_key = "member_latency"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(
        self, pool: AIPool, entry: ConfigEntry, member_id: str, label: str
    ) -> None:
        """Bind the sensor to one member of the pool."""
        super().__init__(pool, entry, member_id, label)
        self._attr_unique_id = f"{entry.entry_id}_{member_id}_latency"

    @property
    def native_value(self) -> float | None:
        """Duration of the last successful call, in seconds."""
        row = self._row()
        return None if row is None else row["latency_last"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Spread of the durations behind the last value."""
        row = self._row()
        if row is None:
            return {}
        return {
            "average_today": row["latency_average"],
            "min_today": row["latency_min"],
            "max_today": row["latency_max"],
            "samples_today": row["latency_samples"],
            # Deliberately not day-scoped: the recent window answers "how fast
            # is it right now", which a daily average hides after a bad morning.
            "recent_average": row["latency_recent_average"],
        }


class AIPoolFallbackSensor(AIPoolSensor):
    """Share of today's requests that needed more than one member.

    Zero means the first choice always served. A high value means the pool is
    working around a preference order that should be changed.
    """

    _attr_translation_key = "pool_fallback_rate"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, pool: AIPool, entry: ConfigEntry) -> None:
        """Bind the sensor to the pool as a whole."""
        super().__init__(pool, entry)
        self._attr_unique_id = f"{entry.entry_id}_fallback_rate"

    @property
    def native_value(self) -> float | None:
        """Fallback rate today, as a percentage of requests."""
        return self._pool.routing_snapshot()["fallback_rate"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Routing counters behind the rate."""
        routing = self._pool.routing_snapshot()
        return {key: value for key, value in routing.items() if key != "fallback_rate"}
