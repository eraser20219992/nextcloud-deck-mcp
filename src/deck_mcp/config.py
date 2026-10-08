from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    url: str
    username: str
    password: str
    board: str | None
    lane: str | None
    board_id: int | None
    lane_id: int | None
    assignee: str | None

    @property
    def agent_uid(self) -> str:
        """The user id whose card assignments are treated as 'assigned to me'."""
        return self.assignee or self.username


def _optional(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _optional_int(name: str) -> int | None:
    value = _optional(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


def load_config(env_file: str | None = None) -> Config:
    """Load and validate configuration from environment variables (and .env)."""
    if env_file is not None:
        load_dotenv(env_file)
    else:
        load_dotenv()

    required = ("NEXTCLOUD_URL", "NEXTCLOUD_USERNAME", "NEXTCLOUD_APP_PASSWORD")
    missing = [name for name in required if not _optional(name)]
    if missing:
        raise ConfigError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    url = _optional("NEXTCLOUD_URL").rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ConfigError("NEXTCLOUD_URL must start with http:// or https://")

    board = _optional("DECK_BOARD")
    board_id = _optional_int("DECK_BOARD_ID")
    if board is None and board_id is None:
        raise ConfigError("Set DECK_BOARD (board title) or DECK_BOARD_ID")

    lane = _optional("DECK_LANE")
    lane_id = _optional_int("DECK_LANE_ID")
    if lane is None and lane_id is None:
        raise ConfigError("Set DECK_LANE (list/lane title) or DECK_LANE_ID")

    return Config(
        url=url,
        username=os.environ["NEXTCLOUD_USERNAME"],
        password=os.environ["NEXTCLOUD_APP_PASSWORD"],
        board=board,
        lane=lane,
        board_id=board_id,
        lane_id=lane_id,
        assignee=_optional("DECK_ASSIGNEE"),
    )
