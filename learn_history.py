"""
Warm-up: let Runa read the operations group's past chat before going live.

    python learn_history.py                      # read data/history_chat.txt
    python learn_history.py --file other.txt     # another history (e.g. a blind test)

Step 1 (this file today): every day of chat goes through the fact reader
(facts.py), oldest first, so Runa knows which facts were true when, and
which ones a later message ended. The result is data/history_facts.json.

Step 2 (next): the rule learner reads those facts, finds what keeps
happening, and proposes rules to the notebook for a supervisor to approve.

Uses the real AI (AI Core or the Anthropic API, from .env). One AI call per
day of chat. Writes nothing to SAP.
"""
import argparse
import json
import os
from collections import OrderedDict
from datetime import datetime

import facts
from config import NOTEBOOK_SEED

HERE = os.path.dirname(os.path.abspath(__file__))
YEAR = 2026


def load_history(path: str) -> tuple[list[dict], list[str]]:
    """Messages ("MM-DD HH:MM | sender | text") and the header's notes about
    who is who (lines after '# People:'), which a real company would put in
    its notebook."""
    messages, people, in_people = [], [], False
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("#"):
                body = line.lstrip("# ").strip()
                if body.startswith("People:"):
                    in_people, body = True, body[len("People:"):].strip()
                if in_people and body:
                    people.append(body)
                continue
            in_people = False
            if not line.strip():
                continue
            when, sender, text = (part.strip() for part in line.split("|", 2))
            at = datetime.strptime(f"{YEAR}-{when}", "%Y-%m-%d %H:%M")
            messages.append({"id": f"h{len(messages) + 1:03d}", "sender": sender,
                             "text": text, "at": at.isoformat(timespec="minutes")})
    return messages, [" ".join(people)] if people else []


def by_day(messages: list[dict]) -> "OrderedDict[str, list[dict]]":
    days = OrderedDict()
    for m in messages:
        days.setdefault(m["at"][:10], []).append(m)
    return days


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=os.path.join(HERE, "data", "history_chat.txt"))
    parser.add_argument("--out", default=os.path.join(HERE, "data", "history_facts.json"))
    args = parser.parse_args()

    messages, people = load_history(args.file)
    people = NOTEBOOK_SEED + people
    known: list[dict] = []
    print(f"Reading {len(messages)} messages, one day at a time\n")
    for day, todays in by_day(messages).items():
        start = datetime.fromisoformat(todays[0]["at"])
        active = [f for f in known if facts.is_active(f, start)]
        new, ended = facts.read_facts(todays, active, people)
        for e in ended:
            fact = next(f for f in known if f["id"] == e["id"])
            fact["ended_at"], fact["ended_by"] = e["at"], e["ref"]
        for f in new:
            f["id"] = len(known) + 1
            f["ended_at"] = f["ended_by"] = None
            for old_id in facts.superseded(f, known):      # same thing, newer news
                old = known[old_id - 1]
                old["ended_at"], old["ended_by"] = f["source"]["at"], f["source"]["ref"]
                ended.append({"id": old_id})
            known.append(f)
        print(f"{day}: {len(todays):3d} messages -> {len(new):2d} facts, {len(ended)} ended")
        for f in new:
            print(f"      [{f['type']}] {f['subject']}: {f['statement']} "
                  f"({f['confidence']:.2f}, {f['source']['ref']})")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"source": os.path.basename(args.file), "messages": len(messages),
                   "facts": known}, fh, ensure_ascii=False, indent=1)
    counts = {}
    for f in known:
        counts[f["type"]] = counts.get(f["type"], 0) + 1
    print(f"\n{len(known)} facts -> {args.out}")
    print("  " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
