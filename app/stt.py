"""Speech-to-text providers over WebSocket / HTTP."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
from array import array
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

import httpx
import websockets

from .config import Settings


@dataclass
class TranscriptEvent:
    text: str
    is_final: bool
    confidence: float


TranscriptHandler = Callable[[TranscriptEvent], None]
ErrorHandler = Callable[[Exception], None]


class SttSession(Protocol):
    async def write_audio(self, samples: array) -> None: ...
    async def end_utterance(self) -> None: ...
    async def close(self) -> None: ...


class SttProvider(Protocol):
    id: str
    live: bool

    async def open(self, on_transcript: TranscriptHandler, on_error: ErrorHandler, language: str | None = None) -> SttSession: ...


def pcm16_bytes(samples: array) -> bytes:
    count = len(samples)
    return struct.pack(f"<{count}h", *(max(-32768, min(32767, int(round(value * 32767)))) for value in samples))


class DeepgramSession:
    def __init__(self, socket, on_transcript: TranscriptHandler, on_error: ErrorHandler) -> None:
        self.socket = socket
        self.on_transcript = on_transcript
        self.on_error = on_error
        self._tasks: list[asyncio.Task] = []
        self._closed = False

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._reader()))
        self._tasks.append(asyncio.create_task(self._keepalive()))

    async def _reader(self) -> None:
        try:
            async for raw in self.socket:
                try:
                    payload = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                kind = payload.get("type")
                if kind == "Results":
                    alternative = (payload.get("channel") or {}).get("alternatives") or [{}]
                    text = (alternative[0].get("transcript") or "").strip()
                    if text:
                        self.on_transcript(
                            TranscriptEvent(
                                text=text,
                                is_final=bool(payload.get("is_final") or payload.get("speech_final")),
                                confidence=float(alternative[0].get("confidence") or 0.0),
                            )
                        )
                elif kind == "Error":
                    self.on_error(RuntimeError(payload.get("description") or payload.get("message") or "deepgram error"))
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - surface to the call session
            if not self._closed:
                self.on_error(error)

    async def _keepalive(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(5)
                if self._closed:
                    return
                await self.socket.send(json.dumps({"type": "KeepAlive"}))
        except asyncio.CancelledError:
            return
        except Exception:
            return

    async def write_audio(self, samples: array) -> None:
        if self._closed:
            return
        try:
            await self.socket.send(pcm16_bytes(samples))
        except Exception:
            self._closed = True

    async def end_utterance(self) -> None:
        # Deepgram segments server-side via endpointing.
        return None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in self._tasks:
            task.cancel()
        try:
            await self.socket.send(json.dumps({"type": "CloseStream"}))
        except Exception:
            pass
        try:
            await self.socket.close()
        except Exception:
            pass


class DeepgramStt:
    id = "deepgram"
    live = True

    def __init__(self, api_key: str, model: str = "nova-3", endpointing_ms: int = 320) -> None:
        self.api_key = api_key
        self.model = model
        self.endpointing_ms = endpointing_ms

    async def open(self, on_transcript: TranscriptHandler, on_error: ErrorHandler, language: str | None = None) -> SttSession:
        params = {
            "model": self.model,
            "language": language or "en",
            "encoding": "linear16",
            "sample_rate": "16000",
            "channels": "1",
            "interim_results": "true",
            "smart_format": "true",
            "punctuate": "true",
            "endpointing": str(self.endpointing_ms),
            "utterance_end_ms": "1000",
            "vad_events": "true",
        }
        query = "&".join(f"{key}={value}" for key, value in params.items())
        uri = f"wss://api.deepgram.com/v1/listen?{query}"
        headers = {"Authorization": f"Token {self.api_key}"}
        try:
            socket = await websockets.connect(uri, additional_headers=headers, max_size=2**22)
        except TypeError:  # websockets < 14
            socket = await websockets.connect(uri, extra_headers=headers, max_size=2**22)  # type: ignore[call-arg]
        session = DeepgramSession(socket, on_transcript, on_error)
        session.start()
        return session


def encode_wav(samples: array, sample_rate: int) -> bytes:
    data = pcm16_bytes(samples)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(data),
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        len(data),
    )
    return header + data


class OpenAiSttSession:
    def __init__(self, api_key: str, model: str, language: str, on_transcript: TranscriptHandler, on_error: ErrorHandler, base_url: str = "https://api.openai.com/v1") -> None:
        self.api_key = api_key
        self.model = model
        self.language = language
        self.on_transcript = on_transcript
        self.on_error = on_error
        self.base_url = base_url.rstrip("/")
        self._buffer: list[array] = []
        self._samples = 0
        self._inflight: asyncio.Task | None = None
        self._closed = False

    async def write_audio(self, samples: array) -> None:
        if self._closed:
            return
        self._buffer.append(samples)
        self._samples += len(samples)
        if self._samples > 16000 * 30:  # never buffer more than 30 s
            self._buffer = self._buffer[len(self._buffer) // 2 :]
            self._samples = sum(len(chunk) for chunk in self._buffer)

    async def end_utterance(self) -> None:
        if self._closed or self._samples < 4000:
            return
        merged = array("f")
        for chunk in self._buffer:
            merged.extend(chunk)
        self._buffer = []
        self._samples = 0
        self._inflight = asyncio.create_task(self._transcribe(merged))

    async def _transcribe(self, samples: array) -> None:
        wav = encode_wav(samples, 16000)
        data = {"model": self.model, "response_format": "json"}
        if self.language:
            data["language"] = self.language.split("-")[0]
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    f"{self.base_url}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    files={"file": ("utterance.wav", wav, "audio/wav")},
                    data=data,
                )
            if response.status_code >= 400:
                raise RuntimeError(f"transcription failed ({response.status_code}): {response.text[:200]}")
            text = (response.json().get("text") or "").strip()
            if text and not self._closed:
                self.on_transcript(TranscriptEvent(text=text, is_final=True, confidence=0.9))
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            if not self._closed:
                self.on_error(error)

    async def close(self) -> None:
        self._closed = True
        self._buffer = []
        self._samples = 0
        if self._inflight:
            try:
                await self._inflight
            except Exception:
                pass


class OpenAiStt:
    id = "openai"
    live = True

    def __init__(self, api_key: str, model: str = "gpt-4o-mini-transcribe", language: str = "en") -> None:
        self.api_key = api_key
        self.model = model
        self.language = language

    async def open(self, on_transcript: TranscriptHandler, on_error: ErrorHandler, language: str | None = None) -> SttSession:
        return OpenAiSttSession(self.api_key, self.model, language or self.language, on_transcript, on_error)


class MockSttSession:
    async def write_audio(self, samples: array) -> None:
        return None

    async def end_utterance(self) -> None:
        return None

    async def close(self) -> None:
        return None


class MockStt:
    id = "mock"
    live = False

    async def open(self, on_transcript: TranscriptHandler, on_error: ErrorHandler, language: str | None = None) -> SttSession:
        return MockSttSession()


def build_stt(config: Settings) -> SttProvider:
    if config.stt.provider == "deepgram" and config.stt.api_key:
        return DeepgramStt(config.stt.api_key, config.stt.model, config.stt.endpointing_ms)
    if config.stt.provider == "openai" and config.stt.api_key:
        return OpenAiStt(config.stt.api_key, config.stt.model, config.stt.language)
    return MockStt()
