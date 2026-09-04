"""Speech-to-text pool entity."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable

from homeassistant.components import stt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_STT_BUFFER_LIMIT, DEFAULT_STT_BUFFER_LIMIT
from .entity import AIPoolEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

CHUNK = 4096


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the pooled speech-to-text entity."""
    async_add_entities([AIPoolSTTEntity(entry.runtime_data, entry)])


class AIPoolSTTEntity(AIPoolEntity, stt.SpeechToTextEntity):
    """A stt entity that delegates recognition to pool members.

    Audio arrives as a one-shot async stream, so it is buffered before the
    first attempt: a second member cannot be handed a stream that the first
    one has already consumed.
    """

    def _member_entities(self) -> list[stt.SpeechToTextEntity]:
        """Resolve configured members to live stt entities."""
        found: list[stt.SpeechToTextEntity] = []
        for member in self.pool.members:
            entity = stt.async_get_speech_to_text_entity(self.hass, member.entity_id)
            if entity is not None:
                found.append(entity)
        return found

    def _union(self, attribute: str) -> list:
        """Union of a member capability list, preserving order."""
        values: list = []
        for entity in self._member_entities():
            for value in getattr(entity, attribute, None) or []:
                if value not in values:
                    values.append(value)
        return values

    def _intersection(self, attribute: str, fallback: list) -> list:
        """Intersection of a member capability list.

        Audio format capabilities use an intersection, unlike languages: the
        pipeline encodes the audio once, before any member is chosen, so the
        format has to be acceptable to every member the pool might route to.
        Falls back when members share nothing, since refusing all audio is
        worse than letting a member reject a request.
        """
        entities = self._member_entities()
        if not entities:
            return fallback
        common: set | None = None
        for entity in entities:
            values = set(getattr(entity, attribute, None) or [])
            common = values if common is None else (common & values)
        if not common:
            return fallback
        # Keep the declared order of the first member for stability.
        first = getattr(entities[0], attribute, None) or []
        return [value for value in first if value in common]

    @property
    def supported_languages(self) -> list[str]:
        """Union of the languages the members advertise."""
        return self._union("supported_languages") or [self.hass.config.language]

    @property
    def supported_formats(self) -> list[stt.AudioFormats]:
        """Formats every member accepts."""
        return self._intersection("supported_formats", [stt.AudioFormats.WAV])

    @property
    def supported_codecs(self) -> list[stt.AudioCodecs]:
        """Codecs every member accepts."""
        return self._intersection("supported_codecs", [stt.AudioCodecs.PCM])

    @property
    def supported_bit_rates(self) -> list[stt.AudioBitRates]:
        """Bit rates every member accepts."""
        return self._intersection("supported_bit_rates", [stt.AudioBitRates.BITRATE_16])

    @property
    def supported_sample_rates(self) -> list[stt.AudioSampleRates]:
        """Sample rates every member accepts."""
        return self._intersection(
            "supported_sample_rates", [stt.AudioSampleRates.SAMPLERATE_16000]
        )

    @property
    def supported_channels(self) -> list[stt.AudioChannels]:
        """Channel layouts every member accepts."""
        return self._intersection(
            "supported_channels", [stt.AudioChannels.CHANNEL_MONO]
        )

    @property
    def _buffer_limit(self) -> int:
        """Maximum bytes of audio held in memory for retries."""
        config = {**self._entry.data, **self._entry.options}
        return int(config.get(CONF_STT_BUFFER_LIMIT, DEFAULT_STT_BUFFER_LIMIT))

    async def async_process_audio_stream(
        self,
        metadata: stt.SpeechMetadata,
        stream: AsyncIterable[bytes],
    ) -> stt.SpeechResult:
        """Recognise using the first member that answers."""
        limit = self._buffer_limit
        buffer = bytearray()
        truncated = False
        async for chunk in stream:
            if len(buffer) + len(chunk) > limit:
                truncated = True
                break
            buffer.extend(chunk)

        if truncated:
            # Retrying with a clipped recording would silently transcribe half
            # a sentence, so the pool degrades to a single pass instead.
            _LOGGER.warning(
                "Pool %s: audio exceeded the %d byte retry buffer; "
                "falling back to a single attempt without failover",
                self._entry.title,
                limit,
            )

        audio = bytes(buffer)

        async def replay() -> AsyncIterable[bytes]:
            for offset in range(0, len(audio), CHUNK):
                yield audio[offset : offset + CHUNK]

        async def run(member: str) -> stt.SpeechResult:
            entity = stt.async_get_speech_to_text_entity(self.hass, member)
            if entity is None:
                raise HomeAssistantError(f"stt entity {member} not found")
            result = await entity.async_process_audio_stream(metadata, replay())
            if result.result is not stt.SpeechResultState.SUCCESS:
                # A member reporting failure must not end the request: without
                # this the pool would return an empty transcript from the first
                # broken member and never reach a working one.
                raise HomeAssistantError(f"stt member {member} returned an error")
            return result

        # Audio bytes rather than characters: not comparable to a text
        # prompt, but the only measure of how much this request weighs.
        # Audio bytes rather than characters: not comparable to a text prompt,
        # but the only measure of how much this request weighs.
        #
        # A clipped recording gets one attempt and no failover. Handing the
        # same half-sentence to a second member cannot produce a better
        # transcript, and doing it three times only multiplies the cost of a
        # request that was already compromised.
        return await self.pool.async_execute(
            run,
            description="stt",
            size=len(audio),
            attempt_limit=1 if truncated else None,
        )
