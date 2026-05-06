from pathlib import Path
from types import SimpleNamespace

import pytest

from cursor2telegram.config import VoiceConfig
from cursor2telegram.voice import OpenAIVoiceService, VoiceUnavailable, _response_bytes


def test_voice_availability_requires_key():
    assert not OpenAIVoiceService(VoiceConfig(api_key="")).available
    assert OpenAIVoiceService(VoiceConfig(api_key="sk-test")).available


def test_voice_require_available_missing_key():
    service = OpenAIVoiceService(VoiceConfig(api_key=""))
    with pytest.raises(VoiceUnavailable):
        service.require_available()


def test_response_bytes_from_content():
    assert _response_bytes(SimpleNamespace(content=b"abc")) == b"abc"


@pytest.mark.asyncio
async def test_voice_service_with_fake_client(monkeypatch, tmp_path: Path):
    service = OpenAIVoiceService(VoiceConfig(api_key="sk-test"))
    audio_path = tmp_path / "in.ogg"
    audio_path.write_bytes(b"voice")

    class FakeTranscriptions:
        async def create(self, **kwargs):
            assert kwargs["model"] == "whisper-1"
            return SimpleNamespace(text="hello from voice")

    class FakeSpeech:
        async def create(self, **kwargs):
            assert kwargs["input"] == "short summary"
            return SimpleNamespace(content=b"audio-bytes")

    class FakeCompletions:
        async def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="short summary"))]
            )

    fake_client = SimpleNamespace(
        audio=SimpleNamespace(transcriptions=FakeTranscriptions(), speech=FakeSpeech()),
        chat=SimpleNamespace(completions=FakeCompletions()),
    )
    monkeypatch.setattr(service, "_client", lambda: fake_client)

    assert await service.transcribe(audio_path) == "hello from voice"
    assert await service.summarize("a long result") == "short summary"
    speech = await service.synthesize("short summary", tmp_path)
    assert speech.path.read_bytes() == b"audio-bytes"
    assert speech.format == "opus"
