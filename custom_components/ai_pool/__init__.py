"""AI Pool: route AI calls across several providers with quota-aware failover."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_CLEAR_COUNTERS,
    ATTR_MEMBER,
    ATTR_POOL,
    CONF_MEMBERS,
    CONF_POOL_TYPE,
    CONFIG_VERSION,
    DOMAIN,
    SERVICE_RESET_MEMBER,
)
from .pool import AIPool

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type AIPoolConfigEntry = ConfigEntry[AIPool]

# The platform a pool publishes on depends on what kind of pool it is; the
# sensor platforms are always added so member health is visible on a dashboard.
POOL_PLATFORM: dict[str, Platform] = {
    "ai_task": Platform.AI_TASK,
    "conversation": Platform.CONVERSATION,
    "tts": Platform.TTS,
    "stt": Platform.STT,
}

RESET_MEMBER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_POOL): cv.string,
        vol.Optional(ATTR_MEMBER): cv.entity_id,
        vol.Optional(ATTR_CLEAR_COUNTERS, default=False): cv.boolean,
    }
)


def _platforms(entry: ConfigEntry) -> list[Platform]:
    """Platforms to load for this entry."""
    pool_type = {**entry.data, **entry.options}[CONF_POOL_TYPE]
    return [POOL_PLATFORM[pool_type], Platform.SENSOR, Platform.BINARY_SENSOR]


@callback
def _prune_orphan_entities(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """Remove registry entries for members the pool no longer has.

    Sensors are only created for current members, but nothing ever removed the
    ones belonging to a member that left: they lingered in the UI as orphans,
    unavailable forever, with no way to tell them from a broken member.
    """
    members = [
        item["entity_id"] for item in {**entry.data, **entry.options}[CONF_MEMBERS]
    ]
    expected = {
        entry.entry_id,
        f"{entry.entry_id}_fallback_rate",
        f"{entry.entry_id}_no_healthy_member",
    }
    for member in members:
        expected.add(f"{entry.entry_id}_{member}")
        expected.add(f"{entry.entry_id}_{member}_latency")

    registry = er.async_get(hass)
    orphans = [
        item.entity_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.unique_id not in expected
    ]
    for entity_id in orphans:
        registry.async_remove(entity_id)
    return orphans


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's services once, for every pool."""

    async def _reset_member(call: ServiceCall) -> None:
        """Clear the penalties held against a member, or against all of them."""
        entry_id: str = call.data[ATTR_POOL]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="pool_not_found",
                translation_placeholders={"pool": entry_id},
            )
        if not isinstance(pool := getattr(entry, "runtime_data", None), AIPool):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="pool_not_loaded",
                translation_placeholders={"pool": entry.title},
            )

        member: str | None = call.data.get(ATTR_MEMBER)
        if member and member not in {candidate.entity_id for candidate in pool.members}:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_a_member",
                translation_placeholders={"member": member, "pool": entry.title},
            )

        reset = await pool.async_reset_member(
            member, clear_counters=call.data[ATTR_CLEAR_COUNTERS]
        )
        _LOGGER.info(
            "Pool %s: cleared penalties on %s",
            entry.title,
            ", ".join(reset) if reset else "no member (nothing was held back)",
        )
        # The member set is unchanged, but a re-admitted member may well be a
        # duplicate of one already there.
        pool.async_check_models()

    hass.services.async_register(
        DOMAIN, SERVICE_RESET_MEMBER, _reset_member, schema=RESET_MEMBER_SCHEMA
    )
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Bring a config entry forward to the current schema.

    Nothing to rewrite yet, and that is exactly why this is here: without the
    hook, the first version bump would fail every existing entry with no way
    back. Reject a version from the future rather than guess at it - the entry
    then shows as needing a newer Home Assistant instead of misreading data.
    """
    return entry.version <= CONFIG_VERSION


async def async_setup_entry(hass: HomeAssistant, entry: AIPoolConfigEntry) -> bool:
    """Set up a pool from a config entry."""
    if orphans := _prune_orphan_entities(hass, entry):
        _LOGGER.info(
            "Pool %s: removed entities for member(s) no longer in the pool: %s",
            entry.title,
            ", ".join(orphans),
        )

    pool = AIPool(hass, entry)
    await pool.async_setup()
    entry.runtime_data = pool

    await hass.config_entries.async_forward_entry_setups(entry, _platforms(entry))
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    # Day counters are otherwise only rolled when the pool is used, so a pool
    # called once a morning would show yesterday's numbers until it was next
    # asked for something.
    entry.async_on_unload(
        async_track_time_change(hass, pool.async_roll_day, hour=0, minute=0, second=5)
    )
    # Checked after the platforms are up, so the members' own entities are in
    # the registry and their models can be read.
    pool.async_check_models()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AIPoolConfigEntry) -> bool:
    """Unload a config entry."""
    if isinstance(pool := getattr(entry, "runtime_data", None), AIPool):
        pool.async_clear_issues()
    return await hass.config_entries.async_unload_platforms(entry, _platforms(entry))


async def async_reload_entry(hass: HomeAssistant, entry: AIPoolConfigEntry) -> None:
    """Reload when the options change.

    Members and strategy are read live from the entry, but the pool type
    decides which platform is loaded, so a full reload is the honest response.
    """
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete persisted counters and repairs along with the entry."""
    pool = AIPool(hass, entry)
    pool.async_clear_issues()
    await pool.async_remove()
