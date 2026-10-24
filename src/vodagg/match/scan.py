"""Sampling footage for the match id and game clock.

A frame is read every few seconds of footage. Each reading pairs a footage
time with whatever OCR could read at that moment: the match id, the game
clock, both or neither (menus, the hero select screen, a webcam-only scene).
Readings are then grouped into the matches the footage shows.
"""

from __future__ import annotations

import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Protocol

import requests
from PIL import Image

from .. import ffmpeg
from ..footage import hls
from . import ocr


@dataclass(frozen=True)
class Reading:
    t: float
    match_id: int | None
    clock_s: int | None


@dataclass(frozen=True)
class MatchSpan:
    """Where in the footage one match was seen, with its clock readings."""

    match_id: int
    first_t: float
    last_t: float
    readings: tuple[Reading, ...]

    @property
    def clock_readings(self) -> list[tuple[float, int]]:
        return [(r.t, r.clock_s) for r in self.readings if r.clock_s is not None]


class FrameSource(Protocol):
    duration: float

    def times(self, start: float, end: float, step: float) -> list[float]:
        """Sample times in [start, end], at least `step` apart, that frame() can show exactly."""
        ...

    def frame(self, t: float) -> Image.Image: ...


class HlsFrames:
    """Frames from an HLS playlist, read from the keyframe opening each segment."""

    def __init__(self, playlist: hls.MediaPlaylist):
        self.playlist = playlist
        self.duration = playlist.duration
        self.session = requests.Session()

    def times(self, start: float, end: float, step: float) -> list[float]:
        times: list[float] = []
        for segment in self.playlist.segments:
            if start <= segment.start <= end and (not times or segment.start - times[-1] >= step - 1e-6):
                times.append(segment.start)
        return times

    def frame(self, t: float) -> Image.Image:
        with tempfile.TemporaryDirectory() as tmp:
            path = hls.grab_keyframe(self.playlist.at(t), Path(tmp) / "frame.png", self.session)
            return Image.open(path).copy()

    def dense_frames(self, t: float, fps: float) -> list[tuple[float, Image.Image]]:
        """Every frame at `fps` across the segment containing `t`."""
        segment = self.playlist.at(t)
        with tempfile.TemporaryDirectory() as tmp:
            hls.fetch_section(self.playlist, segment.start, segment.end, Path(tmp) / "segment.mp4", self.session)
            return _extract(Path(tmp) / "segment.mp4", segment.start, segment.duration, fps)


class FileFrames:
    """Frames from a local video file."""

    def __init__(self, path: Path, duration: float):
        self.path = path
        self.duration = duration

    def times(self, start: float, end: float, step: float) -> list[float]:
        times = []
        t = start
        while t <= min(end, self.duration):
            times.append(t)
            t += step
        return times

    def dense_frames(self, t: float, fps: float, span: float = 10.0) -> list[tuple[float, Image.Image]]:
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            ffmpeg.run("-ss", f"{t:.3f}", "-i", str(self.path), "-t", f"{span:.3f}",
                       "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", str(clip))  # fmt: skip
            return _extract(clip, t, span, fps)

    def frame(self, t: float) -> Image.Image:
        png = ffmpeg.run(
            "-ss", f"{t:.3f}", "-i", str(self.path), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"
        )  # fmt: skip
        if not png:
            raise RuntimeError(f"couldn't read a frame at {t:.1f}s")
        return Image.open(BytesIO(png)).copy()


def _extract(clip: Path, start: float, duration: float, fps: float) -> list[tuple[float, Image.Image]]:
    """Frames of a clip at `fps`, with their times (clip time 0 is `start`)."""
    out_dir = clip.parent / "frames"
    out_dir.mkdir()
    ffmpeg.run("-i", str(clip), "-vf", f"fps={fps}", str(out_dir / "%05d.png"))
    frames = []
    for path in sorted(out_dir.glob("*.png")):
        # The fps filter emits the frame nearest each 1/fps step, starting at 0.
        t = start + (int(path.stem) - 1) / fps
        if t < start + duration:
            frames.append((t, Image.open(path).copy()))
    return frames


