"""
A full dry run of the new demo story, with the REAL AI, against a LOCAL mock.

    uvicorn sap_mock:app --port 8000      # terminal 1 (local, not the cloud)
    python demo_dry_run.py                # terminal 2: 3 crew can do overtime
    python demo_dry_run.py --hard         # only 2 crew: the rules pull apart

1. Resets the local mock, and gives it the rules learned from the history
   (data/history_rules.json) as if Sari had approved them with /setuju.
2. Sends the morning messages. Runa reads the facts in them.
3. Budi reports he'll arrive at 23:00: Runa writes the new time to SAP, sees
   the dock closes at 22:00, and the Planner builds options.
4. Sari picks option a. Runa writes it to SAP.

Uses the AI from .env. Talks only to SAP_BASE, which must be the local mock.
"""
import json
import os
import sys

import httpx

import agents
from config import SAP_BASE, SAP_HEADERS
from pipeline import Runa
from trace import Trace

HERE = os.path.dirname(os.path.abspath(__file__))

MORNING = [
    ("Budi", "Pagi bu, saya sudah jalan dari Cikampek jam 5 pagi, bawa DO 4521 mie"),
    ("Agus", "Bu hari ini saya standby di pool Cikampek, siap kalau ada yang perlu diganti"),
    ("Dewi", "Bu stok mie FG-4471 tinggal 0,9 hari"),
    ("Hendra", "Bu info, malam ini yang bisa lembur 3 orang"),
]
HARD = ("Hendra", "Bu info, malam ini yang bisa lembur cuma 2 orang")
LATE = ("Budi", "Bu macet parah di tol Cikampek, saya sampai gudang jam 11 malam")


def main():
    if not SAP_BASE.startswith(("http://127.0.0.1", "http://localhost")):
        sys.exit(f"SAP_BASE is {SAP_BASE}. Run this against the LOCAL mock only.")
    try:
        httpx.post(f"{SAP_BASE}/_debug/reset", headers=SAP_HEADERS, timeout=5)
    except httpx.HTTPError:
        sys.exit("Start the local mock first:  uvicorn sap_mock:app --port 8000")

    with open(os.path.join(HERE, "data", "history_rules.json"), encoding="utf-8") as fh:
        learned = json.load(fh)["proposed"]
    for r in learned:
        agents.add_note(r["rule"], status="approved", source="learned", by="Sari",
                        kind="rule", evidence=r["evidence"])
    print(f"{len(learned)} learned rules approved (as if Sari typed /setuju)\n")

    morning = MORNING[:-1] + [HARD] if "--hard" in sys.argv else MORNING
    runa = Runa(Trace(echo=False))
    print("— Morning messages —")
    for sender, text in morning:
        replies = runa.handle(sender, text)
        found = runa.observe(sender, text)
        print(f"{sender}: {text}")
        for reply in replies:
            print(f"   Runa: {reply}")
        for f in found:
            print(f"   fact [{f['type']}] {f.get('ringkas') or f['statement']}")

    print("\n— Budi is late —")
    start = len(runa.trace.events)
    print(f"{LATE[0]}: {LATE[1]}")
    for reply in runa.handle(*LATE):
        print(f"\nRuna:\n{reply}")
    runa.observe(*LATE)
    print("\n(Planner and Reviewer, behind the scenes)")
    for e in runa.trace.events[start:]:
        if e["actor"] in ("planner", "reviewer"):
            print(f"   {e['actor']}: {e['summary']}")

    if not runa.pending_decision or not runa.pending_decision.plan:
        print("\nNo Planner options (fixed a/b used). See the lines above for why.")
        return
    print("\n— Sari chooses a —")
    for reply in runa.handle("Sari", "a aja"):
        print(f"Runa: {reply}")
    d = httpx.get(f"{SAP_BASE}/_debug/state", timeout=5).json()["deliveries"]["0080004521"]
    print(f"\nSAP now: driver {d['CarrierContact']}, unloading {d['PlannedGoodsReceipt']}, "
          f"dock closes {d['DockClosingTime']}, note: {d.get('ResolutionNote')}")


if __name__ == "__main__":
    main()
