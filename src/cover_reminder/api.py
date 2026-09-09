import json
import math
import random
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from threading import Event
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import Config
from .images import MAX_IMAGE_BYTES
from .model import Reel

MEDIA_FIELDS = "id,media_type,media_product_type,permalink,thumbnail_url,timestamp"


class ServiceError(Exception):
    def __init__(self, service: str, code: str, retry_after: float = 0):
        self.service = service
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"{service}: {code}")


def validate_thumbnail_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        allowed = any(host == domain or host.endswith("." + domain) for domain in (
            "cdninstagram.com", "fbcdn.net", "fbsbx.com", "instagram.com",
        ))
        if (parsed.scheme != "https" or not allowed or parsed.username or parsed.password
                or parsed.port not in {None, 443}):
            raise ValueError
    except ValueError:
        raise ServiceError("thumbnail", "untrusted_url") from None


class RedirectPolicy(HTTPRedirectHandler):
    def __init__(self, service: str):
        self.service = service

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # API credentials must never follow a redirect to another destination.
        if self.service != "thumbnail":
            raise ServiceError(self.service, "unexpected_redirect")
        validate_thumbnail_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def retry_delay(headers, content: bytes) -> float:
    delay = 0.0
    raw = headers.get("Retry-After")
    if raw:
        try:
            delay = float(raw)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                pass
    try:
        body = json.loads(content)
        parameters = body.get("parameters", {}) if isinstance(body, dict) else {}
        seconds = parameters.get("retry_after", 0) if isinstance(parameters, dict) else 0
        delay = max(delay, float(seconds))
    except (ValueError, TypeError, UnicodeError):
        pass
    return max(0.0, delay) if math.isfinite(delay) else 0.0


class Http:
    def __init__(self, stop: Event | None = None):
        self.stop = stop or Event()
        self.cooldowns: dict[str, float] = {}
        self.on_cooldown: Callable[[str, float], None] | None = None

    def cool_down(self, service: str, delay: float) -> None:
        if delay > 0:
            until = max(self.cooldowns.get(service, 0), time.time() + delay)
            self.cooldowns[service] = until
            if self.on_cooldown:
                self.on_cooldown(service, until)

    def request(self, service: str, url: str, payload: dict | None = None,
                headers: dict | None = None, limit: int = 2 * 1024 * 1024) -> bytes:
        remaining = self.cooldowns.get(service, 0) - time.time()
        if remaining > 0:
            raise ServiceError(service, "rate_limited", remaining)
        if service == "thumbnail":
            validate_thumbnail_url(url)
        body = json.dumps(payload).encode() if payload is not None else None
        request_headers = {"User-Agent": "cover-reminder/0.1", **(headers or {})}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=request_headers)
        opener = build_opener(RedirectPolicy(service))
        for attempt in range(3):
            if self.stop.is_set():
                raise ServiceError(service, "stopping")
            try:
                with opener.open(request, timeout=20) as response:
                    content = response.read(limit + 1)
                    if len(content) > limit:
                        raise ServiceError(service, "response_too_large")
                    return content
            except HTTPError as error:
                with error:
                    try:
                        content = error.read(64 * 1024)
                    except (OSError, HTTPException):
                        content = b""
                delay = retry_delay(error.headers, content)
                code = f"http_{error.code}"
                retryable = error.code in {408, 429, 500, 502, 503, 504}
                self.cool_down(service, delay)
                if not retryable or attempt == 2 or delay > 30:
                    raise ServiceError(service, code, delay) from None
            except (URLError, TimeoutError, OSError, HTTPException):
                code, delay = "network_error", 0.0
                if attempt == 2:
                    raise ServiceError(service, code) from None
            if self.stop.wait(max(delay, 2 ** attempt + random.random())):
                raise ServiceError(service, "stopping")
        raise ServiceError(service, "request_failed")

    def json(self, service: str, url: str, payload: dict | None = None,
             headers: dict | None = None) -> dict:
        content = self.request(service, url, payload, headers)
        try:
            result = json.loads(content)
        except (ValueError, UnicodeError):
            raise ServiceError(service, "invalid_json") from None
        if not isinstance(result, dict):
            raise ServiceError(service, "unexpected_response")
        return result


