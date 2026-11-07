"""LangChain chat models: real OpenAI-compatible gateways and the demo brain."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncIterator, Iterator, Sequence

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI

from .config import Settings
from .prompts import SUMMARY_SYSTEM, build_history, summary_parser, summary_prompt, voice_prompt


def _last_human(messages: Sequence[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""


def _system_text(messages: Sequence[BaseMessage]) -> str:
    for message in messages:
        if isinstance(message, SystemMessage):
            return str(message.content)
    return ""


class DemoChatModel(BaseChatModel):
    """Deterministic, streaming LangChain chat model for demo mode.

    It is a real BaseChatModel subclass, so every chain, parser and callback in
    the app works identically with or without provider keys.
    """

    model_name: str = "voxdesk-demo-brain"
    token_delay: float = 0.015
    company_name: str = "the team"

    @property
    def _llm_type(self) -> str:
        return "voxdesk-demo"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name}

    def _reply(self, messages: Sequence[BaseMessage]) -> str:
        system = _system_text(messages).lower()
        if "health probe" in system:
            return "READY"
        if "return only a json object" in system:
            return self._json_reply(messages)

        text = _last_human(messages).lower()
        turns = sum(1 for message in messages if isinstance(message, AIMessage))
        company = self.company_name

        def has(*needles: str) -> bool:
            return any(needle in text for needle in needles)

        if has("human", "real person", "representative", "agent", "supervisor", "manager"):
            return (
                f"Of course. I can bring a teammate from {company} into this call and share the summary of what we discussed. "
                "Would you like me to transfer you now, or take a callback number instead?"
            )
        if has("price", "pricing", "cost", "how much", "budget", "quote"):
            return (
                "Our plans start at $49 per month for the starter tier, and the growth tier is $149 per month with unlimited seats. "
                "Most teams your size land on growth. Would you like me to walk through what is included, or set up a short demo?"
            )
        if has("appointment", "book", "schedule", "demo", "meeting", "tomorrow", "monday", "tuesday", "wednesday", "thursday", "friday"):
            return (
                "Happy to book that. I have openings tomorrow at 10 a.m. and 2 p.m., and Friday at 11 a.m. "
                "Which of those works best for you? I will send a calendar invite and a confirmation to your email."
            )
        if has("hour", "open", "available", "weekend"):
            return (
                "We are open Monday to Friday, 9 a.m. to 6 p.m. Eastern, and there is always someone on call for urgent issues. "
                "Is there a particular time you were hoping to reach us?"
            )
        if has("refund", "cancel", "billing", "invoice", "charge"):
            return (
                f"I can take care of that. I will flag the request to our billing team at {company} and you will get a confirmation "
                "email within one business day. Before I do, could you confirm the email address on the account?"
            )
        if has("integrat", "api", "webhook", "crm", "salesforce", "hubspot"):
            return (
                "Yes, we have a REST API and native CRM sync for Salesforce and HubSpot. Most integrations take under an hour. "
                "Which system are you using today?"
            )
        if has("problem", "issue", "broken", "not working", "error", "bug", "down"):
            return (
                "I am sorry you are hitting that. Let us get it sorted: is the issue affecting one user or the whole team, "
                "and did it start today?"
            )
        if has("thank", "thanks", "bye", "goodbye", "that is all", "that's all"):
            return "Thank you for calling. I have your notes here, and you will get a summary by email. Have a great rest of your day."
        if turns <= 1:
            return "Got it, thanks for sharing that. So I point you to the right person: are you looking at this for yourself, or for a team?"
        if turns == 2:
            return "That helps. I will note that down. What would a good outcome from this call look like for you?"
        return f"Understood. I have captured that for the {company} team. Is there anything else you would like me to add before I wrap this up?"

    def _json_reply(self, messages: Sequence[BaseMessage]) -> str:
        transcript = "\n".join(str(message.content) for message in messages)
        user_turns = sum(1 for message in messages if isinstance(message, HumanMessage))
        outcome = "escalated" if re.search(r"human|transfer|supervisor", transcript, re.IGNORECASE) else "resolved"
        return json.dumps(
            {
                "summary": (
                    f"The caller spoke with the assistant for {user_turns} "
                    f"{'turn' if user_turns == 1 else 'turns'}. Key topics were captured and follow-up actions noted."
                ),
                "intent": "general_inquiry",
                "outcome": outcome,
                "sentiment": "neutral",
                "actionItems": ["Send a follow-up email summarising the call"],
            }
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        text = self._reply(messages)
        message = AIMessage(content=text)
        return ChatResult(
            generations=[ChatGeneration(message=message)],
            llm_output={
                "token_usage": {"completion_tokens": len(text.split()), "prompt_tokens": 0, "total_tokens": len(text.split())},
                "model_name": self.model_name,
            },
        )

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        text = self._reply(messages)
        for piece in re.findall(r"\S+\s*", text):
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=piece))
            if run_manager:
                await run_manager.on_llm_new_token(piece, chunk=chunk)
            yield chunk
            if self.token_delay:
                await asyncio.sleep(self.token_delay)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        text = self._reply(messages)
        for piece in re.findall(r"\S+\s*", text):
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=piece))
            if run_manager:
                run_manager.on_llm_new_token(piece, chunk=chunk)
            yield chunk


class LlmService:
    def __init__(self, config: Settings) -> None:
        self.config = config
        self.live = bool(config.llm.api_key) and config.llm.provider != "mock"
        self.model: BaseChatModel = self._build_model()
        self.voice_chain = voice_prompt() | self.model | StrOutputParser()
        parser = summary_parser()
        summary_system = SUMMARY_SYSTEM.replace("{format_instructions}", parser.get_format_instructions())
        self.summary_chain = (
            summary_prompt().partial(summary_system=summary_system) | self.model | parser
        )
        self.id = config.llm.provider if self.live else "mock"
        self.model_name = config.llm.model if self.live else self.model.model_name  # type: ignore[attr-defined]

    def _build_model(self) -> BaseChatModel:
        cfg = self.config.llm
        if not cfg.api_key or cfg.provider == "mock":
            return DemoChatModel(company_name=self.config.agent.company)
        headers = {"HTTP-Referer": self.config.public_url, "X-Title": "VoxDesk"} if cfg.provider == "openrouter" else None
        return ChatOpenAI(
            model=cfg.model,
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            timeout=cfg.timeout_ms / 1000,
            streaming=True,
            default_headers=headers,
            max_retries=1,
        )

    async def stream_reply(
        self,
        system_prompt: str,
        history: list[dict[str, str]],
        on_token: Any,
        cancel: asyncio.Event | None = None,
    ) -> str:
        parts: list[str] = []
        async for chunk in self.voice_chain.astream(
            {"system_prompt": system_prompt, "history": build_history(history)}
        ):
            if cancel is not None and cancel.is_set():
                break
            if not chunk:
                continue
            parts.append(chunk)
            on_token(chunk)
        return "".join(parts).strip()

    async def complete(self, messages: list[BaseMessage], *, max_tokens: int = 24, temperature: float = 0.0) -> str:
        bound = self.model.bind(max_tokens=max_tokens, temperature=temperature)
        result = await bound.ainvoke(messages)
        return str(result.content).strip()

    async def summarize(self, transcript: str) -> dict[str, Any] | None:
        try:
            parsed = await self.summary_chain.ainvoke({"transcript": transcript[-12000:]})
            if hasattr(parsed, "model_dump"):
                data = parsed.model_dump()
            else:
                data = dict(parsed)
            summary = data.get("summary")
            if not summary:
                return None
            return {
                "summary": str(summary),
                "intent": str(data.get("intent") or "general_inquiry"),
                "outcome": str(data.get("outcome") or "follow_up_needed"),
                "sentiment": str(data.get("sentiment") or "neutral"),
                "action_items": [str(item) for item in (data.get("action_items") or data.get("actionItems") or [])],
            }
        except Exception:
            return None
