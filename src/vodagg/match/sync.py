"""Lining a match's demo up with footage of it.

The demo counts ticks at a fixed rate the whole time, pauses included, so
footage time is a straight line in ticks:

    footage_time = offset + tick / tick_rate

The on-screen clock is the match clock, which stops during pauses and starts
at zero when the match does. boon converts ticks to that clock with pauses
taken out (`tick_to_match_seconds`), so each clock reading on stream pins the
offset down:

    a clock showing "16:57" at footage time t means the match clock was in
    [1017, 1018), i.e. the tick was in [tick(1017), tick(1018)), so
    offset is in [t - tick(1018)/rate, t - tick(1017)/rate].

Every reading gives an interval like that. OCR mistakes are thrown out by
discarding readings far from the median, and the rest are intersected,
which usually narrows the offset to well under a second.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

OUTLIER_S = 3.0


class MatchClock(Protocol):
    tick_rate: float
    first_tick: int
    last_tick: int

    def match_seconds(self, tick: int) -> float | None:
        """Match-clock seconds at a tick, pauses excluded; negative before the match."""
        ...


@dataclass(frozen=True)
class SyncResult:
    offset: float
    """Footage seconds at tick 0."""
    tick_rate: float
    used: int
    rejected: int
    uncertainty: float
    """Half the width of the range the offset is known to lie in, in seconds."""

    def footage_time(self, tick: int) -> float:
        return self.offset + tick / self.tick_rate

    def tick_at(self, footage_time: float) -> int:
        return round((footage_time - self.offset) * self.tick_rate)


class SyncError(RuntimeError):
    pass


def tick_at_clock(clock: MatchClock, seconds: float) -> int | None:
    """The first tick at which the match clock reaches `seconds`.

    The match clock never runs backwards, so a binary search works; during a
    pause it holds still, and the first tick of the hold is returned.
    """
    lo, hi = clock.first_tick, clock.last_tick
    last = clock.match_seconds(hi)
    if last is None or last < seconds:
        return None
    while lo < hi:
        mid = (lo + hi) // 2
        value = clock.match_seconds(mid)
        if value is None or value < seconds:
            lo = mid + 1
        else:
            hi = mid
    return lo


def fit(readings: Sequence[tuple[float, int]], clock: MatchClock) -> SyncResult:
    """Find the offset from (footage time, clock seconds) readings."""
    intervals = []
    for t, shown in readings:
        if shown <= 0:
            continue  # The clock sits at 0:00 before the match starts.
        start, end = tick_at_clock(clock, shown), tick_at_clock(clock, shown + 1)
        if start is None or end is None:
            continue  # Past the end of the demo: a misread or another match.
        intervals.append((t - end / clock.tick_rate, t - start / clock.tick_rate))
    if not intervals:
        raise SyncError("no clock readings fall inside this match")

    # A reading taken during a pause gives a wide interval (the clock showed
    # the same second for the whole pause), so the median comes from the
    # ordinary one-second readings, and a reading only counts as an outlier if
    # its interval is nowhere near it.
    narrow = [(lo + hi) / 2 for lo, hi in intervals if hi - lo <= 2] or [(lo + hi) / 2 for lo, hi in intervals]
    median = statistics.median(narrow)
    inliers = [(lo, hi) for lo, hi in intervals if lo - OUTLIER_S <= median <= hi + OUTLIER_S]
    lo = max(lo for lo, _ in inliers)
    hi = min(hi for _, hi in inliers)
    if lo <= hi:
        offset, uncertainty = (lo + hi) / 2, (hi - lo) / 2
    else:
        # Readings disagree slightly (a frame caught mid-change, or a stream
        # hiccup); fall back to the middle of them.
        offset = statistics.median((a + b) / 2 for a, b in inliers)
        uncertainty = (lo - hi) / 2
    return SyncResult(
        offset=offset,
        tick_rate=clock.tick_rate,
        used=len(inliers),
        rejected=len(intervals) - len(inliers),
        uncertainty=uncertainty,
    )
