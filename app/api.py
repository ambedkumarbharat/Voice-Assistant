"""FastAPI application: REST, Twilio webhooks, media streams and the dashboard."""

from __future__ import annotations

import asyncio
import base64
import hmac
import hashlib
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .audio import PIPELINE_RATE, decode_mulaw, decode_pcm16_base64, resample
from .channels import BrowserChannel, TwilioChannel
from .config import Settings, get_settings
from .db import Store, now_ms
from .errors import ProviderError
from .hub import CallHub
from .prompts import AgentSettings, SETTINGS_KEY, load_agent_settings
from .providers import Providers, build_providers
from .session import CallSession
from .telephony import TwilioTelephony

log = logging.getLogger("voxdesk.api")
PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"

e164 = re.compile(r"^\+[1-9]\d{6,14}$")


class ApiError(HTTPException):
    def __init__(self, status_code: int, message: str, code: str = "bad_request") -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code


# --------------------------------------------------------------------- models ---


class OutboundCall(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    to: str
    contact_name: str | None = Field(default=None, alias="contactName", max_length=120)
    greeting: str | None = Field(default=None, max_length=600)
    record: bool = False

    @field_validator("to")
    @classmethod
    def _e164(cls, value: str) -> str:
        if not e164.match(value.strip()):
            raise ValueError("phone number must be E.164, e.g. +14155550123")
        return value.strip()


class AgentSettingsPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=60)
    company: str | None = Field(default=None, min_length=1, max_length=120)
    persona: str | None = Field(default=None, min_length=1, max_length=4000)
    greeting: str | None = Field(default=None, min_length=1, max_length=600)
    fallback: str | None = Field(default=None, min_length=1, max_length=600)
    language: str | None = Field(default=None, min_length=2, max_length=20)
    voice: str | None = Field(default=None, max_length=120)
    knowledge: str | None = Field(default=None, max_length=20000)
    transfer_number: str | None = Field(default=None, alias="transferNumber", max_length=30)
    max_turns: int | None = Field(default=None, alias="maxTurns", ge=1, le=200)
    barge_in: bool | None = Field(default=None, alias="bargeIn")


class ContactPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=5, max_length=30)
    email: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)


