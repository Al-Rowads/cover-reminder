import logging
import time
from threading import Event

from .api import Http, Instagram, ServiceError, Telegram
from .config import POLL_SECONDS, Config
from .images import normalize_image
from .storage import Store

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, config: Config, store: Store, stop: Event):
        self.config, self.store, self.stop = config, store, stop
        self.http = Http(stop)
        self.http.cooldowns = {
            service: store.get("cooldown_" + service, 0)
            for service in ("composio", "telegram", "thumbnail")
        }
        self.http.on_cooldown = lambda service, until: store.set("cooldown_" + service, until)
        self.instagram = Instagram(config, self.http)
        self.telegram = Telegram(config, self.http)

    def cycle(self, now: float) -> bool:
        self.store.set("next_poll_at", now + POLL_SECONDS)
        errors, discovered, checked, sent = 0, 0, 0, 0
        activation = self.store.get("activated_at")
        checkpoint = self.store.get("discovery_checkpoint", activation)
        # Overlap covers timestamp boundaries and delayed indexing; only advance after all pages succeed.
        since = max(activation - 1, checkpoint - POLL_SECONDS)
        try:
            for reel in self.instagram.discover(since, now):
                discovered += self.store.add(reel, now)
            self.store.set("discovery_checkpoint", now)
        except (ServiceError, ValueError) as error:
            errors += 1
            logger.error("discovery_failed code=%s", safe_code(error))

        for row in self.store.pending():
            if self.stop.is_set():
                return False
            media_id = row["media_id"]
            try:
                reel = self.instagram.get(media_id)
                if reel.published_at != row["published_at"]:
                    raise ServiceError("composio", "publication_time_changed")
                sample = normalize_image(self.instagram.thumbnail(reel))
                observation = self.store.observe(media_id, sample, now, self.config.difference_threshold)
                checked += 1
                if observation["status"] == "changed":
                    logger.info("cover_change_confirmed media_id=%s", media_id)
                    continue
            except (ServiceError, ValueError) as error:
                errors += 1
                self.store.observation_failed(media_id, safe_code(error))
                logger.error("cover_check_failed media_id=%s code=%s", media_id, safe_code(error))
                continue
            hours = self.store.queue_due(media_id, now)
            if hours is None:
                continue
            try:
                final = " This is the final reminder." if hours == 48 else ""
                message_id = self.telegram.send(
                    f"Cover check: this Reel is at least {hours} hours old, and I haven't "
                    f"detected a cover change. Please check its cover.{final}\n\n{reel.permalink}"
                )
                self.store.sent(media_id, hours, message_id, time.time())
                sent += 1
                logger.info("reminder_sent media_id=%s hours=%s message_id=%s", media_id, hours, message_id)
            except ServiceError as error:
                errors += 1
                logger.error("delivery_failed media_id=%s code=%s", media_id, safe_code(error))
        self.store.set("last_cycle", {
            "completed_at": time.time(), "ok": errors == 0,
            "discovered": discovered, "checked": checked, "sent": sent, "errors": errors,
        })
        logger.info("poll_complete discovered=%s checked=%s sent=%s errors=%s", discovered, checked, sent, errors)
        return errors == 0

    def run(self, once: bool = False) -> bool:
        self.store.activate(self.config, time.time())
        while not self.stop.is_set():
            now = time.time()
            delay = self.store.get("next_poll_at", 0) - now
            if delay > 0:
                if once:
                    logger.info("poll_not_due remaining_seconds=%s", round(delay))
                    return True
                self.stop.wait(min(delay, 60))
                continue
            success = self.cycle(now)
            if once:
                return success
        return True


def safe_code(error: Exception) -> str:
    if isinstance(error, ServiceError):
        return f"{error.service}:{error.code}"
    # Unexpected exception text can include upstream URLs and credentials.
    return type(error).__name__
