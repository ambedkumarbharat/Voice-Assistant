"""Text-to-speech providers returning raw PCM floats."""

from __future__ import annotations

import asyncio
import base64
import struct
import time
from array import array
from dataclasses import dataclass
from typing import Protocol

import httpx

from .audio import synthesize_speech_placeholder
from .config import Settings
from .errors import ProviderError


@dataclass
class TtsResult:
    audio: array
    sample_rate: int
    provider: str
    latency_ms: int


class TtsProvider(Protocol):
    id: str
    live: bool
    sample_rate: int

    async def synthesize(self, text: str, voice: str | None = None, cancel: asyncio.Event | None = None) -> TtsResult: ...


def _pcm_to_float(raw: bytes) -> array:
    count = len(raw) // 2
    if count == 0:
        return array("f")
    values = struct.unpack(f"<{count}h", raw[: count * 2])
    return array("f", (value / 32768.0 for value in values))


class ElevenLabsTts:
    id = "elevenlabs"
    live = True
    sample_rate = 24000

    def __init__(self, api_key: str, model: str = "eleven_flash_v2_5", default_voice: str = "21m00Tcm4TlvDq8ikWAM") -> None:
        self.api_key = api_key
        self.model = model
        self.default_voice = default_voice

    async def synthesize(self, text: str, voice: str | None = None, cancel: asyncio.Event | None = None) -> TtsResult:
        started = time.perf_counter()
        voice_id = voice or self.default_voice
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=pcm_24000"
        payload = {
            "text": text,
            "model_id": self.model,
            "voice_settings": {"stability": 0.45, "similarity_boost": 0.75, "style": 0.15, "use_speaker_boost": True},
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, headers={"xi-api-key": self.api_key}, json=payload)
        if response.status_code >= 400:
            raise ProviderError(f"elevenlabs synthesis failed ({response.status_code})", response.status_code, "elevenlabs")
        raw = response.content
        if len(raw) < 2:
            raise ProviderError("elevenlabs returned empty audio", 502, "elevenlabs")
        return TtsResult(_pcm_to_float(raw), self.sample_rate, self.id, int((time.perf_counter() - started) * 1000))


class OpenAiTts:
    id = "openai"
    live = True
    sample_rate = 24000

    def __init__(self, api_key: str, model: str = "gpt-4o-mini-tts", default_voice: str = "alloy", base_url: str = "https://api.openai.com/v1") -> None:
        self.api_key = api_key
        self.model = model
        self.default_voice = default_voice
        self.base_url = base_url.rstrip("/")

    async def synthesize(self, text: str, voice: str | None = None, cancel: asyncio.Event | None = None) -> TtsResult:
        started = time.perf_counter()
        payload = {
            "model": self.model,
            "voice": voice or self.default_voice,
            "input": text,
            "response_format": "pcm",
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{self.base_url}/audio/speech",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
        if response.status_code >= 400:
            raise ProviderError(f"openai tts failed ({response.status_code})", response.status_code, "openai")
        raw = response.content
        if len(raw) < 2:
            raise ProviderError("openai tts returned empty audio", 502, "openai")
        return TtsResult(_pcm_to_float(raw), self.sample_rate, self.id, int((time.perf_counter() - started) * 1000))


class DeepgramTts:
    id = "deepgram"
    live = True

    def __init__(self, api_key: str, model: str = "aura-2-thalia-en", sample_rate: int = 24000) -> None:
        self.api_key = api_key
        self.model = model
        self.sample_rate = sample_rate

    async def synthesize(self, text: str, voice: str | None = None, cancel: asyncio.Event | None = None) -> TtsResult:
        started = time.perf_counter()
        model = voice or self.model
        url = (
            "https://api.deepgram.com/v1/speak"
            f"?model={model}&encoding=linear16&sample_rate={self.sample_rate}&container=none"
        )
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Token {self.api_key}"},
                json={"text": text},
            )
        if response.status_code >= 400:
            raise ProviderError(f"deepgram tts failed ({response.status_code})", response.status_code, "deepgram")
        raw = response.content
        if len(raw) < 2:
            raise ProviderError("deepgram tts returned empty audio", 502, "deepgram")
        return TtsResult(_pcm_to_float(raw), self.sample_rate, self.id, int((time.perf_counter() - started) * 1000))


class MockTts:
    id = "mock"
    live = False
    sample_rate = 16000

    async def synthesize(self, text: str, voice: str | None = None, cancel: asyncio.Event | None = None) -> TtsResult:
        started = time.perf_counter()
        words = len(text.split())
        await asyncio.sleep(min(0.12, words * 0.004))
        if cancel is not None and cancel.is_set():
            return TtsResult(array("f"), self.sample_rate, self.id, 0)
        return TtsResult(
            synthesize_speech_placeholder(text, self.sample_rate),
            self.sample_rate,
            self.id,
            int((time.perf_counter() - started) * 1000),
        )


def build_tts(config: Settings) -> TtsProvider:
    cfg = config.tts
    if cfg.provider == "elevenlabs" and cfg.api_key:
        return ElevenLabsTts(cfg.api_key, cfg.model, cfg.voice or "21m00Tcm4TlvDq8ikWAM")
    if cfg.provider == "openai" and cfg.api_key:
        return OpenAiTts(cfg.api_key, cfg.model, cfg.voice or "alloy")
    if cfg.provider == "deepgram" and cfg.api_key:
        return DeepgramTts(cfg.api_key, cfg.model or "aura-2-thalia-en", cfg.sample_rate)
    return MockTts()
