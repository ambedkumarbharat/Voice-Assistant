"""Twilio Programmable Voice: outbound calls, hangup, transfer and webhook checks."""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

import httpx

from .config import Settings
from .errors import ProviderError

_ESCAPES = {"<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&apos;"}


def escape_xml(value: str) -> str:
    return "".join(_ESCAPES.get(char, char) for char in value)


class TwilioTelephony:
    id = "twilio"
    live = True

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        from_number: str,
        public_url: str,
        validate_signature: bool = True,
        api_base: str = "https://api.twilio.com",
    ) -> None:
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number
        self.public_url = public_url
        self.validate = validate_signature
        self.api_base = api_base.rstrip("/")

    def _calls_url(self, suffix: str = "") -> str:
        return f"{self.api_base}/2010-04-01/Accounts/{self.account_sid}/Calls{suffix}"

    async def place_call(
        self,
        *,
        to: str,
        answer_url: str,
        stream_url: str,
        status_callback_url: str | None = None,
        record: bool = False,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        data = {
            "To": to,
            "From": self.from_number,
            "Url": answer_url,
            "Method": "POST",
            "Timeout": str(timeout_seconds),
            "MachineDetection": "Disable",
            "Record": "true" if record else "false",
        }
        if status_callback_url:
            data["StatusCallback"] = status_callback_url
            data["StatusCallbackMethod"] = "POST"
        async with httpx.AsyncClient(auth=(self.account_sid, self.auth_token), timeout=25.0) as client:
            response = await client.post(self._calls_url(".json"), data=data)
        if response.status_code >= 400:
            raise ProviderError(
                f"Twilio call failed: {response.text[:300]}", response.status_code, "twilio", response.status_code >= 500
            )
        payload = response.json()
        if not payload.get("sid"):
            raise ProviderError("Twilio returned no call sid", 502, "twilio")
        return {"provider_call_id": payload["sid"], "status": payload.get("status", "queued"), "raw": payload}

    async def hangup(self, provider_call_id: str) -> None:
        async with httpx.AsyncClient(auth=(self.account_sid, self.auth_token), timeout=15.0) as client:
            response = await client.post(self._calls_url(f"/{provider_call_id}.json"), data={"Status": "completed"})
        if response.status_code >= 400 and response.status_code != 404:
            raise ProviderError(f"Twilio hangup failed ({response.status_code})", response.status_code, "twilio")

    async def transfer(self, provider_call_id: str, to_number: str) -> None:
        if not to_number.startswith("+") or not to_number[1:].isdigit() or not (7 <= len(to_number) - 1 <= 15):
            raise ProviderError("Transfer number must be E.164", 400, "twilio")
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?><Response>'
            '<Say voice="Polly.Joanna">Please hold while I connect you.</Say>'
            f'<Dial timeout="30">{escape_xml(to_number)}</Dial></Response>'
        )
        async with httpx.AsyncClient(auth=(self.account_sid, self.auth_token), timeout=15.0) as client:
            response = await client.post(self._calls_url(f"/{provider_call_id}.json"), data={"Twiml": twiml})
        if response.status_code >= 400:
            raise ProviderError(f"Twilio transfer failed ({response.status_code})", response.status_code, "twilio")

    def build_twiml(self, stream_url: str, call_id: str, greeting: str | None = None) -> str:
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<Response>",
            f'  <Say voice="Polly.Joanna">{escape_xml(greeting)}</Say>' if greeting else "",
            "  <Connect>",
            f'    <Stream url="{escape_xml(stream_url)}">',
            f'      <Parameter name="callId" value="{escape_xml(call_id)}" />',
            "    </Stream>",
            "  </Connect>",
            "</Response>",
        ]
        return "\n".join(line for line in lines if line)

    def validate_signature(self, url: str, params: dict[str, Any], signature: str) -> bool:
        if not self.validate:
            return True
        if not signature:
            return False
        data = url
        for key in sorted(params):
            value = params[key]
            if isinstance(value, list):
                data += key + "".join(str(item) for item in value)
            else:
                data += key + str(value)
        expected = base64.b64encode(
            hmac.new(self.auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
        ).decode("ascii")
        return hmac.compare_digest(expected, signature)


class NoopTelephony:
    id = "none"
    live = False
    from_number = ""

    async def place_call(self, **kwargs: Any) -> dict[str, Any]:
        raise ProviderError("Telephony is not configured. Set TELEPHONY_PROVIDER=twilio with credentials.", 501, "none")

    async def hangup(self, provider_call_id: str) -> None:
        return None


def build_telephony(config: Settings) -> TwilioTelephony | NoopTelephony:
    cfg = config.telephony
    if cfg.provider == "twilio" and cfg.account_sid and cfg.auth_token and cfg.from_number:
        return TwilioTelephony(
            account_sid=cfg.account_sid,
            auth_token=cfg.auth_token,
            from_number=cfg.from_number,
            public_url=cfg.public_url,
            validate_signature=cfg.validate_signature,
            api_base=cfg.api_base,
        )
    return NoopTelephony()
