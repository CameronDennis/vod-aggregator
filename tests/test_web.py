import shutil
import subprocess
import time
from dataclasses import replace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from yt_dlp.utils import DownloadError  # noqa: E402

from vodagg import clips  # noqa: E402
from vodagg.config import Config  # noqa: E402
from vodagg.db import Database  # noqa: E402
from vodagg.footage.twitch import TwitchError, VodInfo  # noqa: E402
from vodagg.match import scan, service  # noqa: E402
from vodagg.web.app import create_app  # noqa: E402


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path, twitch_client_id=None, twitch_client_secret=None)


@pytest.fixture
def client(config):
    with TestClient(create_app(config), base_url="http://127.0.0.1") as test_client:
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
    assert client.post("/api/streamers", json={"name": " "}).status_code == 400
    assert client.patch(f"/api/streamers/{a['id']}", json={"name": ""}).status_code == 400


def test_match_detail_and_unlinked_players(client, config, monkeypatch):
    edboi, item = seed_match(config)
    # No stream to trim to the match here, so the player gets the whole VOD's playlist.
    monkeypatch.setattr("vodagg.web.app.download.media_playlist_url", lambda url: None)
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


def test_overlapping_event_clips_become_one(config, tmp_path):
    edboi, item = seed_match(config)
    db = Database(config.db_path)
    events = {e["tick"]: e["id"] for e in db.match_events(9)}
    # Padding of 8s before and 95s after makes each clip reach into the next: one clip from 1:32 to 6:35.
    planned = clips.plan_clips(db, 9, edboi, list(events.values()), 8, 95, tmp_path / "out")
    assert [(c.path.name, c.start, c.end, c.event_id) for c in planned] == [
        ("01-40_death+2_edboi.mp4", 1092.0, 1395.0, events[6400]),
    ]
    apart = clips.plan_clips(db, 9, edboi, list(events.values()), 8, 4, tmp_path / "out")
    assert [c.path.name for c in apart] == ["01-40_death_edboi.mp4", "03-20_kill_edboi.mp4", "05-00_ult_edboi.mp4"]


def test_windows_can_name_the_perspectives_to_cut(config, tmp_path):
    edboi, item = seed_match(config)
    db = Database(config.db_path)
    sleepy = db.find_streamer("sleepy")
    theirs = db.add_footage(sleepy.id, "vod", source_url="https://www.twitch.tv/videos/6")
    db.set_footage_sync(theirs.id, 9, offset_s=500.0, tick_rate=64.0)
    death = next(e["id"] for e in db.match_events(9) if e["tick"] == 6400)  # sleepy isn't in it
    window = clips.Window(6400 - 512, 6400 + 256, death)
    assert [c.streamer for c in clips.plan_windows(db, 9, edboi, [window], tmp_path)] == ["edboi"]
    chosen = replace(window, streamer_ids=(sleepy.id,))
    assert [c.streamer for c in clips.plan_windows(db, 9, edboi, [chosen], tmp_path)] == ["edboi", "sleepy"]
    assert [c.streamer for c in clips.plan_windows(db, 9, edboi, [replace(window, streamer_ids=())], tmp_path)] == ["edboi"]


def test_merge_overlapping_chains_windows():
    windows = [clips.Window(0, 10, 1), clips.Window(30, 40, 4), clips.Window(5, 15, 2), clips.Window(14, 20, 3)]
    assert clips.merge_overlapping(windows) == [clips.Window(0, 20, 1, more_event_ids=(2, 3)), clips.Window(30, 40, 4)]


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


def test_requests_for_other_hosts_are_refused(client):
    assert client.get("/api/streamers", headers={"host": "evil.example"}).status_code == 400
    assert client.get("/api/streamers", headers={"host": "localhost:8000"}).status_code == 200


def test_pages_are_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "app.js" in page.text
    assert client.get("/app.js").status_code == 200


def test_replay_upload_needs_an_unsynced_match(client):
    response = client.post("/api/footage/1/unsynced/123/demo", content=b"PBDEMS2")
    assert response.status_code == 404


def test_replay_upload_is_saved_and_starts_a_sync(client, config):
    _, item = seed_match(config)
    db = Database(config.db_path)
    db.set_unsynced_match(item.id, 77, 10.0, 20.0, "no replay")
    db.close()
    response = client.post(f"/api/footage/{item.id}/unsynced/77/demo", content=b"PBDEMS2" * 1000)
    assert response.status_code == 200 and response.json()["kind"] == "sync"


def test_adding_footage_from_a_match_needs_a_known_streamer(client):
    assert client.post("/api/footage/match", json={"match_id": 1, "streamer_id": 999}).status_code == 404


def test_clips_in_the_same_second_get_their_own_files(config, tmp_path):
    edboi, item = seed_match(config)
    db = Database(config.db_path)
    db.set_match_events(9, [(12800, 200.0, "kill", 11, 2, (), None), (12830, 200.4, "kill", 11, 3, (), None)])
    # Clipped one at a time (as review allows), not merged into one clip.
    windows = [clips.Window(e["tick"] - 512, e["tick"] + 256, e["id"]) for e in db.match_events(9)]
    planned = clips.plan_windows(db, 9, edboi, windows, tmp_path / "out")
    assert [c.path.name for c in planned] == ["03-20_kill_edboi.mp4", "03-20_kill_edboi_2.mp4"]


