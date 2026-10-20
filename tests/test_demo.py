import bz2

import pytest
import zstandard

from vodagg.match.demo import prepare_demo

PAYLOAD = b"PBDEMS2\x00" + bytes(range(256)) * 64


@pytest.mark.parametrize("compress", [bz2.compress, zstandard.compress])
def test_prepare_demo_decompresses_bz2_and_zstd(tmp_path, compress):
    # Valve names both formats .dem.bz2.
    source = tmp_path / "1.dem.bz2"
    source.write_bytes(compress(PAYLOAD))
    assert prepare_demo(source).read_bytes() == PAYLOAD
