"""Persisted per-member usage, health and metrics.

Counters survive restarts because a quota window does not: Home Assistant
restarting at 18:00 must not hand a spent member a fresh allowance.

Three kinds of number live here and they behave differently on purpose. Day
counters (calls, failures, requests, latency aggregates) reset when the local
day rolls, because that is the window a daily quota is spent in. The
recent-latency ring does not: how fast a provider is answering right now is not
a question about today. The request log is a rolling window of its own, pruned
by age rather than by day, because a per-minute limit knows nothing about
midnight.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STORAGE_VERSION

RECENT_LATENCY_SAMPLES = 20

# Providers publish per-minute limits, so a minute is the window that has to be
# measurable. Anything older is pruned: the log answers "how hard am I pushing
# this member right now", never "what happened this morning".
RATE_WINDOW_SECONDS = 60.0
# A hard ceiling on the log, so a runaway caller cannot grow the stored state
# without bound. Well above any free-tier per-minute allowance.
MAX_REQUEST_LOG = 200


@dataclass
class MemberState:
    """Mutable runtime state for one pool member."""

    calls: int = 0
    failures: int = 0
    day: str = ""
    cooldown_until: str | None = None
    blocked_until_day: str | None = None
    disabled_reason: str | None = None
    last_error: str | None = None
    last_success: str | None = None

    # --- Metrics ------------------------------------------------------------
    failures_by_kind: dict[str, int] = field(default_factory=dict)
    latency_last: float | None = None
    latency_count: int = 0
    latency_total: float = 0.0
    latency_min: float | None = None
    latency_max: float | None = None
    latency_recent: list[float] = field(default_factory=list)

    # --- Rate tracking ------------------------------------------------------
    # Requests, not calls: a provider counts a request against the limit
    # whether it answers or refuses, so these move on every attempt while
    # ``calls`` only moves on success.
    requests: int = 0
    input_chars: int = 0
    # [[unix timestamp, request size in characters], ...] for the last minute.
    request_log: list[list[float]] = field(default_factory=list)

    def record_request(self, size: int = 0, now: float | None = None) -> None:
        """Log one attempt against this member, with its input size.

        Called before the request is made, because a limit is spent by asking,
        not by being answered.
        """
        moment = time.time() if now is None else now
        self.requests += 1
        self.input_chars += size
        log = self._window(moment)
        log.append([moment, float(size)])
        self.request_log = log[-MAX_REQUEST_LOG:]

    def _window(self, now: float | None = None) -> list[list[float]]:
        """Entries from the last minute, without mutating the log."""
        moment = time.time() if now is None else now
        return [
            entry
            for entry in self.request_log
            if moment - entry[0] < RATE_WINDOW_SECONDS
        ]

    def requests_last_minute(self, now: float | None = None) -> int:
        """Observed requests per minute: the RPM dimension, as we see it."""
        return len(self._window(now))

    def input_chars_last_minute(self, now: float | None = None) -> int:
        """Input characters sent in the last minute.

        A stand-in for the TPM dimension. Home Assistant hands the integration
        no token count, so characters are what can honestly be measured; the
        usual rule of thumb is roughly four characters per token.
        """
        return int(sum(entry[1] for entry in self._window(now)))

    def record_latency(self, seconds: float) -> None:
        """Record how long a *successful* call took.

        Only successes are timed. A failure's duration measures how long the
        provider took to refuse, which would drag the average away from the
        number the question actually asks: how long a usable answer takes.
        """
        self.latency_last = seconds
        self.latency_count += 1
        self.latency_total += seconds
        if self.latency_min is None or seconds < self.latency_min:
            self.latency_min = seconds
        if self.latency_max is None or seconds > self.latency_max:
            self.latency_max = seconds
        self.latency_recent = [*self.latency_recent, seconds][-RECENT_LATENCY_SAMPLES:]

    def record_failure(self, kind: str) -> None:
        """Count a failure against its classified kind."""
        self.failures += 1
        self.failures_by_kind = {
            **self.failures_by_kind,
            kind: self.failures_by_kind.get(kind, 0) + 1,
        }

    @property
    def latency_average(self) -> float | None:
        """Mean duration of today's successful calls."""
        if not self.latency_count:
            return None
        return self.latency_total / self.latency_count

    @property
    def latency_recent_average(self) -> float | None:
        """Mean duration of the last few successful calls, whatever the day."""
        if not self.latency_recent:
            return None
        return sum(self.latency_recent) / len(self.latency_recent)

    @property
    def success_rate(self) -> float | None:
        """Share of today's attempts that succeeded, as a percentage."""
        attempts = self.calls + self.failures
        if not attempts:
            return None
        return 100.0 * self.calls / attempts

    def reset_day(self, day: str) -> None:
        """Zero the day-scoped counters and stamp the new window.

        The request log survives: it is a one-minute window, and a request made
        at 23:59:50 still counts against the per-minute limit at 00:00:05.
        """
        self.day = day
        self.calls = 0
        self.failures = 0
        self.requests = 0
        self.input_chars = 0
        self.failures_by_kind = {}
        self.latency_count = 0
        self.latency_total = 0.0
        self.latency_min = None
        self.latency_max = None

    def as_dict(self) -> dict:
        """Serialise for the storage helper."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> MemberState:
        """Rehydrate, ignoring keys written by other versions."""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class PoolStats:
    """Day-scoped routing statistics for the pool as a whole.

    These answer the question no per-member counter can: is the configured
    preference order any good? A request that needed a second member is a
    request the first choice should have served.
    """

    day: str = ""
    requests: int = 0
    served: int = 0
    attempts: int = 0
    fallbacks: int = 0
    failures: int = 0
    last_attempts: int = 0
    last_member: str | None = None

    @property
    def fallback_rate(self) -> float | None:
        """Share of requests that needed more than one member, as a percentage."""
        if not self.requests:
            return None
        return 100.0 * self.fallbacks / self.requests

    @property
    def attempts_per_request(self) -> float | None:
        """Mean number of members tried per request."""
        if not self.requests:
            return None
        return self.attempts / self.requests

    def reset_day(self, day: str) -> None:
        """Zero the day-scoped counters and stamp the new window."""
        self.day = day
        self.requests = 0
        self.served = 0
        self.attempts = 0
        self.fallbacks = 0
        self.failures = 0

    def as_dict(self) -> dict:
        """Serialise for the storage helper."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> PoolStats:
        """Rehydrate, ignoring keys written by other versions."""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class PoolState:
    """Everything the pool persists between calls and across restarts."""

    cursor: int = 0
    members: dict[str, MemberState] = field(default_factory=dict)
    stats: PoolStats = field(default_factory=PoolStats)

    def member(self, key: str) -> MemberState:
        """Return the state for a member, creating it on first use."""
        if key not in self.members:
            self.members[key] = MemberState()
        return self.members[key]


