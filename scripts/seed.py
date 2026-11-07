"""Seed the dashboard with realistic demo calls.

    python scripts/seed.py --yes
"""

from __future__ import annotations

import json
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db import Store, now_ms  # noqa: E402

MINUTE = 60_000

CALLS = [
    {
        "direction": "inbound",
        "status": "completed",
        "from": "+14155550142",
        "to": "+14155550100",
        "contact": "Priya Raman",
        "age": 95 * MINUTE,
        "duration": 3 * MINUTE + 12_000,
        "outcome": "resolved",
        "sentiment": "positive",
        "summary": "Priya asked about pricing for an eight-person team. The agent explained the $49 starter and $149 growth tiers and sent a pricing sheet; she will confirm with her co-founder.",
        "actions": ["Email the pricing sheet to priya@lumenlabs.io"],
        "messages": [
            ("assistant", "Hi, thanks for calling Northwind Labs. This is Ava - how can I help you today?", 900),
            ("user", "Hi, I am trying to figure out pricing for a team of eight.", 7200),
            ("assistant", "Happy to help. Our starter tier is $49 a month and growth is $149 a month with unlimited seats - for eight people growth is usually the better fit. Would you like me to send the full breakdown?", 9600),
            ("user", "Yes please, that would help. We are also comparing you to one other tool.", 21000),
            ("assistant", "Understood. I will email the breakdown and a short comparison sheet so you have both side by side. What is the best address for you?", 23800),
            ("user", "priya at lumenlabs dot io.", 32000),
            ("assistant", "Got it - priya at lumenlabs dot io. I have sent that over. Is there anything else I can clear up before you go?", 34600),
            ("user", "No, that is perfect. Thanks for your help.", 41000),
        ],
    },
    {
        "direction": "outbound",
        "status": "completed",
        "from": "+14155550100",
        "to": "+14155550188",
        "contact": "Marcus Webb",
        "age": 26 * 60 * MINUTE,
        "duration": 5 * MINUTE + 40_000,
        "outcome": "resolved",
        "sentiment": "positive",
        "summary": "Outbound demo booking. Marcus picked Thursday at 10 a.m. and asked for a calendar invite plus a note about CRM sync.",
        "actions": ["Send calendar invite for Thursday 10:00", "Include HubSpot sync notes"],
        "messages": [
            ("assistant", "Hi Marcus, this is Ava from Northwind Labs - you asked for a quick walkthrough. Is now still a good time?", 1200),
            ("user", "Yes, I have about ten minutes.", 6000),
            ("assistant", "Perfect. I will keep it tight. Are you mainly evaluating us for inbound support or for outbound follow-up?", 8300),
            ("user", "Inbound mostly, and we use HubSpot.", 15000),
            ("assistant", "That is a common setup. We sync transcripts and outcomes straight into HubSpot, so your reps see the whole conversation. Would Thursday at ten work for a deeper demo?", 18000),
            ("user", "Thursday at ten is good.", 27000),
            ("assistant", "Booked. I will send the invite and a short note on the HubSpot sync. Anything you want the solutions engineer to prepare?", 29500),
        ],
    },
    {
        "direction": "inbound",
        "status": "completed",
        "from": "+14155550163",
        "to": "+14155550100",
        "contact": "Dana Ortiz",
        "age": 51 * 60 * MINUTE,
        "duration": 7 * MINUTE + 5000,
        "outcome": "escalated",
        "sentiment": "negative",
        "summary": "Dana disputed a duplicate charge and was frustrated. The agent captured the invoice details and transferred her to billing.",
        "actions": ["Billing to review invoice INV-4821 duplicate charge", "Follow up with Dana within 24h"],
        "messages": [
            ("assistant", "Thanks for calling Northwind Labs. This is Ava - how can I help?", 800),
            ("user", "I have been charged twice this month and I am not happy about it.", 5200),
            ("assistant", "I am sorry, that is frustrating. Let me pull this up and get it fixed. Do you have the invoice number handy?", 7400),
            ("user", "It is INV-4821, charged on the third and again on the fifth.", 14000),
            ("assistant", "Thank you. I can see the duplicate on the fifth. I am flagging it for billing now and noting both dates on the account.", 16600),
        ],
    },
    {
        "direction": "web",
        "status": "completed",
        "from": None,
        "to": None,
        "contact": None,
        "age": 74 * 60 * MINUTE,
        "duration": 80_000,
        "outcome": "resolved",
        "sentiment": "neutral",
        "summary": "Browser test call asking about support hours. The agent confirmed 9 to 6 Eastern, Monday to Friday.",
        "actions": [],
        "messages": [
            ("assistant", "Hi, thanks for calling. This is Ava - how can I help you today?", 900),
            ("user", "What are your support hours?", 5000),
            ("assistant", "We are open Monday to Friday, 9 a.m. to 6 p.m. Eastern, with an on-call line for urgent issues. Anything else I can check?", 7200),
        ],
    },
    {
        "direction": "outbound",
        "status": "no_answer",
        "from": "+14155550100",
        "to": "+14155550109",
        "contact": None,
        "age": 96 * 60 * MINUTE,
        "duration": 0,
        "outcome": None,
        "sentiment": None,
        "summary": None,
        "actions": [],
        "messages": [],
    },
    {
        "direction": "outbound",
        "status": "completed",
        "from": "+14155550100",
        "to": "+14155550177",
        "contact": "Alex Chen",
        "age": 120 * 60 * MINUTE,
        "duration": 210_000,
        "outcome": "follow_up_needed",
        "sentiment": "neutral",
        "summary": "Alex asked whether the API supports Salesforce sync and webhooks. The agent confirmed both and promised an integration guide.",
        "actions": ["Send Salesforce integration guide"],
        "messages": [
            ("assistant", "Hi Alex, Ava from Northwind Labs. Do you have two minutes to talk integrations?", 1100),
            ("user", "Sure. Does your API sync with Salesforce?", 5600),
            ("assistant", "Yes, there is a native Salesforce sync and a REST API with webhooks for anything custom. Most teams are live in under an hour. Which objects do you need to sync?", 8000),
            ("user", "Contacts and call outcomes, mostly.", 16000),
            ("assistant", "That is the standard mapping. I will send the integration guide with the contact and outcome schemas so your admin can review it.", 18400),
        ],
    },
    {
        "direction": "inbound",
        "status": "completed",
        "from": "+14155550154",
        "to": "+14155550100",
        "contact": "Riley Novak",
        "age": 145 * 60 * MINUTE,
        "duration": 130_000,
        "outcome": "resolved",
        "sentiment": "positive",
        "summary": "Riley requested a refund for a duplicate seat and confirmed the account email. The agent logged the request for billing.",
        "actions": ["Billing to refund duplicate seat"],
        "messages": [
            ("assistant", "Thanks for calling Northwind Labs. This is Ava - how can I help?", 850),
            ("user", "We were billed for an extra seat we did not use. Can that be refunded?", 6200),
            ("assistant", "I can take care of that. I will flag the duplicate seat to billing and you will get a confirmation within one business day. Can you confirm the email on the account?", 8800),
            ("user", "riley at novakdesign dot com.", 16000),
        ],
    },
    {
        "direction": "inbound",
        "status": "failed",
        "from": "+14155550021",
        "to": "+14155550100",
        "contact": None,
        "age": 260 * 60 * MINUTE,
        "duration": 0,
        "outcome": None,
        "sentiment": None,
        "summary": None,
        "actions": [],
        "messages": [],
    },
]

