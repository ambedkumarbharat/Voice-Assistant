"""Provider registry and capability report."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Settings
from .llm import LlmService
from .stt import SttProvider, build_stt
from .telephony import NoopTelephony, TwilioTelephony, build_telephony
from .tts import TtsProvider, build_tts


@dataclass
class Providers:
    config: Settings
    llm: LlmService
    stt: SttProvider
    tts: TtsProvider
    telephony: TwilioTelephony | NoopTelephony

    @property
    def demo_mode(self) -> bool:
        return not (self.llm.live and self.stt.live and self.tts.live)

    def report(self) -> dict[str, Any]:
        cfg = self.config
        return {
            "llm": {"id": self.llm.id, "model": self.llm.model_name, "live": self.llm.live, "keyEnv": cfg.llm.key_env},
            "stt": {"id": self.stt.id, "live": self.stt.live, "keyEnv": cfg.stt.key_env},
            "tts": {"id": self.tts.id, "live": self.tts.live, "keyEnv": cfg.tts.key_env},
            "telephony": {
                "id": self.telephony.id,
                "live": self.telephony.live,
                "fromNumber": self.telephony.from_number if self.telephony.live else None,
            },
            "demoMode": self.demo_mode,
            "warnings": cfg.warnings,
        }


def build_providers(config: Settings) -> Providers:
    return Providers(
        config=config,
        llm=LlmService(config),
        stt=build_stt(config),
        tts=build_tts(config),
        telephony=build_telephony(config),
    )
