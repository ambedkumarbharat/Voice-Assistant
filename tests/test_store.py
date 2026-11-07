from __future__ import annotations

import json

from app.db import Store


def test_calls_messages_and_stats(store: Store):
    call = store.create_call(
        direction="outbound",
        transport="twilio",
        status="in_progress",
        to_number="+14155550123",
        from_number="+14155550100",
        contact_name="Jordan",
    )
    assert call["id"]
    assert call["turns"] == 0

    store.add_message(call["id"], "user", "I need pricing")
    store.add_message(call["id"], "assistant", "Plans start at $49.", 420)
    messages = store.list_messages(call["id"])
    assert len(messages) == 2
    assert messages[1]["latency_ms"] == 420

    store.update_call(call["id"], {"status": "completed", "duration_ms": 61000, "turns": 1, "outcome": "resolved"})
    updated = store.get_call(call["id"])
    assert updated["status"] == "completed"
    assert updated["duration_ms"] == 61000

    stats = store.stats()
    assert stats["totalCalls"] == 1
    assert stats["completed"] == 1
    assert stats["avgDurationMs"] == 61000
    assert stats["messages"] == 2
    assert stats["resolutionRate"] == 100
    assert len(store.daily_volume(7)) == 7


def test_search_status_and_pagination(store: Store):
    for index in range(5):
        call = store.create_call(
            direction="inbound",
            status="completed" if index < 2 else "failed",
            from_number=f"+1415555010{index}",
        )
        store.update_call(call["id"], {"summary": "Refund request" if index == 0 else "General question"})
    calls, total = store.list_calls(limit=2)
    assert len(calls) == 2 and total == 5
    assert store.list_calls(status="completed")[1] == 2
    assert store.list_calls(query="refund")[1] == 1


def test_contact_upsert_and_delete(store: Store):
    created = store.upsert_contact(name="Sam", phone="+14155550111", company="Acme")
    updated = store.upsert_contact(name="Sam Rivera", phone="+14155550111", company="Acme Inc")
    assert created["id"] == updated["id"]
    assert updated["name"] == "Sam Rivera"
    assert len(store.list_contacts()) == 1
    assert store.delete_contact(created["id"]) is True
    assert store.list_contacts() == []


def test_settings_round_trip(store: Store):
    assert store.get_setting("agent", {"name": "Ava"}) == {"name": "Ava"}
    store.set_setting("agent", {"name": "Nova", "bargeIn": False})
    assert store.get_setting("agent", {}) == {"name": "Nova", "bargeIn": False}
    assert json.loads(store._conn.execute("SELECT value FROM settings WHERE key='agent'").fetchone()[0])["name"] == "Nova"


def test_mark_stale_calls(store: Store):
    call = store.create_call(direction="inbound", status="in_progress")
    assert store.mark_stale_calls() == 1
    updated = store.get_call(call["id"])
    assert updated["status"] == "failed"
    assert updated["end_reason"] == "process_restart"
