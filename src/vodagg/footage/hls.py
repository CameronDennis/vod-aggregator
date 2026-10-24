"""Fetching parts of an HLS playlist without reading the whole stream.

Twitch now serves VODs as fragmented MP4 segments, and ffmpeg (which yt-dlp
hands section downloads to) reads those from the first segment onward, so
cutting a minute out of hour three of a VOD downloads three hours of video.
Here the playlist is read directly, only the segments that overlap the wanted
range are fetched, and ffmpeg trims the result.

The same segment lookup is what lets single frames be grabbed anywhere in a
VOD cheaply, which match-clock reading depends on.
"""

from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import requests

from .. import ffmpeg
from ..timeutil import parse_timestamp


@dataclass(frozen=True)
class Segment:
    url: str
    start: float
    duration: float
    init_url: str | None = None
    program_time: float | None = None

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass(frozen=True)
class MediaPlaylist:
    url: str
    segments: tuple[Segment, ...]

    @property
    def duration(self) -> float:
        return self.segments[-1].end if self.segments else 0.0

    def overlapping(self, start: float, end: float) -> list[Segment]:
        return [s for s in self.segments if s.end > start and s.start < end]

    def at(self, t: float) -> Segment:
        for segment in self.segments:
            if segment.start <= t < segment.end:
                return segment
        raise ValueError(f"{t:.1f}s is outside the playlist (0-{self.duration:.1f}s)")


def parse_playlist(text: str, url: str) -> MediaPlaylist:
    """Read a media playlist's segments, start offsets and init sections."""
    segments: list[Segment] = []
    position = 0.0
    init_url = None
    program_time = None
    duration = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MAP:"):
            attributes = _attributes(line[len("#EXT-X-MAP:") :])
            init_url = urljoin(url, attributes["URI"])
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            program_time = parse_timestamp(line.split(":", 1)[1], precise=True)
        elif line.startswith("#EXTINF:"):
            duration = float(line[len("#EXTINF:") :].split(",")[0])
        elif line.startswith("#EXT-X-STREAM-INF"):
            raise ValueError("this is a master playlist; pass one of its media playlists")
        elif not line.startswith("#") and duration is not None:
            segments.append(Segment(urljoin(url, line), position, duration, init_url, program_time))
            position += duration
            duration = None
            program_time = None
    return MediaPlaylist(url, tuple(segments))


def load_playlist(url: str, session: requests.Session | None = None) -> MediaPlaylist:
    response = (session or requests).get(url, timeout=20)
    response.raise_for_status()
    return parse_playlist(response.text, url)


