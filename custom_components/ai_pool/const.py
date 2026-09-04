"""Constants for the AI Pool integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "ai_pool"

# --- Pool types -------------------------------------------------------------
# Each pool publishes exactly one entity in the matching Home Assistant domain.
POOL_TYPE_AI_TASK: Final = "ai_task"
POOL_TYPE_CONVERSATION: Final = "conversation"
POOL_TYPE_TTS: Final = "tts"
POOL_TYPE_STT: Final = "stt"

POOL_TYPES: Final = (
    POOL_TYPE_AI_TASK,
    POOL_TYPE_CONVERSATION,
    POOL_TYPE_TTS,
    POOL_TYPE_STT,
)

# --- Selection strategies ---------------------------------------------------
# round_robin  : rotate on every call (what "alternate by call" means)
# least_used   : pick the member with the most daily quota headroom left
# priority     : always try members in configured order (pure failover)
STRATEGY_ROUND_ROBIN: Final = "round_robin"
STRATEGY_LEAST_USED: Final = "least_used"
STRATEGY_PRIORITY: Final = "priority"

STRATEGIES: Final = (STRATEGY_ROUND_ROBIN, STRATEGY_LEAST_USED, STRATEGY_PRIORITY)
DEFAULT_STRATEGY: Final = STRATEGY_ROUND_ROBIN

# --- Configuration keys -----------------------------------------------------
CONF_POOL_TYPE: Final = "pool_type"
CONF_MEMBERS: Final = "members"
CONF_STRATEGY: Final = "strategy"
CONF_DAILY_LIMIT: Final = "daily_limit"
CONF_RPM_LIMIT: Final = "rpm_limit"
CONF_WEIGHT: Final = "weight"
CONF_COOLDOWN: Final = "cooldown_seconds"
CONF_MAX_ATTEMPTS: Final = "max_attempts"
CONF_TIMEOUT: Final = "timeout_seconds"
CONF_STT_BUFFER_LIMIT: Final = "stt_buffer_limit"

# --- Defaults ---------------------------------------------------------------
DEFAULT_DAILY_LIMIT: Final = 0  # 0 means "no declared limit"
DEFAULT_RPM_LIMIT: Final = 0  # 0 means "no declared limit"
DEFAULT_WEIGHT: Final = 1
DEFAULT_COOLDOWN: Final = 300  # seconds a member sits out after a capacity error
DEFAULT_MAX_ATTEMPTS: Final = 3
# A member that has not answered in this long is abandoned and the next one is
# tried. Generous on purpose - a slow answer is still an answer, and observed
# durations reach a minute - but an unbounded wait is not a policy.
DEFAULT_TIMEOUT: Final = 120
# Ceiling for the doubling cooldown, so a provider having a bad day is retried
# hourly rather than never.
MAX_COOLDOWN: Final = 3600
# The round-robin cursor advances independently of how many members are usable
# right now, so rotation stays even while some sit out. It wraps on the lowest
# common multiple of 1..10, which divides every group size a sane pool can
# have, so wrapping never lands two calls on the same member.
CURSOR_MODULUS: Final = 2520
# Audio is buffered so a failed member can be retried with the same recording.
DEFAULT_STT_BUFFER_LIMIT: Final = 8 * 1024 * 1024

# --- Member health ----------------------------------------------------------
STATUS_HEALTHY: Final = "healthy"
STATUS_COOLDOWN: Final = "cooldown"
# Pace, not allowance: the member is inside its daily quota but at its
# per-minute ceiling, so it is demoted rather than spent.
STATUS_THROTTLED: Final = "throttled"
STATUS_EXHAUSTED: Final = "exhausted"
STATUS_DISABLED: Final = "disabled"
STATUS_UNAVAILABLE: Final = "unavailable"

# --- Events -----------------------------------------------------------------
# Fired so automations can react to a degrading pool without polling sensors.
EVENT_FAILOVER: Final = f"{DOMAIN}_failover"
EVENT_EXHAUSTED: Final = f"{DOMAIN}_exhausted"

# --- Services ---------------------------------------------------------------
SERVICE_RESET_MEMBER: Final = "reset_member"
ATTR_POOL: Final = "pool"
ATTR_MEMBER: Final = "member"
ATTR_CLEAR_COUNTERS: Final = "clear_counters"

# --- Repairs ----------------------------------------------------------------
ISSUE_DUPLICATE_MODEL: Final = "duplicate_model"

STORAGE_VERSION: Final = 1
STORAGE_KEY_TEMPLATE: Final = DOMAIN + ".{entry_id}"
