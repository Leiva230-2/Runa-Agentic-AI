"""
Runa's orchestration: the clerk's thought process.

    hear -> understand -> check the rulebook -> write -> check consequences

Golden rule: the AI JUDGES, the code CONTROLS. The AI says what a message
means and how sure it is. Plain Python decides whether a write is allowed,
does the write, and remembers who is waiting for what.

Safety rule: Runa never crashes and never guesses. When something is wrong,
unclear, or outside its forms, it says so and hands over to a human.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import agents
import checks
from config import (CONFIDENCE_THRESHOLD, MAX_ETA_SHIFT_HOURS,
                    MAX_QUESTIONS_PER_EVENT, MAX_SHORTAGE_RATIO,
                    MATERIAL_ALIASES, SUPERVISORS, SUPPORTED_EVENTS, now)
from trace import Trace

HANDOFF_PREFIX = "Saya teruskan ke supervisor ya"

# What humans see instead of SAP reason codes
REASON_TEXT = {
    "TRAFFIC": "macet",
    "BREAKDOWN": "kendaraan bermasalah",
    "WEATHER": "cuaca",
    "EARLY_DISPATCH": "berangkat lebih awal",
}


@dataclass
class Pending:
    """An event Runa refused to write because it was not sure enough.
    Only the person who reported it can answer the question."""
    candidate: dict
    sender: str
    question: str
    texts: list[str] = field(default_factory=list)   # what the human actually said
    asked: int = 1                                    # questions asked so far


@dataclass
class PendingDecision:
    """Runa escalated a conflict with options and is waiting for a choice."""
    delivery_id: str
    kind: str          # "LATE" or "EARLY"
    options: str


class Authority:
    """Who may choose an option (a/b) when Runa escalates a conflict.

    Telegram: bot.py fills this with the group's ADMINS, matched by Telegram
    user ID — so two people with the same first name can't be confused, and
    no name is written into the code. Replay scripts and tests have no
    Telegram, so they fall back to the SUPERVISORS names in config.py."""

    def __init__(self, names: list[str] | None = None):
        self.names = {n.lower() for n in (SUPERVISORS if names is None else names)}
        self.admin_ids: set = set()
        self.admin_mentions: list[str] = []
        self.refreshed = 0.0

    def set_admins(self, admins: list[tuple]):
        """admins: (user_id, username or None, first_name) for each human admin."""
        self.admin_ids = {uid for uid, _, _ in admins}
        self.admin_mentions = [f"@{u}" if u else name for _, u, name in admins]

    def is_supervisor(self, sender: str, sender_id=None) -> bool:
        if sender_id is not None and sender_id in self.admin_ids:
            return True
        return sender.lower() in self.names

    def mention(self) -> str:
        if self.admin_mentions:
            return " ".join(self.admin_mentions)
        return " ".join(f"@{n.title()}" for n in sorted(self.names)) or "Supervisor:"


class Runa:
    def __init__(self, trace: Trace | None = None,
                 authority: Authority | None = None):
        self.trace = trace or Trace()
        self.authority = authority or Authority()
        self._sender_id = None
        self.conversation: list[str] = []
        self.pending: dict = {}   # person (Telegram ID, or name) -> open questions
        self.pending_decision: PendingDecision | None = None
        self.written = 0
        self.handoffs = 0

    # ================================================================ entry
    def handle(self, sender: str, message: str, sender_id=None) -> list[str]:
        """Process one message. NEVER raises: any failure becomes a handoff.
        sender_id: the Telegram user ID, so people are told apart by ID, not name."""
        self._sender_id = sender_id
        self.conversation.append(f"{sender}: {message}")
        self.trace.log("human", f"{sender}: {message}")
        try:
            return self._route(sender, message)
        except agents.ModelError as e:
            return self._handoff("Sistem AI sedang gangguan", detail=str(e))
        except Exception as e:                       # SAP down, bad data, bugs
            return self._handoff("Ada gangguan sistem",
                                 detail=f"{type(e).__name__}: {e}")

    def _who(self, sender: str):
        return self._sender_id if self._sender_id is not None else sender

    def _route(self, sender: str, message: str) -> list[str]:
        # 0. Text pretending to be instructions to the system? Never processed.
        if checks.looks_like_injection(message):
            return self._handoff(
                "Pesan ini terlihat seperti perintah ke sistem, bukan laporan "
                "operasional", detail="Possible prompt injection — not processed")

        # 1. Did Runa ask THIS person something — and is this actually the answer?
        if self.pending.get(self._who(sender)):
            idx = self._which_answer(sender, message)
            if idx is not None:
                return self._resume(sender, idx, message)
            self.trace.log("resolver", f"{sender} changed topic — question stays "
                                       f"open, treating this as a new message")

        # 2. Is a SUPERVISOR answering an open escalation (opsi a / b)?
        #    Anyone else's "b" is just a message — it can't trigger the option.
        if self.pending_decision:
            if self.authority.is_supervisor(sender, self._sender_id):
                answered = self._decide(message)
                if answered is not None:
                    return answered
            else:
                self.trace.log("policy", f"{sender} is not a supervisor — "
                                         f"can't choose escalation options")

        # 3. Otherwise: a fresh message. Is it work, or chat?
        candidate = agents.listen(message, sender)
        if not candidate.get("is_event"):
            self.trace.log("listener", "Not a business event — discarded")
            return []

        self.trace.log("listener",
                       f"Candidate {candidate.get('event_type')}: {candidate.get('summary')}")

        # 4. Several reports in one message -> human, so none gets dropped
        if (candidate.get("event_count") or 1) > 1:
            return self._handoff(
                "Ada beberapa laporan dalam satu pesan. "
                "terlewat", detail=f"{candidate.get('event_count')} events in one message")

        # 5. Understood, but Runa has no form for it -> human
        if candidate.get("event_type") not in SUPPORTED_EVENTS:
            return self._handoff(
                "Laporannya saya terima, tapi ini di luar yang bisa saya "
                "proses otomatis",
                detail=f"No form for this: {candidate.get('summary')}")

        return self._resolve_and_act(candidate, sender, [message])

    # ============================================================ resolving
    def _resolve_and_act(self, candidate: dict, sender: str, texts: list[str],
                         asked: int = 0, last_question: str = "") -> list[str]:
        """Judge the event. `asked` = how many questions this event has cost so far."""
        deliveries = agents.get_open_deliveries()
        res = agents.resolve(candidate, sender, self.conversation, deliveries)

        self.trace.log(
            "resolver", res.get("reasoning", ""),
            delivery_id=res.get("delivery_id"),
            identity_confidence=res.get("identity_confidence"),
            content_confidence=res.get("content_confidence"),
        )

        # Code checks the AI's work against what the human literally said
        mismatch = self._check_delivery_number(texts, res.get("delivery_id"), deliveries)
        if mismatch:
            reason, question = mismatch
            self.trace.log("policy", f"Delivery number check: {reason}")
            return self._ask_or_handoff(candidate, sender, question, texts,
                                        asked, last_question, reason)

        ident = res.get("identity_confidence", 0) or 0
        content = res.get("content_confidence", 0) or 0
        if ident >= CONFIDENCE_THRESHOLD and content >= CONFIDENCE_THRESHOLD:
            # The AI is sure. Now the CODE checks the facts it can verify.
            doubt = self._fact_check(res, texts, deliveries)
            if doubt:
                reason, question = doubt
                self.trace.log("policy", f"Fact check: {reason}")
                return self._ask_or_handoff(candidate, sender, question, texts,
                                            asked, last_question, reason)
            return self._gate_and_write(res, deliveries)

        weak = "identity" if ident < CONFIDENCE_THRESHOLD else "content"
        question = res.get("clarifying_question") or \
            "Maaf, bisa dikonfirmasi lagi detailnya?"
        self.trace.log("resolver",
                       f"Below threshold on {weak} confidence — asking rather than guessing")
        return self._ask_or_handoff(candidate, sender, question, texts,
                                    asked, last_question,
                                    f"Below threshold on {weak} confidence")

    def _ask_or_handoff(self, candidate: dict, sender: str, question: str,
                        texts: list[str], asked: int, last_question: str,
                        reason: str) -> list[str]:
        """Ask another question if the budget allows. Hand off if Runa has
        already asked the maximum, or would just repeat itself."""
        if asked >= MAX_QUESTIONS_PER_EVENT:
            return self._handoff("Maaf, saya masih belum yakin",
                                 detail=f"{reason} after {asked} question(s)")
        if question.strip() == last_question.strip():
            return self._handoff("Maaf, saya masih belum yakin",
                                 detail=f"{reason} — would repeat the same question")
        return self._ask_back(candidate, sender, question, texts, asked + 1)

    def _ask_back(self, candidate: dict, sender: str, question: str,
                  texts: list[str], asked: int = 1) -> list[str]:
        """Park the event and ask the person who reported it."""
        self.pending.setdefault(self._who(sender), []).append(
            Pending(candidate, sender, question, list(texts), asked))
        self.trace.log("agent", question,
                       question_number=f"{asked}/{MAX_QUESTIONS_PER_EVENT}")
        return [question]

    def _which_answer(self, sender: str, message: str) -> int | None:
        """Is this message answering one of Runa's open questions to this person?"""
        questions = [p.question for p in self.pending[self._who(sender)]]
        result = agents.match_answer(message, questions)
        idx = result.get("answers_index")
        if isinstance(idx, int) and 0 <= idx < len(questions):
            return idx
        return None

    def _resume(self, sender: str, idx: int, reply: str) -> list[str]:
        """The person Runa asked has answered. Re-judge with the new message."""
        who = self._who(sender)
        pending = self.pending[who].pop(idx)
        if not self.pending[who]:
            del self.pending[who]
        self.trace.log("resolver", f"{sender} answered — re-scoring with new information")
        return self._resolve_and_act(pending.candidate, sender,
                                     pending.texts + [reply],
                                     asked=pending.asked,
                                     last_question=pending.question)

    # ======================================================= fact checks
    def _fact_check(self, res: dict, texts: list[str],
                    deliveries: list[dict]) -> tuple[str, str] | None:
        """Facts the code verifies against what the human literally typed.
        Returns (reason, question to ask), or None if everything checks out."""
        latest = texts[-1]
        kind = res.get("event_type")
        by_id = {d["InboundDelivery"]: d for d in deliveries}

        # Named a product? The chosen delivery must actually carry it.
        named = set()
        for text in texts:
            named |= checks.materials_mentioned(text, MATERIAL_ALIASES)
        chosen = by_id.get(res.get("delivery_id"))
        if named and chosen and chosen["Material"] not in named:
            carriers = [d["InboundDelivery"] for d in deliveries if d["Material"] in named]
            options = " atau ".join(f"DO {c}" for c in carriers) or "DO yang mana"
            return (f"Message names {sorted(named)}, but DO {chosen['InboundDelivery']} "
                    f"carries {chosen['Material']}",
                    f"Barang itu ada di {options}. Untuk DO yang mana?")

        hedge = checks.hedge_word(latest)
        if hedge:
            return (f"Hedge word '{hedge}' — an estimate, not a fact",
                    "Itu sudah pasti, atau masih perkiraan?")

        if kind == "ETA_CHANGE":
            reading = checks.latest_time(texts, now())
            said = reading.said
            if reading.kind == "ambiguous":
                return (f"'{said}' could be morning or evening",
                        f"Maksudnya {said} pagi, atau {said} malam?")
            if reading.kind == "past":
                return (f"'{said}' has already passed today",
                        f"{said.capitalize()} hari ini sudah lewat. "
                        f"Maksudnya besok, atau jam lain?")
            if reading.kind == "clear" and res.get("new_eta"):
                try:
                    ai = datetime.fromisoformat(res["new_eta"])
                except ValueError:
                    return None                    # policy check handles it
                if not any(abs(ai - c) <= timedelta(minutes=5)
                           for c in reading.candidates):
                    c = reading.candidates[0]
                    return (f"AI read {ai:%d/%m %H:%M}, message says {c:%d/%m %H:%M}",
                            f"Maksudnya jam {c:%H:%M} ya?")

        if kind == "GOODS_RECEIPT_DISCREPANCY":
            delivery = by_id.get(res.get("delivery_id"))
            if delivery:
                unit = delivery["DeliveryQuantityUnit"]
                clash = checks.unit_mismatch(latest, unit, res.get("quantity_unit"))
                if clash:
                    typed, theirs = clash
                    return (f"Human counted in {theirs}, delivery is in {unit}",
                            f"{typed} itu berapa {checks.UNIT_NAME.get(unit, unit)}?")
        return None

    def _check_delivery_number(self, texts: list[str], chosen: str | None,
                               deliveries: list[dict]) -> tuple[str, str] | None:
        """If the human named a delivery, the AI must use exactly that one.
        The most recent message that names a number wins (so corrections work).
        Returns (reason, question to ask) or None if everything matches."""
        known = {d["InboundDelivery"] for d in deliveries}
        mentioned = []
        for text in reversed(texts):
            mentioned = checks.mentioned_deliveries(text, known)
            if mentioned:
                break
        if not mentioned:
            return None

        unknown = [n for n in mentioned if n not in known]
        if unknown:
            return (f"Human named DO {unknown[0]}, which is not in SAP "
                    f"(AI picked {chosen})",
                    f"DO {unknown[0]} tidak ada di sistem. Maksudnya DO yang mana?")
        if chosen not in mentioned:
            question = (f"Ini untuk DO {mentioned[0]} ya?" if not chosen
                        else f"Maksudnya DO {mentioned[0]}, atau DO {chosen}?")
            return f"Human named DO {mentioned[0]}, but AI picked {chosen}", question
        return None

    # ======================================================== the rulebook
    def _policy_check(self, res: dict, deliveries: list[dict]) -> str | None:
        """Plain-code rules every write must pass. Returns a reason to stop,
        or None if the write is allowed. The AI cannot override any of this."""
        by_id = {d["InboundDelivery"]: d for d in deliveries}
        did = res.get("delivery_id")
        kind = res.get("event_type")

        if did not in by_id:
            return f"Delivery {did} does not exist in SAP"
        delivery = by_id[did]

        if kind == "ETA_CHANGE":
            try:
                new = datetime.fromisoformat(res.get("new_eta") or "")
            except ValueError:
                return "No valid new arrival time"
            old = datetime.fromisoformat(delivery["PlannedGoodsReceipt"])
            shift = abs((new - old).total_seconds()) / 3600
            if shift > MAX_ETA_SHIFT_HOURS:
                return f"Arrival time moves {shift:.0f}h — over the {MAX_ETA_SHIFT_HOURS}h limit"
            if shift == 0:
                return "New arrival time is the same as the planned one"

        elif kind == "GOODS_RECEIPT_DISCREPANCY":
            qty = res.get("quantity_short")
            if not isinstance(qty, (int, float)) or qty <= 0:
                return "No valid shortage quantity"
            if qty > delivery["DeliveryQuantity"] * MAX_SHORTAGE_RATIO:
                return (f"Shortage of {qty} is over {int(MAX_SHORTAGE_RATIO*100)}% "
                        f"of the delivery — needs a human check")

        elif kind == "QUALITY_COMPLAINT":
            if not res.get("defect_description"):
                return "No description of the defect"

        else:
            return f"No form for event type {kind}"

        return None

    # =========================================================== the forms
    def _gate_and_write(self, res: dict, deliveries: list[dict]) -> list[str]:
        stop = self._policy_check(res, deliveries)
        if stop:
            self.trace.log("policy", f"Blocked: {stop}")
            return self._handoff("Ini perlu dicek manusia dulu", detail=stop)
        self.trace.log("policy", "Passed all rules — writing to SAP")

        delivery = {d["InboundDelivery"]: d for d in deliveries}[res["delivery_id"]]
        did = res["delivery_id"]
        ref = f"telegram:{len(self.conversation)}"
        kind = res["event_type"]

        if kind == "GOODS_RECEIPT_DISCREPANCY":
            doc = agents.post_shortage(did, delivery["Material"],
                                       res["quantity_short"], ref)
            self._wrote(f"Posted material document: {res['quantity_short']} short",
                        did, document=doc["MaterialDocument"])
            return [f"Tercatat di SAP: kurang {res['quantity_short']} "
                    f"{delivery['DeliveryQuantityUnit']} pada DO {did}. "
                    f"Dokumen {doc['MaterialDocument']}."]

        if kind == "QUALITY_COMPLAINT":
            qn = agents.post_quality_notification(
                did, delivery["Material"], res["defect_description"],
                res.get("defect_code") or "OTHER", ref)
            self._wrote(f"Created quality notification: {res['defect_description']}",
                        did, document=qn["QualityNotification"])
            return [f"Tercatat di SAP: keluhan kualitas untuk DO {did} "
                    f"({res.get('defect_code') or 'OTHER'}). "
                    f"Notifikasi {qn['QualityNotification']}."]

        # ETA_CHANGE: the CODE decides early vs late, from the actual times
        old = datetime.fromisoformat(delivery["PlannedGoodsReceipt"])
        new = datetime.fromisoformat(res["new_eta"])
        status = "Early" if new < old else "Delayed"
        reason = res.get("reason_code") or ("EARLY_DISPATCH" if status == "Early" else "OTHER")

        updated = agents.post_eta_change(did, res["new_eta"], status, reason, ref)
        new_hm = res["new_eta"].split("T")[1][:5]
        self._wrote(f"Patched arrival time to {new_hm} ({status})", did,
                    reason_code=reason)
        why = REASON_TEXT.get(reason)
        label = "lebih cepat" if status == "Early" else "terlambat"
        out = [f"Tercatat di SAP: DO {did} estimasi tiba {new_hm} "
               f"({label}{', ' + why if why else ''})."]

        # A new time for this delivery makes any old escalation about it stale
        if self.pending_decision and self.pending_decision.delivery_id == did:
            self.trace.log("policy", "New arrival time — the earlier escalation "
                                     "is no longer valid, cancelled")
            self.pending_decision = None

        # Unprompted: does the new time break anything else?
        conflict = agents.check_conflict(updated)
        if conflict:
            ckind, body = conflict
            message = f"{self.authority.mention()} {body}"
            self.trace.log("conflict",
                           f"New time conflicts with dock hours ({ckind}) — "
                           f"surfacing to a human unprompted")
            self.trace.log("agent", message)
            out.append(message)
            self.pending_decision = PendingDecision(did, ckind, message)
        return out

    def _wrote(self, summary: str, did: str, **detail):
        self.written += 1
        self.trace.log("transactor", summary, delivery_id=did, **detail)

    # ========================================================== decisions
    def _decide(self, message: str) -> list[str] | None:
        """Someone may be answering the open escalation. None = they weren't."""
        pd = self.pending_decision
        result = agents.interpret_decision(message, pd.options)
        if result.get("choice") not in ("a", "b"):
            self.trace.log("resolver", "Not an answer to the open escalation — continuing")
            return None

        self.trace.log("resolver",
                       f"Human chose option {result['choice']} — {result.get('reasoning', '')}")
        self.pending_decision = None
        delivery = {d["InboundDelivery"]: d
                    for d in agents.get_open_deliveries()}[pd.delivery_id]
        confirmation = agents.apply_decision(
            delivery, pd.kind, result["choice"], f"telegram:{len(self.conversation)}")
        self._wrote(f"Executed option {result['choice']}", pd.delivery_id)
        self.trace.log("agent", confirmation)
        return [confirmation]

    # ============================================================ handoff
    def _handoff(self, reason: str, detail: str = "") -> list[str]:
        """The safety net: say what happened, pass it to a human, never guess."""
        self.handoffs += 1
        self.trace.log("handoff", f"Forwarded to human — {detail or reason}")
        message = f"{reason}. {HANDOFF_PREFIX}. 🙏"
        self.trace.log("agent", message)
        return [message]
