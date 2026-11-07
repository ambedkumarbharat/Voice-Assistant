"""Configuration and provider resolution.

Mirrors the Node implementation's environment contract so the same `.env` works
for either stack. Everything is validated at boot; missing keys downgrade to an
explicitly-labelled demo provider instead of crashing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

LlmProviderId = Literal["xai", "xkiro", "openai", "openrouter", "groq", "custom", "mock"]
SttProviderId = Literal["deepgram", "openai", "mock"]
TtsProviderId = Literal["elevenlabs", "openai", "deepgram", "mock"]
TelephonyProviderId = Literal["twilio", "none"]


def _str(name: str, default: str = "") -> str:
    value = os.getenv(name, "")
    return value.strip() if value and value.strip() else default


def _int(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.getenv(name, "")
    if not raw or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def _float(name: str, default: float, minimum: float | None = None, maximum: float | None = None) -> float:
    raw = os.getenv(name, "")
    if not raw or not raw.strip():
        return default
    value = float(raw)
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: list[str] | None = None) -> list[str]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


def _first_env(names: list[str]) -> tuple[str | None, str]:
    for name in names:
        value = _str(name)
        if value:
            return name, value
    return None, ""


@dataclass(frozen=True)
class LlmConfig:
    provider: LlmProviderId
    base_url: str
    api_key: str
    model: str
    temperature: float
    max_tokens: int
    timeout_ms: int
    key_env: str


@dataclass(frozen=True)
class SttConfig:
    provider: SttProviderId
    api_key: str
    model: str
    language: str
    endpointing_ms: int
    key_env: str


@dataclass(frozen=True)
class TtsConfig:
    provider: TtsProviderId
    api_key: str
    model: str
    voice: str
    sample_rate: int
    key_env: str


@dataclass(frozen=True)
class TelephonyConfig:
    provider: TelephonyProviderId
    account_sid: str
    auth_token: str
    from_number: str
    public_url: str
    validate_signature: bool
    api_base: str


@dataclass(frozen=True)
class AgentConfig:
    name: str
    company: str
    language: str
    persona: str
    greeting: str
    fallback: str
    max_turns: int
    max_call_seconds: int
    barge_in: bool
    silence_reprompt_ms: int
    hangup_after_silence_ms: int


@dataclass
class Settings:
    env: str
    is_production: bool
    port: int
    host: str
    public_url: str
    log_level: str
    data_dir: Path
    db_path: Path
    cors_origins: list[str]
    api_keys: list[str]
    allow_anonymous_dashboard: bool
    rate_limit_max: int
    rate_limit_window_seconds: int
    trust_proxy: bool
    llm: LlmConfig
    stt: SttConfig
    tts: TtsConfig
    telephony: TelephonyConfig
    agent: AgentConfig
    warnings: list[str] = field(default_factory=list)

    @property
    def demo_mode(self) -> bool:
        return not (self.llm.api_key and self.stt.api_key and self.tts.api_key)

    @property
    def public_ws_base(self) -> str:
        base = self.public_url or f"http://localhost:{self.port}"
        if base.startswith("https://"):
            return "wss://" + base[len("https://") :]
        if base.startswith("http://"):
            return "ws://" + base[len("http://") :]
        return base


_LLM_PRESETS: dict[str, tuple[str, str]] = {
    "xai": ("https://api.x.ai/v1", "grok-4"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "openrouter": ("https://openrouter.ai/api/v1", "x-ai/grok-4"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
}


def _resolve_llm(warnings: list[str]) -> tuple[LlmProviderId, str, str, str, str]:
    explicit_provider = _str("LLM_PROVIDER").lower()
    explicit_base = _str("LLM_BASE_URL")
    explicit_model = _str("LLM_MODEL")
    key_name, explicit_key = _first_env(["LLM_API_KEY", "AI_API_KEY"])

    if explicit_provider and explicit_provider != "mock":
        if explicit_provider not in {"xai", "xkiro", "openai", "openrouter", "groq", "custom"}:
            raise ValueError(
                f"LLM_PROVIDER {explicit_provider!r} is not supported. "
                "Use xai | xkiro | openai | openrouter | groq | custom | mock."
            )
        preset = _LLM_PRESETS.get(explicit_provider)
        base_url = explicit_base or (preset[0] if preset else "")
        api_key = explicit_key or _str(f"{explicit_provider.upper()}_API_KEY")
        if not base_url:
            raise ValueError("LLM_BASE_URL is required when LLM_PROVIDER=custom or xkiro.")
        if not api_key:
            warnings.append(f'LLM provider "{explicit_provider}" selected but no API key found - the agent will run in demo mode.')
        return (
            explicit_provider,  # type: ignore[return-value]
            base_url,
            api_key,
            explicit_model or (preset[1] if preset else "grok-4"),
            key_name or f"{explicit_provider.upper()}_API_KEY",
        )

    candidates: list[tuple[list[str], LlmProviderId, str, str]] = [
        (["XKIRO_API_KEY"], "xkiro", _str("XKIRO_BASE_URL", "https://api.xkiro.ai/v1"), "grok-4"),
        (["XAI_API_KEY", "GROK_API_KEY"], "xai", "https://api.x.ai/v1", "grok-4"),
        (["OPENAI_API_KEY"], "openai", "https://api.openai.com/v1", "gpt-4o-mini"),
        (["OPENROUTER_API_KEY"], "openrouter", "https://openrouter.ai/api/v1", "x-ai/grok-4"),
        (["GROQ_API_KEY"], "groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    ]
    for env_names, provider, base_url, model in candidates:
        found_name, found_value = _first_env(env_names)
        if found_value:
            return provider, explicit_base or base_url, found_value, explicit_model or model, found_name or env_names[0]

    warnings.append("No LLM API key found (XAI_API_KEY / XKIRO_API_KEY / OPENAI_API_KEY ...). Running with the deterministic demo brain.")
    return "mock", "", "", "mock", ""


def _resolve_stt(warnings: list[str]) -> tuple[SttProviderId, str, str, str]:
    explicit = _str("STT_PROVIDER").lower()
    deepgram_name, deepgram_key = _first_env(["DEEPGRAM_API_KEY"])
    openai_name, openai_key = _first_env(["OPENAI_API_KEY"])
    if explicit == "mock":
        return "mock", "", "", ""
    if explicit == "deepgram" or (not explicit and deepgram_key):
        if not deepgram_key:
            warnings.append("STT_PROVIDER=deepgram but DEEPGRAM_API_KEY is missing - falling back to demo STT.")
            return "mock", "", "", ""
        return "deepgram", deepgram_key, _str("STT_MODEL", "nova-3"), deepgram_name or "DEEPGRAM_API_KEY"
    if explicit == "openai" or (not explicit and openai_key):
        if not openai_key:
            warnings.append("STT_PROVIDER=openai but OPENAI_API_KEY is missing - falling back to demo STT.")
            return "mock", "", "", ""
        return "openai", openai_key, _str("STT_MODEL", "gpt-4o-mini-transcribe"), openai_name or "OPENAI_API_KEY"
    warnings.append("No speech-to-text key found (DEEPGRAM_API_KEY or OPENAI_API_KEY). Text input still works.")
    return "mock", "", "", ""


def _resolve_tts(warnings: list[str]) -> tuple[TtsProviderId, str, str, str, str]:
    explicit = _str("TTS_PROVIDER").lower()
    eleven_name, eleven_key = _first_env(["ELEVENLABS_API_KEY", "ELEVEN_API_KEY"])
    openai_name, openai_key = _first_env(["OPENAI_API_KEY"])
    deepgram_name, deepgram_key = _first_env(["DEEPGRAM_API_KEY"])
    if explicit == "mock":
        return "mock", "", "", "", ""
    if explicit == "elevenlabs" or (not explicit and eleven_key):
        if not eleven_key:
            warnings.append("TTS_PROVIDER=elevenlabs but ELEVENLABS_API_KEY is missing - falling back to demo voice.")
            return "mock", "", "", "", ""
        voice = _str("TTS_VOICE", "21m00Tcm4TlvDq8ikWAM")
        return "elevenlabs", eleven_key, _str("TTS_MODEL", "eleven_flash_v2_5"), voice, eleven_name or "ELEVENLABS_API_KEY"
    if explicit == "openai" or (not explicit and openai_key):
        if not openai_key:
            warnings.append("TTS_PROVIDER=openai but OPENAI_API_KEY is missing - falling back to demo voice.")
            return "mock", "", "", "", ""
        voice = _str("TTS_VOICE", "alloy")
        return "openai", openai_key, _str("TTS_MODEL", "gpt-4o-mini-tts"), voice, openai_name or "OPENAI_API_KEY"
    if not explicit and deepgram_key:
        voice = _str("TTS_VOICE", "aura-2-thalia-en")
        return "deepgram", deepgram_key, voice, voice, deepgram_name or "DEEPGRAM_API_KEY"
    warnings.append("No text-to-speech key found (ELEVENLABS_API_KEY / OPENAI_API_KEY / DEEPGRAM_API_KEY). The agent will use a demo voice.")
    return "mock", "", "", "", ""


def load_settings(**overrides: object) -> Settings:
    warnings: list[str] = []
    env = _str("NODE_ENV", "development")
    if env not in {"development", "production", "test"}:
        env = "development"
    is_production = env == "production"

    data_dir = Path(_str("DATA_DIR", str(Path.cwd() / "data"))).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = Path(_str("DB_PATH", str(data_dir / "voxdesk.db"))).resolve()

    llm_provider, llm_base, llm_key, llm_model, llm_key_env = _resolve_llm(warnings)
    stt_provider, stt_key, stt_model, stt_key_env = _resolve_stt(warnings)
    tts_provider, tts_key, tts_model, tts_voice, tts_key_env = _resolve_tts(warnings)

    port = _int("PORT", 8080, 1, 65535)
    public_url = _str("PUBLIC_URL", f"http://localhost:{port}").rstrip("/")
    api_keys = _list("API_KEYS")

    telephony_provider: TelephonyProviderId = "twilio" if _str("TELEPHONY_PROVIDER", "none").lower() == "twilio" else "none"
    telephony = TelephonyConfig(
        provider=telephony_provider,
        account_sid=_str("TWILIO_ACCOUNT_SID"),
        auth_token=_str("TWILIO_AUTH_TOKEN"),
        from_number=_str("TWILIO_PHONE_NUMBER"),
        public_url=_str("PUBLIC_URL"),
        validate_signature=_bool("TWILIO_VALIDATE_SIGNATURE", True),
        api_base=_str("TWILIO_API_BASE", "https://api.twilio.com"),
    )
    if telephony.provider == "twilio" and not (telephony.account_sid and telephony.auth_token and telephony.from_number):
        warnings.append("TELEPHONY_PROVIDER=twilio but credentials are incomplete - outbound dialing disabled.")
        telephony = replace(telephony, provider="none")

    if is_production and not api_keys:
        raise ValueError(
            'API_KEYS must be set in production (comma separated). Generate one with: python -c "import secrets;print(secrets.token_urlsafe(24))"'
        )
    if not is_production and not api_keys:
        warnings.append("API_KEYS is empty: the dashboard and API run unauthenticated for local development.")

    settings = Settings(
        env=env,
        is_production=is_production,
        port=port,
        host=_str("HOST", "0.0.0.0"),
        public_url=public_url,
        log_level=_str("LOG_LEVEL", "info" if is_production else "debug"),
        data_dir=data_dir,
        db_path=db_path,
        cors_origins=_list("CORS_ORIGINS", ["*"]),
        api_keys=api_keys,
        allow_anonymous_dashboard=_bool("ALLOW_ANONYMOUS_DASHBOARD", not is_production and not api_keys),
        rate_limit_max=_int("RATE_LIMIT_MAX", 240, 1),
        rate_limit_window_seconds=_int("RATE_LIMIT_WINDOW_SECONDS", 60, 1),
        trust_proxy=_bool("TRUST_PROXY", True),
        llm=LlmConfig(
            provider=llm_provider,
            base_url=llm_base,
            api_key=llm_key,
            model=_str("LLM_MODEL", llm_model),
            temperature=_float("LLM_TEMPERATURE", 0.6, 0.0, 2.0),
            max_tokens=_int("LLM_MAX_TOKENS", 320, 16, 8192),
            timeout_ms=_int("LLM_TIMEOUT_MS", 20000, 1000, 120000),
            key_env=llm_key_env,
        ),
        stt=SttConfig(
            provider=stt_provider,
            api_key=stt_key,
            model=stt_model,
            language=_str("STT_LANGUAGE", "en"),
            endpointing_ms=_int("STT_ENDPOINTING_MS", 320, 100, 5000),
            key_env=stt_key_env,
        ),
        tts=TtsConfig(
            provider=tts_provider,
            api_key=tts_key,
            model=tts_model,
            voice=tts_voice,
            sample_rate=_int("TTS_SAMPLE_RATE", 24000, 8000, 48000),
            key_env=tts_key_env,
        ),
        telephony=telephony,
        agent=AgentConfig(
            name=_str("AGENT_NAME", "Ava"),
            company=_str("AGENT_COMPANY", "Northwind Labs"),
            language=_str("AGENT_LANGUAGE", "en-US"),
            persona=_str(
                "AGENT_PERSONA",
                "You are a warm, efficient voice assistant who handles inbound customer calls. "
                "You speak in short, natural sentences and confirm important details before moving on.",
            ),
            greeting=_str("AGENT_GREETING", "Hi, thanks for calling. This is Ava - how can I help you today?"),
            fallback=_str("AGENT_FALLBACK", "Sorry, I didn't catch that. Could you say that again?"),
            max_turns=_int("AGENT_MAX_TURNS", 40, 1, 500),
            max_call_seconds=_int("AGENT_MAX_CALL_SECONDS", 900, 30, 7200),
            barge_in=_bool("AGENT_BARGE_IN", True),
            silence_reprompt_ms=_int("AGENT_SILENCE_REPROMPT_MS", 7000, 1000),
            hangup_after_silence_ms=_int("AGENT_HANGUP_SILENCE_MS", 20000, 5000),
        ),
        warnings=warnings,
    )
    if telephony.provider == "twilio" and not settings.public_url.startswith("https://"):
        warnings.append("PUBLIC_URL must be a public https URL for Twilio webhooks and Media Streams.")
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings
