"""Reading the match id and game clock off a stream frame.

Deadlock draws the match id in small text in the bottom-right corner
("MATCH: 118211297") and the game clock at the top middle ("16:57"). Both are
light text on a dark or busy background, so each region is cropped, enlarged
and thresholded to black-on-white before Tesseract reads it.

Regions are fractions of the frame, so any resolution with the default HUD
layout works. Streams that crop or rescale the game into an overlay need their
own regions.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps


@dataclass(frozen=True)
class Region:
    """A box as fractions of the frame's width and height."""

    left: float
    top: float
    width: float
    height: float

    def crop(self, image: Image.Image) -> Image.Image:
        w, h = image.size
        box = (
            round(self.left * w),
            round(self.top * h),
            round((self.left + self.width) * w),
            round((self.top + self.height) * h),
        )
        return image.crop(box)


# Measured on 1920x1080 Deadlock HUD frames.
MATCH_ID_REGION = Region(1720 / 1920, 1040 / 1080, 200 / 1920, 24 / 1080)
CLOCK_REGION = Region(905 / 1920, 16 / 1080, 110 / 1920, 30 / 1080)

_MATCH_ID = re.compile(r"MATCH\W*(\d{6,12})", re.IGNORECASE)
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")


class TesseractMissing(RuntimeError):
    pass


def read_match_id(frame: Image.Image, region: Region = MATCH_ID_REGION) -> int | None:
    # The id is dim grey, so plain inversion reads it more reliably than a
    # threshold; the threshold is a fallback for bright backgrounds.
    crop = region.crop(frame)
    for image in (_prepare(crop), _prepare(crop, threshold=80)):
        match = _MATCH_ID.search(_ocr(image, psm=7).replace(" ", ""))
        if match:
            return int(match.group(1))
    return None


def read_clock(frame: Image.Image, region: Region = CLOCK_REGION) -> int | None:
    """The game clock in seconds, or None if it isn't readable."""
    text = _ocr(_prepare(region.crop(frame), threshold=170), psm=7, whitelist="0123456789:")
    match = _CLOCK.match(text.strip())
    if not match:
        return None
    minutes, seconds = int(match.group(1)), int(match.group(2))
    if seconds >= 60:
        return None
    return minutes * 60 + seconds


def _prepare(crop: Image.Image, threshold: int | None = None, scale: int = 4) -> Image.Image:
    """Enlarge light HUD text and make it dark on light, binarised if `threshold` is given."""
    gray = ImageOps.grayscale(crop)
    gray = gray.resize((gray.width * scale, gray.height * scale), Image.Resampling.BICUBIC)
    if threshold is None:
        return ImageOps.invert(gray)
    return gray.point(lambda value: 0 if value > threshold else 255)


def _ocr(image: Image.Image, psm: int, whitelist: str | None = None) -> str:
    executable = shutil.which("tesseract")
    if executable is None:
        raise TesseractMissing("Tesseract OCR is not installed; see the README")
    args = [executable, "stdin", "stdout", "--psm", str(psm)]
    if whitelist:
        args += ["-c", f"tessedit_char_whitelist={whitelist}"]
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    # One thread per call: frames are read in parallel already, and Tesseract's
    # own threads fight over the CPU. Busy, noisy crops can make it crawl, so
    # give up on a frame rather than stall the scan.
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    try:
        result = subprocess.run(args, input=buffer.getvalue(), capture_output=True, env=env, timeout=10)
    except subprocess.TimeoutExpired:
        return ""
    return result.stdout.decode(errors="replace").strip()
