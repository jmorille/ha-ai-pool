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
CONF_STT_BUFFER_LIMIT: Final = "stt_buffer_limit"

# --- Defaults ---------------------------------------------------------------
DEFAULT_DAILY_LIMIT: Final = 0  # 0 means "no declared limit"
DEFAULT_RPM_LIMIT: Final = 0  # 0 means "no declared limit"
DEFAULT_WEIGHT: Final = 1
DEFAULT_COOLDOWN: Final = 300  # seconds a member sits out after a capacity error
DEFAULT_MAX_ATTEMPTS: Final = 3
# Audio is buffered so a failed member can be retried with the same recording.
DEFAULT_STT_BUFFER_LIMIT: Final = 8 * 1024 * 1024

# --- Member health ----------------------------------------------------------
STATUS_HEALTHY: Final = "healthy"
STATUS_COOLDOWN: Final = "cooldown"
STATUS_EXHAUSTED: Final = "exhausted"
STATUS_DISABLED: Final = "disabled"
STATUS_UNAVAILABLE: Final = "unavailable"

STORAGE_VERSION: Final = 1
STORAGE_KEY_TEMPLATE: Final = DOMAIN + ".{entry_id}"
