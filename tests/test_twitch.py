import pytest

from vodagg.footage.twitch import GqlClient, HelixClient, TwitchError, WEB_CLIENT_ID, make_client, parse_video_id


@pytest.mark.parametrize(
    "value",
    ["123456", "v123456", "https://www.twitch.tv/videos/123456", "twitch.tv/somebody/video/123456?t=1h"],
)
def test_parse_video_id(value):
    assert parse_video_id(value) == "123456"


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, body):
        self.body = body
        self.sent = None

    def post(self, url, json, headers, timeout):
        self.sent = (url, json, headers)
        return FakeResponse(self.body)


NODE = {
    "id": "123",
    "title": "Deadlock ranked",
    "createdAt": "2023-11-14T22:13:20Z",
    "lengthSeconds": 3600,
    "broadcastType": "ARCHIVE",
    "owner": {"login": "mike"},
}


def test_gql_get_vod_uses_public_client_id():
    session = FakeSession({"data": {"video": NODE}})
    vod = GqlClient(session).get_vod("123")
    assert session.sent[2] == {"Client-ID": WEB_CLIENT_ID}
    assert (vod.login, vod.started_at, vod.duration_s, vod.broadcast_type) == ("mike", 1700000000, 3600, "archive")
    assert vod.url == "https://www.twitch.tv/videos/123"


def test_gql_missing_video():
    with pytest.raises(TwitchError):
        GqlClient(FakeSession({"data": {"video": None}})).get_vod("123")


def test_gql_list_vods():
    body = {"data": {"user": {"videos": {"edges": [{"node": NODE}]}}}}
    assert [v.video_id for v in GqlClient(FakeSession(body)).list_vods("mike")] == ["123"]


def test_gql_live_stream():
    body = {"data": {"user": {"stream": {"id": "999", "createdAt": "2023-11-14T22:13:20Z"}}}}
    stream = GqlClient(FakeSession(body)).get_live_stream("mike")
    assert (stream.broadcast_id, stream.started_at) == ("999", 1700000000)
    assert GqlClient(FakeSession({"data": {"user": {"stream": None}}})).get_live_stream("mike") is None


def test_make_client_prefers_helix_only_with_credentials():
    assert isinstance(make_client(), GqlClient)
    assert isinstance(make_client("id", None), GqlClient)
    assert isinstance(make_client("id", "secret"), HelixClient)
