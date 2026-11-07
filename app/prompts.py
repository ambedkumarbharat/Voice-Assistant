"""Prompts, runtime agent settings and the post-call summary chain."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from pydantic import BaseModel, Field

from .config import Settings

SETTINGS_KEY = "agent"
TRANSFER_MARKER = "<<TRANSFER>>"


@dataclass
class AgentSettings:
    name: str
    company: str
    persona: str
    greeting: str
    fallback: str
    language: str
    voice: str
    knowledge: str
    transfer_number: str
    max_turns: int
    barge_in: bool


class CallSummary(BaseModel):
    summary: str = Field(description="Two to three factual sentences about the call")
    intent: str = Field(default="general_inquiry", description="short snake_case intent label")
    outcome: Literal["resolved", "follow_up_needed", "escalated", "abandoned"] = "follow_up_needed"
    sentiment: Literal["positive", "neutral", "negative"] = "neutral"
    action_items: list[str] = Field(default_factory=list, description="short imperative tasks")


def load_agent_settings(store: Any, config: Settings) -> AgentSettings:
    stored = store.get_setting(SETTINGS_KEY, {}) or {}
    if not isinstance(stored, dict):
        stored = {}

    def pick(camel: str, snake: str, default: Any) -> Any:
        value = stored.get(camel, stored.get(snake))
        return default if value is None else value

    agent = config.agent
    return AgentSettings(
        name=pick("name", "name", agent.name),
        company=pick("company", "company", agent.company),
        persona=pick("persona", "persona", agent.persona),
        greeting=pick("greeting", "greeting", agent.greeting),
        fallback=pick("fallback", "fallback", agent.fallback),
        language=pick("language", "language", agent.language),
        voice=pick("voice", "voice", config.tts.voice),
        knowledge=pick("knowledge", "knowledge", ""),
        transfer_number=pick("transferNumber", "transfer_number", ""),
        max_turns=int(pick("maxTurns", "max_turns", agent.max_turns)),
        barge_in=bool(pick("bargeIn", "barge_in", agent.barge_in)),
    )


VOICE_RULES = """Voice rules - these override any other style instruction:
- You are on a live phone call. Write exactly what you would say out loud, nothing else.
- Keep every turn to 1-3 short sentences (roughly 45 words). Never monologue.
- Never use markdown, bullet points, emoji, asterisks, code blocks, URLs or stage directions.
- Spell numbers, emails and names the way a person would say them. Read phone numbers in groups.
- Ask at most one question per turn. End most turns with a short question or a clear next step.
- If the caller interrupts you, stop immediately and respond to what they said.
- Never say you are an AI language model. If asked directly, say you are a virtual assistant for the company and offer a human transfer.
- If you did not understand, ask a short clarifying question instead of guessing."""

GUARDRAILS = """Guardrails:
- Only state facts that appear in the knowledge base or earlier in this call. Never invent prices, policies, availability, order numbers or timelines.
- If the caller asks for something you cannot verify, say you will have the team confirm and take their details.
- Collect name, contact number and reason when the caller needs a follow-up, one item at a time.
- Escalate to a human when the caller asks for one, is upset, or raises billing disputes, legal matters, medical emergencies or account security.
- Never request full card numbers, passwords or one-time codes. If offered, tell the caller not to share them.
- Keep the conversation compliant: no pressure, no repeated upselling, honour opt-out requests immediately."""


def build_system_prompt(agent: AgentSettings, config: Settings) -> str:
    from datetime import datetime

    now = datetime.now().astimezone()
    date_line = now.strftime("%A, %B %d, %Y at %I:%M %p %Z")
    if agent.transfer_number and config.telephony.provider == "twilio":
        transfer_line = (
            f"Human transfer number on file: {agent.transfer_number}. When the caller needs a human, "
            f"say you are connecting them now and append the exact marker {TRANSFER_MARKER} at the very end "
            "of that reply. Never say the marker out loud and never use it for anything else."
        )
    else:
        transfer_line = "No live transfer is available on this line: never promise a transfer. Offer a callback and record the escalation instead."

    parts = [
        f"You are {agent.name}, the voice assistant for {agent.company}.",
        agent.persona,
        "",
        f"Current date and time: {date_line}.",
        f"Language: {agent.language}.",
        transfer_line,
        "",
        VOICE_RULES,
        "",
        GUARDRAILS,
        "",
        (
            f"Knowledge base (authoritative, use it verbatim where relevant):\n{agent.knowledge}"
            if agent.knowledge.strip()
            else "Knowledge base: none supplied. Keep answers general and offer to have the team follow up with specifics."
        ),
        "",
        "Call goals: understand why the caller is calling, help them completely, confirm any details you captured, "
        "then close politely. If the caller says goodbye or the issue is resolved, say a short closing line that "
        "makes clear the call is ending.",
    ]
    if config.telephony.provider == "twilio":
        parts.append("This call is carried over a real telephone line; audio quality may vary.")
    return "\n".join(parts)


def voice_prompt() -> ChatPromptTemplate:
    """System prompt is a variable so braces in a knowledge base are never parsed."""
    return ChatPromptTemplate.from_messages(
        [("system", "{system_prompt}"), MessagesPlaceholder("history")]
    )


SUMMARY_SYSTEM = """You analyse a finished phone call between an AI voice assistant and a caller.
Return ONLY a JSON object with this exact shape:
{
  "summary": "2-3 sentence factual summary of what the caller wanted and what was agreed",
  "intent": "short snake_case label, e.g. pricing_question",
  "outcome": "resolved" | "follow_up_needed" | "escalated" | "abandoned",
  "sentiment": "positive" | "neutral" | "negative",
  "actionItems": ["short imperative task"]
}
Rules: do not invent facts, keep the summary under 60 words, and use an empty actionItems array when nothing is owed.

