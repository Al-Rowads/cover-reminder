import argparse
import json
import logging
import os
import signal
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from threading import Event

from .api import Http, Instagram, ServiceError, Telegram
from .config import POLL_SECONDS, Config, ConfigurationError, database_path
from .model import Reel
from .storage import Store, exclusive_worker
from .worker import Worker


def print_json(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def healthcheck(path: Path, now: float) -> bool:
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            row = db.execute("SELECT value FROM settings WHERE key='last_cycle'").fetchone()
            cycle = json.loads(row[0]) if row else None
            row = db.execute("SELECT value FROM settings WHERE key='telegram_updates'").fetchone()
            updates = json.loads(row[0]) if row else None
        return bool(cycle and cycle["ok"] and 0 <= now - cycle["completed_at"] <= 2 * POLL_SECONDS + 300
                    and updates and updates["ok"] and 0 <= now - updates["checked_at"] <= 2 * POLL_SECONDS + 300)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return False


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Hourly Instagram Reel cover alerts and reminders")
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run the cover monitor")
    run.add_argument("--once", action="store_true", help="Run one poll if due; keep the hourly schedule")
    commands.add_parser("check", help="Read Instagram media and validate Telegram bot credentials")
    commands.add_parser("send-test", help="Send a real test notification to all active Telegram subscribers")
    commands.add_parser("status", help="Show stored counts and the last poll result")
    commands.add_parser("healthcheck", help="Exit successfully when the latest poll is healthy and recent")
    return root


def main() -> int:
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parser().parse_args()
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        if args.command == "healthcheck":
            return 0 if healthcheck(database_path(), time.time()) else 1
        if args.command == "status":
            path = database_path()
            if not path.exists():
                raise ConfigurationError("No monitor database exists yet")
            with closing(Store(path)) as store:
                print_json(store.summary())
            return 0
        config = Config.from_environment()
        http = Http(stop)
        instagram, telegram = Instagram(config, http), Telegram(config, http)
        if args.command == "check":
            page = instagram.page(limit=25)
            items = page.get("data")
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ServiceError("composio", "missing_media_collection")
            reels = [reel for item in items if (reel := Reel.from_media(item))]
            bot = telegram.check()
            telegram.check_polling()
            print_json({
                "instagram": "accessible", "telegram_bot_id": bot.get("id"),
                "toolkit_version": config.toolkit_version,
                "recent_reels": [{"media_id": reel.media_id, "permalink": reel.permalink} for reel in reels[:5]],
                "telegram_delivery": "Send /start in the bot's private chat, then use send-test to verify delivery",
            })
        elif args.command == "send-test":
            with exclusive_worker(config.database_path), closing(Store(config.database_path)) as store:
                result = Worker(config, store, stop).send_test()
                print_json(result)
                return 0 if result["sent"] and not result["failed"] and not result["unattempted"] else 1
        elif args.command == "run":
            with exclusive_worker(config.database_path), closing(Store(config.database_path)) as store:
                return 0 if Worker(config, store, stop).run(args.once) else 1
        return 0
    except ConfigurationError as error:
        logging.error("%s", error)
    except ServiceError as error:
        logging.error("service_failed service=%s code=%s retry_after=%s", error.service, error.code, error.retry_after)
    except (ValueError, OSError, sqlite3.Error, TypeError):
        logging.error("Operation failed: check file permissions, input files, and response schemas")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
