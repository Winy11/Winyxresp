from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Message(db.Model):
    __tablename__ = "messages"
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.Text, nullable=False)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    times_posted = db.Column(db.Integer, default=0, nullable=False)
    last_posted_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ScheduledPost(db.Model):
    __tablename__ = "scheduled_posts"
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.Text, nullable=False)
    run_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default="pending", nullable=False)
    posted_at = db.Column(db.DateTime, nullable=True)
    error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Setting(db.Model):
    __tablename__ = "settings"
    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text, nullable=True)

    DEFAULTS = {
        "channel_id": "",
        "interval_seconds": "3600000",
        "autopost_enabled": "1",
    }

    @classmethod
    def ensure_defaults(cls):
        # Migrate legacy interval_minutes -> interval_seconds
        legacy = cls.query.get("interval_minutes")
        if legacy is not None and cls.query.get("interval_seconds") is None:
            try:
                seconds = max(1, int(legacy.value) * 60)
                db.session.add(cls(key="interval_seconds", value=str(seconds)))
            except (ValueError, TypeError):
                pass
            db.session.delete(legacy)
            db.session.commit()
        # Migrate interval_seconds from old seconds unit -> milliseconds
        existing_interval = cls.query.get("interval_seconds")
        if existing_interval is not None:
            try:
                val = int(existing_interval.value)
                if val < 1000:  # was in seconds, convert to ms
                    existing_interval.value = str(val * 1000)
                    db.session.commit()
            except (ValueError, TypeError):
                pass
        for key, val in cls.DEFAULTS.items():
            if cls.query.get(key) is None:
                db.session.add(cls(key=key, value=val))
        db.session.commit()

    @classmethod
    def get(cls, key, default=None):
        row = cls.query.get(key)
        if row is None:
            return default if default is not None else cls.DEFAULTS.get(key, "")
        return row.value

    @classmethod
    def set(cls, key, value):
        row = cls.query.get(key)
        if row is None:
            row = cls(key=key, value=value)
            db.session.add(row)
        else:
            row.value = value

    @classmethod
    def as_dict(cls):
        out = dict(cls.DEFAULTS)
        for row in cls.query.all():
            out[row.key] = row.value
        return out


class PostLog(db.Model):
    __tablename__ = "post_logs"
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.Text, nullable=False)
    channel = db.Column(db.String(255), nullable=True)
    success = db.Column(db.Boolean, default=False, nullable=False)
    detail = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
