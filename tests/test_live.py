import shutil
import subprocess

import pytest

from vodagg.footage.live import remux_to_mp4


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_remux_to_mp4_replaces_the_recording(tmp_path):
    recording = tmp_path / "mike_1.ts"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=30", "-t", "2",
                    "-c:v", "libx264", "-f", "mpegts", str(recording)], check=True)  # fmt: skip
    mp4 = remux_to_mp4(recording)
    assert mp4 == tmp_path / "mike_1.mp4" and mp4.stat().st_size > 0 and not recording.exists()
