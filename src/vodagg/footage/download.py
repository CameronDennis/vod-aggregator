"""Downloading footage, whole or in sections.

yt-dlp resolves Twitch VODs through the public web API, so no API key is
needed, and downloads whole VODs. Sections of HLS footage (Twitch VODs and
recovered playlists) are fetched segment by segment with `hls`, because
yt-dlp's section download reads a fragmented-MP4 VOD from its start.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import yt_dlp
from yt_dlp.utils import download_range_func

from . import hls


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
    if sections:
        playlist_url = media_playlist_url(url)
        if playlist_url:
            playlist = hls.load_playlist(playlist_url)
            paths = []
            for start, end in sections:
                path = out_path if len(sections) == 1 else out_path.with_suffix(f".{int(start)}.mp4")
                paths.append(hls.fetch_section(playlist, start, end, path))
            return paths[0]
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


def media_playlist_url(url: str) -> str | None:
    """The HLS media playlist behind a VOD URL, or None if it isn't HLS."""
    if url.split("?")[0].endswith(".m3u8"):
        return url
    with yt_dlp.YoutubeDL({"quiet": True, "format": "best"}) as ydl:
        info = ydl.extract_info(url, download=False)
    if str(info.get("protocol", "")).startswith("m3u8"):
        return info["url"]
    return None
