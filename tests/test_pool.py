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
    CONF_RPM_LIMIT,
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
from custom_components.ai_pool.store import (
    MAX_REQUEST_LOG,
    RECENT_LATENCY_SAMPLES,
    MemberState,
)

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
    rpm: dict[str, int] | None = None,
    max_attempts: int = 5,
    cooldown: int = 300,
) -> MockConfigEntry:
    """Create a config entry for a pool of the given members."""
    limits = limits or {}
    rpm = rpm or {}
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
                    CONF_RPM_LIMIT: rpm.get(member, 0),
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


# --- Metrics ----------------------------------------------------------------


def test_recent_latency_ring_is_bounded() -> None:
    """A long-running instance must not grow the stored state without bound."""
    state = MemberState()
    for index in range(RECENT_LATENCY_SAMPLES + 5):
        state.record_latency(float(index))

    assert len(state.latency_recent) == RECENT_LATENCY_SAMPLES
    assert state.latency_recent[0] == 5.0


async def test_latency_is_recorded_for_successes_only(
    hass: HomeAssistant, available
) -> None:
    """How long a refusal took is not how long a usable answer takes."""
    available(A, B)
    pool = await make_pool(hass, build_entry([A, B]))

    async def run(member: str) -> str:
        if member == A:
            raise RuntimeError(GOOGLE_503)
        return "ok"

    await pool.async_execute(run)

    rows = {row["entity_id"]: row for row in pool.snapshot()}
    assert rows[A]["latency_samples"] == 0
    assert rows[A]["latency_last"] is None
    assert rows[B]["latency_samples"] == 1
    assert rows[B]["latency_last"] >= 0
    assert rows[B]["latency_average"] == rows[B]["latency_last"]
    assert rows[B]["latency_recent_average"] == rows[B]["latency_last"]


async def test_failures_are_counted_by_kind(hass: HomeAssistant, available) -> None:
    """A member that is out of quota and one that times out need different fixes."""
    available(A)
    pool = await make_pool(hass, build_entry([A]))
    messages = iter([GOOGLE_503, GOOGLE_429])

    async def run(member: str) -> str:
        raise RuntimeError(next(messages))

    for _ in range(2):
        with pytest.raises(AllMembersFailedError):
            await pool.async_execute(run)

    row = pool.snapshot()[0]
    assert row["failures_by_kind"] == {"capacity": 1, "quota": 1}
    assert row["failures_today"] == 2
    assert row["success_rate"] == 0.0


async def test_success_rate_mixes_calls_and_failures(
    hass: HomeAssistant, available
) -> None:
    available(A)
    pool = await make_pool(hass, build_entry([A]))
    outcomes = iter([True, False])

    async def run(member: str) -> str:
        if next(outcomes):
            return "ok"
        raise RuntimeError(GOOGLE_503)

    await pool.async_execute(run)
    with pytest.raises(AllMembersFailedError):
        await pool.async_execute(run)

    assert pool.snapshot()[0]["success_rate"] == 50.0


async def test_routing_snapshot_tracks_fallbacks(
    hass: HomeAssistant, available
) -> None:
    """The fallback rate is the signal that the preference order is wrong."""
    available(A, B)
    pool = await make_pool(hass, build_entry([A, B]))

    async def run(member: str) -> str:
        if member == A:
            raise RuntimeError(GOOGLE_503)
        return "ok"

    await pool.async_execute(run)  # A refuses, B serves: two attempts
    await pool.async_execute(run)  # A is cooling down, B serves: one attempt

    routing = pool.routing_snapshot()
    assert routing["requests_today"] == 2
    assert routing["served_today"] == 2
    assert routing["failed_today"] == 0
    assert routing["attempts_today"] == 3
    assert routing["fallbacks_today"] == 1
    assert routing["fallback_rate"] == 50.0
    assert routing["attempts_per_request"] == 1.5
    assert routing["last_attempts"] == 1
    assert routing["last_member"] == B
    assert routing["members_total"] == 2
    assert routing["members_healthy"] == 1


async def test_routing_snapshot_counts_a_failed_request(
    hass: HomeAssistant, available
) -> None:
    available(A)
    pool = await make_pool(hass, build_entry([A]))

    async def run(member: str) -> str:
        raise RuntimeError(GOOGLE_503)

    with pytest.raises(AllMembersFailedError):
        await pool.async_execute(run)

    routing = pool.routing_snapshot()
    assert routing["requests_today"] == 1
    assert routing["served_today"] == 0
    assert routing["failed_today"] == 1
    assert routing["last_member"] is None


async def test_empty_pool_has_no_routing_metrics(hass: HomeAssistant) -> None:
    """Nothing was routed, so a rate would be a made-up number."""
    pool = await make_pool(hass, build_entry([]))

    routing = pool.routing_snapshot()
    assert routing["requests_today"] == 0
    assert routing["fallback_rate"] is None
    assert routing["attempts_per_request"] is None


