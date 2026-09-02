"""Text-to-speech pool entity."""

from __future__ import annotations

from typing import Any

from homeassistant.components import tts
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
    """Set up the pooled text-to-speech entity."""
    async_add_entities([AIPoolTTSEntity(entry.runtime_data, entry)])


class AIPoolTTSEntity(AIPoolEntity, tts.TextToSpeechEntity):
    """A tts entity that delegates synthesis to pool members."""

    def _member_entities(self) -> list[tts.TextToSpeechEntity]:
        """Resolve configured members to live tts entities."""
        component = self.hass.data.get(tts.DATA_COMPONENT)
        if component is None:
            return []
        found: list[tts.TextToSpeechEntity] = []
        for member in self.pool.members:
            entity = component.get_entity(member.entity_id)
            if entity is not None:
                found.append(entity)
        return found

    @property
    def default_language(self) -> str:
        """Language used when a caller does not specify one."""
        for entity in self._member_entities():
            if entity.default_language:
                return entity.default_language
        return self.hass.config.language

    @property
    def supported_languages(self) -> list[str]:
        """Union of the languages the members advertise.

        A union, not an intersection: narrowing to the common subset would make
        the pool refuse work that some members can do. A member that cannot
        handle a language raises, and the pool moves to the next one.
        """
        languages: list[str] = []
        for entity in self._member_entities():
            for language in entity.supported_languages or []:
                if language not in languages:
                    languages.append(language)
        return languages or [self.hass.config.language]

    @property
    def supported_options(self) -> list[str]:
        """Union of the options the members accept."""
        options: list[str] = []
        for entity in self._member_entities():
            for option in entity.supported_options or []:
                if option not in options:
                    options.append(option)
        return options

    async def async_get_tts_audio(
        self,
        message: str,
        language: str,
        options: dict[str, Any] | None = None,
    ) -> tts.TtsAudioType:
        """Synthesise using the first member that answers."""

        async def run(member: str) -> tts.TtsAudioType:
            media_source_id = tts.generate_media_source_id(
                self.hass,
                message=message,
                engine=member,
                language=language,
                options=options,
                # The manager already caches the pool's own output according to
                # what the caller asked for; caching the member's copy as well
                # would store the same audio twice.
                cache=False,
            )
            return await tts.async_get_media_source_audio(self.hass, media_source_id)

        return await self.pool.async_execute(run, description="tts")
