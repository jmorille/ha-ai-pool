"""Behaviour that keeps a pool serving: deadlines, backoff, recovery, events.

These cover the five failure modes the pool was blind to: members that are
secretly the same model, members that never answer, members held back for good,
a total failure nothing observes, and a fixed cooldown that keeps knocking on a
saturated provider's door.
"""

import asyncio
from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
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
    EVENT_EXHAUSTED,
    EVENT_FAILOVER,
    ISSUE_DUPLICATE_MODEL,
    MAX_COOLDOWN,
    SERVICE_RESET_MEMBER,
    STATUS_COOLDOWN,
    STATUS_DISABLED,
    STATUS_EXHAUSTED,
    STATUS_HEALTHY,
    STATUS_THROTTLED,
    STRATEGY_PRIORITY,
    STRATEGY_ROUND_ROBIN,
)
from custom_components.ai_pool.errors import FailureKind
from custom_components.ai_pool.models import member_model, shared_models
from custom_components.ai_pool.pool import AIPool, AllMembersFailedError
from custom_components.ai_pool.store import parse_iso

GOOGLE_503 = '{"error": {"code": 503, "status": "UNAVAILABLE"}}'
AUTH = "401 Unauthorized"

A = "ai_task.member_a"
B = "ai_task.member_b"
C = "ai_task.member_c"


def build_entry(
    members: list[str],
    *,
    limits: dict[str, int] | None = None,
    rpm: dict[str, int] | None = None,
    cooldown: int = 300,
    max_attempts: int = 5,
    timeout: float = 0,
) -> MockConfigEntry:
    """Create a config entry for a pool of the given members."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Test pool",
        data={
            CONF_POOL_TYPE: "ai_task",
            CONF_STRATEGY: STRATEGY_PRIORITY,
            CONF_COOLDOWN: cooldown,
            CONF_MAX_ATTEMPTS: max_attempts,
            CONF_TIMEOUT: timeout,
            CONF_MEMBERS: [
                {
                    "entity_id": member,
                    CONF_DAILY_LIMIT: (limits or {}).get(member, 0),
                    CONF_RPM_LIMIT: (rpm or {}).get(member, 0),
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


# --- 1. Members that are secretly the same model ----------------------------


def test_shared_models_ignores_unknown_models() -> None:
    """An unreadable model is not evidence that two members match."""
    assert shared_models({A: None, B: None}) == {}
    assert shared_models({A: "flash", B: "pro"}) == {}
    assert shared_models({A: "flash", B: "flash", C: "pro"}) == {"flash": [A, B]}


async def test_member_model_reads_the_config_entry(hass: HomeAssistant) -> None:
    """A member's model comes from the entry that created it."""
    provider = MockConfigEntry(
        domain="fake_provider", data={"chat_model": "models/from-data"}
    )
    provider.add_to_hass(hass)
    entity = er.async_get(hass).async_get_or_create(
        "ai_task", "fake_provider", "unique-a", config_entry=provider
    )

    assert member_model(hass, entity.entity_id) == "models/from-data"
    # An entity nothing knows about carries no conclusion.
    assert member_model(hass, "ai_task.not_registered") is None


async def test_member_model_prefers_the_subentry(hass: HomeAssistant) -> None:
    """Providers that publish several entities per account do it via subentries.

    This is the shape that actually matters: two Google accounts, each with an
    ai_task subentry naming the model, is exactly the configuration where two
    members turn out to be one.
    """
    provider = MockConfigEntry(domain="fake_provider", data={"chat_model": "account"})
    provider.add_to_hass(hass)
    subentry = ConfigSubentry(
        data={"chat_model": "models/from-subentry"},
        subentry_type="ai_task_data",
        title="Task",
        unique_id=None,
    )
    hass.config_entries.async_add_subentry(provider, subentry)

    entity = er.async_get(hass).async_get_or_create(
        "ai_task",
        "fake_provider",
        "unique-sub",
        config_entry=provider,
        config_subentry_id=subentry.subentry_id,
    )

    assert member_model(hass, entity.entity_id) == "models/from-subentry"


