import argparse
import hashlib
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
from .images import MAX_IMAGE_BYTES, difference, normalize_image
from .model import Reel
from .storage import Store, exclusive_worker
from .worker import Worker


def print_json(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def read_image(path: Path) -> bytes:
    with path.open("rb") as source:
        content = source.read(MAX_IMAGE_BYTES + 1)
    normalize_image(content)
    return content


def sidecar(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".json")


def capture(instagram: Instagram, media_id: str, output: Path, config: Config) -> None:
    if not media_id.isdecimal():
        raise ConfigurationError("Use a numeric Instagram media ID from the check command")
    reel = instagram.get(media_id)
    image = instagram.thumbnail(reel)
    normalize_image(image)
    if output.exists() or sidecar(output).exists():
        raise ConfigurationError("Capture output already exists; choose a new filename")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = {
        "format": 1, "media_id": media_id, "captured_at": time.time(),
        "detector": config.detector_identity(), "sha256": hashlib.sha256(image).hexdigest(),
    }
    with output.open("xb") as destination:
        destination.write(image)
    with sidecar(output).open("x") as destination:
        json.dump(metadata, destination, indent=2)
    print_json({"saved": str(output), "media_id": media_id})


def verification_evidence(paths: list[Path], config: Config) -> dict:
    captures = []
    samples = []
    for path in paths:
        image = read_image(path)
        metadata = json.loads(sidecar(path).read_text())
        if (not isinstance(metadata, dict) or metadata.get("format") != 1
                or metadata.get("detector") != config.detector_identity()
                or metadata.get("sha256") != hashlib.sha256(image).hexdigest()):
            raise ConfigurationError("Capture metadata does not match this account, detector, or image")
        captures.append(metadata)
        samples.append(normalize_image(image))
    first = captures[0]
    media_id = first.get("media_id")
    if not isinstance(media_id, str) or not media_id.isdecimal() or any(
        item.get("media_id") != media_id for item in captures
    ):
        raise ConfigurationError("All three captures must be from the same Reel")
    captured_times = [item.get("captured_at") for item in captures]
    if not all(isinstance(value, (float, int)) for value in captured_times) or not (
        captured_times[0] < captured_times[1] < captured_times[2] <= time.time()
    ):
        raise ConfigurationError("Provide captures in order: before edit, after edit, confirmation")
    scores = {
        "before_after": difference(samples[0], samples[1]),
        "before_confirmation": difference(samples[0], samples[2]),
        "after_confirmation": difference(samples[1], samples[2]),
    }
    threshold = config.difference_threshold
    if (scores["before_after"] <= threshold or scores["before_confirmation"] <= threshold
            or scores["after_confirmation"] > threshold):
        raise ConfigurationError(
            "Cover change is not stable and distinguishable at the configured threshold; "
            "use compare-covers to inspect the difference. Monitoring remains disabled."
        )
    return {
        "detector": config.detector_identity(), "verified_at": time.time(),
        "media_id": media_id, "scores": scores,
        "capture_hashes": [item["sha256"] for item in captures],
    }


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
    root = argparse.ArgumentParser(description="Hourly Instagram Reel cover reminders")
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run the verified monitor")
    run.add_argument("--once", action="store_true", help="Run one poll if due; keep the hourly schedule")
    commands.add_parser("check", help="Read Instagram media and validate Telegram bot credentials")
    commands.add_parser("send-test", help="Send a real test notification to all active Telegram subscribers")
    commands.add_parser("status", help="Show stored counts and the last poll result")
    commands.add_parser("healthcheck", help="Exit successfully when the latest poll is healthy and recent")
    capture_command = commands.add_parser("capture-cover", help="Save a real thumbnail and verification metadata")
    capture_command.add_argument("media_id")
    capture_command.add_argument("--output", required=True, type=Path)
    compare = commands.add_parser("compare-covers", help="Measure two saved images without modifying monitor state")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    verify = commands.add_parser("verify-cover", help="Record a successful live cover-change verification")
    verify.add_argument("before", type=Path)
    verify.add_argument("after", type=Path)
    verify.add_argument("confirmation", type=Path)
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
        if args.command == "compare-covers":
            score = difference(normalize_image(read_image(args.before)), normalize_image(read_image(args.after)))
            print_json({"difference": score, "difference_percent": score * 100})
            return 0
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
        elif args.command == "capture-cover":
            capture(instagram, args.media_id, args.output, config)
        elif args.command == "verify-cover":
            evidence = verification_evidence([args.before, args.after, args.confirmation], config)
            with exclusive_worker(config.database_path), closing(Store(config.database_path)) as store:
                store.set("cover_verification", evidence)
            print_json({"cover_detection_verified": True, "scores": evidence["scores"]})
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
