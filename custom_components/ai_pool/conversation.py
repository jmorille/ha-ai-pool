"""Conversation agent pool entity."""

from __future__ import annotations

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import intent
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import AIPoolEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the pooled conversation agent."""
    async_add_entities([AIPoolConversationEntity(entry.runtime_data, entry)])


class AIPoolConversationEntity(AIPoolEntity, conversation.ConversationEntity):
    """A conversation agent that delegates to pool members."""

    @property
    def supported_languages(self) -> list[str] | str:
        """Languages supported by the pool.

        Members may disagree, so the pool advertises match-all and lets the
        member reject a language it cannot handle - which the classifier then
        treats as a reason to try the next one.
        """
        return conversation.MATCH_ALL

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Answer using the first member that responds."""

        async def run(member: str) -> conversation.ConversationResult:
            result = await conversation.async_converse(
                self.hass,
                text=user_input.text,
                conversation_id=user_input.conversation_id,
                context=user_input.context,
                language=user_input.language,
                agent_id=member,
                device_id=user_input.device_id,
            )
            if result.response.response_type is intent.IntentResponseType.ERROR:
                # A member that answers with an error still counts as a failure,
                # otherwise the pool would happily return "sorry" from the first
                # broken member and never reach a working one.
                speech = result.response.speech.get("plain", {}).get("speech", "")
                raise HomeAssistantError(
                    f"conversation member {member} returned an error response: {speech}"
                )
            return result

        return await self.pool.async_execute(run, description="conversation")
