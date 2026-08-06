"""Multi-program (P0 + MAC P1) wrapper emission invariants (no Vivado).

The PYRO MAC contract adds a second co-resident program to the RP child:
P0 = the existing engine behind the proven core wrapper, P1 = the
SipHash-2-4 digest program (``pyro_mac_engine_top``), composed behind a
wire-frame scheduler top.  Behavioral RTL truth belongs to an xsim
differential harness; these are the fast structural checks in the
``test_p2b_wide_engine.py`` mold:

  * every pre-MAC invocation stays BYTE-IDENTICAL (regression safety —
    the flashed overlay child and every cached artifact are untouched);
  * ``mac_program=True`` emits the renamed core + the P1 program + the
    dispatcher top with the contract's kinds, CSRs and defaults;
  * invalid compositions fail loud;
  * the build-input source list is exported for the assembly sites.
"""
import os

import pytest

import pyro.hdl.rp_wrapper as w

HASH = "aabbccddeeff00112233445566778899"
OVERLAY_HASH = "0a5e000100000000000000000000a5e1"

# anchors that exist ONLY in the multi-program emission
_MAC_ANCHORS = ("pyro_rp_mac_prog", "pyro_mac_engine_top",
                "sched_mode", "records_lost", "reports_sent")


def test_old_mode_byte_identity_across_default_kwarg():
    v2 = w.generate_rp_child(HASH)
    assert v2 == w.generate_rp_child(HASH, mac_program=False)


def test_old_mode_emissions_carry_no_mac_text():
    for txt in (
        w.generate_rp_child(HASH),
        w.generate_rp_child(HASH, datapath_bytes=8),
        w.generate_rp_child(HASH, max_frame_bytes=9600,
                            engine_backpressure=True),
        w.generate_rp_child(HASH, cores=4),
    ):
        for anchor in _MAC_ANCHORS:
            assert anchor not in txt


def test_old_mode_overlay_invocation_unchanged():
    # the exact build_overlay_rm.sh invocation must not change
    a = w.generate_rp_child(OVERLAY_HASH, max_frame_bytes=9600,
                            engine_backpressure=True)
    b = w.generate_rp_child(OVERLAY_HASH, max_frame_bytes=9600,
                            engine_backpressure=True,
                            mac_program=False)
    assert a == b
    assert a.rstrip().endswith("endmodule : pyro_rp")


def test_mac_mode_emits_two_programs_and_a_top():
    t = w.generate_rp_child(HASH, mac_program=True)
    # P0: the proven core, renamed but otherwise the classic emission
    assert "module pyro_rp_core #(" in t
    assert "endmodule : pyro_rp_core" in t
    assert "pyro_circuit engine_inst (" in t
    # P1: the MAC program around the contract engine
    assert "module pyro_rp_mac_prog #(" in t
    assert "pyro_mac_engine_top mac_engine_inst (" in t
    # dispatcher top owns the R80 boundary
    assert "module pyro_rp #(" in t
    assert t.rstrip().endswith("endmodule : pyro_rp")
    # both program instances present
    assert ") u_prog0 (" in t and ") u_prog1 (" in t


def test_mac_mode_contract_surface():
    t = w.generate_rp_child(HASH, mac_program=True)
    # the seven new frame kinds, 0x0E..0x14
    for a in ("KIND_MAC_REPORT   = 8'h0E", "KIND_MAC_KEY_LOAD = 8'h0F",
              "KIND_MAC_KEY_ACK  = 8'h10", "KIND_SCHED_SET    = 8'h11",
              "KIND_SCHED_ACK    = 8'h12", "KIND_MAC_STAT_REQ = 8'h13",
              "KIND_MAC_STAT_REP = 8'h14"):
        assert a in t, a
    # the engine CSR block at 0x0090+
    for a in ("16'h0090", "16'h0094", "16'h0098", "16'h009C",
              "16'h00A0", "16'h00A4", "16'h00A8", "16'h00AC",
              "16'h00B0", "16'h00B4", "16'h00B8", "16'h00BC",
              "16'h00C0"):
        assert a in t, a
    # reset default schedule: round-robin, quantum 1 (contract)
    assert "sched_mode    <= 2'd2;" in t
    assert "sched_quantum <= 16'd1;" in t
    # 2^18-cycle flush timer and the 64-record batcher
    assert "18'h3FFFF" in t
    assert "rfifo [0:63]" in t
    # wire frames only enter the schedule via the tuser src tag
    assert "wire_tag = si_u0[22]" in t


def test_mac_mode_composes_with_existing_knobs():
    tj = w.generate_rp_child(HASH, mac_program=True,
                             max_frame_bytes=9600,
                             engine_backpressure=True)
    assert "pyro_rp_mac_prog" in tj
    assert "eng_in_ready" in tj          # SR7 gate stays in the core
    tw = w.generate_rp_child(HASH, mac_program=True, datapath_bytes=8)
    assert "kmask" in tw and "pyro_rp_mac_prog" in tw


def test_mac_mode_rejects_multicore():
    with pytest.raises(ValueError, match="cores"):
        w.generate_rp_child(HASH, mac_program=True, cores=2)


def test_mac_engine_source_list():
    srcs = w.mac_engine_sources()
    assert [os.path.basename(p) for p in srcs] == list(w.MAC_ENGINE_FILES)
    assert w.MAC_ENGINE_FILES == ("pyro_mac_engine_top.v",
                                  "pyro_mac_engine.v",
                                  "pyro_siphash.v")
    for p in srcs:
        assert os.path.sep + os.path.join("hw", "rtl") + os.path.sep in p
