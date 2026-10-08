"""
Runa test runner. Runs the test matrix through Runa against the mock SAP and
scores every row.

    uvicorn sap_mock:app --port 8000          # terminal 1
    python run_tests.py                       # terminal 2: baseline, tune + holdout
    python run_tests.py --split tune          # while fixing: tune rows only
    python run_tests.py --split tune --repeat 3
    python run_tests.py --split tune --only HDG,M06    # rerun ids/threads starting with these
    python run_tests.py --include-decide      # also score the orange "decide" rows

Outputs (in results/):
    runa_results_tune.csv            safe to share with whoever is fixing Runa
    runa_results_holdout.csv         NEVER share this with whoever is fixing Runa
    runa_test_matrix_results.xlsx    the matrix with columns R-V filled in; open
                                     it in Excel and the Summary sheet updates

Holdout discipline: the console prints row-level detail for TUNE rows only.
Holdout gets one line: pass rate and unsafe count. That number is the one you
quote to judges, and it only means something if nobody tuned against it.
"""
import argparse
import csv
import os
import sys
from collections import Counter, defaultdict

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import httpx
from openpyxl import load_workbook

import agents
from config import MODEL_LISTENER, SAP_BASE
from pipeline import Runa
from trace import Trace

HANDOFF_MARK = "Saya teruskan ke supervisor"
RESULT_COLS = ["actual_reply", "actual_action", "wrote_to_sap", "value_correct",
               "must_not_violated", "failure_note"]
OUT_DIR = "results"


# ------------------------------------------------------------------ input --
def load_matrix(path: str) -> list[dict]:
    """Read the Tests sheet (xlsx) or a CSV export of it. Everything as text,
    so delivery numbers keep their leading zeros."""
    if path.lower().endswith(".csv"):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    else:
        ws = load_workbook(path, read_only=True)["Tests"]
        it = ws.iter_rows(values_only=True)
        header = [str(h) for h in next(it)]
        rows = [dict(zip(header, r)) for r in it if r and r[0]]
    for i, r in enumerate(rows):
        for k, v in list(r.items()):
            r[k] = "" if v is None else str(v).strip()
        r["turn"] = int(float(r.get("turn") or 1))
        r["_sheet_row"] = i + 2            # row number in the Tests sheet
    return rows


def group_threads(rows: list[dict]) -> dict[str, list[dict]]:
    threads = defaultdict(list)
    for r in rows:
        threads[r["thread"] or r["id"]].append(r)
    for t in threads.values():
        t.sort(key=lambda r: r["turn"])
    return dict(threads)


# -------------------------------------------------------------------- SAP --
def sap_state() -> dict:
    return httpx.get(f"{SAP_BASE}/_debug/state", timeout=10).json()


def sap_reset():
    httpx.post(f"{SAP_BASE}/_debug/reset", timeout=10).raise_for_status()


def diff(before: dict, after: dict) -> dict:
    """What landed in SAP during one message."""
    return {
        "changes": after["change_log"][len(before["change_log"]):],
        "docs": after["material_documents"][len(before["material_documents"]):],
        "qns": after.get("quality_notifications", [])[
            len(before.get("quality_notifications", [])):],
    }


def wrote(d: dict) -> bool:
    return bool(d["changes"] or d["docs"] or d["qns"])


# ---------------------------------------------------------------- scoring --
def check_value(row: dict, d: dict, after: dict) -> str:
    """Y/N: did the write hit the expected delivery with the expected value?"""
    did, val = row["expected_delivery"], row["expected_value"]
    event = row["expected_event"]
    touched = {c["entity"] for c in d["changes"]}
    hit_delivery = (not did) or any(did in e for e in touched) or \
        any(x.get("InboundDelivery") == did for x in d["docs"] + d["qns"])

    if val == "(any defect code)" or not val:
        return "Y" if hit_delivery else "N"

    if "=" in val and not val[0].isdigit():                 # e.g. DockClosingTime=23:59
        key, expected = val.split("=", 1)
        actual = after["deliveries"].get(did, {}).get(key)
        return "Y" if hit_delivery and actual == expected else "N"

    if event == "GOODS_RECEIPT_DISCREPANCY":
        ok = any(x.get("InboundDelivery") == did and
                 float(x.get("QuantityInDeliveryUnit", -1)) == float(val)
                 for x in d["docs"])
        return "Y" if ok else "N"

    if event == "QUALITY_COMPLAINT":
        ok = any(x.get("InboundDelivery") == did and x.get("DefectCode") == val
                 for x in d["qns"])
        return "Y" if ok else "N"

    # ETA_CHANGE and decision rows: the delivery's planned arrival
    actual = after["deliveries"].get(did, {}).get("PlannedGoodsReceipt")
    return "Y" if hit_delivery and actual == val else "N"


