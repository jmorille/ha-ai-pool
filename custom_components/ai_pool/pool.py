"""Pool runtime: member selection, failover and bookkeeping.

The pool is deliberately provider-agnostic. Platforms supply a callable that
performs the real work against one member entity; everything about *which*
member and *what to do when it fails* lives here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, TypeVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import (
    CONF_COOLDOWN,
    CONF_DAILY_LIMIT,
    CONF_MAX_ATTEMPTS,
    CONF_MEMBERS,
    CONF_POOL_TYPE,
    CONF_RPM_LIMIT,
    CONF_STRATEGY,
    CONF_TIMEOUT,
    CONF_WEIGHT,
    CURSOR_MODULUS,
    DEFAULT_COOLDOWN,
    DEFAULT_DAILY_LIMIT,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RPM_LIMIT,
    DEFAULT_STRATEGY,
    DEFAULT_TIMEOUT,
    DEFAULT_WEIGHT,
    DOMAIN,
    EVENT_EXHAUSTED,
    EVENT_FAILOVER,
    ISSUE_DUPLICATE_MODEL,
    MAX_COOLDOWN,
    MODEL_CACHE_TTL,
    STATUS_COOLDOWN,
    STATUS_DISABLED,
    STATUS_EXHAUSTED,
    STATUS_HEALTHY,
    STATUS_THROTTLED,
    STATUS_UNAVAILABLE,
)
from .errors import FailureKind, Verdict, classify
from .models import member_model, shared_models
from .store import UsageStore, days_ahead, parse_iso
from .strategies import Candidate, order_candidates
from .views import MemberView

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


def _rounded(value: float | None, digits: int = 3) -> float | None:
    """Round a metric for display, keeping None as None.

    Rounded at the edge rather than at the source: the stored values stay
    exact, while the attributes the recorder writes on every call stay short.
    """
    return None if value is None else round(value, digits)


# Only "unavailable" means unusable. "unknown" is the resting state of every
# ai_task, conversation, tts and stt entity, whose state is the timestamp of
# their last activity: treating it as unusable would demote every member the
# instance has not called since it started.
UNAVAILABLE_STATES = frozenset({"unavailable"})


class AllMembersFailedError(HomeAssistantError):
    """Every member of the pool refused or failed the request."""


@dataclass(frozen=True)
class MemberConfig:
    """Static configuration for one pool member."""

    entity_id: str
    daily_limit: int = DEFAULT_DAILY_LIMIT
    rpm_limit: int = DEFAULT_RPM_LIMIT
    weight: int = DEFAULT_WEIGHT

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemberConfig:
        """Build from stored config-entry data."""
        return cls(
            entity_id=data["entity_id"],
            daily_limit=int(data.get(CONF_DAILY_LIMIT, DEFAULT_DAILY_LIMIT) or 0),
            rpm_limit=int(data.get(CONF_RPM_LIMIT, DEFAULT_RPM_LIMIT) or 0),
            weight=int(data.get(CONF_WEIGHT, DEFAULT_WEIGHT) or 1),
        )


class AIPool:
    """Routes calls across member entities and tracks their health."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the pool for a config entry."""
        self.hass = hass
        self.entry = entry
        self.store = UsageStore(hass, entry.entry_id)
        self._listeners: list[Callable[[], None]] = []
        self._models: dict[str, str | None] = {}
        self._models_expire = 0.0

    # --- Configuration ------------------------------------------------------

    @property
    def _config(self) -> dict[str, Any]:
        """Merged config-entry data, options winning over initial data."""
        return {**self.entry.data, **self.entry.options}

    @property
    def pool_type(self) -> str:
        """Which Home Assistant domain this pool fronts."""
        return self._config[CONF_POOL_TYPE]

    @property
    def strategy(self) -> str:
        """Configured ordering strategy."""
        return self._config.get(CONF_STRATEGY, DEFAULT_STRATEGY)

    @property
    def cooldown(self) -> timedelta:
        """How long a member sits out after a capacity refusal."""
        return timedelta(seconds=int(self._config.get(CONF_COOLDOWN, DEFAULT_COOLDOWN)))

    @property
    def max_attempts(self) -> int:
        """Upper bound on member attempts for a single request."""
        return max(int(self._config.get(CONF_MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS)), 1)

    @property
    def timeout(self) -> float:
        """Seconds to wait on one member before giving up on it. 0 disables."""
        return max(float(self._config.get(CONF_TIMEOUT, DEFAULT_TIMEOUT) or 0), 0)

    @property
    def members(self) -> list[MemberConfig]:
        """Configured members, in declared order."""
        return [
            MemberConfig.from_dict(item) for item in self._config.get(CONF_MEMBERS, [])
        ]

    # --- Lifecycle ----------------------------------------------------------

    async def async_setup(self) -> None:
        """Load persisted counters, purge departed members, re-admit the held."""
        await self.store.async_load()
        dirty = False

        if dropped := self.store.prune({config.entity_id for config in self.members}):
            _LOGGER.info(
                "Pool %s: forgetting counters for member(s) no longer in the pool: %s",
                self.entry.title,
                ", ".join(dropped),
            )
            dirty = True

        # Setting up or reloading the entry is the user's way of saying "try
        # again", and it is the only escape from a permanent disable.
        if cleared := self.store.clear_disabled():
            _LOGGER.info(
                "Pool %s: re-admitting previously disabled member(s): %s",
                self.entry.title,
                ", ".join(cleared),
            )
            dirty = True

        if dirty:
            await self.store.async_save()

    async def async_reset_member(
        self, member: str | None = None, *, clear_counters: bool = False
    ) -> list[str]:
        """Clear the penalties on one member, or on all of them.

        Returns the members that were actually holding something back, so a
        caller can tell "reset three members" from "there was nothing to
        reset". A member spent against its *declared* daily limit counts as
        held: that verdict comes from our own counter, and reporting nothing to
        reset while the sensor reads exhausted would be a lie.

        Lifting that one requires ``clear_counters``, because the only way to
        make the member eligible again today is to forget how much it has
        already done - which costs the day's metrics for that member.
        """
        by_id = {config.entity_id: config for config in self.members}
        targets = [member] if member else list(by_id)
        today = self.store.today()
        reset: list[str] = []
        for entity_id in targets:
            state = self.store.state.member(entity_id)
            config = by_id.get(entity_id)
            spent = bool(
                config
                and config.daily_limit
                and state.day == today
                and state.calls >= config.daily_limit
            )
            if (
                state.disabled_reason
                or state.cooldown_until
                or state.blocked_until_day
                or state.cooldown_strikes
                or spent
            ):
                reset.append(entity_id)
            state.clear_penalties()
            if clear_counters:
                state.reset_day(today)
        await self.store.async_save()
        self._notify()
        return reset

    async def async_roll_day(self, now: Any = None) -> None:
        """Zero the day counters when the local day changes.

        Driven by a midnight trigger. The request path rolls them too, because
        there correctness depends on it, but a pool called once a morning would
        otherwise show yesterday's numbers until it was next used.
        """
        if self.store.roll_day():
            self._models.clear()
            self.store.async_schedule_save()
            self._notify()

    async def async_remove(self) -> None:
        """Drop persisted counters when the entry is deleted."""
        await self.store.async_remove()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a callback fired whenever member state changes."""
        self._listeners.append(listener)

        def _remove() -> None:
            self._listeners.remove(listener)

        return _remove

    @callback
    def _notify(self) -> None:
        """Tell listeners state changed."""
        for listener in list(self._listeners):
            listener()

    # --- Models -------------------------------------------------------------

    def member_model(self, entity_id: str) -> str | None:
        """Which provider model backs a member, as far as it can be read.

        Cached for a few minutes. Resolving it walks the entity registry and
        another integration's config entry, and every sensor read asks for
        every member - so with N members and 2N+1 sensors an uncached lookup
        fanned out as N squared. A model only changes when somebody edits that
        other integration, which the cache is allowed to notice late.
        """
        now = time.monotonic()
        if now >= self._models_expire:
            self._models.clear()
            self._models_expire = now + MODEL_CACHE_TTL
        if entity_id not in self._models:
            self._models[entity_id] = member_model(self.hass, entity_id)
        return self._models[entity_id]

    def duplicate_models(self) -> dict[str, list[str]]:
        """Models used by more than one member of this pool."""
        return shared_models(
            {
                member.entity_id: self.member_model(member.entity_id)
                for member in self.members
            }
        )

    @property
    def _model_issue_id(self) -> str:
        """Issue id for this entry's duplicate-model repair."""
        return f"{ISSUE_DUPLICATE_MODEL}_{self.entry.entry_id}"

    @callback
    def async_clear_issues(self) -> None:
        """Drop this pool's repairs.

        Called when the entry unloads or is removed. The issue id embeds the
        entry id, so an issue left behind by a deleted pool can never be
        matched again: it would sit in Repairs for good, describing a pool that
        no longer exists, with "Ignore" as the user's only recourse.
        """
        ir.async_delete_issue(self.hass, DOMAIN, self._model_issue_id)

    @callback
    def async_check_models(self) -> None:
        """Raise or clear a repair issue about members sharing a model.

        Worth a repair rather than a log line: a pool of duplicates looks
        healthy in every sensor right up to the moment one provider-side
        refusal takes all of them out together.
        """
        issue_id = self._model_issue_id
        duplicates = self.duplicate_models()
        if not duplicates:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return

        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_DUPLICATE_MODEL,
            translation_placeholders={
                "pool": self.entry.title,
                "models": ", ".join(
                    f"{model} ({len(members)} members)"
                    for model, members in sorted(duplicates.items())
                ),
            },
        )

    # --- Health -------------------------------------------------------------

    def member_status(self, member: MemberConfig) -> str:
        """Return the current health of a member.

        Read-only, and correct without anything having rolled the day first.
        Day counters are only zeroed when the pool is used, so at 00:00 they
        still hold yesterday's numbers: every check below therefore either
        compares against today's date or ignores a counter whose window has
        passed. Reporting a member exhausted because of yesterday's traffic
        would raise a false alarm on the problem sensor, which is polled and so
        asks this question when nothing else is happening.
        """
        state = self.store.state.member(member.entity_id)

        if state.disabled_reason:
            return STATUS_DISABLED

        entity_state = self.hass.states.get(member.entity_id)
        if entity_state is None or entity_state.state in UNAVAILABLE_STATES:
            return STATUS_UNAVAILABLE

        cooldown_until = parse_iso(state.cooldown_until)
        if cooldown_until and cooldown_until > dt_util.utcnow():
            return STATUS_COOLDOWN

        if state.blocked_until_day and state.blocked_until_day > self.store.today():
            return STATUS_EXHAUSTED

        if (
            member.daily_limit
            and state.day == self.store.today()
            and state.calls >= member.daily_limit
        ):
            return STATUS_EXHAUSTED

        # Throttled rather than exhausted: the allowance is intact, the pace is
        # not. Like every declared limit this only demotes - the member stays
        # in the queue as a last resort, because the number is the user's
        # estimate and a real refusal is always detected from the error.
        if member.rpm_limit and state.requests_last_minute() >= member.rpm_limit:
            return STATUS_THROTTLED

        return STATUS_HEALTHY

    def snapshot(self) -> list[MemberView]:
        """Per-member view for sensors and diagnostics.

        A pure read. It used to roll the day as its first act, which made
        looking at a sensor mutate persisted state; the roll now has its own
        call sites - the request path, where it must be exact, and a midnight
        trigger, so displays are right without anyone asking.
        """
        result: list[MemberView] = []
        for member in self.members:
            state = self.store.state.member(member.entity_id)
            remaining: int | None = None
            if member.daily_limit:
                remaining = max(member.daily_limit - state.calls, 0)
            # Against the provider's own counter, which a refusal also spends,
            # so this is the pessimistic reading of the same allowance.
            rpd_remaining: int | None = None
            if member.daily_limit:
                rpd_remaining = max(member.daily_limit - state.requests, 0)
            per_minute = state.requests_last_minute()
            rpm_remaining: int | None = None
            if member.rpm_limit:
                rpm_remaining = max(member.rpm_limit - per_minute, 0)
            result.append(
                MemberView(
                    entity_id=member.entity_id,
                    status=self.member_status(member),
                    model=self.member_model(member.entity_id),
                    calls_today=state.calls,
                    failures_today=state.failures,
                    daily_limit=member.daily_limit or None,
                    remaining=remaining,
                    requests_today=state.requests,
                    rpd_remaining=rpd_remaining,
                    requests_last_minute=per_minute,
                    rpm_limit=member.rpm_limit or None,
                    rpm_remaining=rpm_remaining,
                    input_chars_today=state.input_chars,
                    input_chars_last_minute=state.input_chars_last_minute(),
                    weight=member.weight,
                    cooldown_until=state.cooldown_until,
                    cooldown_strikes=state.cooldown_strikes,
                    last_error=state.last_error,
                    last_success=state.last_success,
                    failures_by_kind=dict(state.failures_by_kind),
                    success_rate=_rounded(state.success_rate, 1),
                    latency_last=_rounded(state.latency_last),
                    latency_average=_rounded(state.latency_average),
                    latency_min=_rounded(state.latency_min),
                    latency_max=_rounded(state.latency_max),
                    latency_recent_average=_rounded(state.latency_recent_average),
                    latency_samples=state.latency_count,
                )
            )
        return result

    def routing_snapshot(self) -> dict[str, Any]:
        """Pool-wide view of what the routing itself cost today.

        Left as a mapping, unlike the per-member view: every consumer either
        dumps it wholesale or reads one key by name, so naming the fields twice
        would buy nothing.
        """
        stats = self.store.state.stats
        statuses = [self.member_status(member) for member in self.members]
        return {
            "requests_today": stats.requests,
            "served_today": stats.served,
            "failed_today": stats.failures,
            "attempts_today": stats.attempts,
            "fallbacks_today": stats.fallbacks,
            "fallback_rate": _rounded(stats.fallback_rate, 1),
            "attempts_per_request": _rounded(stats.attempts_per_request, 2),
            "last_attempts": stats.last_attempts,
            "last_member": stats.last_member,
            "members_total": len(self.members),
            "members_healthy": statuses.count(STATUS_HEALTHY),
        }

    # --- Selection ----------------------------------------------------------

    def _candidates(self) -> tuple[list[MemberConfig], list[MemberConfig]]:
        """Split members into preferred and last-resort groups.

        Last resort means "we believe this will fail" - an exhausted counter, an
        active cooldown, an unavailable entity. They are still tried when
        nothing better exists, because a declared limit is an estimate and an
        announcement that fails loudly beats one that never runs.
        """
        preferred: list[MemberConfig] = []
        last_resort: list[MemberConfig] = []
        for member in self.members:
            status = self.member_status(member)
            if status == STATUS_HEALTHY:
                preferred.append(member)
            elif status == STATUS_DISABLED:
                continue  # Permanently broken: never worth a request.
            else:
                last_resort.append(member)
        return preferred, last_resort

    def _ordered(self, members: list[MemberConfig]) -> list[MemberConfig]:
        """Apply the configured strategy to a group of members."""
        by_key = {member.entity_id: member for member in members}
        candidates = [
            Candidate(
                key=member.entity_id,
                weight=member.weight,
                daily_limit=member.daily_limit,
                used_today=self.store.state.member(member.entity_id).calls,
            )
            for member in members
        ]
        ordered = order_candidates(candidates, self.strategy, self.store.state.cursor)
        return [by_key[candidate.key] for candidate in ordered]

    # --- Execution ----------------------------------------------------------

    async def async_execute(
        self,
        run: Callable[[str], Awaitable[T]],
        *,
        description: str = "request",
        size: int = 0,
        attempt_limit: int | None = None,
    ) -> T:
        """Run ``run`` against pool members until one succeeds.

        ``run`` receives a member ``entity_id``. Its exceptions are classified
        to decide whether the member is merely busy, out of allowance, or
        permanently unusable.

        ``size`` is how large the request is in characters. Providers meter
        input tokens, which Home Assistant never reports back, so the platforms
        pass what they do know and the metrics call it what it is.

        ``attempt_limit`` caps the members tried below the pool's configured
        maximum, for a caller that cannot honestly be retried - a recording
        clipped to fit the retry buffer being the one case.
        """
        self.store.roll_day()
        preferred, last_resort = self._candidates()
        queue = self._ordered(preferred) + self._ordered(last_resort)

        if not queue:
            raise AllMembersFailedError(
                f"Pool {self.entry.title}: no usable member for {description}"
            )

        # Advanced by one, independently of the queue length. Taking it modulo
        # the queue used to skew the rotation the moment a member sat out: with
        # three members and one in cooldown the cursor cycled 0,1,2 over a
        # two-member group, so offsets ran 0,1,0,0,1,0 and one member served
        # two calls in three.
        self.store.state.cursor = (self.store.state.cursor + 1) % CURSOR_MODULUS

        limit = self.max_attempts if attempt_limit is None else max(attempt_limit, 1)
        attempts = 0
        last_error: BaseException | None = None
        # Models that just refused for capacity. Asking a second member backed
        # by the same model is asking the same engine the same question.
        spent_models: set[str] = set()

        for member in queue:
            if attempts >= limit:
                break
            model = self.member_model(member.entity_id)
            if model and model in spent_models:
                _LOGGER.debug(
                    "Pool %s: skipping %s, model %s just refused for capacity",
                    self.entry.title,
                    member.entity_id,
                    model,
                )
                continue
            attempts += 1
            state = self.store.touch(member.entity_id)
            # Recorded before the call: the provider charges the request
            # against its limits when it receives it, not when it answers.
            state.record_request(size)
            started = time.monotonic()
            try:
                # A member that never answers would otherwise hold the whole
                # request open: the deadline turns waiting into failing over.
                async with asyncio.timeout(self.timeout or None):
                    result = await run(member.entity_id)
            except Exception as err:
                verdict = classify(err)
                last_error = err
                state.record_failure(verdict.kind.value)
                state.last_error = f"{verdict.kind.value}: {verdict.message}"[:255]
                self._apply_verdict(member, verdict)
                if verdict.kind is FailureKind.CAPACITY and model:
                    spent_models.add(model)
                _LOGGER.warning(
                    "Pool %s: member %s failed %s (%s), trying next",
                    self.entry.title,
                    member.entity_id,
                    description,
                    verdict.kind.value,
                )
                self._fire(
                    EVENT_FAILOVER,
                    member=member.entity_id,
                    model=model,
                    kind=verdict.kind.value,
                    message=verdict.message[:255],
                    description=description,
                    attempt=attempts,
                )
                continue

            state.calls += 1
            state.record_latency(time.monotonic() - started)
            state.last_success = dt_util.utcnow().isoformat()
            state.cooldown_until = None
            # A success is the only evidence that the provider has recovered,
            # so it is what resets the escalating cooldown.
            state.cooldown_strikes = 0
            self._record_request(attempts, member.entity_id, served=True)
            self.store.async_schedule_save()
            self._notify()
            return result

        self._record_request(attempts, None, served=False)
        self.store.async_schedule_save()
        self._fire(
            EVENT_EXHAUSTED,
            attempts=attempts,
            description=description,
            members=len(queue),
        )
        self._notify()
        raise AllMembersFailedError(
            f"Pool {self.entry.title}: all {attempts} attempted member(s) failed "
            f"for {description}"
        ) from last_error

    @callback
    def _fire(self, event: str, **data: Any) -> None:
        """Fire a pool event on the Home Assistant bus.

        Every event carries the pool's identity, so a single automation can
        watch one event type across every pool and branch on the payload.
        """
        self.hass.bus.async_fire(
            event,
            {
                "entry_id": self.entry.entry_id,
                "pool": self.entry.title,
                "pool_type": self.pool_type,
                **data,
            },
        )

    @callback
    def _record_request(
        self, attempts: int, member: str | None, *, served: bool
    ) -> None:
        """Record how much routing one request cost.

        A request that needed more than one member is the signal that the
        preference order is wrong - something no per-member counter shows,
        since each member only knows about its own calls.
        """
        stats = self.store.state.stats
        stats.requests += 1
        stats.attempts += attempts
        stats.last_attempts = attempts
        if attempts > 1:
            stats.fallbacks += 1
        if served:
            stats.served += 1
            stats.last_member = member
        else:
            stats.failures += 1

    @callback
    def _apply_verdict(self, member: MemberConfig, verdict: Verdict) -> None:
        """Update member health from a classified failure."""
        state = self.store.state.member(member.entity_id)

        if verdict.blocks_until_quota_reset:
            # Spent for this window; eligible again once the local day rolls.
            state.blocked_until_day = days_ahead(1)
        elif verdict.deserves_cooldown:
            state.cooldown_strikes += 1
            state.cooldown_until = (
                dt_util.utcnow() + self._backoff(state.cooldown_strikes)
            ).isoformat()
        elif verdict.kind is FailureKind.AUTH:
            state.disabled_reason = "authentication"

    def _backoff(self, strikes: int) -> timedelta:
        """How long a member sits out after ``strikes`` refusals in a row.

        Capacity refusals arrive in clusters, so a fixed cooldown sends the
        pool back to a saturated provider every few minutes to fail again.
        Each consecutive refusal doubles the wait, up to a ceiling that keeps
        the member in hourly rotation rather than dropping it for good.
        """
        seconds = self.cooldown.total_seconds() * 2 ** max(strikes - 1, 0)
        return timedelta(seconds=min(seconds, MAX_COOLDOWN))
