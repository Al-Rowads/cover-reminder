from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Reel:
    media_id: str
    permalink: str
    published_at: float
    thumbnail_url: str | None

    @classmethod
    def from_media(cls, media: dict) -> "Reel | None":
        if media.get("media_type") != "VIDEO" or media.get("media_product_type") != "REELS":
            return None
        media_id = media.get("id")
        permalink = media.get("permalink")
        timestamp = media.get("timestamp")
        if not isinstance(media_id, str) or not media_id.isdecimal():
            raise ValueError("invalid_media_id")
        if not isinstance(permalink, str) or not isinstance(timestamp, str):
            raise ValueError("missing_media_fields")
        link = urlsplit(permalink)
        if (link.scheme != "https" or link.hostname not in {"instagram.com", "www.instagram.com"}
                or link.username or link.password or link.port not in {None, 443}):
            raise ValueError("invalid_permalink")
        published = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if published.tzinfo is None:
            raise ValueError("missing_timestamp_timezone")
        thumbnail = media.get("thumbnail_url")
        if thumbnail is not None and not isinstance(thumbnail, str):
            raise ValueError("invalid_thumbnail_url")
        return cls(media_id, permalink, published.timestamp(), thumbnail)
