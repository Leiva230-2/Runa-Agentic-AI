"""
Mock SAP S/4HANA OData service.

This implements the request/response shape of the real
API_INBOUND_DELIVERY_SRV OData v2 service: the same entity set name, the same
field names, and the same {"d": {...}} envelope S/4HANA returns.

Because the contract is real, pointing Runa at a live S/4HANA system is a
change to SAP_BASE in .env and nothing else.

Run with:  uvicorn sap_mock:app --port 8000
"""
import secrets
from copy import deepcopy
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import INBOUND_DELIVERIES, NOTEBOOK_SEED, RUNA_API_KEY

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
    if RUNA_API_KEY and request.method not in ("GET", "HEAD", "OPTIONS"):
        given = request.headers.get("X-Runa-Key", "")
        if not secrets.compare_digest(given, RUNA_API_KEY):
            return JSONResponse({"detail": "Missing or wrong X-Runa-Key"},
                                status_code=401)
    return await call_next(request)


SERVICE = "/sap/opu/odata/sap/API_INBOUND_DELIVERY_SRV"

DB = deepcopy(INBOUND_DELIVERIES)
MATERIAL_DOCUMENTS: list[dict] = []
QUALITY_NOTIFICATIONS: list[dict] = []
CHANGE_LOG: list[dict] = []
RUNA_EVENTS: list[dict] = []
NOTEBOOK: list[dict] = []      # Runa's notebook (not SAP — Runa's own memory)


class DeliveryPatch(BaseModel):
    PlannedGoodsReceipt: str | None = None
    Status: str | None = None
    DelayReasonCode: str | None = None
    SourceMessageRef: str | None = None
    DockClosingTime: str | None = None
    ResolutionNote: str | None = None


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
    """Runa reports what it just did."""
    record = {"id": len(RUNA_EVENTS) + 1, **event.model_dump()}
    RUNA_EVENTS.append(record)
    return record

@app.get("/runa/events")
def list_events():
    """Reads and lists everything that runa did"""
    return RUNA_EVENTS

# ------------------------------------------------------------------ notebook
# Not part of SAP. Runa's notebook lives here so the bot and the dashboard
# share one copy. In production: SAP HANA Cloud, so it survives restarts.

NOTE_STATUSES = {"proposed", "approved", "rejected", "removed"}


class NoteIn(BaseModel):
    text: str
    status: str = "proposed"       # Runa proposes; a supervisor's own note is approved
    source: str = "learned"        # seed / supervisor / learned
    by: str = "Runa"


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


@app.get("/runa/notebook")
def list_notes():
    return NOTEBOOK


@app.post("/runa/notebook")
def add_note(note: NoteIn):
    text = " ".join(note.text.split())
    if not text or len(text) > 300:
        raise HTTPException(422, "A note must be 1–300 characters")
    if note.status not in ("proposed", "approved"):
        raise HTTPException(422, "A new note is either proposed or approved")
    if any(n["text"].lower() == text.lower() and n["status"] in ("proposed", "approved")
           for n in NOTEBOOK):
        raise HTTPException(409, "The notebook already has this note")
    record = {"id": len(NOTEBOOK) + 1, "text": text, "status": note.status,
              "source": note.source, "by": note.by,
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


@app.post("/_debug/reset")
def debug_reset():
    """Not part of the SAP contract — puts the demo data back to the start."""
    DB.clear()
    DB.update(deepcopy(INBOUND_DELIVERIES))
    MATERIAL_DOCUMENTS.clear()
    QUALITY_NOTIFICATIONS.clear()
    CHANGE_LOG.clear()
    RUNA_EVENTS.clear()
    seed_notebook()
    return {"reset": True}


@app.get("/_debug/state")
def debug_state():
    """Not part of the SAP contract — used by the demo to show what landed."""
    return {
        "deliveries": DB,
        "material_documents": MATERIAL_DOCUMENTS,
        "quality_notifications": QUALITY_NOTIFICATIONS,
        "change_log": CHANGE_LOG,
    }
