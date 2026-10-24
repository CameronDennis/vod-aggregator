import pytest

from vodagg.match import sync
from vodagg.match.events import pause_runs
from vodagg.match.scan import Reading, group_matches


class FakeClock:
    """64 ticks a second; the match starts at tick 640 and pauses for 30 s at 100 s on the clock."""

    tick_rate = 64.0
    first_tick = 0
    last_tick = 64 * 1000
    start = 640
    pause_at = 100.0
    pause_s = 30.0

    def match_seconds(self, tick):
        s = (tick - self.start) / self.tick_rate
        if s > self.pause_at:
            s = max(self.pause_at, s - self.pause_s)
        return s

    def screen(self, footage_t, offset):
        """What the HUD clock shows at a footage time."""
        import math

        return math.floor(self.match_seconds(round((footage_t - offset) * self.tick_rate)))


def test_tick_at_clock_skips_the_pause():
    clock = FakeClock()
    assert sync.tick_at_clock(clock, 0) == 640
    assert sync.tick_at_clock(clock, 50) == 640 + 50 * 64
    # 101 s on the clock comes 30 s of ticks after 100 s.
    assert sync.tick_at_clock(clock, 101) - sync.tick_at_clock(clock, 100) == 31 * 64
    assert sync.tick_at_clock(clock, 10_000) is None


def test_fit_recovers_offset_across_a_pause():
    clock = FakeClock()
    true_offset = 1234.37
    times = [true_offset + 15 + 7.3 * i for i in range(60)]
    readings = [(t, clock.screen(t, true_offset)) for t in times]
    readings += [(times[3], 500), (times[8], 3), (times[10], clock.screen(times[10], true_offset) + 3)]  # misreads
    result = sync.fit(readings, clock)
    assert result.rejected == 3
    assert abs(result.offset - true_offset) <= result.uncertainty + 0.02
    assert result.uncertainty < 0.5
    # An event after the pause lands where it happened in the footage.
    tick = sync.tick_at_clock(clock, 150)
    assert result.footage_time(tick) == pytest.approx(true_offset + tick / 64, abs=0.5)


def test_fit_needs_readings_inside_the_match():
    with pytest.raises(sync.SyncError):
        sync.fit([(10.0, 0), (20.0, 5000)], FakeClock())


def test_group_matches_merges_dropped_digit_misreads_and_noise():
    readings = [Reading(t, 118211297, t) for t in range(0, 100, 10)]
    readings += [Reading(15, 18211297, None), Reading(25, 18211297, None), Reading(35, 123, None)]
    readings += [Reading(55, None, 55), Reading(500, None, 30)]
    (span,) = group_matches(readings)
    assert span.match_id == 118211297
    assert (span.first_t, span.last_t) == (0, 90)
    assert (55, 55) in span.clock_readings and (500, 30) not in span.clock_readings


def test_group_matches_merges_swapped_digit_misreads():
    readings = [Reading(t, 118333586, t) for t in range(0, 100, 10)]
    readings += [Reading(15, 116333586, None), Reading(25, 116333586, None), Reading(35, 118233586, None)]
    (span,) = group_matches(readings)
    assert span.match_id == 118333586


def test_pause_runs_groups_consecutive_ticks():
    assert pause_runs([5, 6, 7, 20, 21, 40]) == [(5, 7), (20, 21), (40, 40)]
    assert pause_runs([]) == []


def test_offset_at_turnover_takes_the_frame_where_the_clock_changes():
    clock = FakeClock()
    # The clock turns over to 1:00 at tick 640 + 60 * 64, shown at 500 s of footage.
    assert sync.offset_at_turnover(clock, 500.0, 60) == 500.0 - (640 + 60 * 64) / 64
    # After the 30 s pause at 1:40, 1:50 comes 30 s of ticks later than it would without it.
    assert sync.offset_at_turnover(clock, 500.0, 110) == 500.0 - (640 + 140 * 64) / 64
    with pytest.raises(sync.SyncError):
        sync.offset_at_turnover(clock, 500.0, 10_000)
