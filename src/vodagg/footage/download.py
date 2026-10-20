"""Downloading footage with yt-dlp, whole or in sections.

yt-dlp fetches Twitch VODs through the public web API, so no API key is needed.
It also downloads recovered VOD playlists directly from their m3u8 URL.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import yt_dlp
from yt_dlp.utils import download_range_func


def download(
    url: str,
    out_path: Path,
    sections: Sequence[tuple[float, float]] | None = None,
    quiet: bool = False,
) -> Path:
    """Download a video, or only the given (start, end) second ranges of it.

    Sections are cut on the nearest keyframes, which is fast and lossless. When
    several sections are given, each becomes its own file with the section's
    start time appended to the name.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    template = str(out_path.with_suffix(""))
    if sections and len(sections) > 1:
        template += ".%(section_start)d"
    options = {
        "outtmpl": template + ".%(ext)s",
        "format": "best",
        "merge_output_format": "mp4",
        "quiet": quiet,
        "noprogress": quiet,
        "concurrent_fragment_downloads": 8,
    }
    if sections:
        options["download_ranges"] = download_range_func(None, list(sections))
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info))
