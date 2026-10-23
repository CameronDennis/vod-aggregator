"""The web UI: a small local server over the same code the CLI uses.

`vodagg web` starts it. Pages are plain HTML and JavaScript in `static/`, and
everything they show comes from the JSON API here. Scans, syncs and exports
run as background jobs the pages poll.

Twitch's video CDN doesn't allow other sites to read its streams, so VOD
playback goes through this server (`/api/footage/{id}/hls/...`).
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, PositiveInt

from .. import clips
from ..config import Config, load_config
from ..db import Database, Footage, Streamer
from ..footage import download, hls, local, twitch
from ..match import demo, events, service
from ..match import scan as scanner
from ..timeutil import parse_timestamp
from .jobs import Job, Jobs

STATIC_DIR = Path(__file__).parent / "static"
_SEGMENT_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class NewStreamer(BaseModel):
    name: str
    twitch_login: str | None = None
    steam_account_ids: list[PositiveInt] = []


class StreamerChanges(BaseModel):
    name: str | None = None
    twitch_login: str | None = None
    notes: str | None = None


class NewAccount(BaseModel):
    account_id: PositiveInt


class NewVod(BaseModel):
    video: str
    scan: bool = True


class NewLocalFootage(BaseModel):
    streamer: str
    path: str
    started: str | None = None


class NewMatchFootage(BaseModel):
    match_id: PositiveInt
    streamer_id: int


class SyncRequest(BaseModel):
    start: float = 0.0
    end: float | None = None
    every: float = 30.0
    match_id: int | None = None


class ClipWindow(BaseModel):
    start_tick: float
    end_tick: float
    event_id: int | None = None
    label: str | None = None


class ExportRequest(BaseModel):
    match_id: int
    streamer_id: int
    event_ids: list[int] = []
    before: float = 8.0
    after: float = 4.0
    others: bool = True
    windows: list[ClipWindow] | None = None
    """Exact stretches to clip, used instead of `event_ids` with `before` and `after`."""


class FolderRequest(BaseModel):
    path: str


def create_app(config: Config | None = None) -> FastAPI:
    config = config or load_config()
    jobs = Jobs()
    playlist_urls: dict[int, str] = {}
    session = requests.Session()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        jobs.shutdown()

    app = FastAPI(title="vodagg", lifespan=lifespan)

    @contextmanager
    def open_db():
        # One connection per request or job: SQLite connections stay in their own thread.
        db = Database(config.db_path)
        try:
            yield db
        finally:
            db.close()

    # Streamers

    @app.get("/api/streamers")
    def list_streamers():
        with open_db() as db:
            return [_streamer_json(s) for s in db.list_streamers()]

    @app.post("/api/streamers")
    def add_streamer(body: NewStreamer):
        with open_db() as db:
            try:
                added = db.add_streamer(
                    body.name,
                    twitch_login=body.twitch_login or body.name.lower(),
                    steam_account_ids=tuple(body.steam_account_ids),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(409, "A streamer with that name, Twitch ID or Steam id exists.")
            return _streamer_json(added)

    @app.patch("/api/streamers/{streamer_id}")
    def update_streamer(streamer_id: int, body: StreamerChanges):
        with open_db() as db:
            _get_streamer(db, streamer_id)
            try:
                changed = db.update_streamer(streamer_id, **body.model_dump(exclude_none=True))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "Another streamer has that name or Twitch ID.")
            return _streamer_json(changed)

    @app.delete("/api/streamers/{streamer_id}")
    def remove_streamer(streamer_id: int):
        with open_db() as db:
            _get_streamer(db, streamer_id)
            db.remove_streamer(streamer_id)
        return {"ok": True}

    @app.post("/api/streamers/{streamer_id}/accounts")
    def add_account(streamer_id: int, body: NewAccount):
        with open_db() as db:
            _get_streamer(db, streamer_id)
            try:
                return _streamer_json(db.add_steam_account(streamer_id, body.account_id))
            except sqlite3.IntegrityError:
                owner = db.find_streamer(body.account_id)
                raise HTTPException(409, f"Steam id {body.account_id} already belongs to {owner.name}.")

    @app.delete("/api/streamers/{streamer_id}/accounts/{account_id}")
    def remove_account(streamer_id: int, account_id: int):
        with open_db() as db:
            _get_streamer(db, streamer_id)
            return _streamer_json(db.remove_steam_account(streamer_id, account_id))

    @app.get("/api/players/unlinked")
    def unlinked_players():
        with open_db() as db:
            return db.unlinked_players()

    # Footage

    @app.get("/api/footage")
    def list_footage():
        with open_db() as db:
            names = {s.id: s.name for s in db.list_streamers()}
            return [
                {**asdict(f), "streamer": names.get(f.streamer_id), "synced_matches": len(db.footage_matches(f.id)),
                 "unsynced_matches": len(db.unsynced_matches(f.id))}
                for f in db.list_footage()
            ]

    @app.post("/api/footage/vod")
    def add_vod(body: NewVod):
        try:
            video_id = twitch.parse_video_id(body.video)
        except ValueError as error:
            raise HTTPException(400, str(error))
        info = twitch.make_client(config.twitch_client_id, config.twitch_client_secret).get_vod(video_id)
        with open_db() as db:
            owner = db.find_streamer(info.login) or db.add_streamer(info.login, twitch_login=info.login)
            item = db.add_footage(
                owner.id,
                "vod",
                twitch_video_id=info.video_id,
                source_url=info.url,
                started_at=info.started_at,
                duration_s=info.duration_s,
            )
        job = start_sync(item, SyncRequest()) if body.scan else None
        return {"footage": asdict(item), "job": job.to_dict() if job else None}

    @app.post("/api/footage/match")
    def add_match_footage(body: NewMatchFootage):
        """Find a streamer's VOD of a match from when deadlock-api.com says it was played, and sync it."""
        with open_db() as db:
            owner = _get_streamer(db, body.streamer_id)
        if not owner.twitch_login:
            raise HTTPException(400, f"{owner.name} has no Twitch ID.")
        try:
            started, duration = demo.match_time(body.match_id, session)
        except demo.DemoUnavailable as error:
            raise HTTPException(404, str(error))
        with open_db() as db:
            item = next(iter(db.footage_covering(owner.id, started)), None)
            if item is None:
                client = twitch.make_client(config.twitch_client_id, config.twitch_client_secret)
                info = next((v for v in client.list_vods(owner.twitch_login, limit=100)
                             if v.started_at <= started <= v.started_at + v.duration_s), None)  # fmt: skip
                if info is None:
                    played = datetime.fromtimestamp(started, timezone.utc).strftime("%Y-%m-%d %H:%M")
                    raise HTTPException(404, f"No VOD of {owner.twitch_login} covers match {body.match_id}, "
                                             f"played {played} UTC.")  # fmt: skip
                item = db.add_footage(owner.id, "vod", twitch_video_id=info.video_id, source_url=info.url,
                                      started_at=info.started_at, duration_s=info.duration_s)  # fmt: skip
        # Read just the match's stretch of the VOD, with room for loading in and the end screen.
        offset = started - item.started_at
        job = start_sync(item, SyncRequest(start=max(0, offset - 120), end=offset + duration + 180, match_id=body.match_id))
        return {"footage": asdict(item), "job": job.to_dict()}

    @app.post("/api/footage/import")
    def import_footage(body: NewLocalFootage):
        path = Path(body.path).expanduser()
        if not path.is_file():
            raise HTTPException(400, f"No file at {path}.")
        with open_db() as db:
            owner = db.find_streamer(body.streamer)
            if owner is None:
                raise HTTPException(404, f"No streamer {body.streamer!r}.")
            duration = local.probe_duration(path)
            item = db.add_footage(
                owner.id,
                "local",
                local_path=str(path.resolve()),
                started_at=parse_timestamp(body.started) if body.started else None,
                duration_s=int(duration) if duration else None,
            )
        return asdict(item)

    @app.post("/api/footage/{footage_id}/sync")
    def sync_footage(footage_id: int, body: SyncRequest):
        with open_db() as db:
            item = db.get_footage(footage_id)
        if item is None:
            raise HTTPException(404, f"No footage {footage_id}.")
        return start_sync(item, body).to_dict()

    def start_sync(item: Footage, body: SyncRequest) -> Job:
        def work(job: Job) -> dict:
            source = service.frame_source(item)
            end = body.end if body.end is not None else source.duration
            job.step(0, len(source.times(body.start, end, body.every)), "Reading the match id and clock")
            readings = scanner.scan(source, body.start, end, body.every, progress=lambda _: job.step(job.done + 1))
            spans = scanner.group_matches(readings)
            if body.match_id:
                spans = [s for s in spans if s.match_id == body.match_id]
                if not spans:
                    raise ValueError(f"Couldn't read match {body.match_id} in the footage.")
            job.step(0, len(spans), f"Found {len(spans)} matches")
            results = []
            missing = []
            with open_db() as db:
                for i, span in enumerate(spans):
                    job.step(message=f"Syncing match {span.match_id} ({i + 1} of {len(spans)})")
                    outcome = service.sync_span(db, item.id, source, span, config.demo_dir)
                    results.append({"match_id": span.match_id, "synced": outcome.result is not None,
                                    "error": outcome.error})  # fmt: skip
                    if outcome.match is None:
                        missing.append((span, outcome.error))
                    job.step(i + 1)
                # A misread id inside a synced match's stretch of footage isn't a match of its own.
                synced = [s for s, r in zip(spans, results) if r["synced"]]
                for span, error in missing:
                    if not any(s.first_t <= span.last_t and span.first_t <= s.last_t for s in synced):
                        db.set_unsynced_match(item.id, span.match_id, span.first_t, span.last_t, error)
            synced = sum(r["synced"] for r in results)
            job.step(message=f"Synced {synced} of {len(spans)} matches")
            return {"footage_id": item.id, "matches": results}

        return jobs.start("sync", f"Scan and sync footage {item.id}", work)

    @app.get("/api/footage/{footage_id}/matches")
    def footage_matches(footage_id: int):
        with open_db() as db:
            return db.footage_matches(footage_id)

    @app.get("/api/footage/{footage_id}/unsynced")
    def footage_unsynced(footage_id: int):
        with open_db() as db:
            return db.unsynced_matches(footage_id)

    @app.post("/api/footage/{footage_id}/unsynced/{match_id}/demo")
    async def upload_demo(footage_id: int, match_id: int, request: Request):
        """Sync a match from a replay file sent as the request body (.dem, .dem.bz2 or zstd)."""
        with open_db() as db:
            item = db.get_footage(footage_id)
            row = next((m for m in db.unsynced_matches(footage_id) if m["match_id"] == match_id), None)
        if item is None or row is None:
            raise HTTPException(404, f"Match {match_id} isn't waiting for a replay in footage {footage_id}.")
        config.demo_dir.mkdir(parents=True, exist_ok=True)
        upload = config.demo_dir / f"upload-{match_id}"
        with upload.open("wb") as handle:
            async for chunk in request.stream():
                handle.write(chunk)

        def work(job: Job) -> dict:
            job.step(message=f"Reading the replay for match {match_id}")
            path = demo.prepare_upload(upload)
            try:
                parsed = events.parse(path)
                if parsed.match_id != match_id:
                    raise ValueError(f"That replay is match {parsed.match_id}, not {match_id}.")
            except Exception:
                path.unlink()
                raise
            path = path.replace(config.demo_dir / f"{match_id}.dem")
            source = service.frame_source(item)
            # Read the match's stretch of footage again for its clock.
            job.step(message=f"Finding match {match_id} in the footage")
            readings = scanner.scan(source, max(0.0, row["first_t"] - 60), row["last_t"] + 60, 30.0)
            span = next((s for s in scanner.group_matches(readings) if s.match_id == match_id), None)
            if span is None:
                raise ValueError(f"Couldn't read match {match_id} in the footage again.")
            with open_db() as db:
                outcome = service.sync_span(db, footage_id, source, span, config.demo_dir, (parsed, path))
            if outcome.result is None:
                raise ValueError(outcome.error)
            job.step(message=f"Synced match {match_id}")
            return {"footage_id": footage_id, "match_id": match_id}

        return jobs.start("sync", f"Sync match {match_id} from a replay", work).to_dict()

    @app.get("/api/footage/{footage_id}/file")
    def footage_file(footage_id: int):
        with open_db() as db:
            item = db.get_footage(footage_id)
        if item is None or not item.local_path or not Path(item.local_path).is_file():
            raise HTTPException(404, "This footage has no local file.")
        return FileResponse(item.local_path)

    @app.get("/api/footage/{footage_id}/hls/{name}")
    def footage_hls(footage_id: int, name: str):
        if not _SEGMENT_NAME.match(name):
            raise HTTPException(400, "Bad segment name.")
        url = media_playlist(footage_id)
        base = url.rsplit("/", 1)[0] + "/"
        if name == "index.m3u8":
            response = session.get(url, timeout=20)
            response.raise_for_status()
            # Segment names are relative, so the player fetches them back through here.
            return Response(response.text.replace(base, ""), media_type="application/vnd.apple.mpegurl")
        response = session.get(base + name, timeout=60)
        if response.status_code in (403, 404):
            for variant in hls._muted_variants(base + name):
                retry = session.get(variant, timeout=60)
                if retry.ok:
                    response = retry
                    break
        if not response.ok:
            raise HTTPException(response.status_code, "Twitch refused this segment.")
        return Response(response.content, media_type=response.headers.get("content-type", "video/mp4"))

    def media_playlist(footage_id: int) -> str:
        if footage_id not in playlist_urls:
            with open_db() as db:
                item = db.get_footage(footage_id)
            if item is None or not item.source_url:
                raise HTTPException(404, "This footage has no stream.")
            url = download.media_playlist_url(item.source_url)
            if url is None:
                raise HTTPException(400, "This footage isn't an HLS stream.")
            playlist_urls[footage_id] = url
        return playlist_urls[footage_id]

    # Matches

    @app.get("/api/matches/{match_id}")
    def match_detail(match_id: int):
        with open_db() as db:
            match = db.get_match(match_id)
            if match is None:
                raise HTTPException(404, f"No match {match_id}.")
            owners = {a: s for s in db.list_streamers() for a in s.steam_account_ids}
            footage = {f.id: f for f in db.list_footage()}
            players = [
                {**p, "streamer_id": owners[p["account_id"]].id if p["account_id"] in owners else None,
                 "streamer": owners[p["account_id"]].name if p["account_id"] in owners else None}
                for p in db.match_players(match_id)
            ]  # fmt: skip
            perspectives = []
            for row in db.footage_syncs(match_id):
                item = footage[row["footage_id"]]
                perspectives.append({
                    **row,
                    "streamer_id": item.streamer_id,
                    "streamer": db.get_streamer_by_id(item.streamer_id).name,
                    "video": f"/api/footage/{item.id}/file" if item.local_path
                    else f"/api/footage/{item.id}/hls/index.m3u8",
                    "is_hls": not item.local_path,
                })  # fmt: skip
            return {"match": match, "players": players, "events": db.match_events(match_id),
                    "perspectives": perspectives}  # fmt: skip

    # Exports

    @app.post("/api/exports")
    def export(body: ExportRequest):
        out_dir = config.data_dir / "exports" / str(body.match_id)
        with open_db() as db:
            main = _get_streamer(db, body.streamer_id)
            if body.windows is not None:
                if any(w.end_tick <= w.start_tick for w in body.windows):
                    raise HTTPException(400, "A clip has to end after it starts.")
                windows = [clips.Window(w.start_tick, w.end_tick, w.event_id, w.label) for w in body.windows]
                planned = clips.plan_windows(db, body.match_id, main, windows, out_dir, body.others)
            else:
                planned = clips.plan_clips(
                    db, body.match_id, main, body.event_ids, body.before, body.after, out_dir, body.others
                )
        if not planned:
            raise HTTPException(400, "None of those clips have synced footage to cut from.")

        def work(job: Job) -> dict:
            job.result = {
                "folder": str(out_dir.resolve()),
                "clips": [{"file": c.path.name, "streamer": c.streamer, "event_id": c.event_id,
                           "start": c.start, "end": c.end, "status": "waiting"} for c in planned],
            }  # fmt: skip
            job.step(0, len(planned), f"Cutting {len(planned)} clips")
            playlists: dict = {}
            failed = 0
            for i, (clip, row) in enumerate(zip(planned, job.result["clips"])):
                row["status"] = "cutting"
                try:
                    clips.cut(clip, playlists)
                    row["status"] = "done"
                except Exception as error:  # One bad clip shouldn't stop the rest.
                    row["status"] = f"failed: {error}"
                    failed += 1
                job.step(i + 1)
            job.step(message=f"{len(planned) - failed} of {len(planned)} clips saved")
            return job.result

        return jobs.start("export", f"Export {len(planned)} clips from match {body.match_id}", work).to_dict()

    @app.post("/api/open-folder")
    def open_folder(body: FolderRequest):
        path = Path(body.path).resolve()
        if not path.is_relative_to(config.data_dir.resolve()):
            raise HTTPException(400, "Only folders inside the data folder can be opened.")
        if not path.is_dir():
            raise HTTPException(404, f"{path} doesn't exist any more.")
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif "microsoft" in platform.release().lower() and shutil.which("explorer.exe"):
            # Under WSL, open it in Windows Explorer, which needs the Windows form of the path.
            windows_path = subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True).stdout.strip()
            subprocess.Popen(["explorer.exe", windows_path])
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
        return {"ok": True}

    # Jobs

    @app.get("/api/jobs")
    def list_jobs():
        return [job.to_dict() for job in jobs.all()]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"No job {job_id}.")
        return job.to_dict()

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def _get_streamer(db: Database, streamer_id: int) -> Streamer:
    try:
        return db.get_streamer_by_id(streamer_id)
    except KeyError:
        raise HTTPException(404, f"No streamer {streamer_id}.")


def _streamer_json(streamer: Streamer) -> dict:
    return {**asdict(streamer), "steam_account_ids": list(streamer.steam_account_ids)}
