import bz2

import pytest
import zstandard

from vodagg.match.demo import match_time, prepare_demo, prepare_upload

PAYLOAD = b"PBDEMS2\x00" + bytes(range(256)) * 64


@pytest.mark.parametrize("compress", [bz2.compress, zstandard.compress])
def test_prepare_demo_decompresses_bz2_and_zstd(tmp_path, compress):
    # Valve names both formats .dem.bz2.
    source = tmp_path / "1.dem.bz2"
    source.write_bytes(compress(PAYLOAD))
    assert prepare_demo(source).read_bytes() == PAYLOAD


@pytest.mark.parametrize("compress", [bz2.compress, zstandard.compress, lambda data: data])
def test_prepare_upload_makes_a_dem_whatever_was_sent(tmp_path, compress):
    upload = tmp_path / "upload-1"
    upload.write_bytes(compress(PAYLOAD))
    path = prepare_upload(upload)
    assert path == tmp_path / "upload-1.dem" and path.read_bytes() == PAYLOAD and not upload.exists()


def test_prepare_upload_keeps_a_compressed_upload_named_dem(tmp_path):
    upload = tmp_path / "upload-1.dem"
    upload.write_bytes(zstandard.compress(PAYLOAD))
    assert prepare_upload(upload).read_bytes() == PAYLOAD


def test_match_time_reads_deadlock_api_start_time_as_utc():
    class Session:
        def get(self, url, params, timeout):
            assert params["match_ids"] == 118070191
            return type("Response", (), {
                "status_code": 200, "raise_for_status": lambda self: None,
                "json": lambda self: [{"match_id": 118070191, "start_time": "2026-10-16 16:49:10", "duration_s": 1306}],
            })()

    assert match_time(118070191, Session()) == (1792169350, 1306)
