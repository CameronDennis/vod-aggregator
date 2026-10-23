"""Getting a match's demo file: from deadlock-api.com by match id, or a local file.

deadlock-api.com looks up the match's replay "salt" and returns a link to the
compressed demo on Valve's replay servers. Valve keeps demos for a limited
time, so recent matches work best.
"""

from __future__ import annotations

import bz2
import shutil
from pathlib import Path

import requests

SALTS_URL = "https://api.deadlock-api.com/v1/matches/{match_id}/salts"


class DemoUnavailable(RuntimeError):
    pass


def demo_url(match_id: int, session: requests.Session | None = None) -> str:
    session = session or requests.Session()
    # Many matches are already in deadlock-api.com's own database. Asking it
    # not to go to Steam avoids Steam's tight rate limit for those.
    cached = session.get(SALTS_URL.format(match_id=match_id), params={"disable_steam": "true"}, timeout=30)
    if cached.ok and cached.json().get("demo_url"):
        return cached.json()["demo_url"]
    response = session.get(SALTS_URL.format(match_id=match_id), timeout=30)
    if response.status_code in (429, 503):
        wait = response.headers.get("Retry-After")
        hint = f"; try again in {int(wait) // 60 + 1} minutes" if wait and wait.isdigit() else ""
        raise DemoUnavailable(f"deadlock-api.com is rate limited by Steam{hint}")
    if response.status_code == 404:
        raise DemoUnavailable(f"deadlock-api.com has no replay for match {match_id}")
    response.raise_for_status()
    url = response.json().get("demo_url")
    if not url:
        raise DemoUnavailable(f"deadlock-api.com has no replay link for match {match_id}")
    return url


def download_demo(match_id: int, dest_dir: Path, session: requests.Session | None = None) -> Path:
    """Download and decompress a match's demo, reusing one already downloaded."""
    dest = dest_dir / f"{match_id}.dem"
    if dest.exists():
        return dest
    session = session or requests.Session()
    url = demo_url(match_id, session)
    dest_dir.mkdir(parents=True, exist_ok=True)
    compressed = dest.with_suffix(".dem.bz2.part")
    with session.get(url, stream=True, timeout=60) as response:
        if response.status_code == 404:
            raise DemoUnavailable(f"Valve no longer has the demo for match {match_id}")
        response.raise_for_status()
        with compressed.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                handle.write(chunk)
    _decompress(compressed, dest)
    compressed.unlink()
    return dest


def prepare_demo(path: Path) -> Path:
    """A demo path ready for boon, decompressing a .bz2 next to it if needed."""
    if path.suffix != ".bz2":
        return path
    dest = path.with_suffix("")
    if not dest.exists():
        _decompress(path, dest)
    return dest


def prepare_upload(path: Path) -> Path:
    """An uploaded demo, whatever its name, as a .dem next to it: decompressed if it's bz2 or zstd."""
    dest = path.with_suffix(".dem")
    with path.open("rb") as handle:
        magic = handle.read(4)
    if magic[:3] == b"BZh" or magic == ZSTD_MAGIC:
        _decompress(path, dest)
        path.unlink()
    else:
        path.replace(dest)
    return dest


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _decompress(source: Path, dest: Path) -> None:
    # Valve's replay links still end in .dem.bz2, but newer demos are zstd.
    with source.open("rb") as handle:
        is_zstd = handle.read(4) == ZSTD_MAGIC
    partial = dest.with_suffix(dest.suffix + ".part")
    with _open_compressed(source, is_zstd) as reader, partial.open("wb") as writer:
        shutil.copyfileobj(reader, writer, length=1 << 20)
    partial.replace(dest)


def _open_compressed(source: Path, is_zstd: bool):
    if not is_zstd:
        return bz2.open(source, "rb")
    import zstandard

    return zstandard.open(source, "rb")