async def test_capacity_refusal_skips_members_on_the_same_model(
    hass: HomeAssistant, available, monkeypatch
) -> None:
    """Asking the same model twice is asking the same engine the same question."""
    available(A, B, C)
    pool = await make_pool(hass, build_entry([A, B, C]))
    models = {A: "flash", B: "flash", C: "pro"}
    monkeypatch.setattr(AIPool, "member_model", lambda self, key: models[key])

    tried: list[str] = []

    async def run(member: str) -> str:
        tried.append(member)
        if member == A:
            raise RuntimeError(GOOGLE_503)
        return "ok"

    assert await pool.async_execute(run) == "ok"
    # B never gets asked: it is the same model as A, which just refused.
    assert tried == [A, C]
    # And the skip is not charged as an attempt against the request.
    assert pool.routing_snapshot()["attempts_today"] == 2


async def test_a_transient_failure_does_not_skip_the_same_model(
    hass: HomeAssistant, available, monkeypatch
) -> None:
    """Only a capacity refusal implicates the model; a blip implicates nothing."""
    available(A, B)
    pool = await make_pool(hass, build_entry([A, B]))
    monkeypatch.setattr(AIPool, "member_model", lambda self, key: "flash")

    tried: list[str] = []

    async def run(member: str) -> str:
        tried.append(member)
        if member == A:
            raise RuntimeError("Error talking to API")
        return "ok"

    assert await pool.async_execute(run) == "ok"
    assert tried == [A, B]


async def test_duplicate_models_raise_and_clear_a_repair(
    hass: HomeAssistant, monkeypatch
) -> None:
    """A pool of duplicates looks healthy in every sensor, so it needs a repair."""
    entry = build_entry([A, B])
    pool = await make_pool(hass, entry)
    issue_id = f"{ISSUE_DUPLICATE_MODEL}_{entry.entry_id}"

    monkeypatch.setattr(AIPool, "member_model", lambda self, key: "flash")
    pool.async_check_models()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert "flash" in issue.translation_placeholders["models"]

    # Point them at different models and the repair goes away on its own.
    distinct = {A: "flash", B: "pro"}
    monkeypatch.setattr(AIPool, "member_model", lambda self, key: distinct[key])
    pool.async_check_models()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


# --- 2. Members that never answer -------------------------------------------


async def test_a_member_that_hangs_is_abandoned(hass: HomeAssistant, available) -> None:
    """An unbounded wait is not a policy: the deadline turns it into a failover."""
    available(A, B)
    # Far shorter than any real call, so the test does not actually wait.
    pool = await make_pool(hass, build_entry([A, B], timeout=0.01))

    async def run(member: str) -> str:
        if member == A:
            await asyncio.sleep(30)
        return "ok"

    assert await pool.async_execute(run) == "ok"

    rows = {row.entity_id: row for row in pool.snapshot()}
    assert rows[A].failures_by_kind == {FailureKind.TIMEOUT.value: 1}
    # Giving up on a member says nothing about its allowance or its health.
    assert rows[A].status == STATUS_HEALTHY
    assert rows[B].calls_today == 1


async def test_zero_timeout_waits(hass: HomeAssistant, available) -> None:
    """Zero means no deadline, for anyone who would rather wait than fail."""
    available(A)
    pool = await make_pool(hass, build_entry([A], timeout=0))
    assert pool.timeout == 0

    async def run(member: str) -> str:
        await asyncio.sleep(0)
        return "ok"

    assert await pool.async_execute(run) == "ok"


# --- 3. Members held back for good ------------------------------------------


