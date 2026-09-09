"""Runa — configuration and seeded SAP master data."""
import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# If you get a model_not_found (404) error, swap these. Run check_models.py to
# find which strings your account can call.
MODEL_LISTENER = os.getenv("MODEL_LISTENER", "claude-haiku-4-5")
MODEL_RESOLVER = os.getenv("MODEL_RESOLVER", "claude-sonnet-4-6")

# Runa acts autonomously only when BOTH confidence scores clear this bar.
# Below it, the agent asks instead of guessing. This threshold is the single
# most important number in the system.
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.85"))

SAP_BASE = os.getenv("SAP_BASE", "http://127.0.0.1:8000")
SAP_SERVICE = "/sap/opu/odata/sap/API_INBOUND_DELIVERY_SRV"

TRACE_FILE = "logs/trace.jsonl"

# --------------------------------------------------------------------------
# Seeded master data. In production this is read from S/4HANA through
# AgentCore Gateway; here it seeds the mock OData service.
# --------------------------------------------------------------------------

INBOUND_DELIVERIES = {
    "0080004521": {
        "InboundDelivery": "0080004521",
        "SupplierName": "PT Sinar Baja Logistik",
        "Supplier": "0001042288",
        "Material": "FG-4471",
        "MaterialDescription": "Instant noodle carton 40x85g",
        "DeliveryQuantity": 480,
        "DeliveryQuantityUnit": "CTN",
        "PlannedGoodsReceipt": "2026-09-09T21:00:00",
        "ReceivingPlant": "1710",
        "ReceivingDock": "DOCK-02",
        "DockClosingTime": "22:00",
        "Status": "In transit",
        "CarrierContact": "Budi Santoso",
        "Route": "Cikampek - Cikarang DC",
    },
    "0080004522": {
        "InboundDelivery": "0080004522",
        "SupplierName": "CV Mitra Kemasan",
        "Supplier": "0001042301",
        "Material": "PKG-1120",
        "MaterialDescription": "Corrugated shipper 60x40x35",
        "DeliveryQuantity": 1200,
        "DeliveryQuantityUnit": "PC",
        "PlannedGoodsReceipt": "2026-09-10T08:00:00",
        "ReceivingPlant": "1710",
        "ReceivingDock": "DOCK-01",
        "DockClosingTime": "22:00",
        "Status": "In transit",
        "CarrierContact": "Rudi Hartono",
        "Route": "Bekasi - Cikarang DC",
    },
    "0080004530": {
        "InboundDelivery": "0080004530",
        "SupplierName": "PT Sinar Baja Logistik",
        "Supplier": "0001042288",
        "Material": "FG-4471",
        "MaterialDescription": "Instant noodle carton 40x85g",
        "DeliveryQuantity": 240,
        "DeliveryQuantityUnit": "CTN",
        "PlannedGoodsReceipt": "2026-09-11T14:00:00",
        "ReceivingPlant": "1710",
        "ReceivingDock": "DOCK-02",
        "DockClosingTime": "22:00",
        "Status": "Planned",
        "CarrierContact": "Budi Santoso",
        "Route": "Cikampek - Cikarang DC",
    },
}

# Per-channel memory: who is who in this operations group. In production this
# is AgentCore Memory; here it is a seeded dict.
CHANNEL_MEMORY = {
    "Budi": "Carrier driver for PT Sinar Baja Logistik. Usually runs the "
            "Cikampek route. Currently assigned to delivery 0080004521.",
    "Rudi": "Carrier driver for CV Mitra Kemasan on the Bekasi route.",
    "Sari": "Warehouse supervisor, Cikarang DC. Owns dock scheduling.",
}

EVENT_TYPES = [
    "DELIVERY_DELAY",
    "GOODS_RECEIPT_DISCREPANCY",
    "QUALITY_COMPLAINT",
]
