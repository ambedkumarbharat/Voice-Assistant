# Changelog

## v1.0.1 — 2026-11-07

Latency, personalisation and interruption fixes found while reviewing a real
call flow.

### Added

- **Caller-name greeting.** `AGENT_GREETING` supports `{name}`. Outbound calls
  use the dial-list contact name; inbound calls resolve it from the contacts
  table by caller number. Unknown callers hear "Hi there". Works for both the
  TwiML `<Say>` path and the TTS path.
- **Fast first chunk.** `AGENT_FIRST_CHUNK_CHARS` (default 36) lets the first
  speakable chunk be emitted before a long sentence finishes, so TTS starts
  while the LangChain stream is still generating the rest of the answer.
- **Barge-in test** with real PCM audio: caller talks over the agent, the reply
  is interrupted, and the next question is answered normally.

### Fixed

- Browser test line blocked its WebSocket receive loop while injecting text
  (`await session.inject_text`), so microphone audio was not processed during an
  agent reply and barge-in never fired. Text injection is now a background task;
  Twilio media handling was already non-blocking.
- Silence reprompt/hangup timers are suppressed while the caller is speaking, so
  a long question can no longer trigger "Sorry, I didn't catch that".
- Transcript ordering: the scripted greeting is recorded before synthesis, so a
  caller who barges in immediately cannot push it behind their own turn.

## v1.0.0 — 2026-11-06

Initial Python release, ported from the Node/TypeScript implementation.

- FastAPI + LangChain LCEL voice agent (`ChatPromptTemplate | ChatOpenAI |
  StrOutputParser`), `JsonOutputParser`/Pydantic call summaries, streaming
  `DemoChatModel` for keyless demo mode.
- Twilio Programmable Voice + Media Streams: inbound, outbound, hangup, warm
  transfer, signed webhooks and signed media sockets.
- μ-law ↔ 16 kHz PCM pipeline, adaptive VAD, barge-in, sentence TTS chunking,
  paced 20 ms frames.
- Deepgram streaming or OpenAI batch STT; ElevenLabs, OpenAI or Deepgram TTS.
- SQLite WAL persistence, API-key auth, rate limiting, security headers,
  dashboard, Docker image, 44 tests and an end-to-end smoke script.
