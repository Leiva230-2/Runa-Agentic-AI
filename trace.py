"""
Structured reasoning trace.

Every step Runa takes is written here as JSONL. This serves two purposes:
it is the audit trail that links each SAP record back to its source message,
and it is the demo evidence you show judges.

In production this is AgentCore Observability.
"""
import json
import os
from datetime import datetime

from config import TRACE_FILE

C = {
    "listener": "\033[36m",   # cyan
    "resolver": "\033[35m",   # magenta
    "transactor": "\033[33m", # yellow
    "conflict": "\033[31m",   # red
    "human": "\033[32m",      # green
    "agent": "\033[94m",      # blue
    "dim": "\033[2m",
    "bold": "\033[1m",
    "off": "\033[0m",
}


class Trace:
    def __init__(self, echo: bool = True):
        self.echo = echo
        self.events: list[dict] = []
        os.makedirs(os.path.dirname(TRACE_FILE), exist_ok=True)

    def log(self, actor: str, summary: str, **detail):
        entry = {
            "ts": datetime.now().strftime("%H:%M:%S"),
            "actor": actor,
            "summary": summary,
            **detail,
        }
        self.events.append(entry)
        with open(TRACE_FILE, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
        if self.echo:
            self._print(entry)
        return entry

    def _print(self, e: dict):
        colour = C.get(e["actor"].lower(), "")
        label = e["actor"].upper()
        print(f"{C['dim']}{e['ts']}{C['off']}  "
              f"{colour}{C['bold']}{label:<11}{C['off']} {e['summary']}")
        for key in ("identity_confidence", "content_confidence",
                    "delivery_id", "document", "reason_code"):
            if key in e:
                print(f"{'':>13}{C['dim']}{key}: {e[key]}{C['off']}")

    def replay(self) -> str:
        """Plain-text trace suitable for pasting into the proposal."""
        return "\n".join(
            f"{e['ts']}  {e['actor'].upper():<11} {e['summary']}"
            for e in self.events
        )
