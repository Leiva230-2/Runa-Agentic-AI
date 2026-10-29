"""
Mock SAP S/4HANA OData service.

This implements the request/response shape of the real
API_INBOUND_DELIVERY_SRV OData v2 service: the same entity set name, the same
field names, and the same {"d": {...}} envelope S/4HANA returns.

Because the contract is real, pointing Runa at a live S/4HANA system is a
change to SAP_BASE in .env and nothing else.

Run with:  uvicorn sap_mock:app --port 8000
"""
import asyncio
import secrets
import time
from copy import deepcopy
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import DEMO_NOW, NOTEBOOK_SEED, RUNA_API_KEY, STAGES, SUPERVISOR_PIN, now, seed_deliveries
from store import Store, keep_events

app = FastAPI(title="Mock S/4HANA — API_INBOUND_DELIVERY_SRV")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], ## should only be dashboard address
    allow_methods=["*"],
    allow_headers=["*"],
)



@app.middleware("http")
async def require_key(request: Request, call_next):
    """The lock. Reading is open, because the dashboard only reads.
    Changing anything (POST, PATCH) needs the secret key, so a stranger who
    finds this address can't write fake data or wipe the demo."""
    # /a2a is Joule's read-only door to Runa (see a2a_agent.py): it can't write.
    # /runa/decision/choose is the dashboard's button: it has its own lock,
    # the supervisor's PIN, and only passes a choice on to Runa (see below).
    if (RUNA_API_KEY and request.method not in ("GET", "HEAD", "OPTIONS")
            and not request.url.path.startswith("/a2a")
            and request.url.path != "/runa/decision/choose"):
        given = request.headers.get("X-Runa-Key", "")
        if not secrets.compare_digest(given, RUNA_API_KEY):
            return JSONResponse({"detail": "Missing or wrong X-Runa-Key"},
                                status_code=401)
    response = await call_next(request)
    # Anything that changed data is saved before Runa hears "done".
    if (request.method in ("POST", "PATCH") and response.status_code < 400
            and not request.url.path.startswith("/a2a")):
        await asyncio.to_thread(STORE.save, snapshot)
    return response


SERVICE = "/sap/opu/odata/sap/API_INBOUND_DELIVERY_SRV"

DB = seed_deliveries()
MATERIAL_DOCUMENTS: list[dict] = []
QUALITY_NOTIFICATIONS: list[dict] = []
CHANGE_LOG: list[dict] = []
RUNA_EVENTS: list[dict] = []
NOTEBOOK: list[dict] = []      # Runa's notebook (not SAP — Runa's own memory)
FACTS: list[dict] = []         # what Runa read from the chat about constraints
DECISIONS: list[dict] = []     # options the Planner offered, and what a human chose
OPEN_DECISION: dict | None = None   # the escalation Runa is waiting on (sent by the bot)
CHOICES: list[dict] = []            # options picked on the dashboard, for the bot to carry out
PIN_FAILURES: list[float] = []      # wrong PINs, for a short lockout
LOG: list[dict] = []           # the warehouse log: every change, newest last
LOG_LIMIT = 2000


def log_entry(kind: str, delivery: str | None, summary: str, source: dict | None = None,
              **extra) -> None:
    """One line in the warehouse log (the dashboard's Log tab). Written HERE,
    on the SAP side, for every change, so nothing can change without a trace.
      kind: sap / decision / undo / conflict / problem / chat
      source: who and what caused it, sent by Runa (RunaSource)"""
    LOG.append({"id": max((e["id"] for e in LOG), default=0) + 1,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "kind": kind, "delivery": delivery, "summary": summary,
                "source": source or {}, **extra})
    del LOG[:-LOG_LIMIT]


class Source(BaseModel):
    """Not part of SAP: who and what made Runa write, for the log.
    In real S/4HANA this would live in Runa's own audit store."""
    sender: str | None = None
    text: str | None = None
    agent: str | None = None
    scores: list[float] | None = None
    option: str | None = None


