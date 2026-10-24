import shutil
from pathlib import Path

import pytest
from PIL import Image

from vodagg.match import ocr

FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.skipif(shutil.which("tesseract") is None, reason="needs Tesseract")


def frame_with_hud() -> Image.Image:
    """A 1080p frame with HUD crops from a real stream pasted where they were."""
    frame = Image.new("RGB", (1920, 1080), (30, 30, 40))
    frame.paste(Image.open(FIXTURES / "hud_match_id.png"), (1700, 1030))
    frame.paste(Image.open(FIXTURES / "hud_clock.png"), (880, 0))
    return frame


def test_reads_match_id_and_clock():
    frame = frame_with_hud()
    assert ocr.read_match_id(frame) == 118211297
    assert ocr.read_clock(frame) == 16 * 60 + 57


def test_regions_scale_with_resolution():
    frame = frame_with_hud().resize((2560, 1440), Image.Resampling.LANCZOS)
    assert ocr.read_clock(frame) == 16 * 60 + 57


def test_blank_frame_reads_nothing():
    blank = Image.new("RGB", (1920, 1080))
    assert ocr.read_match_id(blank) is None
    assert ocr.read_clock(blank) is None
