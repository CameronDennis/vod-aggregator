"""Parsing for the times and durations people type on the command line."""

from __future__ import annotations

import re
from datetime import datetime, timezone

_UNITS = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$")


def parse_timestamp(value: str, precise: bool = False) -> int | float:
    """Unix seconds from either a unix timestamp or an ISO 8601 datetime.

    An ISO datetime with no offset is taken to be UTC, which is what Twitch
    reports. Whole seconds are returned unless `precise` is set.
    """
    value = value.strip()
    if re.fullmatch(r"\d+(\.\d+)?", value):
        seconds = float(value)
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        seconds = parsed.timestamp()
    return seconds if precise else int(seconds)


def parse_duration(value: str) -> float:
    """Seconds from '90', '1:30', '1:02:03' or '1h2m3s'."""
    value = value.strip()
    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value)
    if ":" in value:
        seconds = 0.0
        for part in value.split(":"):
            seconds = seconds * 60 + float(part)
        return seconds
    match = _UNITS.fullmatch(value)
    if not match or not any(match.groups()):
        raise ValueError(f"not a duration: {value!r}")
    hours, minutes, seconds = (float(group or 0) for group in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def format_timestamp(unix_time: int | None) -> str:
    if unix_time is None:
        return "-"
    return datetime.fromtimestamp(unix_time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
