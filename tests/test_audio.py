from __future__ import annotations

import math
import struct

from app.audio import (
    decode_mulaw,
    decode_pcm16_base64,
    dbfs,
    encode_mulaw,
    encode_pcm16_base64,
    frame_audio,
    mulaw_byte_to_sample,
    pcm_sample_to_mulaw,
    resample,
    synthesize_tone,
)
from app.segmenter import SentenceSegmenter
from app.vad import EnergyVad


def test_mulaw_round_trip_within_tolerance():
    for index in range(0, 4000, 7):
        sample = int(-32768 + (index % 65536))
        decoded = mulaw_byte_to_sample(pcm_sample_to_mulaw(sample))
        tolerance = max(8, abs(sample) * 0.06 + 24)
        assert abs(decoded - sample) <= tolerance


def test_mulaw_decode_returns_normalised_floats():
    payload = __import__("base64").b64encode(bytes([0xFF, 0x7F, 0x00, 0x80])).decode()
    values = decode_mulaw(payload)
    assert len(values) == 4
    assert all(-1.0 <= value <= 1.0 for value in values)


def test_encode_mulaw_is_one_byte_per_sample():
    assert len(encode_mulaw([0.25] * 160)) == 160


def test_resample_doubles_samples_from_8k_to_16k():
    tone = synthesize_tone(100, 440, 8000)
    output = resample(tone, 8000, 16000)
    assert abs(len(output) - len(tone) * 2) <= 1


def test_resample_noop_at_same_rate():
    tone = synthesize_tone(50, 300, 16000)
    assert resample(tone, 16000, 16000) is tone


def test_pcm16_base64_round_trip():
    samples = [0.0, 0.5, -0.5, 0.999, -0.999]
    decoded = decode_pcm16_base64(encode_pcm16_base64(samples))
    assert len(decoded) == len(samples)
    for original, restored in zip(samples, decoded):
        assert abs(original - restored) < 0.001


def test_framing_pads_tail():
    frames = frame_audio([0.0] * 700, 320)
    assert len(frames) == 3
    assert all(len(frame) == 320 for frame in frames)


def test_dbfs_reports_silence_and_tone():
    assert dbfs([0.0] * 320) == -100.0
    level = dbfs(synthesize_tone(100, 440, 16000, 0.5))
    assert -12 < level < -3


def test_segmenter_splits_sentences():
    segmenter = SentenceSegmenter(min_chars=10)
    assert segmenter.push("Hello there. How are you today? I am fine!") == [
        "Hello there.",
        "How are you today?",
        "I am fine!",
    ]


def test_segmenter_keeps_abbreviations_and_decimals():
    segmenter = SentenceSegmenter(min_chars=5)
    assert segmenter.push("Mr. Smith paid 3.5 dollars. Next item is ready.") == [
        "Mr. Smith paid 3.5 dollars.",
        "Next item is ready.",
    ]


def test_segmenter_waits_for_sentence_end():
    segmenter = SentenceSegmenter(min_chars=10)
    assert segmenter.push("I can help") == []
    assert segmenter.push(" with that.") == ["I can help with that."]


def test_segmenter_hard_splits_long_text():
    segmenter = SentenceSegmenter(min_chars=20, max_chars=80)
    chunks = segmenter.push("word " * 60)
    assert len(chunks) > 1
    assert all(len(chunk) <= 82 for chunk in chunks)


def test_flush_emits_remainder_once():
    segmenter = SentenceSegmenter(min_chars=40)
    segmenter.push("Short tail without punctuation")
    assert segmenter.flush() == ["Short tail without punctuation"]
    assert segmenter.flush() == []


def test_vad_detects_speech_start_and_end():
    vad = EnergyVad(frame_ms=20, hangover_ms=100, min_speech_ms=60, speech_frames=2)
    silence = [0.0] * 320
    speech = [0.3] * 320
    started = ended = False
    for _ in range(20):
        assert vad.push(silence).speech_start is False
    for _ in range(10):
        decision = vad.push(speech)
        started = started or decision.speech_start
    for _ in range(20):
        decision = vad.push(silence)
        ended = ended or decision.speech_end
    assert started and ended


def test_vad_ignores_short_clicks():
    vad = EnergyVad(frame_ms=20, hangover_ms=60, min_speech_ms=200, speech_frames=1)
    silence = [0.0] * 320
    for _ in range(10):
        vad.push(silence)
    vad.push([0.5] * 320)
    assert not any(vad.push(silence).speech_end for _ in range(10))