class DeliveryPatch(BaseModel):
    PlannedGoodsReceipt: str | None = None
    Status: str | None = None
    DelayReasonCode: str | None = None
    SourceMessageRef: str | None = None
    DockClosingTime: str | None = None
    ResolutionNote: str | None = None
    # Set by a supervisor's choice from the Planner. In real S/4HANA the
    # carrier and the dock live elsewhere (partner functions, EWM); the mock
    # keeps them on the delivery so the demo shows one record.
    CarrierContact: str | None = None
    ReceivingDock: str | None = None
    Stage: str | None = None           # see config.STAGES
    RunaSource: Source | None = None   # for the log, not stored on the delivery


class QualityNotification(BaseModel):
    """Mirrors the shape of S/4HANA API_QUALITYNOTIFICATION."""
    InboundDelivery: str
    Material: str
    NotificationText: str
    DefectCode: str
    SourceMessageRef: str | None = None
    RunaSource: Source | None = None

class CancelRequest(BaseModel):
    """Who/what asked for a reversal — kept for the audit trail."""
    SourceMessageRef: str | None = None
    RunaSource: Source | None = None


class RunaEvent(BaseModel):
    ts: str
    sender: str
    text: str
    decision: str
    e: float = 0
    v: float = 0
    hedges: list[str] = []
    resolved: str | None = None
    doc: str | None = None
    q: str | None = None
    note: str = ""
    timings: dict = {}

class MaterialDocument(BaseModel):
    InboundDelivery: str
    Material: str
    QuantityInDeliveryUnit: float
    GoodsMovementReasonCode: str
    SourceMessageRef: str | None = None
    RunaSource: Source | None = None


@app.get(f"{SERVICE}/A_InboundDelivery")
def list_deliveries():
    """OData v2 collection read."""
    return {"d": {"results": list(DB.values())}}


@app.get(f"{SERVICE}/A_InboundDelivery('{{delivery_id}}')")
def get_delivery(delivery_id: str):
    if delivery_id not in DB:
        raise HTTPException(404, f"Inbound delivery {delivery_id} not found")
    return {"d": DB[delivery_id]}


@app.patch(f"{SERVICE}/A_InboundDelivery('{{delivery_id}}')")
def patch_delivery(delivery_id: str, patch: DeliveryPatch):
    """OData v2 update. S/4HANA returns 204 No Content; we return the entity
    so the demo can display what changed."""
    if delivery_id not in DB:
        raise HTTPException(404, f"Inbound delivery {delivery_id} not found")

    before = deepcopy(DB[delivery_id])
    # Only the fields the caller actually sent. A field sent as null is set to
    # null — that's how an undo puts back a value that was empty before.
    changes = patch.model_dump(exclude_unset=True)
    source = changes.pop("RunaSource", None)
    if changes.get("Stage") and changes["Stage"] not in STAGES:
        raise HTTPException(422, f"Stage must be one of {', '.join(STAGES)}")
    DB[delivery_id].update(changes)
    DB[delivery_id]["LastChangeDateTime"] = datetime.now().isoformat(timespec="seconds")

    CHANGE_LOG.append({
        "timestamp": DB[delivery_id]["LastChangeDateTime"],
        "entity": f"A_InboundDelivery('{delivery_id}')",
        "changes": changes,
        "before": {k: before.get(k) for k in changes},
    })
    shown = {k: [before.get(k), v] for k, v in changes.items()
             if k not in ("SourceMessageRef", "ResolutionNote", "DelayReasonCode")
             and before.get(k) != v}
    if shown:
        agent = (source or {}).get("agent") or ""
        kind = "undo" if agent == "undo" else "decision" if agent == "decision" else "sap"
        log_entry(kind, delivery_id, changes.get("ResolutionNote") or "", source,
                  changes=shown)
    return {"d": DB[delivery_id]}