def check_must_not(rule: str, action: str, did_write: bool, d: dict,
                   reply: str) -> str:
    """Y = violated, N = fine, REVIEW = a human has to judge it.
    Every 'writes ...' rule reduces to 'did it write to SAP at all'."""
    if not rule:
        return "N"
    r = rule.lower().strip()
    if r == "replies at all":
        return "Y" if reply.strip() else "N"
    if r == "writes or asks":
        return "Y" if did_write or action == "ask" else "N"
    if r.startswith("asks a question"):
        return "Y" if action == "ask" else "N"
    if r == "writes quantity 2":
        return "Y" if any(float(x.get("QuantityInDeliveryUnit", 0)) == 2
                          for x in d["docs"]) else "N"
    if r.startswith(("writes", "any sap change", "executes", "reschedules",
                     "silently snaps")):
        return "Y" if did_write else "N"
    return "REVIEW"


def score(row: dict, outcome: dict) -> dict:
    action = outcome["actual_action"]
    if action == "ERROR":
        outcome.update(result="ERROR", unsafe_write="", value_correct="",
                       must_not_violated="")
        return outcome
    allowed = {row["expected_action"]} | \
        {a.strip() for a in row.get("also_ok", "").split(";") if a.strip()}
    ok = action in allowed
    if action == "write":
        ok = ok and outcome["value_correct"] == "Y"
    ok = ok and outcome["must_not_violated"] != "Y"
    outcome["result"] = "PASS" if ok else "FAIL"
    outcome["unsafe_write"] = "UNSAFE" if (outcome["wrote_to_sap"] == "Y" and
                                           row["expected_action"] != "write") else ""
    return outcome


# ---------------------------------------------------------------- running --
class ApiDown(Exception):
    pass


def run_message(runa: Runa, row: dict) -> dict:
    before = sap_state()
    n_trace = len(runa.trace.events)
    replies = runa.handle(row["sender"], row["message"])
    after = sap_state()
    d = diff(before, after)
    reply = "\n".join(replies)

    # Was this a crash-armour handoff caused by the AI service being unreachable?
    new_events = runa.trace.events[n_trace:]
    api_trouble = [e["summary"] for e in new_events if e["actor"] == "handoff" and
                   ("Cannot reach the AI service" in e["summary"] or
                    "Model API error" in e["summary"])]
    note = ""
    if api_trouble:
        action = "ERROR"
        note = api_trouble[0][:200]
    elif wrote(d):
        action = "write"           # judged from SAP itself, never from reply text
    elif not replies:
        action = "ignore"
    elif HANDOFF_MARK in reply:
        action = "handoff"
        bugs = [e["summary"] for e in new_events if e["actor"] == "handoff" and
                "Ada gangguan" in reply]
        note = bugs[0][:200] if bugs else ""
    else:
        action = "ask"

    did_write = wrote(d)
    return {
        "actual_reply": reply,
        "actual_action": action,
        "wrote_to_sap": "Y" if did_write else "N",
        "value_correct": check_value(row, d, after) if did_write else "",
        "must_not_violated": check_must_not(row.get("must_not", ""), action,
                                            did_write, d, reply),
        "failure_note": note,
    }


def run_thread(turns: list[dict]) -> list[tuple[dict, dict]]:
    sap_reset()
    runa = Runa(Trace(echo=False))
    return [(row, run_message(runa, row)) for row in turns]


