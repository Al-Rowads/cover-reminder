import fcntl
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .config import Config, ConfigurationError
from .images import difference
from .model import Reel


@contextmanager
def exclusive_worker(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigurationError("Another worker is already using this database") from None
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=30)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1, 2, 3}:
            self.db.close()
            raise ConfigurationError("Database schema is newer than this application")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        try:
            self._initialize()
        except Exception:
            self.db.rollback()
            self.db.close()
            raise

    def _initialize(self) -> None:
        self.db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reels (
                media_id TEXT PRIMARY KEY,
                permalink TEXT NOT NULL,
                published_at REAL NOT NULL,
                first_seen_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'changed', 'completed')),
                baseline BLOB,
                previous_sample BLOB,
                change_streak INTEGER NOT NULL DEFAULT 0,
                last_checked_at REAL,
                last_difference REAL,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS reminders (
                media_id TEXT NOT NULL REFERENCES reels(media_id),
                hours INTEGER NOT NULL CHECK(hours IN (24, 48)),
                status TEXT NOT NULL CHECK(status IN ('pending', 'sent', 'superseded')),
                message_id INTEGER,
                sent_at REAL,
                PRIMARY KEY (media_id, hours)
            );
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY CHECK(chat_id > 0),
                active INTEGER NOT NULL CHECK(active IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                media_id TEXT NOT NULL,
                hours INTEGER NOT NULL,
                chat_id INTEGER NOT NULL REFERENCES subscribers(chat_id),
                status TEXT NOT NULL CHECK(status IN ('pending', 'sent', 'cancelled')),
                message_id INTEGER,
                sent_at REAL,
                PRIMARY KEY (media_id, hours, chat_id),
                FOREIGN KEY (media_id, hours) REFERENCES reminders(media_id, hours)
            );
            CREATE INDEX IF NOT EXISTS deliveries_by_status ON deliveries(media_id, hours, status);
            CREATE INDEX IF NOT EXISTS deliveries_by_subscriber ON deliveries(chat_id, status);
            CREATE TABLE IF NOT EXISTS cover_change_alerts (
                media_id TEXT PRIMARY KEY REFERENCES reels(media_id),
                status TEXT NOT NULL CHECK(status IN ('pending', 'sent', 'superseded')),
                audience_captured INTEGER NOT NULL DEFAULT 0 CHECK(audience_captured IN (0, 1)),
                detected_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cover_change_deliveries (
                media_id TEXT NOT NULL REFERENCES cover_change_alerts(media_id),
                chat_id INTEGER NOT NULL REFERENCES subscribers(chat_id),
                status TEXT NOT NULL CHECK(status IN ('pending', 'sent', 'cancelled')),
                message_id INTEGER,
                sent_at REAL,
                PRIMARY KEY (media_id, chat_id)
            );
            CREATE INDEX IF NOT EXISTS cover_change_deliveries_by_status
                ON cover_change_deliveries(media_id, status);
            CREATE INDEX IF NOT EXISTS cover_change_deliveries_by_subscriber
                ON cover_change_deliveries(chat_id, status);
        """)
        # Another connection may have migrated while this connection waited for the write lock.
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1, 2, 3}:
            raise ConfigurationError("Database schema is newer than this application")
        if version < 2:
            self.db.execute("ALTER TABLE reminders ADD COLUMN audience_captured INTEGER NOT NULL DEFAULT 0")
            identity = self.get("monitor_identity")
            if identity is not None:
                identity.pop("telegram_chat_id", None)
                self._set("monitor_identity", identity)
        self.db.execute("PRAGMA user_version=3")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value) -> None:
        with self.db:
            self._set(key, value)

    def _set(self, key: str, value) -> None:
        self.db.execute(
            "INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def bind_bot(self, bot_id: int) -> None:
        existing = self.get("telegram_bot_id")
        if existing is not None and existing != bot_id:
            raise ConfigurationError("This database belongs to a different Telegram bot; use a separate database")
        self.set("telegram_bot_id", bot_id)

    def subscribers(self) -> list[int]:
        return [row[0] for row in self.db.execute(
            "SELECT chat_id FROM subscribers WHERE active=1 ORDER BY chat_id"
        )]

    def is_subscribed(self, chat_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM subscribers WHERE chat_id=? AND active=1", (chat_id,)
        ).fetchone() is not None

    def apply_update(self, update_id: int, chat_id: int | None = None, active: bool | None = None,
                     *, received_at: float | None = None) -> bool:
        now = time.time() if received_at is None else received_at
        # IDs can reset after a quiet week. Deduplicate the last batch by equality,
        # expiring receipts with Telegram's 24-hour update retention window.
        recent = [item for item in self.get("telegram_recent_updates", []) if now - item[1] < 86400]
        if any(item[0] == update_id for item in recent):
            return False
        # Commit the subscription and offset together before Telegram is asked to acknowledge it.
        with self.db:
            if chat_id is not None and active is not None:
                self.db.execute("""
                    INSERT INTO subscribers VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET active=excluded.active
                """, (chat_id, int(active)))
                if not active:
                    self._cancel_subscriber(chat_id)
            self._set("telegram_update_offset", update_id + 1)
            self._set("telegram_recent_updates", (recent + [[update_id, now]])[-100:])
        return True

    def deactivate(self, chat_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE subscribers SET active=0 WHERE chat_id=?", (chat_id,))
            self._cancel_subscriber(chat_id)

    def _cancel_subscriber(self, chat_id: int) -> None:
        affected = self.db.execute(
            "SELECT media_id, hours FROM deliveries WHERE chat_id=? AND status='pending'", (chat_id,)
        ).fetchall()
        self.db.execute("UPDATE deliveries SET status='cancelled' WHERE chat_id=? AND status='pending'", (chat_id,))
        for row in affected:
            self._finish_reminder(row["media_id"], row["hours"])
        changed = self.db.execute(
            "SELECT media_id FROM cover_change_deliveries WHERE chat_id=? AND status='pending'", (chat_id,)
        ).fetchall()
        self.db.execute(
            "UPDATE cover_change_deliveries SET status='cancelled' WHERE chat_id=? AND status='pending'",
            (chat_id,),
        )
        for row in changed:
            self._finish_change_alert(row["media_id"])

    def activate(self, config: Config, now: float) -> None:
        self.check_account(config)
        self.set("monitor_identity", config.identity())
        if self.get("activated_at") is None:
            self.set("activated_at", now)
            self.set("discovery_checkpoint", now)

    def check_account(self, config: Config) -> None:
        existing = self.get("monitor_identity")
        if existing is not None and existing != config.identity():
            raise ConfigurationError("This database belongs to a different Instagram account; use a separate database")

    def add(self, reel: Reel, now: float) -> bool:
        if reel.published_at < self.get("activated_at"):
            return False
        if reel.published_at > now + 300:
            raise ValueError("future_publication_time")
        with self.db:
            result = self.db.execute(
                "INSERT OR IGNORE INTO reels (media_id, permalink, published_at, first_seen_at) VALUES (?, ?, ?, ?)",
                (reel.media_id, reel.permalink, reel.published_at, now),
            )
        return bool(result.rowcount)

    def reel(self, media_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM reels WHERE media_id=?", (media_id,)).fetchone()
        if row is None:
            raise ValueError("unknown_reel")
        return row

    def pending(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM reels WHERE status='pending' ORDER BY published_at, media_id"
        ).fetchall()

    def observe(self, media_id: str, sample: bytes, now: float, threshold: float) -> sqlite3.Row:
        row = self.reel(media_id)
        if row["status"] != "pending" or (row["last_checked_at"] is not None and now <= row["last_checked_at"]):
            return row
        baseline = row["baseline"] if row["baseline"] is not None else sample
        score = difference(baseline, sample)
        changed = score > threshold
        status = "changed" if changed else "pending"
        with self.db:
            self.db.execute("""
                UPDATE reels SET baseline=?, previous_sample=?, change_streak=?, status=?,
                    last_checked_at=?, last_difference=?, last_error=NULL WHERE media_id=?
            """, (baseline, sample if changed else None, int(changed), status, now, score, media_id))
            if changed:
                self.db.execute(
                    "UPDATE deliveries SET status='cancelled' WHERE media_id=? AND status='pending'", (media_id,)
                )
                self.db.execute(
                    "UPDATE reminders SET status='superseded' WHERE media_id=? AND status='pending'",
                    (media_id,),
                )
                self.db.execute(
                    """INSERT OR IGNORE INTO cover_change_alerts
                       (media_id, status, audience_captured, detected_at) VALUES (?, 'pending', 0, ?)""",
                    (media_id, now),
                )
                captured = self.db.execute("""
                    UPDATE cover_change_alerts SET audience_captured=1
                    WHERE media_id=? AND status='pending' AND audience_captured=0
                """, (media_id,))
                if captured.rowcount:
                    self.db.execute("""
                        INSERT INTO cover_change_deliveries (media_id, chat_id, status)
                        SELECT ?, chat_id, 'pending' FROM subscribers WHERE active=1
                    """, (media_id,))
                    self._finish_change_alert(media_id)
        return self.reel(media_id)

    def observation_failed(self, media_id: str, code: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE reels SET change_streak=0, previous_sample=NULL, last_error=? WHERE media_id=?",
                (code, media_id),
            )

    def queue_due(self, media_id: str, now: float) -> int | None:
        row = self.reel(media_id)
        # A 24-hour delivery must be revalidated against a fresh cover on every retry.
        if (row["status"] != "pending" or row["last_checked_at"] != now
                or row["last_error"] or row["baseline"] is None or row["change_streak"]):
            return None
        age = now - row["published_at"]
        hours = 48 if age >= 48 * 3600 else 24 if age >= 24 * 3600 else None
        if hours is None:
            return None
        with self.db:
            if hours == 48:
                self.db.execute("""
                    UPDATE deliveries SET status='cancelled' WHERE media_id=? AND hours=24 AND status='pending'
                """, (media_id,))
                self.db.execute("""
                    INSERT INTO reminders (media_id, hours, status) VALUES (?, 24, 'superseded')
                    ON CONFLICT(media_id, hours) DO UPDATE SET status='superseded'
                    WHERE reminders.status='pending'
                """, (media_id,))
            self.db.execute(
                "INSERT OR IGNORE INTO reminders (media_id, hours, status) VALUES (?, ?, 'pending')",
                (media_id, hours),
            )
            captured = self.db.execute("""
                UPDATE reminders SET audience_captured=1
                WHERE media_id=? AND hours=? AND status='pending' AND audience_captured=0
            """, (media_id, hours))
            if captured.rowcount:
                self.db.execute("""
                    INSERT INTO deliveries (media_id, hours, chat_id, status)
                    SELECT ?, ?, chat_id, 'pending' FROM subscribers WHERE active=1
                """, (media_id, hours))
                self._finish_reminder(media_id, hours)
            if hours == 48:
                self.db.execute(
                    "UPDATE reels SET status='completed' WHERE media_id=? AND status='pending'", (media_id,)
                )
        reminder = self.db.execute(
            "SELECT status FROM reminders WHERE media_id=? AND hours=?", (media_id, hours),
        ).fetchone()
        return hours if reminder[0] == "pending" else None

    def pending_deliveries(self, media_id: str, hours: int) -> list[int]:
        return [row[0] for row in self.db.execute("""
            SELECT d.chat_id FROM deliveries d JOIN subscribers s USING(chat_id)
            JOIN reminders r USING(media_id, hours)
            WHERE d.media_id=? AND d.hours=? AND d.status='pending' AND s.active=1 AND r.status='pending'
            ORDER BY d.chat_id
        """, (media_id, hours))]

    def pending_reminders_for_delivery(self, now: float) -> list[sqlite3.Row]:
        return self.db.execute("""
            SELECT reminders.media_id, reminders.hours, reels.permalink
            FROM reminders JOIN reels USING(media_id)
            WHERE reminders.status='pending' AND (
                reminders.hours=48 OR (
                    reminders.hours=24 AND reels.status='pending'
                    AND reels.last_checked_at=? AND reels.last_error IS NULL
                    AND reels.baseline IS NOT NULL AND reels.change_streak=0
                )
            )
            ORDER BY reels.published_at, reminders.media_id
        """, (now,)).fetchall()

    def delivery_pending(self, media_id: str, hours: int, chat_id: int) -> bool:
        return self.db.execute("""
            SELECT 1 FROM deliveries WHERE media_id=? AND hours=? AND chat_id=? AND status='pending'
        """, (media_id, hours, chat_id)).fetchone() is not None

    def sent(self, media_id: str, hours: int, chat_id: int, message_id: int, now: float) -> None:
        with self.db:
            updated = self.db.execute("""
                UPDATE deliveries SET status='sent', message_id=?, sent_at=?
                WHERE media_id=? AND hours=? AND chat_id=? AND status='pending'
            """, (message_id, now, media_id, hours, chat_id))
            if updated.rowcount != 1:
                raise ValueError("delivery_not_pending")
            self._finish_reminder(media_id, hours)

    def _finish_reminder(self, media_id: str, hours: int) -> None:
        if self.db.execute("""
            SELECT 1 FROM deliveries WHERE media_id=? AND hours=? AND status='pending' LIMIT 1
        """, (media_id, hours)).fetchone():
            return
        sent = self.db.execute("""
            SELECT 1 FROM deliveries WHERE media_id=? AND hours=? AND status='sent' LIMIT 1
        """, (media_id, hours)).fetchone()
        self.db.execute("""
            UPDATE reminders SET status=? WHERE media_id=? AND hours=? AND status='pending'
        """, ("sent" if sent else "superseded", media_id, hours))
        if hours == 48:
            self.db.execute("UPDATE reels SET status='completed' WHERE media_id=? AND status='pending'", (media_id,))

    def pending_change_alerts(self) -> list[sqlite3.Row]:
        return self.db.execute("""
            SELECT cover_change_alerts.media_id, reels.permalink
            FROM cover_change_alerts JOIN reels USING(media_id)
            WHERE cover_change_alerts.status='pending'
            ORDER BY cover_change_alerts.detected_at, cover_change_alerts.media_id
        """).fetchall()

    def pending_change_deliveries(self, media_id: str) -> list[int]:
        return [row[0] for row in self.db.execute("""
            SELECT delivery.chat_id
            FROM cover_change_deliveries AS delivery
            JOIN subscribers USING(chat_id)
            JOIN cover_change_alerts USING(media_id)
            WHERE delivery.media_id=? AND delivery.status='pending'
                AND subscribers.active=1 AND cover_change_alerts.status='pending'
            ORDER BY delivery.chat_id
        """, (media_id,))]

    def change_delivery_pending(self, media_id: str, chat_id: int) -> bool:
        return self.db.execute("""
            SELECT 1 FROM cover_change_deliveries
            WHERE media_id=? AND chat_id=? AND status='pending'
        """, (media_id, chat_id)).fetchone() is not None

    def change_sent(self, media_id: str, chat_id: int, message_id: int, now: float) -> None:
        with self.db:
            updated = self.db.execute("""
                UPDATE cover_change_deliveries SET status='sent', message_id=?, sent_at=?
                WHERE media_id=? AND chat_id=? AND status='pending'
            """, (message_id, now, media_id, chat_id))
            if updated.rowcount != 1:
                raise ValueError("change_delivery_not_pending")
            self._finish_change_alert(media_id)

    def _finish_change_alert(self, media_id: str) -> None:
        if self.db.execute("""
            SELECT 1 FROM cover_change_deliveries
            WHERE media_id=? AND status='pending' LIMIT 1
        """, (media_id,)).fetchone():
            return
        sent = self.db.execute("""
            SELECT 1 FROM cover_change_deliveries
            WHERE media_id=? AND status='sent' LIMIT 1
        """, (media_id,)).fetchone()
        self.db.execute("""
            UPDATE cover_change_alerts SET status=? WHERE media_id=? AND status='pending'
        """, ("sent" if sent else "superseded", media_id))

    def summary(self) -> dict:
        return {
            "activated_at": self.get("activated_at"),
            "last_cycle": self.get("last_cycle"),
            "next_poll_at": self.get("next_poll_at"),
            "telegram_updates": self.get("telegram_updates"),
            "subscribers": {"active": len(self.subscribers()), "inactive": self.db.execute(
                "SELECT count(*) FROM subscribers WHERE active=0"
            ).fetchone()[0]},
            "deliveries": dict(self.db.execute("SELECT status, count(*) FROM deliveries GROUP BY status").fetchall()),
            "cover_change_alerts": dict(self.db.execute(
                "SELECT status, count(*) FROM cover_change_alerts GROUP BY status"
            ).fetchall()),
            "cover_change_deliveries": dict(self.db.execute(
                "SELECT status, count(*) FROM cover_change_deliveries GROUP BY status"
            ).fetchall()),
            "reels": dict(self.db.execute("SELECT status, count(*) FROM reels GROUP BY status").fetchall()),
            "reminders": dict(self.db.execute("SELECT status, count(*) FROM reminders GROUP BY status").fetchall()),
        }
