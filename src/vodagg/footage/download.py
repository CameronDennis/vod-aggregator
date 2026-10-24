"""Downloading stretches of footage.

Twitch VODs and recovered playlists are HLS, so a stretch is fetched segment
by segment with `hls`: only the part wanted is downloaded. yt-dlp finds a
VOD's playlist through Twitch's public web API, so no API key is needed.
"""

from __future__ import annotations

from pathlib import Path

import yt_dlp

from . import hls


def download_section(url: str, start: float, end: float, out_path: Path) -> Path:
    """Save the [start, end) seconds of a VOD, cut on keyframes (fast and lossless)."""
    playlist_url = media_playlist_url(url)
    if playlist_url is None:
        raise ValueError("this footage isn't an HLS stream, so a stretch of it can't be fetched")
    return hls.fetch_section(hls.load_playlist(playlist_url), start, end, out_path)


def media_playlist_url(url: str) -> str | None:
    """The HLS media playlist behind a VOD URL, or None if it isn't HLS."""
    if url.split("?")[0].endswith(".m3u8"):
        return url
    with yt_dlp.YoutubeDL({"quiet": True, "format": "best"}) as ydl:
        info = ydl.extract_info(url, download=False)
    if str(info.get("protocol", "")).startswith("m3u8"):
        return info["url"]
    return None