CONTACTS = [
    ("Priya Raman", "+14155550142", "priya@lumenlabs.io", "Lumen Labs", "Evaluating growth tier for 8 seats."),
    ("Marcus Webb", "+14155550188", "marcus@webbops.com", "Webb Ops", "HubSpot user, demo Thursday."),
    ("Dana Ortiz", "+14155550163", "dana@ortiz.co", "Ortiz & Co", "Billing dispute, invoice INV-4821."),
    ("Alex Chen", "+14155550177", "alex@northstar.dev", "Northstar", "Salesforce integration."),
]


def main() -> None:
    config = get_settings()
    if "--yes" not in sys.argv:
        print(f"This will add demo calls to {config.db_path}")
        print("Re-run with --yes to confirm:  python scripts/seed.py --yes")
        return

    store = Store(config.db_path)
    now = now_ms()
    count = 0
    try:
        for entry in CALLS:
            call_id = str(uuid.uuid4())
            started = now - entry["age"]
            ended = started + entry["duration"]
            turns = sum(1 for role, _text, _offset in entry["messages"] if role == "user")
            with store._conn:
                store._conn.execute(
                    """INSERT INTO calls (id, direction, transport, status, from_number, to_number, contact_name,
                       provider_call_id, started_at, answered_at, ended_at, duration_ms, end_reason, turns,
                       summary, outcome, sentiment, action_items, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        call_id,
                        entry["direction"],
                        "browser" if entry["direction"] == "web" else "twilio",
                        entry["status"],
                        entry["from"],
                        entry["to"],
                        entry["contact"],
                        f"CA{uuid.uuid4().hex[:24]}" if entry["direction"] != "web" else None,
                        started,
                        started + 800 if entry["messages"] else None,
                        ended if entry["status"] != "no_answer" else ended,
                        entry["duration"] or None,
                        "no-answer" if entry["status"] == "no_answer" else "carrier_failed" if entry["status"] == "failed" else "carrier_hangup",
                        turns,
                        entry["summary"],
                        entry["outcome"],
                        entry["sentiment"],
                        json.dumps(entry["actions"]),
                        json.dumps({"seeded": True}),
                    ),
                )
                for role, content, offset in entry["messages"]:
                    store._conn.execute(
                        "INSERT INTO messages (call_id, role, content, at, latency_ms) VALUES (?, ?, ?, ?, ?)",
                        (call_id, role, content, started + offset, 480 if role == "assistant" else None),
                    )
                store._conn.execute(
                    "INSERT INTO events (call_id, type, payload, at) VALUES (?, ?, ?, ?)",
                    (call_id, "call.started", json.dumps({"transport": "twilio", "seeded": True}), started),
                )
            count += 1
        for name, phone, email, company, notes in CONTACTS:
            store.upsert_contact(name=name, phone=phone, email=email, company=company, notes=notes)
    finally:
        store.close()

    print(f"Seeded {count} calls and {len(CONTACTS)} contacts into {config.db_path}")


if __name__ == "__main__":
    main()
