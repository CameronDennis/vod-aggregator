import hashlib

from vodagg.footage.recovery import candidate_urls, find_vod, fix_muted_playlist, path_for


def test_path_matches_twitch_layout():
    base = "streamer_42_1700000000"
    expected = hashlib.sha1(base.encode()).hexdigest()[:20] + "_" + base
    assert path_for("Streamer", "42", 1700000000) == expected


def test_candidates_try_nearest_start_times_first():
    starts = [start for start, _ in candidate_urls("s", "1", 100, window_s=2, hosts=["h"])]
    assert starts == [100, 99, 101, 98, 102]


class FakeSession:
    def __init__(self, good_url):
        self.good_url = good_url
        self.calls = 0

    def head(self, url, timeout):
        self.calls += 1
        return type("R", (), {"status_code": 200 if url == self.good_url else 403})()


def test_find_vod_returns_the_working_playlist():
    good = next(url for start, url in candidate_urls("s", "1", 100, window_s=3) if start == 102)
    found = find_vod("s", "1", 100, window_s=3, session=FakeSession(good))
    assert found.started_at == 102
    assert found.playlist_url == good


def test_find_vod_none_when_nothing_answers():
    assert find_vod("s", "1", 100, window_s=1, session=FakeSession("nope")) is None


def test_fix_muted_playlist():
    playlist = "#EXTM3U\n#EXTINF:10,\n0.ts\n#EXTINF:10,\n1.ts\n"
    fixed = fix_muted_playlist(playlist, available={"0.ts", "1-muted.ts"})
    assert fixed == "#EXTM3U\n#EXTINF:10,\n0.ts\n#EXTINF:10,\n1-muted.ts\n"
