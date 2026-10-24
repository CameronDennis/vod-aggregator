"""Command-line interface: `vodagg --help`."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import click

from . import __version__, library
from .config import load_config
from .db import Database, Streamer
from .footage import download as downloader
from .footage import live, local, recovery, twitch
from .match import demo as demos
from .match import events as match_events
from .match import scan as scanner
from .match import service
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
        found, added = library.streamer_for_login(self.db, login)
        if added:
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
@click.option("--steam", "steam_account_ids", type=int, multiple=True,
              help="Steam account id used in Deadlock (repeat for alt accounts).")  # fmt: skip
@click.option("--notes")
@pass_ctx
def streamer_add(ctx: Context, name, twitch_login, steam_account_ids, notes):
    """Add a streamer."""
    try:
        added = ctx.db.add_streamer(name, twitch_login=twitch_login, steam_account_ids=steam_account_ids, notes=notes)
    except sqlite3.IntegrityError as error:
        raise click.ClickException(f"a streamer with that name, login or Steam id exists ({error})")
    click.echo(f"Added {added.name}.")


@streamer.command("update")
@click.argument("key")
@click.option("--name")
@click.option("--twitch", "twitch_login")
@click.option("--steam", "add_steam", type=int, multiple=True, help="Add a Steam account id (repeatable).")
@click.option("--remove-steam", type=int, multiple=True, help="Remove a Steam account id (repeatable).")
@click.option("--notes")
@pass_ctx
def streamer_update(ctx: Context, key, add_steam, remove_steam, **fields):
    """Change a streamer, found by name, Twitch login or Steam id."""
    streamer_id = ctx.streamer(key).id
    fields = {k: v for k, v in fields.items() if v is not None}
    updated = ctx.db.update_streamer(streamer_id, **fields)
    try:
        for account_id in add_steam:
            updated = ctx.db.add_steam_account(streamer_id, account_id)
    except sqlite3.IntegrityError:
        raise click.ClickException(f"Steam id {account_id} already belongs to {ctx.db.find_streamer(account_id).name}")
    for account_id in remove_steam:
        updated = ctx.db.remove_steam_account(streamer_id, account_id)
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
    rows = [
        (s.name, s.twitch_login or "-", ", ".join(map(str, s.steam_account_ids)) or "-")
        for s in ctx.db.list_streamers()
    ]
    _table(("Name", "Twitch", "Steam ids"), rows)


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
    existing = library.find_vod(ctx.db, info.video_id)
    if existing is not None:
        raise click.ClickException(f"VOD {info.video_id} is already footage {existing.id}")
    path = None
    if download:
        path = str(downloader.download(info.url, _footage_path(ctx, info.login, f"vod_{info.video_id}")))
    footage = library.add_vod(ctx.db, owner.id, info, path)
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
    try:
        hosts = twitch.GqlClient().storage_hosts(login)
    except Exception:  # Recovery still works from the built-in host list.
        hosts = []
    found = recovery.find_vod(login, broadcast_id, parse_timestamp(started), window, hosts=hosts)
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
    item = library.import_file(ctx.db, owner.id, path, parse_timestamp(started) if started else None)
    click.echo(f"Footage {item.id}: {path} ({format_duration(item.duration_s)}).")
    if not started:
        click.echo("No start time given; it can be set later or worked out from the in-game clock.")


@main.command("record")
@click.argument("login")
@pass_ctx
def record(ctx: Context, login):
    """Record a live stream until it ends or you press Ctrl+C."""
    owner = ctx.streamer_for_login(login)
    broadcast = None
    try:
        broadcast = twitch.GqlClient().get_live_stream(login)
    except Exception as error:  # Recording still works without the broadcast details.
        click.echo(f"Couldn't read broadcast details: {error}")
    recording = live.start_recording(login, ctx.config.footage_dir / owner.name)
    click.echo(f"Recording {login} to {recording.path}. Press Ctrl+C to stop.")
    try:
        recording.wait()
    except KeyboardInterrupt:
        recording.stop()
    path = recording.path
    try:
        path = live.remux_to_mp4(path)
    except RuntimeError as error:  # The recording is still usable as it is, just less precisely.
        click.echo(f"Couldn't convert the recording to MP4, keeping {path.name}: {error}")
    item = ctx.db.add_footage(
        owner.id,
        "live",
        broadcast_id=broadcast.broadcast_id if broadcast else None,
        source_url=f"https://www.twitch.tv/{login}",
        local_path=str(path.resolve()),
        started_at=int(recording.started_at),
        duration_s=int(local.probe_duration(path) or recording.duration_s),
    )
    click.echo(f"Footage {item.id}: {format_duration(item.duration_s)} recorded.")


@main.command("web")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True)
@click.option("--open/--no-open", "open_browser", default=True, help="Open the page in a browser.")
def web(host, port, open_browser):
    """Start the web UI."""
    try:
        import uvicorn

        from .web.app import create_app
    except ImportError:
        raise click.ClickException("the web UI needs its extras: pip install 'vodagg[web]'")
    url = f"http://{host}:{port}/"
    click.echo(f"vodagg web UI at {url} (Ctrl+C to stop)")
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, webbrowser.open, (url,)).start()
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


# Matches


@main.group("match")
def match_group():
    """Match ids, demos and syncing them with footage."""


@match_group.command("scan")
@click.argument("footage_id", type=int)
@click.option("--from", "start", default="0", help="Where to start, such as 1:02:03.")
@click.option("--to", "end", help="Where to stop (default: the end).")
@click.option("--every", default=30.0, show_default=True, help="Seconds between frames read.")
@pass_ctx
def match_scan(ctx: Context, footage_id, start, end, every):
    """List the matches footage shows, by reading the match id and clock."""
    _, source = _footage_source(ctx, footage_id)
    spans = scanner.group_matches(_scan(source, start, end, every))
    _table(
        ("Match", "From", "To", "Clock reads"),
        [
            (str(s.match_id), format_duration(s.first_t), format_duration(s.last_t), str(len(s.clock_readings)))
            for s in spans
        ],
    )


@match_group.command("sync")
@click.argument("footage_id", type=int)
@click.option("--match", "match_id", type=int, help="Only this match (default: every match found).")
@click.option("--demo", "demo_path", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Use this demo (.dem or .dem.bz2) instead of downloading it.")  # fmt: skip
@click.option("--from", "start", default="0", help="Where to start reading, such as 1:02:03.")
@click.option("--to", "end", help="Where to stop reading (default: the end).")
@click.option("--every", default=30.0, show_default=True, help="Seconds between frames read.")
@pass_ctx
def match_sync(ctx: Context, footage_id, match_id, demo_path, start, end, every):
    """Find matches in footage, parse their demos and line them up with it.

    Reads the match id and game clock from the footage, gets each match's demo
    from deadlock-api.com (or --demo), records players, kills, deaths and ults,
    and works out where each tick falls in the footage, pauses included.
    """
    item, source = _footage_source(ctx, footage_id)
    owner = ctx.db.get_streamer_by_id(item.streamer_id)
    parsed_given = None
    if demo_path:
        demo_path = demos.prepare_demo(demo_path)
        parsed_given = match_events.parse(demo_path)
        match_id = match_id or parsed_given.match_id
    spans = scanner.group_matches(_scan(source, start, end, every))
    if match_id:
        spans = [s for s in spans if s.match_id == match_id]
    if not spans:
        raise click.ClickException("no match found in the footage" + (f" with id {match_id}" if match_id else ""))

    given = (parsed_given, demo_path) if parsed_given else None
    for span in spans:
        click.echo(f"\nMatch {span.match_id}: footage {format_duration(span.first_t)}-{format_duration(span.last_t)}")
        outcome = service.sync_span(ctx.db, item.id, source, span, ctx.config.demo_dir, given)
        if outcome.match is None:
            click.echo(f"  Skipped: {outcome.error}. Pass --demo to use a file you have.")
            continue
        parsed = outcome.match
        for note in outcome.notes:
            click.echo(f"  {note}")
        for streamer, account_id in outcome.linked:
            click.echo(f"  Linked {streamer.name} to Steam id {account_id} by name.")
        if outcome.result is None:
            click.echo(f"  Couldn't sync: {outcome.error}.")
            continue
        result = outcome.result
        click.echo(
            f"  Synced from {result.used} clock readings ({result.rejected} discarded),"
            f" to within {result.uncertainty:.2f}s. The match starts at"
            f" {format_duration(outcome.start_t)} in the footage."
        )
        owner = ctx.db.get_streamer_by_id(owner.id)
        if any(parsed.player(account_id) for account_id in owner.steam_account_ids):
            _print_events(ctx, span.match_id, owner)
        else:
            click.echo(f"  {owner.name} isn't linked to a player here. Players in this match:")
            for p in parsed.players:
                click.echo(f"    {p.account_id:>10}  {p.name} ({p.hero_name})")
            click.echo(f"  Link them with `vodagg streamer update {owner.name} --steam <id>`.")


@match_group.command("events")
@click.argument("match_id", type=int)
@click.option("--streamer", "key", help="Only this streamer (default: every known streamer in the match).")
@pass_ctx
def match_events_command(ctx: Context, match_id, key):
    """Kills, deaths and ults of streamers in a match, with footage times."""
    if ctx.db.get_match(match_id) is None:
        raise click.ClickException(f"match {match_id} hasn't been parsed; run `vodagg match sync` first")
    streamers = [ctx.streamer(key)] if key else ctx.db.streamers_in_match(match_id)
    if not streamers:
        raise click.ClickException("no known streamers played in this match")
    for streamer in streamers:
        _print_events(ctx, match_id, streamer)


def _print_events(ctx: Context, match_id: int, streamer: Streamer) -> None:
    syncs = {row["footage_id"]: row for row in ctx.db.footage_syncs(match_id)}
    own = [f for f in ctx.db.list_footage(streamer.id) if f.id in syncs]
    sync_row = syncs[own[0].id] if own else None
    played = {p["account_id"] for p in ctx.db.match_players(match_id)}
    account_id = next((a for a in streamer.steam_account_ids if a in played), None)
    timed = service.player_events(ctx.db, match_id, account_id, sync_row) if account_id else []
    where = f"footage {own[0].id}" if own else "no synced footage"
    click.echo(f"  {streamer.name}: {len(timed)} events ({where})")
    _table(
        ("Clock", "Footage", "Event", "With"),
        [
            (
                _clock(e.clock_s),
                format_duration(e.footage_t) if e.footage_t is not None else "-",
                e.kind,
                e.other or e.ability or "",
            )
            for e in timed
        ],
    )


def _scan(source, start, end, every):
    with click.progressbar(length=1, label="Reading frames") as bar:
        times = source.times(parse_duration(start), parse_duration(end) if end else source.duration, every)
        bar.length = len(times)
        return scanner.scan(
            source,
            parse_duration(start),
            parse_duration(end) if end else None,
            every,
            progress=lambda reading: bar.update(1),
        )


def _footage_source(ctx: Context, footage_id: int):
    item = ctx.db.get_footage(footage_id)
    if item is None:
        raise click.ClickException(f"no footage with id {footage_id}")
    try:
        return item, service.frame_source(item)
    except ValueError as error:
        raise click.ClickException(str(error))


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    sign = "-" if seconds < 0 else ""
    seconds = int(abs(seconds))
    return f"{sign}{seconds // 60}:{seconds % 60:02d}"


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
