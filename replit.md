# Telegram Auto Poster Bot

A Flask-based web dashboard for managing a Telegram bot that auto-posts messages to a channel on a recurring schedule, with one-off scheduled posts and a rotating message library.

## Architecture

- **Web dashboard (Flask)** — `app.py` serves a single-page management UI on port 5000.
- **Database (PostgreSQL via SQLAlchemy)** — `models.py` defines `Message`, `ScheduledPost`, `Setting`, and `PostLog`. Uses `DATABASE_URL` from the environment, falling back to SQLite (`autoposter.db`) if unset.
- **Scheduler (APScheduler)** — `scheduler.py` runs a `BackgroundScheduler` inside the Flask process. One recurring `IntervalTrigger` job auto-posts the next-due rotation message; one-off `DateTrigger` jobs handle scheduled posts.
- **Telegram client (python-telegram-bot v22)** — `bot.py` wraps `Bot.send_message` in a synchronous helper for use from the scheduler.

## Configuration

Environment variables:
- `TELEGRAM_BOT_TOKEN` (required for posting) — bot token from [@BotFather](https://t.me/BotFather).
- `TELEGRAM_CHANNEL_ID` (optional) — overrides the channel set in the dashboard.
- `DATABASE_URL` (optional) — Postgres connection string. Defaults to SQLite if unset.
- `SESSION_SECRET` (optional) — Flask session secret.

In-app settings (stored in DB): channel ID, interval (minutes), auto-post on/off.

## Message rotation logic

The recurring job picks the enabled message with the lowest `times_posted` count (random tie-break), increments its counter, and updates `last_posted_at`. Disabled messages are skipped.

## Project layout

```
app.py            # Flask routes and bootstrapping
models.py         # SQLAlchemy models
scheduler.py      # APScheduler setup, recurring + one-off jobs
bot.py            # python-telegram-bot wrapper
templates/
  index.html      # Dashboard UI
static/
  style.css       # Dark Telegram-themed styling
```

## Workflow

- `Start application` — `python app.py`, port 5000, webview output.
