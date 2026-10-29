"""
Telegram transport for Runa.

Runa joins the group as a declared participant. It reads every message
(BotFather privacy must be Disabled), decides what matters, and replies in
channel when it needs to.

Supervisors = the group's ADMINS. Only they can choose options when Runa
escalates a conflict, and only they can /reset. To make someone a supervisor,
make them an admin of the group in Telegram. Nothing to change in the code.

    uvicorn sap_mock:app --port 8000    # terminal 1
    python bot.py                        # terminal 2
"""
import asyncio
import logging
import time

import httpx

from telegram import Update
from telegram.ext import (Application, ContextTypes, MessageHandler,
                          CommandHandler, filters)

from config import SAP_BASE, SAP_HEADERS, TELEGRAM_TOKEN
import agents
from pipeline import Authority, Runa
from trace import Trace

logging.basicConfig(level=logging.WARNING)

trace = Trace()
sessions: dict[int, Runa] = {}
background: set = set()          # side jobs still running (kept so they finish)
ADMIN_REFRESH_SECONDS = 300


def session(chat_id: int) -> Runa:
    if chat_id not in sessions:
        # In Telegram, supervisors come ONLY from the group's admin list —
        # never from a name, which anyone could share.
        sessions[chat_id] = Runa(trace, Authority(names=[]), report=True)
        sessions[chat_id].defer_planning = True     # confirm first, then plan
    return sessions[chat_id]


async def refresh_supervisors(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                              runa: Runa, force: bool = False):
    """Ask Telegram who the group's admins are. Cached for 5 minutes."""
    auth = runa.authority
    if not force and time.time() - auth.refreshed < ADMIN_REFRESH_SECONDS:
        return
    chat = update.effective_chat
    if chat.type == "private":                    # testing alone with the bot
        u = update.effective_user
        auth.set_admins([(u.id, u.username, u.first_name)])
    else:
        try:
            admins = await ctx.bot.get_chat_administrators(chat.id)
            auth.set_admins([(a.user.id, a.user.username, a.user.first_name)
                             for a in admins if not a.user.is_bot])
        except Exception as e:                    # keep the last known list
            logging.warning("Could not fetch admins: %s", e)
    auth.refreshed = time.time()


# ------------------------------------------------- decisions on the dashboard
# runa-sap shows the open escalation on the dashboard, so a supervisor can pick
# an option there (with the supervisor PIN). The bot keeps runa-sap up to date
# and carries out a dashboard choice exactly as an "opsi a" typed here.
_published: dict[int, str] = {}       # what runa-sap last heard, per chat


def _publish_decision(chat_id: int, runa: Runa) -> None:
    """Tell runa-sap which escalation is open now (or none). Only on change."""
    import json
    summary = runa.decision_summary()
    key = json.dumps(summary, sort_keys=True, default=str)
    if _published.get(chat_id) == key:
        return
    try:
        httpx.post(f"{SAP_BASE}/runa/decision", json={"decision": summary, "chat_id": chat_id},
                   headers=SAP_HEADERS, timeout=5).raise_for_status()
        _published[chat_id] = key
    except httpx.HTTPError as e:
        logging.warning("Could not publish the open decision: %s", e)


async def sync_decision(chat_id: int, runa: Runa) -> None:
    await asyncio.to_thread(_publish_decision, chat_id, runa)


def _next_choice() -> dict | None:
    try:
        r = httpx.post(f"{SAP_BASE}/runa/decision/next", headers=SAP_HEADERS, timeout=5)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


async def dashboard_choices(app: Application) -> None:
    """Every 2 seconds: did a supervisor pick an option on the dashboard?
    Every 30 seconds the open escalations are sent again, so a runa-sap
    restart (a deploy, a new setting) can't hide them from the dashboard."""
    rounds = 0
    while True:
        await asyncio.sleep(2)
        rounds += 1
        if rounds % 15 == 0:
            _published.clear()
            for chat_id, runa in list(sessions.items()):
                await sync_decision(chat_id, runa)
        try:
            choice = await asyncio.to_thread(_next_choice)
            if not choice or choice.get("chat_id") not in sessions:
                continue
            chat_id = choice["chat_id"]
            runa = sessions[chat_id]
            pd = runa.pending_decision
            if not pd or pd.delivery_id != choice.get("delivery"):
                await sync_decision(chat_id, runa)      # stale: tell the dashboard
                continue
            replies = await asyncio.to_thread(runa.choose_from_dashboard, choice["choice"])
            await app.bot.send_message(
                chat_id, f"🖥️ Supervisor memilih opsi {choice['choice']} lewat dashboard Runa.")
            for reply in replies:
                await app.bot.send_message(chat_id, reply)
            await sync_decision(chat_id, runa)
        except Exception as e:                   # never stop listening for choices
            logging.warning("Dashboard choice failed: %s", e)


