from __future__ import annotations

import json


def test_health_is_public(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["demoMode"] is True
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_api_requires_key(client, auth):
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/overview", headers={"authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/overview", headers=auth).status_code == 200
    assert client.get("/api/overview", headers={"x-api-key": "test-key"}).status_code == 200
    assert client.get("/api/overview?token=test-key").status_code == 200


def test_overview_and_empty_calls(client, auth):
    overview = client.get("/api/overview", headers=auth).json()
    assert overview["stats"]["totalCalls"] == 0
    assert overview["providers"]["llm"]["id"] == "mock"
    assert len(overview["volume"]) == 14
    calls = client.get("/api/calls", headers=auth).json()
    assert calls == {"calls": [], "total": 0}


def test_outbound_rejected_without_telephony(client, auth):
    response = client.post("/api/calls", headers=auth, json={"to": "+14155550123"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "telephony_not_configured"

    bad = client.post("/api/calls", headers=auth, json={"to": "555-0123"})
    assert bad.status_code == 400
    assert "E.164" in bad.json()["error"]["message"]


def test_settings_validation_and_persistence(client, auth):
    invalid = client.put("/api/settings", headers=auth, json={"maxTurns": 1000})
    assert invalid.status_code == 400

    updated = client.put(
        "/api/settings",
        headers=auth,
        json={"name": "Nova", "knowledge": "Support is 9-6 ET.", "bargeIn": False},
    )
    assert updated.status_code == 200
    assert updated.json()["settings"]["name"] == "Nova"
    assert updated.json()["settings"]["bargeIn"] is False

    fetched = client.get("/api/settings", headers=auth).json()["settings"]
    assert fetched["knowledge"] == "Support is 9-6 ET."
    assert fetched["transferNumber"] == ""


def test_contacts_crud(client, auth):
    created = client.post(
        "/api/contacts",
        headers=auth,
        json={"name": "Jordan Lee", "phone": "+14155550123", "company": "Acme"},
    )
    assert created.status_code == 201
    contact_id = created.json()["contact"]["id"]
    assert len(client.get("/api/contacts", headers=auth).json()["contacts"]) == 1
    assert client.delete(f"/api/contacts/{contact_id}", headers=auth).status_code == 200
    assert client.delete(f"/api/contacts/{contact_id}", headers=auth).status_code == 404


def test_call_detail_export_and_404(client, auth, store):
    call = store.create_call(direction="web", transport="browser", status="completed", from_number="+14155550000")
    store.add_message(call["id"], "user", "Hello")
    store.add_message(call["id"], "assistant", "Hi, how can I help?")
    store.update_call(call["id"], {"duration_ms": 4200, "summary": "Greeting", "outcome": "resolved"})

    assert client.get("/api/calls/missing", headers=auth).status_code == 404
    detail = client.get(f"/api/calls/{call['id']}", headers=auth).json()
    assert len(detail["messages"]) == 2
    assert detail["action_items"] == []
    assert detail["metadata"] == {}

    export = client.get(f"/api/calls/{call['id']}/export", headers=auth)
    assert export.status_code == 200
    assert "Caller: Hello" in export.text
    assert "Agent: Hi, how can I help?" in export.text


def test_provider_self_test(client, auth):
    response = client.post("/api/providers/test", headers=auth, json={"target": "llm"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["sample"] == "READY"


def test_unknown_api_route_is_json_404(client, auth):
    response = client.get("/api/nope", headers=auth)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_spa_fallback_serves_dashboard(client):
    response = client.get("/some/deep/link")
    assert response.status_code == 200
    assert "VoxDesk" in response.text


def test_settings_payload_is_merged_not_replaced(client, auth):
    client.put("/api/settings", headers=auth, json={"name": "Nova", "language": "en-GB"})
    client.put("/api/settings", headers=auth, json={"knowledge": "Returns within 30 days."})
    fetched = client.get("/api/settings", headers=auth).json()["settings"]
    assert fetched["name"] == "Nova"
    assert fetched["language"] == "en-GB"
    assert fetched["knowledge"] == "Returns within 30 days."
    assert json.dumps(fetched)
