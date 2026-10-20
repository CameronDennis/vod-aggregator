import sqlite3

import pytest

from vodagg.db import Database


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


def test_find_streamer_by_any_key(db):
    added = db.add_streamer("Mike", twitch_login="mikeplays", steam_account_id=1234)
    assert db.find_streamer("mike") == added
    assert db.find_streamer("MikePlays") == added
    assert db.find_streamer(1234) == added
    assert db.find_streamer("1234") == added
    assert db.find_streamer("nobody") is None


def test_duplicate_twitch_login_rejected(db):
    db.add_streamer("A", twitch_login="same")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_streamer("B", twitch_login="same")


def test_update_streamer(db):
    added = db.add_streamer("A")
    updated = db.update_streamer(added.id, steam_account_id=99)
    assert updated.steam_account_id == 99
    with pytest.raises(ValueError):
        db.update_streamer(added.id, bogus=1)


def test_streamers_in_match_cross_checks_steam_ids(db):
    a = db.add_streamer("A", steam_account_id=1)
    b = db.add_streamer("B", steam_account_id=2)
    db.add_streamer("C", steam_account_id=3)
    db.upsert_match(555, start_time=1000)
    db.set_match_players(555, [(1, 10, 0), (2, 11, 1), (77, 12, 1)])
    assert db.streamers_in_match(555) == [a, b]


def test_upsert_match_keeps_known_fields(db):
    db.upsert_match(1, start_time=100, duration_s=1800)
    db.upsert_match(1, demo_path="x.dem")
    row = db.conn.execute("SELECT * FROM matches WHERE match_id = 1").fetchone()
    assert (row["start_time"], row["duration_s"], row["demo_path"]) == (100, 1800, "x.dem")


def test_footage_covering(db):
    s = db.add_streamer("A")
    vod = db.add_footage(s.id, "vod", started_at=1000, duration_s=600)
    db.add_footage(s.id, "local")  # no timing: never matches
    assert db.footage_covering(s.id, 1300) == [vod]
    assert db.footage_covering(s.id, 1700) == []


def test_removing_streamer_removes_footage(db):
    s = db.add_streamer("A")
    db.add_footage(s.id, "vod")
    db.remove_streamer(s.id)
    assert db.list_footage() == []
