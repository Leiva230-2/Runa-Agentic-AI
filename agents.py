"""
Runa's three agents.

  Listener    — is this message a business event at all?
  Resolver    — which record does it concern, and how sure am I? (two scores)
  Transactor  — validate and post to the SAP system of record.

Plus a downstream conflict check that runs after a write lands.

The Resolver producing TWO independent confidence scores is the heart of the
system. Identity confidence ("which delivery is this?") and content confidence
("what actually happened?") fail independently: a driver can be unmistakably
identified while saying something vague, or say something precise about an
unclear order. Collapsing them into one number loses the distinction that
decides whether Runa acts or asks.
"""
import json
import re

import anthropic
import httpx

from config import (CHANNEL_MEMORY, CONFIDENCE_THRESHOLD, EVENT_TYPES,
                    MODEL_LISTENER, MODEL_RESOLVER, SAP_BASE, SAP_SERVICE)

client = anthropic.Anthropic()


def _json(text: str) -> dict:
    """Parse model output that should be JSON, tolerating stray prose or fences."""
    text = re.sub(r"```(?:json)?|```", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise ValueError(f"No JSON found in model output: {text[:200]}")
        return json.loads(match.group(0))


def _ask(model: str, system: str, user: str, max_tokens: int = 1024) -> dict:
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return _json(resp.content[0].text)


# ---------------------------------------------------------------- Listener

LISTENER_SYSTEM = f"""You filter an Indonesian logistics operations group chat.

Most messages are irrelevant chatter, greetings, or personal talk. A small
minority report a real business event. Your only job is to tell them apart.

Business event types: {", ".join(EVENT_TYPES)}

Return ONLY JSON:
{{"is_event": true|false,
  "event_type": "<one of the types above, or null>",
  "summary": "<one short English sentence describing what happened>"}}

Be strict. "Selamat pagi semua" is not an event. "Barang kurang 3 dus" is.
Messages that merely acknowledge ("oke", "siap", "noted") are not events."""


def listen(message: str, sender: str) -> dict:
    return _ask(MODEL_LISTENER, LISTENER_SYSTEM,
                f"Sender: {sender}\nMessage: {message}")


# ---------------------------------------------------------------- Resolver

RESOLVER_SYSTEM = """You resolve an informal Indonesian message into a specific
SAP inbound delivery record, and you judge how certain you are.

You will be given open deliveries, what is known about the people in this
channel, and the conversation so far.

Produce TWO INDEPENDENT confidence scores between 0 and 1:

  identity_confidence — how sure are you WHICH delivery record this concerns?
  content_confidence  — how sure are you WHAT happened, precisely enough to
                        write it into an ERP?

Score content_confidence LOW when the speaker hedges. Indonesian hedging
markers include "kayaknya", "kira-kira", "sepertinya", "mungkin", "sekitar".
A hedged estimate is not a fact and must not be written as one.

BUT: if a later message in the conversation firmly confirms a previously
hedged detail, score content_confidence on the confirmation, not on the
original hedge. A hedge that has since been confirmed is no longer hedged.

If either score is below 0.85, write a SHORT clarifying question in Bahasa
Indonesia that would resolve the specific uncertainty. Ask about one thing
only. Be polite and natural, the way a colleague would ask.

Return ONLY JSON:
{"delivery_id": "<id or null>",
 "identity_confidence": 0.0,
 "content_confidence": 0.0,
 "event_type": "DELIVERY_DELAY|GOODS_RECEIPT_DISCREPANCY|QUALITY_COMPLAINT",
 "new_eta": "<YYYY-MM-DDTHH:MM:SS or null>",
 "quantity_short": <number or null>,
 "reason_code": "<TRAFFIC|BREAKDOWN|WEATHER|SHORT_DELIVERY|DAMAGE|OTHER>",
 "reasoning": "<one sentence, in English, on what drove your scores>",
 "clarifying_question": "<Bahasa Indonesia question, or null if both scores clear>"}"""


def resolve(candidate: dict, sender: str, conversation: list[str],
            deliveries: list[dict]) -> dict:
    context = {
        "open_deliveries": deliveries,
        "channel_memory": CHANNEL_MEMORY,
        "current_datetime": "2026-09-09T19:42:00",
    }
    user = (
        f"Candidate event: {json.dumps(candidate)}\n"
        f"Sender: {sender}\n"
        f"Conversation so far:\n" + "\n".join(conversation) + "\n\n"
        f"Context:\n{json.dumps(context, indent=2)}"
    )
    return _ask(MODEL_RESOLVER, RESOLVER_SYSTEM, user, max_tokens=1500)


def clears_threshold(res: dict) -> bool:
    return (res.get("identity_confidence", 0) >= CONFIDENCE_THRESHOLD
            and res.get("content_confidence", 0) >= CONFIDENCE_THRESHOLD)


# -------------------------------------------------------------- Transactor

def get_open_deliveries() -> list[dict]:
    r = httpx.get(f"{SAP_BASE}{SAP_SERVICE}/A_InboundDelivery", timeout=10)
    r.raise_for_status()
    return r.json()["d"]["results"]


def post_delay(delivery_id: str, new_eta: str, reason_code: str,
               source_ref: str) -> dict:
    """PATCH the inbound delivery — the same call shape as live S/4HANA."""
    r = httpx.patch(
        f"{SAP_BASE}{SAP_SERVICE}/A_InboundDelivery('{delivery_id}')",
        json={"PlannedGoodsReceipt": new_eta,
              "Status": "Delayed",
              "DelayReasonCode": reason_code,
              "SourceMessageRef": source_ref},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["d"]


def post_shortage(delivery_id: str, material: str, qty_short: float,
                  source_ref: str) -> dict:
    r = httpx.post(
        f"{SAP_BASE}{SAP_SERVICE}/A_MaterialDocumentHeader",
        json={"InboundDelivery": delivery_id,
              "Material": material,
              "QuantityInDeliveryUnit": qty_short,
              "GoodsMovementReasonCode": "SHORT_DELIVERY",
              "SourceMessageRef": source_ref},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["d"]


# ---------------------------------------------------------------- Conflict

def check_conflict(delivery: dict) -> str | None:
    """After a write lands, reason about what it means for the rest of the
    operation. Nobody asked for this check — that is the point."""
    eta = delivery.get("PlannedGoodsReceipt")
    closing = delivery.get("DockClosingTime")
    if not eta or not closing:
        return None

    eta_hm = eta.split("T")[1][:5]
    if eta_hm > closing:
        did = delivery["InboundDelivery"]
        return (f"@Sari DO {did} sekarang perkiraan tiba {eta_hm}, "
                f"tapi {delivery['ReceivingDock']} tutup {closing}. "
                f"Opsi: (a) perpanjang shift, (b) reschedule besok pagi 06:00. "
                f"Mana yang diambil?")
    return None