async def test_day_roll_resets_metrics_but_keeps_recent_latency(
    hass: HomeAssistant, available
) -> None:
    """Day counters describe the quota window; recent latency does not."""
    available(A)
    pool = await make_pool(hass, build_entry([A]))

    async def run(member: str) -> str:
        return "ok"

    await pool.async_execute(run)
    recent = pool.snapshot()[0]["latency_recent_average"]
    assert recent is not None

    pool.store.state.member(A).day = "2000-01-01"
    pool.store.state.stats.day = "2000-01-01"
    pool.store.roll_day()

    row = pool.snapshot()[0]
    assert row["latency_samples"] == 0
    assert row["latency_average"] is None
    assert row["failures_by_kind"] == {}
    assert row["latency_recent_average"] == recent
    assert pool.routing_snapshot()["requests_today"] == 0


async def test_requests_count_attempts_not_successes(
    hass: HomeAssistant, available
) -> None:
    """A provider charges the request when it receives it, refusal included."""
    available(A, B)
    pool = await make_pool(hass, build_entry([A, B]))

    async def run(member: str) -> str:
        if member == A:
            raise RuntimeError(GOOGLE_503)
        return "ok"

    await pool.async_execute(run)

    rows = {row["entity_id"]: row for row in pool.snapshot()}
    assert rows[A]["calls_today"] == 0
    assert rows[A]["requests_today"] == 1
    assert rows[B]["calls_today"] == 1
    assert rows[B]["requests_today"] == 1


async def test_rate_headroom_uses_declared_limits(
    hass: HomeAssistant, available
) -> None:
    """Both dimensions are reported against what the user declared."""
    available(A)
    pool = await make_pool(hass, build_entry([A], limits={A: 200}, rpm={A: 10}))

    async def run(member: str) -> str:
        return "ok"

    await pool.async_execute(run)
    await pool.async_execute(run)

    row = pool.snapshot()[0]
    assert row["requests_last_minute"] == 2
    assert row["rpm_limit"] == 10
    assert row["rpm_remaining"] == 8
    assert row["daily_limit"] == 200
    assert row["rpd_remaining"] == 198


async def test_undeclared_limits_report_no_headroom(
    hass: HomeAssistant, available
) -> None:
    """With no declared limit there is no remaining count to invent."""
    available(A)
    pool = await make_pool(hass, build_entry([A]))

    async def run(member: str) -> str:
        return "ok"

    await pool.async_execute(run)

    row = pool.snapshot()[0]
    assert row["requests_last_minute"] == 1
    assert row["rpm_limit"] is None
    assert row["rpm_remaining"] is None
    assert row["rpd_remaining"] is None


async def test_input_size_is_tracked(hass: HomeAssistant, available) -> None:
    """Characters stand in for tokens, which never reach the integration."""
    available(A)
    pool = await make_pool(hass, build_entry([A]))

    async def run(member: str) -> str:
        return "ok"

    await pool.async_execute(run, size=120)
    await pool.async_execute(run, size=80)

    row = pool.snapshot()[0]
    assert row["input_chars_today"] == 200
    assert row["input_chars_last_minute"] == 200


def test_request_window_forgets_older_requests() -> None:
    """A per-minute limit knows nothing about what happened two minutes ago."""
    state = MemberState()
    state.record_request(10, now=1000.0)
    state.record_request(10, now=1030.0)

    assert state.requests_last_minute(now=1030.0) == 2
    assert state.input_chars_last_minute(now=1030.0) == 20

    # A minute later the first request has aged out, the second has not.
    assert state.requests_last_minute(now=1070.0) == 1
    assert state.input_chars_last_minute(now=1070.0) == 10
    assert state.requests_last_minute(now=1200.0) == 0

    # Day counters are unaffected by the window sliding.
    assert state.requests == 2
    assert state.input_chars == 20


def test_request_log_is_bounded() -> None:
    """A runaway caller must not grow the stored state without bound."""
    state = MemberState()
    for index in range(MAX_REQUEST_LOG + 50):
        state.record_request(1, now=1000.0 + index * 0.001)

    assert len(state.request_log) == MAX_REQUEST_LOG
    assert state.requests == MAX_REQUEST_LOG + 50


def test_day_roll_keeps_the_rate_window() -> None:
    """A request made at 23:59:50 still counts against RPM at 00:00:05."""
    state = MemberState()
    state.record_request(30, now=1000.0)
    state.reset_day("2026-09-03")

    assert state.requests == 0
    assert state.input_chars == 0
    assert state.requests_last_minute(now=1015.0) == 1


def test_request_log_survives_a_round_trip() -> None:
    """The window is persisted, so a restart does not hide recent requests."""
    state = MemberState()
    state.record_request(42, now=1000.0)

    restored = MemberState.from_dict(state.as_dict())

    assert restored.requests_last_minute(now=1010.0) == 1
    assert restored.input_chars_last_minute(now=1010.0) == 42
