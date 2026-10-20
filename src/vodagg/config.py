"""Paths and settings, overridable with environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path
    twitch_client_id: str | None
    twitch_client_secret: str | None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "vodagg.sqlite3"

    @property
    def footage_dir(self) -> Path:
        return self.data_dir / "footage"

    @property
    def has_twitch_api(self) -> bool:
        return bool(self.twitch_client_id and self.twitch_client_secret)


def load_config() -> Config:
    return Config(
        data_dir=Path(os.environ.get("VODAGG_DATA_DIR", "data")).expanduser(),
        twitch_client_id=os.environ.get("TWITCH_CLIENT_ID"),
        twitch_client_secret=os.environ.get("TWITCH_CLIENT_SECRET"),
    )
