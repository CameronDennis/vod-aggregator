"""Adding streamers and footage: the steps the CLI and the web UI share."""

from __future__ import annotations

from pathlib import Path

from .db import Database, Footage, Streamer
from .footage import local
from .footage.twitch import VodInfo


def streamer_for_login(db: Database, login: str) -> tuple[Streamer, bool]:
    """The streamer with this Twitch login, and whether they were just added for it."""
    found = db.find_streamer(login)
    if found is not None:
        return found, False
    return db.add_streamer(login, twitch_login=login), True


def find_vod(db: Database, video_id: str) -> Footage | None:
    """The footage already registered for a Twitch VOD, if any."""
    return next((f for f in db.list_footage() if f.twitch_video_id == video_id), None)


def add_vod(db: Database, owner_id: int, info: VodInfo, local_path: str | None = None) -> Footage:
    return db.add_footage(
        owner_id,
        "vod",
        twitch_video_id=info.video_id,
        source_url=info.url,
        local_path=local_path,
        started_at=info.started_at,
        duration_s=info.duration_s,
    )


def import_file(db: Database, owner_id: int, path: Path, started_at: int | None) -> Footage:
    """Register a video file on this machine, with its length read by ffprobe."""
    duration = local.probe_duration(path)
    return db.add_footage(
        owner_id,
        "local",
        local_path=str(path.resolve()),
        started_at=started_at,
        duration_s=int(duration) if duration else None,
    )