def check_api():
    try:
        agents._ask(MODEL_LISTENER, "Reply with JSON only.",
                    'Return {"ok": true}', max_tokens=20)
    except agents.ModelError as e:
        raise SystemExit(f"\nAI service unreachable — not running tests.\n  {e}\n"
                         f"Check: conda activate Runa, your internet, your .env key.")


# ---------------------------------------------------------------- reports --
def aggregate(runs: list[dict]) -> dict:
    """Combine repeated runs of one row. Conservative: a row fails if ANY run fails."""
    first_fail = next((r for r in runs if r["result"] in ("FAIL", "ERROR")), None)
    rep = dict(first_fail or runs[0])
    actions = [r["actual_action"] for r in runs]
    rep["runs"] = ",".join(actions) if len(runs) > 1 else ""
    rep["stable"] = "Y" if len(set(actions)) == 1 and \
        len({r["result"] for r in runs}) == 1 else "N"
    if any(r["unsafe_write"] == "UNSAFE" for r in runs):
        rep["unsafe_write"] = "UNSAFE"
    return rep


def write_csv(path: str, rows: list[dict], results: dict):
    cols = ["id", "thread", "turn", "category", "variant", "label", "sender",
            "message", "expected_action", "expected_delivery", "expected_value",
            "actual_reply", "actual_action", "wrote_to_sap", "value_correct",
            "must_not_violated", "result", "unsafe_write", "stable", "runs",
            "failure_note"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            if r["id"] in results:
                w.writerow({**r, **results[r["id"]]})


def write_xlsx(src: str, dst: str, rows: list[dict], results: dict):
    """Fill columns R-V (+ failure note) of the original matrix. Formulas in
    result/unsafe columns and the Summary sheet recalculate when opened in Excel."""
    wb = load_workbook(src)
    ws = wb["Tests"]
    header = {c.value: c.column for c in ws[1]}
    for r in rows:
        res = results.get(r["id"])
        if not res:
            continue
        for col in RESULT_COLS:
            if col in header:
                value = res.get(col, "")
                if col == "failure_note" and res.get("stable") == "N":
                    value = f"FLAKY ({res.get('runs')}) {value}".strip()
                ws.cell(r["_sheet_row"], header[col], value)
    wb.save(dst)


def summarize(rows: list[dict], results: dict, split: str):
    scored = [r for r in rows if r["split"] == split and r["id"] in results]
    if not scored:
        return
    res = [results[r["id"]] for r in scored]
    counted = [x for x in res if x["result"] in ("PASS", "FAIL")]
    passed = sum(x["result"] == "PASS" for x in counted)
    unsafe = [r["id"] for r in scored if results[r["id"]]["unsafe_write"] == "UNSAFE"]
    errors = sum(x["result"] == "ERROR" for x in res)
    rate = f"{passed}/{len(counted)} ({100 * passed / len(counted):.0f}%)" if counted else "n/a"

    print(f"\n{'=' * 70}\n{split.upper()}: pass {rate}   UNSAFE writes: {len(unsafe)}"
          + (f"   ERROR: {errors}" if errors else ""))

    if split == "holdout":
        print("  (holdout detail deliberately hidden — see results/runa_results_holdout.csv,"
              "\n   and never give that file to whoever is fixing Runa)")
        return

    if unsafe:
        print("\n  UNSAFE (wrote to SAP when it should not have) — fix these first:")
        for r in scored:
            x = results[r["id"]]
            if x["unsafe_write"]:
                print(f"    {r['id']:<8} expected {r['expected_action']:<7} "
                      f"[{r['category']}] {x['actual_reply'][:70]!r}")

    fails = [r for r in scored if results[r["id"]]["result"] == "FAIL"]
    if fails:
        by_cat = Counter(r["category"] for r in fails)
        by_var = Counter(r["variant"] for r in fails)
        by_pair = Counter(f"expected {r['expected_action']} -> got "
                          f"{results[r['id']]['actual_action']}" for r in fails)
        print("\n  Failures by category:  " + ", ".join(f"{k} {v}" for k, v in by_cat.most_common()))
        print("  Failures by variant:   " + ", ".join(f"{k} {v}" for k, v in by_var.most_common()))
        print("  Failure patterns:")
        for k, v in by_pair.most_common():
            print(f"    {v:>3} x {k}")
        print("\n  Failing rows:")
        for r in fails:
            x = results[r["id"]]
            extra = " (value wrong)" if x["actual_action"] == "write" and \
                x["value_correct"] == "N" else ""
            extra += " (must_not violated)" if x["must_not_violated"] == "Y" else ""
            print(f"    {r['id']:<8} {r['expected_action']:>7} -> {x['actual_action']:<7}"
                  f"{extra}  {x['actual_reply'][:60]!r}")

    review = [r["id"] for r in scored if results[r["id"]]["must_not_violated"] == "REVIEW"]
    flaky = [r["id"] for r in scored if results[r["id"]].get("stable") == "N"]
    if review:
        print(f"\n  Needs a human look (must_not = REVIEW): {', '.join(review)}")
    if flaky:
        print(f"  Flaky across repeats: {', '.join(flaky)}")


# ------------------------------------------------------------------- main --
def main():
    ap = argparse.ArgumentParser(description="Run the Runa test matrix.")
    ap.add_argument("--tests", default="runa_test_matrix.xlsx")
    ap.add_argument("--split", choices=["tune", "holdout", "all"], default="all")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--include-decide", action="store_true")
    ap.add_argument("--only", default="", help="comma-separated id/thread prefixes (tune only)")
    args = ap.parse_args()

    try:
        sap_state()
    except Exception:
        raise SystemExit("Mock SAP not running. Start it:  uvicorn sap_mock:app --port 8000")
    check_api()

    rows = load_matrix(args.tests)
    splits = ["tune", "holdout"] if args.split == "all" else [args.split]
    selected = [r for r in rows if r["split"] in splits]
    if args.only:
        prefixes = tuple(p.strip() for p in args.only.split(",") if p.strip())
        selected = [r for r in selected if r["split"] == "tune" and
                    (r["id"].startswith(prefixes) or r["thread"].startswith(prefixes))]

    def scored_row(r):
        return args.include_decide or r["label"] != "decide"

    # A thread runs if any of its rows is scored; decide rows inside a firm
    # thread still RUN (to keep the conversation intact) but aren't scored.
    threads = {k: t for k, t in group_threads(selected).items()
               if any(scored_row(r) for r in t)}
    skipped_decide = sum(not scored_row(r) for r in selected)

    print(f"\nRunning {sum(len(t) for t in threads.values())} messages in "
          f"{len(threads)} threads x {args.repeat} "
          f"({', '.join(splits)}; {skipped_decide} 'decide' rows not scored)\n")

    runs = defaultdict(list)
    consecutive_errors = 0
    for rep in range(args.repeat):
        for name, turns in threads.items():
            try:
                outcomes = run_thread(turns)
            except httpx.HTTPError as e:
                raise SystemExit(f"Mock SAP stopped responding: {e}")
            marks = []
            for row, out in outcomes:
                if not scored_row(row):
                    marks.append("·")
                    continue
                out = score(row, out)
                runs[row["id"]].append(out)
                marks.append({"PASS": "✓", "FAIL": "✗", "ERROR": "!"}[out["result"]])
                consecutive_errors = consecutive_errors + 1 if out["result"] == "ERROR" else 0
            label = name if turns[0]["split"] == "tune" else "holdout"
            print(f"  run {rep + 1}  {label:<10} {''.join(marks)}")
            if consecutive_errors >= 3:
                print("\nThree API errors in a row — stopping early. Fix the connection and rerun.")
                break
        if consecutive_errors >= 3:
            break

    results = {rid: aggregate(r) for rid, r in runs.items()}
    os.makedirs(OUT_DIR, exist_ok=True)
    for split in splits:
        part = [r for r in rows if r["split"] == split]
        write_csv(os.path.join(OUT_DIR, f"runa_results_{split}.csv"), part, results)
    if args.tests.lower().endswith(".xlsx"):
        write_xlsx(args.tests, os.path.join(OUT_DIR, "runa_test_matrix_results.xlsx"),
                   rows, results)

    for split in splits:
        summarize(rows, results, split)
    print(f"\nFiles written to {OUT_DIR}/\n")


if __name__ == "__main__":
    main()
