"""Cutting clips around match events, from each synced perspective.

An event happens at one demo tick. Every piece of footage synced to the match
maps that tick to its own time (offset + tick / tick_rate), so the same moment
can be cut from the main streamer's footage and from the footage of other
streamers in the fight.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from . import ffmpeg
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
    """The clips to cut: each event from `before` seconds ahead of it to `after` seconds past it, as plan_windows.
    Events whose clips would overlap share one longer clip."""
    syncs = db.footage_syncs(match_id)
    if not syncs:
        return []
    rate = syncs[0]["tick_rate"]
    wanted = set(event_ids)
    windows = [
        Window(event["tick"] - before * rate, event["tick"] + after * rate, event["id"])
        for event in db.match_events(match_id)
        if event["id"] in wanted
    ]
    return plan_windows(db, match_id, main, merge_overlapping(windows), out_dir, others)


@dataclass(frozen=True)
class Window:
    """A stretch of a match to clip, in demo ticks, optionally around one event
    (`event_id`) and any later ones it was merged with (`more_event_ids`)."""

    start_tick: float
    end_tick: float
    event_id: int | None = None
    label: str | None = None
    more_event_ids: tuple[int, ...] = ()


def merge_overlapping(windows: list[Window]) -> list[Window]:
    """Join windows that overlap into one, from the first one's start to the last one's end."""
    merged: list[Window] = []
    for window in sorted(windows, key=lambda w: w.start_tick):
        last = merged[-1] if merged else None
        if last and window.start_tick < last.end_tick:
            ids = tuple(i for i in (window.event_id, *window.more_event_ids) if i is not None)
            merged[-1] = replace(last, end_tick=max(last.end_tick, window.end_tick),
                                 more_event_ids=last.more_event_ids + ids)  # fmt: skip
        else:
            merged.append(window)
    return merged


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
        merged = [events[i] for i in window.more_event_ids if i in events]
        involved = {p for e in [event, *merged] if e for p in (e["actor"], e["target"], *e["assisters"])} or None
        if event:
            clock = int(event["clock_s"] or 0)
            stem = f"{clock // 60:02d}-{clock % 60:02d}_{_kind_for(event, main)}" + (f"+{len(merged)}" if merged else "")
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
    return _unique_paths(clips)


def _unique_paths(clips: list[Clip]) -> list[Clip]:
    """Number clips that would share a file, such as two kills in the same second, so none overwrites another."""
    seen: Counter[Path] = Counter()
    unique = []
    for clip in clips:
        seen[clip.path] += 1
        n = seen[clip.path]
        unique.append(clip if n == 1 else replace(clip, path=clip.path.with_stem(f"{clip.path.stem}_{n}")))
    return unique


def cut(clip: Clip, playlists: dict[int, hls.MediaPlaylist] | None = None) -> Path:
    """Save one clip, from the local file if there is one, else from the stream."""
    clip.path.parent.mkdir(parents=True, exist_ok=True)
    item = clip.footage
    if item.local_path and Path(item.local_path).exists():
        ffmpeg.run("-ss", f"{clip.start:.3f}", "-i", item.local_path, "-t", f"{clip.end - clip.start:.3f}",
                   *hls.EXACT_CODECS, "-movflags", "+faststart", str(clip.path))  # fmt: skip
        return clip.path
    playlists = {} if playlists is None else playlists
    if item.id not in playlists:
        url = download.media_playlist_url(item.source_url) if item.source_url else None
        if url is None:
            raise RuntimeError(f"footage {item.id} has no file or stream to cut from")
        playlists[item.id] = hls.load_playlist(url)
    return hls.fetch_section(playlists[item.id], clip.start, clip.end, clip.path, exact=True)


def _kind_for(event: dict, main: Streamer) -> str:
    if event["kind"] == "ult":
        return "ult"
    if event["target"] in main.steam_account_ids:
        return "death"
    return "kill"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "streamer"
