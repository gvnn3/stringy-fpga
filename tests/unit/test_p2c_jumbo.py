"""P2c jumbo frame bound (R78.9a, v2.6.0): codec + wrapper emission invariants.

Behavioral truth is tests/hw/xsim_diff.py --max-frame 9600 (byte-exact
replies incl. a max-size corpus through the widened slices); these are the
fast checks:

  * codec defaults unchanged (1486 bound) — every pre-P2c call site
    byte-identical; jumbo accepted only via the explicit max_payload kwarg;
  * DeviceConfig.max_payload fail-closed default (R78.9a);
  * wrapper jumbo emission widens the word-index slices and only them;
    1536 emission byte-identical; invalid sizes fail loud.
"""
import pytest

import pyro.device as pdev
import pyro.hdl.rp_wrapper as w

HASH = "aabbccddeeff00112233445566778899"


def test_codec_default_bound_unchanged():
    assert pdev.MAX_PAYLOAD == 1486 and pdev.MAX_PAYLOAD_JUMBO == 9568
    big = b"\x00" * 1487
    with pytest.raises(pdev.PyroFrameError, match="1486"):
        pdev.encode_frame(pdev.KIND_MATCH_REQUEST, 1, 1, big)
    frame = pdev.encode_frame(pdev.KIND_MATCH_REQUEST, 1, 1, big,
                              max_payload=pdev.MAX_PAYLOAD_JUMBO)
    assert len(frame) == pdev.PYRO_HEADER_LEN + 1487
    with pytest.raises(pdev.PyroFrameError, match="9568"):
        pdev.encode_frame(pdev.KIND_MATCH_REQUEST, 1, 1, b"\x00" * 9569,
                          max_payload=pdev.MAX_PAYLOAD_JUMBO)


def test_decode_jumbo_roundtrip_and_default_reject():
    payload = b"\x5a" * 9000
    frame = pdev.encode_frame(pdev.KIND_MATCH_REQUEST, 1, 7, payload,
                              max_payload=pdev.MAX_PAYLOAD_JUMBO)
    with pytest.raises(pdev.PyroFrameError, match="1486"):
        pdev.decode_frame(frame)                      # fail-closed default
    dec = pdev.decode_frame(frame, max_payload=pdev.MAX_PAYLOAD_JUMBO)
    assert dec.length == 9000 and dec.payload == payload


def test_device_config_fail_closed():
    assert pdev.DeviceConfig().max_payload == pdev.MAX_PAYLOAD
    assert pdev.DeviceConfig(
        max_payload=pdev.MAX_PAYLOAD_JUMBO).max_payload == 9568


def test_wrapper_jumbo_slices_widened():
    v2 = w.generate_rp_child(HASH)
    assert v2 == w.generate_rp_child(HASH, max_frame_bytes=1536)
    j8 = w.generate_rp_child(HASH, datapath_bytes=8, max_frame_bytes=9600)
    assert "MAX_FRAME = 9600" in j8
    assert "rx_beat[7:0]" in j8 and "rx_beat[4:0]" not in j8
    assert "compose_idx[13:6]" in j8 and "compose_idx[10:6]" not in j8
    assert "feed_addr[13:6]" in j8 and "feed_addr[10:6]" not in j8
    assert "reg [7:0]   txw_sel;" in j8
    with pytest.raises(ValueError, match="max_frame_bytes"):
        w.generate_rp_child(HASH, max_frame_bytes=4096)
