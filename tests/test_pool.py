"""Pool routing and failover behaviour."""

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
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
    STATUS_COOLDOWN,
    STATUS_DISABLED,
    STATUS_EXHAUSTED,
    STATUS_HEALTHY,
    STATUS_UNAVAILABLE,
    STRATEGY_PRIORITY,
    STRATEGY_ROUND_ROBIN,
)
from custom_components.ai_pool.pool import AIPool, AllMembersFailedError

GOOGLE_503 = '{"error": {"code": 503, "status": "UNAVAILABLE"}}'
GOOGLE_429 = '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}'
AUTH = "401 Unauthorized"

A = "ai_task.member_a"
B = "ai_task.member_b"
C = "ai_task.member_c"


def build_entry(
    members: list[str],
    *,
    strategy: str = STRATEGY_PRIORITY,
    limits: dict[str, int] | None = None,
    max_attempts: int = 5,
    cooldown: int = 300,
) -> MockConfigEntry:
    """Create a config entry for a pool of the given members."""
    limits = limits or {}
    return MockConfigEntry(
        domain=DOMAIN,
        title="Test pool",
        data={
            CONF_POOL_TYPE: "ai_task",
            CONF_STRATEGY: strategy,
            CONF_COOLDOWN: cooldown,
            CONF_MAX_ATTEMPTS: max_attempts,
            CONF_MEMBERS: [
                {
                    "entity_id": member,
                    CONF_DAILY_LIMIT: limits.get(member, 0),
                    CONF_WEIGHT: 1,
                }
                for member in members
            ],
        },
    )


@pytest.fixture
def available(hass: HomeAssistant):
    """Mark member entities as present and healthy."""

    def _set(*members: str) -> None:
        for member in members:
            hass.states.async_set(member, "2026-01-01T00:00:00+00:00")

    return _set


async def make_pool(hass: HomeAssistant, entry: MockConfigEntry) -> AIPool:
    """Instantiate and load a pool."""
    entry.add_to_hass(hass)
    pool = AIPool(hass, entry)
    await pool.async_setup()
    return pool


async def test_first_healthy_member_serves(hass: HomeAssistant, available) -> None:
    available(A, B)
    pool = await make_pool(hass, build_entry([A, B]))
    used: list[str] = []

    async def run(member: str) -> str:
        used.append(member)
        return "ok"

    assert await pool.async_execute(run) == "ok"
    assert used == [A]
    assert pool.snapshot()[0]["calls_today"] == 1


async def test_capacity_failure_falls_through_and_cools_down(
    hass: HomeAssistant, available
) -> None:
    """A 503 must not consume the day's allowance for that member."""
    available(A, B)
    pool = await make_pool(hass, build_entry([A, B]))
    used: list[str] = []

    async def run(member: str) -> str:
        used.append(member)
        if member == A:
            raise RuntimeError(GOOGLE_503)
        return "ok"

    assert await pool.async_execute(run) == "ok"
    assert used == [A, B]

    rows = {row["entity_id"]: row for row in pool.snapshot()}
    assert rows[A]["status"] == STATUS_COOLDOWN
    assert rows[A]["failures_today"] == 1
    assert rows[A]["calls_today"] == 0
    assert rows[B]["calls_today"] == 1


async def test_quota_failure_exhausts_for_the_day(
    hass: HomeAssistant, available
) -> None:
    available(A, B)
    pool = await make_pool(hass, build_entry([A, B]))

    async def run(member: str) -> str:
        if member == A:
            raise RuntimeError(GOOGLE_429)
        return "ok"

    await pool.async_execute(run)
    rows = {row["entity_id"]: row for row in pool.snapshot()}
    assert rows[A]["status"] == STATUS_EXHAUSTED


async def test_auth_failure_disables_the_member(hass: HomeAssistant, available) -> None:
    """Bad credentials cannot be retried, so the member stops being asked."""
    available(A, B)
    pool = await make_pool(hass, build_entry([A, B]))

    async def run(member: str) -> str:
        if member == A:
            raise RuntimeError(AUTH)
        return "ok"

    await pool.async_execute(run)
    rows = {row["entity_id"]: row for row in pool.snapshot()}
    assert rows[A]["status"] == STATUS_DISABLED

    # A disabled member is not attempted again at all.
    used: list[str] = []

    async def run2(member: str) -> str:
        used.append(member)
        return "ok"

    await pool.async_execute(run2)
    assert used == [B]


async def test_all_members_failing_raises(hass: HomeAssistant, available) -> None:
    available(A, B)
    pool = await make_pool(hass, build_entry([A, B]))

    async def run(member: str) -> str:
        raise RuntimeError(GOOGLE_503)

    with pytest.raises(AllMembersFailedError):
        await pool.async_execute(run)


async def test_max_attempts_caps_the_fan_out(hass: HomeAssistant, available) -> None:
    """Latency is bounded: a three-deep chain must not try everyone."""
    available(A, B, C)
    pool = await make_pool(hass, build_entry([A, B, C], max_attempts=2))
    used: list[str] = []

    async def run(member: str) -> str:
        used.append(member)
        raise RuntimeError(GOOGLE_503)

    with pytest.raises(AllMembersFailedError):
        await pool.async_execute(run)
    assert len(used) == 2


