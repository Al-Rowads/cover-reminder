import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

POLL_SECONDS = 3600


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    composio_api_key: str = field(repr=False)
    connected_account_id: str
    instagram_user_id: str
    toolkit_version: str
    telegram_token: str = field(repr=False)
    database_path: Path
    composio_user_id: str
    difference_threshold: float = 0.05

    @classmethod
    def from_environment(cls) -> "Config":
        required = (
            "COMPOSIO_API_KEY", "COMPOSIO_CONNECTED_ACCOUNT_ID",
            "TELEGRAM_BOT_TOKEN", "COMPOSIO_USER_ID",
        )
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        if missing:
            raise ConfigurationError("Missing configuration: " + ", ".join(missing))
        version = os.environ.get("INSTAGRAM_TOOLKIT_VERSION", "20260819_00")
        if not re.fullmatch(r"\d{8}_\d{2}", version):
            raise ConfigurationError("INSTAGRAM_TOOLKIT_VERSION must be a dated, pinned version")
        user = os.environ.get("INSTAGRAM_USER_ID", "me")
        if user != "me" and not user.isdecimal():
            raise ConfigurationError("INSTAGRAM_USER_ID must be me or a numeric Instagram ID")
        try:
            threshold = float(os.environ.get("COVER_DIFFERENCE_THRESHOLD", "0.05"))
        except ValueError:
            raise ConfigurationError("COVER_DIFFERENCE_THRESHOLD must be numeric") from None
        if not math.isfinite(threshold) or not 0 < threshold < 1:
            raise ConfigurationError("COVER_DIFFERENCE_THRESHOLD must be between 0 and 1")
        return cls(
            composio_api_key=os.environ[required[0]].strip(),
            connected_account_id=os.environ[required[1]].strip(),
            instagram_user_id=user, toolkit_version=version,
            telegram_token=os.environ[required[2]].strip(),
            database_path=database_path(),
            composio_user_id=os.environ[required[3]].strip(),
            difference_threshold=threshold,
        )

    def identity(self) -> dict:
        return {
            "connected_account_id": self.connected_account_id,
            "composio_user_id": self.composio_user_id,
            "instagram_user_id": self.instagram_user_id,
        }

def database_path() -> Path:
    return Path(os.environ.get("DATABASE_PATH", "data/reminders.sqlite3"))
