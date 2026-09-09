import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import POLL_SECONDS, Config, ConfigurationError
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
        if version not in {0, 1}:
            self.db.close()
            raise ConfigurationError("Database schema is newer than this application")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
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
            PRAGMA user_version=1;
        """)

    def close(self) -> None:
        self.db.close()

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def activate(self, config: Config, now: float) -> None:
        verification = self.get("cover_verification", {})
        if verification.get("detector") != config.detector_identity():
            raise ConfigurationError(
                "Live cover verification is required; follow the capture-cover / verify-cover steps in README.md"
            )
        identity = {**config.identity(), "telegram_chat_id": config.telegram_chat_id}
        existing = self.get("monitor_identity")
        if existing is not None and existing != identity:
            raise ConfigurationError("This database belongs to a different account or Telegram chat; use a separate database")
        self.set("monitor_identity", identity)
        if self.get("activated_at") is None:
            self.set("activated_at", now)
            self.set("discovery_checkpoint", now)

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
        streak = 0
        if score > threshold:
            previous = row["previous_sample"]
            consecutive = (
                row["last_checked_at"] is not None
                and now - row["last_checked_at"] <= POLL_SECONDS * 1.5
            )
            stable = (
                previous is not None and difference(previous, sample) <= threshold
                and difference(baseline, previous) > threshold
            )
            streak = row["change_streak"] + 1 if consecutive and stable else 1
        status = "changed" if streak >= 2 else "pending"
        with self.db:
            self.db.execute("""
                UPDATE reels SET baseline=?, previous_sample=?, change_streak=?, status=?,
                    last_checked_at=?, last_difference=?, last_error=NULL WHERE media_id=?
            """, (baseline, sample, streak, status, now, score, media_id))
            if status == "changed":
                self.db.execute(
                    "UPDATE reminders SET status='superseded' WHERE media_id=? AND status='pending'",
                    (media_id,),
                )
        return self.reel(media_id)

    def observation_failed(self, media_id: str, code: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE reels SET change_streak=0, previous_sample=NULL, last_error=? WHERE media_id=?",
                (code, media_id),
            )

    def queue_due(self, media_id: str, now: float) -> int | None:
        row = self.reel(media_id)
        # A pending delivery must be revalidated against a fresh cover on every retry.
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
                    INSERT INTO reminders (media_id, hours, status) VALUES (?, 24, 'superseded')
                    ON CONFLICT(media_id, hours) DO UPDATE SET status='superseded'
                    WHERE reminders.status='pending'
                """, (media_id,))
            self.db.execute(
                "INSERT OR IGNORE INTO reminders (media_id, hours, status) VALUES (?, ?, 'pending')",
                (media_id, hours),
            )
        reminder = self.db.execute(
            "SELECT status FROM reminders WHERE media_id=? AND hours=?", (media_id, hours),
        ).fetchone()
        return hours if reminder[0] == "pending" else None

    def sent(self, media_id: str, hours: int, message_id: int, now: float) -> None:
        with self.db:
            updated = self.db.execute("""
                UPDATE reminders SET status='sent', message_id=?, sent_at=?
                WHERE media_id=? AND hours=? AND status='pending'
            """, (message_id, now, media_id, hours))
            if updated.rowcount != 1:
                raise ValueError("reminder_not_pending")
            if hours == 48:
                self.db.execute("UPDATE reels SET status='completed' WHERE media_id=?", (media_id,))

    def summary(self) -> dict:
        return {
            "activated_at": self.get("activated_at"),
            "last_cycle": self.get("last_cycle"),
            "next_poll_at": self.get("next_poll_at"),
            "reels": dict(self.db.execute("SELECT status, count(*) FROM reels GROUP BY status").fetchall()),
            "reminders": dict(self.db.execute("SELECT status, count(*) FROM reminders GROUP BY status").fetchall()),
        }
