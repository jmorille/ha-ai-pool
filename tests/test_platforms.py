"""Delegation through real member entities, one pool type at a time.

These tests publish fake members into the domain the pool fronts and then call
the pool through the same public entry points Home Assistant uses, so a wrong
helper signature or a broken adapter fails here rather than on someone's
instance.
"""

from collections.abc import AsyncIterable
from typing import Any

import pytest
from homeassistant.components import ai_task, conversation, stt, tts
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import intent
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockEntityPlatform,
    MockPlatform,
)

from custom_components.ai_pool.const import (
    CONF_COOLDOWN,
    CONF_DAILY_LIMIT,
    CONF_MAX_ATTEMPTS,
    CONF_MEMBERS,
    CONF_POOL_TYPE,
    CONF_STRATEGY,
    CONF_STT_BUFFER_LIMIT,
    CONF_WEIGHT,
    DOMAIN,
    STATUS_COOLDOWN,
    STATUS_EXHAUSTED,
    STRATEGY_PRIORITY,
)
from custom_components.ai_pool.diagnostics import async_get_config_entry_diagnostics

QUOTA = '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}'
CAPACITY = '{"error": {"code": 503, "status": "UNAVAILABLE"}}'

TITLE = "Test pool"
A = "member_a"
B = "member_b"


@pytest.fixture(autouse=True)
async def setup_core(hass: HomeAssistant) -> None:
    """Set up the core integration the fronted domains rely on."""
    assert await async_setup_component(hass, "homeassistant", {})


async def publish_members(
    hass: HomeAssistant, domain: str, entities: list[Any]
) -> None:
    """Publish fake entities into ``domain``.

    Every entity platform of a domain shares one entity map, so entities added
    here are found by the same public lookups the pool goes through.
    """
    platform = MockEntityPlatform(
        hass,
        domain=domain,
        platform_name="ai_pool_member",
        platform=MockPlatform(),
    )
    await platform.async_add_entities(entities)
    await hass.async_block_till_done()