async def test_round_robin_alternates_between_calls(
    hass: HomeAssistant, available
) -> None:
    """This is the rotation that multiplies per-model daily capacity."""
    available(A, B)
    pool = await make_pool(hass, build_entry([A, B], strategy=STRATEGY_ROUND_ROBIN))
    used: list[str] = []

    async def run(member: str) -> str:
        used.append(member)
        return "ok"

    for _ in range(4):
        await pool.async_execute(run)

    assert used == [A, B, A, B]


async def test_declared_limit_pushes_member_to_the_back(
    hass: HomeAssistant, available
) -> None:
    available(A, B)
    pool = await make_pool(hass, build_entry([A, B], limits={A: 1}))
    used: list[str] = []

    async def run(member: str) -> str:
        used.append(member)
        return "ok"

    await pool.async_execute(run)  # A serves, reaching its limit
    await pool.async_execute(run)  # A is spent, B serves
    assert used == [A, B]

    rows = {row["entity_id"]: row for row in pool.snapshot()}
    assert rows[A]["status"] == STATUS_EXHAUSTED
    assert rows[A]["remaining"] == 0


async def test_spent_member_is_still_tried_as_a_last_resort(
    hass: HomeAssistant, available
) -> None:
    """A declared limit is a guess; it must not cause a silent no-op.

    With the only member's counter spent, the pool still calls it rather than
    refusing, because the guess may be wrong and a real error is more useful
    than nothing happening.
    """
    available(A)
    pool = await make_pool(hass, build_entry([A], limits={A: 1}))
    used: list[str] = []

    async def run(member: str) -> str:
        used.append(member)
        return "ok"

    await pool.async_execute(run)
    await pool.async_execute(run)
    assert used == [A, A]


async def test_unavailable_member_is_skipped(hass: HomeAssistant, available) -> None:
    available(B)
    hass.states.async_set(A, "unavailable")
    pool = await make_pool(hass, build_entry([A, B]))
    used: list[str] = []

    async def run(member: str) -> str:
        used.append(member)
        return "ok"

    await pool.async_execute(run)
    assert used == [B]

    rows = {row["entity_id"]: row for row in pool.snapshot()}
    assert rows[A]["status"] == STATUS_UNAVAILABLE


async def test_missing_member_entity_is_unavailable(
    hass: HomeAssistant, available
) -> None:
    available(B)
    pool = await make_pool(hass, build_entry([A, B]))
    rows = {row["entity_id"]: row for row in pool.snapshot()}
    assert rows[A]["status"] == STATUS_UNAVAILABLE


async def test_cooldown_expires(hass: HomeAssistant, available) -> None:
    available(A, B)
    pool = await make_pool(hass, build_entry([A, B], cooldown=60))

    async def failing(member: str) -> str:
        if member == A:
            raise RuntimeError(GOOGLE_503)
        return "ok"

    await pool.async_execute(failing)
    state = pool.store.state.member(A)
    # Rewind the cooldown to simulate time passing.
    state.cooldown_until = (dt_util.utcnow() - timedelta(seconds=1)).isoformat()

    rows = {row["entity_id"]: row for row in pool.snapshot()}
    assert rows[A]["status"] == STATUS_HEALTHY


async def test_counters_reset_on_a_new_local_day(
    hass: HomeAssistant, available
) -> None:
    """A quota window is a calendar day; a restart must not reset it early."""
    available(A)
    pool = await make_pool(hass, build_entry([A]))

    async def run(member: str) -> str:
        return "ok"

    await pool.async_execute(run)
    assert pool.snapshot()[0]["calls_today"] == 1

    # Pretend the stored counters belong to a previous day.
    pool.store.state.member(A).day = "2000-01-01"
    pool.store.roll_day()
    assert pool.snapshot()[0]["calls_today"] == 0


async def test_empty_pool_raises(hass: HomeAssistant) -> None:
    pool = await make_pool(hass, build_entry([]))

    async def run(member: str) -> str:
        return "ok"

    with pytest.raises(AllMembersFailedError):
        await pool.async_execute(run)


async def test_last_error_is_recorded_with_its_kind(
    hass: HomeAssistant, available
) -> None:
    available(A, B)
    pool = await make_pool(hass, build_entry([A, B]))

    async def run(member: str) -> str:
        if member == A:
            raise RuntimeError(GOOGLE_503)
        return "ok"

    await pool.async_execute(run)
    rows = {row["entity_id"]: row for row in pool.snapshot()}
    assert rows[A]["last_error"].startswith("capacity:")
    assert rows[B]["last_success"] is not None


async def test_member_with_unknown_state_is_healthy(hass: HomeAssistant) -> None:
    """A member not called since startup reports "unknown", not unavailable.

    ai_task, conversation, tts and stt entities all publish their last activity
    as their state, so "unknown" is simply a member nobody has used yet.
    """
    hass.states.async_set(A, "unknown")
    pool = await make_pool(hass, build_entry([A]))

    rows = {row["entity_id"]: row for row in pool.snapshot()}
    assert rows[A]["status"] == STATUS_HEALTHY
