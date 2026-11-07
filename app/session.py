"""One live call: VAD + STT -> LangChain stream -> sentence TTS -> paced audio."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from array import array
from typing import Any

from .audio import PIPELINE_FRAME_SAMPLES, PIPELINE_RATE, resample, synthesize_speech_placeholder
from .channels import AudioPlayer, CallChannel
from .config import Settings
from .db import Store, now_ms
from .hub import CallHub
from .prompts import (
    TRANSFER_MARKER,
    AgentSettings,
    build_system_prompt,
    load_agent_settings,
    sanitize_for_speech,
)
from .providers import Providers
from .segmenter import SentenceSegmenter
from .stt import SttSession, TranscriptEvent
from .vad import EnergyVad

log = logging.getLogger("voxdesk.session")

_PARTIAL_MARKER = re.compile(r"<+[A-Z]*>{0,2}$")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().strip(".,!?").strip()


class CallSession:
    def __init__(
        self,
        *,
        config: Settings,
        store: Store,
        providers: Providers,
        hub: CallHub,
        channel: CallChannel,
        direction: str,
        agent: AgentSettings | None = None,
        call_id: str | None = None,
        greeting: str | None = None,
        to_number: str | None = None,
        from_number: str | None = None,
        contact_name: str | None = None,
        provider_call_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.providers = providers
        self.hub = hub
        self.channel = channel
        self.direction = direction
        self.agent = agent or load_agent_settings(store, config)
        self.system_prompt = build_system_prompt(self.agent, config)
        self.greeting_override = greeting
        self.provider_call_id = provider_call_id
        self.started_at = now_ms()
        self.turns = 0
        self.history: list[dict[str, str]] = []
        self.status = "initializing"
        self._ended = False

        record = store.get_call(call_id) if call_id else None
        if record:
            store.update_call(
                record["id"],
                {
                    "status": "in_progress",
                    "transport": channel.kind,
                    "provider_call_id": provider_call_id or record.get("provider_call_id"),
                },
            )
            self.id = record["id"]
        else:
            self.id = store.create_call(
                call_id=call_id,
                direction=direction,
                transport=channel.kind,
                status="in_progress",
                from_number=from_number,
                to_number=to_number,
                contact_name=contact_name,
                provider_call_id=provider_call_id,
                metadata=metadata,
            )["id"]

        self.vad = EnergyVad(frame_ms=20, hangover_ms=650, min_speech_ms=200)
        self.player = AudioPlayer(channel, 20, on_start=self._on_audio_start)
        self._stt: SttSession | None = None
        self._stt_reconnects = 0
        self._stt_language = self.agent.language

        self._pending_user: list[str] = []
        self._queued_user: list[str] = []
        self._debounce_task: asyncio.Task | None = None
        self._user_task: asyncio.Task | None = None
        self._speech_tasks: set[asyncio.Task] = set()
        self._silence_tasks: list[asyncio.Task] = []
        self._max_duration_task: asyncio.Task | None = None
        self._cancel: asyncio.Event | None = None
        self._turn_in_flight = False
        self._current_reply = ""
        self._spoken_chunks: list[str] = []
        self._reply_recorded = False
        self._hold = ""
        self._transfer_requested = False
        self._transfer_initiated = False
        self._turn_started = 0
        self._first_audio_ms: int | None = None
        self._last_level_publish = 0

    # ------------------------------------------------------------- lifecycle ---

    async def start(self) -> None:
        self.hub.add(self)
        self.store.update_call(self.id, {"answered_at": now_ms()})
        self.store.add_event(
            self.id,
            "call.started",
            {
                "transport": self.channel.kind,
                "direction": self.direction,
                "llm": self.providers.llm.id,
                "stt": self.providers.stt.id,
                "tts": self.providers.tts.id,
            },
        )
        self.publish(
            "call.started",
            {
                "direction": self.direction,
                "transport": self.channel.kind,
                "agent": self.agent.name,
                "llm": self.providers.llm.id,
                "stt": self.providers.stt.id,
                "tts": self.providers.tts.id,
                "demo": self.providers.demo_mode,
            },
        )
        self._set_status("listening")
        self._max_duration_task = asyncio.create_task(self._max_duration())

        await self._connect_stt()

        greeting = self.agent.greeting if self.greeting_override is None else self.greeting_override
        if greeting and not self._ended:
            # Persist the scripted greeting before synthesis so the transcript is
            # ordered even if the caller barges in immediately.
            self.store.add_message(self.id, "assistant", greeting)
            self.history.append({"role": "assistant", "content": greeting})
            self.publish("assistant.message", {"text": greeting, "scripted": True})
            await self._say(greeting, record=False)
        if not self._ended:
            self._set_status("listening")
            self._arm_silence()
            self.publish("call.ready", {"greetingSkipped": not bool(greeting)})

    async def handle_audio(self, samples: array) -> None:
        if self._ended:
            return
        for offset in range(0, len(samples), PIPELINE_FRAME_SAMPLES):
            frame = samples[offset : offset + PIPELINE_FRAME_SAMPLES]
            if len(frame) < PIPELINE_FRAME_SAMPLES // 2:
                continue
            decision = self.vad.push(frame)
            if self._stt is not None:
                try:
                    await self._stt.write_audio(frame)
                except Exception as error:  # noqa: BLE001
                    log.debug("stt write failed: %s", error)

            if decision.speech_start:
                self._clear_silence()
                if self.agent.barge_in and self.status in {"speaking", "thinking"}:
                    self.interrupt("caller_barge_in")
                self.publish("caller.speaking", {"levelDb": round(decision.level_db)})
            if decision.speech_end:
                if self._stt is not None:
                    try:
                        await self._stt.end_utterance()
                    except Exception:
                        pass
                self.publish("caller.silence", {})

            current = now_ms()
            if current - self._last_level_publish > 100:
                self._last_level_publish = current
                self.publish("caller.level", {"levelDb": round(decision.level_db), "speaking": decision.speaking})

    async def inject_text(self, raw: str) -> None:
        if self._ended:
            return
        text = _normalize(raw)
        if not text:
            return
        self._clear_silence()
        if self.status in {"speaking", "thinking"}:
            self.interrupt("text_input")
        self.publish("transcript.final", {"text": text, "confidence": 1, "source": "text"})
        await self._handle_user_text(text)

    async def hangup(self) -> None:
        await self.end("agent_hangup")

    async def end(self, reason: str) -> None:
        if self._ended:
            return
        self._ended = True
        self._clear_silence()
        if self._max_duration_task:
            self._max_duration_task.cancel()
        if self._debounce_task:
            self._debounce_task.cancel()
        for task in list(self._speech_tasks):
            task.cancel()
        self._speech_tasks.clear()
        if self._cancel:
            self._cancel.set()
        current = asyncio.current_task()
        if self._user_task and self._user_task is not current:
            self._user_task.cancel()
        self.player.stop()

        if self._stt is not None:
            try:
                await self._stt.close()
            except Exception:
                pass
            self._stt = None

        ended_at = now_ms()
        duration = ended_at - self.started_at
        final_reason = "transferred" if self._transfer_initiated and reason != "transferred" else reason
        self._set_status("ended")
        self.store.update_call(
            self.id,
            {
                "status": "completed",
                "ended_at": ended_at,
                "duration_ms": duration,
                "end_reason": final_reason,
                "turns": self.turns,
            },
        )
        self.store.add_event(self.id, "call.ended", {"reason": final_reason, "durationMs": duration, "turns": self.turns})
        self.publish("call.ended", {"reason": final_reason, "durationMs": duration, "turns": self.turns})
        self.hub.remove(self.id)
        try:
            await self.channel.close()
        except Exception:
            pass

        if self.providers.telephony.live and self.provider_call_id:
            if reason in {"agent_hangup", "silence_timeout", "max_duration", "api_hangup", "session_error"}:
                try:
                    await self.providers.telephony.hangup(self.provider_call_id)
                except Exception as error:  # noqa: BLE001
                    log.warning("provider hangup failed: %s", error)

        asyncio.create_task(self._summarize())

    # ---------------------------------------------------------------- speech ---

    def _on_token(self, token: str, segmenter: SentenceSegmenter, cancel: asyncio.Event) -> None:
        if cancel.is_set() or self._ended:
            return
        self._current_reply += token
        self.publish("assistant.delta", {"text": token})
        self._hold += token
        if TRANSFER_MARKER in self._hold:
            self._transfer_requested = True
            self._hold = self._hold.replace(TRANSFER_MARKER, "")
        match = _PARTIAL_MARKER.search(self._hold)
        if match:
            self._consume(self._hold[: match.start()], segmenter, cancel)
            self._hold = self._hold[match.start() :]
        else:
            self._consume(self._hold, segmenter, cancel)
            self._hold = ""

    def _consume(self, text: str, segmenter: SentenceSegmenter, cancel: asyncio.Event) -> None:
        if not text:
            return
        for chunk in segmenter.push(text):
            self._spawn_speech(chunk, cancel)

    def _spawn_speech(self, text: str, cancel: asyncio.Event) -> None:
        task = asyncio.create_task(self._speak(text, cancel))
        self._speech_tasks.add(task)
        task.add_done_callback(self._speech_tasks.discard)

    async def _synthesize(self, text: str, cancel: asyncio.Event) -> array | None:
        clean = sanitize_for_speech(text)
        if not clean or cancel.is_set() or self._ended:
            return None
        try:
            result = await self.providers.tts.synthesize(clean, voice=self.agent.voice or None, cancel=cancel)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            log.warning("tts failed: %s", error)
            self.publish("tts.error", {"message": str(error)})
            if not self.providers.tts.live:
                return synthesize_speech_placeholder(clean, PIPELINE_RATE)
            return None
        if cancel.is_set() or self._ended:
            return None
        samples = result.audio
        if result.sample_rate != PIPELINE_RATE:
            samples = resample(samples, result.sample_rate, PIPELINE_RATE)
        self.store.add_event(
            self.id,
            "tts.chunk",
            {"chars": len(clean), "provider": result.provider, "latencyMs": result.latency_ms},
        )
        return samples

    async def _speak(self, text: str, cancel: asyncio.Event) -> None:
        samples = await self._synthesize(text, cancel)
        if samples is None or cancel.is_set() or self._ended:
            return
        self.player.enqueue(samples)
        if self._first_audio_ms is None and self._turn_started:
            self._first_audio_ms = now_ms() - self._turn_started
        self._spoken_chunks.append(sanitize_for_speech(text))
        self.publish("assistant.speaking", {"text": sanitize_for_speech(text)})

    async def _say(self, text: str, record: bool) -> None:
        clean = sanitize_for_speech(text)
        if not clean or self._ended:
            return
        cancel = asyncio.Event()
        started = now_ms()
        samples = await self._synthesize(clean, cancel)
        if samples is None:
            return
        self.player.enqueue(samples)
        # Record before playout so a barge-in cannot reorder the transcript.
        if record and not self._ended:
            latency = now_ms() - started
            self.store.add_message(self.id, "assistant", clean, latency)
            self.history.append({"role": "assistant", "content": clean})
            self.publish("assistant.message", {"text": clean, "latencyMs": latency, "scripted": True})
        await self.player.wait_idle()

    def _on_audio_start(self) -> None:
        if not self._ended and self.status != "speaking":
            self._set_status("speaking")

    # ------------------------------------------------------------------ turns ---

    def _on_transcript(self, event: TranscriptEvent) -> None:
        if self._ended:
            return
        text = _normalize(event.text)
        if not text:
            return
        if not event.is_final:
            self.publish("transcript.partial", {"text": text})
            return
        self.publish("transcript.final", {"text": text, "confidence": event.confidence})
        self._pending_user.append(text)
        self._clear_silence()
        if self._debounce_task:
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._flush_user_turn())

    async def _flush_user_turn(self) -> None:
        try:
            await asyncio.sleep(0.55)
        except asyncio.CancelledError:
            return
        if self._ended:
            return
        combined = _normalize(" ".join(self._pending_user))
        self._pending_user = []
        if combined:
            await self._handle_user_text(combined)

    async def _handle_user_text(self, text: str) -> None:
        if self._turn_in_flight:
            self._queued_user.append(text)
            return
        self.store.add_message(self.id, "user", text)
        self.history.append({"role": "user", "content": text})
        self.turns += 1
        self.publish("transcript.message", {"role": "user", "text": text})
        self.store.update_call(self.id, {"turns": self.turns})
        self._user_task = asyncio.create_task(self._respond())
        await self._user_task

    async def _respond(self) -> None:
        self._turn_in_flight = True
        cancel = asyncio.Event()
        self._cancel = cancel
        self._current_reply = ""
        self._hold = ""
        self._spoken_chunks = []
        self._reply_recorded = False
        self._transfer_requested = False
        self._turn_started = now_ms()
        self._first_audio_ms = None
        self._set_status("thinking")
        segmenter = SentenceSegmenter(min_chars=30, max_chars=220)

        try:
            await self.providers.llm.stream_reply(
                self.system_prompt,
                self.history,
                lambda token: self._on_token(token, segmenter, cancel),
                cancel=cancel,
            )
            if cancel.is_set() or self._ended:
                return
            if self._hold:
                leftover = self._hold.replace(TRANSFER_MARKER, "")
                self._hold = ""
                self._consume(re.sub(r"<+(TRANSFER)?>{0,2}$", "", leftover), segmenter, cancel)
            for chunk in segmenter.flush():
                self._spawn_speech(chunk, cancel)
            await asyncio.gather(*list(self._speech_tasks), return_exceptions=True)
            self._speech_tasks.clear()
            await self.player.wait_idle()
            if cancel.is_set() or self._ended:
                return
            self._finalize_reply()
            if self._transfer_requested:
                await self._perform_transfer()
                return
        except asyncio.CancelledError:
            return
        except Exception as error:  # noqa: BLE001
            log.exception("llm turn failed")
            self.publish("llm.error", {"message": str(error)})
            self._reply_recorded = True
            try:
                await self._say(self.agent.fallback, record=True)
            except Exception:
                pass
        finally:
            self._turn_in_flight = False
            self._cancel = None
            if not self._ended:
                if self.turns >= self.agent.max_turns:
                    await self._say("I will wrap up here so I do not keep you. Thanks for calling, and take care.", record=True)
                    await self.end("max_turns")
                    return
                self._set_status("listening")
                if self._queued_user:
                    nxt = _normalize(" ".join(self._queued_user))
                    self._queued_user = []
                    if nxt:
                        await self._handle_user_text(nxt)
                        return
                self._arm_silence()

    def _finalize_reply(self) -> None:
        if self._reply_recorded:
            return
        reply = _normalize(" ".join(self._spoken_chunks) if self._spoken_chunks else self._current_reply)
        if not reply:
            return
        self._reply_recorded = True
        self.store.add_message(self.id, "assistant", reply, self._first_audio_ms)
        self.history.append({"role": "assistant", "content": reply})
        self.publish(
            "assistant.message",
            {"text": reply, "latencyMs": self._first_audio_ms, "interrupted": bool(self._spoken_chunks and len(reply) < len(self._current_reply))},
        )

    def interrupt(self, reason: str) -> None:
        if self._ended:
            return
        if self._cancel:
            self._cancel.set()
        for task in list(self._speech_tasks):
            task.cancel()
        self._speech_tasks.clear()
        self.player.stop()
        if self._current_reply.strip() and not self._reply_recorded:
            self._reply_recorded = True
            partial = _normalize(" ".join(self._spoken_chunks))
            if partial:
                self.store.add_message(self.id, "assistant", partial)
                self.history.append({"role": "assistant", "content": partial})
                self.publish("assistant.message", {"text": partial, "interrupted": True})
        self._current_reply = ""
        self._spoken_chunks = []
        self.store.add_event(self.id, "assistant.interrupted", {"reason": reason})
        self.publish("assistant.interrupted", {"reason": reason})
        self._set_status("listening")

    # -------------------------------------------------------------- transfer ---

    async def _perform_transfer(self) -> None:
        telephony = self.providers.telephony
        number = self.agent.transfer_number
        if self._ended:
            return
        if not telephony.live or not self.provider_call_id or not number:
            self.publish("call.transfer_skipped", {"reason": "transfer_unavailable"})
            self._set_status("listening")
            self._arm_silence()
            return
        self.store.add_event(self.id, "call.transfer", {"to": number})
        self.publish("call.transfer", {"to": number})
        self._transfer_initiated = True
        try:
            await telephony.transfer(self.provider_call_id, number)
            await self.end("transferred")
        except Exception as error:  # noqa: BLE001
            log.warning("transfer failed: %s", error)
            self.store.add_event(self.id, "call.transfer_failed", {"message": str(error)})
            self.publish("call.transfer_failed", {"message": str(error)})
            await self._say("I am sorry, I could not connect you just now. I will have a teammate call you back shortly.", record=True)
            if not self._ended:
                self._set_status("listening")
                self._arm_silence()

    # ---------------------------------------------------------------- timing ---

    def _arm_silence(self) -> None:
        self._clear_silence()
        if self._ended:
            return
        self._silence_tasks = [
            asyncio.create_task(self._silence_reprompt()),
            asyncio.create_task(self._silence_hangup()),
        ]

    def _clear_silence(self) -> None:
        for task in self._silence_tasks:
            task.cancel()
        self._silence_tasks = []

    async def _silence_reprompt(self) -> None:
        try:
            await asyncio.sleep(self.config.agent.silence_reprompt_ms / 1000)
        except asyncio.CancelledError:
            return
        if self._ended or self.status != "listening":
            return
        await self._say(self.agent.fallback, record=True)
        if not self._ended:
            self._arm_silence()

    async def _silence_hangup(self) -> None:
        try:
            await asyncio.sleep(self.config.agent.hangup_after_silence_ms / 1000)
        except asyncio.CancelledError:
            return
        if not self._ended and self.status == "listening":
            await self.end("silence_timeout")

    async def _max_duration(self) -> None:
        try:
            await asyncio.sleep(self.config.agent.max_call_seconds)
        except asyncio.CancelledError:
            return
        await self.end("max_duration")

    # -------------------------------------------------------------------- stt ---

    async def _connect_stt(self) -> None:
        try:
            self._stt = await self.providers.stt.open(
                self._on_transcript, self._on_stt_error, language=self._stt_language
            )
            self._stt_reconnects = 0
        except Exception as error:  # noqa: BLE001
            log.warning("stt open failed: %s", error)
            self.publish("stt.error", {"message": str(error), "fatal": True})
            self._schedule_stt_reconnect()

    def _on_stt_error(self, error: Exception) -> None:
        log.warning("stt error: %s", error)
        self.publish("stt.error", {"message": str(error)})
        self._schedule_stt_reconnect()

    def _schedule_stt_reconnect(self) -> None:
        if self._ended or not self.providers.stt.live or self._stt_reconnects >= 3:
            return
        self._stt_reconnects += 1
        delay = 0.8 * self._stt_reconnects

        async def reconnect() -> None:
            await asyncio.sleep(delay)
            if self._ended:
                return
            if self._stt is not None:
                try:
                    await self._stt.close()
                except Exception:
                    pass
            await self._connect_stt()

        asyncio.create_task(reconnect())

    # -------------------------------------------------------------- summarize ---

    async def _summarize(self) -> None:
        try:
            messages = self.store.list_messages(self.id)
            if len(messages) < 2:
                return
            transcript = "\n".join(
                f"{'Caller' if message['role'] == 'user' else 'Agent'}: {message['content']}"
                for message in messages
                if message["role"] != "system"
            )
            parsed = await self.providers.llm.summarize(transcript)
            if not parsed:
                return
            self.store.update_call(
                self.id,
                {
                    "summary": parsed["summary"],
                    "outcome": parsed["outcome"],
                    "sentiment": parsed["sentiment"],
                    "action_items": __import__("json").dumps(parsed["action_items"]),
                },
            )
            self.publish("call.summary", parsed)
        except Exception as error:  # noqa: BLE001
            log.warning("summary failed: %s", error)

    # ------------------------------------------------------------------ utils ---

    def _set_status(self, status: str) -> None:
        if self.status == status:
            return
        self.status = status
        self.publish("call.status", {"status": status, "turns": self.turns})

    def publish(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        self.hub.publish({"type": event_type, "callId": self.id, "at": now_ms(), "data": data})

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "direction": self.direction,
            "transport": self.channel.kind,
            "turns": self.turns,
            "startedAt": self.started_at,
            "durationMs": now_ms() - self.started_at,
            "toNumber": None,
            "fromNumber": None,
            "contactName": None,
            "demo": self.providers.demo_mode,
            "lastMessage": self.history[-1]["content"] if self.history else None,
        }
