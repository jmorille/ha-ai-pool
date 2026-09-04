"""The pool's read model.

What a sensor, a diagnostics dump or a test asks the pool for is one shape, and
it used to be an untyped dictionary of two dozen string keys. That made the
integration's real internal contract invisible: renaming a key broke nothing a
compiler could see, and a typo in a consumer read as a missing value rather than
an error. A frozen dataclass says what a member view is, once.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class MemberView:
    """Everything the pool can say about one member, at one moment."""

    entity_id: str
    status: str
    # Which provider model backs this member, as far as it can be read.
    model: str | None

    # --- Today's work -------------------------------------------------------
    calls_today: int
    failures_today: int
    success_rate: float | None

    # --- Allowances ---------------------------------------------------------
    # `remaining` counts against successes, `rpd_remaining` against every
    # attempt: the provider's own counter sits between the two.
    daily_limit: int | None
    remaining: int | None
    requests_today: int
    rpd_remaining: int | None
    requests_last_minute: int
    rpm_limit: int | None
    rpm_remaining: int | None
    input_chars_today: int
    input_chars_last_minute: int

    # --- Health -------------------------------------------------------------
    weight: int
    cooldown_until: str | None
    cooldown_strikes: int
    last_error: str | None
    last_success: str | None
    failures_by_kind: dict[str, int] = field(default_factory=dict)

    # --- Latency ------------------------------------------------------------
    latency_last: float | None = None
    latency_average: float | None = None
    latency_min: float | None = None
    latency_max: float | None = None
    latency_recent_average: float | None = None
    latency_samples: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Flatten for the diagnostics dump, which is JSON either way."""
        return asdict(self)
