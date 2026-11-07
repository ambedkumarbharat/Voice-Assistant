"""End-to-end smoke test against a real uvicorn server and WebSocket.

    python scripts/smoke.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
import websockets  # noqa: E402

from app.api import create_app, media_signature  # noqa: E402
from app.config import TelephonyConfig, load_settings  # noqa: E402
from app.db import Store  # noqa: E402
from app.providers import build_providers  # noqa: E402

TOKEN = "smoke-key"


async def run(base: str, ws_base: str) -> list[str]:
    results: list[str] = []
    async with httpx.AsyncClient(timeout=10) as client:
        health = await client.get(f"{base}/api/health")
        assert health.status_code == 200, health.text
        results.append("health ok")

        unauth = await client.get(f"{base}/api/calls")
        assert unauth.status_code == 401, unauth.status_code
        results.append("auth enforced")

    audio_frames = 0
    assistant_messages: list[str] = []
    events: list[str] = []
    call_id = ""
    async with websockets.connect(f"{ws_base}/ws/test-call?token={TOKEN}", max_size=2**24) as ws:
        ready = json.loads(await asyncio.wait_for(ws.recv(), 10))
        assert ready["type"] == "ready"
        call_id = ready["callId"]
        results.append(f"test call {call_id[:8]} started")
        await ws.send(json.dumps({"type": "text", "text": "How much does it cost for a team of eight?"}))
        deadline = asyncio.get_running_loop().time() + 45
        while asyncio.get_running_loop().time() < deadline:
            message = json.loads(await asyncio.wait_for(ws.recv(), 12))
            if message["type"] == "audio":
                audio_frames += 1
            elif message["type"] == "event":
                event = message["event"]
                events.append(event["type"])
                if event["type"] == "assistant.message":
                    assistant_messages.append(event["data"]["text"])
                    if len(assistant_messages) >= 2:
                        break
        await ws.send(json.dumps({"type": "end"}))
        await asyncio.sleep(0.5)

    assert audio_frames > 100, f"expected audio frames, got {audio_frames}"
    assert len(assistant_messages) >= 2, assistant_messages
    assert "$49" in assistant_messages[-1]
    results.append(f"{audio_frames} audio frames delivered")
    results.append("user turn + agent reply")

    async with httpx.AsyncClient(timeout=10) as client:
        detail = (await client.get(f"{base}/api/calls/{call_id}", headers={"authorization": f"Bearer {TOKEN}"})).json()
        assert detail["turns"] == 1, detail["turns"]
        assert len(detail["messages"]) >= 3, len(detail["messages"])
        assert detail["messages"][1]["role"] == "user"
        results.append(f"persisted {len(detail['messages'])} messages")

        settings = await client.put(
            f"{base}/api/settings",
            headers={"authorization": f"Bearer {TOKEN}"},
            json={"name": "Nova", "knowledge": "Support hours are 9 to 6."},
        )
        assert settings.status_code == 200
        results.append("settings update")

    return results


async def media_check(base: str, ws_base: str, store: Store, config) -> list[str]:
    call = store.create_call(
        direction="inbound",
        transport="twilio",
        status="queued",
        from_number="+14155550123",
        provider_call_id="CAsmoke0001",
    )
    signature = media_signature(config.telephony.auth_token, call["id"])
    async with websockets.connect(
        f"{ws_base}/media/twilio?callId={call['id']}&sig={signature}&greeted=0", max_size=2**24
    ) as ws:
        await ws.send(
            json.dumps(
                {
                    "event": "start",
                    "start": {"streamSid": "MZ1", "callSid": "CAsmoke0001", "customParameters": {"callId": call["id"]}},
                }
            )
        )
        await asyncio.sleep(0.4)
        frames = 0
        while frames < 3:
            message = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if message.get("event") == "media":
                assert len(__import__("base64").b64decode(message["media"]["payload"])) == 160
                frames += 1
        await ws.send(json.dumps({"event": "stop"}))
    for _ in range(50):
        if store.get_call(call["id"])["status"] == "completed":
            break
        await asyncio.sleep(0.05)
    assert store.get_call(call["id"])["status"] == "completed"
    return [f"media stream: {frames} x 160-byte frames, call completed"]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config = load_settings(
            env="test",
            is_production=False,
            data_dir=pathlib.Path(tmp),
            db_path=str(pathlib.Path(tmp) / "smoke.db"),
            log_level="warning",
            api_keys=[TOKEN],
            allow_anonymous_dashboard=False,
            public_url="https://voice.example.com",
            telephony=TelephonyConfig(
                provider="twilio",
                account_sid="ACsmoke",
                auth_token="smoke-media-token",
                from_number="+14155550100",
                public_url="https://voice.example.com",
                validate_signature=True,
                api_base="http://127.0.0.1:1",
            ),
        )
        store = Store(config.db_path)
        providers = build_providers(config)
        app = create_app(settings=config, store=store, providers=providers)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started, "uvicorn did not start"
        port = server.servers[0].sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        ws_base = f"ws://127.0.0.1:{port}"
        try:
            results = asyncio.run(run(base, ws_base))
            results += asyncio.run(media_check(base, ws_base, store, config))
        finally:
            server.should_exit = True
            thread.join(timeout=10)
        print("SMOKE PASS")
        for line in results:
            print(f"  ok  {line}")


if __name__ == "__main__":
    main()
