"""Transport adapters and the real-time paced audio player."""

from __future__ import annotations

import asyncio
import base64
from array import array
from collections import deque
from typing import Any, Awaitable, Callable, Protocol

from .audio import (
    PIPELINE_RATE,
    TELEPHONY_FRAME_SAMPLES,
    TELEPHONY_RATE,
    encode_mulaw,
    encode_pcm16_base64,
    frame_audio,
    resample,
)


class CallChannel(Protocol):
    kind: str

    async def send_audio(self, samples: array) -> None: ...
    async def clear_audio(self) -> None: ...
    async def close(self) -> None: ...


class TwilioChannel:
    kind = "twilio"

    def __init__(self, websocket: Any, stream_sid: str) -> None:
        self.websocket = websocket
        self.stream_sid = stream_sid

    async def send_audio(self, samples: array) -> None:
        downsampled = resample(samples, PIPELINE_RATE, TELEPHONY_RATE)
        for offset in range(0, len(downsampled), TELEPHONY_FRAME_SAMPLES):
            frame = downsampled[offset : offset + TELEPHONY_FRAME_SAMPLES]
            payload = base64.b64encode(encode_mulaw(frame)).decode("ascii")
            await self.websocket.send_json({"event": "media", "streamSid": self.stream_sid, "media": {"payload": payload}})

    async def clear_audio(self) -> None:
        try:
            await self.websocket.send_json({"event": "clear", "streamSid": self.stream_sid})
        except Exception:
            pass

    async def close(self) -> None:
        try:
            await self.websocket.close()
        except Exception:
            pass


class BrowserChannel:
    kind = "browser"

    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._send = send

    async def send_audio(self, samples: array) -> None:
        chunk_size = PIPELINE_RATE * 40 // 1000  # 40 ms
        for offset in range(0, len(samples), chunk_size):
            piece = samples[offset : offset + chunk_size]
            await self._send(
                {
                    "type": "audio",
                    "format": "pcm16",
                    "sampleRate": PIPELINE_RATE,
                    "payload": encode_pcm16_base64(piece),
                }
            )

    async def clear_audio(self) -> None:
        try:
            await self._send({"type": "clear"})
        except Exception:
            pass

    async def close(self) -> None:
        try:
            await self._send({"type": "call.ended"})
        except Exception:
            pass


class AudioPlayer:
    """Releases 20 ms frames on a real-time clock so barge-in is instant."""

    def __init__(
        self,
        channel: CallChannel,
        frame_ms: int = 20,
        on_start: Callable[[], None] | None = None,
    ) -> None:
        self.channel = channel
        self.frame_ms = frame_ms
        self.on_start = on_start
        self._queue: deque[array] = deque()
        self._task: asyncio.Task | None = None
        self._idle = asyncio.Event()
        self._idle.set()
        self._started = False

    @property
    def speaking(self) -> bool:
        return bool(self._queue) or self._task is not None

    def enqueue(self, samples: array) -> None:
        frame_size = PIPELINE_RATE * self.frame_ms // 1000
        for frame in frame_audio(samples, frame_size):
            self._queue.append(frame)
        if self._queue:
            self._idle.clear()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while self._queue:
                frame = self._queue.popleft()
                if not self._started:
                    self._started = True
                    if self.on_start:
                        self.on_start()
                try:
                    await self.channel.send_audio(frame)
                except Exception:
                    break
                await asyncio.sleep(self.frame_ms / 1000)
        finally:
            self._task = None
            self._started = False
            if not self._queue:
                self._idle.set()

    async def wait_idle(self) -> None:
        await self._idle.wait()

    def stop(self) -> None:
        self._queue.clear()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._started = False
        self._idle.set()
        asyncio.create_task(self.channel.clear_audio())