{format_instructions}"""


def summary_prompt() -> ChatPromptTemplate:
    # The system text is a variable: JSON braces inside it must not be parsed as
    # template placeholders.
    return ChatPromptTemplate.from_messages(
        [("system", "{summary_system}"), ("user", "Transcript:\n{transcript}")]
    )


def summary_parser() -> JsonOutputParser:
    return JsonOutputParser(pydantic_object=CallSummary)


def render_greeting(greeting: str, name: str | None) -> str:
    """Fill `{name}` with the caller's first name, or `there` when unknown.

    One template serves inbound calls (contact lookup by number) and outbound
    calls (dial-list name) without duplicating greeting strings.
    """
    if "{name}" not in greeting:
        return greeting
    cleaned = (name or "").strip()
    first = cleaned.split(" ")[0] if cleaned else ""
    return greeting.replace("{name}", first or "there")


def build_history(history: list[dict[str, str]], budget_chars: int = 12000) -> list[BaseMessage]:
    """Keep the newest turns inside a predictable prompt budget."""
    messages: list[BaseMessage] = []
    used = 0
    for item in reversed(history):
        role = item.get("role")
        content = item.get("content", "")
        if role not in {"user", "assistant"} or not content:
            continue
        cost = len(content) + 12
        if used + cost > budget_chars and len(messages) >= 4:
            break
        messages.append(HumanMessage(content=content) if role == "user" else AIMessage(content=content))
        used += cost
    messages.reverse()
    return messages


_SPEECH_REPLACEMENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\*\*(.*?)\*\*"), r"\1"),
    (re.compile(r"[*_`#>]"), ""),
    (re.compile(r"\[(.*?)\]\(.*?\)"), r"\1"),
    (re.compile(r"https?://\S+", re.IGNORECASE), "the link I will send you"),
    (re.compile(r"\s+"), " "),
]


def sanitize_for_speech(text: str) -> str:
    out = text
    for pattern, replacement in _SPEECH_REPLACEMENTS:
        out = pattern.sub(replacement, out)
    return out.strip()
