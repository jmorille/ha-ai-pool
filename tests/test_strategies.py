"""Member ordering policy."""

from custom_components.ai_pool.const import (
    STRATEGY_LEAST_USED,
    STRATEGY_PRIORITY,
    STRATEGY_ROUND_ROBIN,
)
from custom_components.ai_pool.strategies import Candidate, order_candidates


def keys(candidates: list[Candidate]) -> list[str]:
    return [candidate.key for candidate in candidates]


def test_priority_keeps_declared_order() -> None:
    pool = [Candidate("a"), Candidate("b"), Candidate("c")]
    assert keys(order_candidates(pool, STRATEGY_PRIORITY)) == ["a", "b", "c"]


def test_round_robin_rotates_with_the_cursor() -> None:
    pool = [Candidate("a"), Candidate("b"), Candidate("c")]
    assert keys(order_candidates(pool, STRATEGY_ROUND_ROBIN, 0))[0] == "a"
    assert keys(order_candidates(pool, STRATEGY_ROUND_ROBIN, 1))[0] == "b"
    assert keys(order_candidates(pool, STRATEGY_ROUND_ROBIN, 2))[0] == "c"
    assert keys(order_candidates(pool, STRATEGY_ROUND_ROBIN, 3))[0] == "a"


def test_round_robin_still_returns_everyone() -> None:
    """Rotation picks who goes first, it does not drop the rest."""
    pool = [Candidate("a"), Candidate("b"), Candidate("c")]
    assert sorted(keys(order_candidates(pool, STRATEGY_ROUND_ROBIN, 1))) == [
        "a",
        "b",
        "c",
    ]


def test_least_used_prefers_the_most_headroom() -> None:
    pool = [
        Candidate("spent_a_lot", daily_limit=100, used_today=90),
        Candidate("barely_used", daily_limit=100, used_today=5),
    ]
    assert keys(order_candidates(pool, STRATEGY_LEAST_USED))[0] == "barely_used"


def test_least_used_compares_shares_not_absolute_counts() -> None:
    """A big allowance half spent beats a small one nearly spent."""
    pool = [
        Candidate("small", daily_limit=10, used_today=9),
        Candidate("large", daily_limit=1000, used_today=500),
    ]
    assert keys(order_candidates(pool, STRATEGY_LEAST_USED))[0] == "large"


def test_least_used_rotates_unlimited_members() -> None:
    """Members without a declared limit must still take turns.

    Otherwise the first unlimited member would absorb every call and the
    rotation that multiplies capacity would never happen.
    """
    pool = [Candidate("a", used_today=7), Candidate("b", used_today=1)]
    assert keys(order_candidates(pool, STRATEGY_LEAST_USED))[0] == "b"


def test_weight_biases_selection() -> None:
    """A heavier member is treated as having more room to give."""
    light = Candidate("light", weight=1, used_today=3)
    heavy = Candidate("heavy", weight=10, used_today=3)
    assert heavy.headroom > light.headroom


def test_spent_members_go_last_but_are_not_dropped() -> None:
    """A declared limit is an estimate, never ground truth.

    Dropping an apparently spent member would turn a wrong guess about a quota
    into a silent no-announcement; keeping it last means the worst case is a
    real error from the provider.
    """
    pool = [
        Candidate("spent", daily_limit=5, used_today=5),
        Candidate("fresh", daily_limit=5, used_today=0),
    ]
    ordered = keys(order_candidates(pool, STRATEGY_PRIORITY))
    assert ordered == ["fresh", "spent"]


def test_all_spent_still_returns_candidates() -> None:
    pool = [
        Candidate("a", daily_limit=1, used_today=1),
        Candidate("b", daily_limit=1, used_today=1),
    ]
    assert len(order_candidates(pool, STRATEGY_ROUND_ROBIN)) == 2


def test_empty_pool() -> None:
    assert order_candidates([], STRATEGY_ROUND_ROBIN) == []


def test_unknown_strategy_degrades_to_priority() -> None:
    pool = [Candidate("a"), Candidate("b")]
    assert keys(order_candidates(pool, "nonsense")) == ["a", "b"]


def test_remaining_and_is_spent() -> None:
    assert Candidate("x").remaining is None
    assert Candidate("x").is_spent is False
    assert Candidate("x", daily_limit=3, used_today=1).remaining == 2
    assert Candidate("x", daily_limit=3, used_today=9).remaining == 0
    assert Candidate("x", daily_limit=3, used_today=9).is_spent is True
