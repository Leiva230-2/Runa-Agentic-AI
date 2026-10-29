"""
Fill the LOCAL mock with a demo day, to look at the Calendar and Log tabs.
No AI is called: the AI's answers are scripted (the same fakes the tests use),
so it is free, instant and the same every time.

    uvicorn sap_mock:app --port 8000          # terminal 1
    python seed_calendar.py                    # terminal 2: reset + the demo story
    python seed_calendar.py move 4603 20:30    # move one delivery, today
    python seed_calendar.py move 4603 2026-09-10T07:00
    python seed_calendar.py reset              # back to the starting data

The story needs the demo clock (the mock started without RUNA_NOW=live);
`move` and `reset` work on either clock.

The story: DO 4603 leaves, DO 4601 is ready then undone, DO 4605 arrives wet
(quality complaint), DO 4602 is being packed but short of bubble wrap, and
DO 4521 runs late past the dock's 22:00 close; Sari picks option a.
"""
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "x")      # nothing is sent to the AI

import httpx

from config import SAP_BASE, SAP_HEADERS, SAP_SERVICE


def local_only():
    if not SAP_BASE.startswith(("http://127.0.0.1", "http://localhost")):
        sys.exit(f"SAP_BASE is {SAP_BASE}. This script only runs against the LOCAL mock.")


def reset():
    httpx.post(f"{SAP_BASE}/_debug/reset", headers=SAP_HEADERS, timeout=5).raise_for_status()


def server_clock() -> tuple[str, bool]:
    state = httpx.get(f"{SAP_BASE}/_debug/state", timeout=5).json()
    return state.get("now", "2026-09-09T19:42:00"), state.get("live_clock", False)


def story():
    if server_clock()[1]:
        sys.exit("The scripted story is written for the demo evening (9 Sep 19:42).\n"
                 "Start the mock without RUNA_NOW=live to load it, or use `move` on the real clock.")
    import test_surprises as t
    ai, r, (budi, agus), rule = t.planner_setup()        # resets the mock first
    r.report = True
    t.fake_stages({"updates": [{"delivery": "4603", "stage": "IN_TRANSIT", "evidence": "berangkat", "confidence": 0.93}]})
    r.handle("Fajar", "DO 4603 berangkat sekarang bu"); r.track_stages("Fajar", "DO 4603 berangkat sekarang bu")
    t.fake_stages({"updates": [{"delivery": "4601", "stage": "READY", "evidence": "siap kirim", "confidence": 0.9}]})
    r.handle("Dimas", "DO 4601 sudah siap kirim"); r.track_stages("Dimas", "DO 4601 sudah siap kirim")
    r.undo("Sari")
    ai.listens = [t.event("QUALITY_COMPLAINT")]
    ai.resolves = [t.res(did="0080004605", event_type="QUALITY_COMPLAINT",
                         defect_description="Cartons wet from rain", defect_code="WET")]
    r.handle("Hendra", "DO 4605 tadi kehujanan, paket basah semua")
    msg = "DO 4602 lagi dikemas, tapi kita kekurangan bubble wrap"
    t.fake_stages({"updates": [{"delivery": "4602", "stage": "PACKING", "evidence": "lagi dikemas", "confidence": 0.95}],
                   "problems": [{"delivery": "4602", "note": "Kekurangan bubble wrap"}]})
    r.handle("Rina", msg); r.track_stages("Rina", msg)
    ai.plans = [t.plan_options(budi, agus, rule)]
    t.late_conflict(r, ai)
    ai.decisions = [{"choice": "a"}]
    r.handle("Sari", "a aja, Agus yang bawa")


def move(number: str, when: str):
    did = number if len(number) == 10 else f"008000{number[-4:]}"
    if "T" not in when:
        when = f"{server_clock()[0][:10]}T{when}:00"[:19]
    body = {"PlannedGoodsReceipt": when,
            "RunaSource": {"sender": "Tes", "text": f"(tes) DO {did[-4:]} pindah ke {when}", "agent": "test"}}
    resp = httpx.patch(f"{SAP_BASE}{SAP_SERVICE}/A_InboundDelivery('{did}')",
                       json=body, headers=SAP_HEADERS, timeout=5)
    print(resp.status_code, resp.text[:200])


if __name__ == "__main__":
    local_only()
    args = sys.argv[1:]
    if args[:1] == ["reset"]:
        reset(); print("reset to the starting data")
    elif args[:1] == ["move"] and len(args) == 3:
        move(args[1], args[2])
    elif not args:
        story(); print("demo day loaded: open the Calendar tab")
    else:
        sys.exit(__doc__)
