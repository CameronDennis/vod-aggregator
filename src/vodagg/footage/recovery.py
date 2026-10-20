"""Recover deleted or expired Twitch VODs.

Twitch keeps a broadcast's video segments on its storage servers for a while
after the VOD page is gone. The playlist lives at a path derived from the
channel login, the broadcast id and the broadcast's start time in unix seconds:

    https://<host>/<sha1(base)[:20]>_<base>/chunked/index-dvr.m3u8
    where base = "<login>_<broadcast_id>_<start_unix>"

The broadcast id and start time come from the live stream (see
GqlClient.get_live_stream) or from a stream tracker site such as
TwitchTracker. Tracker start times are often off by a few seconds, so recovery
tries every second in a window around the given time.

Recovery is best-effort: segments may be missing, muted segments are renamed,
and old broadcasts are eventually deleted for good.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests

# Storage hosts Twitch has served VOD segments from.
HOSTS = (
    "d2e2de1etea730.cloudfront.net",
    "dqrpb9wgowsf5.cloudfront.net",
    "ds0h3roq6wcgc.cloudfront.net",
    "d2nvs31859zcd8.cloudfront.net",
    "d2aba1wr3818hz.cloudfront.net",
    "d3c27h4odz752x.cloudfront.net",
    "dgeft87wbj63p.cloudfront.net",
    "d1m7jfoe9zdc1j.cloudfront.net",
    "d3vd9lfkzbru3h.cloudfront.net",
    "d2vjef5jvl6bfs.cloudfront.net",
    "d1ymi26ma8va5x.cloudfront.net",
    "d1mhjrowxxagfy.cloudfront.net",
    "ddacn6pr5v0tl.cloudfront.net",
    "d3aqoihi2n8ty8.cloudfront.net",
    "d3fi1amfgojobc.cloudfront.net",
    "d3stzm2eumvgb4.cloudfront.net",
    "d2vi6trrdongqn.cloudfront.net",
    "vod-secure.twitch.tv",
    "vod-metro.twitch.tv",
    "vod-pop-secure.twitch.tv",
)

@dataclass(frozen=True)
class RecoveredVod:
    login: str
    broadcast_id: str
    started_at: int
    playlist_url: str


def path_for(login: str, broadcast_id: str, start_unix: int) -> str:
    base = f"{login.lower()}_{broadcast_id}_{start_unix}"
    digest = hashlib.sha1(base.encode()).hexdigest()[:20]
    return f"{digest}_{base}"


def candidate_urls(
    login: str,
    broadcast_id: str,
    start_unix: int,
    window_s: int = 60,
    hosts: Iterable[str] = HOSTS,
    quality: str = "chunked",
) -> Iterator[tuple[int, str]]:
    """(start time, playlist URL) pairs, nearest start times first."""
    hosts = tuple(hosts)
    for offset in sorted(range(-window_s, window_s + 1), key=abs):
        start = start_unix + offset
        path = path_for(login, broadcast_id, start)
        for host in hosts:
            yield start, f"https://{host}/{path}/{quality}/index-dvr.m3u8"


def find_vod(
    login: str,
    broadcast_id: str,
    start_unix: int,
    window_s: int = 60,
    session: requests.Session | None = None,
    hosts: Iterable[str] = (),
    workers: int = 16,
) -> RecoveredVod | None:
    """Search the storage hosts for a broadcast's playlist.

    `hosts` are tried before the built-in list; the hosts serving the
    channel's other VODs (see GqlClient.storage_hosts) are the likeliest.
    """
    session = session or requests.Session()
    all_hosts = tuple(dict.fromkeys((*hosts, *HOSTS)))

    def exists(url: str) -> bool:
        try:
            return session.head(url, timeout=5).status_code == 200
        except requests.RequestException:
            return False

    candidates = list(candidate_urls(login, broadcast_id, start_unix, window_s, all_hosts))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Batches keep the nearest start times first while checking in parallel.
        for i in range(0, len(candidates), workers):
            batch = candidates[i : i + workers]
            for (start, url), found in zip(batch, pool.map(exists, [url for _, url in batch])):
                if found:
                    return RecoveredVod(login=login, broadcast_id=broadcast_id, started_at=start, playlist_url=url)
    return None


def fix_muted_playlist(playlist: str, available: set[str]) -> str:
    """Point muted segment names at the files that actually exist.

    Twitch renames segments it muted for copyrighted audio from "N.ts" to
    "N-muted.ts" (or "N-unmuted.ts" once restored), but a recovered playlist
    often still lists the original names. `available` is the set of segment
    names known to exist on the host.
    """
    lines = []
    for line in playlist.splitlines():
        if line.endswith(".ts") and not line.startswith("#"):
            stem = line[: -len(".ts")]
            if line not in available:
                for variant in (f"{stem}-muted.ts", f"{stem}-unmuted.ts"):
                    if variant in available:
                        line = variant
                        break
        lines.append(line)
    return "\n".join(lines) + "\n"
