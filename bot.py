"""
Telegram transport for Runa.

Runa joins the group as a declared participant. It reads every message
(BotFather privacy must be Disabled), decides what matters, and replies in
channel when it needs to.

    uvicorn sap_mock:app --port 8000    # terminal 1
    python bot.py                        # terminal 2
"""
import asyncio
import logging

from telegram import Update
from telegram.ext import (Application, ContextTypes, MessageHandler,
                          CommandHandler, filters)

from config import TELEGRAM_TOKEN
from pipeline import Runa
from trace import Trace

logging.basicConfig(level=logging.WARNING)

trace = Trace()
sessions: dict[int, Runa] = {}


def session(chat_id: int) -> Runa:
    if chat_id not in sessions:
        sessions[chat_id] = Runa(trace)
    return sessions[chat_id]


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    sender = update.message.from_user.first_name or "Unknown"
    runa = session(update.message.chat_id)

    # The LLM calls are blocking; keep the event loop responsive.
    replies = await asyncio.to_thread(runa.handle, sender, update.message.text)
    for reply in replies:
        await update.message.reply_text(reply)


async def on_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    runa = session(update.message.chat_id)
    await update.message.reply_text(
        f"Runa aktif. Transaksi tercatat sesi ini: {runa.written}. "
        f"Pesan diproses: {len(runa.conversation)}."
    )


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN missing from .env")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("status", on_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    print("Runa listening. Add the bot to your group and start talking.")
    print("Reminder: BotFather → /setprivacy → Disable, or Runa only sees @mentions.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
