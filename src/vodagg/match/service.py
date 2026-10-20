"""Putting match parsing, the database and footage sync together."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..db import Database, Footage, Streamer
from ..footage import download, hls, local
from . import events, scan


def frame_source(item: Footage) -> scan.FrameSource:
    """Frames of registered footage: the local file if there is one, else the stream."""
    if item.local_path and Path(item.local_path).exists():
        duration = item.duration_s or local.probe_duration(Path(item.local_path)) or 0.0
        return scan.FileFrames(Path(item.local_path), float(duration))
    if not item.source_url:
        raise ValueError(f"footage {item.id} has no file or stream to read")
    playlist_url = download.media_playlist_url(item.source_url)
    if playlist_url is None:
        raise ValueError(f"footage {item.id} isn't an HLS stream; download it and import the file")
    return scan.HlsFrames(hls.load_playlist(playlist_url))


def record_match(db: Database, match: events.ParsedMatch, demo_path: Path) -> list[Streamer]:
    """Store a parsed match and link players to streamers.

    Returns streamers who gained a Steam id here: known streamers with no
    Steam id yet whose name or Twitch login matches a player's name.
    """
    db.upsert_match(
        match.match_id,
        duration_s=int(match.clock.match_seconds(match.clock.last_tick) or 0) or None,
        demo_path=str(demo_path),
        tick_rate=match.tick_rate,
    )
    db.set_match_players(
        match.match_id, [(p.account_id, p.hero_id, p.team, p.name, p.hero_name) for p in match.players]
    )
    db.set_match_events(
        match.match_id,
        [
            (e.tick, match.clock.match_seconds(e.tick), e.kind, e.actor, e.target, e.assisters, e.ability)
            for e in match.events
        ],
    )
    linked = []
    for streamer in db.list_streamers():
        if streamer.steam_account_id is not None:
            continue
        keys = {_simplify(streamer.name), _simplify(streamer.twitch_login or "")} - {""}
        matches = [p for p in match.players if _simplify(p.name) in keys]
        if len(matches) == 1 and db.find_streamer(matches[0].account_id) is None:
            linked.append(db.update_streamer(streamer.id, steam_account_id=matches[0].account_id))
    return linked


@dataclass(frozen=True)
class TimedEvent:
    kind: str  # "kill", "death" or "ult", from the point of view of `player`
    tick: int
    clock_s: float
    footage_t: float | None
    player: str
    other: str | None
    ability: str | None


def player_events(db: Database, match_id: int, account_id: int, sync_row: dict | None) -> list[TimedEvent]:
    """A player's kills, deaths and ults with match-clock and footage times."""
    names = {p["account_id"]: f'{p["player_name"]} ({p["hero_name"]})' for p in db.match_players(match_id)}
    timed = []
    for event in db.match_events(match_id):
        if event["kind"] == "kill" and event["actor"] == account_id:
            kind, other = "kill", event["target"]
        elif event["kind"] == "kill" and event["target"] == account_id:
            kind, other = "death", event["actor"]
        elif event["kind"] == "ult" and event["actor"] == account_id:
            kind, other = "ult", None
        else:
            continue
        timed.append(
            TimedEvent(
                kind=kind,
                tick=event["tick"],
                clock_s=event["clock_s"],
                footage_t=sync_row["offset_s"] + event["tick"] / sync_row["tick_rate"] if sync_row else None,
                player=names.get(account_id, str(account_id)),
                other=names.get(other, "non-player") if kind != "ult" else None,
                ability=event["ability"],
            )
        )
    return timed


def _simplify(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())
