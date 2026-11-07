from __future__ import annotations

import asyncio
import base64
import json
import threading
import time

import httpx
import pytest
import uvicorn
import websockets
from websockets.exceptions import InvalidStatus

from app.api import create_app, media_signature
from app.config import TelephonyConfig, load_settings
from app.db import Store
from app.providers import build_providers


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("voxdesk-live")
    config = load_settings(
        env="test",
        is_production=False,
        data_dir=tmp,
        db_path=str(tmp / "live.db"),
        log_level="silent",
        api_keys=["test-key"],
        allow_anonymous_dashboard=False,
        public_url="https://voice.example.com",
        telephony=TelephonyConfig(
            provider="twilio",
            account_sid="AClive",
            auth_token="media-token",
            from_number="+14155550100",
            public_url="https://voice.example.com",
            validate_signature=True,
            api_base="http://127.0.0.1:1",
        ),
    )
    store = Store(str(tmp / "live.db"))
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
    try:
        yield f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}", store, config
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.mark.asyncio
async def test_browser_call_end_to_end(live_server):
    base, ws_base, store, _config = live_server
    audio_frames = 0
    events: list[str] = []
    call_id = ""
    assistant_messages: list[str] = []

    async with websockets.connect(f"{ws_base}/ws/test-call?token=test-key", max_size=2**24) as ws:
        ready = json.loads(await asyncio.wait_for(ws.recv(), 10))
        assert ready["type"] == "ready"
        call_id = ready["callId"]
        assert ready["format"] == "pcm16"
        assert ready["sampleRate"] == 16000

        await ws.send(json.dumps({"type": "text", "text": "How much does it cost for a team of eight?"}))
        deadline = asyncio.get_running_loop().time() + 40
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

    assert audio_frames > 50, f"expected audio frames, got {audio_frames}"
    assert len(assistant_messages) >= 2
    assert "$49" in assistant_messages[-1]
    assert "transcript.message" in events

    async with httpx.AsyncClient(timeout=10) as client:
        detail = (await client.get(f"{base}/api/calls/{call_id}", headers={"authorization": "Bearer test-key"})).json()
    assert detail["turns"] == 1
    assert len(detail["messages"]) >= 3
    assert detail["messages"][1]["role"] == "user"


@pytest.mark.asyncio
async def test_media_stream_audio_and_teardown(live_server):
    _base, ws_base, store, config = live_server
    call = store.create_call(
        direction="inbound",
        transport="twilio",
        status="queued",
        from_number="+14155550123",
        to_number="+14155550100",
        provider_call_id="CAmedia0001",
    )
    signature = media_signature(config.telephony.auth_token, call["id"])
    url = f"{ws_base}/media/twilio?callId={call['id']}&sig={signature}&greeted=0"

    async with websockets.connect(url, max_size=2**24) as ws:
        await ws.send(
            json.dumps(
                {
                    "event": "start",
                    "start": {
                        "streamSid": "MZ123",
                        "callSid": "CAmedia0001",
                        "customParameters": {"callId": call["id"], "greeted": "0"},
                    },
                }
            )
        )
        silence = base64.b64encode(b"\xff" * 160).decode()
        for _ in range(3):
            await ws.send(json.dumps({"event": "media", "streamSid": "MZ123", "media": {"payload": silence}}))

        media_frames: list[str] = []
        deadline = asyncio.get_running_loop().time() + 15
        while len(media_frames) < 3 and asyncio.get_running_loop().time() < deadline:
            message = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if message.get("event") == "media":
                assert message["streamSid"] == "MZ123"
                media_frames.append(message["media"]["payload"])

        assert len(media_frames) >= 3
        assert len(base64.b64decode(media_frames[0])) == 160
        await ws.send(json.dumps({"event": "stop"}))

    for _ in range(100):
        record = store.get_call(call["id"])
        if record and record["status"] == "completed":
            break
        await asyncio.sleep(0.05)
    record = store.get_call(call["id"])
    assert record["status"] == "completed"
    assert record["end_reason"] == "carrier_stop"


@pytest.mark.asyncio
async def test_media_stream_rejects_forged_signature(live_server):
    _base, ws_base, store, _config = live_server
    call = store.create_call(direction="inbound", transport="twilio", status="queued")
    with pytest.raises(InvalidStatus) as error:
        async with websockets.connect(f"{ws_base}/media/twilio?callId={call['id']}&sig=deadbeef"):
            pass
    assert error.value.response.status_code in {401, 403}


@pytest.mark.asyncio
async def test_media_stream_rejects_unknown_call(live_server):
    _base, ws_base, _store, config = live_server
    unknown = "00000000-0000-4000-8000-000000000000"
    signature = media_signature(config.telephony.auth_token, unknown)
    with pytest.raises(InvalidStatus) as error:
        async with websockets.connect(f"{ws_base}/media/twilio?callId={unknown}&sig={signature}"):
            pass
    assert error.value.response.status_code in {403, 404}
