"""Runa — configuration and seeded SAP master data."""
import json
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

# The lock on runa-sap. Anyone may READ (the dashboard only reads), but
# changing anything needs this key. Empty = no lock, for local development.
# On the cloud, give runa-sap and runa-bot the SAME value with cf set-env.
# In real S/4HANA this job is done by SAP's own login (OAuth / technical user).
RUNA_API_KEY = os.getenv("RUNA_API_KEY", "")
SAP_HEADERS = {"X-Runa-Key": RUNA_API_KEY} if RUNA_API_KEY else {}
# The supervisor's PIN for choosing an option on the dashboard. The dashboard
# is a public page and can't hold the API key, so a choice made there needs
# this PIN (set it on runa-sap). Not set = choosing on the dashboard is off.
SUPERVISOR_PIN = os.getenv("RUNA_SUPERVISOR_PIN", "")
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
        "Stage": "IN_TRANSIT",
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
        "Stage": "READY",
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
        "Stage": "PACKING",
        "CarrierContact": "Budi Santoso",
        "Route": "Cikampek - Cikarang DC",
    },
}


def _delivery(number, supplier, supplier_id, material, description, qty, unit,
              eta, dock, status, stage, driver, route):
    return {"InboundDelivery": number, "SupplierName": supplier, "Supplier": supplier_id,
            "Material": material, "MaterialDescription": description,
            "DeliveryQuantity": qty, "DeliveryQuantityUnit": unit,
            "PlannedGoodsReceipt": eta, "ReceivingPlant": "1710", "ReceivingDock": dock,
            "DockOpeningTime": "06:00", "DockClosingTime": "22:00", "Status": status,
            "Stage": stage, "CarrierContact": driver, "Route": route}


# More inbound deliveries, so the warehouse looks like a real one on the Log
# page. Other suppliers and other drivers than the demo story (Budi, Rudi),
# so they don't make Runa unsure about which delivery a driver means.
INBOUND_DELIVERIES.update({d["InboundDelivery"]: d for d in [
    _delivery("0080004601", "PT Plastik Jaya", "0001043310", "PKG-2210",
              "PET bottle 600 ml", 3000, "PC", "2026-09-10T08:00:00", "DOCK-01",
              "Planned", "PACKING", "Dimas Saputra", "Karawang - Cikarang DC"),
    _delivery("0080004602", "PT Kimia Sentosa", "0001043355", "FG-5520",
              "Liquid soap 1 L carton", 600, "CTN", "2026-09-11T10:00:00", "DOCK-01",
              "Planned", "PRODUCTION", "Eko Wijaya", "Bekasi Timur - Cikarang DC"),
    _delivery("0080004603", "PT Sumber Pangan", "0001043402", "RM-3105",
              "Refined sugar 50 kg sack", 200, "SAK", "2026-09-10T13:00:00", "DOCK-02",
              "Planned", "READY", "Fajar Nugroho", "Cikarang Utara - Cikarang DC"),
    _delivery("0080004604", "CV Karya Logam", "0001043417", "PKG-4410",
              "Tin can 330 ml", 5000, "PC", "2026-09-09T18:30:00", "DOCK-01",
              "In transit", "AT_GATE", "Gilang Ramadhan", "Cibitung - Cikarang DC"),
    _delivery("0080004605", "PT Plastik Jaya", "0001043310", "PKG-2230",
              "Bottle cap 28 mm", 20000, "PC", "2026-09-09T17:00:00", "DOCK-02",
              "In transit", "UNLOADING", "Dimas Saputra", "Karawang - Cikarang DC"),
    _delivery("0080004606", "PT Kertas Nusantara", "0001043450", "PKG-5120",
              "Label roll 80 x 50 mm", 120, "ROL", "2026-09-09T09:00:00", "DOCK-02",
              "Received", "RECEIVED", "Hadi Kurniawan", "Cikarang Barat - Cikarang DC"),
]})

# The life of an inbound delivery, in order. Runa may only move a delivery
# FORWARD through these (stages.py). A step back needs a human.
STAGES = ["PRODUCTION", "PACKING", "READY", "IN_TRANSIT", "AT_GATE",
          "UNLOADING", "RECEIVED"]

# The second AI model, from a different vendor, on the same SAP AI Core:
# it reads delivery stages and reviews the Planner's options. If it fails,
# Claude does the job instead.
AICORE_SECOND_DEPLOYMENT = os.getenv("AICORE_SECOND_DEPLOYMENT", "d1a82575de60e02a")
SECOND_MODEL_NAME = os.getenv("SECOND_MODEL_NAME", "GPT-5.4")

# Runa's notebook: what this company knows that SAP doesn't — who is who in
# the group, nicknames, routes. notebook.json holds the starting notes; the
# live notebook (with everything learned since) lives in runa-sap.
NOTEBOOK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notebook.json")
with open(NOTEBOOK_FILE, encoding="utf-8") as _fh:
    NOTEBOOK_SEED: list[str] = json.load(_fh)["notes"]
NOTEBOOK_LIMIT = 25        # at most this many notes go to the AI per message

# The "forms" Runa knows how to fill in. Anything work-related outside these
# is OTHER_OPERATIONAL: Runa understands it, but forwards it to a human.
# Everyday names people use for each material. In production these come from
# the material master (descriptions and synonyms). Only DISTINCTIVE names:
# "kardus" alone is too generic — every delivery here comes in cartons.
MATERIAL_ALIASES = {
    "FG-4471": ["mie", "mi instan", "indomie", "noodle", "noodles"],
    "PKG-1120": ["shipper", "corrugated", "kardus kosong", "karton kosong"],
    "PKG-2210": ["botol pet", "botol 600"],
    "FG-5520": ["sabun", "sabun cair", "soap"],
    "RM-3105": ["gula", "sugar"],
    "PKG-4410": ["kaleng", "tin can"],
    "PKG-2230": ["tutup botol"],
    "PKG-5120": ["label"],
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
# Set RUNA_NOW=live to use the real clock instead: Jakarta time (RUNA_TZ_OFFSET,
# default +7, because the cloud servers run on UTC), and the seed deliveries
# moved to today, so the story happens on the real day. Set it on BOTH apps
# (runa-sap and runa-bot) so the dashboard and Runa agree on the time.
DEMO_NOW = os.getenv("RUNA_NOW", "2026-09-09T19:42:00")
DEMO_DAY = "2026-09-09"                      # the day the seed data is written for
TZ_OFFSET_HOURS = float(os.getenv("RUNA_TZ_OFFSET", "7"))


def now():
    from datetime import datetime, timedelta, timezone
    if DEMO_NOW == "live":
        local = timezone(timedelta(hours=TZ_OFFSET_HOURS))
        return datetime.now(local).replace(tzinfo=None, microsecond=0)
    return datetime.fromisoformat(DEMO_NOW)


def seed_deliveries() -> dict:
    """The starting deliveries, a fresh copy. With the real clock, every
    arrival time moves by the same number of days, so the demo day is today."""
    from copy import deepcopy
    from datetime import date, datetime, timedelta
    seed = deepcopy(INBOUND_DELIVERIES)
    shift = (now().date() - date.fromisoformat(DEMO_DAY)).days
    if shift:
        for d in seed.values():
            moved = datetime.fromisoformat(d["PlannedGoodsReceipt"]) + timedelta(days=shift)
            d["PlannedGoodsReceipt"] = moved.isoformat(timespec="seconds")
    return seed
