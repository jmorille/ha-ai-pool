"""Member ordering strategies.

Kept as pure functions over plain data so the routing policy can be tested
without a Home Assistant instance.
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import STRATEGY_LEAST_USED, STRATEGY_PRIORITY, STRATEGY_ROUND_ROBIN


@dataclass(frozen=True)
class Candidate:
    """A selectable member and everything the policy needs to rank it."""

    key: str
    weight: int = 1
    daily_limit: int = 0
    used_today: int = 0

    @property
    def has_declared_limit(self) -> bool:
        """Whether a daily allowance was declared for this member."""
        return self.daily_limit > 0

    @property
    def remaining(self) -> int | None:
        """Calls left today, or None when no limit was declared."""
        if not self.has_declared_limit:
            return None
        return max(self.daily_limit - self.used_today, 0)

    @property
    def is_spent(self) -> bool:
        """Whether the declared allowance is used up."""
        return self.remaining == 0

    @property
    def headroom(self) -> float:
        """Weighted share of allowance left; higher sorts first.

        Members without a declared limit are ranked by inverse usage so they
        still rotate rather than absorbing every call.
        """
        weight = max(self.weight, 1)
        if not self.has_declared_limit:
            return 1.0 / (1.0 + self.used_today / weight)
        return (self.daily_limit - self.used_today) / (self.daily_limit * weight)


def order_candidates(
    candidates: list[Candidate],
    strategy: str,
    cursor: int = 0,
) -> list[Candidate]:
    """Return candidates in the order they should be attempted.

    Every candidate is always returned: ordering decides who goes *first*,
    while the ones behind remain available as fallbacks. Members whose declared
    allowance is spent are pushed to the back rather than dropped, so a pool
    whose counters are all exhausted still tries rather than failing outright —
    declared limits are an estimate, never ground truth.
    """
    if not candidates:
        return []

    spent = [c for c in candidates if c.is_spent]
    live = [c for c in candidates if not c.is_spent]

    if strategy == STRATEGY_PRIORITY:
        ordered = live
    elif strategy == STRATEGY_LEAST_USED:
        ordered = sorted(
            live,
            key=lambda c: (-c.headroom, candidates.index(c)),
        )
    elif strategy == STRATEGY_ROUND_ROBIN:
        if live:
            offset = cursor % len(live)
            ordered = live[offset:] + live[:offset]
        else:
            ordered = live
    else:  # Unknown strategy: behave like plain failover.
        ordered = live

    return [*ordered, *spent]
