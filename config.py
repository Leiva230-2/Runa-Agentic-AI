"""Runa — configuration and seeded SAP master data."""
import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# ---- Where Runa's brain runs -------------------------------------------
#   sapaicore — Claude on SAP AI Core (Generative AI Hub), served via AWS Bedrock
#   anthropic — the Anthropic API directly: the fallback if AI Core misbehaves
AI_PROVIDER = os.getenv("AI_PROVIDER", "anthropic").strip().lower()

AICORE_CLIENT_ID = os.getenv("AICORE_CLIENT_ID")
AICORE_CLIENT_SECRET = os.getenv("AICORE_CLIENT_SECRET")
AICORE_AUTH_URL = os.getenv("AICORE_AUTH_URL")
AICORE_API_URL = os.getenv("AICORE_API_URL")
AICORE_RESOURCE_GROUP = os.getenv("AICORE_RESOURCE_GROUP", "default")
# anthropic--claude-4.6-sonnet in the hackathon's AI Core. AI Core has no
# Haiku, so on sapaicore the Listener and the Resolver both use this one.
AICORE_DEPLOYMENT = os.getenv("AICORE_DEPLOYMENT", "dc9594f579d3e4c1")

# Anthropic API models (only used when AI_PROVIDER=anthropic).
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
        "DockOpeningTime": "06:00",
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
        "DockOpeningTime": "06:00",
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
        "DockOpeningTime": "06:00",
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

# The "forms" Runa knows how to fill in. Anything work-related outside these
# is OTHER_OPERATIONAL: Runa understands it, but forwards it to a human.
# Everyday names people use for each material. In production these come from
# the material master (descriptions and synonyms). Only DISTINCTIVE names:
# "kardus" alone is too generic — every delivery here comes in cartons.
MATERIAL_ALIASES = {
    "FG-4471": ["mie", "mi instan", "indomie", "noodle", "noodles"],
    "PKG-1120": ["shipper", "corrugated", "kardus kosong", "karton kosong"],
}

EVENT_TYPES = [
    "ETA_CHANGE",                 # arriving earlier OR later than planned
    "GOODS_RECEIPT_DISCREPANCY",  # arrived short
    "QUALITY_COMPLAINT",          # arrived damaged, wet, wrong item
    "OTHER_OPERATIONAL",          # work-related, but no form for it
]
SUPPORTED_EVENTS = {"ETA_CHANGE", "GOODS_RECEIPT_DISCREPANCY", "QUALITY_COMPLAINT"}

# ---- The rulebook. Code enforces these; the AI never decides them. ----
MAX_ETA_SHIFT_HOURS = 24      # moving a delivery by more than this needs a human
MAX_SHORTAGE_RATIO = 0.5      # "short" more than half the delivery? a human checks
MAX_QUESTIONS_PER_EVENT = 2   # after this many questions, a human takes over

# Who may choose an option (a/b) when Runa escalates a conflict.
# In Telegram, the group's ADMINS are added automatically — make your
# supervisors admins of the ops group. This list is the fallback for the
# replay scripts and tests, which have no Telegram. Comma-separated names.
SUPERVISORS = [s.strip() for s in os.getenv("RUNA_SUPERVISORS", "Sari").split(",")
               if s.strip()]

# The demo story takes place on this evening, and the seed data matches it.
# Set RUNA_NOW=live in .env to use the real clock (with real SAP data).
DEMO_NOW = os.getenv("RUNA_NOW", "2026-09-09T19:42:00")


def now():
    from datetime import datetime
    if DEMO_NOW == "live":
        return datetime.now().replace(microsecond=0)
    return datetime.fromisoformat(DEMO_NOW)
