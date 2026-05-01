import os
import logging
import random
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger
import pytz

from models import db, Message, ScheduledPost, Setting, PostLog
from bot import send_message

log = logging.getLogger("scheduler")

scheduler = BackgroundScheduler(timezone=pytz.UTC)
_app = None


def init_scheduler(app):
    global _app
    _app = app
    if not scheduler.running:
        scheduler.start()
    refresh_recurring_job()
    # Re-register pending one-off jobs on boot
    with app.app_context():
        for post in ScheduledPost.query.filter_by(status="pending").all():
            schedule_one_off(post.id, post.run_at)


def _parse_channels(raw: str) -> list[str]:
    if not raw:
        return []
    parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
    return [p for p in parts if p]


def get_active_channels() -> list[str]:
    """Return all configured channels (env override → DB setting → admin fallback)."""
    env_chat = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
    if env_chat:
        return _parse_channels(env_chat)
    if _app is not None:
        with _app.app_context():
            from_db = (Setting.get("channel_id", "") or "").strip()
            if from_db:
                return _parse_channels(from_db)
    admin = os.environ.get("TELEGRAM_ADMIN_ID", "").strip()
    return [admin] if admin else []


def get_active_channel() -> str:
    """Backwards-compat: first configured channel as a single string."""
    chans = get_active_channels()
    return ",".join(chans) if chans else ""


def _autopost_enabled() -> bool:
    if _app is None:
        return False
    with _app.app_context():
        return Setting.get("autopost_enabled", "1") == "1"


def _interval_seconds() -> int:  # reads ms from DB, converts to seconds for APScheduler
    if _app is None:
        return 3600
    with _app.app_context():
        # Prefer new key; migrate from old interval_minutes if present
        raw = Setting.get("interval_seconds", None)
        if raw is None or raw == "":
            old = Setting.get("interval_minutes", None)
            if old:
                try:
                    return max(1, int(old) * 60)
                except (ValueError, TypeError):
                    pass
            return 3600
        try:
            ms = max(1, int(raw))
            return max(1, ms // 1000)  # stored as ms, APScheduler needs seconds
        except (ValueError, TypeError):
            return 3600


def refresh_recurring_job():
    """(Re)create the recurring auto-post job from current settings."""
    try:
        scheduler.remove_job("hourly_post")
    except Exception:
        pass

    if not _autopost_enabled():
        log.info("Auto-post is disabled; not scheduling recurring job")
        return

    seconds = _interval_seconds()
    scheduler.add_job(
        _recurring_post_job,
        trigger=IntervalTrigger(seconds=seconds),
        id="hourly_post",
        replace_existing=True,
        next_run_time=datetime.now(pytz.UTC),
    )
    log.info(f"Recurring auto-post scheduled every {seconds} second(s)")


def schedule_one_off(post_id: int, run_at: datetime):
    if run_at.tzinfo is None:
        run_at = pytz.UTC.localize(run_at)
    scheduler.add_job(
        _scheduled_post_job,
        trigger=DateTrigger(run_date=run_at),
        id=f"scheduled_{post_id}",
        replace_existing=True,
        args=[post_id],
    )


def _broadcast(text: str) -> tuple[int, int, list[str]]:
    """Send `text` to every configured channel. Returns (ok_count, fail_count, details)."""
    channels = get_active_channels()
    if not channels:
        _record_log(text, "", False, "No channel configured")
        return 0, 0, ["No channel configured"]
    ok_count = 0
    fail_count = 0
    details = []
    for ch in channels:
        ok, info = send_message(ch, text)
        _record_log(text, ch, ok, info)
        if ok:
            ok_count += 1
            details.append(f"{ch}: ok ({info})")
            log.info(f"Posted to {ch}: {info}")
        else:
            fail_count += 1
            details.append(f"{ch}: FAIL ({info})")
            log.error(f"Post to {ch} failed: {info}")
    return ok_count, fail_count, details


def _recurring_post_job():
    if _app is None:
        return
    with _app.app_context():
        msg = _pick_next_message()
        if msg is None:
            log.info("No enabled messages available for auto-post")
            return
        ok_count, fail_count, _ = _broadcast(msg.text)
        if ok_count > 0:
            msg.times_posted += 1
            msg.last_posted_at = datetime.utcnow()
            db.session.commit()
            log.info(f"Auto-posted message #{msg.id} to {ok_count}/{ok_count + fail_count} channels")


def _scheduled_post_job(post_id: int):
    if _app is None:
        return
    with _app.app_context():
        post = ScheduledPost.query.get(post_id)
        if post is None or post.status != "pending":
            return
        ok_count, fail_count, details = _broadcast(post.text)
        if ok_count > 0 and fail_count == 0:
            post.status = "posted"
            post.posted_at = datetime.utcnow()
        elif ok_count > 0:
            post.status = "posted"
            post.posted_at = datetime.utcnow()
            post.error = f"Partial: {fail_count} failed — " + "; ".join(details)
        else:
            post.status = "failed"
            post.error = "; ".join(details)
        db.session.commit()


def _pick_next_message():
    """Pick the message that has been posted the fewest times (with random tie-break)."""
    enabled = Message.query.filter_by(enabled=True).all()
    if not enabled:
        return None
    min_posts = min(m.times_posted for m in enabled)
    candidates = [m for m in enabled if m.times_posted == min_posts]
    return random.choice(candidates)


def _record_log(text: str, channel: str, success: bool, detail: str):
    try:
        entry = PostLog(text=text[:1000], channel=channel, success=success, detail=detail)
        db.session.add(entry)
        db.session.commit()
    except Exception:
        log.exception("Failed to write post log")
        db.session.rollback()


def post_now() -> tuple[bool, str]:
    """Manually trigger an immediate post of the next message to all channels."""
    if _app is None:
        return False, "App not initialized"
    with _app.app_context():
        msg = _pick_next_message()
        if msg is None:
            return False, "No enabled messages to post"
        ok_count, fail_count, details = _broadcast(msg.text)
        if ok_count > 0:
            msg.times_posted += 1
            msg.last_posted_at = datetime.utcnow()
            db.session.commit()
        summary = f"{ok_count} sent, {fail_count} failed"
        if fail_count > 0:
            summary += " — " + "; ".join(d for d in details if "FAIL" in d)
        return (ok_count > 0), summary
