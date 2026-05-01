import os
import asyncio
import threading
import logging
from datetime import datetime
from functools import wraps

import pytz
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from models import db, Message, ScheduledPost, Setting
import scheduler as sched

log = logging.getLogger("bot_commands")

_flask_app = None
_thread = None
_application = None


HELP_TEXT = (
    "📢 *Telegram Auto Poster*\n\n"
    "*Messages*\n"
    "/list — show rotation messages\n"
    "/add <text> — add a message to the rotation\n"
    "/delete <id> — delete a message\n"
    "/enable <id> — enable a message\n"
    "/disable <id> — disable a message\n\n"
    "*Posting*\n"
    "/postnow — post the next message right now\n"
    "/schedule YYYY-MM-DD HH:MM | text — schedule a one-off post (UTC)\n"
    "/scheduled — list pending scheduled posts\n"
    "/cancel <id> — cancel a scheduled post\n\n"
    "*Settings*\n"
    "/status — show current config\n"
    "/channels — list all configured channels\n"
    "/setchannel <a,b,c|me> — replace channel list (comma-separated)\n"
    "/addchannel <id|@name|me> — add a channel\n"
    "/removechannel <id|@name> — remove a channel\n"
    "/setinterval <milliseconds> — auto-post interval in milliseconds\n"
    "/autopost on|off — toggle auto-posting\n\n"
    "/help — show this help"
)


def _admin_id() -> int | None:
    val = os.environ.get("TELEGRAM_ADMIN_ID", "").strip()
    try:
        return int(val) if val else None
    except ValueError:
        return None


def admin_only(handler):
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        return await handler(update, context)

    return wrapper


def _ctx():
    """Run DB ops inside the Flask app context."""
    return _flask_app.app_context()


@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Hi! I'm your auto-poster bot.\nYour chat id: `{update.effective_chat.id}`\n\n" + HELP_TEXT,
        parse_mode="Markdown",
    )


@admin_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


@admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with _ctx():
        total = Message.query.count()
        enabled = Message.query.filter_by(enabled=True).count()
        pending = ScheduledPost.query.filter_by(status="pending").count()
        chans = sched.get_active_channels()
        channel = (f"{len(chans)} channel(s): " + ", ".join(chans)) if chans else "(not set)"
        interval = Setting.get("interval_seconds", "3600000")
        autopost = Setting.get("autopost_enabled", "1") == "1"

    job = sched.scheduler.get_job("hourly_post")
    next_run = "—"
    if job and job.next_run_time:
        next_run = job.next_run_time.astimezone(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    await update.message.reply_text(
        f"📊 *Status*\n"
        f"Channel: `{channel}`\n"
        f"Interval: every {interval} ms ({int(interval)//1000} sec)\n"
        f"Auto-post: {'on ✅' if autopost else 'off ⛔'}\n"
        f"Next run: {next_run}\n"
        f"Messages: {enabled} enabled / {total} total\n"
        f"Scheduled: {pending} pending",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with _ctx():
        msgs = Message.query.order_by(Message.id.asc()).all()
    if not msgs:
        await update.message.reply_text("No messages yet. Use /add <text> to add one.")
        return
    lines = ["📝 *Rotation:*"]
    for m in msgs:
        flag = "✅" if m.enabled else "⛔"
        preview = (m.text[:80] + "…") if len(m.text) > 80 else m.text
        lines.append(f"{flag} `#{m.id}` (sent {m.times_posted}×) — {preview}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@admin_only
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip() if context.args else ""
    if not text and update.message and update.message.text:
        text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Usage: /add <message text>")
        return
    with _ctx():
        m = Message(text=text)
        db.session.add(m)
        db.session.commit()
        new_id = m.id
    await update.message.reply_text(f"✅ Added message `#{new_id}`.", parse_mode="Markdown")


@admin_only
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /delete <id>")
        return
    try:
        mid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Id must be a number.")
        return
    with _ctx():
        m = Message.query.get(mid)
        if not m:
            await update.message.reply_text(f"No message with id {mid}.")
            return
        db.session.delete(m)
        db.session.commit()
    await update.message.reply_text(f"🗑️ Deleted message `#{mid}`.", parse_mode="Markdown")


async def _toggle(update, context, enabled: bool):
    if not context.args:
        await update.message.reply_text(f"Usage: /{'enable' if enabled else 'disable'} <id>")
        return
    try:
        mid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Id must be a number.")
        return
    with _ctx():
        m = Message.query.get(mid)
        if not m:
            await update.message.reply_text(f"No message with id {mid}.")
            return
        m.enabled = enabled
        db.session.commit()
    await update.message.reply_text(
        f"{'✅ Enabled' if enabled else '⛔ Disabled'} message `#{mid}`.", parse_mode="Markdown"
    )


@admin_only
async def cmd_enable(update, context):
    await _toggle(update, context, True)


@admin_only
async def cmd_disable(update, context):
    await _toggle(update, context, False)


@admin_only
async def cmd_postnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, info = sched.post_now()
    if ok:
        await update.message.reply_text(f"📤 Posted ({info}).")
    else:
        await update.message.reply_text(f"⚠️ Could not post: {info}")


@admin_only
async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.partition(" ")[2].strip()
    if "|" not in raw:
        await update.message.reply_text(
            "Usage: /schedule YYYY-MM-DD HH:MM | message text\n"
            "Time is UTC. Example: /schedule 2026-05-02 14:30 | Hello world"
        )
        return
    when_str, _, text = raw.partition("|")
    text = text.strip()
    when_str = when_str.strip()
    if not text:
        await update.message.reply_text("Message text is required after the `|`.")
        return
    try:
        when = datetime.strptime(when_str, "%Y-%m-%d %H:%M")
        when = pytz.UTC.localize(when)
    except ValueError:
        await update.message.reply_text("Could not parse the date. Use YYYY-MM-DD HH:MM (UTC).")
        return
    with _ctx():
        post = ScheduledPost(text=text, run_at=when, status="pending")
        db.session.add(post)
        db.session.commit()
        pid = post.id
        sched.schedule_one_off(pid, when)
    await update.message.reply_text(
        f"⏰ Scheduled post `#{pid}` for {when.strftime('%Y-%m-%d %H:%M UTC')}.",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_scheduled(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with _ctx():
        posts = ScheduledPost.query.filter_by(status="pending").order_by(ScheduledPost.run_at.asc()).all()
    if not posts:
        await update.message.reply_text("No pending scheduled posts.")
        return
    lines = ["⏰ *Pending scheduled posts:*"]
    for p in posts:
        preview = (p.text[:60] + "…") if len(p.text) > 60 else p.text
        lines.append(f"`#{p.id}` {p.run_at.strftime('%Y-%m-%d %H:%M UTC')} — {preview}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@admin_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /cancel <id>")
        return
    try:
        pid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Id must be a number.")
        return
    with _ctx():
        post = ScheduledPost.query.get(pid)
        if not post:
            await update.message.reply_text(f"No scheduled post with id {pid}.")
            return
        try:
            sched.scheduler.remove_job(f"scheduled_{pid}")
        except Exception:
            pass
        db.session.delete(post)
        db.session.commit()
    await update.message.reply_text(f"🗑️ Cancelled scheduled post `#{pid}`.", parse_mode="Markdown")


def _current_channels() -> list[str]:
    raw = (Setting.get("channel_id", "") or "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]


@admin_only
async def cmd_setchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /setchannel <@a,@b,-100…|me>\n"
            "Comma-separated for multiple channels. `me` = this chat.",
            parse_mode="Markdown",
        )
        return
    raw = " ".join(context.args).strip()
    targets = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
    targets = [str(update.effective_chat.id) if t.lower() == "me" else t for t in targets]
    if not targets:
        await update.message.reply_text("No valid channels provided.")
        return
    with _ctx():
        Setting.set("channel_id", ", ".join(targets))
        db.session.commit()
    await update.message.reply_text(
        f"✅ Channels set ({len(targets)}):\n" + "\n".join(f"• `{t}`" for t in targets),
        parse_mode="Markdown",
    )


@admin_only
async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /addchannel <@name|-100…|me>")
        return
    target = " ".join(context.args).strip()
    if target.lower() == "me":
        target = str(update.effective_chat.id)
    with _ctx():
        chans = _current_channels()
        if target in chans:
            await update.message.reply_text(f"ℹ️ `{target}` already in list.", parse_mode="Markdown")
            return
        chans.append(target)
        Setting.set("channel_id", ", ".join(chans))
        db.session.commit()
    await update.message.reply_text(
        f"➕ Added `{target}`. Total channels: {len(chans)}.", parse_mode="Markdown"
    )


@admin_only
async def cmd_removechannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removechannel <@name|-100…>")
        return
    target = " ".join(context.args).strip()
    with _ctx():
        chans = _current_channels()
        if target not in chans:
            await update.message.reply_text(f"`{target}` is not in the list.", parse_mode="Markdown")
            return
        chans = [c for c in chans if c != target]
        Setting.set("channel_id", ", ".join(chans))
        db.session.commit()
    await update.message.reply_text(
        f"➖ Removed `{target}`. Remaining: {len(chans)}.", parse_mode="Markdown"
    )


@admin_only
async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chans = sched.get_active_channels()
    if not chans:
        await update.message.reply_text("No channels configured. Use /addchannel to add one.")
        return
    lines = [f"📡 *Configured channels ({len(chans)}):*"]
    for c in chans:
        lines.append(f"• `{c}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@admin_only
async def cmd_setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setinterval <milliseconds>\nExample: /setinterval 5000 (= 5 seconds)")
        return
    try:
        ms = max(1, int(context.args[0]))
    except ValueError:
        await update.message.reply_text("Milliseconds must be a number.")
        return
    with _ctx():
        Setting.set("interval_seconds", str(ms))
        db.session.commit()
    sched.refresh_recurring_job()
    await update.message.reply_text(f"⏱️ Interval set to every {ms} ms ({ms/1000:.1f} sec).")


@admin_only
async def cmd_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Usage: /autopost on|off")
        return
    enabled = context.args[0].lower() == "on"
    with _ctx():
        Setting.set("autopost_enabled", "1" if enabled else "0")
        db.session.commit()
    sched.refresh_recurring_job()
    await update.message.reply_text(f"Auto-post {'enabled ✅' if enabled else 'disabled ⛔'}.")


async def _on_error(update, context):
    log.exception("Handler error", exc_info=context.error)
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(f"⚠️ Error: {context.error}")
    except Exception:
        pass


def _build_application(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("enable", cmd_enable))
    app.add_handler(CommandHandler("disable", cmd_disable))
    app.add_handler(CommandHandler("postnow", cmd_postnow))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("scheduled", cmd_scheduled))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("setchannel", cmd_setchannel))
    app.add_handler(CommandHandler("addchannel", cmd_addchannel))
    app.add_handler(CommandHandler("removechannel", cmd_removechannel))
    app.add_handler(CommandHandler("channels", cmd_channels))
    app.add_handler(CommandHandler("setinterval", cmd_setinterval))
    app.add_handler(CommandHandler("autopost", cmd_autopost))
    app.add_error_handler(_on_error)
    return app


def _polling_loop(token: str):
    global _application
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        _application = _build_application(token)
        log.info("Starting Telegram bot polling…")
        _application.run_polling(stop_signals=None, close_loop=False, drop_pending_updates=True)
    except Exception:
        log.exception("Polling loop crashed")


def start(flask_app):
    """Start the Telegram polling thread if a bot token is configured."""
    global _flask_app, _thread
    _flask_app = flask_app
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.info("TELEGRAM_BOT_TOKEN not set; bot commands disabled")
        return
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_polling_loop, args=(token,), daemon=True, name="tg-polling")
    _thread.start()
