"""SQLite store for streamers, matches and footage.

Streamers are linked to their Twitch login and their Steam account id (the
32-bit id Deadlock uses in demos and on deadlock-api.com). Every parsed match
records who played in it, so a match can be cross-checked for known streamers.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS streamers (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE COLLATE NOCASE,
    twitch_login     TEXT UNIQUE COLLATE NOCASE,
    steam_account_id INTEGER UNIQUE,
    notes            TEXT,
    created_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    match_id   INTEGER PRIMARY KEY,
    start_time INTEGER,
    duration_s INTEGER,
    demo_path  TEXT,
    parsed_at  INTEGER,
    tick_rate  REAL
);

CREATE TABLE IF NOT EXISTS match_players (
    match_id    INTEGER NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    account_id  INTEGER NOT NULL,
    hero_id     INTEGER,
    team        INTEGER,
    player_name TEXT,
    hero_name   TEXT,
    PRIMARY KEY (match_id, account_id)
);

-- Kills (actor killed target), and ults (target is null), by demo tick.
CREATE TABLE IF NOT EXISTS match_events (
    id         INTEGER PRIMARY KEY,
    match_id   INTEGER NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    tick       INTEGER NOT NULL,
    clock_s    REAL,
    kind       TEXT NOT NULL,
    actor      INTEGER,
    target     INTEGER,
    assisters  TEXT,
    ability    TEXT
);

CREATE INDEX IF NOT EXISTS match_events_match ON match_events(match_id, tick);

-- Where a match sits in a piece of footage: footage time = offset_s + tick / tick_rate.
CREATE TABLE IF NOT EXISTS footage_sync (
    footage_id    INTEGER NOT NULL REFERENCES footage(id) ON DELETE CASCADE,
    match_id      INTEGER NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    offset_s      REAL NOT NULL,
    tick_rate     REAL NOT NULL,
    uncertainty_s REAL,
    readings      INTEGER,
    synced_at     INTEGER NOT NULL,
    PRIMARY KEY (footage_id, match_id)
);

CREATE TABLE IF NOT EXISTS footage (
    id              INTEGER PRIMARY KEY,
    streamer_id     INTEGER NOT NULL REFERENCES streamers(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN ('vod', 'recovered', 'live', 'local')),
    twitch_video_id TEXT,
    broadcast_id    TEXT,
    source_url      TEXT,
    local_path      TEXT,
    started_at      INTEGER,
    duration_s      INTEGER,
    match_id        INTEGER REFERENCES matches(match_id),
    created_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS footage_streamer ON footage(streamer_id, started_at);
"""


@dataclass(frozen=True)
class Streamer:
    id: int
    name: str
    twitch_login: str | None
    steam_account_id: int | None
    notes: str | None


