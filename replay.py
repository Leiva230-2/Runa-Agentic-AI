"""
Scripted demo. Runs the full pipeline without Telegram.

Use this to capture your proposal trace, and as the fallback if Telegram
misbehaves during the live demo. Same agents, same SAP writes — only the
transport differs.

    uvicorn sap_mock:app --port 8000     # terminal 1
    python replay.py                     # terminal 2: the rehearsed story
    python replay.py surprises           # terminal 2: unrehearsed messages
    python replay.py conversations       # terminal 2: multi-turn threads
"""
import sys
import time

import httpx

from config import SAP_BASE
from pipeline import Runa
from trace import C, Trace

SCRIPTS = {
    # The original story: hedge -> ask -> confirm -> write -> conflict -> decide
    "demo": [
        ("Sari",  "Pagi semua, hari ini ada 3 inbound ya"),
        ("Budi",  "siap bu"),
        ("Budi",  "Pak macet parah di tol Cikampek, kayaknya baru "
                  "sampai gudang jam 11 malam"),
        ("Budi",  "sudah pasti pak, jam 23:00. sekarang sudah lewat Karawang"),
        ("Sari",  "opsi b runa"),
    ],
    # Things you did NOT rehearse — what judges will type
    "surprises": [
        ("Rudi",  "bu, truk Mitra Kemasan bakal datang lebih cepat besok, "
                  "jam 7 pagi sudah sampai"),
        ("Sari",  "DO 0080004521 kardusnya basah semua kena hujan"),
        ("Sari",  "forklift di gudang 2 rusak lagi"),
        ("Budi",  "pak DO 0080009999 telat ya"),
        ("Rudi",  "kirain hari ini libur hehe"),
    ],
    # Multi-turn threads: topic switches and the two-question budget
    "conversations": [
        ("Sari",  "DO 0080004521 kayaknya ada yang kurang beberapa dus"),   # vague -> asks
        ("Sari",  "eh forklift di gudang 2 rusak lagi"),                   # topic switch
        ("Sari",  "oh iya yang tadi, kurang 5 dus"),                       # the real answer
        ("Budi",  "pak DO 0080009999 telat ya"),                           # bad number, no time
        ("Rudi",  "kirain hari ini libur hehe"),                           # someone else
        ("Budi",  "eh salah, maksudnya yang 4521"),                        # fixes number only
        ("Budi",  "jam 23:00 pak, macet di Cikampek"),                     # now complete
        ("Sari",  "opsi a aja, perpanjang shift"),                         # decision
    ],
}



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

    name = sys.argv[1] if len(sys.argv) > 1 else "demo"
    script = SCRIPTS[name]
    httpx.post(f"{SAP_BASE}/_debug/reset", timeout=5)   # clean ledger every run

    trace = Trace()
    runa = Runa(trace)

    print(f"\n{C['bold']}RUNA — operations channel{C['off']}")
    print(f"{C['dim']}{'-' * 70}{C['off']}\n")

    for sender, message in script:
        for reply in runa.handle(sender, message):
            print(f"{'':>13}{C['agent']}{C['bold']}RUNA →{C['off']} {reply}")
        time.sleep(0.4)

    print(f"\n{C['dim']}{'-' * 70}{C['off']}")
    print(f"{C['bold']}Transactions written to SAP: {runa.written}   "
          f"Handed to a human: {runa.handoffs}{C['off']}")

    state = httpx.get(f"{SAP_BASE}/_debug/state", timeout=5).json()
    for entry in state["change_log"]:
        print(f"{C['dim']}  {entry['entity']}{C['off']}")
        for key, value in entry["changes"].items():
            before = entry["before"].get(key)
            print(f"    {key}: {before} → {C['bold']}{value}{C['off']}")

    print(f"\n{C['dim']}Full trace written to logs/trace.jsonl{C['off']}\n")


if __name__ == "__main__":
    main()
