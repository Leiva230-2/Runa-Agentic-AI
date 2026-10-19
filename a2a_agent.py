"""
Runa as an A2A agent, so SAP Joule can ask it questions.

A supervisor types in Joule: "Apa yang terjadi dengan DO 4521 hari ini?"
Joule calls this agent over A2A (agent-to-agent), through a BTP destination.
Runa answers from what it actually knows: the SAP records and the events it
recorded from the operations chat.

This agent only READS. Nothing that arrives here can change SAP; writing
stays in the Telegram pipeline, behind the confidence scores and the rules.

    answer(question, state) -> text      the logic, plain Python, testable
    add_a2a_routes(app, get_state)        the A2A wrapper, at /a2a on runa-sap
"""
import json
import os
import re

DECISION_TEXT = {
    "written": "ditulis ke SAP",
    "asked": "Runa bertanya dulu",
    "handoff": "diteruskan ke supervisor",
    "ignored": "diabaikan (obrolan)",
    "conflict": "konflik jadwal dock",
    "undone": "dibatalkan supervisor",
}


# ------------------------------------------------------------------ logic --
def find_deliveries(question: str, known: list[str]) -> list[str]:
    """Delivery numbers in the question: the full number, or its last
    digits when they match exactly one delivery ("DO 4521")."""
    found = []
    for digits in re.findall(r"\d{3,10}", question):
        if digits in known:
            match = [digits]
        else:
            match = [d for d in known if d.endswith(digits)]
        if len(match) == 1 and match[0] not in found:
            found.append(match[0])
    return found


def _hm(iso: str | None) -> str:
    """'2026-09-09T23:00:00' -> '09/09 23:00'."""
    if not iso or "T" not in iso:
        return iso or "-"
    date, time = iso.split("T")
    return f"{date[8:10]}/{date[5:7]} {time[:5]}"


def _closes_schedule(e: dict) -> bool:
    """Did this event settle the delivery's schedule? A new arrival time, a
    supervisor's option, or an undo of either. A shortage or a complaint
    doesn't: the dock question is still open after those."""
    note = e.get("note") or ""
    return (note.startswith(("Patched arrival time", "Executed option"))
            or (e["decision"] == "undone" and ("estimasi tiba" in note or "opsi" in note)))


def _open_conflict(events: list[dict], did: str) -> dict | None:
    """The newest dock conflict for this delivery that nothing has settled."""
    for e in reversed(events):
        if e.get("resolved") != did:
            continue
        if e["decision"] == "conflict":
            return e
        if _closes_schedule(e):
            return None
    return None


def describe_delivery(did: str, state: dict) -> str:
    d = state["deliveries"][did]
    events = state["events"]
    lines = [f"**DO {did}** — {d.get('SupplierName')} "
             f"({d.get('MaterialDescription')}, {d.get('DeliveryQuantity')} "
             f"{d.get('DeliveryQuantityUnit')})"]

    # Arrival time, and the original plan if Runa or a supervisor changed it
    changes = [c for c in state["change_log"]
               if c["entity"] == f"A_InboundDelivery('{did}')"
               and "PlannedGoodsReceipt" in c["changes"]]
    eta = f"Estimasi tiba: {_hm(d.get('PlannedGoodsReceipt'))}"
    if changes:
        eta += f" (rencana awal {_hm(changes[0]['before'].get('PlannedGoodsReceipt'))})"
    lines.append(f"- Status di SAP: {d.get('Status')}. {eta}.")
    lines.append(f"- Dock: {d.get('ReceivingDock')}, buka {d.get('DockOpeningTime')}"
                 f"–{d.get('DockClosingTime')}."
                 + (f" Catatan: {d['ResolutionNote']}." if d.get("ResolutionNote") else ""))

    for doc in state["material_documents"]:
        if doc["InboundDelivery"] != did or doc.get("ReversedMaterialDocument"):
            continue
        note = (f" — dibatalkan (dokumen pembalik {doc['ReversalDocument']})"
                if doc.get("ReversalDocument") else "")
        lines.append(f"- Kekurangan barang: {doc['QuantityInDeliveryUnit']:g} "
                     f"{d.get('DeliveryQuantityUnit')}, dokumen {doc['MaterialDocument']}{note}.")
    for qn in state["quality_notifications"]:
        if qn["InboundDelivery"] == did:
            lines.append(f"- Keluhan kualitas: {qn['DefectCode']} — {qn['NotificationText']} "
                         f"(notifikasi {qn['QualityNotification']}, "
                         f"{qn.get('NotificationStatus', 'OUTSTANDING')}).")

    conflict = _open_conflict(events, did)
    if conflict:
        lines.append("- ⚠️ Menunggu keputusan supervisor: tiba setelah dock tutup "
                     "(opsi a/b ditanyakan di grup Telegram).")

    history = [e for e in events if e.get("resolved") == did and e["sender"] != "Runa"][-5:]
    if history:
        lines.append("- Dari grup operasional (terbaru di bawah):")
        for e in history:
            lines.append(f"    - {e['ts']} {e['sender']}: \"{e['text']}\" → "
                         f"{DECISION_TEXT.get(e['decision'], e['decision'])}")
    else:
        lines.append("- Belum ada laporan dari grup tentang delivery ini.")
    return "\n".join(lines)


