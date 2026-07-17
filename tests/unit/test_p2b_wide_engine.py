"""P2b widened datapath: generator + wrapper emission invariants (no Vivado).

The behavioral RTL truth lives in tests/hw/xsim_diff.py (xsim differential,
byte-exact replies for N in {1,2,4,8}); these are the fast structural checks:

  * N=1 emission keeps the pre-P2b identity and single-byte engine shape
    (no in_keep port) — no cache/identity rollover for existing artifacts;
  * N>1 rolls the R47a identity (DPB domain in the hash) and reports N in
    CAPS0 low16 (R42);
  * unsupported widths fail loud in both generator and wrapper;
  * the v2 wrapper is emitted verbatim for N=1 (anchored rewrite only for
    N>1, with kmask/eng_in_keep present).
"""
import pytest

import pyro.hdl.generator as g
import pyro.hdl.rp_wrapper as w

PAT = "abc[a-f]{2}"
HASH = "aabbccddeeff00112233445566778899"


def test_supported_widths_and_rejection():
    assert g.DATAPATH_BYTES == 1
    assert set(g.SUPPORTED_DATAPATH_BYTES) == {1, 2, 4, 8, 16}
    with pytest.raises(ValueError, match="datapath_bytes"):
        g.generate(PAT, 0, datapath_bytes=3)


def test_n1_engine_unchanged_shape_and_identity_stability():
    c = g.generate(PAT, 0)
    assert c.datapath_bytes == 1
    assert "in_keep" not in c.rtl
    assert "input  wire [7:0]  in_data," in c.rtl
    # identity must not depend on the default datapath kwarg pathway
    c2 = g.generate(PAT, 0, datapath_bytes=1)
    assert c2.circ_id == c.circ_id and c2.rtl == c.rtl


def test_wide_engine_identity_rolls_and_caps_reports_n():
    c1 = g.generate(PAT, 0)
    c8 = g.generate(PAT, 0, datapath_bytes=8)
    assert c8.circ_id != c1.circ_id, "wide circuit must not alias the 1B cache"
    assert c8.datapath_bytes == 8
    assert f"CAPS0       = 32'h0001000{8:x}".lower() in c8.rtl.lower()
    assert "input  wire [63:0] in_data," in c8.rtl
    assert "input  wire [7:0]  in_keep," in c8.rtl
    # distinct widths are distinct artifacts
    c4 = g.generate(PAT, 0, datapath_bytes=4)
    assert c4.circ_id not in (c1.circ_id, c8.circ_id)


def test_wrapper_v2_verbatim_and_v3_widened():
    v2 = w.generate_rp_child(HASH)
    assert "eng_in_keep" not in v2 and "kmask" not in v2
    assert v2 == w.generate_rp_child(HASH, datapath_bytes=1)
    v3 = w.generate_rp_child(HASH, datapath_bytes=8)
    assert "eng_in_keep" in v3 and "kmask" in v3
    assert ".in_keep        (eng_in_keep)," in v3
    assert "feed_idx + 16'd8" in v3
    with pytest.raises(AssertionError):
        w.generate_rp_child(HASH, datapath_bytes=16)  # would straddle words
