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
from uuid import uuid4
from datetime import datetime, timedelta, timezone

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

STEP_NAMES = {
    "listener" : "Listener",
    "resolver" : "Resolver",
    "policy" : "Rules",
    "transactor" : "Transactor",
    "conflict" : "Conflict check", 
    "notebook" : "Notebook",
    "planner" : "Planner",
    "reviewer" : "Reviewer",
    "stages" : "Stage reader",
}
WIB = timezone(timedelta(hours=7))


@dataclass
class Pending:
    """An event Runa refused to write because it was not sure enough.
    Only the person who reported it can answer the question."""
    candidate: dict
    sender: str
    question: str
    texts: list[str] = field(default_factory=list)   # what the human actually said
    asked: int = 1                                    # questions asked so far
    delivery_id: str | None = None                    # the AI's best guess when asking


@dataclass
class PendingDecision:
    """Runa escalated a conflict with options and is waiting for a choice."""
    delivery_id: str
    kind: str          # "LATE" or "EARLY"
    options: str       # the message the supervisor saw
    plan: dict | None = None   # the Planner's checked options; None = the fixed a/b


@dataclass
class Undo:
    """One SAP write Runa made, and how to reverse it.
    kind: "restore" (put delivery fields back), "shortage" or "quality"
    (cancel the document Runa created)."""
    kind: str
    delivery_id: str
    label: str                       # what the supervisor sees, in Indonesian
    before: dict | None = None       # restore: the field values before the write
    document: str | None = None      # shortage / quality: the document number


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
    def __init__(self, trace: Trace | None = None, authority: Authority | None = None, report: bool = False):
        self.trace = trace or Trace()
        self.report = report
        self.authority = authority or Authority()
        self._sender_id = None
        self._decider = None
        # Telegram: post the SAP confirmation first, then think about options
        # (bot.py turns this on). Tests and replays plan straight away.
        self.defer_planning = False
        self._deferred = None
        self.conversation: list[str] = []
        self.pending: dict = {}   # person (Telegram ID, or name) -> open questions
        self.pending_decision: PendingDecision | None = None
        self.undo_stack: list[Undo] = []   # newest last; /undo takes from the end
        self.written = 0
        self.handoffs = 0

    # ================================================================ entry
    def handle(self, sender: str, message: str, sender_id=None) -> list[str]:
        """Process one message. NEVER raises: any failure becomes a handoff.
        sender_id: the Telegram user ID, so people are told apart by ID, not name."""
        self._sender_id = sender_id
        start = len(self.trace.events)
        agents.set_source(sender=sender, text=message, agent="Runa")
        self.conversation.append(f"{sender}: {message}")
        self.trace.log("human", f"{sender}: {message}")
        try:
            replies = self._route(sender, message)
        except agents.ModelError as e:
            replies = self._handoff("Sistem AI sedang bermasalah", detail=str(e))
        except Exception as e:                       # SAP down, bad data, bugs
            replies = self._handoff("Ada gangguan di sistem", detail=f"{type(e).__name__}: {e}")

        # What the main pipeline did with this message, for the stage reader
        self._last_actors = {e["actor"] for e in self.trace.events[start:]}
        if self.report:
            try:
                self._report(sender, message, self.trace.events[start:])
            except Exception as e:
                print(f"[report] skipped: {type(e).__name__}: {e}")
        return replies


    # ============================================================== stages
    def track_stages(self, sender: str, message: str) -> list[str]:
        """Side job, after the reply: did this message report a delivery's
        stage ("DO 1989 lagi dikemas")? The stage reader (stages.py, on the
        second model) says what it sees; code checks it; the stage goes to
        SAP. Problems it mentions go to the warehouse log. Never raises."""
        import checks
        import stages
        try:
            deliveries = agents.get_open_deliveries()
            known = {d["InboundDelivery"] for d in deliveries}
            if not checks.mentioned_deliveries(message, known):
                return []                         # no DO named: nothing to read
            raw, model = stages.read_stages(message, deliveries, self.conversation[-5:-1])
            found = stages.check_stages(raw, message, deliveries)
        except Exception as e:
            print(f"[stages] skipped: {type(e).__name__}: {e}")
            return []
        for why, u in found["refused"]:
            self.trace.log("stages", f"Not written ({why}): {u}")
        agents.set_source(sender=sender, text=message, agent=f"stages ({model})")
        out = []
        for u in found["updates"]:
            did, old, new = u["delivery"], u["from"], u["to"]
            before = {"Stage": old}
            patch = {"Stage": new, "SourceMessageRef": f"telegram:{len(self.conversation)}"}
            if new == "RECEIVED":
                d = next(x for x in deliveries if x["InboundDelivery"] == did)
                before["Status"], patch["Status"] = d.get("Status"), "Received"
            try:
                agents.restore_delivery(did, patch)      # a plain field update
            except Exception as e:
                self.trace.log("stages", f"SAP refused the stage for DO {did}: {e}")
                continue
            label = f"tahap DO {did} {stages.LABEL.get(old, old)} → {stages.LABEL[new]}"
            self.undo_stack.append(Undo("restore", did, label, before=before))
            self.trace.log("stages", f"{model}: {label} (\"{u['evidence']}\", "
                                     f"{u['confidence']:.2f})", delivery_id=did)
            out.append(f"📦 DO {did}: {stages.LABEL.get(old, old)} → {stages.LABEL[new]}. "
                       f"Tercatat di SAP.")
        # Problems go to the log, unless the main pipeline already wrote or
        # forwarded this message (then the log has it already).
        if not ({"transactor", "handoff"} & getattr(self, "_last_actors", set())):
            for p in found["problems"]:
                agents.log_note("problem", p["delivery"], p["note"])
                self.trace.log("stages", f"Problem logged for DO {p['delivery']}: {p['note']}")
        return out

    # ================================================================ facts
    def observe(self, sender: str, message: str, at: datetime | None = None) -> list[dict]:
        """Side job, run AFTER the reply has gone out: read the constraints in
        this message (who drives since when, crew, forklifts, stock, rain)
        into the fact list. Never raises and never delays a reply: if the AI
        or runa-sap is down, Runa just doesn't learn from this message."""
        import facts
        if facts.is_chatter(message):
            return []
        at = at or now()
        try:
            active = [f for f in facts.get_facts() if facts.is_active(f, at)]
            msg = {"id": f"live-{uuid4().hex[:8]}", "sender": sender, "text": message,
                   "at": at.isoformat(timespec="minutes")}
            earlier = self.conversation[-6:-1] if self.conversation[-1:] == [
                f"{sender}: {message}"] else self.conversation[-5:]
            new, ended = facts.read_facts([msg], active, agents.notes_for(sender, message),
                                          context=earlier)
            ended_ids = {e["id"] for e in ended}
            for f in new:                          # same thing, newer news
                ended += [{"id": i, "at": f["source"]["at"], "ref": f["source"]["ref"]}
                          for i in facts.superseded(f, active) if i not in ended_ids]
                ended_ids = {e["id"] for e in ended}
            if new or ended:
                new = facts.save_facts(new, ended)
            for f in new:
                self.trace.log("facts", f"[{f['type']}] {f['subject']}: {f['statement']}")
            return new
        except Exception as e:
            print(f"[facts] skipped: {type(e).__name__}: {e}")
            return []

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
                self._decider = sender
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
                "Ada beberapa laporan dalam satu pesan",
                detail=f"{candidate.get('event_count')} events in one message")

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
        guess = res.get("delivery_id")
        mismatch = self._check_delivery_number(texts, guess, deliveries)
        if mismatch:
            reason, question = mismatch
            self.trace.log("policy", f"Delivery number check: {reason}")
            return self._ask_or_handoff(candidate, sender, question, texts,
                                        asked, last_question, reason, guess)

        ident = res.get("identity_confidence", 0) or 0
        content = res.get("content_confidence", 0) or 0

        # Runa asked "is this for DO X?" and the reporter said a plain "iya".
        # That human confirmation settles WHICH delivery — the AI doesn't get
        # to keep doubting it just because the sender isn't the driver on file.
        # Content must still pass on its own, and every check below still runs.
        proposed = checks.proposed_delivery(
            last_question, {d["InboundDelivery"] for d in deliveries})
        if (ident < CONFIDENCE_THRESHOLD and proposed
                and proposed == res.get("delivery_id") and checks.is_yes(texts[-1])):
            ident = CONFIDENCE_THRESHOLD
            self.trace.log("policy", f"{sender} confirmed DO {proposed} — "
                                     f"identity settled by the human",
                           delivery_id=proposed, identity_confidence=ident,
                           content_confidence=content)

        if ident >= CONFIDENCE_THRESHOLD and content >= CONFIDENCE_THRESHOLD:
            # The AI is sure. Now the CODE checks the facts it can verify.
            doubt = self._fact_check(res, texts, deliveries)
            if doubt:
                reason, question = doubt
                self.trace.log("policy", f"Fact check: {reason}")
                return self._ask_or_handoff(candidate, sender, question, texts,
                                            asked, last_question, reason, guess)
            written_before = self.written
            # For the warehouse log: the words behind the write, and the scores
            agents.update_source(text=" / ".join(texts[-3:]),
                                 scores=[round(ident, 2), round(content, 2)])
            out = self._gate_and_write(res, deliveries)
            if asked and self.written > written_before:
                # It took a question to get here. Anything worth remembering?
                out += self._learn(sender, texts, last_question, res, deliveries)
            return out

        weak = "identity" if ident < CONFIDENCE_THRESHOLD else "content"
        question = res.get("clarifying_question") or \
            "Maaf, bisa dikonfirmasi lagi detailnya?"
        self.trace.log("resolver",
                       f"Below threshold on {weak} confidence — asking rather than guessing")
        return self._ask_or_handoff(candidate, sender, question, texts,
                                    asked, last_question,
                                    f"Below threshold on {weak} confidence", guess)

    def _ask_or_handoff(self, candidate: dict, sender: str, question: str,
                        texts: list[str], asked: int, last_question: str,
                        reason: str, guess: str | None = None) -> list[str]:
        """Ask another question if the budget allows. Hand off if Runa has
        already asked the maximum, or would just repeat itself."""
        if asked >= MAX_QUESTIONS_PER_EVENT:
            return self._handoff("Maaf, saya masih belum yakin",
                                 detail=f"{reason} after {asked} question(s)")
        if question.strip() == last_question.strip():
            return self._handoff("Maaf, saya masih belum yakin",
                                 detail=f"{reason} — would repeat the same question")
        return self._ask_back(candidate, sender, question, texts, asked + 1, guess)

    def _ask_back(self, candidate: dict, sender: str, question: str,
                  texts: list[str], asked: int = 1,
                  guess: str | None = None) -> list[str]:
        """Park the event and ask the person who reported it."""
        self.pending.setdefault(self._who(sender), []).append(
            Pending(candidate, sender, question, list(texts), asked, guess))
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
        self._drop_stale_questions(res["delivery_id"], res["event_type"])

        delivery = {d["InboundDelivery"]: d for d in deliveries}[res["delivery_id"]]
        did = res["delivery_id"]
        ref = f"telegram:{len(self.conversation)}"
        kind = res["event_type"]

        if kind == "GOODS_RECEIPT_DISCREPANCY":
            doc = agents.post_shortage(did, delivery["Material"],
                                       res["quantity_short"], ref)
            self._wrote(f"Posted material document: {res['quantity_short']} short",
                        did, document=doc["MaterialDocument"])
            self.undo_stack.append(Undo(
                "shortage", did,
                f"kurang {res['quantity_short']} {delivery['DeliveryQuantityUnit']} "
                f"pada DO {did} (dokumen {doc['MaterialDocument']})",
                document=doc["MaterialDocument"]))
            return [f"Tercatat di SAP: kurang {res['quantity_short']} "
                    f"{delivery['DeliveryQuantityUnit']} pada DO {did}. "
                    f"Dokumen {doc['MaterialDocument']}."]

        if kind == "QUALITY_COMPLAINT":
            qn = agents.post_quality_notification(
                did, delivery["Material"], res["defect_description"],
                res.get("defect_code") or "OTHER", ref)
            self._wrote(f"Created quality notification: {res['defect_description']}",
                        did, document=qn["QualityNotification"])
            self.undo_stack.append(Undo(
                "quality", did,
                f"keluhan kualitas DO {did} (notifikasi {qn['QualityNotification']})",
                document=qn["QualityNotification"]))
            return [f"Tercatat di SAP: keluhan kualitas untuk DO {did} "
                    f"({res.get('defect_code') or 'OTHER'}). "
                    f"Notifikasi {qn['QualityNotification']}."]

        # ETA_CHANGE: the CODE decides early vs late, from the actual times
        old = datetime.fromisoformat(delivery["PlannedGoodsReceipt"])
        new = datetime.fromisoformat(res["new_eta"])
        status = "Early" if new < old else "Delayed"
        reason = res.get("reason_code") or ("EARLY_DISPATCH" if status == "Early" else "OTHER")

        before = {k: delivery.get(k) for k in
                  ("PlannedGoodsReceipt", "Status", "DelayReasonCode", "SourceMessageRef")}
        updated = agents.post_eta_change(did, res["new_eta"], status, reason, ref)
        new_hm = res["new_eta"].split("T")[1][:5]
        self._wrote(f"Patched arrival time to {new_hm} ({status})", did,
                    reason_code=reason)
        self.undo_stack.append(Undo(
            "restore", did,
            f"estimasi tiba DO {did} {old:%H:%M} → {new_hm}", before=before))
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
        if conflict and self.defer_planning and conflict[0] == "LATE":
            self._deferred = (updated, conflict)
            notice = "⏳ Jadwal ini bentrok dengan jam dock. Runa menyusun opsi untuk supervisor…"
            self.trace.log("agent", notice)
            out.append(notice)
        elif conflict:
            self.trace.log("conflict",
                           f"New time conflicts with dock hours ({conflict[0]}) — "
                           f"surfacing to a human unprompted")
            message = self._escalate(updated, *conflict)
            self.trace.log("agent", message)
            out.append(message)
        return out

    def finish_escalation(self) -> list[str]:
        """Telegram only: the planning that handle() put off so the driver
        got his SAP confirmation at once. Never raises."""
        if not self._deferred:
            return []
        delivery, (kind, fixed) = self._deferred
        self._deferred = None
        try:
            self.trace.log("conflict", f"New time conflicts with dock hours ({kind}) — "
                                       f"surfacing to a human unprompted")
            message = self._escalate(delivery, kind, fixed)
        except Exception as e:                    # never lose the escalation
            self.trace.log("planner", f"Escalation failed ({type(e).__name__})")
            message = f"{self.authority.mention()} {fixed}"
            self.pending_decision = PendingDecision(delivery["InboundDelivery"], kind, message)
        self.trace.log("agent", message)
        if self.report:
            agents.report_event({"ts": datetime.now(WIB).strftime("%H:%M"), "sender": "Runa",
                                 "text": message, "decision": "conflict",
                                 "resolved": delivery["InboundDelivery"],
                                 "note": "New time conflicts with dock hours"})
        return [message]

    def _escalate(self, delivery: dict, kind: str, fixed: str) -> str:
        """Open a decision for a supervisor. For a late truck, the Planner
        builds options from the facts and approved rules; its options must
        pass the code checks. If the Planner fails or no option survives,
        the fixed a/b options are used, so an escalation is never lost."""
        import facts
        import planner
        did = delivery["InboundDelivery"]
        plan = None
        if kind == "LATE":
            at = now()
            eta = delivery["PlannedGoodsReceipt"].split("T")[1][:5]
            problem = (f"DO {did} perkiraan tiba {eta}, tapi {delivery['ReceivingDock']} "
                       f"tutup {delivery['DockClosingTime']}.")
            try:
                known = [f for f in facts.get_facts() if facts.is_active(f, at)]
                rules = [{"id": r["id"], "text": r["text"]} for r in agents.approved_rules()]
                raw = planner.make_plan(problem, delivery, known, rules, at)
                plan = planner.check_plan(raw, delivery, known, rules, at)
                self.trace.log("planner", f"{len(plan['options'])} options passed the code checks")
                if plan["options"]:
                    try:                          # the second pair of eyes
                        review = planner.review_plan(plan, problem, delivery, known, rules, at)
                        plan = planner.apply_review(plan, review, known, rules)
                        self.trace.log("reviewer", f"{review.get('_model', 'Reviewer')}: "
                                                   f"{len(plan['options'])} options left after "
                                                   f"the review, {len(plan['warnings'])} warnings")
                    except Exception as e:
                        self.trace.log("reviewer", f"Reviewer unavailable ({type(e).__name__}) "
                                                   f"— options keep the Planner's own checks")
                for r in plan["rejected"]:
                    self.trace.log("planner", f"Rejected ({r['by']}): {r['title']} — {r['reason']}")
                if not plan["options"]:
                    # Runa looked and found NO option that keeps every rule.
                    # Say that plainly; the supervisor decides, knowing why.
                    self.trace.log("planner", "No option keeps every rule — the supervisor decides")
                    reasons = [f"✗ {r['title']}: {r['reason']}" for r in sorted(
                        (r for r in plan["rejected"] if r.get("show")),
                        key=lambda r: r.get("rank", 3))[:3]]
                    message = "\n".join(
                        [f"⚠️ {problem}",
                         "Runa tidak menemukan opsi yang memenuhi semua aturan:"] + reasons +
                        [f"{self.authority.mention()} mohon keputusan Ibu/Bapak, misalnya "
                         f"menambah kru lembur. Kalau tetap memakai opsi biasa: "
                         f"(a) perpanjang shift, (b) reschedule besok pagi "
                         f"{delivery.get('DockOpeningTime') or '06:00'}."])
                    self.pending_decision = PendingDecision(did, kind, message)
                    return message
            except Exception as e:
                self.trace.log("planner", f"Planner unavailable ({type(e).__name__}) — "
                                          f"using the fixed a/b")
                plan = None
            if plan:
                message = planner.describe(plan, problem, delivery, known, rules, at,
                                           self.authority.mention())
                self.pending_decision = PendingDecision(did, kind, message, plan)
                return message
        message = f"{self.authority.mention()} {fixed}"
        self.pending_decision = PendingDecision(did, kind, message)
        return message

    def _drop_stale_questions(self, did: str, kind: str):
        """This event is going into SAP now. Any OTHER open question about the
        same delivery and the same kind of event is already answered — forget
        it, so a later "p" or "ok" can't wake it up and report it twice."""
        for who in list(self.pending):
            keep = [p for p in self.pending[who]
                    if not (p.delivery_id == did
                            and p.candidate.get("event_type") == kind)]
            if len(keep) < len(self.pending[who]):
                self.trace.log("policy", f"Closed {len(self.pending[who]) - len(keep)} "
                                         f"older question(s) about DO {did} — already recorded")
            if keep:
                self.pending[who] = keep
            else:
                del self.pending[who]

    def _wrote(self, summary: str, did: str, **detail):
        self.written += 1
        self.trace.log("transactor", summary, delivery_id=did, **detail)

    # ========================================================== decisions
    def decision_summary(self) -> dict | None:
        """The open escalation, for the dashboard: which delivery, and the
        options as letter + title. None when nothing is waiting."""
        pd = self.pending_decision
        if not pd:
            return None
        if pd.plan:
            options = [{"label": o["label"], "title": o["title"]} for o in pd.plan["options"]]
        elif pd.kind == "LATE":
            options = [{"label": "a", "title": "Perpanjang shift dock sampai 23:59"},
                       {"label": "b", "title": "Reschedule besok pagi saat dock buka"}]
        else:
            options = [{"label": "a", "title": "Buka dock lebih awal"},
                       {"label": "b", "title": "Truk menunggu sampai dock buka"}]
        return {"delivery": pd.delivery_id, "kind": pd.kind, "options": options,
                "message": pd.options}

    def choose_from_dashboard(self, choice: str, who: str = "Supervisor (dashboard)") -> list[str]:
        """A supervisor picked an option on the dashboard (checked by PIN in
        runa-sap). Same path as "opsi a" in Telegram: same write, same log,
        same /undo. Never raises."""
        if not self.pending_decision:
            return []
        start = len(self.trace.events)
        text = f"opsi {choice} (dipilih di dashboard)"
        agents.set_source(sender=who, text=text, agent="Runa")
        self.conversation.append(f"{who}: {text}")
        self.trace.log("human", f"{who}: {text}")
        self._decider = who
        try:
            replies = self._decide(text, choice=choice) or []
        except agents.ModelError as e:
            replies = self._handoff("Sistem AI sedang bermasalah", detail=str(e))
        except Exception as e:
            replies = self._handoff("Ada gangguan di sistem", detail=f"{type(e).__name__}: {e}")
        if self.report:
            try:
                self._report(who, text, self.trace.events[start:])
            except Exception as e:
                print(f"[report] skipped: {type(e).__name__}: {e}")
        return replies

    def _decide(self, message: str, choice: str | None = None) -> list[str] | None:
        """Someone may be answering the open escalation. None = they weren't.
        choice: already known (the dashboard's button), so the AI isn't asked."""
        pd = self.pending_decision
        letters = [o["label"] for o in pd.plan["options"]] if pd.plan else ["a", "b"]
        result = ({"choice": choice, "reasoning": "chosen on the dashboard"} if choice
                  else agents.interpret_decision(message, pd.options))
        if result.get("choice") not in letters:
            self.trace.log("resolver", "Not an answer to the open escalation — continuing")
            return None

        self.trace.log("resolver",
                       f"Human chose option {result['choice']} — {result.get('reasoning', '')}")
        self.pending_decision = None
        delivery = {d["InboundDelivery"]: d
                    for d in agents.get_open_deliveries()}[pd.delivery_id]
        before = {k: delivery.get(k) for k in
                  ("PlannedGoodsReceipt", "Status", "DockClosingTime",
                   "ResolutionNote", "SourceMessageRef", "CarrierContact",
                   "ReceivingDock")}
        ref = f"telegram:{len(self.conversation)}"
        agents.update_source(agent="decision", option=result["choice"])
        if pd.plan:
            import planner
            option = next(o for o in pd.plan["options"] if o["label"] == result["choice"])
            agents.update_source(option=f"{option['label']}: {option['title']}")
            confirmation = planner.apply_option(delivery, option, ref)
            agents.log_decision({"delivery": pd.delivery_id, "kind": pd.kind,
                                 "options": pd.plan["options"],
                                 "rejected": pd.plan["rejected"],
                                 "chosen": result["choice"], "by": self._decider,
                                 "message": message})
        else:
            confirmation = agents.apply_decision(delivery, pd.kind, result["choice"], ref)
        self._wrote(f"Executed option {result['choice']}", pd.delivery_id)
        self.undo_stack.append(Undo(
            "restore", pd.delivery_id,
            f"opsi {result['choice']} untuk DO {pd.delivery_id}", before=before))
        self.trace.log("agent", confirmation)
        return [confirmation]

    # ============================================================ notebook
    # Runa may PROPOSE what it learned; only a supervisor can make it a note.
    # Notes help the AI tell WHICH delivery a message is about. They can't
    # touch the rulebook: thresholds, limits and checks stay in the code.

    def _learn(self, sender: str, texts: list[str], question: str,
               res: dict, deliveries: list[dict]) -> list[str]:
        """After a write that needed a question: propose a note, if there's
        one lasting fact that would have saved the question."""
        delivery = {d["InboundDelivery"]: d for d in deliveries}[res["delivery_id"]]
        try:
            idea = agents.draft_note(sender, texts, question, delivery)
            text = " ".join(str(idea.get("note") or "").split())
            if not text or len(text) > 200 or checks.looks_like_injection(text):
                return []
            note = agents.add_note(text, status="proposed", source="learned", by="Runa")
        except Exception as e:                  # learning is a bonus, never a blocker
            self.trace.log("notebook", f"No proposal — {type(e).__name__}: {e}")
            return []
        if not note:                            # the notebook already has it
            return []
        self.trace.log("notebook", f"Proposed note #{note['id']}: {text}")
        return [f"📒 Usulan catatan #{note['id']}: \"{text}\"\n"
                f"{self.authority.mention()} balas /setuju {note['id']} untuk "
                f"menyimpan, atau /tolak {note['id']}."]

    def teach(self, sender: str, text: str, sender_id=None) -> list[str]:
        """/catat <text> — a supervisor writes a note directly."""
        if not self.authority.is_supervisor(sender, sender_id):
            return ["Maaf, hanya supervisor yang bisa menambah catatan."]
        text = " ".join(text.split())
        if not text:
            return ["Tulis catatannya setelah /catat, contoh: "
                    "/catat Pak Joko itu supir CV Mitra Kemasan"]
        if checks.looks_like_injection(text):
            return ["Catatan ini terlihat seperti perintah ke sistem, jadi tidak disimpan."]
        note = agents.add_note(text, status="approved", source="supervisor", by=sender)
        if not note:
            return ["Catatan itu sudah ada di buku catatan Runa."]
        self.trace.log("notebook", f"{sender} added note #{note['id']}: {text}")
        return [f"📒 Dicatat (#{note['id']}): \"{text}\""]

    def review(self, sender: str, note_id: int, approve: bool,
               sender_id=None) -> list[str]:
        """/setuju N or /tolak N — a supervisor decides on a proposed note."""
        if not self.authority.is_supervisor(sender, sender_id):
            return ["Maaf, hanya supervisor yang bisa menyetujui catatan."]
        current = next((n for n in agents.get_notebook(fresh=True)
                        if n["id"] == note_id), None)
        if not current:
            return [f"Catatan #{note_id} tidak ditemukan."]
        if current["status"] != "proposed":
            return [f"Catatan #{note_id} sudah {current['status']}, tidak menunggu keputusan."]
        status = "approved" if approve else "rejected"
        agents.review_note(note_id, status, sender)
        self.trace.log("notebook", f"{sender} {status} note #{note_id}")
        if approve:
            return [f"📒 Disimpan (#{note_id}). Mulai sekarang Runa memakai catatan ini."]
        return [f"Catatan #{note_id} ditolak, tidak dipakai."]

    def forget(self, sender: str, note_id: int, sender_id=None) -> list[str]:
        """/hapus N — a supervisor takes a note out of use (it stays on record)."""
        if not self.authority.is_supervisor(sender, sender_id):
            return ["Maaf, hanya supervisor yang bisa menghapus catatan."]
        if not agents.review_note(note_id, "removed", sender):
            return [f"Catatan #{note_id} tidak ditemukan."]
        self.trace.log("notebook", f"{sender} removed note #{note_id}")
        return [f"Catatan #{note_id} tidak dipakai lagi."]

    def learn_rules(self, sender: str, sender_id=None) -> list[str]:
        """/pelajari — a supervisor asks Runa to look for rules in what it
        has read so far. Rules are only PROPOSED; /setuju makes them count."""
        import facts
        import rules
        if not self.authority.is_supervisor(sender, sender_id):
            return ["Maaf, hanya supervisor yang bisa meminta Runa belajar aturan."]
        known = facts.get_facts()[-300:]
        notebook = [n["text"] for n in agents.get_notebook(fresh=True)]
        kept, dropped = rules.propose_rules(known, notebook)
        proposed = []
        for r in kept:
            note = agents.add_note(r["rule"], status="proposed", source="learned",
                                   by="Runa", kind="rule", evidence=r["evidence"])
            if note:
                proposed.append(note)
        self.trace.log("notebook", f"Learned {len(proposed)} rules "
                                   f"({len(dropped)} dropped by the checks)")
        if not proposed:
            return ["Belum ada aturan baru yang cukup kuat buktinya."]
        lines = [f"📒 Runa mengusulkan {len(proposed)} aturan dari percakapan:"]
        for n in proposed:
            seen = ", ".join(e["at"][5:16].replace("T", " ") for e in n["evidence"][:3])
            lines.append(f"#{n['id']} {n['text']}\n   (dari pesan {seen})")
        lines.append(f"{self.authority.mention()} balas /setuju <nomor> atau /tolak <nomor>.")
        return ["\n".join(lines)]

    def notebook_text(self) -> list[str]:
        """/catatan — show what Runa knows, and what is waiting for approval."""
        notes = agents.get_notebook(fresh=True)
        used = [n for n in notes if n["status"] == "approved"]
        waiting = [n for n in notes if n["status"] == "proposed"]
        lines = ["📒 Buku catatan Runa"]
        lines += [f"#{n['id']} {n['text']}" for n in used
                  if n.get("kind", "note") == "note"] or ["(kosong)"]
        rules_used = [n for n in used if n.get("kind") == "rule"]
        if rules_used:
            lines += ["", "Aturan gudang (dipakai saat menyusun opsi):"]
            lines += [f"#{n['id']} {n['text']}" for n in rules_used]
        if waiting:
            lines += ["", "Menunggu persetujuan supervisor:"]
            lines += [f"#{n['id']} {n['text']}  →  /setuju {n['id']} atau /tolak {n['id']}"
                      for n in waiting]
        return ["\n".join(lines)]

        # ================================================================ undo
    def undo(self, sender: str) -> list[str]:
        """Supervisor command (/undo): reverse Runa's most recent SAP write.

        Nothing is deleted. SAP gets a NEW transaction that cancels the old
        one, so the audit trail shows both: what Runa did, and who reversed it.
        Then Runa checks consequences again, exactly as after a normal write."""
        if not self.undo_stack:
            return ["Belum ada catatan Runa di sesi ini yang bisa dibatalkan."]
        last = self.undo_stack.pop()
        agents.set_source(sender=sender, text=f"/undo: {last.label}", agent="undo")
        ref = f"telegram:undo:{len(self.conversation)}"
        try:
            if last.kind == "restore":
                restored = agents.restore_delivery(last.delivery_id, last.before)
            elif last.kind == "shortage":
                restored = None
                agents.cancel_material_document(last.document, ref)
            else:
                restored = None
                agents.cancel_quality_notification(last.document, ref)
        except Exception as e:                  # nothing changed: keep it undoable
            self.undo_stack.append(last)
            self.trace.log("handoff", f"Undo failed — {type(e).__name__}: {e}")
            return [f"Gagal membatalkan {last.label}. SAP tidak berubah, "
                    f"silakan coba lagi atau cek manual. 🙏"]

        self.trace.log("transactor", f"Undone by {sender}: {last.label}",
                       delivery_id=last.delivery_id)
        out = [f"↩️ Dibatalkan oleh {sender}: {last.label}. "
               f"Data SAP sudah dikembalikan; jejaknya tetap tercatat."]

        # Putting a time or schedule back may make the open escalation untrue.
        # (Undoing a shortage or a complaint doesn't touch the schedule.)
        if (last.kind == "restore" and self.pending_decision
                and self.pending_decision.delivery_id == last.delivery_id):
            self.pending_decision = None
            self.trace.log("policy", "Undo — the open escalation for this delivery is cancelled")

        # Putting a time back can re-create a dock conflict. Check, like after any write.
        conflict = agents.check_conflict(restored) if restored else None
        if conflict:
            self.trace.log("conflict", f"After undo the dock conflict is back ({conflict[0]})")
            out.append(self._escalate(restored, *conflict))

        if self.report:
            agents.report_event({
                "ts": datetime.now(WIB).strftime("%H:%M"),
                "sender": sender, "text": "/undo", "decision": "undone",
                "resolved": last.delivery_id, "doc": last.document,
                "note": f"Undone: {last.label}",
            })
            if conflict:
                agents.report_event({
                    "ts": datetime.now(WIB).strftime("%H:%M"),
                    "sender": "Runa", "text": out[-1], "decision": "conflict",
                    "resolved": last.delivery_id,
                    "note": "After undo the dock conflict is back",
                })
        return out

    # =========================================================== dashboard
    def _report(self, sender: str, message: str, steps: list[dict]):
        """Turn the trace lines of ONE message into a dashboard event."""
        actors = [s["actor"] for s in steps]

        # 1. What did Runa decide? (the order matters: handoff beats everything)
        if "handoff" in actors:
            decision = "handoff"
        elif "transactor" in actors:
            decision = "written"
        elif any("question_number" in s for s in steps):
            decision = "asked"
        else:
            decision = "ignored"

        # 2. The details, picked out of the trace lines
        scored = [s for s in steps if "identity_confidence" in s]
        scores = scored[-1] if scored else {}
        delivery = next((s["delivery_id"] for s in reversed(steps)
                         if s.get("delivery_id")), None)
        document = next((s["document"] for s in steps if s.get("document")), None)
        question = next((s["summary"] for s in steps if "question_number" in s), None)
        reason = next((s["summary"] for s in reversed(steps)
                       if s["actor"] in ("listener", "resolver", "policy",
                                         "transactor", "handoff")), "")
        hedge = checks.hedge_word(message)

        # 3. Where the time went: each step = time since the line before it
        timings = {}
        for before, step in zip(steps, steps[1:]):
            name = STEP_NAMES.get(step["actor"])
            if name:
                ms = round((step["t"] - before["t"]) * 1000)
                timings[name] = timings.get(name, 0) + ms

        agents.report_event({
            "ts": datetime.now(WIB).strftime("%H:%M"),
            "sender": sender,
            "text": message,
            "decision": decision,
            "e": scores.get("identity_confidence") or 0,
            "v": scores.get("content_confidence") or 0,
            "hedges": [hedge] if hedge else [],
            "resolved": delivery,
            "doc": document,
            "q": question,
            "note": reason,
            "timings": timings,
        })

        # 4. Runa raised a dock conflict on its own? That's a second event.
        for i, step in enumerate(steps):
            if step["actor"] == "conflict":
                posted = next((s["summary"] for s in steps[i + 1:] if s["actor"] == "agent"),
                              step["summary"])          # the message Runa posted
                agents.report_event({
                    "ts": datetime.now(WIB).strftime("%H:%M"),
                    "sender": "Runa",
                    "text": posted,
                    "decision": "conflict",
                    "resolved": delivery,
                    "note": step["summary"],
                })

    # ============================================================ handoff
    def _handoff(self, reason: str, detail: str = "") -> list[str]:
        """The safety net: say what happened, pass it to a human, never guess."""
        self.handoffs += 1
        self.trace.log("handoff", f"Forwarded to human — {detail or reason}")
        message = f"{reason}. {HANDOFF_PREFIX}. 🙏"
        self.trace.log("agent", message)
        return [message]