class Instagram:
    def __init__(self, config: Config, http: Http):
        self.config, self.http = config, http

    def execute(self, tool: str, arguments: dict) -> dict:
        result = self.http.json(
            "composio", f"https://backend.composio.dev/api/v3.1/tools/execute/{tool}",
            {"connected_account_id": self.config.connected_account_id,
             "version": self.config.toolkit_version, "arguments": arguments},
            {"x-api-key": self.config.composio_api_key},
        )
        if result.get("successful") is not True:
            raise ServiceError("composio", "tool_execution_failed")
        data = result.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (ValueError, UnicodeError):
                raise ServiceError("composio", "invalid_tool_data") from None
        if not isinstance(data, dict):
            raise ServiceError("composio", "unexpected_tool_data")
        return data

    def page(self, **parameters) -> dict:
        return self.execute("INSTAGRAM_GET_IG_USER_MEDIA", {
            "ig_user_id": self.config.instagram_user_id, "fields": MEDIA_FIELDS,
            "limit": 100, **parameters,
        })

    def discover(self, since: float, until: float) -> Iterator[Reel]:
        parameters = {"since": int(since), "until": math.ceil(until)}
        cursors: set[str] = set()
        while True:
            page = self.page(**parameters)
            media = page.get("data")
            if not isinstance(media, list):
                raise ServiceError("composio", "missing_media_collection")
            for item in media:
                if not isinstance(item, dict):
                    raise ServiceError("composio", "invalid_media_object")
                try:
                    reel = Reel.from_media(item)
                except (ValueError, TypeError, OverflowError):
                    raise ServiceError("composio", "invalid_reel_fields") from None
                if reel:
                    yield reel
            paging = page.get("paging", {})
            if not isinstance(paging, dict):
                raise ServiceError("composio", "invalid_pagination")
            if not paging.get("next"):
                return
            cursor_data = paging.get("cursors", {})
            cursor = cursor_data.get("after") if isinstance(cursor_data, dict) else None
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ServiceError("composio", "invalid_pagination_cursor")
            cursors.add(cursor)
            parameters["after"] = cursor

    def get(self, media_id: str) -> Reel:
        data = self.execute("INSTAGRAM_GET_IG_MEDIA", {
            "ig_media_id": media_id, "fields": MEDIA_FIELDS,
        })
        try:
            reel = Reel.from_media(data)
        except (ValueError, TypeError, OverflowError):
            raise ServiceError("composio", "invalid_reel_fields") from None
        if reel is None or reel.media_id != media_id:
            raise ServiceError("composio", "unexpected_media_identity")
        return reel

    def thumbnail(self, reel: Reel) -> bytes:
        if not reel.thumbnail_url:
            raise ServiceError("thumbnail", "missing_thumbnail")
        return self.http.request("thumbnail", reel.thumbnail_url, limit=MAX_IMAGE_BYTES)


class Telegram:
    def __init__(self, config: Config, http: Http):
        self.config, self.http = config, http

    def call(self, method: str, payload: dict | None = None) -> dict:
        response = self.http.json(
            "telegram", f"https://api.telegram.org/bot{self.config.telegram_token}/{method}",
            payload,
        )
        if response.get("ok") is not True:
            delay = retry_delay({}, json.dumps(response).encode())
            self.http.cool_down("telegram", delay)
            code = response.get("error_code")
            reason = f"api_{code}" if isinstance(code, int) else "unsuccessful_response"
            raise ServiceError("telegram", reason, delay)
        if not isinstance(response.get("result"), dict):
            raise ServiceError("telegram", "unexpected_response")
        return response["result"]

    def check(self) -> dict:
        return self.call("getMe")

    def send(self, text: str) -> int:
        response = self.call("sendMessage", {
            "chat_id": self.config.telegram_chat_id, "text": text,
            "disable_notification": False,
        })
        message_id = response.get("message_id")
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            raise ServiceError("telegram", "missing_message_id")
        return message_id
