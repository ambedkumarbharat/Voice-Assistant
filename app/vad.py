"""Adaptive energy VAD with hysteresis (drives barge-in and silence timers)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .audio import dbfs


@dataclass
class VadDecision:
    speaking: bool
    speech_start: bool
    speech_end: bool
    level_db: float
    noise_floor_db: float


class EnergyVad:
    def __init__(
        self,
        floor_db: float = -48.0,
        margin_db: float = 9.0,
        speech_frames: int = 3,
        hangover_ms: int = 600,
        min_speech_ms: int = 180,
        frame_ms: int = 20,
    ) -> None:
        self.floor_db = floor_db
        self.margin_db = margin_db
        self.speech_frames = speech_frames
        self.hangover_ms = hangover_ms
        self.min_speech_ms = min_speech_ms
        self.frame_ms = frame_ms
        self.noise_floor_db = -60.0
        self._speech_run = 0
        self._silence_run = 0
        self._voiced_ms = 0
        self.speaking = False

    def reset(self) -> None:
        self._speech_run = 0
        self._silence_run = 0
        self._voiced_ms = 0
        self.speaking = False

    def push(self, frame: Sequence[float]) -> VadDecision:
        level_db = dbfs(frame)
        threshold = max(self.floor_db, self.noise_floor_db + self.margin_db)
        is_speech = level_db > threshold

        speech_start = False
        speech_end = False
        if is_speech:
            self._speech_run += 1
            self._silence_run = 0
            if not self.speaking and self._speech_run >= self.speech_frames:
                self.speaking = True
                self._voiced_ms = 0
                speech_start = True
            if self.speaking:
                self._voiced_ms += self.frame_ms
        else:
            self._speech_run = 0
            if self.speaking:
                self._silence_run += self.frame_ms
                if self._silence_run >= self.hangover_ms and self._voiced_ms >= self.min_speech_ms:
                    self.speaking = False
                    speech_end = True
                elif self._silence_run >= self.hangover_ms:
                    self.speaking = False
                    self._voiced_ms = 0
            if not self.speaking:
                target = min(self.floor_db, level_db)
                self.noise_floor_db = self.noise_floor_db * 0.98 + target * 0.02
                self.noise_floor_db = max(-70.0, self.noise_floor_db)

        return VadDecision(self.speaking, speech_start, speech_end, level_db, self.noise_floor_db)
