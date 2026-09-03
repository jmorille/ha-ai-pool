"""Failure classification.

The messages below are verbatim from real providers; the classifier exists
because Home Assistant flattens provider errors into a single exception type,
so the status is only recoverable from the text.
"""

import pytest

from custom_components.ai_pool.errors import FailureKind, classify

GOOGLE_503 = (
    "Sorry, I had a problem getting a response from Google Generative AI.: {\n"
    '  "error": {\n'
    '    "code": 503,\n'
    '    "message": "This model is currently experiencing high demand. Spikes in '
    'demand are usually temporary. Please try again later.",\n'
    '    "status": "UNAVAILABLE"\n'
    "  }\n"
    "}\n"
)
OPENROUTER_GENERIC = "Error talking to API"
GOOGLE_429 = (
    '{"error": {"code": 429, "message": "You exceeded your current quota", '
    '"status": "RESOURCE_EXHAUSTED"}}'
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (GOOGLE_503, FailureKind.CAPACITY),
        (OPENROUTER_GENERIC, FailureKind.TRANSIENT),
        (GOOGLE_429, FailureKind.QUOTA),
        ("API key not valid. Please pass a valid API key.", FailureKind.AUTH),
        ("403 Forbidden", FailureKind.AUTH),
        (
            "400 INVALID_ARGUMENT: response_schema is not supported",
            FailureKind.UNSUPPORTED,
        ),
        (
            "AI Task entity ai_task.x does not support generating data",
            FailureKind.UNSUPPORTED,
        ),
        ("500 Internal Server Error", FailureKind.TRANSIENT),
        ("504 Gateway Timeout", FailureKind.TRANSIENT),
        ("Cannot connect to host", FailureKind.TRANSIENT),
        ("something nobody has ever seen", FailureKind.UNKNOWN),
        ("", FailureKind.UNKNOWN),
    ],
)
def test_classify(message: str, expected: FailureKind) -> None:
    assert classify(message).kind is expected


def test_capacity_is_not_mistaken_for_quota() -> None:
    """The Google 503 says UNAVAILABLE, not exhausted.

    Getting this backwards would sit a perfectly healthy member out until
    midnight over a transient spike, which is the whole point of separating
    the two kinds.
    """
    verdict = classify(GOOGLE_503)
    assert verdict.deserves_cooldown is True
    assert verdict.blocks_until_quota_reset is False
    assert verdict.is_permanent is False


def test_quota_blocks_for_the_window() -> None:
    verdict = classify(GOOGLE_429)
    assert verdict.blocks_until_quota_reset is True
    assert verdict.deserves_cooldown is False


def test_auth_is_permanent() -> None:
    verdict = classify("401 Unauthorized")
    assert verdict.is_permanent is True


def test_timeout_exception_has_its_own_kind() -> None:
    """A deadline we imposed is a different diagnosis from a provider error.

    Both are retryable and neither earns a cooldown, but "too slow for us" and
    "the server broke" call for different fixes, so they are counted apart.
    """
    verdict = classify(TimeoutError())
    assert verdict.kind is FailureKind.TIMEOUT
    assert verdict.deserves_cooldown is False
    assert verdict.is_permanent is False


def test_exception_instances_are_accepted() -> None:
    assert classify(RuntimeError(GOOGLE_429)).kind is FailureKind.QUOTA


def test_every_verdict_allows_trying_the_next_member() -> None:
    """No failure should ever abort the whole request.

    Even an auth failure on one member says nothing about the others.
    """
    for message in (GOOGLE_503, GOOGLE_429, "401 Unauthorized", "boom"):
        assert classify(message).should_try_next_member is True
