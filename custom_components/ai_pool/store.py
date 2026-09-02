"""Persisted per-member usage and health.

Counters survive restarts because a quota window does not: Home Assistant
restarting at 18:00 must not hand a spent member a fresh allowance.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STORAGE_VERSION


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

    def as_dict(self) -> dict:
        """Serialise for the storage helper."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> MemberState:
        """Rehydrate, ignoring keys written by other versions."""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class PoolState:
    """Everything the pool persists between calls and across restarts."""

    cursor: int = 0
    members: dict[str, MemberState] = field(default_factory=dict)

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

    def roll_day(self, now: datetime | None = None) -> bool:
        """Zero counters whose stored day is not today. Returns True if rolled."""
        current = self.today(now)
        rolled = False
        for state in self.state.members.values():
            if state.day != current:
                state.day = current
                state.calls = 0
                state.failures = 0
                rolled = True
            if state.blocked_until_day and state.blocked_until_day <= current:
                # The window the member was spent in has passed.
                state.blocked_until_day = None
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
