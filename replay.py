"""
Scripted demo. Runs the full pipeline without Telegram.

Use this to capture your proposal trace, and as the fallback if Telegram
misbehaves during the live demo. Same agents, same SAP writes — only the
transport differs.

    uvicorn sap_mock:app --port 8000     # terminal 1
    python replay.py                     # terminal 2
"""
import time

import httpx

from config import SAP_BASE
from pipeline import Runa
from trace import C, Trace

SCRIPT = [
    ("Sari",  "Pagi semua, hari ini ada 3 inbound ya"),          # noise
    ("Budi",  "siap bu"),                                        # noise
    ("Budi",  "Pak macet parah di tol Cikampek, kayaknya baru "
              "sampai gudang jam 11 malam"),                     # hedged event
    ("Budi",  "sudah pasti pak, jam 23:00. sekarang sudah lewat Karawang"),  # firm
]


def wait_for_sap(retries: int = 10) -> bool:
    for _ in range(retries):
        try:
            httpx.get(f"{SAP_BASE}/_debug/state", timeout=2)
            return True
        except Exception:
            time.sleep(1)
    return False


def main():
    if not wait_for_sap():
        print(f"{C['conflict']}SAP mock not reachable at {SAP_BASE}.{C['off']}")
        print("Start it first:  uvicorn sap_mock:app --port 8000")
        return

    trace = Trace()
    runa = Runa(trace)

    print(f"\n{C['bold']}RUNA — operations channel{C['off']}")
    print(f"{C['dim']}{'-' * 70}{C['off']}\n")

    for sender, message in SCRIPT:
        for reply in runa.handle(sender, message):
            print(f"{'':>13}{C['agent']}{C['bold']}RUNA →{C['off']} {reply}")
        time.sleep(0.4)

    print(f"\n{C['dim']}{'-' * 70}{C['off']}")
    print(f"{C['bold']}Transactions written to SAP: {runa.written}{C['off']}")

    state = httpx.get(f"{SAP_BASE}/_debug/state", timeout=5).json()
    for entry in state["change_log"]:
        print(f"{C['dim']}  {entry['entity']}{C['off']}")
        for key, value in entry["changes"].items():
            before = entry["before"].get(key)
            print(f"    {key}: {before} → {C['bold']}{value}{C['off']}")

    print(f"\n{C['dim']}Full trace written to logs/trace.jsonl{C['off']}\n")


if __name__ == "__main__":
    main()
