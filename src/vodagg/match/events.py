"""Kills, deaths and ultimate uses from a demo, parsed with boon.

A kill is stored once, with the attacker as `actor` and the victim as
`target`; a player's deaths are the kills where they are the target. Players
are identified by their 32-bit Steam account id, the id deadlock-api.com and
the streamer database use.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

STEAM64_BASE = 76561197960265728


@dataclass(frozen=True)
class Player:
    account_id: int
    name: str
    hero_id: int
    hero_name: str
    team: int


@dataclass(frozen=True)
class Event:
    kind: str  # "kill" or "ult"
    tick: int
    actor: int | None  # account id; None for a kill by troopers, guardians and the like
    target: int | None = None
    assisters: tuple[int, ...] = ()
    ability: str | None = None


@dataclass
class ParsedMatch:
    match_id: int
    tick_rate: float
    players: list[Player]
    events: list[Event]
    clock: "BoonClock"
    notes: list[str]

    def player(self, account_id: int | None) -> Player | None:
        return next((p for p in self.players if p.account_id == account_id), None)


class BoonClock:
    """The match clock from boon's pause-aware tick conversions."""

    def __init__(self, demo):
        self.demo = demo
        self.tick_rate = float(demo.tick_rate)
        self.first_tick = 0
        self.last_tick = int(demo.game_over_tick or demo.total_ticks)
        self._start_s = None
        if demo.pregame_seconds is None:
            # No pregame length recorded: count from the game-start tick instead.
            self._start_s = demo.tick_to_seconds(int(demo.game_start_tick or 0))

    def match_seconds(self, tick: int) -> float | None:
        if self._start_s is not None:
            return self.demo.tick_to_seconds(tick) - self._start_s
        return self.demo.tick_to_match_seconds(tick)


def account_id(steam_id: int) -> int:
    """32-bit account id from a 64-bit Steam id (or an account id already)."""
    return steam_id - STEAM64_BASE if steam_id >= STEAM64_BASE else steam_id


def parse(path) -> ParsedMatch:
    from boon import Demo, hero_names

    demo = Demo(str(path))
    heroes = hero_names()
    players = [
        Player(
            account_id=account_id(int(row["steam_id"])),
            name=row["player_name"],
            hero_id=int(row["hero_id"]),
            hero_name=heroes.get(int(row["hero_id"]), str(row["hero_id"])),
            team=int(row["team_num"]),
        )
        for row in demo.players.iter_rows(named=True)
    ]
    by_hero = {p.hero_id: p.account_id for p in players}
    notes: list[str] = []

    events = [
        Event(
            kind="kill",
            tick=int(row["tick"]),
            actor=by_hero.get(row["attacker_hero_id"]),
            target=by_hero.get(row["victim_hero_id"]),
            assisters=tuple(by_hero[h] for h in row["assister_hero_ids"] or () if h in by_hero),
        )
        for row in demo.kills.iter_rows(named=True)
    ]

    ultimates = ultimate_abilities()
    if ultimates is None:
        notes.append("Ultimate names unavailable (boon-data catalog couldn't load); ults skipped.")
    else:
        for row in demo.abilities.iter_rows(named=True):
            hero_id = int(row["hero_id"])
            if hero_id in by_hero and row["ability"] == ultimates.get(hero_id):
                events.append(Event("ult", int(row["tick"]), by_hero[hero_id], ability=row["ability"]))

    events.sort(key=lambda event: event.tick)
    return ParsedMatch(
        match_id=int(demo.match_id),
        tick_rate=float(demo.tick_rate),
        players=players,
        events=events,
        clock=BoonClock(demo),
        notes=notes,
    )


def ultimate_abilities() -> dict[int, str] | None:
    """Hero id to the ability in its ultimate slot, from boon-data's hero catalog."""
    try:
        from boon import data

        catalog = json.loads(data.catalog_path("heroes.json").read_text())
    except Exception:
        return None
    ultimates = {}
    for record in catalog.get("records", []):
        definition = record.get("definition", {})
        ability = (definition.get("m_mapBoundAbilities") or {}).get("ESlot_Signature_4")
        if "m_HeroID" in definition and ability:
            ultimates[int(definition["m_HeroID"])] = ability
    return ultimates
