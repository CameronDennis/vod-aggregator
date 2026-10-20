"""Twitch VOD and stream metadata.

The default client talks to Twitch's public web GraphQL endpoint with the same
client id the twitch.tv website uses, so it needs no API key. The Helix client
is an alternative for people who have registered an application and set
TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

import requests

from ..timeutil import parse_duration, parse_timestamp

GQL_URL = "https://gql.twitch.tv/gql"
# Public client id of the twitch.tv web player. Not a secret; yt-dlp and
# streamlink use the same one.
WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

HELIX_URL = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"

_VIDEO_URL = re.compile(r"(?:twitch\.tv/(?:\w+/)?videos?/|^v?)(\d+)")


@dataclass(frozen=True)
class VodInfo:
    video_id: str
    login: str
    title: str
    started_at: int
    duration_s: int
    broadcast_type: str

    @property
    def url(self) -> str:
        return f"https://www.twitch.tv/videos/{self.video_id}"


@dataclass(frozen=True)
class LiveStream:
    broadcast_id: str
    login: str
    started_at: int


class TwitchError(RuntimeError):
    pass


class TwitchClient(Protocol):
    def get_vod(self, video_id: str) -> VodInfo: ...

    def list_vods(self, login: str, limit: int = 20) -> list[VodInfo]: ...


def parse_video_id(value: str) -> str:
    """Video id from a bare id, 'v123' or a twitch.tv/videos/ URL."""
    match = _VIDEO_URL.search(value.strip())
    if not match:
        raise ValueError(f"not a Twitch video id or URL: {value!r}")
    return match.group(1)


class GqlClient:
    """No-key client for Twitch's public web GraphQL API."""

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    def _query(self, query: str, variables: dict) -> dict:
        response = self.session.post(
            GQL_URL,
            json={"query": query, "variables": variables},
            headers={"Client-ID": WEB_CLIENT_ID},
            timeout=20,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            raise TwitchError(f"Twitch GQL error: {body['errors']}")
        return body["data"]

    def get_vod(self, video_id: str) -> VodInfo:
        data = self._query(
            "query($id: ID!) { video(id: $id) {"
            " id title createdAt lengthSeconds broadcastType owner { login } } }",
            {"id": video_id},
        )
        if not data.get("video"):
            raise TwitchError(f"video {video_id} not found (deleted, private or sub-only)")
        return _vod_from_gql(data["video"])

    def list_vods(self, login: str, limit: int = 20) -> list[VodInfo]:
        data = self._query(
            "query($login: String!, $first: Int!) { user(login: $login) {"
            " videos(first: $first, type: ARCHIVE, sort: TIME) { edges { node {"
            " id title createdAt lengthSeconds broadcastType owner { login } } } } } }",
            {"login": login, "first": limit},
        )
        if not data.get("user"):
            raise TwitchError(f"no Twitch user {login!r}")
        return [_vod_from_gql(edge["node"]) for edge in data["user"]["videos"]["edges"]]

    def storage_hosts(self, login: str, limit: int = 10) -> list[str]:
        """Hosts serving the channel's current VODs, for deleted-VOD recovery."""
        data = self._query(
            "query($login: String!, $first: Int!) { user(login: $login) {"
            " videos(first: $first) { edges { node { seekPreviewsURL } } } } }",
            {"login": login, "first": limit},
        )
        edges = ((data.get("user") or {}).get("videos") or {}).get("edges") or []
        hosts = [urlparse(edge["node"].get("seekPreviewsURL") or "").hostname for edge in edges]
        return list(dict.fromkeys(host for host in hosts if host))

    def get_live_stream(self, login: str) -> LiveStream | None:
        """The current broadcast, if the channel is live.

        The broadcast id and start time are what deleted-VOD recovery needs,
        so it is worth recording them while a stream is still live.
        """
        data = self._query(
            "query($login: String!) { user(login: $login) { stream { id createdAt } } }",
            {"login": login},
        )
        stream = (data.get("user") or {}).get("stream")
        if not stream:
            return None
        return LiveStream(
            broadcast_id=stream["id"], login=login, started_at=parse_timestamp(stream["createdAt"])
        )


class HelixClient:
    """Client for the official Twitch API, using app credentials."""

    def __init__(self, client_id: str, client_secret: str, session: requests.Session | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session or requests.Session()
        self._token: str | None = None
        self._token_expires = 0.0

    def _headers(self) -> dict:
        if self._token is None or time.time() >= self._token_expires:
            response = self.session.post(
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
                timeout=20,
            )
            response.raise_for_status()
            body = response.json()
            self._token = body["access_token"]
            self._token_expires = time.time() + body.get("expires_in", 3600) - 60
        return {"Client-ID": self.client_id, "Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, params: dict) -> list[dict]:
        response = self.session.get(f"{HELIX_URL}/{path}", params=params, headers=self._headers(), timeout=20)
        response.raise_for_status()
        return response.json()["data"]

    def get_vod(self, video_id: str) -> VodInfo:
        videos = self._get("videos", {"id": video_id})
        if not videos:
            raise TwitchError(f"video {video_id} not found")
        return _vod_from_helix(videos[0])

    def list_vods(self, login: str, limit: int = 20) -> list[VodInfo]:
        users = self._get("users", {"login": login})
        if not users:
            raise TwitchError(f"no Twitch user {login!r}")
        videos = self._get("videos", {"user_id": users[0]["id"], "type": "archive", "first": limit})
        return [_vod_from_helix(video) for video in videos]


def make_client(client_id: str | None = None, client_secret: str | None = None) -> TwitchClient:
    if client_id and client_secret:
        return HelixClient(client_id, client_secret)
    return GqlClient()


def _vod_from_gql(node: dict) -> VodInfo:
    return VodInfo(
        video_id=node["id"],
        login=node["owner"]["login"],
        title=node.get("title") or "",
        started_at=parse_timestamp(node["createdAt"]),
        duration_s=int(node["lengthSeconds"]),
        broadcast_type=(node.get("broadcastType") or "").lower(),
    )


def _vod_from_helix(video: dict) -> VodInfo:
    return VodInfo(
        video_id=video["id"],
        login=video["user_login"],
        title=video.get("title") or "",
        started_at=parse_timestamp(video["created_at"]),
        duration_s=int(parse_duration(video["duration"])),  # e.g. "3h8m33s"
        broadcast_type=video.get("type", ""),
    )

