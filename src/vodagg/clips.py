"""Cutting clips around match events, from each synced perspective.

An event happens at one demo tick. Every piece of footage synced to the match
maps that tick to its own time (offset + tick / tick_rate), so the same moment
can be cut from the main streamer's footage and from the footage of other
streamers in the fight.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .db import Database, Footage, Streamer
from .footage import download, hls


@dataclass(frozen=True)
class Clip:
    event_id: int | None
    streamer: str
    footage: Footage
    start: float
    end: float
    path: Path


def plan_clips(
    db: Database,
    match_id: int,
    main: Streamer,
    event_ids: list[int],
    before: float,
    after: float,
    out_dir: Path,
    others: bool = True,
) -> list[Clip]:
    """The clips to cut: each event from the main streamer's footage and, with
    `others`, from every other streamer in the event who has synced footage."""
    syncs = {row["footage_id"]: row for row in db.footage_syncs(match_id)}
    synced = [f for f in db.list_footage() if f.id in syncs]
    streamers = {s.id: s for s in db.list_streamers()}
    wanted = set(event_ids)
    clips = []
    for event in db.match_events(match_id):
        if event["id"] not in wanted:
            continue
        involved = {event["actor"], event["target"], *event["assisters"]}
        kind = _kind_for(event, main)
        clock = int(event["clock_s"] or 0)
        for item in synced:
            streamer = streamers[item.streamer_id]
            if streamer.id != main.id and not (others and involved & set(streamer.steam_account_ids)):
                continue
            row = syncs[item.id]
            t = row["offset_s"] + event["tick"] / row["tick_rate"]
            name = f"{clock // 60:02d}-{clock % 60:02d}_{kind}_{_safe(streamer.name)}.mp4"
            clips.append(Clip(event["id"], streamer.name, item, max(0.0, t - before), t + after, out_dir / name))
    return clips


@dataclass(frozen=True)
class Window:
    """A stretch of a match to clip, in demo ticks, optionally around one event."""

    start_tick: float
    end_tick: float
    event_id: int | None = None
    label: str | None = None


def plan_windows(
    db: Database, match_id: int, main: Streamer, windows: list[Window], out_dir: Path, others: bool = True
) -> list[Clip]:
    """The clips to cut for chosen stretches of a match: each from the main
    streamer's footage and, with `others`, from other streamers' synced footage
    (only those in the event, for a window around one)."""
    syncs = {row["footage_id"]: row for row in db.footage_syncs(match_id)}
    synced = [f for f in db.list_footage() if f.id in syncs]
    streamers = {s.id: s for s in db.list_streamers()}
    events = {event["id"]: event for event in db.match_events(match_id)}
    clips = []
    for window in windows:
        event = events.get(window.event_id) if window.event_id is not None else None
        involved = {event["actor"], event["target"], *event["assisters"]} if event else None
        if event:
            clock = int(event["clock_s"] or 0)
            stem = f"{clock // 60:02d}-{clock % 60:02d}_{_kind_for(event, main)}"
        else:
            stem = _safe(window.label or "clip")
        for item in synced:
            streamer = streamers[item.streamer_id]
            in_view = involved is None or bool(involved & set(streamer.steam_account_ids))
            if streamer.id != main.id and not (others and in_view):
                continue
            row = syncs[item.id]
            start = row["offset_s"] + window.start_tick / row["tick_rate"]
            end = row["offset_s"] + window.end_tick / row["tick_rate"]
            path = out_dir / f"{stem}_{_safe(streamer.name)}.mp4"
            clips.append(Clip(window.event_id, streamer.name, item, max(0.0, start), end, path))
    return clips


def cut(clip: Clip, playlists: dict[int, hls.MediaPlaylist] | None = None) -> Path:
    """Save one clip, from the local file if there is one, else from the stream."""
    clip.path.parent.mkdir(parents=True, exist_ok=True)
    item = clip.footage
    if item.local_path and Path(item.local_path).exists():
        executable = shutil.which("ffmpeg")
        if executable is None:
            raise RuntimeError("ffmpeg is not installed")
        result = subprocess.run(
            [executable, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{clip.start:.3f}",
             "-i", item.local_path, "-t", f"{clip.end - clip.start:.3f}", "-c", "copy", str(clip.path)],
            capture_output=True, text=True,
        )  # fmt: skip
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()}")
        return clip.path
    playlists = {} if playlists is None else playlists
    if item.id not in playlists:
        url = download.media_playlist_url(item.source_url) if item.source_url else None
        if url is None:
            raise RuntimeError(f"footage {item.id} has no file or stream to cut from")
        playlists[item.id] = hls.load_playlist(url)
    return hls.fetch_section(playlists[item.id], clip.start, clip.end, clip.path)


def _kind_for(event: dict, main: Streamer) -> str:
    if event["kind"] == "ult":
        return "ult"
    if event["target"] in main.steam_account_ids:
        return "death"
    return "kill"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "streamer"
