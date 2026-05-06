"""OpenAI-backed voice transcription and spoken summaries."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import VoiceConfig


class VoiceError(RuntimeError):
    """Base voice feature failure."""


class VoiceUnavailable(VoiceError):
    """Raised when voice is disabled or missing credentials."""


@dataclass(slots=True)
class SpeechFile:
    path: Path
    format: str
    text: str


class OpenAIVoiceService:
    """Small wrapper around OpenAI speech APIs.

    The OpenAI import is intentionally lazy so the rest of the bot can start
    even when the optional dependency or API key is missing.
    """

    def __init__(self, config: VoiceConfig) -> None:
        self.config = config

    @property
    def available(self) -> bool:
        return self.config.enabled and self.config.provider == "openai" and bool(self.config.api_key)

    def require_available(self) -> None:
        if not self.config.enabled:
            raise VoiceUnavailable("Voice features are disabled in config.")
        if self.config.provider != "openai":
            raise VoiceUnavailable(f"Unsupported voice provider: {self.config.provider}")
        if not self.config.api_key:
            raise VoiceUnavailable("OPENAI_API_KEY is missing in /etc/cursor2telegram/env.")

    def _client(self):
        self.require_available()
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - dependency installed in normal env
            raise VoiceUnavailable("The openai Python package is not installed.") from exc
        return AsyncOpenAI(api_key=self.config.api_key)

    async def transcribe(self, audio_path: Path) -> str:
        client = self._client()
        with audio_path.open("rb") as f:
            result = await client.audio.transcriptions.create(
                model=self.config.transcription_model,
                file=f,
            )
        text = getattr(result, "text", None)
        if not text and isinstance(result, dict):
            text = result.get("text")
        if not text:
            raise VoiceError("OpenAI transcription returned no text.")
        return str(text).strip()

    async def summarize(self, text: str, *, context: str = "") -> str:
        client = self._client()
        prompt = self.config.summary_prompt
        if context:
            prompt = f"{prompt}\nContext: {context}"
        trimmed = text.strip()
        if not trimmed:
            raise VoiceError("Nothing to summarize.")
        response = await client.chat.completions.create(
            model=self.config.summary_model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": trimmed[: self.config.summary_max_chars * 4]},
            ],
            max_tokens=80,
            temperature=0.2,
        )
        summary = response.choices[0].message.content if response.choices else ""
        summary = (summary or "").strip()
        if not summary:
            summary = trimmed[: self.config.summary_max_chars].strip()
        return summary

    async def synthesize(self, text: str, out_dir: Path, *, stem: str = "voice-summary") -> SpeechFile:
        client = self._client()
        out_dir.mkdir(parents=True, exist_ok=True)
        fmt = self.config.tts_format.strip().lower() or "opus"
        ext = "ogg" if fmt == "opus" else fmt
        out_path = out_dir / f"{stem}.{ext}"
        response = await client.audio.speech.create(
            model=self.config.tts_model,
            voice=self.config.tts_voice,
            input=text,
            response_format=fmt,
        )
        data = _response_bytes(response)
        out_path.write_bytes(data)
        return SpeechFile(path=out_path, format=fmt, text=text)


def _response_bytes(response: Any) -> bytes:
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content
    if hasattr(response, "read"):
        data = response.read()
        if isinstance(data, bytes):
            return data
    if isinstance(response, bytes):
        return response
    raise VoiceError("OpenAI TTS returned an unsupported response type.")
