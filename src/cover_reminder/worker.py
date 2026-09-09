import logging
import time
from threading import Event

from .api import Http, Instagram, ServiceError, Telegram
from .config import POLL_SECONDS, Config, ConfigurationError
from .images import normalize_image
from .storage import Store
from .subscriptions import subscription_change

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
        self.bot_username = ""
        self.last_update_check = time.monotonic()

    def prepare_telegram(self) -> None:
        self.store.check_account(self.config)
        bot = self.telegram.check()
        self.telegram.check_polling()
        self.store.bind_bot(bot["id"])
        self.bot_username = bot["username"]

    def sync_updates(self, timeout: int = 0) -> bool:
        self.last_update_check = time.monotonic()
        try:
            while not self.stop.is_set():
                updates = self.telegram.updates(self.store.get("telegram_update_offset"), timeout)
                for update in updates:
                    chat_id, active = subscription_change(update, self.bot_username)
                    applied = self.store.apply_update(update["update_id"], chat_id, active)
                    if applied and active is not None and "message" in update:
                        text = ("Subscribed to upcoming cover reminders. Send /stop to unsubscribe."
                                if active else "Unsubscribed from cover reminders. Send /start to subscribe again.")
                        try:
                            self.send_to(chat_id, text)
                        except ServiceError as error:
                            # Registration is durable even if its acknowledgement cannot be delivered.
                            logger.warning("subscription_reply_failed code=%s", safe_code(error))
                if len(updates) < 100:
                    self.store.set("telegram_updates", {"checked_at": time.time(), "ok": True})
                    return True
                timeout = 0
        except ServiceError as error:
            self.store.set("telegram_updates", {
                "checked_at": time.time(), "ok": False, "error": safe_code(error),
            })
            if error.code in {"http_409", "api_409"}:
                raise ConfigurationError(
                    "Telegram update polling conflicts with another consumer or webhook; run only one consumer per bot"
                ) from None
            logger.error("telegram_updates_failed code=%s", safe_code(error))
        return False

    def send_to(self, chat_id: int, text: str) -> int | None:
        try:
            return self.telegram.send(chat_id, text)
        except ServiceError as error:
            if error.code in {"http_403", "api_403"}:
                self.store.deactivate(chat_id)
                logger.info("subscriber_deactivated reason=forbidden")
                return None
            raise

    def send_test(self) -> dict:
        self.prepare_telegram()
        if not self.sync_updates():
            raise ServiceError("telegram", "update_sync_failed")
        recipients = self.store.subscribers()
        if not recipients:
            raise ConfigurationError("No active subscribers. Send /start in the bot's private chat, then retry send-test")
        sent, failed, inactive = 0, 0, 0
        for chat_id in recipients:
            if time.monotonic() - self.last_update_check >= 10:
                self.sync_updates()
            if not self.store.is_subscribed(chat_id):
                inactive += 1
                continue
            try:
                message_id = self.send_to(chat_id, "Cover Reminder: Telegram delivery is working.")
                if message_id is None:
                    inactive += 1
                else:
                    sent += 1
            except ServiceError as error:
                failed += 1
                logger.error("test_delivery_failed code=%s", safe_code(error))
                if error.retry_after or error.code in {"stopping", "http_401", "api_401", "rate_limited"}:
                    break
        return {"subscribers": len(recipients), "sent": sent, "failed": failed,
                "inactive": inactive, "unattempted": len(recipients) - sent - failed - inactive}

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
            if time.monotonic() - self.last_update_check >= 10:
                self.sync_updates()
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
            final = " This is the final reminder." if hours == 48 else ""
            text = (f"Cover check: this Reel is at least {hours} hours old, and I haven't "
                    f"detected a cover change. Please check its cover.{final}\n\n{reel.permalink}")
            for chat_id in self.store.pending_deliveries(media_id, hours):
                if self.stop.is_set():
                    return False
                if time.monotonic() - self.last_update_check >= 10:
                    self.sync_updates()
                if not self.store.delivery_pending(media_id, hours, chat_id):
                    continue
                try:
                    message_id = self.send_to(chat_id, text)
                    if message_id is not None:
                        self.store.sent(media_id, hours, chat_id, message_id, time.time())
                        sent += 1
                        logger.info("reminder_sent media_id=%s hours=%s message_id=%s", media_id, hours, message_id)
                except ServiceError as error:
                    errors += 1
                    logger.error("delivery_failed media_id=%s code=%s", media_id, safe_code(error))
                    if error.retry_after or error.code in {"http_401", "api_401", "rate_limited"}:
                        break
        self.store.set("last_cycle", {
            "completed_at": time.time(), "ok": errors == 0,
            "discovered": discovered, "checked": checked, "sent": sent, "errors": errors,
        })
        logger.info("poll_complete discovered=%s checked=%s sent=%s errors=%s", discovered, checked, sent, errors)
        return errors == 0

    def run(self, once: bool = False) -> bool:
        self.prepare_telegram()
        self.store.activate(self.config, time.time())
        while not self.stop.is_set():
            updates_ok = self.sync_updates(timeout=0 if once else 10)
            now = time.time()
            delay = self.store.get("next_poll_at", 0) - now
            if delay > 0:
                if once:
                    logger.info("poll_not_due remaining_seconds=%s", round(delay))
                    return updates_ok
                if not updates_ok:
                    self.stop.wait(min(delay, 60))
                continue
            success = self.cycle(now)
            if once:
                return success and updates_ok
        return True


def safe_code(error: Exception) -> str:
    if isinstance(error, ServiceError):
        return f"{error.service}:{error.code}"
    # Unexpected exception text can include upstream URLs and credentials.
    return type(error).__name__
