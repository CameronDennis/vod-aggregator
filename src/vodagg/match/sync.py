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

Every reading gives an interval like that. The offset is where the most
intervals overlap, which ignores OCR mistakes and, with readings at
different fractions of a second, narrows it to well under a second.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

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

    # The true offset lies in every correct reading's interval, while misreads
    # land anywhere. So take the stretch of offsets covered by the most
    # intervals: a sweep over the interval ends.
    ends = sorted([(lo, 0) for lo, _ in intervals] + [(hi, 1) for _, hi in intervals])
    best, best_lo, best_hi, depth = 0, 0.0, 0.0, 0
    for i, (value, is_end) in enumerate(ends):
        depth += -1 if is_end else 1
        if not is_end and depth > best:
            best, best_lo = depth, value
            best_hi = ends[i + 1][0]  # The next start or end closes this stretch.
    offset = (best_lo + best_hi) / 2
    uncertainty = (best_hi - best_lo) / 2
    used = sum(1 for lo, hi in intervals if lo <= offset <= hi)
    return SyncResult(
        offset=offset,
        tick_rate=clock.tick_rate,
        used=used,
        rejected=len(intervals) - used,
        uncertainty=uncertainty,
    )
