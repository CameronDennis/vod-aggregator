"""Recording live streams with streamlink.

The wall-clock time the recording starts is stored with the footage as a rough
anchor. It runs a few seconds behind the broadcast because of stream delay;
exact alignment with match events comes from reading the in-game clock.

streamlink saves MPEG-TS, which ffmpeg can't seek accurately (cuts start
frozen, frames read late by up to a keyframe), so finished recordings are
remuxed to MP4.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .. import ffmpeg


class StreamlinkMissing(RuntimeError):
    pass


@dataclass
class Recording:
    login: str
    path: Path
    started_at: float
    process: subprocess.Popen

    def wait(self) -> int:
        return self.process.wait()

    def stop(self) -> None:
        self.process.terminate()
        self.process.wait(timeout=30)

    @property
    def duration_s(self) -> float:
        return time.time() - self.started_at


def start_recording(login: str, out_dir: Path) -> Recording:
    """Start recording a channel and return without waiting for it to end."""
    executable = shutil.which("streamlink")
    if executable is None:
        raise StreamlinkMissing("streamlink is not installed; run `pip install vodagg[live]`")
    out_dir.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    path = out_dir / f"{login}_{int(started_at)}.ts"
    process = subprocess.Popen(
        [
            executable,
            "--twitch-disable-ads",
            "--output",
            str(path),
            f"https://www.twitch.tv/{login}",
            "best",
        ]
    )
    return Recording(login=login, path=path, started_at=started_at, process=process)


def remux_to_mp4(path: Path) -> Path:
    """Copy a recording's streams into an MP4 beside it, without re-encoding, and remove the original."""
    dest = path.with_suffix(".mp4")
    ffmpeg.run("-i", str(path), "-c", "copy", "-movflags", "+faststart", str(dest))
    path.unlink()
    return dest
