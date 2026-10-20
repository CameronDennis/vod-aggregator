"""Recording live streams with streamlink.

The wall-clock time the recording starts is stored with the footage as a rough
anchor. It runs a few seconds behind the broadcast because of stream delay;
exact alignment with match events comes from reading the in-game clock.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


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


def start_recording(login: str, out_dir: Path, quality: str = "best") -> Recording:
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
            quality,
        ]
    )
    return Recording(login=login, path=path, started_at=started_at, process=process)