@dataclass(frozen=True)
class Footage:
    id: int
    streamer_id: int
    kind: str
    twitch_video_id: str | None
    broadcast_id: str | None
    source_url: str | None
    local_path: str | None
    started_at: int | None
    duration_s: int | None
    match_id: int | None

    def covers(self, unix_time: float) -> bool:
        """Whether this footage was recording at the given moment."""
        if self.started_at is None or self.duration_s is None:
            return False
        return self.started_at <= unix_time <= self.started_at + self.duration_s


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Bring databases made by older versions up to the current schema."""
        added = {"matches": {"tick_rate": "REAL"}, "match_players": {"player_name": "TEXT", "hero_name": "TEXT"}}
        for table, columns in added.items():
            existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # Streamers

    def add_streamer(
        self,
        name: str,
        twitch_login: str | None = None,
        steam_account_id: int | None = None,
        notes: str | None = None,
    ) -> Streamer:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO streamers (name, twitch_login, steam_account_id, notes, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (name, twitch_login, steam_account_id, notes, int(time.time())),
            )
        return self.get_streamer_by_id(cur.lastrowid)

    def update_streamer(self, streamer_id: int, **fields) -> Streamer:
        allowed = {"name", "twitch_login", "steam_account_id", "notes"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown streamer fields: {sorted(unknown)}")
        if fields:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            with self.conn:
                self.conn.execute(
                    f"UPDATE streamers SET {assignments} WHERE id = ?",
                    (*fields.values(), streamer_id),
                )
        return self.get_streamer_by_id(streamer_id)

    def remove_streamer(self, streamer_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM streamers WHERE id = ?", (streamer_id,))

    def get_streamer_by_id(self, streamer_id: int) -> Streamer:
        row = self.conn.execute("SELECT * FROM streamers WHERE id = ?", (streamer_id,)).fetchone()
        if row is None:
            raise KeyError(f"no streamer with id {streamer_id}")
        return _streamer(row)

    def find_streamer(self, key: str | int) -> Streamer | None:
        """Look a streamer up by name, Twitch login or Steam account id."""
        row = self.conn.execute(
            "SELECT * FROM streamers WHERE name = ? OR twitch_login = ? OR steam_account_id = ?",
            (str(key), str(key), _as_int(key)),
        ).fetchone()
        return _streamer(row) if row else None

    def list_streamers(self) -> list[Streamer]:
        rows = self.conn.execute("SELECT * FROM streamers ORDER BY name").fetchall()
        return [_streamer(row) for row in rows]

    # Matches

    def upsert_match(
        self,
        match_id: int,
        start_time: int | None = None,
        duration_s: int | None = None,
        demo_path: str | None = None,
        tick_rate: float | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO matches (match_id, start_time, duration_s, demo_path, parsed_at, tick_rate)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(match_id) DO UPDATE SET"
                "   start_time = COALESCE(excluded.start_time, start_time),"
                "   duration_s = COALESCE(excluded.duration_s, duration_s),"
                "   demo_path  = COALESCE(excluded.demo_path, demo_path),"
                "   tick_rate  = COALESCE(excluded.tick_rate, tick_rate),"
                "   parsed_at  = excluded.parsed_at",
                (match_id, start_time, duration_s, demo_path, int(time.time()), tick_rate),
            )

    def get_match(self, match_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,)).fetchone()
        return dict(row) if row else None

    def set_match_players(self, match_id: int, players: list[tuple]) -> None:
        """Record (account_id, hero_id, team[, player_name, hero_name]) for everyone in a match."""
        with self.conn:
            self.conn.execute("DELETE FROM match_players WHERE match_id = ?", (match_id,))
            self.conn.executemany(
                "INSERT INTO match_players (match_id, account_id, hero_id, team, player_name, hero_name)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(match_id, *player, *(None,) * (5 - len(player))) for player in players],
            )

    def match_players(self, match_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM match_players WHERE match_id = ? ORDER BY team, account_id", (match_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def set_match_events(self, match_id: int, events: list[tuple]) -> None:
        """Record (tick, clock_s, kind, actor, target, assisters, ability) events, replacing earlier ones.

        clock_s is the match clock at the event, as shown on the HUD.
        """
        with self.conn:
            self.conn.execute("DELETE FROM match_events WHERE match_id = ?", (match_id,))
            self.conn.executemany(
                "INSERT INTO match_events (match_id, tick, clock_s, kind, actor, target, assisters, ability)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (match_id, tick, clock_s, kind, actor, target, ",".join(map(str, assisters)), ability)
                    for tick, clock_s, kind, actor, target, assisters, ability in events
                ],
            )

    def match_events(self, match_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM match_events WHERE match_id = ? ORDER BY tick, id", (match_id,)
        ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["assisters"] = [int(a) for a in (event["assisters"] or "").split(",") if a]
            events.append(event)
        return events

    def set_footage_sync(
        self,
        footage_id: int,
        match_id: int,
        offset_s: float,
        tick_rate: float,
        uncertainty_s: float | None = None,
        readings: int | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO footage_sync"
                " (footage_id, match_id, offset_s, tick_rate, uncertainty_s, readings, synced_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (footage_id, match_id, offset_s, tick_rate, uncertainty_s, readings, int(time.time())),
            )
            self.conn.execute(
                "UPDATE footage SET match_id = ? WHERE id = ? AND match_id IS NULL", (match_id, footage_id)
            )

    def footage_syncs(self, match_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM footage_sync WHERE match_id = ? ORDER BY footage_id", (match_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def streamers_in_match(self, match_id: int) -> list[Streamer]:
        """Known streamers who played in a match, matched by Steam account id."""
        rows = self.conn.execute(
            "SELECT s.* FROM streamers s"
            " JOIN match_players p ON p.account_id = s.steam_account_id"
            " WHERE p.match_id = ? ORDER BY s.name",
            (match_id,),
        ).fetchall()
        return [_streamer(row) for row in rows]

    # Footage

    def add_footage(
        self,
        streamer_id: int,
        kind: str,
        *,
        twitch_video_id: str | None = None,
        broadcast_id: str | None = None,
        source_url: str | None = None,
        local_path: str | None = None,
        started_at: int | None = None,
        duration_s: int | None = None,
        match_id: int | None = None,
    ) -> Footage:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO footage (streamer_id, kind, twitch_video_id, broadcast_id, source_url,"
                " local_path, started_at, duration_s, match_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    streamer_id,
                    kind,
                    twitch_video_id,
                    broadcast_id,
                    source_url,
                    local_path,
                    started_at,
                    duration_s,
                    match_id,
                    int(time.time()),
                ),
            )
        row = self.conn.execute("SELECT * FROM footage WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _footage(row)

    def get_footage(self, footage_id: int) -> Footage | None:
        row = self.conn.execute("SELECT * FROM footage WHERE id = ?", (footage_id,)).fetchone()
        return _footage(row) if row else None

    def list_footage(self, streamer_id: int | None = None) -> list[Footage]:
        if streamer_id is None:
            rows = self.conn.execute("SELECT * FROM footage ORDER BY started_at").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM footage WHERE streamer_id = ? ORDER BY started_at", (streamer_id,)
            ).fetchall()
        return [_footage(row) for row in rows]

    def footage_covering(self, streamer_id: int, unix_time: float) -> list[Footage]:
        """Footage of a streamer that was recording at a given moment."""
        return [f for f in self.list_footage(streamer_id) if f.covers(unix_time)]


def _as_int(value: str | int) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _streamer(row: sqlite3.Row) -> Streamer:
    return Streamer(
        id=row["id"],
        name=row["name"],
        twitch_login=row["twitch_login"],
        steam_account_id=row["steam_account_id"],
        notes=row["notes"],
    )


def _footage(row: sqlite3.Row) -> Footage:
    return Footage(**{key: row[key] for key in Footage.__dataclass_fields__})