def test_adding_a_vod_twice_keeps_one_footage(client, monkeypatch):
    class FakeClient:
        def get_vod(self, video_id):
            return VodInfo(video_id, "newguy", "t", 1700000000, 3600, "archive")

    monkeypatch.setattr("vodagg.web.app.twitch.make_client", lambda *a: FakeClient())
    first = client.post("/api/footage/vod", json={"video": "77", "scan": False}).json()
    again = client.post("/api/footage/vod", json={"video": "77", "scan": False}).json()
    assert again["footage"]["id"] == first["footage"]["id"]
    assert len(client.get("/api/footage").json()) == 1


def test_twitch_refusals_come_back_as_messages(client, config, monkeypatch):
    _, item = seed_match(config)

    def gone(url):
        raise DownloadError("ERROR: [twitch:vod] 5: Video 5 does not exist")

    monkeypatch.setattr("vodagg.web.app.download.media_playlist_url", gone)
    response = client.get(f"/api/footage/{item.id}/hls/index.m3u8")
    assert response.status_code == 502 and response.json()["detail"] == "[twitch:vod] 5: Video 5 does not exist"

    class FakeClient:
        def get_vod(self, video_id):
            raise TwitchError("video 77 not found (deleted, private or sub-only)")

    monkeypatch.setattr("vodagg.web.app.twitch.make_client", lambda *a: FakeClient())
    response = client.post("/api/footage/vod", json={"video": "77", "scan": False})
    assert response.status_code == 502 and "not found" in response.json()["detail"]


def wait_for(client, job):
    for _ in range(100):
        job = client.get(f"/api/jobs/{job['id']}").json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job['id']} didn't finish")


def test_a_match_whose_clock_cant_be_read_is_a_clock_failure(client, config, monkeypatch):
    _, item = seed_match(config)

    class Source:
        duration = 100.0

        def times(self, start, end, step):
            return [10.0, 40.0]

    monkeypatch.setattr(service, "frame_source", lambda item: Source())
    monkeypatch.setattr(scan, "scan", lambda *a, **k: [scan.Reading(10.0, 77, None), scan.Reading(40.0, 77, None)])
    monkeypatch.setattr(service, "sync_span", lambda *a, **k: service.SyncOutcome(77, error="no clock", match=object()))
    job = wait_for(client, client.post(f"/api/footage/{item.id}/sync", json={}).json())
    assert job["status"] == "done"
    assert [(m["match_id"], m["kind"]) for m in client.get(f"/api/footage/{item.id}/unsynced").json()] == [(77, "clock")]


def test_syncing_by_hand_uses_the_frame_where_the_clock_turns_over(client, config, monkeypatch):
    _, item = seed_match(config)
    db = Database(config.db_path)
    db.upsert_match(77, tick_rate=64.0)  # Recorded when its clock failed.
    db.set_unsynced_match(item.id, 77, 10.0, 20.0, "no clock", "clock")
    db.close()

    class Clock:
        tick_rate = 64.0
        first_tick = 0
        last_tick = 64 * 1000

        def match_seconds(self, tick):
            return (tick - 640) / 64

    parsed = type("Parsed", (), {"clock": Clock(), "tick_rate": 64.0})()
    monkeypatch.setattr("vodagg.web.app.demo.download_demo", lambda match_id, demo_dir: demo_dir / "77.dem")
    monkeypatch.setattr("vodagg.web.app.events.parse", lambda path: parsed)
    monkeypatch.setattr(service, "record_match", lambda *a: [])
    response = client.post(f"/api/footage/{item.id}/unsynced/77/manual", json={"t": 300.0, "clock_s": 60})
    job = wait_for(client, response.json())
    assert job["status"] == "done", job["error"]
    synced = [m for m in client.get(f"/api/footage/{item.id}/matches").json() if m["match_id"] == 77]
    assert synced[0]["offset_s"] == 300.0 - (640 + 60 * 64) / 64 and synced[0]["readings"] == 0
    assert client.get(f"/api/footage/{item.id}/unsynced").json() == []


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_frames_and_clock_regions(client, config, tmp_path):
    video = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30", "-t", "3",
                    "-c:v", "libx264", str(video)], check=True)  # fmt: skip
    streamer = client.post("/api/streamers", json={"name": "A"}).json()
    item = client.post("/api/footage/import", json={"streamer": "A", "path": str(video)}).json()
    frame = client.get(f"/api/footage/{item['id']}/frame", params={"t": 1.5})
    assert frame.status_code == 200 and frame.content.startswith(b"\x89PNG")
    box = {"left": 0.4, "top": 0.0, "width": 0.2, "height": 0.1, "t": 1.5}
    saved = client.put(f"/api/footage/{item['id']}/clock-region", json=box)
    assert saved.status_code == 200 and saved.json()["clock_region"] == [0.4, 0.0, 0.2, 0.1]
    footage = next(f for f in client.get("/api/footage").json() if f["id"] == item["id"])
    assert footage["clock_region"] == [0.4, 0.0, 0.2, 0.1] and footage["streamer_id"] == streamer["id"]
    outside = client.put(f"/api/footage/{item['id']}/clock-region", json={**box, "left": 0.9})
    assert outside.status_code == 400