def trim_playlist(text: str, start: float, end: float) -> tuple[str, float]:
    """A media playlist cut down to the segments overlapping [start, end) seconds,
    and the time in the original at which the cut one begins (its 0:00).

    A player given it only ever loads, and only lets you seek within, that stretch.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    first_tag = next((i for i, line in enumerate(lines)
                      if line.startswith(("#EXTINF", "#EXT-X-PROGRAM-DATE-TIME", "#EXT-X-DISCONTINUITY"))), len(lines))  # fmt: skip
    header, rest = lines[:first_tag], lines[first_tag:]
    # Each segment is the tags since the previous one, then its URI.
    blocks: list[list[str]] = []
    block: list[str] = []
    for line in rest:
        block.append(line)
        if not line.startswith("#"):
            blocks.append(block)
            block = []
    trailer = block
    kept: list[list[str]] = []
    kept_start = None
    position = 0.0
    map_line = next((line for line in header if line.startswith("#EXT-X-MAP:")), None)
    for index, segment in enumerate(blocks):
        duration = next(float(line[len("#EXTINF:"):].split(",")[0]) for line in segment if line.startswith("#EXTINF:"))
        maps = [line for line in segment if line.startswith("#EXT-X-MAP:")]
        if position + duration > start and position < end:
            if kept_start is None:
                kept_start = position
                first_index = index
                if map_line and not maps and map_line not in header:
                    segment = [map_line, *segment]
            kept.append(segment)
        map_line = maps[-1] if maps else map_line
        position += duration
    if not kept:
        return text, 0.0
    sequence = "#EXT-X-MEDIA-SEQUENCE:"
    header = [f"{sequence}{int(line[len(sequence):]) + first_index}" if line.startswith(sequence) else line
              for line in header]  # fmt: skip
    return "\n".join([*header, *(line for segment in kept for line in segment), *trailer]) + "\n", kept_start


def fetch_section(
    playlist: MediaPlaylist,
    start: float,
    end: float,
    out_path: Path,
    session: requests.Session | None = None,
    workers: int = 8,
    exact: bool = False,
) -> Path:
    """Save the [start, end) seconds of a playlist to an MP4 file.

    By default the cut is stream-copied, which is fast and lossless but can
    leave the video starting at the first keyframe after `start` while the
    audio starts at `start`, out of step. With `exact` it is re-encoded so
    both start exactly at `start`, which is what clips need.
    """
    segments = playlist.overlapping(start, end)
    if not segments:
        raise ValueError(f"no segments between {start:.1f}s and {end:.1f}s")
    session = session or requests.Session()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        joined = Path(tmp) / ("joined" + _suffix(segments[0].url))
        _download_joined(segments, joined, session, workers)
        seek = ("-ss", f"{start - segments[0].start:.3f}")
        # An exact cut decodes the joined segments from their start and seeks on the output:
        # seeking the input lands MPEG-TS segments between keyframes, which froze the first second.
        ffmpeg.run(
            *(() if exact else seek),
            "-i", str(joined),
            *(seek if exact else ()),
            "-t", f"{end - start:.3f}",
            *(EXACT_CODECS if exact else ("-c", "copy", "-avoid_negative_ts", "make_zero")),
            "-movflags", "+faststart",
            str(out_path),
        )  # fmt: skip
    return out_path


EXACT_CODECS = ("-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k")
"""Re-encoding settings for cuts that must start exactly where asked, audio and video together."""


def grab_keyframe(
    segment: Segment,
    out_path: Path,
    session: requests.Session | None = None,
    head_bytes: int = 1_000_000,
) -> Path:
    """Save the first frame of a segment, fetching only the start of it.

    Every segment opens on a keyframe, so its first megabyte or so is enough
    to decode one frame. This makes sampling a long VOD every few seconds
    cheap. Falls back to the whole segment if the partial read won't decode.
    """
    session = session or requests.Session()
    init = _get(session, segment.init_url) if segment.init_url else b""
    response = session.get(segment.url, headers={"Range": f"bytes=0-{head_bytes - 1}"}, timeout=60)
    response.raise_for_status()
    with tempfile.TemporaryDirectory() as tmp:
        partial = Path(tmp) / ("segment" + _suffix(segment.url))
        partial.write_bytes(init + response.content)
        try:
            ffmpeg.run("-i", str(partial), "-frames:v", "1", str(out_path))
        except RuntimeError:
            _download_joined([segment], partial, session, 1)
            ffmpeg.run("-i", str(partial), "-frames:v", "1", str(out_path))
    return out_path


_init_cache: dict[str, bytes] = {}


def _get(session: requests.Session, url: str) -> bytes:
    """Fetch a small, unchanging file such as an init section, once per process."""
    if url not in _init_cache:
        response = session.get(url, timeout=60)
        response.raise_for_status()
        _init_cache[url] = response.content
    return _init_cache[url]


def _download_joined(segments: list[Segment], out: Path, session: requests.Session, workers: int) -> None:
    """Concatenate segments (with the init section before each change of it) into one file."""

    def get(url: str) -> bytes:
        response = get_segment(session, url)
        response.raise_for_status()
        return response.content

    parts: list[str] = []
    current_init = None
    for segment in segments:
        if segment.init_url and segment.init_url != current_init:
            parts.append(segment.init_url)
            current_init = segment.init_url
        parts.append(segment.url)
    with ThreadPoolExecutor(max_workers=workers) as pool, out.open("wb") as handle:
        for data in pool.map(get, parts):
            handle.write(data)


def get_segment(session: requests.Session, url: str) -> requests.Response:
    """Fetch a segment, under its muted name if the playlist's name is refused.

    Recovered playlists keep the original names of segments Twitch later
    muted ("N.ts" became "N-muted.ts").
    """
    response = session.get(url, timeout=60)
    if response.status_code in (403, 404):
        for variant in _muted_variants(url):
            retry = session.get(variant, timeout=60)
            if retry.ok:
                return retry
    return response


def _muted_variants(url: str) -> list[str]:
    base, dot, ext = url.rpartition(".")
    if not dot or base.endswith(("-muted", "-unmuted")):
        return []
    return [f"{base}-muted.{ext}", f"{base}-unmuted.{ext}"]


def _suffix(url: str) -> str:
    name = url.split("?")[0].rsplit("/", 1)[-1]
    return ".mp4" if name.endswith((".mp4", ".m4s")) else ".ts"


def _attributes(text: str) -> dict[str, str]:
    attributes = {}
    for part in text.split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            attributes[key.strip()] = value.strip().strip('"')
    return attributes