class UsageStore:
    """Thin wrapper over Home Assistant's Store with day-rollover semantics."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialise the store for one config entry."""
        self._hass = hass
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
        self.state = PoolState()

    async def async_load(self) -> PoolState:
        """Load persisted state and roll counters if the local day changed."""
        raw = await self._store.async_load()
        if raw:
            self.state = PoolState(
                cursor=raw.get("cursor", 0),
                members={
                    key: MemberState.from_dict(value)
                    for key, value in (raw.get("members") or {}).items()
                },
                stats=PoolStats.from_dict(raw.get("stats") or {}),
            )
        self.roll_day()
        return self.state

    async def async_save(self) -> None:
        """Persist current state."""
        await self._store.async_save(
            {
                "cursor": self.state.cursor,
                "members": {
                    key: value.as_dict() for key, value in self.state.members.items()
                },
                "stats": self.state.stats.as_dict(),
            }
        )

    async def async_remove(self) -> None:
        """Delete persisted state, used when the config entry is removed."""
        await self._store.async_remove()

    # --- Day handling -------------------------------------------------------

    @staticmethod
    def today(now: datetime | None = None) -> str:
        """Local calendar day used as the quota window key.

        Local rather than UTC: a provider's daily reset is not the point, the
        household's day is. It only has to be consistent.
        """
        moment = now or dt_util.now()
        return dt_util.as_local(moment).date().isoformat()

    def touch(self, key: str, now: datetime | None = None) -> MemberState:
        """Return a member's state, stamped with the current quota window.

        Stamping when the member is picked, rather than when it succeeds,
        matters: an unstamped state looks like it belongs to an earlier day,
        so the next day roll would erase the failure just recorded against it.
        """
        state = self.state.member(key)
        state.day = self.today(now)
        return state

    def roll_day(self, now: datetime | None = None) -> bool:
        """Zero counters whose stored day is not today. Returns True if rolled."""
        current = self.today(now)
        rolled = False
        for state in self.state.members.values():
            if state.day != current:
                state.reset_day(current)
                rolled = True
            if state.blocked_until_day and state.blocked_until_day <= current:
                # The window the member was spent in has passed.
                state.blocked_until_day = None
                rolled = True
        if self.state.stats.day != current:
            self.state.stats.reset_day(current)
            rolled = True
        return rolled

    def is_new_day(self, stored_day: str, now: datetime | None = None) -> bool:
        """Whether a stored day marker predates the current local day."""
        return stored_day != self.today(now)


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO timestamp written by this integration, tolerating junk."""
    if not value:
        return None
    return dt_util.parse_datetime(value)


def days_ahead(offset: int, now: datetime | None = None) -> str:
    """Return the local day ``offset`` days from now, as an ISO date string."""
    moment = dt_util.as_local(now or dt_util.now()).date()
    return date.fromordinal(moment.toordinal() + offset).isoformat()
