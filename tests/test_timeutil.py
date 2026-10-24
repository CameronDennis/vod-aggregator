import pytest

from vodagg.timeutil import format_duration, parse_duration, parse_timestamp


@pytest.mark.parametrize(
    "value, seconds",
    [("90", 90), ("1:30", 90), ("1:02:03", 3723), ("1h2m3s", 3723), ("45m", 2700), ("2.5s", 2.5)],
)
def test_parse_duration(value, seconds):
    assert parse_duration(value) == seconds


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_parse_timestamp():
    assert parse_timestamp("1700000000") == 1700000000
    assert parse_timestamp("2023-11-14T22:13:20Z") == 1700000000
    assert parse_timestamp("2023-11-14 22:13:20") == 1700000000
    assert parse_timestamp("2023-11-14T17:13:20-05:00") == 1700000000


def test_format_duration():
    assert format_duration(3723) == "1:02:03"
