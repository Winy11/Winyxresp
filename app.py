import os
import logging
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from models import db, Message, ScheduledPost, Setting
from scheduler import init_scheduler, scheduler, post_now, get_active_channel
import bot_commands
import pytz

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("app")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SESSION_SECRET", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///autoposter.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

with app.app_context():
    db.create_all()
    Setting.ensure_defaults()

init_scheduler(app)
bot_commands.start(app)


@app.route("/")
def index():
    messages = Message.query.order_by(Message.created_at.desc()).all()
    posts = ScheduledPost.query.order_by(ScheduledPost.run_at.asc()).all()
    settings = Setting.as_dict()
    next_run = None
    job = scheduler.get_job("hourly_post")
    if job and job.next_run_time:
        next_run = job.next_run_time.astimezone(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    from scheduler import get_active_channels
    bot_configured = bool(os.environ.get("TELEGRAM_BOT_TOKEN"))
    channels = get_active_channels()
    return render_template(
        "index.html",
        messages=messages,
        posts=posts,
        settings=settings,
        next_run=next_run,
        bot_configured=bot_configured,
        active_channel=", ".join(channels) if channels else "",
        channel_count=len(channels),
    )


@app.route("/messages/add", methods=["POST"])
def add_message():
    text = request.form.get("text", "").strip()
    if not text:
        flash("Message text cannot be empty", "error")
        return redirect(url_for("index"))
    msg = Message(text=text)
    db.session.add(msg)
    db.session.commit()
    flash("Message added to the rotation", "success")
    return redirect(url_for("index"))


@app.route("/messages/<int:msg_id>/delete", methods=["POST"])
def delete_message(msg_id):
    msg = Message.query.get_or_404(msg_id)
    db.session.delete(msg)
    db.session.commit()
    flash("Message deleted", "success")
    return redirect(url_for("index"))


@app.route("/messages/<int:msg_id>/toggle", methods=["POST"])
def toggle_message(msg_id):
    msg = Message.query.get_or_404(msg_id)
    msg.enabled = not msg.enabled
    db.session.commit()
    flash(f"Message {'enabled' if msg.enabled else 'disabled'}", "success")
    return redirect(url_for("index"))


@app.route("/messages/<int:msg_id>/edit", methods=["POST"])
def edit_message(msg_id):
    msg = Message.query.get_or_404(msg_id)
    text = request.form.get("text", "").strip()
    if not text:
        flash("Message text cannot be empty", "error")
        return redirect(url_for("index"))
    msg.text = text
    db.session.commit()
    flash("Message updated", "success")
    return redirect(url_for("index"))


@app.route("/scheduled/add", methods=["POST"])
def add_scheduled():
    text = request.form.get("text", "").strip()
    run_at_str = request.form.get("run_at", "").strip()
    if not text or not run_at_str:
        flash("Text and time required", "error")
        return redirect(url_for("index"))
    try:
        run_at = datetime.fromisoformat(run_at_str)
        if run_at.tzinfo is None:
            run_at = pytz.UTC.localize(run_at)
    except ValueError:
        flash("Invalid date/time format", "error")
        return redirect(url_for("index"))
    post = ScheduledPost(text=text, run_at=run_at, status="pending")
    db.session.add(post)
    db.session.commit()
    from scheduler import schedule_one_off
    schedule_one_off(post.id, run_at)
    flash("Scheduled post added", "success")
    return redirect(url_for("index"))


@app.route("/scheduled/<int:post_id>/delete", methods=["POST"])
def delete_scheduled(post_id):
    post = ScheduledPost.query.get_or_404(post_id)
    try:
        scheduler.remove_job(f"scheduled_{post.id}")
    except Exception:
        pass
    db.session.delete(post)
    db.session.commit()
    flash("Scheduled post removed", "success")
    return redirect(url_for("index"))


@app.route("/settings", methods=["POST"])
def update_settings():
    channel = request.form.get("channel_id", "").strip()
    interval_seconds = request.form.get("interval_seconds", "3600000").strip()
    enabled = request.form.get("autopost_enabled") == "on"

    Setting.set("channel_id", channel)
    try:
        interval = max(1, int(interval_seconds))  # stored as milliseconds
    except ValueError:
        interval = 3600000
    Setting.set("interval_seconds", str(interval))
    Setting.set("autopost_enabled", "1" if enabled else "0")
    db.session.commit()

    from scheduler import refresh_recurring_job
    refresh_recurring_job()
    flash("Settings saved", "success")
    return redirect(url_for("index"))


@app.route("/post-now", methods=["POST"])
def trigger_post_now():
    ok, info = post_now()
    if ok:
        flash(f"Posted: {info}", "success")
    else:
        flash(f"Could not post: {info}", "error")
    return redirect(url_for("index"))


@app.route("/api/status")
def api_status():
    job = scheduler.get_job("hourly_post")
    next_run = None
    if job and job.next_run_time:
        next_run = job.next_run_time.isoformat()
    return jsonify({
        "bot_configured": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
        "active_channel": get_active_channel(),
        "next_run": next_run,
        "messages": Message.query.count(),
        "scheduled": ScheduledPost.query.filter_by(status="pending").count(),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
