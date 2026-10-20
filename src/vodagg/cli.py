"""Command-line interface: `vodagg --help`."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import click

from . import __version__
from .config import load_config
from .db import Database, Streamer
from .footage import download as downloader
from .footage import live, local, recovery, twitch
from .timeutil import format_duration, format_timestamp, parse_duration, parse_timestamp


class Context:
    def __init__(self):
        self.config = load_config()
        self._db: Database | None = None

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = Database(self.config.db_path)
        return self._db

    def twitch(self) -> twitch.TwitchClient:
        return twitch.make_client(self.config.twitch_client_id, self.config.twitch_client_secret)

    def streamer(self, key: str) -> Streamer:
        found = self.db.find_streamer(key)
        if found is None:
            raise click.ClickException(f"no streamer {key!r}; add one with `vodagg streamer add`")
        return found

    def streamer_for_login(self, login: str) -> Streamer:
        """The streamer with this Twitch login, added if they're new."""
        found = self.db.find_streamer(login)
        if found is None:
            found = self.db.add_streamer(login, twitch_login=login)
            click.echo(f"Added streamer {login}.")
        return found


pass_ctx = click.make_pass_decorator(Context, ensure=True)


@click.group()
@click.version_option(__version__)
def main():
    """Gather multi-perspective Twitch footage of Deadlock matches."""


# Streamers


@main.group()
def streamer():
    """Manage the streamer database."""


@streamer.command("add")
@click.argument("name")
@click.option("--twitch", "twitch_login", help="Twitch login, if different from the name.")
@click.option("--steam", "steam_account_id", type=int, help="Steam account id used in Deadlock.")
@click.option("--notes")
@pass_ctx
def streamer_add(ctx: Context, name, twitch_login, steam_account_id, notes):
    """Add a streamer."""
    try:
        added = ctx.db.add_streamer(
            name, twitch_login=twitch_login or name.lower(), steam_account_id=steam_account_id, notes=notes
        )
    except sqlite3.IntegrityError as error:
        raise click.ClickException(f"a streamer with that name, login or Steam id exists ({error})")
    click.echo(f"Added {added.name}.")


@streamer.command("update")
@click.argument("key")
@click.option("--name")
@click.option("--twitch", "twitch_login")
@click.option("--steam", "steam_account_id", type=int)
@click.option("--notes")
@pass_ctx
def streamer_update(ctx: Context, key, **fields):
    """Change a streamer, found by name, Twitch login or Steam id."""
    fields = {k: v for k, v in fields.items() if v is not None}
    updated = ctx.db.update_streamer(ctx.streamer(key).id, **fields)
    click.echo(f"Updated {updated.name}.")


@streamer.command("remove")
@click.argument("key")
@click.confirmation_option(prompt="Remove this streamer and their footage records?")
@pass_ctx
def streamer_remove(ctx: Context, key):
    """Remove a streamer and their footage records (files are kept)."""
    found = ctx.streamer(key)
    ctx.db.remove_streamer(found.id)
    click.echo(f"Removed {found.name}.")


@streamer.command("list")
@pass_ctx
def streamer_list(ctx: Context):
    """List known streamers."""
    rows = [(s.name, s.twitch_login or "-", str(s.steam_account_id or "-")) for s in ctx.db.list_streamers()]
    _table(("Name", "Twitch", "Steam id"), rows)


# Footage


@main.group()
def vod():
    """Twitch VODs (no API key needed)."""


@vod.command("list")
@click.argument("login")
@click.option("--limit", default=20, show_default=True)
@pass_ctx
def vod_list(ctx: Context, login, limit):
    """List a channel's past broadcasts."""
    vods = ctx.twitch().list_vods(login, limit)
    _table(
        ("Video id", "Started", "Length", "Title"),
        [(v.video_id, format_timestamp(v.started_at), format_duration(v.duration_s), v.title[:60]) for v in vods],
    )


@vod.command("add")
@click.argument("video")
@click.option("--download/--no-download", default=False, help="Download the whole VOD now.")
@pass_ctx
def vod_add(ctx: Context, video, download):
    """Register a VOD by id or URL, recording when it started."""
    info = ctx.twitch().get_vod(twitch.parse_video_id(video))
    owner = ctx.streamer_for_login(info.login)
    path = None
    if download:
        path = str(downloader.download(info.url, _footage_path(ctx, info.login, f"vod_{info.video_id}")))
    footage = ctx.db.add_footage(
        owner.id,
        "vod",
        twitch_video_id=info.video_id,
        source_url=info.url,
        local_path=path,
        started_at=info.started_at,
        duration_s=info.duration_s,
    )
    click.echo(
        f"Footage {footage.id}: {owner.name}, started {format_timestamp(info.started_at)},"
        f" {format_duration(info.duration_s)} long."
    )


