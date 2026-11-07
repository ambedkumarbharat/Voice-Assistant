from __future__ import annotations

import pytest

from app.llm import DemoChatModel, LlmService
from app.prompts import TRANSFER_MARKER, build_system_prompt, load_agent_settings


@pytest.mark.asyncio
async def test_demo_model_streams_tokens(config):
    llm = LlmService(config)
    tokens: list[str] = []
    text = await llm.stream_reply(
        "You are a helpful agent.",
        [{"role": "user", "content": "How much does it cost?"}],
        tokens.append,
    )
    assert "$49" in text
    assert len(tokens) > 5
    assert "".join(tokens) == text


@pytest.mark.asyncio
async def test_demo_model_summary_is_valid_json(config):
    llm = LlmService(config)
    summary = await llm.summarize("Caller: I need pricing\nAgent: Plans start at $49.")
    assert summary is not None
    assert summary["outcome"] in {"resolved", "follow_up_needed", "escalated", "abandoned"}
    assert isinstance(summary["action_items"], list)


@pytest.mark.asyncio
async def test_cancel_stops_streaming(config):
    import asyncio

    llm = LlmService(config)
    cancel = asyncio.Event()
    tokens: list[str] = []

    def on_token(token: str) -> None:
        tokens.append(token)
        if len(tokens) == 2:
            cancel.set()

    await llm.stream_reply("You are a helpful agent.", [{"role": "user", "content": "Tell me about pricing"}], on_token, cancel=cancel)
    assert len(tokens) <= 3


def test_demo_model_type_and_identifying_params():
    model = DemoChatModel()
    assert model._llm_type == "voxdesk-demo"
    assert model._identifying_params["model_name"] == "voxdesk-demo-brain"


def test_prompt_contains_guardrails_and_knowledge(config):
    agent = load_agent_settings(__import__("app.db", fromlist=["Store"]).Store(":memory:"), config)
    agent.knowledge = "Support hours are 9 to 6 Eastern."
    prompt = build_system_prompt(agent, config)
    assert "Support hours are 9 to 6 Eastern." in prompt
    assert "Never say you are an AI language model" in prompt
    assert "Never request full card numbers" in prompt
    assert TRANSFER_MARKER not in prompt  # no Twilio line configured


def test_transfer_marker_requires_twilio(config):
    from dataclasses import replace

    from app.db import Store

    agent = load_agent_settings(Store(":memory:"), config)
    agent.transfer_number = "+14155550100"
    assert TRANSFER_MARKER not in build_system_prompt(agent, config)

    twilio_config = replace(
        config,
        telephony=replace(config.telephony, provider="twilio"),
    )
    prompt = build_system_prompt(agent, twilio_config)
    assert TRANSFER_MARKER in prompt
