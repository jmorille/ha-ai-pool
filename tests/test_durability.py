"""The risks nothing was testing: concurrency, the clock, and real storage.

Three properties this integration rests on had no test at all. Whether
simultaneous calls can lose a counter was reasoned about, not demonstrated.
Whether the day rolls at midnight was only ever forced by writing a stale date
by hand. And whether counters survive a restart was checked against the
dataclass rather than against Home Assistant's storage helper - which is the
thing that actually has to round-trip them.
"""

import asyncio
from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ai_pool.const import (
    CONF_COOLDOWN,
    CONF_DAILY_LIMIT,
    CONF_MAX_ATTEMPTS,
    CONF_MEMBERS,
    CONF_POOL_TYPE,
    CONF_RPM_LIMIT,
    CONF_STRATEGY,
    CONF_TIMEOUT,
    CONF_WEIGHT,
    DOMAIN,
    STRATEGY_LEAST_USED,
    STRATEGY_ROUND_ROBIN,
)
from custom_components.ai_pool.pool import AIPool
from custom_components.ai_pool.store import UsageStore

A = "ai_task.member_a"
B = "ai_task.member_b"
C = "ai_task.member_c"


def build_entry(
    members: list[str],
    *,
    strategy: str = STRATEGY_ROUND_ROBIN,
    limits: dict[str, int] | None = None,
    weights: dict[str, int] | None = None,
) -> MockConfigEntry:
    """Create a config entry for a pool of the given members."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Test pool",
        data={
            CONF_POOL_TYPE: "ai_task",
            CONF_STRATEGY: strategy,
            CONF_COOLDOWN: 300,
            CONF_MAX_ATTEMPTS: 3,
            CONF_TIMEOUT: 0,
            CONF_MEMBERS: [
                {
                    "entity_id": member,
                    CONF_DAILY_LIMIT: (limits or {}).get(member, 0),
                    CONF_RPM_LIMIT: 0,
                    CONF_WEIGHT: (weights or {}).get(member, 1),
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


# --- Concurrency -------------------------------------------------------------


async def test_simultaneous_calls_lose_no_counter(
    hass: HomeAssistant, available
) -> None:
    """A pool is re-entrant: scripts and automations call it at the same time.

    Every mutation on the request path is deliberately await-free so that two
    in-flight calls cannot read the same counter and write it back twice. That
    is a property of the code, and this is what holds it to it.
    """
    available(A, B, C)
    pool = await make_pool(hass, build_entry([A, B, C]))

    async def run(member: str) -> str:
        # Yield control mid-call, which is where an interleaving would happen.
        await asyncio.sleep(0)
        return "ok"

    await asyncio.gather(*(pool.async_execute(run) for _ in range(30)))

    rows = pool.snapshot()
    assert sum(row.calls_today for row in rows) == 30
    assert sum(row.requests_today for row in rows) == 30
    routing = pool.routing_snapshot()
    assert routing["requests_today"] == 30
    assert routing["served_today"] == 30
    assert routing["attempts_today"] == 30


async def test_simultaneous_calls_still_rotate(hass: HomeAssistant, available) -> None:
    """The cursor advances synchronously, so concurrency must not skew it."""
    available(A, B, C)
    pool = await make_pool(hass, build_entry([A, B, C]))
    served: list[str] = []

    async def run(member: str) -> str:
        served.append(member)
        await asyncio.sleep(0)
        return "ok"

    await asyncio.gather(*(pool.async_execute(run) for _ in range(30)))

    # Thirty calls over three members, whatever order they interleaved in.
    assert {served.count(member) for member in (A, B, C)} == {10}


async def test_a_failure_during_a_concurrent_call_is_attributed_correctly(
    hass: HomeAssistant, available
) -> None:
    """Failures are counted per member, not per request."""
    available(A, B)
    pool = await make_pool(hass, build_entry([A, B]))

    async def run(member: str) -> str:
        await asyncio.sleep(0)
        if member == A:
            raise RuntimeError("Error talking to API")
        return "ok"

    await asyncio.gather(*(pool.async_execute(run) for _ in range(6)))

    rows = {row.entity_id: row for row in pool.snapshot()}
    assert rows[A].calls_today == 0
    assert rows[A].failures_today == 3
    assert rows[B].calls_today == 6


# --- The clock ---------------------------------------------------------------


async def test_the_day_rolls_on_its_own_at_midnight(
    hass: HomeAssistant, available, freezer: FrozenDateTimeFactory
) -> None:
    """A pool called once a morning showed yesterday's numbers until noon.

    Nothing rolled the day except using the pool, and reading a sensor is not
    using it - so the midnight trigger is what makes a display right.
    """
    # Times in UTC and a full day of travel: the test instance does not run in
    # the same timezone as the deployment, and the day that matters is local.
    freezer.move_to("2026-09-04 10:00:00+00:00")
    available(A)
    entry = build_entry([A])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    pool: AIPool = entry.runtime_data

    async def run(member: str) -> str:
        return "ok"

    await pool.async_execute(run)
    assert pool.snapshot()[0].calls_today == 1

    # Cross one local midnight without touching the pool, wherever it falls.
    freezer.move_to("2026-09-05 10:00:01+00:00")
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert pool.snapshot()[0].calls_today == 0
    assert pool.routing_snapshot()["requests_today"] == 0


async def test_a_cooldown_expires_without_the_pool_being_used(
    hass: HomeAssistant, available, freezer: FrozenDateTimeFactory
) -> None:
    """Member health has to be right when nothing is calling the pool."""
    freezer.move_to("2026-09-04 10:00:00+00:00")
    available(A, B)
    pool = await make_pool(hass, build_entry([A, B]))

    async def run(member: str) -> str:
        if member == A:
            raise RuntimeError('{"error": {"code": 503, "status": "UNAVAILABLE"}}')
        return "ok"

    await pool.async_execute(run)
    rows = {row.entity_id: row for row in pool.snapshot()}
    assert rows[A].status == "cooldown"

    # The default cooldown is 300 seconds and this is a pure read of the clock.
    freezer.tick(timedelta(seconds=301))
    rows = {row.entity_id: row for row in pool.snapshot()}
    assert rows[A].status == "healthy"


# --- Real storage ------------------------------------------------------------


async def test_counters_survive_the_storage_round_trip(
    hass: HomeAssistant, available, hass_storage
) -> None:
    """A restart at 18:00 must not hand a spent member a fresh allowance.

    Checked through Home Assistant's storage helper rather than the dataclass,
    because the helper is what has to serialise every field - including the
    nested request log and the failure-kind map.
    """
    available(A, B)
    entry = build_entry([A, B], limits={A: 10})
    pool = await make_pool(hass, entry)

    async def run(member: str) -> str:
        if member == A:
            raise RuntimeError('{"error": {"code": 503, "status": "UNAVAILABLE"}}')
        return "ok"

    await pool.async_execute(run, size=42)
    # The request path schedules a delayed write; force it out now.
    await pool.store.async_save()

    assert f"{DOMAIN}.{entry.entry_id}" in hass_storage

    # A fresh store over the same key is exactly what a restart produces.
    restored = UsageStore(hass, entry.entry_id)
    await restored.async_load()

    member_a = restored.state.member(A)
    assert member_a.failures == 1
    assert member_a.failures_by_kind == {"capacity": 1}
    assert member_a.cooldown_until is not None
    assert member_a.cooldown_strikes == 1
    assert member_a.requests == 1
    assert member_a.input_chars == 42
    assert member_a.requests_last_minute() == 1

    member_b = restored.state.member(B)
    assert member_b.calls == 1
    assert member_b.latency_last is not None

    assert restored.state.stats.requests == 1
    assert restored.state.stats.fallbacks == 1


async def test_a_departed_member_is_forgotten(hass: HomeAssistant, available) -> None:
    """State was created on first use and never removed."""
    available(A, B)
    entry = build_entry([A, B])
    pool = await make_pool(hass, entry)

    async def run(member: str) -> str:
        return "ok"

    await pool.async_execute(run)
    await pool.store.async_save()
    assert A in pool.store.state.members

    # B alone from now on.
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_MEMBERS: [
                {"entity_id": B, CONF_DAILY_LIMIT: 0, CONF_WEIGHT: 1},
            ],
        },
    )
    reloaded = AIPool(hass, entry)
    await reloaded.async_setup()

    assert A not in reloaded.store.state.members
    assert [row.entity_id for row in reloaded.snapshot()] == [B]


# --- Strategies through the pool ---------------------------------------------


async def test_least_used_spreads_by_declared_headroom(
    hass: HomeAssistant, available
) -> None:
    """Exercised through the pool, not just through the ordering function.

    The strategy only means something once the pool's own partitioning has had
    its say, and that combination had no test.
    """
    available(A, B)
    pool = await make_pool(
        hass,
        build_entry([A, B], strategy=STRATEGY_LEAST_USED, limits={A: 10, B: 90}),
    )
    served: list[str] = []

    async def run(member: str) -> str:
        served.append(member)
        return "ok"

    for _ in range(10):
        await pool.async_execute(run)

    # B declares nine times the allowance, so it should take most of the work.
    assert served.count(B) > served.count(A)
    rows = {row.entity_id: row for row in pool.snapshot()}
    assert rows[A].remaining == 10 - served.count(A)
    assert rows[B].remaining == 90 - served.count(B)


async def test_weight_shifts_the_rotation(hass: HomeAssistant, available) -> None:
    """Weights are configurable, so they have to change something."""
    available(A, B)
    pool = await make_pool(
        hass,
        build_entry(
            [A, B],
            strategy=STRATEGY_LEAST_USED,
            limits={A: 100, B: 100},
            weights={A: 1, B: 10},
        ),
    )
    served: list[str] = []

    async def run(member: str) -> str:
        served.append(member)
        return "ok"

    for _ in range(12):
        await pool.async_execute(run)

    assert served.count(A) != served.count(B)