def refine(
    source: FrameSource,
    readings: Sequence[Reading],
    segments: int = 2,
    fps: float = 4.0,
    clock_region: ocr.Region = ocr.CLOCK_REGION,
) -> list[Reading]:
    """Clock readings at sub-second spacing around a few earlier readings.

    Readings taken at segment starts all fall at the same fraction of a
    second relative to the clock, so they can't narrow the offset below a
    second. A few seconds read densely catch the clock ticking over, which
    pins it to about 1/fps.
    """
    with_clock = [r for r in readings if r.clock_s is not None and r.clock_s > 0]
    if not with_clock or not hasattr(source, "dense_frames"):
        return []
    picks = [with_clock[round(i * (len(with_clock) - 1) / max(segments - 1, 1))] for i in range(segments)]
    dense = []
    for pick in dict.fromkeys(picks):
        try:
            frames = source.dense_frames(pick.t, fps)
        except (RuntimeError, requests.RequestException):
            continue
        dense.extend(Reading(t, None, ocr.read_clock(image, clock_region)) for t, image in frames)
    return [r for r in dense if r.clock_s is not None]


def read_frame(image: Image.Image, t: float, clock_region: ocr.Region = ocr.CLOCK_REGION) -> Reading:
    return Reading(t=t, match_id=ocr.read_match_id(image), clock_s=ocr.read_clock(image, clock_region))


def scan(
    source: FrameSource,
    start: float = 0.0,
    end: float | None = None,
    step: float = 30.0,
    workers: int = 8,
    progress: Callable[[Reading], None] | None = None,
    clock_region: ocr.Region = ocr.CLOCK_REGION,
) -> list[Reading]:
    """Read the match id and clock every `step` seconds of footage."""
    times = source.times(start, source.duration if end is None else end, step)

    def read(t: float) -> Reading:
        try:
            return read_frame(source.frame(t), t, clock_region)
        except (RuntimeError, requests.RequestException):
            return Reading(t, None, None)

    readings = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for reading in pool.map(read, times):
            readings.append(reading)
            if progress:
                progress(reading)
    return readings


def group_matches(readings: Sequence[Reading], min_reads: int = 2) -> list[MatchSpan]:
    """Group readings into matches, ignoring ids read too rarely to trust.

    A misread id (a dropped or swapped digit) shows up once or twice, while the
    real one is read on most frames of the match. Readings with a clock but no
    id belong to the match whose span they fall in.
    """
    readings = _merge_misreads(readings)
    counts = Counter(r.match_id for r in readings if r.match_id is not None)
    trusted = {match_id for match_id, count in counts.items() if count >= min_reads}
    spans = []
    for match_id in trusted:
        times = [r.t for r in readings if r.match_id == match_id]
        first, last = min(times), max(times)
        inside = tuple(r for r in readings if first <= r.t <= last and r.match_id in (match_id, None))
        spans.append(MatchSpan(match_id, first, last, inside))
    return sorted(spans, key=lambda span: span.first_t)


def _merge_misreads(readings: Sequence[Reading]) -> list[Reading]:
    """Credit ids one digit away from a more common id to that id.

    OCR sometimes drops the digit after "MATCH:" (118211297 read as
    18211297) or swaps a look-alike digit (6 for 8), often enough to pass as
    an id of its own.
    """
    counts = Counter(r.match_id for r in readings if r.match_id is not None)
    fixes = {}
    for rare in counts:
        likely = [full for full in counts if counts[full] > counts[rare] and _one_digit_off(str(rare), str(full))]
        if likely:
            fixes[rare] = max(likely, key=lambda full: counts[full])
    return [Reading(r.t, fixes.get(r.match_id, r.match_id), r.clock_s) for r in readings]


def _one_digit_off(rare: str, full: str) -> bool:
    if len(full) == len(rare) + 1:
        return any(full[:i] + full[i + 1 :] == rare for i in range(len(full)))
    if len(full) == len(rare):
        return sum(a != b for a, b in zip(rare, full)) == 1
    return False
