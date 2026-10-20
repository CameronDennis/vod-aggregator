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
    parsed_at  INTEGER
);

CREATE TABLE IF NOT EXISTS match_players (
    match_id   INTEGER NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL,
    hero_id    INTEGER,
    team       INTEGER,
    PRIMARY KEY (match_id, account_id)
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
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO matches (match_id, start_time, duration_s, demo_path, parsed_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(match_id) DO UPDATE SET"
                "   start_time = COALESCE(excluded.start_time, start_time),"
                "   duration_s = COALESCE(excluded.duration_s, duration_s),"
                "   demo_path  = COALESCE(excluded.demo_path, demo_path),"
                "   parsed_at  = excluded.parsed_at",
                (match_id, start_time, duration_s, demo_path, int(time.time())),
            )

    def set_match_players(self, match_id: int, players: list[tuple[int, int | None, int | None]]) -> None:
        """Record (account_id, hero_id, team) for everyone in a match."""
        with self.conn:
            self.conn.execute("DELETE FROM match_players WHERE match_id = ?", (match_id,))
            self.conn.executemany(
                "INSERT INTO match_players (match_id, account_id, hero_id, team) VALUES (?, ?, ?, ?)",
                [(match_id, *player) for player in players],
            )

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