@app.post(f"{SERVICE}/A_MaterialDocumentHeader")
def post_material_document(doc: MaterialDocument):
    """Goods receipt discrepancy posting."""
    number = f"50000{len(MATERIAL_DOCUMENTS) + 1:04d}"
    body = doc.model_dump()
    source = body.pop("RunaSource", None)
    record = {
        "MaterialDocument": number,
        "MaterialDocumentYear": "2026",
        "PostingDate": datetime.now().strftime("%Y-%m-%d"),
        **body,
    }
    MATERIAL_DOCUMENTS.append(record)
    log_entry("sap", doc.InboundDelivery,
              f"Kurang {doc.QuantityInDeliveryUnit:g}: dokumen {number}", source,
              document=number, problem=True)
    return {"d": record}


@app.post(f"{SERVICE}/A_MaterialDocumentHeader('{{number}}')/Cancel")
def cancel_material_document(number: str, req: CancelRequest):
    """Undo of a posting. Like real S/4HANA, nothing is deleted: a REVERSAL
    document is posted that cancels the original, and both stay on record."""
    original = next((d for d in MATERIAL_DOCUMENTS
                     if d["MaterialDocument"] == number), None)
    if not original:
        raise HTTPException(404, f"Material document {number} not found")
    if original.get("ReversalDocument"):
        raise HTTPException(409, f"Material document {number} is already cancelled")
    reversal = {
        **original,
        "MaterialDocument": f"50000{len(MATERIAL_DOCUMENTS) + 1:04d}",
        "PostingDate": datetime.now().strftime("%Y-%m-%d"),
        "QuantityInDeliveryUnit": -original["QuantityInDeliveryUnit"],
        "ReversedMaterialDocument": number,
        "SourceMessageRef": req.SourceMessageRef,
    }
    reversal.pop("RunaSource", None)
    MATERIAL_DOCUMENTS.append(reversal)
    original["ReversalDocument"] = reversal["MaterialDocument"]
    log_entry("undo", original["InboundDelivery"],
              f"Dokumen {number} dibatalkan: dokumen pembalik {reversal['MaterialDocument']}",
              req.RunaSource.model_dump() if req.RunaSource else None,
              document=reversal["MaterialDocument"])
    return {"d": reversal}


@app.post("/sap/opu/odata/sap/API_QUALITYNOTIFICATION/A_QualityNotification")
def post_quality_notification(qn: QualityNotification):
    if qn.InboundDelivery not in DB:
        raise HTTPException(404, f"Inbound delivery {qn.InboundDelivery} not found")
    number = f"20000{len(QUALITY_NOTIFICATIONS) + 1:04d}"
    body = qn.model_dump()
    source = body.pop("RunaSource", None)
    record = {
        "QualityNotification": number,
        "NotificationType": "Q2",          # S/4 type for supplier complaints
        "CreationDate": datetime.now().strftime("%Y-%m-%d"),
        "NotificationStatus": "OUTSTANDING",
        **body,
    }
    QUALITY_NOTIFICATIONS.append(record)
    log_entry("sap", qn.InboundDelivery, f"Keluhan kualitas {number}: {qn.NotificationText}",
              source, document=number, problem=True)
    return {"d": record}


@app.post("/sap/opu/odata/sap/API_QUALITYNOTIFICATION/"
          "A_QualityNotification('{number}')/Cancel")
def cancel_quality_notification(number: str, req: CancelRequest):
    """Undo of a complaint: the notification is marked cancelled, not deleted."""
    qn = next((q for q in QUALITY_NOTIFICATIONS
               if q["QualityNotification"] == number), None)
    if not qn:
        raise HTTPException(404, f"Quality notification {number} not found")
    if qn["NotificationStatus"] == "CANCELLED":
        raise HTTPException(409, f"Quality notification {number} is already cancelled")
    qn["NotificationStatus"] = "CANCELLED"
    qn["CancellationRef"] = req.SourceMessageRef
    log_entry("undo", qn["InboundDelivery"], f"Keluhan kualitas {number} dibatalkan",
              req.RunaSource.model_dump() if req.RunaSource else None, document=number)
    return {"d": qn}