async def test_an_auth_failure_no_longer_retires_a_member(
    hass: HomeAssistant, available
) -> None:
    """A revoked key would otherwise remove a provider permanently."""
    available(A, B)
    entry = build_entry([A, B])
    pool = await make_pool(hass, entry)

    async def run(member: str) -> str:
        if member == A:
            raise RuntimeError(AUTH)
        return "ok"

    await pool.async_execute(run)
    rows = {row.entity_id: row for row in pool.snapshot()}
    assert rows[A].status == STATUS_DISABLED

    # Reloading the entry is the user saying "try again", and it is the only
    # escape a persisted flag would otherwise have.
    reloaded = AIPool(hass, entry)
    await reloaded.async_setup()
    rows = {row.entity_id: row for row in reloaded.snapshot()}
    assert rows[A].status == STATUS_HEALTHY


async def test_reset_reports_only_members_that_were_held(
    hass: HomeAssistant, available
) -> None:
    """Resetting three members and resetting nothing must read differently."""
    available(A, B)
    pool = await make_pool(hass, build_entry([A, B]))

    async def run(member: str) -> str:
        if member == A:
            raise RuntimeError(GOOGLE_503)
        return "ok"

    await pool.async_execute(run)
    assert pool.snapshot()[0].status == STATUS_COOLDOWN

    assert await pool.async_reset_member() == [A]
    assert pool.snapshot()[0].status == STATUS_HEALTHY
    # Nothing is held back any more, so a second reset has nothing to report.
    assert await pool.async_reset_member() == []


async def test_reset_member_service_clears_one_member(
    hass: HomeAssistant, available
) -> None:
    """The service is the only way back for a member the pool gave up on."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "ai_task", {})
    available(A, B)

    entry = build_entry([A, B])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    pool: AIPool = entry.runtime_data
    pool.store.state.member(A).disabled_reason = "authentication"
    assert pool.snapshot()[0].status == STATUS_DISABLED

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_MEMBER,
        {"pool": entry.entry_id, "member": A},
        blocking=True,
    )
    assert pool.snapshot()[0].status == STATUS_HEALTHY


async def test_reset_member_service_rejects_a_stranger(
    hass: HomeAssistant, available
) -> None:
    """Resetting something that is not in the pool is a mistake worth naming."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "ai_task", {})
    available(A)

    entry = build_entry([A])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError, match=r"not_a_member|not a member"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_MEMBER,
            {"pool": entry.entry_id, "member": "ai_task.stranger"},
            blocking=True,
        )


# --- 4. A total failure nothing observes ------------------------------------


async def test_failover_and_exhaustion_are_announced(
    hass: HomeAssistant, available
) -> None:
    """The quiet failure: every member refused and only the trace knew."""
    available(A, B)
    pool = await make_pool(hass, build_entry([A, B]))
    failovers = async_capture_events(hass, EVENT_FAILOVER)
    exhausted = async_capture_events(hass, EVENT_EXHAUSTED)

    async def run(member: str) -> str:
        raise RuntimeError(GOOGLE_503)

    with pytest.raises(AllMembersFailedError):
        await pool.async_execute(run, description="weather")
    await hass.async_block_till_done()

    assert [event.data["member"] for event in failovers] == [A, B]
    assert failovers[0].data["kind"] == FailureKind.CAPACITY.value
    assert failovers[0].data["pool"] == "Test pool"
    assert failovers[0].data["pool_type"] == "ai_task"
    assert failovers[0].data["description"] == "weather"

    assert len(exhausted) == 1
    assert exhausted[0].data["attempts"] == 2
    assert exhausted[0].data["entry_id"] == pool.entry.entry_id


async def test_a_served_request_announces_nothing(
    hass: HomeAssistant, available
) -> None:
    """Events are for trouble; a working pool should be silent."""
    available(A)
    pool = await make_pool(hass, build_entry([A]))
    failovers = async_capture_events(hass, EVENT_FAILOVER)
    exhausted = async_capture_events(hass, EVENT_EXHAUSTED)

    async def run(member: str) -> str:
        return "ok"

    await pool.async_execute(run)
    await hass.async_block_till_done()

    assert failovers == []
    assert exhausted == []


