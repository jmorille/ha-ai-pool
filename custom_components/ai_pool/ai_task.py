"""AI Task pool entity."""

from __future__ import annotations

from homeassistant.components import ai_task, conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import AIPoolEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the pooled AI Task entity."""
    async_add_entities([AIPoolTaskEntity(entry.runtime_data, entry)])


class AIPoolTaskEntity(AIPoolEntity, ai_task.AITaskEntity):
    """An ai_task entity that delegates generation to pool members.

    ``SUPPORT_ATTACHMENTS`` is deliberately not advertised: attachments reach
    the entity already resolved, and there is no supported way to hand a
    resolved attachment to another entity. Home Assistant therefore rejects
    such requests before they reach the pool, which is the honest outcome.
    """

    _attr_supported_features = ai_task.AITaskEntityFeature.GENERATE_DATA

    async def _async_generate_data(
        self,
        task: ai_task.GenDataTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenDataTaskResult:
        """Generate data using the first member that answers."""

        async def run(member: str) -> ai_task.GenDataTaskResult:
            return await ai_task.async_generate_data(
                self.hass,
                task_name=task.name,
                entity_id=member,
                instructions=task.instructions,
                structure=task.structure,
                llm_api=task.llm_api,
            )

        result = await self.pool.async_execute(run, description="generate_data")

        # The member's own conversation_id belongs to its chat session, not
        # ours; callers correlate against the pool's log.
        return ai_task.GenDataTaskResult(
            conversation_id=chat_log.conversation_id,
            data=result.data,
        )
