from click.testing import CliRunner

from vodagg.cli import main
from vodagg.footage.twitch import VodInfo


def run(tmp_path, monkeypatch, *args):
    monkeypatch.setenv("VODAGG_DATA_DIR", str(tmp_path))
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def test_streamer_add_and_list(tmp_path, monkeypatch):
    assert run(tmp_path, monkeypatch, "streamer", "add", "Mike", "--steam", "42").exit_code == 0
    result = run(tmp_path, monkeypatch, "streamer", "list")
    assert "Mike" in result.output and "mike" in result.output and "42" in result.output


def test_streamer_update_adds_and_removes_steam_ids(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, "streamer", "add", "Mike", "--steam", "42")
    run(tmp_path, monkeypatch, "streamer", "add", "Other", "--steam", "7")
    assert run(tmp_path, monkeypatch, "streamer", "update", "Mike", "--steam", "43").exit_code == 0
    assert "42, 43" in run(tmp_path, monkeypatch, "streamer", "list").output
    taken = run(tmp_path, monkeypatch, "streamer", "update", "Mike", "--steam", "7")
    assert taken.exit_code != 0 and "already belongs to Other" in taken.output
    run(tmp_path, monkeypatch, "streamer", "update", "43", "--remove-steam", "42")
    assert "42" not in run(tmp_path, monkeypatch, "streamer", "list").output


def test_vod_add_creates_streamer_and_footage(tmp_path, monkeypatch):
    class FakeClient:
        def get_vod(self, video_id):
            return VodInfo(video_id, "newguy", "t", 1700000000, 3600, "archive")

    monkeypatch.setattr("vodagg.cli.twitch.make_client", lambda *a: FakeClient())
    result = run(tmp_path, monkeypatch, "vod", "add", "https://www.twitch.tv/videos/77")
    assert "Added streamer newguy" in result.output
    listing = run(tmp_path, monkeypatch, "footage", "list", "newguy")
    assert "2023-11-14 22:13:20Z" in listing.output and "1:00:00" in listing.output
    again = CliRunner().invoke(main, ["vod", "add", "77"])
    assert again.exit_code != 0 and "already footage 1" in again.output


def test_footage_import(tmp_path, monkeypatch):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")
    run(tmp_path, monkeypatch, "streamer", "add", "Mike")
    result = run(tmp_path, monkeypatch, "footage", "import", "Mike", str(video), "--started", "1700000000")
    assert result.exit_code == 0
    assert "local" in run(tmp_path, monkeypatch, "footage", "list").output
