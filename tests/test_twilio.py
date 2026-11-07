from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import TelephonyConfig
from app.db import Store
from app.providers import build_providers
from app.telephony import TwilioTelephony

PUBLIC = "https://voice.example.com"
AUTH_TOKEN = "twilio-test-token"
SID = "CA" + "f" * 32


class MockTwilioHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode()
        MockTwilioHandler.requests.append(
            {"path": self.path, "body": body, "authorization": self.headers.get("authorization")}
        )
        if self.path.endswith("/Calls.json"):
            payload = {"sid": SID, "status": "queued"}
            self.send_response(201)
        else:
            payload = {"sid": SID, "status": "completed"}
            self.send_response(200)
        encoded = json.dumps(payload).encode()
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args) -> None:  # silence the test server
        return


@pytest.fixture
def mock_twilio():
    MockTwilioHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockTwilioHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def sign(url: str, params: dict[str, str]) -> str:
    data = url + "".join(key + params[key] for key in sorted(params))
    return base64.b64encode(hmac.new(AUTH_TOKEN.encode(), data.encode(), hashlib.sha1).digest()).decode()


@pytest.fixture
def twilio_app(config, mock_twilio):
    cfg = replace(
        config,
        public_url=PUBLIC,
        telephony=TelephonyConfig(
            provider="twilio",
            account_sid="ACtest123",
            auth_token=AUTH_TOKEN,
            from_number="+14155550100",
            public_url=PUBLIC,
            validate_signature=True,
            api_base=mock_twilio,
        ),
    )
    store = Store(":memory:")
    providers = build_providers(cfg)
    app = create_app(settings=cfg, store=store, providers=providers)
    with TestClient(app) as client:
        yield client, store


def test_twilio_signature_and_twiml():
    telephony = TwilioTelephony(
        account_sid="AC1",
        auth_token=AUTH_TOKEN,
        from_number="+14155550100",
        public_url=PUBLIC,
    )
    url = f"{PUBLIC}/webhooks/twilio/voice?callId=abc"
    params = {"CallSid": "CA1", "From": "+14155550123", "To": "+14155550100"}
    signature = sign(url, params)
    assert telephony.validate_signature(url, params, signature) is True
    assert telephony.validate_signature(url, {**params, "To": "+14155550199"}, signature) is False
    assert telephony.validate_signature(url, params, "") is False

    twiml = telephony.build_twiml("wss://example.com/media/twilio?callId=1", "call-1", "Hi <script> & welcome")
    assert "<Connect>" in twiml
    assert "wss://example.com/media/twilio?callId=1" in twiml
    assert "&lt;script&gt;" in twiml
    assert "<script>" not in twiml
    assert '<Parameter name="callId" value="call-1" />' in twiml


@pytest.mark.asyncio
async def test_transfer_requires_e164():
    telephony = TwilioTelephony(
        account_sid="AC1",
        auth_token=AUTH_TOKEN,
        from_number="+14155550100",
        public_url=PUBLIC,
    )
    with pytest.raises(Exception):
        await telephony.transfer("CA1", "555-0123")


def test_inbound_greeting_uses_contact_name(twilio_app):
    client, store = twilio_app
    store.upsert_contact(name="Priya Raman", phone="+14155550142", company="Lumen")
    params = {"CallSid": "CAinbound999", "From": "+14155550142", "To": "+14155550100"}
    url = PUBLIC + "/webhooks/twilio/voice"
    response = client.post(url, data=params, headers={"x-twilio-signature": sign(url, params)})
    assert response.status_code == 200
    assert "Hi Priya, thanks for calling" in response.text


def test_outbound_flow_with_mock_carrier(twilio_app, mock_twilio):
    client, store = twilio_app
    auth = {"authorization": "Bearer test-key"}

    placed = client.post(
        "/api/calls",
        headers=auth,
        json={"to": "+14155550123", "contactName": "Jordan", "greeting": "Hi Jordan, this is the test line."},
    )
    assert placed.status_code == 201
    call = placed.json()["call"]
    assert call["status"] == "ringing"

    place_request = MockTwilioHandler.requests[0]
    assert place_request["path"].endswith("/Calls.json")
    assert place_request["authorization"] == "Basic " + base64.b64encode(b"ACtest123:" + AUTH_TOKEN.encode()).decode()
    body = {key: values[0] for key, values in parse_qs(place_request["body"]).items()}
    assert body["To"] == "+14155550123"
    assert "callId=" in body["Url"]

    # Signed TwiML fetch
    voice_path = f"/webhooks/twilio/voice?callId={call['id']}"
    params = {"CallSid": SID, "From": "+14155550100", "To": "+14155550123"}
    twiml = client.post(
        voice_path,
        data=params,
        headers={"x-twilio-signature": sign(PUBLIC + voice_path, params)},
    )
    assert twiml.status_code == 200
    assert "<Connect>" in twiml.text
    assert "wss://voice.example.com/media/twilio?callId=" in twiml.text
    assert "&amp;sig=" in twiml.text
    assert "Hi Jordan, this is the test line." in twiml.text

    tampered = client.post(
        voice_path,
        data=params,
        headers={"x-twilio-signature": "bm90LWEtc2ln"},
    )
    assert tampered.status_code == 403

    # Status callback
    status_path = "/webhooks/twilio/status"
    status_params = {"CallSid": SID, "CallStatus": "completed", "CallDuration": "42"}
    status = client.post(
        status_path,
        data=status_params,
        headers={"x-twilio-signature": sign(PUBLIC + status_path, status_params)},
    )
    assert status.status_code == 204
    detail = store.get_call(call["id"])
    assert detail["status"] == "completed"
    assert detail["duration_ms"] == 42000

    # Hangup reaches the carrier
    hangup = client.post(f"/api/calls/{call['id']}/hangup", headers=auth)
    assert hangup.status_code == 200
    assert any(f"/Calls/{SID}.json" in request["path"] for request in MockTwilioHandler.requests)
    assert any("Status=completed" in request["body"] for request in MockTwilioHandler.requests)
