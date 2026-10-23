import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from vodagg import clips  # noqa: E402
from vodagg.config import Config  # noqa: E402
from vodagg.db import Database  # noqa: E402
from vodagg.web.app import create_app  # noqa: E402


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path, twitch_client_id=None, twitch_client_secret=None)


@pytest.fixture
def client(config):
    with TestClient(create_app(config)) as test_client:
        yield test_client


def seed_match(config):
    """Edboi (two accounts) synced to match 9; asd fights them; sleepy is a streamer with no footage."""
    db = Database(config.db_path)
    edboi = db.add_streamer("edboi", steam_account_ids=(1, 11))
    db.add_streamer("sleepy", steam_account_ids=(3,))
    item = db.add_footage(edboi.id, "vod", source_url="https://www.twitch.tv/videos/5")
    db.upsert_match(9, duration_s=600, tick_rate=64.0)
    db.set_match_players(9, [(11, 84, 2, "EdBoi", "Rat King"), (2, 58, 3, "asd", "Vyper"), (3, 78, 3, "sleepy", "Danny")])
    db.set_match_events(
        9,
        [
            (6400, 100.0, "kill", 2, 11, (), None),
            (12800, 200.0, "kill", 11, 2, (3,), None),
            (19200, 300.0, "ult", 11, None, (), "ability_x"),
        ],
    )
    db.set_footage_sync(item.id, 9, offset_s=1000.0, tick_rate=64.0, uncertainty_s=0.1, readings=50)
    db.close()
    return edboi, item


def test_streamer_accounts_api(client):
    a = client.post("/api/streamers", json={"name": "A", "steam_account_ids": [1]}).json()
    b = client.post("/api/streamers", json={"name": "B"}).json()
    assert client.post(f"/api/streamers/{a['id']}/accounts", json={"account_id": 2}).json()["steam_account_ids"] == [1, 2]
    taken = client.post(f"/api/streamers/{b['id']}/accounts", json={"account_id": 1})
    assert taken.status_code == 409 and "already belongs to A" in taken.json()["detail"]
    assert client.post(f"/api/streamers/{a['id']}/accounts", json={"account_id": 0}).status_code == 422
    assert client.delete(f"/api/streamers/{a['id']}/accounts/1").json()["steam_account_ids"] == [2]
    assert client.post("/api/streamers", json={"name": "a"}).status_code == 409
    assert client.patch("/api/streamers/999", json={"notes": "x"}).status_code == 404


def test_match_detail_and_unlinked_players(client, config):
    edboi, item = seed_match(config)
    detail = client.get("/api/matches/9").json()
    by_id = {p["account_id"]: p for p in detail["players"]}
    assert by_id[11]["streamer"] == "edboi" and by_id[2]["streamer"] is None
    assert [p["footage_id"] for p in detail["perspectives"]] == [item.id]
    assert detail["perspectives"][0]["video"] == f"/api/footage/{item.id}/hls/index.m3u8"
    assert [m["match_id"] for m in client.get(f"/api/footage/{item.id}/matches").json()] == [9]
    unlinked = client.get("/api/players/unlinked").json()
    assert [(p["player_name"], p["matches"], p["played_with"]) for p in unlinked] == [("asd", 1, ["edboi", "sleepy"])]


def test_plan_clips_cuts_each_event_from_synced_footage(config, tmp_path):
    edboi, item = seed_match(config)
    db = Database(config.db_path)
    events = {e["tick"]: e["id"] for e in db.match_events(9)}
    planned = clips.plan_clips(db, 9, edboi, [events[6400], events[12800]], 8, 4, tmp_path / "out")
    assert [(c.path.name, c.start, c.end) for c in planned] == [
        ("01-40_death_edboi.mp4", 1092.0, 1104.0),
        ("03-20_kill_edboi.mp4", 1192.0, 1204.0),
    ]
    # sleepy assisted, but has no synced footage, so there is nothing more to cut.
    assert all(c.streamer == "edboi" for c in planned)


def test_plan_windows_cuts_exact_stretches(config, tmp_path):
    edboi, item = seed_match(config)
    db = Database(config.db_path)
    events = {e["tick"]: e["id"] for e in db.match_events(9)}
    windows = [clips.Window(6400 - 64 * 3, 6400 + 64 * 10, events[6400]), clips.Window(640, 1280, None, "00-10_clip")]
    planned = clips.plan_windows(db, 9, edboi, windows, tmp_path / "out")
    assert [(c.path.name, c.start, c.end) for c in planned] == [
        ("01-40_death_edboi.mp4", 1097.0, 1110.0),
        ("00-10_clip_edboi.mp4", 1010.0, 1020.0),
    ]


def test_export_windows_must_end_after_they_start(client, config):
    edboi, _ = seed_match(config)
    window = {"start_tick": 1280, "end_tick": 640}
    response = client.post("/api/exports", json={"match_id": 9, "streamer_id": edboi.id, "windows": [window]})
    assert response.status_code == 400


def test_export_without_synced_footage_is_refused(client, config):
    seed_match(config)
    sleepy = next(s for s in client.get("/api/streamers").json() if s["name"] == "sleepy")
    response = client.post("/api/exports", json={"match_id": 9, "streamer_id": sleepy["id"], "event_ids": [999]})
    assert response.status_code == 400


def test_segment_names_and_folders_are_checked(client, config, tmp_path):
    _, item = seed_match(config)
    assert client.get(f"/api/footage/{item.id}/hls/..%2Fsecret").status_code in (400, 404)
    outside = client.post("/api/open-folder", json={"path": str(tmp_path.parent)})
    assert outside.status_code == 400
    gone = client.post("/api/open-folder", json={"path": str(config.data_dir / "exports" / "9")})
    assert gone.status_code == 404 and "doesn't exist" in gone.json()["detail"]


def test_pages_are_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "app.js" in page.text
    assert client.get("/app.js").status_code == 200


def test_replay_upload_needs_an_unsynced_match(client):
    response = client.post("/api/footage/1/unsynced/123/demo", content=b"PBDEMS2")
    assert response.status_code == 404


def test_adding_footage_from_a_match_needs_a_known_streamer(client):
    assert client.post("/api/footage/match", json={"match_id": 1, "streamer_id": 999}).status_code == 404


def test_clips_in_the_same_second_get_their_own_files(config, tmp_path):
    edboi, item = seed_match(config)
    db = Database(config.db_path)
    db.set_match_events(9, [(12800, 200.0, "kill", 11, 2, (), None), (12830, 200.4, "kill", 11, 3, (), None)])
    planned = clips.plan_clips(db, 9, edboi, [e["id"] for e in db.match_events(9)], 8, 4, tmp_path / "out")
    assert [c.path.name for c in planned] == ["03-20_kill_edboi.mp4", "03-20_kill_edboi_2.mp4"]