async def setup_pool(
    hass: HomeAssistant,
    pool_type: str,
    *,
    limits: dict[str, int] | None = None,
    extra: dict[str, Any] | None = None,
) -> MockConfigEntry:
    """Set up a pool of the given type over ``member_a`` and ``member_b``."""
    limits = limits or {}
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        data={
            CONF_POOL_TYPE: pool_type,
            CONF_STRATEGY: STRATEGY_PRIORITY,
            CONF_COOLDOWN: 300,
            CONF_MAX_ATTEMPTS: 3,
            **(extra or {}),
            CONF_MEMBERS: [
                {
                    "entity_id": f"{pool_type}.{name}",
                    CONF_DAILY_LIMIT: limits.get(name, 0),
                    CONF_WEIGHT: 1,
                }
                for name in (A, B)
            ],
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def status_of(entry: MockConfigEntry, entity_id: str) -> str:
    """Look up one member's status in the pool snapshot."""
    rows = {row.entity_id: row for row in entry.runtime_data.snapshot()}
    return rows[entity_id].status


# --- ai_task ----------------------------------------------------------------


class FakeTaskMember(ai_task.AITaskEntity):
    """An ai_task member that answers, or fails with a given message."""

    _attr_supported_features = ai_task.AITaskEntityFeature.GENERATE_DATA

    def __init__(
        self, name: str, *, data: Any = None, error: str | None = None
    ) -> None:
        """Initialise the fake member."""
        self._attr_name = name
        self._data = data
        self._error = error
        self.calls = 0
        self.seen_instructions: list[str] = []

    async def _async_generate_data(
        self, task: ai_task.GenDataTask, chat_log: conversation.ChatLog
    ) -> ai_task.GenDataTaskResult:
        """Answer the task."""
        self.calls += 1
        self.seen_instructions.append(task.instructions)
        if self._error:
            raise HomeAssistantError(self._error)
        return ai_task.GenDataTaskResult(
            conversation_id=chat_log.conversation_id, data=self._data
        )


async def test_ai_task_pool_falls_back_to_the_next_member(hass: HomeAssistant) -> None:
    """An exhausted member must not end the request."""
    assert await async_setup_component(hass, "ai_task", {})
    member_a = FakeTaskMember(A, error=QUOTA)
    member_b = FakeTaskMember(B, data={"answer": "from b"})
    await publish_members(hass, "ai_task", [member_a, member_b])
    entry = await setup_pool(hass, "ai_task")

    result = await ai_task.async_generate_data(
        hass,
        task_name="pool test",
        entity_id="ai_task.test_pool",
        instructions="say something",
    )

    assert result.data == {"answer": "from b"}
    assert member_a.calls == 1
    assert member_b.seen_instructions == ["say something"]
    assert status_of(entry, "ai_task.member_a") == STATUS_EXHAUSTED


async def test_ai_task_pool_serves_the_first_member(hass: HomeAssistant) -> None:
    """The healthy first choice is used alone."""
    assert await async_setup_component(hass, "ai_task", {})
    member_a = FakeTaskMember(A, data={"answer": "from a"})
    member_b = FakeTaskMember(B, data={"answer": "from b"})
    await publish_members(hass, "ai_task", [member_a, member_b])
    await setup_pool(hass, "ai_task")

    result = await ai_task.async_generate_data(
        hass,
        task_name="pool test",
        entity_id="ai_task.test_pool",
        instructions="say something",
    )

    assert result.data == {"answer": "from a"}
    assert member_b.calls == 0


async def test_ai_task_pool_refuses_attachments(hass: HomeAssistant) -> None:
    """Attachments cannot be forwarded, so they are rejected up front."""
    assert await async_setup_component(hass, "ai_task", {})
    await publish_members(hass, "ai_task", [FakeTaskMember(A, data={})])
    await setup_pool(hass, "ai_task")

    with pytest.raises(HomeAssistantError, match="attachments"):
        await ai_task.async_generate_data(
            hass,
            task_name="pool test",
            entity_id="ai_task.test_pool",
            instructions="describe this",
            attachments=[
                {
                    "media_content_id": "media-source://x",
                    "media_content_type": "image/jpeg",
                }
            ],
        )


# --- conversation -----------------------------------------------------------


class FakeConversationMember(conversation.ConversationEntity):
    """A conversation member that answers, errors, or raises."""

    def __init__(
        self,
        name: str,
        *,
        speech: str = "",
        error_response: bool = False,
        error: str | None = None,
    ) -> None:
        """Initialise the fake member."""
        self._attr_name = name
        self._speech = speech
        self._error_response = error_response
        self._error = error
        self.calls = 0

    @property
    def supported_languages(self) -> list[str] | str:
        """Any language, like most LLM-backed agents."""
        return conversation.MATCH_ALL

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Answer the sentence."""
        self.calls += 1
        if self._error:
            raise HomeAssistantError(self._error)
        response = intent.IntentResponse(language=user_input.language or "en")
        if self._error_response:
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN, "provider is out of quota"
            )
        else:
            response.async_set_speech(self._speech)
        return conversation.ConversationResult(
            response=response, conversation_id=chat_log.conversation_id
        )


async def test_conversation_pool_skips_an_error_response(hass: HomeAssistant) -> None:
    """Answering "sorry" is a failure, not an answer.

    Without this the pool would return the first broken member's apology and
    never reach a working one.
    """
    assert await async_setup_component(hass, "conversation", {})
    member_a = FakeConversationMember(A, error_response=True)
    member_b = FakeConversationMember(B, speech="answer from b")
    await publish_members(hass, "conversation", [member_a, member_b])
    await setup_pool(hass, "conversation")

    result = await conversation.async_converse(
        hass,
        text="hello",
        conversation_id=None,
        context=Context(),
        agent_id="conversation.test_pool",
    )

    assert result.response.speech["plain"]["speech"] == "answer from b"
    assert member_a.calls == 1


async def test_conversation_pool_answers_from_the_first_member(
    hass: HomeAssistant,
) -> None:
    """A working first member is answered from directly."""
    assert await async_setup_component(hass, "conversation", {})
    member_a = FakeConversationMember(A, speech="answer from a")
    member_b = FakeConversationMember(B, speech="answer from b")
    await publish_members(hass, "conversation", [member_a, member_b])
    await setup_pool(hass, "conversation")

    result = await conversation.async_converse(
        hass,
        text="hello",
        conversation_id=None,
        context=Context(),
        agent_id="conversation.test_pool",
    )

    assert result.response.speech["plain"]["speech"] == "answer from a"
    assert member_b.calls == 0


# --- tts --------------------------------------------------------------------


class FakeTTSMember(tts.TextToSpeechEntity):
    """A tts member that returns fixed audio, or fails."""

    def __init__(
        self,
        name: str,
        *,
        audio: bytes = b"",
        error: str | None = None,
        languages: list[str] | None = None,
    ) -> None:
        """Initialise the fake member."""
        self._attr_name = name
        self._attr_default_language = "en"
        self._attr_supported_languages = languages or ["en"]
        self._audio = audio
        self._error = error
        self.calls = 0

    def get_tts_audio(
        self, message: str, language: str, options: dict[str, Any] | None = None
    ) -> tts.TtsAudioType:
        """Not used; the async variant is implemented instead."""
        raise NotImplementedError

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict[str, Any] | None = None
    ) -> tts.TtsAudioType:
        """Synthesise the message."""
        self.calls += 1
        if self._error:
            raise HomeAssistantError(self._error)
        return "mp3", self._audio


async def test_tts_pool_returns_audio_from_the_next_member(
    hass: HomeAssistant,
) -> None:
    """A busy provider is skipped and the audio still arrives."""
    assert await async_setup_component(hass, "tts", {})
    member_a = FakeTTSMember(A, error=CAPACITY)
    member_b = FakeTTSMember(B, audio=b"audio-from-b")
    await publish_members(hass, "tts", [member_a, member_b])
    entry = await setup_pool(hass, "tts")

    media_source_id = tts.generate_media_source_id(
        hass,
        "bonjour",
        engine="tts.test_pool",
        language="en",
        options=None,
        cache=False,
    )
    extension, audio = await tts.async_get_media_source_audio(hass, media_source_id)

    assert audio == b"audio-from-b"
    assert extension == "mp3"
    assert member_a.calls == 1
    # A 503 is a temporary refusal, so the member is cooled down rather than
    # written off for the day.
    assert status_of(entry, "tts.member_a") == STATUS_COOLDOWN


async def test_tts_pool_advertises_the_union_of_member_languages(
    hass: HomeAssistant,
) -> None:
    """Narrowing to the common subset would refuse work a member can do."""
    assert await async_setup_component(hass, "tts", {})
    await publish_members(
        hass,
        "tts",
        [
            FakeTTSMember(A, audio=b"a", languages=["en", "fr"]),
            FakeTTSMember(B, audio=b"b", languages=["fr", "de"]),
        ],
    )
    await setup_pool(hass, "tts")

    pool_entity = hass.data[tts.DATA_COMPONENT].get_entity("tts.test_pool")
    assert pool_entity is not None
    assert sorted(pool_entity.supported_languages) == ["de", "en", "fr"]
    # The tts manager refuses an engine whose name is None.
    assert pool_entity.name == TITLE


# --- stt --------------------------------------------------------------------


class FakeSTTMember(stt.SpeechToTextEntity):
    """A stt member that transcribes fixed text, or fails."""

    def __init__(self, name: str, *, text: str = "", error: str | None = None) -> None:
        """Initialise the fake member."""
        self._attr_name = name
        self._text = text
        self._error = error
        self.calls = 0
        self.received = b""

    @property
    def supported_languages(self) -> list[str]:
        """Languages this member accepts."""
        return ["en-US"]

    @property
    def supported_formats(self) -> list[stt.AudioFormats]:
        """Formats this member accepts."""
        return [stt.AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[stt.AudioCodecs]:
        """Codecs this member accepts."""
        return [stt.AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[stt.AudioBitRates]:
        """Bit rates this member accepts."""
        return [stt.AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[stt.AudioSampleRates]:
        """Sample rates this member accepts."""
        return [stt.AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self) -> list[stt.AudioChannels]:
        """Channel layouts this member accepts."""
        return [stt.AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(
        self, metadata: stt.SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> stt.SpeechResult:
        """Transcribe the recording."""
        self.calls += 1
        self.received = b"".join([chunk async for chunk in stream])
        if self._error:
            raise HomeAssistantError(self._error)
        return stt.SpeechResult(self._text, stt.SpeechResultState.SUCCESS)


def audio_metadata() -> stt.SpeechMetadata:
    """Metadata matching what the fake members accept."""
    return stt.SpeechMetadata(
        language="en-US",
        format=stt.AudioFormats.WAV,
        codec=stt.AudioCodecs.PCM,
        bit_rate=stt.AudioBitRates.BITRATE_16,
        sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
        channel=stt.AudioChannels.CHANNEL_MONO,
    )


async def test_stt_pool_replays_the_recording_to_the_next_member(
    hass: HomeAssistant,
) -> None:
    """The audio stream is one-shot, so the pool has to buffer it.

    Without buffering the second member would receive an already-drained
    stream and transcribe silence.
    """
    assert await async_setup_component(hass, "stt", {})
    member_a = FakeSTTMember(A, error=CAPACITY)
    member_b = FakeSTTMember(B, text="transcript from b")
    await publish_members(hass, "stt", [member_a, member_b])
    await setup_pool(hass, "stt")

    recording = b"0123456789" * 900  # spans several replay chunks

    async def stream() -> AsyncIterable[bytes]:
        for offset in range(0, len(recording), 1024):
            yield recording[offset : offset + 1024]

    pool_entity = stt.async_get_speech_to_text_entity(hass, "stt.test_pool")
    assert pool_entity is not None
    result = await pool_entity.async_process_audio_stream(audio_metadata(), stream())

    assert result.result is stt.SpeechResultState.SUCCESS
    assert result.text == "transcript from b"
    assert member_a.received == recording
    assert member_b.received == recording


async def test_stt_pool_does_not_replay_a_clipped_recording(
    hass: HomeAssistant,
) -> None:
    """A recording too long to buffer gets one attempt and no failover.

    Handing the same half-sentence to a second member cannot produce a better
    transcript; it only multiplies the cost of a request already compromised.
    """
    assert await async_setup_component(hass, "stt", {})
    member_a = FakeSTTMember(A, error=CAPACITY)
    member_b = FakeSTTMember(B, text="transcript from b")
    await publish_members(hass, "stt", [member_a, member_b])
    await setup_pool(hass, "stt", extra={CONF_STT_BUFFER_LIMIT: 2048})

    recording = b"0123456789" * 900  # 9000 bytes, well over the 2 KiB buffer

    async def stream() -> AsyncIterable[bytes]:
        for offset in range(0, len(recording), 1024):
            yield recording[offset : offset + 1024]

    pool_entity = stt.async_get_speech_to_text_entity(hass, "stt.test_pool")
    assert pool_entity is not None
    with pytest.raises(HomeAssistantError):
        await pool_entity.async_process_audio_stream(audio_metadata(), stream())

    # The first member was tried with what fitted; the second was never asked.
    assert member_a.calls == 1
    assert len(member_a.received) <= 2048
    assert member_b.calls == 0


async def test_stt_pool_intersects_audio_capabilities(hass: HomeAssistant) -> None:
    """The pipeline encodes once, before a member is chosen."""
    assert await async_setup_component(hass, "stt", {})
    await publish_members(hass, "stt", [FakeSTTMember(A), FakeSTTMember(B)])
    await setup_pool(hass, "stt")

    pool_entity = stt.async_get_speech_to_text_entity(hass, "stt.test_pool")
    assert pool_entity is not None
    assert pool_entity.supported_formats == [stt.AudioFormats.WAV]
    assert pool_entity.supported_sample_rates == [stt.AudioSampleRates.SAMPLERATE_16000]
    assert pool_entity.supported_languages == ["en-US"]


# --- diagnostics ------------------------------------------------------------


async def test_diagnostics_report_policy_and_members(hass: HomeAssistant) -> None:
    """Diagnostics carry enough to explain a routing decision."""
    assert await async_setup_component(hass, "ai_task", {})
    await publish_members(hass, "ai_task", [FakeTaskMember(A, data={})])
    entry = await setup_pool(hass, "ai_task", limits={A: 25})

    data = await async_get_config_entry_diagnostics(hass, entry)

    assert data["strategy"] == STRATEGY_PRIORITY
    assert data["max_attempts"] == 3
    assert data["cooldown_seconds"] == 300
    # Diagnostics is a JSON dump, so the views arrive flattened to mappings.
    members = {row["entity_id"]: row for row in data["members"]}
    assert members["ai_task.member_a"]["daily_limit"] == 25
    assert members["ai_task.member_a"]["remaining"] == 25
