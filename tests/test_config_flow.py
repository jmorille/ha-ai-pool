"""Config and options flow."""

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ai_pool.const import (
    CONF_COOLDOWN,
    CONF_DAILY_LIMIT,
    CONF_MAX_ATTEMPTS,
    CONF_MEMBERS,
    CONF_POOL_TYPE,
    CONF_RPM_LIMIT,
    CONF_STRATEGY,
    CONF_WEIGHT,
    DOMAIN,
    STRATEGY_LEAST_USED,
    STRATEGY_ROUND_ROBIN,
)

A = "ai_task.member_a"
B = "ai_task.member_b"


async def test_full_flow_creates_a_pool(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Gemini pool", CONF_POOL_TYPE: "ai_task"}
    )
    assert result["step_id"] == "members"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_MEMBERS: [A, B],
            CONF_STRATEGY: STRATEGY_ROUND_ROBIN,
            CONF_COOLDOWN: 300,
            CONF_MAX_ATTEMPTS: 3,
        },
    )
    assert result["step_id"] == "limits"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            f"limit_{A}": 250,
            f"rpm_{A}": 10,
            f"weight_{A}": 1,
            f"limit_{B}": 250,
            f"rpm_{B}": 15,
            f"weight_{B}": 2,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Gemini pool"

    data = result["data"]
    assert data[CONF_POOL_TYPE] == "ai_task"
    assert data[CONF_STRATEGY] == STRATEGY_ROUND_ROBIN
    assert data[CONF_MEMBERS] == [
        {
            "entity_id": A,
            CONF_DAILY_LIMIT: 250,
            CONF_RPM_LIMIT: 10,
            CONF_WEIGHT: 1,
        },
        {
            "entity_id": B,
            CONF_DAILY_LIMIT: 250,
            CONF_RPM_LIMIT: 15,
            CONF_WEIGHT: 2,
        },
    ]
    # The name is the entry title, not a config value.
    assert "name" not in data


async def test_members_step_rejects_an_empty_selection(
    hass: HomeAssistant,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Empty", CONF_POOL_TYPE: "tts"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_MEMBERS: [],
            CONF_STRATEGY: STRATEGY_ROUND_ROBIN,
            CONF_COOLDOWN: 300,
            CONF_MAX_ATTEMPTS: 3,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MEMBERS: "no_members"}


async def test_options_flow_updates_members_and_policy(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Pool",
        data={
            CONF_POOL_TYPE: "ai_task",
            CONF_STRATEGY: STRATEGY_ROUND_ROBIN,
            CONF_COOLDOWN: 300,
            CONF_MAX_ATTEMPTS: 3,
            CONF_MEMBERS: [{"entity_id": A, CONF_DAILY_LIMIT: 100, CONF_WEIGHT: 1}],
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_MEMBERS: [A, B],
            CONF_STRATEGY: STRATEGY_LEAST_USED,
            CONF_COOLDOWN: 60,
            CONF_MAX_ATTEMPTS: 2,
        },
    )
    assert result["step_id"] == "limits"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            f"limit_{A}": 100,
            f"rpm_{A}": 0,
            f"weight_{A}": 1,
            f"limit_{B}": 500,
            f"rpm_{B}": 5,
            f"weight_{B}": 3,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_STRATEGY] == STRATEGY_LEAST_USED
    assert result["data"][CONF_COOLDOWN] == 60
    assert len(result["data"][CONF_MEMBERS]) == 2
    # The pool type is not editable and must survive the round trip.
    assert result["data"][CONF_POOL_TYPE] == "ai_task"


async def test_options_flow_keeps_existing_limits_as_defaults(
    hass: HomeAssistant,
) -> None:
    """Re-opening options must not silently zero a declared allowance."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Pool",
        data={
            CONF_POOL_TYPE: "ai_task",
            CONF_STRATEGY: STRATEGY_ROUND_ROBIN,
            CONF_COOLDOWN: 300,
            CONF_MAX_ATTEMPTS: 3,
            CONF_MEMBERS: [
                {
                    "entity_id": A,
                    CONF_DAILY_LIMIT: 777,
                    CONF_RPM_LIMIT: 12,
                    CONF_WEIGHT: 4,
                }
            ],
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_MEMBERS: [A],
            CONF_STRATEGY: STRATEGY_ROUND_ROBIN,
            CONF_COOLDOWN: 300,
            CONF_MAX_ATTEMPTS: 3,
        },
    )
    schema_keys = {str(key): key for key in result["data_schema"].schema}
    assert schema_keys[f"limit_{A}"].default() == 777
    assert schema_keys[f"rpm_{A}"].default() == 12
    assert schema_keys[f"weight_{A}"].default() == 4