async def on_start(app: Application) -> None:
    """When the bot starts it has no open escalation: clear what the dashboard
    shows, then start listening for dashboard choices."""
    try:
        await asyncio.to_thread(lambda: httpx.post(
            f"{SAP_BASE}/runa/decision", json={"decision": None}, headers=SAP_HEADERS, timeout=5))
    except httpx.HTTPError:
        pass
    task = asyncio.create_task(dashboard_choices(app))
    background.add(task)


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # A photo with a caption ("DO 4521 basah semua" + a picture) is read by
    # its caption. A photo with no words is left alone — Runa only reads text.
    text = update.message and (update.message.text or update.message.caption)
    if not text:
        return

    user = update.message.from_user
    sender = user.first_name or user.username or "Unknown"
    runa = session(update.message.chat_id)
    await refresh_supervisors(update, ctx, runa)

    # The LLM calls are blocking; keep the event loop responsive.
    replies = await asyncio.to_thread(runa.handle, sender, text, user.id)
    for reply in replies:
        await update.message.reply_text(reply)

    # A late truck: the driver has his confirmation; now build the options.
    for reply in await asyncio.to_thread(runa.finish_escalation):
        await update.message.reply_text(reply)
    await sync_decision(update.message.chat_id, runa)

    # After replying, in the background so the next message isn't kept
    # waiting: read the constraints in this message (facts.py) and any
    # delivery stage it reports (stages.py, on the second model).
    async def side_jobs():
        await asyncio.to_thread(runa.observe, sender, text)
        for reply in await asyncio.to_thread(runa.track_stages, sender, text):
            await update.message.reply_text(reply)
    task = asyncio.create_task(side_jobs())
    background.add(task)
    task.add_done_callback(background.discard)


async def on_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    runa = session(update.message.chat_id)
    await refresh_supervisors(update, ctx, runa, force=True)
    sup = runa.authority.mention() if runa.authority.admin_mentions else "(belum ada)"
    await update.message.reply_text(
        f"Runa aktif. Transaksi tercatat sesi ini: {runa.written}. "
        f"Pesan diproses: {len(runa.conversation)}.\n"
        f"Supervisor (admin grup): {sup}\n"
        f"Otak AI: {agents.brain_name()}"
    )


async def on_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Demo helper: put SAP and Runa's memory back to the start. Admins only."""
    runa = session(update.message.chat_id)
    await refresh_supervisors(update, ctx, runa, force=True)
    if update.effective_user.id not in runa.authority.admin_ids:
        await update.message.reply_text("Maaf, /reset hanya untuk admin grup.")
        return
    httpx.post(f"{SAP_BASE}/_debug/reset", headers=SAP_HEADERS, timeout=5)
    sessions.pop(update.message.chat_id, None)
    _published.pop(update.message.chat_id, None)       # runa-sap's reset cleared it too
    await update.message.reply_text("Reset. Data SAP dan memori Runa kembali ke awal.")


async def on_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Supervisor: reverse Runa's most recent SAP write. Admins only."""
    runa = session(update.message.chat_id)
    await refresh_supervisors(update, ctx, runa, force=True)
    user = update.effective_user
    if user.id not in runa.authority.admin_ids:
        await update.message.reply_text("Maaf, /undo hanya untuk supervisor (admin grup).")
        return
    name = user.first_name or user.username or "Supervisor"
    replies = await asyncio.to_thread(runa.undo, name)
    for reply in replies:
        await update.message.reply_text(reply)
    await sync_decision(update.message.chat_id, runa)


# --------------------------------------------------------------- notebook
async def _notebook_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                            action: str):
    """/catat, /setuju, /tolak, /hapus, /catatan. Who may do what is checked
    in pipeline.py, by Telegram user ID — group admins are the supervisors."""
    runa = session(update.message.chat_id)
    await refresh_supervisors(update, ctx, runa, force=action != "list")
    user = update.effective_user
    name = user.first_name or user.username or "Unknown"
    try:
        if action == "list":
            replies = await asyncio.to_thread(runa.notebook_text)
        elif action == "learn":
            await update.message.reply_text("Sebentar, Runa membaca ulang catatan faktanya…")
            replies = await asyncio.to_thread(runa.learn_rules, name, user.id)
        elif action == "teach":
            replies = await asyncio.to_thread(runa.teach, name, " ".join(ctx.args), user.id)
        else:
            if not ctx.args or not ctx.args[0].lstrip("#").isdigit():
                await update.message.reply_text(f"Tulis nomornya, contoh: /{action} 4")
                return
            note_id = int(ctx.args[0].lstrip("#"))
            if action == "hapus":
                replies = await asyncio.to_thread(runa.forget, name, note_id, user.id)
            else:
                replies = await asyncio.to_thread(runa.review, name, note_id,
                                                  action == "setuju", user.id)
    except Exception as e:
        logging.warning("Notebook command failed: %s", e)
        replies = ["Maaf, buku catatan sedang tidak bisa dibuka. Coba lagi sebentar. 🙏"]
    for reply in replies:
        await update.message.reply_text(reply)


async def on_catat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _notebook_command(update, ctx, "teach")


async def on_setuju(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _notebook_command(update, ctx, "setuju")


async def on_tolak(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _notebook_command(update, ctx, "tolak")


async def on_hapus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _notebook_command(update, ctx, "hapus")


async def on_catatan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _notebook_command(update, ctx, "list")


async def on_pelajari(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _notebook_command(update, ctx, "learn")


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN missing from .env")

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(on_start).build()
    app.add_handler(CommandHandler("status", on_status))
    app.add_handler(CommandHandler("reset", on_reset))
    app.add_handler(CommandHandler("undo", on_undo))
    app.add_handler(CommandHandler("catat", on_catat))
    app.add_handler(CommandHandler("setuju", on_setuju))
    app.add_handler(CommandHandler("tolak", on_tolak))
    app.add_handler(CommandHandler("hapus", on_hapus))
    app.add_handler(CommandHandler("catatan", on_catatan))
    app.add_handler(CommandHandler("pelajari", on_pelajari))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
                                   on_message))

    print("Runa listening. Add the bot to your group and start talking.")
    print("Reminder: BotFather → /setprivacy → Disable, or Runa only sees @mentions.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
