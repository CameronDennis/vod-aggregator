"""Registering video files that were downloaded some other way.

This covers footage saved with browser tools such as voddownloader.com, which
can't be driven from here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def probe_duration(path: Path) -> float | None:
    """Duration of a video file in seconds, or None if ffprobe can't tell."""
    executable = shutil.which("ffprobe")
    if executable is None:
        return None
    result = subprocess.run(
        [executable, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, ValueError):
        return None
