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
from copy import deepcopy
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import INBOUND_DELIVERIES, NOTEBOOK_SEED, RUNA_API_KEY
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
    if (RUNA_API_KEY and request.method not in ("GET", "HEAD", "OPTIONS")
            and not request.url.path.startswith("/a2a")):
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

DB = deepcopy(INBOUND_DELIVERIES)
MATERIAL_DOCUMENTS: list[dict] = []
QUALITY_NOTIFICATIONS: list[dict] = []
CHANGE_LOG: list[dict] = []
RUNA_EVENTS: list[dict] = []
NOTEBOOK: list[dict] = []      # Runa's notebook (not SAP — Runa's own memory)
FACTS: list[dict] = []         # what Runa read from the chat about constraints
DECISIONS: list[dict] = []     # options the Planner offered, and what a human chose


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


class QualityNotification(BaseModel):
    """Mirrors the shape of S/4HANA API_QUALITYNOTIFICATION."""
    InboundDelivery: str
    Material: str
    NotificationText: str
    DefectCode: str
    SourceMessageRef: str | None = None

class CancelRequest(BaseModel):
    """Who/what asked for a reversal — kept for the audit trail."""
    SourceMessageRef: str | None = None


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
    DB[delivery_id].update(changes)
    DB[delivery_id]["LastChangeDateTime"] = datetime.now().isoformat(timespec="seconds")

    CHANGE_LOG.append({
        "timestamp": DB[delivery_id]["LastChangeDateTime"],
        "entity": f"A_InboundDelivery('{delivery_id}')",
        "changes": changes,
        "before": {k: before.get(k) for k in changes},
    })
    return {"d": DB[delivery_id]}


@app.post(f"{SERVICE}/A_MaterialDocumentHeader")
def post_material_document(doc: MaterialDocument):
    """Goods receipt discrepancy posting."""
    number = f"50000{len(MATERIAL_DOCUMENTS) + 1:04d}"
    record = {
        "MaterialDocument": number,
        "MaterialDocumentYear": "2026",
        "PostingDate": datetime.now().strftime("%Y-%m-%d"),
        **doc.model_dump(),
    }
    MATERIAL_DOCUMENTS.append(record)
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
    MATERIAL_DOCUMENTS.append(reversal)
    original["ReversalDocument"] = reversal["MaterialDocument"]
    return {"d": reversal}


@app.post("/sap/opu/odata/sap/API_QUALITYNOTIFICATION/A_QualityNotification")
def post_quality_notification(qn: QualityNotification):
    if qn.InboundDelivery not in DB:
        raise HTTPException(404, f"Inbound delivery {qn.InboundDelivery} not found")
    number = f"20000{len(QUALITY_NOTIFICATIONS) + 1:04d}"
    record = {
        "QualityNotification": number,
        "NotificationType": "Q2",          # S/4 type for supplier complaints
        "CreationDate": datetime.now().strftime("%Y-%m-%d"),
        "NotificationStatus": "OUTSTANDING",
        **qn.model_dump(),
    }
    QUALITY_NOTIFICATIONS.append(record)
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
               "facts": FACTS, "decisions": DECISIONS}


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


@app.post("/_debug/reset")
def debug_reset():
    """Not part of the SAP contract — puts the demo data back to the start."""
    DB.clear()
    DB.update(deepcopy(INBOUND_DELIVERIES))
    MATERIAL_DOCUMENTS.clear()
    QUALITY_NOTIFICATIONS.clear()
    CHANGE_LOG.clear()
    RUNA_EVENTS.clear()
    FACTS.clear()
    DECISIONS.clear()
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
    }


# --------------------------------------------------------------- SAP Joule
# Joule asks Runa questions over A2A at /a2a. Read-only: see a2a_agent.py.
def live_state() -> dict:
    return {"deliveries": DB, "change_log": CHANGE_LOG,
            "material_documents": MATERIAL_DOCUMENTS,
            "quality_notifications": QUALITY_NOTIFICATIONS,
            "events": RUNA_EVENTS}


try:
    from a2a_agent import add_a2a_routes
    add_a2a_routes(app, live_state)
except ImportError as e:              # a2a-sdk not installed: SAP mock still runs
    print(f"[a2a] Joule endpoint disabled: {e}")
