"""Integration setup and teardown."""

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ai_pool.const import (
    CONF_COOLDOWN,
    CONF_DAILY_LIMIT,
    CONF_MAX_ATTEMPTS,
    CONF_MEMBERS,
    CONF_POOL_TYPE,
    CONF_STRATEGY,
    CONF_WEIGHT,
    DOMAIN,
    STRATEGY_ROUND_ROBIN,
)

A = "member_a"
B = "member_b"


def build_entry(pool_type: str) -> MockConfigEntry:
    """Create a config entry for a pool of the given type."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"{pool_type} pool",
        data={
            CONF_POOL_TYPE: pool_type,
            CONF_STRATEGY: STRATEGY_ROUND_ROBIN,
            CONF_COOLDOWN: 300,
            CONF_MAX_ATTEMPTS: 3,
            CONF_MEMBERS: [
                {
                    "entity_id": f"{pool_type}.{name}",
                    CONF_DAILY_LIMIT: 100,
                    CONF_WEIGHT: 1,
                }
                for name in (A, B)
            ],
        },
    )


@pytest.mark.parametrize("pool_type", ["ai_task", "conversation", "tts", "stt"])
async def test_setup_and_unload(hass: HomeAssistant, pool_type: str) -> None:
    """Every pool type loads its own platform plus the sensors."""
    assert await async_setup_component(hass, pool_type, {})

    entry = build_entry(pool_type)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    # One diagnostic sensor per member.
    states = [
        state
        for state in hass.states.async_all("sensor")
        if state.entity_id.startswith("sensor.")
        and pool_type in state.attributes.get("friendly_name", "")
    ]
    assert len(states) == 2

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_pool_entity_is_published(hass: HomeAssistant) -> None:
    """The pool must appear as a normal entity in its target domain."""
    assert await async_setup_component(hass, "ai_task", {})

    entry = build_entry("ai_task")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    pool_entities = [
        state
        for state in hass.states.async_all("ai_task")
        if state.attributes.get("friendly_name") == "ai_task pool"
    ]
    assert len(pool_entities) == 1


async def test_options_update_triggers_reload(hass: HomeAssistant) -> None:
    """Editing members must take effect without a restart."""
    assert await async_setup_component(hass, "ai_task", {})

    entry = build_entry("ai_task")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.config_entries.async_update_entry(
        entry,
        options={
            CONF_POOL_TYPE: "ai_task",
            CONF_STRATEGY: STRATEGY_ROUND_ROBIN,
            CONF_COOLDOWN: 60,
            CONF_MAX_ATTEMPTS: 1,
            CONF_MEMBERS: [
                {
                    "entity_id": "ai_task.member_a",
                    CONF_DAILY_LIMIT: 5,
                    CONF_WEIGHT: 1,
                }
            ],
        },
    )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert len(entry.runtime_data.members) == 1
    assert entry.runtime_data.max_attempts == 1
