"""Pool runtime: member selection, failover and bookkeeping.

The pool is deliberately provider-agnostic. Platforms supply a callable that
performs the real work against one member entity; everything about *which*
member and *what to do when it fails* lives here.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, TypeVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import (
    CONF_COOLDOWN,
    CONF_DAILY_LIMIT,
    CONF_MAX_ATTEMPTS,
    CONF_MEMBERS,
    CONF_POOL_TYPE,
    CONF_STRATEGY,
    CONF_WEIGHT,
    DEFAULT_COOLDOWN,
    DEFAULT_DAILY_LIMIT,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_STRATEGY,
    DEFAULT_WEIGHT,
    STATUS_COOLDOWN,
    STATUS_DISABLED,
    STATUS_EXHAUSTED,
    STATUS_HEALTHY,
    STATUS_UNAVAILABLE,
)
from .errors import FailureKind, Verdict, classify
from .store import UsageStore, days_ahead, parse_iso
from .strategies import Candidate, order_candidates

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
    weight: int = DEFAULT_WEIGHT

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemberConfig:
        """Build from stored config-entry data."""
        return cls(
            entity_id=data["entity_id"],
            daily_limit=int(data.get(CONF_DAILY_LIMIT, DEFAULT_DAILY_LIMIT) or 0),
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
    def members(self) -> list[MemberConfig]:
        """Configured members, in declared order."""
        return [
            MemberConfig.from_dict(item) for item in self._config.get(CONF_MEMBERS, [])
        ]

    # --- Lifecycle ----------------------------------------------------------

    async def async_setup(self) -> None:
        """Load persisted counters."""
        await self.store.async_load()

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

    # --- Health -------------------------------------------------------------

    def member_status(self, member: MemberConfig) -> str:
        """Return the current health of a member."""
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

        if member.daily_limit and state.calls >= member.daily_limit:
            return STATUS_EXHAUSTED

        return STATUS_HEALTHY

    def snapshot(self) -> list[dict[str, Any]]:
        """Per-member view for sensors and diagnostics."""
        self.store.roll_day()
        result: list[dict[str, Any]] = []
        for member in self.members:
            state = self.store.state.member(member.entity_id)
            remaining: int | None = None
            if member.daily_limit:
                remaining = max(member.daily_limit - state.calls, 0)
            result.append(
                {
                    "entity_id": member.entity_id,
                    "status": self.member_status(member),
                    "calls_today": state.calls,
                    "failures_today": state.failures,
                    "daily_limit": member.daily_limit or None,
                    "remaining": remaining,
                    "weight": member.weight,
                    "cooldown_until": state.cooldown_until,
                    "last_error": state.last_error,
                    "last_success": state.last_success,
                    "failures_by_kind": dict(state.failures_by_kind),
                    "success_rate": _rounded(state.success_rate, 1),
                    "latency_last": _rounded(state.latency_last),
                    "latency_average": _rounded(state.latency_average),
                    "latency_min": _rounded(state.latency_min),
                    "latency_max": _rounded(state.latency_max),
                    "latency_recent_average": _rounded(state.latency_recent_average),
                    "latency_samples": state.latency_count,
                }
            )
        return result

    def routing_snapshot(self) -> dict[str, Any]:
        """Pool-wide view of what the routing itself cost today."""
        self.store.roll_day()
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
    ) -> T:
        """Run ``run`` against pool members until one succeeds.

        ``run`` receives a member ``entity_id``. Its exceptions are classified
        to decide whether the member is merely busy, out of allowance, or
        permanently unusable.
        """
        self.store.roll_day()
        preferred, last_resort = self._candidates()
        queue = self._ordered(preferred) + self._ordered(last_resort)

        if not queue:
            raise AllMembersFailedError(
                f"Pool {self.entry.title}: no usable member for {description}"
            )

        self.store.state.cursor = (self.store.state.cursor + 1) % max(len(queue), 1)

        attempts = 0
        last_error: BaseException | None = None

        for member in queue:
            if attempts >= self.max_attempts:
                break
            attempts += 1
            state = self.store.touch(member.entity_id)
            started = time.monotonic()
            try:
                result = await run(member.entity_id)
            except Exception as err:
                verdict = classify(err)
                last_error = err
                state.record_failure(verdict.kind.value)
                state.last_error = f"{verdict.kind.value}: {verdict.message}"[:255]
                self._apply_verdict(member, verdict)
                _LOGGER.warning(
                    "Pool %s: member %s failed %s (%s), trying next",
                    self.entry.title,
                    member.entity_id,
                    description,
                    verdict.kind.value,
                )
                continue

            state.calls += 1
            state.record_latency(time.monotonic() - started)
            state.last_success = dt_util.utcnow().isoformat()
            state.cooldown_until = None
            self._record_request(attempts, member.entity_id, served=True)
            await self.store.async_save()
            self._notify()
            return result

        self._record_request(attempts, None, served=False)
        await self.store.async_save()
        self._notify()
        raise AllMembersFailedError(
            f"Pool {self.entry.title}: all {attempts} attempted member(s) failed "
            f"for {description}"
        ) from last_error

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
            state.cooldown_until = (dt_util.utcnow() + self.cooldown).isoformat()
        elif verdict.kind is FailureKind.AUTH:
            state.disabled_reason = "authentication"