async def test_problem_sensor_follows_member_health(
    hass: HomeAssistant, available
) -> None:
    """One entity answering whether this pool can serve at all."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "ai_task", {})
    available(A)

    entry = build_entry([A])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    problem = next(
        entity.entity_id
        for entity in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if entity.domain == "binary_sensor"
    )
    assert hass.states.get(problem).state == "off"

    hass.states.async_set(A, "unavailable")
    await hass.services.async_call(
        "homeassistant", "update_entity", {"entity_id": problem}, blocking=True
    )

    state = hass.states.get(problem)
    assert state.state == "on"
    assert state.attributes["members_healthy"] == 0


# --- 5. A cooldown that keeps knocking --------------------------------------


async def test_consecutive_refusals_lengthen_the_cooldown(
    hass: HomeAssistant, available
) -> None:
    """Capacity refusals cluster, so the wait doubles instead of repeating."""
    available(A)
    pool = await make_pool(hass, build_entry([A], cooldown=300))

    assert pool._backoff(1).total_seconds() == 300
    assert pool._backoff(2).total_seconds() == 600
    assert pool._backoff(3).total_seconds() == 1200
    # Capped, so a provider having a bad day stays in hourly rotation.
    assert pool._backoff(99).total_seconds() == MAX_COOLDOWN


async def test_a_success_resets_the_backoff(hass: HomeAssistant, available) -> None:
    """Only a success is evidence that the provider has recovered."""
    available(A)
    pool = await make_pool(hass, build_entry([A], cooldown=300))
    refuse = True

    async def run(member: str) -> str:
        if refuse:
            raise RuntimeError(GOOGLE_503)
        return "ok"

    with pytest.raises(AllMembersFailedError):
        await pool.async_execute(run)
    with pytest.raises(AllMembersFailedError):
        await pool.async_execute(run)

    state = pool.store.state.member(A)
    assert state.cooldown_strikes == 2
    first = parse_iso(state.cooldown_until)
    assert first is not None

    refuse = False
    await pool.async_reset_member(A)
    assert await pool.async_execute(run) == "ok"
    assert pool.store.state.member(A).cooldown_strikes == 0


async def test_strikes_are_reported(hass: HomeAssistant, available) -> None:
    """The strike count explains an unusually long cooldown on a dashboard."""
    available(A)
    pool = await make_pool(hass, build_entry([A]))

    async def run(member: str) -> str:
        raise RuntimeError(GOOGLE_503)

    with pytest.raises(AllMembersFailedError):
        await pool.async_execute(run)

    assert pool.snapshot()[0].cooldown_strikes == 1


async def test_calls_sensor_carries_model_and_strikes(
    hass: HomeAssistant, available, monkeypatch
) -> None:
    """Both facts are documented as dashboard-visible, so they must be there."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "ai_task", {})
    available(A)
    monkeypatch.setattr(AIPool, "member_model", lambda self, key: "models/flash")

    entry = build_entry([A])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Picked by unique_id: the entity id is built from a translated name, and
    # the test instance does not speak the same language as the deployment.
    calls = next(
        entity.entity_id
        for entity in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if entity.unique_id == f"{entry.entry_id}_{A}"
    )
    attributes = hass.states.get(calls).attributes
    assert attributes["model"] == "models/flash"
    assert attributes["cooldown_strikes"] == 0


# --- What the audit found -----------------------------------------------------


