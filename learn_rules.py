"""
Warm-up step 2: find the warehouse's rules in the facts from the history.

    python learn_history.py           # step 1: chat -> data/history_facts.json
    python learn_rules.py             # step 2: facts -> rules, scored (nothing sent)
    python learn_rules.py --post      # also send the rules to runa-sap's notebook,
                                      # as PROPOSED, for a supervisor to /setuju
    python learn_rules.py --saved --post --approved-by Sari
                                      # demo setup: send the rules saved last time
                                      # (data/history_rules.json, already read by
                                      # the team) without asking the AI again

The score compares the rules with data/history_answer_key.json by evidence:
a planted rule counts as found when a proposed rule cites one of the messages
it was planted in. The AI never sees the answer key.
"""
import argparse
import json
import os

import rules
from config import NOTEBOOK_SEED

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--facts", default=os.path.join(HERE, "data", "history_facts.json"))
    parser.add_argument("--key", default=os.path.join(HERE, "data", "history_answer_key.json"))
    parser.add_argument("--out", default=os.path.join(HERE, "data", "history_rules.json"))
    parser.add_argument("--post", action="store_true",
                        help="send the rules to runa-sap (SAP_BASE) as proposed")
    parser.add_argument("--saved", action="store_true",
                        help="use the rules saved last time instead of asking the AI")
    parser.add_argument("--approved-by", default=None,
                        help="with --post: send them as approved by this supervisor")
    args = parser.parse_args()

    with open(args.facts, encoding="utf-8") as fh:
        facts = json.load(fh)["facts"]
    if args.post:
        import agents
        notebook = [n["text"] for n in agents.get_notebook(fresh=True)]
    else:
        notebook = list(NOTEBOOK_SEED)

    if args.saved:
        with open(args.out, encoding="utf-8") as fh:
            saved = json.load(fh)
        kept, dropped = saved["proposed"], saved["dropped"]
        print(f"Using the {len(kept)} rules saved in {args.out} (no AI call)\n")
    else:
        print(f"Looking for rules in {len(facts)} facts\n")
        kept, dropped = rules.propose_rules(facts, notebook)

    print(f"PROPOSED ({len(kept)})")
    for i, r in enumerate(kept, 1):
        days = sorted({e["at"][5:10] for e in r["evidence"]})
        again = "  (after a second look)" if r.get("second_look") else ""
        print(f"{i:2d}. [{r['kind']}] {r['rule']}{again}\n    evidence: "
              f"{', '.join(e['ref'] for e in r['evidence'])} (days {', '.join(days)})")
    if dropped:
        print(f"\nDROPPED BY THE CODE CHECKS ({len(dropped)})")
        for r in dropped:
            print(f" -  {r['rule']}  <- {r['dropped']}")

    if not args.saved:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"proposed": kept, "dropped": dropped}, fh, ensure_ascii=False, indent=1)

    if os.path.exists(args.key):
        with open(args.key, encoding="utf-8") as fh:
            result = rules.score(kept, json.load(fh))
        print(f"\nSCORE: found {len(result['found'])} of {result['total']} planted rules, "
              f"fell for {len(result['traps'])} of 4 one-off traps")
        if result["missed"]:
            print(f"  missed: {', '.join(result['missed'])}")
        for trap, text in result["traps"]:
            print(f"  TRAP {', '.join(trap)}: {text}")
        for text in result["extra"]:
            print(f"  not in the key (check by hand): {text}")

    if args.post:
        import agents
        sent = 0
        status = "approved" if args.approved_by else "proposed"
        for r in kept:
            if agents.add_note(r["rule"], status=status, source="learned",
                               by=args.approved_by or "Runa", kind="rule",
                               evidence=r["evidence"]):
                sent += 1
        if args.approved_by:
            print(f"\nSent {sent} rules to the notebook, approved by {args.approved_by}.")
        else:
            print(f"\nSent {sent} rules to the notebook as proposed. "
                  f"A supervisor approves each with /setuju <number> (see /catatan).")
    print(f"\nSaved to {args.out}")


if __name__ == "__main__":
    main()
