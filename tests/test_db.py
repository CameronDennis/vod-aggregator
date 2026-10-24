import sqlite3

import pytest

from vodagg.db import Database


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


def test_find_streamer_by_any_key(db):
    added = db.add_streamer("Mike", twitch_login="mikeplays", steam_account_ids=(1234,))
    assert db.find_streamer("mike") == added
    assert db.find_streamer("MikePlays") == added
    assert db.find_streamer(1234) == added
    assert db.find_streamer("1234") == added
    assert db.find_streamer("nobody") is None


def test_find_streamer_prefers_a_name_to_a_steam_id(db):
    numbered = db.add_streamer("Other", steam_account_ids=(123,))
    named = db.add_streamer("123")
    assert db.find_streamer("123") == named
    assert db.find_streamer(123) == named
    assert db.find_streamer("Other") == numbered


def test_duplicate_twitch_login_rejected(db):
    db.add_streamer("A", twitch_login="same")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_streamer("B", twitch_login="same")


def test_update_streamer(db):
    added = db.add_streamer("A")
    updated = db.update_streamer(added.id, notes="hi")
    assert updated.notes == "hi"
    with pytest.raises(ValueError):
        db.update_streamer(added.id, bogus=1)


def test_streamer_with_several_steam_accounts(db):
    added = db.add_streamer("A", steam_account_ids=(1,))
    assert db.add_steam_account(added.id, 2).steam_account_ids == (1, 2)
    assert db.add_steam_account(added.id, 2).steam_account_ids == (1, 2)  # Already theirs: no change.
    assert db.find_streamer(2).id == added.id
    assert db.remove_steam_account(added.id, 1).steam_account_ids == (2,)
    assert db.find_streamer(1) is None


def test_steam_account_belongs_to_one_streamer(db):
    db.add_streamer("A", steam_account_ids=(1,))
    b = db.add_streamer("B")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_steam_account(b.id, 1)


def test_old_steam_id_column_is_moved(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE streamers (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,"
        " twitch_login TEXT UNIQUE COLLATE NOCASE, steam_account_id INTEGER UNIQUE, notes TEXT,"
        " created_at INTEGER NOT NULL)"
    )
    old.execute("INSERT INTO streamers (name, steam_account_id, created_at) VALUES ('A', 7, 0)")
    old.commit()
    old.close()
    database = Database(path)
    assert database.find_streamer("A").steam_account_ids == (7,)
    database.remove_steam_account(database.find_streamer("A").id, 7)
    database.close()
    assert Database(path).find_streamer("A").steam_account_ids == ()  # Not copied back on reopen.


def test_streamers_in_match_cross_checks_steam_ids(db):
    a = db.add_streamer("A", steam_account_ids=(1,))
    b = db.add_streamer("B", steam_account_ids=(2, 20))
    db.add_streamer("C", steam_account_ids=(3,))
    db.upsert_match(555, start_time=1000)
    db.set_match_players(555, [(1, 10, 0), (20, 11, 1), (77, 12, 1)])
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


def test_syncing_a_match_takes_it_off_the_unsynced_list(db):
    s = db.add_streamer("A")
    vod = db.add_footage(s.id, "vod")
    db.set_unsynced_match(vod.id, 7, 300.0, 2100.0, "no replay")
    db.set_unsynced_match(vod.id, 8, 2400.0, 4200.0, "no replay")
    assert [m["match_id"] for m in db.unsynced_matches(vod.id)] == [7, 8]
    db.upsert_match(7)
    db.set_footage_sync(vod.id, 7, 250.0, 64.0)
    assert [m["match_id"] for m in db.unsynced_matches(vod.id)] == [8]


def test_renaming_footage(db):
    s = db.add_streamer("A")
    vod = db.add_footage(s.id, "vod")
    assert db.rename_footage(vod.id, "Scrims night").name == "Scrims night"
    assert db.rename_footage(vod.id, "").name is None


def test_match_keeps_its_start_tick_and_pauses(db):
    db.upsert_match(1, start_tick=640, pauses=[(1000, 1063), (5000, 5127)])
    db.upsert_match(1, demo_path="x.dem")
    match = db.get_match(1)
    assert match["start_tick"] == 640 and match["pauses"] == [[1000, 1063], [5000, 5127]]
    db.upsert_match(2)
    assert db.get_match(2)["pauses"] is None