@vod.command("recover")
@click.argument("login")
@click.argument("broadcast_id")
@click.argument("started")
@click.option("--window", default=60, show_default=True, help="Seconds either side of STARTED to try.")
@pass_ctx
def vod_recover(ctx: Context, login, broadcast_id, started, window):
    """Find a deleted VOD from its broadcast id and start time.

    STARTED is a unix timestamp or ISO datetime (UTC unless an offset is
    given), such as the start time TwitchTracker lists for the stream.
    """
    found = recovery.find_vod(login, broadcast_id, parse_timestamp(started), window)
    if found is None:
        raise click.ClickException("no playlist found; the broadcast may be gone or the start time too far off")
    owner = ctx.streamer_for_login(login)
    footage = ctx.db.add_footage(
        owner.id,
        "recovered",
        broadcast_id=broadcast_id,
        source_url=found.playlist_url,
        started_at=found.started_at,
    )
    click.echo(f"Footage {footage.id}: recovered playlist {found.playlist_url}")


@main.group()
def footage():
    """Footage already registered."""


@footage.command("list")
@click.argument("key", required=False)
@pass_ctx
def footage_list(ctx: Context, key):
    """List footage, optionally for one streamer."""
    names = {s.id: s.name for s in ctx.db.list_streamers()}
    items = ctx.db.list_footage(ctx.streamer(key).id if key else None)
    _table(
        ("Id", "Streamer", "Kind", "Started", "Length", "File"),
        [
            (
                str(f.id),
                names.get(f.streamer_id, "?"),
                f.kind,
                format_timestamp(f.started_at),
                format_duration(f.duration_s),
                f.local_path or "-",
            )
            for f in items
        ],
    )


@footage.command("download")
@click.argument("footage_id", type=int)
@click.option("--from", "start", help="Section start, such as 1:02:03.")
@click.option("--to", "end", help="Section end.")
@pass_ctx
def footage_download(ctx: Context, footage_id, start, end):
    """Download registered footage, whole or one section."""
    item = next((f for f in ctx.db.list_footage() if f.id == footage_id), None)
    if item is None or not item.source_url:
        raise click.ClickException(f"no downloadable footage with id {footage_id}")
    sections = None
    suffix = ""
    if start or end:
        if not (start and end):
            raise click.UsageError("give both --from and --to")
        sections = [(parse_duration(start), parse_duration(end))]
        suffix = f"_{int(sections[0][0])}-{int(sections[0][1])}"
    owner = ctx.db.get_streamer_by_id(item.streamer_id)
    path = downloader.download(item.source_url, _footage_path(ctx, owner.name, f"{item.kind}_{item.id}{suffix}"), sections)
    click.echo(f"Saved {path}")


@footage.command("import")
@click.argument("key")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--started", help="When the footage starts (unix or ISO datetime, UTC unless offset given).")
@click.option("--copy/--no-copy", default=False, help="Copy the file into the data folder.")
@pass_ctx
def footage_import(ctx: Context, key, path, started, copy):
    """Register a local video file, such as one saved with voddownloader.com."""
    owner = ctx.streamer(key)
    if copy:
        target = _footage_path(ctx, owner.name, path.stem).with_suffix(path.suffix)
        shutil.copy2(path, target)
        path = target
    duration = local.probe_duration(path)
    item = ctx.db.add_footage(
        owner.id,
        "local",
        local_path=str(path.resolve()),
        started_at=parse_timestamp(started) if started else None,
        duration_s=int(duration) if duration else None,
    )
    click.echo(f"Footage {item.id}: {path} ({format_duration(duration)}).")
    if not started:
        click.echo("No start time given; it can be set later or worked out from the in-game clock.")


@main.command("record")
@click.argument("login")
@click.option("--quality", default="best", show_default=True)
@pass_ctx
def record(ctx: Context, login, quality):
    """Record a live stream until it ends or you press Ctrl+C."""
    owner = ctx.streamer_for_login(login)
    broadcast = None
    try:
        broadcast = twitch.GqlClient().get_live_stream(login)
    except Exception as error:  # Recording still works without the broadcast details.
        click.echo(f"Couldn't read broadcast details: {error}")
    recording = live.start_recording(login, ctx.config.footage_dir / owner.name, quality)
    click.echo(f"Recording {login} to {recording.path}. Press Ctrl+C to stop.")
    try:
        recording.wait()
    except KeyboardInterrupt:
        recording.stop()
    item = ctx.db.add_footage(
        owner.id,
        "live",
        broadcast_id=broadcast.broadcast_id if broadcast else None,
        source_url=f"https://www.twitch.tv/{login}",
        local_path=str(recording.path.resolve()),
        started_at=int(recording.started_at),
        duration_s=int(local.probe_duration(recording.path) or recording.duration_s),
    )
    click.echo(f"Footage {item.id}: {format_duration(item.duration_s)} recorded.")


def _footage_path(ctx: Context, streamer_name: str, stem: str) -> Path:
    directory = ctx.config.footage_dir / streamer_name
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{stem}.mp4"


def _table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    if not rows:
        click.echo("Nothing yet.")
        return
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows)]
    for row in (headers, *rows):
        click.echo("  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)).rstrip())


if __name__ == "__main__":
    main()
