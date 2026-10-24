"""Running ffmpeg, which cuts, joins and reads frames from footage."""

from __future__ import annotations

import shutil
import subprocess


def run(*args: str) -> bytes:
    """Run ffmpeg with these arguments and return what it wrote to stdout."""
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise RuntimeError("ffmpeg is not installed")
    result = subprocess.run([executable, "-y", "-hide_banner", "-loglevel", "error", *args], capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout
