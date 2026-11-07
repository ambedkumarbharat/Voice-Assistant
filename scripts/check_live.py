"""Prove configured provider keys work.

    python scripts/check_live.py            # LLM + TTS + Twilio account check
    python scripts/check_live.py --llm      # cheapest

Never places a phone call: Twilio is verified with a read-only account lookup.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.providers import build_providers  # noqa: E402


async def main() -> int:
    config = get_settings()
    providers = build_providers(config)
    report = providers.report()
    llm_only = "--llm" in sys.argv

    print("Configured stack")
    print(f"  LLM        {report['llm']['id']} / {report['llm']['model']}{'' if report['llm']['live'] else '  (demo - no key)'}")
    print(f"  STT        {report['stt']['id']}{'' if report['stt']['live'] else '  (demo - no key)'}")
    print(f"  TTS        {report['tts']['id']}{'' if report['tts']['live'] else '  (demo - no key)'}")
    print(f"  Telephony  {('twilio ' + str(report['telephony']['fromNumber'])) if report['telephony']['live'] else 'browser only'}")
    print()

    failures = 0

    def fail(message: str) -> None:
        nonlocal failures
        failures += 1
        print(f"  FAIL {message}")

    if not providers.llm.live:
        fail("LLM has no key. Set XAI_API_KEY / XKIRO_API_KEY / OPENAI_API_KEY, or LLM_PROVIDER + LLM_API_KEY.")
    else:
        started = time.perf_counter()
        try:
            text = await providers.llm.complete(
                [
                    SystemMessage(content="You are a connectivity probe. Answer in three words or fewer."),
                    HumanMessage(content="Say READY."),
                ]
            )
            elapsed = int((time.perf_counter() - started) * 1000)
            print(f'  OK   LLM {providers.llm.id}/{providers.llm.model_name} responded in {elapsed}ms: "{text[:80]}"')
            if elapsed > 15000:
                print(f"  WARN first completion took {elapsed}ms; voice replies may feel slow.")
        except Exception as error:  # noqa: BLE001
            fail(f"LLM request failed: {error}")

    if not llm_only:
        if not providers.tts.live:
            print("  SKIP TTS has no key (set ELEVENLABS_API_KEY, OPENAI_API_KEY or DEEPGRAM_API_KEY).")
        else:
            started = time.perf_counter()
            try:
                result = await providers.tts.synthesize("Live voice check from VoxDesk.")
                seconds = len(result.audio) / result.sample_rate
                print(f"  OK   TTS {result.provider} synthesized {seconds:.2f}s in {int((time.perf_counter() - started) * 1000)}ms")
            except Exception as error:  # noqa: BLE001
                fail(f"TTS request failed: {error}")
        if not providers.stt.live:
            print("  SKIP STT has no key (set DEEPGRAM_API_KEY for streaming transcription).")
        else:
            print(f"  OK   STT {providers.stt.id} configured (streaming is exercised by a live call).")

        if providers.telephony.live:
            cfg = config.telephony
            async with httpx.AsyncClient(auth=(cfg.account_sid, cfg.auth_token), timeout=15.0) as client:
                try:
                    response = await client.get(f"{cfg.api_base}/2010-04-01/Accounts/{cfg.account_sid}.json")
                    if response.status_code >= 400:
                        fail(f"Twilio account check returned {response.status_code} (check SID and auth token)")
                    else:
                        payload = response.json()
                        print(
                            f'  OK   Twilio account "{payload.get("friendly_name", cfg.account_sid)}" is '
                            f'{payload.get("status", "active")} ({payload.get("type", "trial")})'
                        )
                        print(f'       Place a real call: curl -X POST {config.public_url}/api/calls -H "authorization: Bearer $API_KEYS" -H "content-type: application/json" -d \'{{"to":"+14155550123"}}\'')
                except Exception as error:  # noqa: BLE001
                    fail(f"Twilio account check failed: {error}")

    print()
    if failures:
        print(f"{failures} check(s) failed.")
        return 1
    print("All configured providers responded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