async def test_yesterdays_traffic_does_not_look_like_exhaustion(
    hass: HomeAssistant, available
) -> None:
    """Day counters are only rolled when the pool is used.

    At 00:00 they still hold yesterday's numbers, and the problem sensor is
    polled - so it asks this question when nothing has rolled anything.
    """
    available(A)
    entry = build_entry([A])
    entry.add_to_hass(hass)
    pool = AIPool(hass, entry)
    await pool.async_setup()
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_MEMBERS: [
                {"entity_id": A, CONF_DAILY_LIMIT: 50, CONF_WEIGHT: 1},
            ],
        },
    )

    state = pool.store.state.member(A)
    state.day = "2000-01-01"
    state.calls = 50

    member = pool.members[0]
    assert member.daily_limit == 50
    # Fifty calls, but not today's fifty.
    assert pool.member_status(member) == STATUS_HEALTHY


async def test_rotation_stays_even_while_a_member_sits_out(
    hass: HomeAssistant, available
) -> None:
    """The cursor must not be taken modulo the members that happen to be up.

    With three members and one in cooldown it used to cycle 0,1,2 over a
    two-member group, so one member served two calls in three.
    """
    available(A, B, C)
    entry = build_entry([A, B, C], max_attempts=1)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_STRATEGY: STRATEGY_ROUND_ROBIN}
    )
    pool = AIPool(hass, entry)
    await pool.async_setup()

    # A is out of the running, so B and C should split the traffic evenly.
    pool.store.state.member(A).cooldown_until = (
        dt_util.utcnow() + timedelta(hours=1)
    ).isoformat()

    served: list[str] = []

    async def run(member: str) -> str:
        served.append(member)
        return "ok"

    for _ in range(6):
        await pool.async_execute(run)

    assert served.count(B) == 3, served
    assert served.count(C) == 3, served


async def test_a_member_at_its_rpm_ceiling_is_demoted_not_dropped(
    hass: HomeAssistant, available
) -> None:
    """A declared per-minute limit has to steer something to be worth asking for.

    Like every declared limit it only demotes: the number is the user's
    estimate, so the member stays in the queue as a last resort.
    """
    available(A, B)
    pool = await make_pool(hass, build_entry([A, B], rpm={A: 2}))

    served: list[str] = []

    async def run(member: str) -> str:
        served.append(member)
        return "ok"

    await pool.async_execute(run)
    await pool.async_execute(run)
    assert served == [A, A]

    rows = {row.entity_id: row for row in pool.snapshot()}
    assert rows[A].status == STATUS_THROTTLED
    assert rows[A].rpm_remaining == 0

    # Third call goes to B, which is not throttled.
    await pool.async_execute(run)
    assert served[-1] == B

    # And A is still reachable when it is the only one left.
    hass.states.async_set(B, "unavailable")
    await pool.async_execute(run)
    assert served[-1] == A


async def test_reset_sees_a_locally_spent_allowance(
    hass: HomeAssistant, available
) -> None:
    """Reporting nothing to reset while the sensor reads exhausted was a lie."""
    available(A)
    pool = await make_pool(hass, build_entry([A], limits={A: 2}))

    async def run(member: str) -> str:
        return "ok"

    await pool.async_execute(run)
    await pool.async_execute(run)
    assert pool.snapshot()[0].status == STATUS_EXHAUSTED

    # The member is held back, and the service says so.
    assert await pool.async_reset_member(A) == [A]
    # But the allowance is counted from our own counter, so it takes the
    # counters going with it.
    assert pool.snapshot()[0].status == STATUS_EXHAUSTED

    assert await pool.async_reset_member(A, clear_counters=True) == [A]
    row = pool.snapshot()[0]
    assert row.status == STATUS_HEALTHY
    assert row.calls_today == 0


async def test_the_duplicate_model_repair_does_not_outlive_the_pool(
    hass: HomeAssistant, available, monkeypatch
) -> None:
    """The issue id embeds the entry id, so a leaked issue is unclearable."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "ai_task", {})
    available(A, B)
    monkeypatch.setattr(AIPool, "member_model", lambda self, key: "flash")

    entry = build_entry([A, B])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    issue_id = f"{ISSUE_DUPLICATE_MODEL}_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