class TextPayload(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class ProviderTestPayload(BaseModel):
    target: Literal["llm", "tts"] = "llm"


class LiveCallHandle:
    def __init__(self, call_id: str, hangup: Any, snapshot: Any) -> None:
        self.id = call_id
        self.hangup = hangup
        self.snapshot = snapshot


# --------------------------------------------------------------------- helpers ---


def token_valid(config: Settings, token: str | None) -> bool:
    if not config.api_keys:
        return True
    if not token:
        return False
    return any(hmac.compare_digest(key, token) for key in config.api_keys)


def extract_token(source: Any) -> str | None:
    headers = source.headers
    authorization = headers.get("authorization") or ""
    if authorization.lower().startswith("bearer "):
        value = authorization[7:].strip()
        if value:
            return value
    api_key = headers.get("x-api-key")
    if api_key:
        return api_key.strip()
    query_params = getattr(source, "query_params", None)
    if query_params is not None:
        for name in ("token", "api_key"):
            value = query_params.get(name)
            if value:
                return value
    return None


def media_signature(auth_token: str, call_id: str) -> str:
    return hmac.new(auth_token.encode("utf-8"), f"media:{call_id}".encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def verify_media(auth_token: str, call_id: str, signature: str | None) -> bool:
    if not auth_token:
        return True
    if not signature:
        return False
    return hmac.compare_digest(media_signature(auth_token, call_id), signature)


def agent_dict(agent: AgentSettings) -> dict[str, Any]:
    return {
        "name": agent.name,
        "company": agent.company,
        "persona": agent.persona,
        "greeting": agent.greeting,
        "fallback": agent.fallback,
        "language": agent.language,
        "voice": agent.voice,
        "knowledge": agent.knowledge,
        "transferNumber": agent.transfer_number,
        "maxTurns": agent.max_turns,
        "bargeIn": agent.barge_in,
    }


def parse_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def call_detail(store: Store, hub: CallHub, call: dict[str, Any] | None) -> dict[str, Any] | None:
    if not call:
        return None
    detail = dict(call)
    detail["action_items"] = parse_json(call.get("action_items"), [])
    detail["metadata"] = parse_json(call.get("metadata"), {})
    detail["messages"] = store.list_messages(call["id"])
    detail["events"] = [
        {**event, "payload": parse_json(event.get("payload"), None)} for event in store.list_events(call["id"], 100)
    ]
    live = hub.get(call["id"])
    detail["live"] = live.snapshot() if live else None
    return detail


@asynccontextmanager
async def lifespan(app: FastAPI):
    reaped = app.state.store.mark_stale_calls()
    if reaped:
        log.warning("closed %s calls left open by a previous process", reaped)
    yield
    app.state.store.close()


# ------------------------------------------------------------------------ app ---


def create_app(
    *,
    settings: Settings | None = None,
    store: Store | None = None,
    providers: Providers | None = None,
) -> FastAPI:
    config = settings or get_settings()
    db = store or Store(config.db_path)
    prov = providers or build_providers(config)
    hub = CallHub()

    app = FastAPI(title="VoxDesk", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.config = config
    app.state.store = db
    app.state.providers = prov
    app.state.hub = hub
    app.state.rate = {}
    app.state.rate_lock = asyncio.Lock()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins or ["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["content-type", "authorization", "x-api-key"],
        max_age=600,
    )

    @app.middleware("http")
    async def guard_and_headers(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path != "/api/health":
            key = request.headers.get("x-api-key") or (request.client.host if request.client else "unknown")
            now = time.monotonic()
            async with app.state.rate_lock:
                bucket = app.state.rate.get(key)
                if bucket is None or now - bucket[0] > config.rate_limit_window_seconds:
                    app.state.rate[key] = [now, 1]
                else:
                    bucket[1] += 1
                    if bucket[1] > config.rate_limit_max:
                        return JSONResponse(
                            status_code=429,
                            content={"error": {"message": "Too many requests", "code": "rate_limited"}},
                        )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "microphone=(self), camera=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self' ws: wss:; media-src 'self' blob:; "
            "font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if config.is_production and config.public_url.startswith("https://"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, error: ApiError):
        return JSONResponse(status_code=error.status_code, content={"error": {"message": error.detail, "code": error.code}})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, error: RequestValidationError):
        detail = "; ".join(f"{'.'.join(str(part) for part in issue['loc'][1:])}: {issue['msg']}" for issue in error.errors())
        return JSONResponse(status_code=400, content={"error": {"message": detail, "code": "bad_request"}})

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, error: StarletteHTTPException):
        if error.status_code == 404 and not request.url.path.startswith("/api/"):
            return FileResponse(PUBLIC_DIR / "index.html")
        return JSONResponse(
            status_code=error.status_code,
            content={"error": {"message": str(error.detail), "code": "not_found" if error.status_code == 404 else "http_error"}},
        )

    # ------------------------------------------------------------------ auth ---

    async def require_auth(request: Request) -> None:
        cfg: Settings = request.app.state.config
        if not cfg.api_keys:
            return
        if not token_valid(cfg, extract_token(request)):
            raise ApiError(401, "Unauthorized: send Authorization: Bearer <API_KEYS>", "unauthorized")

    # ---------------------------------------------------------------- health ---

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        app.state.store.ping()
        return {
            "status": "ok",
            "version": __version__,
            "uptimeSeconds": int(time.monotonic() - app.state.started_at),
            "now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "demoMode": prov.demo_mode,
            "providers": prov.report(),
            "liveCalls": len(hub.list()),
        }

    app.state.started_at = time.monotonic()
    api = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

    @api.get("/overview")
    async def overview() -> dict[str, Any]:
        agent = load_agent_settings(db, config)
        calls, _ = db.list_calls(limit=8)
        return {
            "stats": db.stats(),
            "volume": db.daily_volume(14),
            "recent": calls,
            "live": hub.list(),
            "providers": prov.report(),
            "settings": agent_dict(agent),
            "liveConfigured": prov.telephony.live,
            "demoMode": prov.demo_mode,
        }

    @api.get("/calls")
    async def list_calls(
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        status_filter: str | None = Query(None, alias="status"),
        q: str | None = Query(None, max_length=120),
    ) -> dict[str, Any]:
        calls, total = db.list_calls(limit=limit, offset=offset, status=status_filter, query=q)
        for call in calls:
            call["action_items"] = parse_json(call.get("action_items"), [])
            call["live"] = hub.has(call["id"])
        return {"calls": calls, "total": total}

    @api.get("/calls/{call_id}")
    async def get_call(call_id: str) -> dict[str, Any]:
        detail = call_detail(db, hub, db.get_call(call_id))
        if not detail:
            raise ApiError(404, "Call not found", "not_found")
        return detail

    @api.get("/calls/{call_id}/export", response_class=PlainTextResponse)
    async def export_call(call_id: str) -> PlainTextResponse:
        call = db.get_call(call_id)
        if not call:
            raise ApiError(404, "Call not found", "not_found")
        messages = db.list_messages(call_id)
        lines = [
            "VoxDesk call transcript",
            f"Call: {call['id']}",
            f"Direction: {call['direction']} ({call['transport']})",
            f"Status: {call['status']}  Ended: {call.get('end_reason') or 'n/a'}",
            f"Started: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(call['started_at'] / 1000))}",
            f"Duration: {round((call.get('duration_ms') or 0) / 1000)}s",
            f"Summary: {call.get('summary') or ''}",
            "",
        ]
        for message in messages:
            role = "Caller" if message["role"] == "user" else "Agent" if message["role"] == "assistant" else "System"
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(message["at"] / 1000))
            lines.append(f"[{stamp}] {role}: {message['content']}")
        return PlainTextResponse(
            "\n".join(lines) + "\n",
            headers={"content-disposition": f'attachment; filename="call-{call_id}.txt"'},
        )

    @api.post("/calls", status_code=201)
    async def place_call(body: OutboundCall) -> dict[str, Any]:
        if not prov.telephony.live:
            raise ApiError(
                409,
                "Outbound calling requires TELEPHONY_PROVIDER=twilio plus TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN "
                "and TWILIO_PHONE_NUMBER. Use the browser test line meanwhile.",
                "telephony_not_configured",
            )
        call_id = str(uuid.uuid4())
        token = config.telephony.auth_token
        sig = f"&sig={media_signature(token, call_id)}" if token else ""
        stream_url = f"{config.public_ws_base}/media/twilio?callId={call_id}{sig}"
        db.create_call(
            call_id=call_id,
            direction="outbound",
            transport="twilio",
            status="queued",
            to_number=body.to,
            from_number=prov.telephony.from_number,  # type: ignore[union-attr]
            contact_name=body.contact_name,
            metadata={"signedStreamUrl": stream_url, "greeting": body.greeting},
        )
        try:
            result = await prov.telephony.place_call(  # type: ignore[union-attr]
                to=body.to,
                answer_url=f"{config.public_url}/webhooks/twilio/voice?callId={call_id}",
                stream_url=stream_url,
                status_callback_url=f"{config.public_url}/webhooks/twilio/status",
                record=body.record,
            )
            db.update_call(call_id, {"provider_call_id": result["provider_call_id"], "status": "ringing"})
            db.add_event(call_id, "outbound.requested", {"to": body.to, "providerCallId": result["provider_call_id"]})
            return {"call": call_detail(db, hub, db.get_call(call_id))}
        except ProviderError as error:
            db.update_call(call_id, {"status": "failed", "end_reason": "provider_error"})
            db.add_event(call_id, "outbound.failed", {"message": str(error)})
            raise ApiError(502, str(error), "provider_error") from error

    @api.post("/calls/{call_id}/hangup")
    async def hangup_call(call_id: str) -> dict[str, Any]:
        call = db.get_call(call_id)
        if not call:
            raise ApiError(404, "Call not found", "not_found")
        session = hub.get(call_id)
        if session:
            await session.hangup()
            return {"ok": True, "ended": True}
        if call.get("provider_call_id") and prov.telephony.live:
            try:
                await prov.telephony.hangup(call["provider_call_id"])  # type: ignore[union-attr]
            except Exception:
                pass
        db.update_call(call_id, {"status": "completed", "end_reason": "api_hangup", "ended_at": now_ms()})
        return {"ok": True, "ended": False}

    @api.post("/calls/{call_id}/message")
    async def inject_message(call_id: str, body: TextPayload) -> dict[str, Any]:
        session = hub.get(call_id)
        if not session:
            raise ApiError(409, "Call is not live", "not_live")
        await session.inject_text(body.text)
        return {"ok": True}

    @api.post("/providers/test")
    async def provider_test(body: ProviderTestPayload) -> dict[str, Any]:
        started = now_ms()
        try:
            if body.target == "tts":
                result = await prov.tts.synthesize("This is a voice check from VoxDesk.")
                return {
                    "ok": True,
                    "target": "tts",
                    "provider": result.provider,
                    "latencyMs": now_ms() - started,
                    "audioSeconds": round(len(result.audio) / result.sample_rate, 1),
                    "sampleRate": result.sample_rate,
                    "live": prov.tts.live,
                }
            text = await prov.llm.complete(
                [SystemMessage(content="You are a health probe. Reply with exactly: READY"), HumanMessage(content="Ping")]
            )
            return {
                "ok": True,
                "target": "llm",
                "provider": prov.llm.id,
                "model": prov.llm.model_name,
                "live": prov.llm.live,
                "latencyMs": now_ms() - started,
                "sample": text[:120],
            }
        except Exception as error:  # noqa: BLE001
            return JSONResponse(
                status_code=502,
                content={"ok": False, "target": body.target, "latencyMs": now_ms() - started, "error": str(error)},
            )

    # -------------------------------------------------------------- settings ---

    @api.get("/settings")
    async def get_agent_settings() -> dict[str, Any]:
        agent = load_agent_settings(db, config)
        return {"settings": agent_dict(agent), "providers": prov.report(), "limits": {"maxTurns": agent.max_turns}}

    @api.put("/settings")
    async def update_agent_settings(payload: AgentSettingsPayload) -> dict[str, Any]:
        current = db.get_setting(SETTINGS_KEY, {}) or {}
        if not isinstance(current, dict):
            current = {}
        updates = payload.model_dump(by_alias=True, exclude_none=True)
        merged = {**current, **updates}
        db.set_setting(SETTINGS_KEY, merged)
        db.add_event(None, "settings.updated", {"keys": list(updates)})
        return {"settings": agent_dict(load_agent_settings(db, config))}

    # -------------------------------------------------------------- contacts ---

    @api.get("/contacts")
    async def list_contacts(q: str | None = Query(None, max_length=120)) -> dict[str, Any]:
        return {"contacts": db.list_contacts(q.strip() if q and q.strip() else None)}

    @api.post("/contacts", status_code=201)
    async def create_contact(body: ContactPayload) -> dict[str, Any]:
        contact = db.upsert_contact(
            name=body.name,
            phone=body.phone,
            email=body.email or None,
            company=body.company,
            notes=body.notes,
        )
        return {"contact": contact}

    @api.delete("/contacts/{contact_id}")
    async def delete_contact(contact_id: str) -> dict[str, Any]:
        if not db.delete_contact(contact_id):
            raise ApiError(404, "Contact not found", "not_found")
        return {"ok": True}

    app.include_router(api)

    # ------------------------------------------------------- Twilio webhooks ---

    def twilio_telephony() -> TwilioTelephony:
        if not isinstance(prov.telephony, TwilioTelephony) or not prov.telephony.live:
            raise ApiError(503, "Telephony is not configured", "telephony_not_configured")
        return prov.telephony

    def signed_url(request: Request) -> str:
        raw_path = request.scope.get("raw_path") or b""
        path = raw_path.decode("latin-1") if isinstance(raw_path, bytes) else str(raw_path)
        query = request.scope.get("query_string") or b""
        query_text = query.decode("latin-1") if isinstance(query, bytes) else str(query)
        return config.public_url + path + (f"?{query_text}" if query_text else "")

    @app.post("/webhooks/twilio/voice", include_in_schema=False)
    async def twilio_voice(request: Request) -> Response:
        telephony = twilio_telephony()
        form = await request.form()
        params = {key: str(value) for key, value in form.items()}
        signature = request.headers.get("x-twilio-signature", "")
        if not telephony.validate_signature(signed_url(request), params, signature):
            raise ApiError(403, "Invalid Twilio signature", "invalid_signature")

        query_call_id = request.query_params.get("callId")
        provider_call_id = params.get("CallSid") or ""
        existing = db.get_call(query_call_id) if query_call_id else None
        if not existing and provider_call_id:
            existing = db.find_call_by_provider_id(provider_call_id)

        if existing:
            call_id = existing["id"]
            db.update_call(call_id, {"status": "in_progress", "provider_call_id": provider_call_id or existing.get("provider_call_id")})
            record = db.get_call(call_id) or existing
        else:
            record = db.create_call(
                direction="inbound",
                transport="twilio",
                status="queued",
                from_number=params.get("From"),
                to_number=params.get("To"),
                provider_call_id=provider_call_id or None,
                metadata={"answeredBy": params.get("AnsweredBy"), "city": params.get("FromCity"), "country": params.get("FromCountry")},
            )
            call_id = record["id"]

        agent = load_agent_settings(db, config)
        metadata = parse_json(record.get("metadata"), {})
        greeting = (metadata.get("greeting") or "").strip() or agent.greeting
        use_twiml_greeting = not prov.tts.live
        token = config.telephony.auth_token
        sig = f"&sig={media_signature(token, call_id)}" if token else ""
        stream_url = (
            f"{config.public_ws_base}/media/twilio?callId={call_id}{sig}&greeted={'1' if use_twiml_greeting else '0'}"
        )
        twiml = telephony.build_twiml(stream_url, call_id, greeting if use_twiml_greeting else None)
        return Response(content=twiml, media_type="text/xml")

    @app.post("/webhooks/twilio/status", include_in_schema=False)
    async def twilio_status(request: Request) -> Response:
        telephony = twilio_telephony()
        form = await request.form()
        params = {key: str(value) for key, value in form.items()}
        if not telephony.validate_signature(signed_url(request), params, request.headers.get("x-twilio-signature", "")):
            raise ApiError(403, "Invalid Twilio signature", "invalid_signature")
        call = db.find_call_by_provider_id(params.get("CallSid", "")) if params.get("CallSid") else None
        if not call:
            return Response(status_code=204)
        status_value = params.get("CallStatus", "")
        duration = int(params.get("CallDuration") or 0)
        patch: dict[str, Any] = {}
        if status_value == "completed":
            patch = {
                "status": "completed",
                "ended_at": now_ms(),
                "end_reason": call.get("end_reason") or "carrier_hangup",
                "duration_ms": duration * 1000 if duration else call.get("duration_ms"),
            }
        elif status_value in {"in-progress", "answered"}:
            patch = {"status": "in_progress", "answered_at": now_ms()}
        elif status_value in {"ringing", "initiated"}:
            patch = {"status": "ringing"}
        elif status_value in {"busy", "no-answer", "failed", "canceled"}:
            patch = {
                "status": "no_answer" if status_value == "no-answer" else "failed",
                "ended_at": now_ms(),
                "end_reason": status_value,
            }
        if patch:
            db.update_call(call["id"], patch)
        db.add_event(call["id"], "twilio.status", {"status": status_value, "durationSeconds": duration})
        return Response(status_code=204)

    # ----------------------------------------------------------- media stream ---

    @app.websocket("/media/twilio")
    async def media_stream(websocket: WebSocket) -> None:
        call_id = websocket.query_params.get("callId") or ""
        signature = websocket.query_params.get("sig")
        if not call_id:
            await websocket.close(code=1008)
            return
        if not verify_media(config.telephony.auth_token, call_id, signature):
            log.warning("media stream rejected: bad signature for %s", call_id)
            await websocket.close(code=1008)
            return
        record = db.get_call(call_id)
        if not record:
            await websocket.close(code=1008)
            return

        await websocket.accept()
        greeted = websocket.query_params.get("greeted") == "1"
        stream_sid = ""
        session: CallSession | None = None
        try:
            while True:
                message = await websocket.receive_json()
                event = message.get("event")
                if event == "start":
                    start = message.get("start") or {}
                    stream_sid = start.get("streamSid") or message.get("streamSid") or ""
                    params = start.get("customParameters") or {}
                    actual_id = params.get("callId") or call_id
                    record = db.get_call(actual_id) or record
                    metadata = parse_json(record.get("metadata"), {})
                    greeting = (metadata.get("greeting") or "").strip() or None
                    channel = TwilioChannel(websocket, stream_sid)
                    session = CallSession(
                        config=config,
                        store=db,
                        providers=prov,
                        hub=hub,
                        channel=channel,
                        direction=record["direction"],
                        call_id=record["id"],
                        to_number=record.get("to_number"),
                        from_number=record.get("from_number"),
                        contact_name=record.get("contact_name"),
                        provider_call_id=start.get("callSid") or record.get("provider_call_id"),
                        greeting="" if greeted else greeting,
                        metadata={"transport": "twilio", "streamSid": stream_sid},
                    )
                    asyncio.create_task(session.start())
                elif event == "media" and session is not None:
                    payload = (message.get("media") or {}).get("payload")
                    if payload:
                        samples = resample(decode_mulaw(payload), 8000, PIPELINE_RATE)
                        await session.handle_audio(samples)
                elif event == "stop":
                    if session is not None:
                        await session.end("carrier_stop")
                        session = None
                    break
        except WebSocketDisconnect:
            pass
        except Exception as error:  # noqa: BLE001
            log.warning("media stream error: %s", error)
        finally:
            if session is not None:
                await session.end("transport_closed")

    # ------------------------------------------------------ dashboard events ---

    @app.websocket("/ws/live")
    async def live_events(websocket: WebSocket) -> None:
        if not token_valid(config, websocket.query_params.get("token")):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        raw_call_id = websocket.query_params.get("callId")
        call_id = None if not raw_call_id or raw_call_id == "all" else raw_call_id
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        hub.subscribe(call_id, queue)
        await websocket.send_json({"type": "hello", "live": hub.list(), "callId": call_id, "now": now_ms()})
        if call_id:
            record = db.get_call(call_id)
            if record:
                await websocket.send_json(
                    {
                        "type": "snapshot",
                        "call": record,
                        "messages": db.list_messages(call_id),
                        "live": hub.get(call_id).snapshot() if hub.has(call_id) else None,
                    }
                )

        async def forward() -> None:
            while True:
                event = await queue.get()
                await websocket.send_json({"type": "event", "event": event})

        forward_task = asyncio.create_task(forward())
        try:
            while True:
                message = await websocket.receive_json()
                if message.get("type") == "ping":
                    await websocket.send_json({"type": "pong", "now": now_ms()})
                elif message.get("type") == "list":
                    await websocket.send_json({"type": "live", "live": hub.list()})
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            forward_task.cancel()
            hub.unsubscribe(call_id, queue)

    # -------------------------------------------------------- browser test line ---

    @app.websocket("/ws/test-call")
    async def test_call(websocket: WebSocket) -> None:
        if not token_valid(config, websocket.query_params.get("token")):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        agent = load_agent_settings(db, config)
        greeting_override = websocket.query_params.get("greeting")

        async def send(message: dict[str, Any]) -> None:
            await websocket.send_json(message)

        channel = BrowserChannel(send)
        session = CallSession(
            config=config,
            store=db,
            providers=prov,
            hub=hub,
            channel=channel,
            direction="web",
            greeting=greeting_override,
            metadata={"transport": "browser"},
        )
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        hub.subscribe(session.id, queue)
        await websocket.send_json(
            {
                "type": "ready",
                "callId": session.id,
                "agent": {"name": agent.name, "company": agent.company, "greeting": greeting_override or agent.greeting},
                "providers": prov.report(),
                "sampleRate": PIPELINE_RATE,
                "format": "pcm16",
            }
        )

        async def forward() -> None:
            while True:
                event = await queue.get()
                await websocket.send_json({"type": "event", "event": event})

        forward_task = asyncio.create_task(forward())
        start_task = asyncio.create_task(session.start())
        try:
            while True:
                message = await websocket.receive_json()
                kind = message.get("type")
                if kind == "audio" and isinstance(message.get("payload"), str):
                    try:
                        await session.handle_audio(decode_pcm16_base64(message["payload"]))
                    except Exception:
                        pass
                elif kind == "text" and isinstance(message.get("text"), str):
                    await session.inject_text(message["text"])
                elif kind == "end":
                    await session.end("client_hangup")
                    break
                elif kind == "ping":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        except Exception as error:  # noqa: BLE001
            log.warning("test call error: %s", error)
        finally:
            start_task.cancel()
            forward_task.cancel()
            hub.unsubscribe(session.id, queue)
            await session.end("client_closed")

    # --------------------------------------------------------------- dashboard ---

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(PUBLIC_DIR / "index.html")

    @app.get("/styles.css", include_in_schema=False)
    async def styles() -> FileResponse:
        return FileResponse(PUBLIC_DIR / "styles.css", media_type="text/css")

    @app.get("/app.js", include_in_schema=False)
    async def app_js() -> FileResponse:
        return FileResponse(PUBLIC_DIR / "app.js", media_type="application/javascript")

    @app.get("/audio.js", include_in_schema=False)
    async def audio_js() -> FileResponse:
        return FileResponse(PUBLIC_DIR / "audio.js", media_type="application/javascript")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> Response:
        if full_path.startswith(("api/", "ws/", "media/", "webhooks/")):
            return JSONResponse(status_code=404, content={"error": {"message": "Not found", "code": "not_found"}})
        return FileResponse(PUBLIC_DIR / "index.html")

    return app