def describe_day(state: dict) -> str:
    events = state["events"]
    people = [e for e in events if e["sender"] != "Runa"]
    counts = {k: sum(e["decision"] == k for e in people) for k in DECISION_TEXT}
    lines = ["**Ringkasan Runa**",
             f"- {len(people)} pesan dari grup diproses: {counts['written']} ditulis ke SAP, "
             f"{counts['asked']} perlu ditanya dulu, {counts['handoff']} diteruskan ke "
             f"supervisor, {counts['ignored']} obrolan diabaikan."]
    lines.append("- Delivery:")
    for did, d in state["deliveries"].items():
        flag = " ⚠️ menunggu keputusan supervisor" if _open_conflict(events, did) else ""
        lines.append(f"    - DO {did} ({d.get('SupplierName')}): {d.get('Status')}, "
                     f"tiba {_hm(d.get('PlannedGoodsReceipt'))}{flag}")
    handoffs = [e for e in people if e["decision"] == "handoff"][-3:]
    if handoffs:
        lines.append("- Diteruskan ke supervisor (terbaru):")
        lines += [f"    - {e['ts']} {e['sender']}: \"{e['text']}\"" for e in handoffs]
    lines.append("Tanyakan satu delivery untuk detailnya, misalnya: \"Bagaimana DO 4521?\"")
    return "\n".join(lines)


def answer(question: str, state: dict) -> str:
    """What Runa tells Joule. Plain code: no AI, no guessing, read-only."""
    question = (question or "").strip()
    known = list(state["deliveries"])
    asked = find_deliveries(question, known)
    if asked:
        return "\n\n".join(describe_delivery(did, state) for did in asked)
    if re.search(r"\b(do|delivery|pengiriman)\b\s*#?\s*\d", question, re.I):
        return ("Nomor delivery itu tidak ada di SAP. Delivery yang terbuka: "
                + ", ".join(f"DO {d}" for d in known) + ".")
    return describe_day(state)


# ------------------------------------------------------------- A2A wrapper --
def _public_url() -> str:
    """This app's own address: from Cloud Foundry when deployed."""
    vcap = json.loads(os.environ.get("VCAP_APPLICATION", "{}"))
    uris = vcap.get("application_uris") or []
    return f"https://{uris[0]}" if uris else "http://127.0.0.1:8000"


def add_a2a_routes(app, get_state):
    """Add the A2A endpoints Joule talks to onto runa-sap's own app.
    get_state() returns live SAP + Runa data.

      GET  /a2a/.well-known/agent-card.json   who Runa is, what it can answer
      POST /a2a  (and /a2a/)                  a question in, Runa's answer out

    Both spellings of the POST address are registered on purpose: a redirect
    from one to the other would drop the question on the way."""
    from a2a.server.agent_execution import AgentExecutor, RequestContext
    from a2a.server.apps.jsonrpc import A2AFastAPIApplication
    from a2a.server.events import EventQueue
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.tasks import InMemoryTaskStore
    from a2a.types import (AgentCapabilities, AgentCard, AgentSkill, Artifact,
                           TaskArtifactUpdateEvent, TaskState, TaskStatus,
                           TaskStatusUpdateEvent, TextPart)

    class RunaExecutor(AgentExecutor):
        async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
            text = answer(context.get_user_input(), get_state())
            await event_queue.enqueue_event(TaskArtifactUpdateEvent(
                task_id=context.task_id, context_id=context.context_id,
                artifact=Artifact(artifactId="runa_answer", name="runa_answer",
                                  parts=[TextPart(text=text)])))
            await event_queue.enqueue_event(TaskStatusUpdateEvent(
                task_id=context.task_id, context_id=context.context_id,
                status=TaskStatus(state=TaskState.completed), final=True))

        async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
            await event_queue.enqueue_event(TaskStatusUpdateEvent(
                task_id=context.task_id, context_id=context.context_id,
                status=TaskStatus(state=TaskState.canceled), final=True))

    card = AgentCard(
        name="Runa",
        description=("Operations agent for an Indonesian warehouse. Runa listens to the "
                     "operations group chat and records delays, shortages and damage in "
                     "SAP. Ask it what happened to an inbound delivery, or for a summary."),
        url=f"{_public_url()}/a2a",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=False),
        skills=[AgentSkill(
            id="delivery_status",
            name="Status inbound delivery",
            description=("Tells what happened to an inbound delivery today: arrival time, "
                         "shortages, damage, open decisions and what the drivers reported "
                         "in the chat. Without a delivery number, a summary of the day."),
            tags=["logistics", "inbound delivery", "warehouse", "SAP"],
            examples=["Apa yang terjadi dengan DO 4521 hari ini?", "Ringkasan gudang hari ini"],
            inputModes=["text/plain"], outputModes=["text/markdown"])],
        defaultInputModes=["text/plain"],
        defaultOutputModes=["text/markdown"],
    )
    handler = DefaultRequestHandler(agent_executor=RunaExecutor(),
                                    task_store=InMemoryTaskStore())
    a2a = A2AFastAPIApplication(agent_card=card, http_handler=handler)
    for rpc_url in ("/a2a", "/a2a/"):
        a2a.add_routes_to_app(app, agent_card_url="/a2a/.well-known/agent-card.json",
                              rpc_url=rpc_url,
                              extended_agent_card_url="/a2a/agent/authenticatedExtendedCard")
