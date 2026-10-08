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

from config import SAP_BASE, TELEGRAM_TOKEN
import agents
from pipeline import Authority, Runa
from trace import Trace

logging.basicConfig(level=logging.WARNING)

trace = Trace()
sessions: dict[int, Runa] = {}
ADMIN_REFRESH_SECONDS = 300


def session(chat_id: int) -> Runa:
    if chat_id not in sessions:
        # In Telegram, supervisors come ONLY from the group's admin list —
        # never from a name, which anyone could share.
        sessions[chat_id] = Runa(trace, Authority(names=[]))
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


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user = update.message.from_user
    sender = user.first_name or user.username or "Unknown"
    runa = session(update.message.chat_id)
    await refresh_supervisors(update, ctx, runa)

    # The LLM calls are blocking; keep the event loop responsive.
    replies = await asyncio.to_thread(runa.handle, sender, update.message.text,
                                      user.id)
    for reply in replies:
        await update.message.reply_text(reply)


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
    httpx.post(f"{SAP_BASE}/_debug/reset", timeout=5)
    sessions.pop(update.message.chat_id, None)
    await update.message.reply_text("Reset. Data SAP dan memori Runa kembali ke awal.")


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN missing from .env")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("status", on_status))
    app.add_handler(CommandHandler("reset", on_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    print("Runa listening. Add the bot to your group and start talking.")
    print("Reminder: BotFather → /setprivacy → Disable, or Runa only sees @mentions.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
