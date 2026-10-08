"""
Mock SAP S/4HANA OData service.

This implements the request/response shape of the real
API_INBOUND_DELIVERY_SRV OData v2 service: the same entity set name, the same
field names, and the same {"d": {...}} envelope S/4HANA returns.

Because the contract is real, pointing Runa at a live S/4HANA system is a
change to SAP_BASE in .env and nothing else.

Run with:  uvicorn sap_mock:app --port 8000
"""
from copy import deepcopy
from datetime import datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from config import INBOUND_DELIVERIES

app = FastAPI(title="Mock S/4HANA — API_INBOUND_DELIVERY_SRV")

SERVICE = "/sap/opu/odata/sap/API_INBOUND_DELIVERY_SRV"

DB = deepcopy(INBOUND_DELIVERIES)
MATERIAL_DOCUMENTS: list[dict] = []
QUALITY_NOTIFICATIONS: list[dict] = []
CHANGE_LOG: list[dict] = []


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
    changes = patch.model_dump(exclude_none=True)
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


@app.post("/sap/opu/odata/sap/API_QUALITYNOTIFICATION/A_QualityNotification")
def post_quality_notification(qn: QualityNotification):
    if qn.InboundDelivery not in DB:
        raise HTTPException(404, f"Inbound delivery {qn.InboundDelivery} not found")
    number = f"20000{len(QUALITY_NOTIFICATIONS) + 1:04d}"
    record = {
        "QualityNotification": number,
        "NotificationType": "Q2",          # S/4 type for supplier complaints
        "CreationDate": datetime.now().strftime("%Y-%m-%d"),
        **qn.model_dump(),
    }
    QUALITY_NOTIFICATIONS.append(record)
    return {"d": record}


@app.post("/_debug/reset")
def debug_reset():
    """Not part of the SAP contract — puts the demo data back to the start."""
    DB.clear()
    DB.update(deepcopy(INBOUND_DELIVERIES))
    MATERIAL_DOCUMENTS.clear()
    QUALITY_NOTIFICATIONS.clear()
    CHANGE_LOG.clear()
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
