import os
import asyncio
import logging
from telegram import Bot
from telegram.error import TelegramError

log = logging.getLogger("bot")


def get_bot():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    return Bot(token=token)


def send_message(chat_id: str, text: str) -> tuple[bool, str]:
    """Send a message to a Telegram chat. Returns (success, info)."""
    bot = get_bot()
    if bot is None:
        return False, "TELEGRAM_BOT_TOKEN is not set"
    if not chat_id:
        return False, "No channel/chat id configured"

    async def _send():
        async with bot:
            return await bot.send_message(chat_id=chat_id, text=text)

    try:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Run in fresh loop to avoid conflicts
                raise RuntimeError("loop running")
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        msg = loop.run_until_complete(_send())
        return True, f"message_id={msg.message_id}"
    except TelegramError as e:
        log.exception("Telegram send failed")
        return False, f"Telegram error: {e}"
    except Exception as e:
        log.exception("Send failed")
        return False, f"Error: {e}"