@app.post("/runa/events")
def add_event(event: RunaEvent):
    """Runa reports what it just did. Old small talk is trimmed here
    (the rule is keep_events in store.py)."""
    record = {"id": max((e["id"] for e in RUNA_EVENTS), default=0) + 1,
              "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              **event.model_dump()}
    RUNA_EVENTS.append(record)
    RUNA_EVENTS[:] = keep_events(RUNA_EVENTS)
    if event.decision == "conflict":
        # The first line of what Runa posted: "⚠️ DO … tiba 23:00, tapi DOCK-02 tutup 22:00."
        first = event.text.strip().splitlines()[0].lstrip("⚠️ ").strip() if event.text else ""
        log_entry("conflict", event.resolved, first or "Bentrok dengan jam dock",
                  {"agent": "Runa"})
    elif event.decision == "handoff":
        log_entry("problem", event.resolved, "Diteruskan ke supervisor",
                  {"sender": event.sender, "text": event.text, "agent": "Runa"})
    return record

@app.get("/runa/events")
def list_events():
    """Reads and lists everything that runa did"""
    return RUNA_EVENTS

# ------------------------------------------------------------------ notebook
# Not part of SAP. Runa's notebook lives here so the bot and the dashboard
# share one copy. Saved to the database with everything else (store.py).

NOTE_STATUSES = {"proposed", "approved", "rejected", "removed"}


class NoteIn(BaseModel):
    text: str
    status: str = "proposed"       # Runa proposes; a supervisor's own note is approved
    source: str = "learned"        # seed / supervisor / learned
    by: str = "Runa"
    kind: str = "note"             # note: who is who / rule: a constraint for planning
    evidence: list[dict] = []      # the chat messages a learned rule came from


class NoteReview(BaseModel):
    status: str                    # approved / rejected / removed
    by: str


def seed_notebook():
    NOTEBOOK.clear()
    for text in NOTEBOOK_SEED:
        NOTEBOOK.append({"id": len(NOTEBOOK) + 1, "text": text, "status": "approved",
                         "source": "seed", "by": "setup", "reviewed_by": None,
                         "created": datetime.now().isoformat(timespec="seconds")})


seed_notebook()


# ------------------------------------------------------------------ storage
# Load what was saved before the last restart. Without DATABASE_URL this does
# nothing and the app starts from the demo data, as before.
STORE = Store()
COLLECTIONS = {"deliveries": DB, "material_documents": MATERIAL_DOCUMENTS,
               "quality_notifications": QUALITY_NOTIFICATIONS,
               "change_log": CHANGE_LOG, "events": RUNA_EVENTS, "notebook": NOTEBOOK,
               "facts": FACTS, "decisions": DECISIONS, "log": LOG}


def snapshot() -> dict:
    """A copy of everything, for saving. Copied with a retry because the
    request threads may be changing a list at the same moment."""
    for _ in range(5):
        try:
            return deepcopy(COLLECTIONS)
        except RuntimeError:          # "changed size during iteration"
            continue
    return deepcopy(COLLECTIONS)


_saved = STORE.load()
if _saved:
    for _key, _value in _saved.items():
        COLLECTIONS[_key].clear()
        (COLLECTIONS[_key].update if isinstance(_value, dict)
         else COLLECTIONS[_key].extend)(_value)
else:
    STORE.save(snapshot)              # first run: the database gets the demo data


@app.get("/runa/notebook")
def list_notes():
    return NOTEBOOK


@app.post("/runa/notebook")
def add_note(note: NoteIn):
    text = " ".join(note.text.split())
    if not text or len(text) > 300:
        raise HTTPException(422, "A note must be 1–300 characters")
    if note.kind not in ("note", "rule"):
        raise HTTPException(422, "kind is note or rule")
    if note.status not in ("proposed", "approved"):
        raise HTTPException(422, "A new note is either proposed or approved")
    if any(n["text"].lower() == text.lower() and n["status"] in ("proposed", "approved")
           for n in NOTEBOOK):
        raise HTTPException(409, "The notebook already has this note")
    record = {"id": len(NOTEBOOK) + 1, "text": text, "status": note.status,
              "source": note.source, "by": note.by, "kind": note.kind,
              "evidence": note.evidence[:10],
              "reviewed_by": note.by if note.status == "approved" else None,
              "created": datetime.now().isoformat(timespec="seconds")}
    NOTEBOOK.append(record)
    return record


@app.patch("/runa/notebook/{note_id}")
def review_note(note_id: int, review: NoteReview):
    note = next((n for n in NOTEBOOK if n["id"] == note_id), None)
    if not note:
        raise HTTPException(404, f"Note {note_id} not found")
    if review.status not in NOTE_STATUSES - {"proposed"}:
        raise HTTPException(422, "Status must be approved, rejected or removed")
    note["status"] = review.status
    note["reviewed_by"] = review.by
    return note


# --------------------------------------------------------------------- facts
# Not part of SAP. Constraints Runa read from the chat (facts.py): who has
# been driving since when, how many crew, which forklift is broken. Facts are
# never deleted when they stop being true; they are marked ended, so the
# rule learner and the dashboard can still see where a rule came from.

class FactsIn(BaseModel):
    facts: list[dict] = []
    ended: list[dict] = []        # [{"id": 3, "at": "...", "ref": "..."}]


@app.get("/runa/facts")
def list_facts():
    return FACTS


@app.post("/runa/facts")
def add_facts(body: FactsIn):
    created = []
    for f in body.facts:
        record = {"id": max((x["id"] for x in FACTS), default=0) + 1,
                  "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "ended_at": None, "ended_by": None, **f}
        FACTS.append(record)
        created.append(record)
    for e in body.ended:
        fact = next((x for x in FACTS if x["id"] == e.get("id")), None)
        if fact and not fact.get("ended_at"):
            fact["ended_at"], fact["ended_by"] = e.get("at"), e.get("ref")
    return {"created": created}


class LogIn(BaseModel):
    kind: str = "chat"                 # chat (a note) / problem (needs attention)
    delivery: str | None = None
    summary: str
    source: Source | None = None


@app.get("/runa/log")
def list_log(limit: int = 300):
    """The warehouse log, newest first."""
    return LOG[::-1][:max(1, min(limit, LOG_LIMIT))]


@app.post("/runa/log")
def add_log(entry: LogIn):
    """A note from the group about a delivery that isn't an SAP change
    ("kurang bubble wrap"). SAP changes are logged by runa-sap itself."""
    if entry.kind not in ("chat", "problem"):
        raise HTTPException(422, "kind is chat or problem")
    if entry.delivery and entry.delivery not in DB:
        raise HTTPException(404, f"Inbound delivery {entry.delivery} not found")
    log_entry(entry.kind, entry.delivery, " ".join(entry.summary.split())[:200],
              entry.source.model_dump() if entry.source else None,
              problem=entry.kind == "problem")
    return LOG[-1]


@app.get("/runa/decisions")
def list_decisions():
    return DECISIONS


@app.post("/runa/decisions")
def add_decision(body: dict):
    """The Planner's options for one problem, and which one a human chose.
    Kept for the audit trail and for the rule learner."""
    record = {"id": max((d["id"] for d in DECISIONS), default=0) + 1,
              "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **body}
    DECISIONS.append(record)
    return record


# ------------------------------------------------- decisions from the dashboard
# The bot keeps the open escalation (opsi a / b) and tells runa-sap about it.
# A supervisor can then pick an option on the dashboard; runa-sap checks the
# PIN and queues the choice; the bot picks it up within seconds and carries it
# out exactly as if the supervisor had typed it in Telegram.
class DecisionState(BaseModel):
    decision: dict | None = None
    chat_id: int | None = None


class DashboardChoice(BaseModel):
    delivery: str
    choice: str
    pin: str


@app.post("/runa/decision")
def set_decision(body: DecisionState):
    """From the bot: the escalation now open, or none. Needs the API key."""
    global OPEN_DECISION
    if body.decision:
        OPEN_DECISION = {**body.decision, "chat_id": body.chat_id,
                         "opened": (OPEN_DECISION or {}).get("opened")
                         if OPEN_DECISION and OPEN_DECISION.get("delivery") == body.decision.get("delivery")
                         else datetime.now(timezone.utc).isoformat(timespec="seconds")}
    else:
        OPEN_DECISION = None
        CHOICES.clear()
    return {"ok": True}


@app.post("/runa/decision/choose", status_code=202)
def choose_on_dashboard(body: DashboardChoice):
    """From the dashboard: a supervisor's choice, checked by PIN."""
    if not SUPERVISOR_PIN:
        raise HTTPException(503, "Memilih dari dashboard belum diaktifkan (RUNA_SUPERVISOR_PIN).")
    recent = [t for t in PIN_FAILURES if time.time() - t < 300]
    PIN_FAILURES[:] = recent
    if len(recent) >= 5:
        raise HTTPException(429, "Terlalu banyak PIN salah. Coba lagi dalam 5 menit.")
    if not secrets.compare_digest(body.pin.strip(), SUPERVISOR_PIN):
        PIN_FAILURES.append(time.time())
        raise HTTPException(403, "PIN supervisor salah.")
    d = OPEN_DECISION
    if not d or d.get("delivery") != body.delivery:
        raise HTTPException(409, "Keputusan ini sudah tidak menunggu.")
    choice = body.choice.strip().lower()
    if choice not in {o["label"] for o in d.get("options", [])}:
        raise HTTPException(400, "Opsi itu tidak ada.")
    CHOICES.clear()                      # one decision, one choice: the last click wins
    CHOICES.append({"delivery": body.delivery, "choice": choice, "chat_id": d.get("chat_id"),
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    d["requested"] = choice
    return {"queued": choice}


@app.post("/runa/decision/next")
def next_dashboard_choice():
    """From the bot: take the waiting dashboard choice, if any. Needs the API key."""
    return CHOICES.pop(0) if CHOICES else None


@app.post("/_debug/reset")
def debug_reset():
    """Not part of the SAP contract — puts the demo data back to the start."""
    DB.clear()
    DB.update(seed_deliveries())
    MATERIAL_DOCUMENTS.clear()
    QUALITY_NOTIFICATIONS.clear()
    CHANGE_LOG.clear()
    RUNA_EVENTS.clear()
    FACTS.clear()
    DECISIONS.clear()
    LOG.clear()
    global OPEN_DECISION
    OPEN_DECISION = None
    CHOICES.clear()
    seed_notebook()
    return {"reset": True}


@app.get("/_debug/state")
def debug_state():
    """Not part of the SAP contract — used by the demo to show what landed."""
    return {
        "storage": STORE.status,
        "deliveries": DB,
        "material_documents": MATERIAL_DOCUMENTS,
        "quality_notifications": QUALITY_NOTIFICATIONS,
        "change_log": CHANGE_LOG,
        # Runa's clock, so the dashboard's "now" line and "today" match it.
        "now": now().isoformat(timespec="seconds"),
        "live_clock": DEMO_NOW == "live",
        # The escalation waiting for a supervisor (options a/b...), if any
        "decision": OPEN_DECISION,
    }


# --------------------------------------------------------------- SAP Joule
# Joule asks Runa questions over A2A at /a2a. Read-only: see a2a_agent.py.
def live_state() -> dict:
    return {"deliveries": DB, "change_log": CHANGE_LOG,
            "material_documents": MATERIAL_DOCUMENTS,
            "quality_notifications": QUALITY_NOTIFICATIONS,
            "events": RUNA_EVENTS, "log": LOG}


try:
    from a2a_agent import add_a2a_routes
    add_a2a_routes(app, live_state)
except ImportError as e:              # a2a-sdk not installed: SAP mock still runs
    print(f"[a2a] Joule endpoint disabled: {e}")
