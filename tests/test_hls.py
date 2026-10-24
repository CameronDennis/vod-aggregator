import pytest

from vodagg.footage.hls import parse_playlist

PLAYLIST = """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXT-X-MAP:URI="init-0.mp4"
#EXT-X-PROGRAM-DATE-TIME:2026-10-16T19:38:11.990Z
#EXTINF:10.000,
0-muted.mp4
#EXT-X-PROGRAM-DATE-TIME:2026-10-16T19:38:21.990Z
#EXTINF:10.000,
1.mp4
#EXT-X-MAP:URI="init-1.mp4"
#EXTINF:4.500,
2.mp4
#EXT-X-ENDLIST
"""


def test_segments_get_offsets_init_sections_and_absolute_urls():
    playlist = parse_playlist(PLAYLIST, "https://host/abc/1080p60/index.m3u8")
    starts = [s.start for s in playlist.segments]
    assert starts == [0.0, 10.0, 20.0]
    assert playlist.duration == 24.5
    assert playlist.segments[0].url == "https://host/abc/1080p60/0-muted.mp4"
    assert playlist.segments[1].init_url == "https://host/abc/1080p60/init-0.mp4"
    assert playlist.segments[2].init_url == "https://host/abc/1080p60/init-1.mp4"
    assert playlist.segments[1].program_time == pytest.approx(1792179501.99)
    assert playlist.segments[2].program_time is None


def test_overlapping_and_at_select_only_needed_segments():
    playlist = parse_playlist(PLAYLIST, "https://host/x.m3u8")
    assert [s.start for s in playlist.overlapping(9, 12)] == [0.0, 10.0]
    assert [s.start for s in playlist.overlapping(10, 20)] == [10.0]
    assert playlist.at(21).start == 20.0
    with pytest.raises(ValueError):
        playlist.at(30)


def test_master_playlists_are_rejected():
    with pytest.raises(ValueError):
        parse_playlist("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nlow.m3u8\n", "https://h/m.m3u8")


def test_muted_segments_are_fetched_under_their_new_names(tmp_path):
    from vodagg.footage.hls import Segment, _download_joined

    class Response:
        def __init__(self, status, content=b""):
            self.status_code, self.content, self.ok = status, content, status == 200

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError(self.status_code)

    files = {"https://h/init.mp4": b"I", "https://h/0-muted.mp4": b"A", "https://h/1.mp4": b"B"}

    class Session:
        def get(self, url, timeout):
            return Response(200, files[url]) if url in files else Response(403)

    segments = [Segment("https://h/0.mp4", 0, 10, "https://h/init.mp4"), Segment("https://h/1.mp4", 10, 10, "https://h/init.mp4")]
    out = tmp_path / "joined.mp4"
    _download_joined(segments, out, Session(), workers=2)
    assert out.read_bytes() == b"IAB"
