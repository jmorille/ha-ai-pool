"""Failure classification for pool members.

Home Assistant integrations flatten provider errors into ``HomeAssistantError``
with a human-readable message, so the provider's HTTP status is only available
as text. Classification therefore matches on the message, which is why the
patterns below are deliberately broad and ordered from most to least specific.

Observed in the wild:
    Google Generative AI : '503 ... "message": "This model is currently
                            experiencing high demand" ... "UNAVAILABLE"'
    OpenRouter           : 'Error talking to API'
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class FailureKind(StrEnum):
    """Why a member call failed, and therefore what to do about it."""

    QUOTA = "quota"
    """Allowance is spent. Sit the member out until the quota window rolls."""

    CAPACITY = "capacity"
    """Provider is busy but the allowance is intact. Short cooldown, retry later."""

    TRANSIENT = "transient"
    """Server error or timeout. Immediately retryable, no cooldown."""

    AUTH = "auth"
    """Credentials are wrong or revoked. Retrying cannot help."""

    UNSUPPORTED = "unsupported"
    """The member cannot serve this request shape (e.g. structured output)."""

    UNKNOWN = "unknown"
    """Unrecognised. Treated as transient but surfaced for diagnosis."""


@dataclass(frozen=True, slots=True)
class Verdict:
    """Classification result for a single failed attempt."""

    kind: FailureKind
    message: str

    @property
    def should_try_next_member(self) -> bool:
        """Whether another member should be attempted for this request."""
        return True

    @property
    def blocks_until_quota_reset(self) -> bool:
        """Whether the member is spent for the rest of the quota window."""
        return self.kind is FailureKind.QUOTA

    @property
    def deserves_cooldown(self) -> bool:
        """Whether the member should sit out for the configured cooldown."""
        return self.kind is FailureKind.CAPACITY

    @property
    def is_permanent(self) -> bool:
        """Whether retrying this member can never succeed as configured."""
        return self.kind in (FailureKind.AUTH, FailureKind.UNSUPPORTED)


# Ordered most-specific first: the first pattern that matches wins.
_PATTERNS: tuple[tuple[FailureKind, re.Pattern[str]], ...] = (
    (
        FailureKind.AUTH,
        re.compile(
            r"\b401\b|\b403\b|unauthori[sz]ed|permission[ _]denied|forbidden"
            r"|invalid[ _]api[ _]key|api[ _]key[ _]not[ _]valid|expired[ _]token"
            r"|authentication",
            re.IGNORECASE,
        ),
    ),
    (
        FailureKind.QUOTA,
        re.compile(
            r"\b429\b|resource[ _]exhausted|insufficient[ _]quota|quota"
            r"|rate[ _-]?limit|too[ _]many[ _]requests|billing",
            re.IGNORECASE,
        ),
    ),
    (
        FailureKind.UNSUPPORTED,
        re.compile(
            r"not[ _]supported|unsupported|does[ _]not[ _]support"
            r"|invalid[ _]argument|\b400\b|response[ _]schema|response[ _]format",
            re.IGNORECASE,
        ),
    ),
    (
        FailureKind.CAPACITY,
        re.compile(
            r"\b503\b|unavailable|high[ _]demand|overload|capacity"
            r"|model[ _]is[ _]busy|try[ _]again[ _]later",
            re.IGNORECASE,
        ),
    ),
    (
        FailureKind.TRANSIENT,
        re.compile(
            r"\b500\b|\b502\b|\b504\b|timed?[ _]?out|timeout|internal[ _]error"
            r"|connect|network|talking[ _]to[ _]api|temporar|socket|reset[ _]by",
            re.IGNORECASE,
        ),
    ),
)


def classify(error: BaseException | str) -> Verdict:
    """Classify a member failure.

    Accepts an exception or a bare message so the classifier stays trivially
    testable without constructing Home Assistant error types.
    """
    if isinstance(error, TimeoutError):
        return Verdict(FailureKind.TRANSIENT, "timeout")

    message = str(error).strip()
    if not message:
        message = type(error).__name__ if isinstance(error, BaseException) else ""

    for kind, pattern in _PATTERNS:
        if pattern.search(message):
            return Verdict(kind, message)

    return Verdict(FailureKind.UNKNOWN, message)
