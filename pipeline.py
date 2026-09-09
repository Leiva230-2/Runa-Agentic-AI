"""
Runa's orchestration.

The pipeline is a loop, not a chain. When confidence is low the agent posts a
question, parks the event, and resumes when a human replies. That park-and-
resume is what separates an agent from a script.
"""
from dataclasses import dataclass, field

import agents
from config import CONFIDENCE_THRESHOLD
from trace import Trace


@dataclass
class Pending:
    """An event Runa refused to write because it was not sure enough."""
    candidate: dict
    sender: str
    question: str
    conversation: list[str] = field(default_factory=list)


class Runa:
    def __init__(self, trace: Trace | None = None):
        self.trace = trace or Trace()
        self.conversation: list[str] = []
        self.pending: Pending | None = None
        self.written = 0

    # ------------------------------------------------------------------
    def handle(self, sender: str, message: str) -> list[str]:
        """Process one inbound message. Returns messages Runa wants to post."""
        self.conversation.append(f"{sender}: {message}")
        self.trace.log("human", f"{sender}: {message}")

        if self.pending:
            return self._resume(sender, message)

        candidate = agents.listen(message, sender)
        if not candidate.get("is_event"):
            self.trace.log("listener", "Not a business event — discarded")
            return []

        self.trace.log("listener",
                       f"Candidate {candidate['event_type']}: {candidate['summary']}")
        return self._resolve_and_act(candidate, sender)

    # ------------------------------------------------------------------
    def _resolve_and_act(self, candidate: dict, sender: str) -> list[str]:
        deliveries = agents.get_open_deliveries()
        res = agents.resolve(candidate, sender, self.conversation, deliveries)

        self.trace.log(
            "resolver", res.get("reasoning", ""),
            delivery_id=res.get("delivery_id"),
            identity_confidence=res.get("identity_confidence"),
            content_confidence=res.get("content_confidence"),
        )

        if not agents.clears_threshold(res):
            question = res.get("clarifying_question") or \
                "Maaf, bisa dikonfirmasi lagi detailnya?"
            low = ("identity" if res.get("identity_confidence", 0) < CONFIDENCE_THRESHOLD
                   else "content")
            self.trace.log("resolver",
                           f"Below threshold on {low} confidence — asking rather than guessing")
            self.pending = Pending(candidate, sender, question,
                                   list(self.conversation))
            self.trace.log("agent", question)
            return [question]

        return self._write(res)

    # ------------------------------------------------------------------
    def _resume(self, sender: str, message: str) -> list[str]:
        """A human answered the clarifying question — re-decide with the reply."""
        pending, self.pending = self.pending, None
        self.trace.log("resolver", "Reply received — re-scoring with new information")

        deliveries = agents.get_open_deliveries()
        res = agents.resolve(pending.candidate, pending.sender,
                             self.conversation, deliveries)

        self.trace.log(
            "resolver", res.get("reasoning", ""),
            delivery_id=res.get("delivery_id"),
            identity_confidence=res.get("identity_confidence"),
            content_confidence=res.get("content_confidence"),
        )

        if not agents.clears_threshold(res):
            self.trace.log("resolver",
                           "Still below threshold — escalating to a human rather than writing")
            return ["Maaf, saya masih belum yakin. Saya teruskan ke supervisor ya."]

        return self._write(res)

    # ------------------------------------------------------------------
    def _write(self, res: dict) -> list[str]:
        """Both scores cleared. Post to SAP, then reason about consequences."""
        out = []
        did = res["delivery_id"]
        source_ref = f"telegram:{len(self.conversation)}"

        if res["event_type"] == "GOODS_RECEIPT_DISCREPANCY" and res.get("quantity_short"):
            deliveries = {d["InboundDelivery"]: d for d in agents.get_open_deliveries()}
            material = deliveries[did]["Material"]
            doc = agents.post_shortage(did, material, res["quantity_short"], source_ref)
            self.written += 1
            self.trace.log("transactor",
                           f"Posted material document for {res['quantity_short']} short",
                           document=doc["MaterialDocument"], delivery_id=did)
            out.append(f"Tercatat di SAP: selisih {res['quantity_short']} "
                       f"pada DO {did}. Material document {doc['MaterialDocument']}.")
            return out

        delivery = agents.post_delay(did, res["new_eta"],
                                     res.get("reason_code", "OTHER"), source_ref)
        self.written += 1
        eta_hm = res["new_eta"].split("T")[1][:5]
        self.trace.log("transactor",
                       f"Patched inbound delivery ETA to {eta_hm}",
                       delivery_id=did, reason_code=res.get("reason_code"))
        out.append(f"Tercatat di SAP: DO {did} estimasi tiba {eta_hm} "
                   f"({res.get('reason_code')}).")

        # Unprompted downstream reasoning
        conflict = agents.check_conflict(delivery)
        if conflict:
            self.trace.log("conflict",
                           "New state conflicts with dock closing time — "
                           "surfacing to a human unprompted")
            self.trace.log("agent", conflict)
            out.append(conflict)

        return out
