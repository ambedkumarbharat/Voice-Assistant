"""G.711 mu-law, resampling and PCM helpers.

Twilio Media Streams deliver 8 kHz mono mu-law frames (base64, 20 ms). Speech
providers expect 16 kHz linear PCM. The dashboard streams 16 kHz PCM16. All
audio is normalised to float32 [-1, 1] at PIPELINE_RATE.
"""

from __future__ import annotations

import base64
import math
import struct
from array import array

TELEPHONY_RATE = 8000
PIPELINE_RATE = 16000
FRAME_MS = 20
TELEPHONY_FRAME_SAMPLES = TELEPHONY_RATE * FRAME_MS // 1000  # 160
PIPELINE_FRAME_SAMPLES = PIPELINE_RATE * FRAME_MS // 1000  # 320


def _build_decode_table() -> list[int]:
    table: list[int] = []
    for i in range(256):
        u = ~i & 0xFF
        sign = u & 0x80
        exponent = (u >> 4) & 0x07
        mantissa = u & 0x0F
        sample = ((mantissa << 3) + 0x84) << exponent
        sample -= 0x84
        table.append(-sample if sign else sample)
    return table


_ULAW_TABLE = _build_decode_table()
_ULAW_FLOAT = [value / 32768.0 for value in _ULAW_TABLE]


def mulaw_byte_to_sample(byte: int) -> int:
    return _ULAW_TABLE[byte & 0xFF]


def pcm_sample_to_mulaw(sample: int) -> int:
    BIAS = 0x84
    CLIP = 32635
    sign = (sample >> 8) & 0x80
    if sign:
        sample = -sample
    if sample > CLIP:
        sample = CLIP
    sample += BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and (sample & mask) == 0:
        mask >>= 1
        exponent -= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def decode_mulaw(base64_payload: str) -> array:
    """Base64 mu-law -> float array at 8 kHz."""
    raw = base64.b64decode(base64_payload)
    return array("f", (_ULAW_FLOAT[byte] for byte in raw))


def encode_mulaw(samples: array | list[float]) -> bytes:
    out = bytearray(len(samples))
    for index, value in enumerate(samples):
        clamped = max(-1.0, min(1.0, value))
        out[index] = pcm_sample_to_mulaw(int(round(clamped * 32767)))
    return bytes(out)


def resample(samples: array | list[float], from_rate: int, to_rate: int) -> array:
    if from_rate == to_rate or len(samples) == 0:
        return samples if isinstance(samples, array) else array("f", samples)
    ratio = to_rate / from_rate
    out_length = max(1, int(round(len(samples) * ratio)))
    step = (len(samples) - 1) / max(1, out_length - 1)
    out = array("f", [0.0]) * out_length
    for i in range(out_length):
        position = i * step
        index = int(position)
        nxt = min(len(samples) - 1, index + 1)
        fraction = position - index
        out[i] = samples[index] * (1 - fraction) + samples[nxt] * fraction
    return out


def decode_pcm16_base64(base64_payload: str) -> array:
    raw = base64.b64decode(base64_payload)
    count = len(raw) // 2
    values = struct.unpack(f"<{count}h", raw[: count * 2]) if count else ()
    return array("f", (value / 32768.0 for value in values))


def encode_pcm16_base64(samples: array | list[float]) -> str:
    pcm = bytearray(len(samples) * 2)
    for index, value in enumerate(samples):
        clamped = max(-1.0, min(1.0, value))
        struct.pack_into("<h", pcm, index * 2, int(round(clamped * 32767)))
    return base64.b64encode(bytes(pcm)).decode("ascii")


def pcm16_bytes(samples: array | list[float]) -> bytes:
    return base64.b64decode(encode_pcm16_base64(samples))


def frame_audio(samples: array, frame_size: int) -> list[array]:
    frames: list[array] = []
    for offset in range(0, len(samples), frame_size):
        chunk = samples[offset : offset + frame_size]
        if len(chunk) == frame_size:
            frames.append(chunk)
        else:
            padded = array("f", chunk)
            padded.extend([0.0] * (frame_size - len(chunk)))
            frames.append(padded)
    return frames


def rms(samples: array | list[float]) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(value * value for value in samples) / len(samples))


def dbfs(samples: array | list[float]) -> float:
    value = rms(samples)
    if value <= 1e-7:
        return -100.0
    return max(-100.0, 20 * math.log10(value))


def synthesize_tone(duration_ms: int, frequency: float = 440.0, sample_rate: int = PIPELINE_RATE, gain: float = 0.12) -> array:
    length = max(1, round(duration_ms / 1000 * sample_rate))
    return array("f", (math.sin(2 * math.pi * frequency * i / sample_rate) * gain for i in range(length)))


def synthesize_speech_placeholder(text: str, sample_rate: int = PIPELINE_RATE) -> array:
    """Audible speech-shaped placeholder for demo mode (no TTS key)."""
    words = max(1, len(text.split()))
    duration_ms = min(30000, max(600, words * 260))
    length = round(duration_ms / 1000 * sample_rate)
    out = array("f", [0.0]) * length
    phase = 0.0
    for i in range(length):
        t = i / sample_rate
        syllable = math.sin(2 * math.pi * 3.1 * t) * 0.5 + 0.5
        phase += 2 * math.pi * (120 + 60 * syllable) / sample_rate
        carrier = math.sin(phase) * 0.5 + math.sin(phase * 2.01) * 0.25 + math.sin(phase * 3.02) * 0.12
        attack = min(1.0, t * 12)
        release = min(1.0, max(0.0, (length - i) / (sample_rate * 0.25)))
        out[i] = carrier * attack * release * 0.18 * (0.55 + 0.45 * syllable)
    return out
