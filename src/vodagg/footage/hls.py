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

import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import requests

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


def fetch_section(
    playlist: MediaPlaylist,
    start: float,
    end: float,
    out_path: Path,
    session: requests.Session | None = None,
    workers: int = 8,
) -> Path:
    """Save the [start, end) seconds of a playlist to an MP4 file.

    The cut is stream-copied from the nearest keyframe at or before `start`,
    so it may begin up to a segment early. That keeps it fast and lossless.
    """
    segments = playlist.overlapping(start, end)
    if not segments:
        raise ValueError(f"no segments between {start:.1f}s and {end:.1f}s")
    session = session or requests.Session()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        joined = Path(tmp) / ("joined" + _suffix(segments[0].url))
        _download_joined(segments, joined, session, workers)
        _ffmpeg(
            "-ss", f"{start - segments[0].start:.3f}",
            "-i", str(joined),
            "-t", f"{end - start:.3f}",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            str(out_path),
        )  # fmt: skip
    return out_path


def grab_frame(
    playlist: MediaPlaylist,
    t: float,
    out_path: Path,
    session: requests.Session | None = None,
) -> Path:
    """Save the frame shown at `t` seconds as an image."""
    segment = playlist.at(t)
    session = session or requests.Session()
    with tempfile.TemporaryDirectory() as tmp:
        joined = Path(tmp) / ("segment" + _suffix(segment.url))
        _download_joined([segment], joined, session, 1)
        _ffmpeg("-ss", f"{t - segment.start:.3f}", "-i", str(joined), "-frames:v", "1", str(out_path))
    return out_path


def _download_joined(segments: list[Segment], out: Path, session: requests.Session, workers: int) -> None:
    """Concatenate segments (with the init section before each change of it) into one file."""

    def get(url: str) -> bytes:
        response = session.get(url, timeout=60)
        if response.status_code in (403, 404):
            # Recovered playlists keep the original names of segments Twitch
            # later muted ("N.ts" became "N-muted.ts").
            for variant in _muted_variants(url):
                retry = session.get(variant, timeout=60)
                if retry.ok:
                    return retry.content
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


def _ffmpeg(*args: str) -> None:
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise RuntimeError("ffmpeg is not installed")
    result = subprocess.run(
        [executable, "-y", "-hide_banner", "-loglevel", "error", *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()}")


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
