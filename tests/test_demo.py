import bz2

import pytest
import zstandard

from vodagg.match.demo import prepare_demo, prepare_upload

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
