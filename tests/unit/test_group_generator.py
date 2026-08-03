"""SR7 pattern-set emitter: N automata, ONE harness (structural invariants).

The behavioral truth for this emitter is ``tests/hw/xsim_diff.py --group ...``
(real RTL under xsim, replies compared byte-for-byte against the reference
composer, at datapath_bytes 1 and 8).  What lives here is everything that can
be checked without a simulator, plus the two things a simulator would not
notice:

  * :func:`pyro.hdl.generator.generate` must stay BYTE-IDENTICAL — the flashed
    S1 child, every cached bitstream and every existing test depend on the
    single-pattern emission not moving (R47b);
  * the group harness must be shared exactly once (that sharing is the SR7
    design: measured 452 -> 130 LUT/slot) and each automaton must carry its
    OWN closure bound (a group-wide max would reintroduce the 2.3.0 Vivado
    stall on every slot).
"""
import pytest

import pyro._circuit_model as cm
import pyro.hdl.automaton as _auto
import pyro.hdl.estimator as E
import pyro.hdl.generator as g
import pyro.hdl.identity as ident
import pyro.hdl.rp_wrapper as w

PAT = "abc[a-f]{2}"
HASH = "aabbccddeeff00112233445566778899"
GRP = [rb"abc", rb"bc", rb"c"]


# --------------------------------------------------------------------------
# generate() is untouched (the compatibility gate)
# --------------------------------------------------------------------------
def test_single_pattern_emission_is_unchanged():
    c = g.generate(rb"abc", 0)
    # Identity of the S1-era single-pattern circuit at generator 2.3.0.  If
    # this moves, every cached artifact and the flashed child are stale.
    assert c.pattern_hash16.hex() == "2cf49e0d02398a60bd20589220a584d3"
    assert c.num_patterns == 1
    # the single-pattern engine has no group harness anywhere in it
    for token in ("in_ready", "pend", "skid", "NSLOTS", "accv"):
        assert token not in c.rtl, token
    assert "module pyro_circuit (" in c.rtl


def test_group_does_not_perturb_the_single_pattern_path():
    before = g.generate(PAT, 0).rtl
    g.generate_group(GRP)
    g.generate_group(GRP, datapath_bytes=8)
    assert g.generate(PAT, 0).rtl == before


def test_group_of_one_is_not_the_single_pattern_circuit():
    """A 1-slot group is a DIFFERENT artifact from generate() and must not
    alias its cache key: same module name, different identity domain."""
    one = g.generate_group([rb"abc"])
    single = g.generate(rb"abc", 0)
    assert one.circ_id != single.circ_id
    assert one.rtl != single.rtl


# --------------------------------------------------------------------------
# structure: N automata, one harness
# --------------------------------------------------------------------------
def test_n_independent_state_vectors_and_one_shared_harness():
    c = g.generate_group(GRP)
    rtl = c.rtl
    assert c.num_patterns == 3 and c.n_slots == 3
    # N independent registered state vectors, N accept vectors
    for i in range(3):
        assert f"reg [NS{i}-1:0] st{i};" in rtl
        assert f"wire [0:0] accv{i} =" in rtl
    assert "reg [NS3-1:0] st3;" not in rtl
    # ONE of each shared harness element (CSR block, counters, ring writer)
    assert rtl.count("16'h0000: csr_rdata = MAGIC;") == 1
    assert rtl.count("reg  [63:0] byte_index;") == 1
    assert rtl.count("reg  [63:0] cycle_count;") == 1
    assert rtl.count("res_wr <= 1'b1;") == 1
    assert rtl.count("module pyro_circuit (") == 1
    assert rtl.count("endmodule") == 1


def test_each_automaton_gets_its_own_closure_bound():
    """A per-slot bound, not a group-wide max: the 2.3.0 fix was that the
    emitted loop count is closure_passes(au), and a shared max would put the
    deepest slot's replication cost on every slot."""
    pats = [rb"abc", rb"a?b?c?d", rb"(?:(?:a?)?)?b"]   # closure depth 1/2/3
    c = g.generate_group(pats)
    for i, au in enumerate(c.automata):
        k = g.closure_passes(au)
        assert f"for (it{i}_0 = 0; it{i}_0 < {k};" in c.rtl
        assert f"{k} relaxation pass(es)" in c.rtl
    bounds = {g.closure_passes(au) for au in c.automata}
    assert len(bounds) > 1, "fixture should span several closure depths"


def test_closure_loop_variables_are_not_shared_between_always_blocks():
    c = g.generate_group(GRP, datapath_bytes=8)
    for i in range(3):
        for j in range(9):          # n+1 closure stages at datapath_bytes=8
            assert c.rtl.count(f"integer it{i}_{j};") == 1


# --------------------------------------------------------------------------
# accept arbitration + the two hazards
# --------------------------------------------------------------------------
def test_pending_vector_priority_encoder_and_feed_stall():
    rtl = g.generate_group(GRP).rtl
    assert "reg  [NPEND-1:0] pend;" in rtl
    assert "assign in_ready = !pend_any && !skid_valid;" in rtl
    assert "if (pend[pe]) pend_idx = pe[" in rtl        # priority encoder
    assert "pend      <= acc_flat;" in rtl              # latch on consume
    assert "pend_base <= byte_index;" in rtl


def test_hazard_a_input_skid_holds_the_in_flight_beat():
    rtl = g.generate_group(GRP).rtl
    assert "if (in_valid && !in_ready && !skid_valid) begin" in rtl
    assert "skid_valid <= 1'b1;" in rtl
    assert "wire        fed_valid = skid_valid || in_valid;" in rtl
    assert "if (skid_valid) skid_valid <= 1'b0;" in rtl  # retire on consume
    assert "HAZARD (a)" in rtl


def test_hazard_b_overflow_still_retires_the_pending_entry():
    """The pend clear must be OUTSIDE the cap test.  If OVF skipped it, pend
    would never empty, in_ready would never rise and the scan would hang."""
    rtl = g.generate_group(GRP).rtl
    clear = rtl.index("pend[pend_idx] <= 1'b0;")
    captest = rtl.index("if (out_count < out_cap) begin")
    ovf = rtl.index("status <= status | 32'h0000_0008;  // OVF")
    assert clear < captest < ovf, (
        "clear must precede (and not be inside) the cap test")
    assert "HAZARD (b)" in rtl


def test_done_is_not_asserted_while_entries_are_pending():
    """The drain branch has priority over `finishing`, so STATUS.DONE (which
    is what the wrapper's ST_DRAIN polls) can only appear once the ring is
    complete."""
    rtl = g.generate_group(GRP).rtl
    drain = rtl.index("end else if (pend_any) begin")
    finish = rtl.index("end else if (finishing) begin")
    consume = rtl.index("end else if (running && fed_valid) begin")
    assert drain < finish < consume


# --------------------------------------------------------------------------
# pattern_id / slots / tombstones
# --------------------------------------------------------------------------
def test_pattern_id_is_the_slot_index_and_bounds_use_n_slots():
    c = g.generate_group([rb"abc", None, rb"c"])
    assert c.num_patterns == 3          # NOT the live count (2)
    assert c.n_slots == 3
    assert "localparam integer NSLOTS     = 3;" in c.rtl
    assert (c.circ_flags >> 16) == 3    # CIRC_FLAGS NUM_PAT (R45 0x0028)
    assert "res_pattern_id <= {" in c.rtl


def test_tombstoned_slot_keeps_its_index_and_emits_no_logic():
    c = g.generate_group([rb"abc", None, rb"c"])
    assert c.automata[1] is None
    assert "reg [NS1-1:0] st1;" not in c.rtl
    assert "wire [0:0] accv1 = {1{1'b0}};" in c.rtl
    # the live slots keep their own indices either side of the hole
    assert "reg [NS0-1:0] st0;" in c.rtl and "reg [NS2-1:0] st2;" in c.rtl


def test_tombstone_changes_identity_but_not_neighbour_slots():
    live = g.generate_group([rb"abc", rb"bc", rb"c"])
    dead = g.generate_group([rb"abc", None, rb"c"])
    assert live.circ_id != dead.circ_id
    assert "wire [0:0] accv2 = {(a2_1[ACCEPT2] & fed_keep[0])};" in dead.rtl


# --------------------------------------------------------------------------
# identity (SR7/SR14)
# --------------------------------------------------------------------------
def test_group_hash_is_baked_into_circ_id():
    digest = bytes(range(16))
    c = g.generate_group(GRP, group_hash=digest)
    assert c.pattern_hash16 == digest
    assert c.circ_id == ident.circ_id_words(digest)
    for i, word in enumerate(c.circ_id):
        assert f"localparam [31:0] CIRC_ID{i}    = 32'h{word:08X};" in c.rtl


def test_default_identity_is_deterministic_and_domain_separated():
    a = g.generate_group(GRP)
    assert a.circ_id == g.generate_group(GRP).circ_id
    # slot ORDER is part of the identity (slot index IS pattern_id)
    assert g.generate_group(list(reversed(GRP))).circ_id != a.circ_id
    # a tombstone never aliases a live slot
    assert g.generate_group([rb"abc", None]).circ_id != \
        g.generate_group([rb"abc", rb""]).circ_id
    # ... and never aliases a single-pattern R47a identity
    assert ident.pattern_set_hash([rb"abc"], [0], g.GENERATOR_VERSION,
                                  g.HARNESS_VERSION) != \
        ident.pattern_hash(rb"abc", 0, g.GENERATOR_VERSION, g.HARNESS_VERSION)


def test_datapath_width_rolls_the_default_identity_and_reports_in_caps0():
    c1 = g.generate_group(GRP, datapath_bytes=1)
    c8 = g.generate_group(GRP, datapath_bytes=8)
    assert c1.circ_id != c8.circ_id
    assert "CAPS0       = 32'h00010001;" in c1.rtl
    assert "CAPS0       = 32'h00010008;" in c8.rtl
    assert "input  wire [63:0] in_data," in c8.rtl
    assert "input  wire [7:0]  in_keep," in c8.rtl
    assert "in_keep" not in c1.rtl.replace("fed_keep", "")


def test_group_hash_must_be_128_bit():
    with pytest.raises(ValueError, match="16 bytes"):
        g.generate_group(GRP, group_hash=b"short")


# --------------------------------------------------------------------------
# argument validation
# --------------------------------------------------------------------------
def test_rejects_bad_width_empty_group_and_flag_mismatch():
    with pytest.raises(ValueError, match="datapath_bytes"):
        g.generate_group(GRP, datapath_bytes=3)
    with pytest.raises(ValueError, match="at least one slot"):
        g.generate_group([])
    with pytest.raises(ValueError, match="flags_per_pattern"):
        g.generate_group(GRP, flags_per_pattern=[0])
    with pytest.raises(ValueError, match="not HW-eligible"):
        g.generate_group([rb"(?P<x>a)(?P=x)"])       # backreference (R10)


def test_rejects_more_slots_than_max_patterns():
    from pyro import _classify
    too_many = [rb"a"] * (_classify.MAX_PATTERNS + 1)
    with pytest.raises(ValueError, match="MAX_PATTERNS"):
        g.generate_group(too_many)


# --------------------------------------------------------------------------
# the model consumes the emitter's artifact directly
# --------------------------------------------------------------------------
def test_generated_group_is_the_group_models_duck_type():
    c = g.generate_group([rb"abc", rb"bc", rb"c"])
    m = cm.GroupCircuitModel(c)
    m.resident = True
    entries, ovf = m.scan(b"xxabc")
    assert not ovf
    # all three slots accept on the SAME byte (end == 5) — the case the
    # priority encoder serializes; the model's ring order is (end, pattern_id).
    assert [(e.end, e.pattern_id) for e in entries] == [(5, 0), (5, 1), (5, 2)]
    assert m.n_slots == 3
    assert m.identity_block() == (*c.circ_id, c.circ_flags)


def test_model_and_emitter_agree_on_slot_count_with_tombstones():
    c = g.generate_group([rb"abc", None, rb"c"])
    m = cm.GroupCircuitModel(c)
    m.resident = True
    entries, _ = m.scan(b"abc")
    assert m.n_slots == 3                      # tombstone occupies index 1
    assert {e.pattern_id for e in entries} == {0, 2}


# --------------------------------------------------------------------------
# resource model
# --------------------------------------------------------------------------
def _fixed(est):
    """The estimate minus everything that scales with slots/width/automata."""
    return (est["luts"] - est["group_harness_luts"],
            est["ffs"] - est["group_harness_ffs"])


def test_estimate_group_shares_the_harness_and_ignores_tombstones():
    aus = [_auto.build(rb"abc", 0, _auto.ENC_BYTES) for _ in range(4)]
    one = E.estimate_group(aus[:1])
    four = E.estimate_group(aus)
    # the fixed harness is counted ONCE, not per slot (the SR7 sharing claim).
    # Compared on the automaton term only: the group harness's own `pend`
    # vector legitimately grows with the SLOT COUNT, so it is netted out here
    # and pinned by test_estimate_group_models_the_group_harness_registers.
    one_l, one_f = _fixed(one)
    four_l, four_f = _fixed(four)
    assert four_l - one_l == 3 * (one_l - E._HARNESS_LUTS)
    assert four_f - one_f == 3 * (one_f - E._HARNESS_FFS)
    with_dead = E.estimate_group(aus + [None])
    # A tombstone costs no automaton logic (SR6) ...
    assert _fixed(with_dead)[0] == four_l
    assert with_dead["num_patterns"] == 5 and with_dead["live_slots"] == 4
    # ... but it does keep its slot INDEX, and `pend` is indexed by slot, so it
    # is not free in the harness.  (4 slots -> stride 4; 5 -> stride 8.)
    assert with_dead["pend_bits"] == 8 and four["pend_bits"] == 4


def test_estimate_group_width_scales_luts_and_the_harness_not_state_flops():
    """R74 clause 2 for the SR7 harness: widening adds `pend`/skid FLOPS too.

    The state vector is one-hot and width-independent, but ``NPEND`` is
    ``datapath_bytes * 2**ceil(log2(n_slots))`` — 8x from dpb=1 to dpb=8 — and
    the skid widens with the beat.  An FF model that reported the same number
    at both widths would under-count the real design (measured ~48% low on a
    253-slot dpb=8 group), and `real <= est` is exactly what SR8 cites.
    """
    aus = [_auto.build(rb"abc", 0, _auto.ENC_BYTES) for _ in range(4)]
    e1, e8 = E.estimate_group(aus, 1), E.estimate_group(aus, 8)
    assert e8["ffs"] > e1["ffs"]
    # the STATE flops are what is width-independent, not the FF total
    assert _fixed(e8)[1] == _fixed(e1)[1]
    assert _fixed(e8)[0] - E._HARNESS_LUTS == 8 * \
                  (_fixed(e1)[0] - E._HARNESS_LUTS)
    assert e8["pend_bits"] == 8 * e1["pend_bits"]


def test_estimate_group_models_the_group_harness_registers():
    """Every register `_emit_rtl_group` declares outside the automata is
    counted.

    Read against the emitter: `pend` (NPEND), `pend_base` (64), and the 1-deep
    skid (`skid_data` 8*w, `skid_keep` w when w>1, `skid_valid`, `skid_last`).
    """
    aus = [_auto.build(rb"abc", 0, _auto.ENC_BYTES) for _ in range(253)]
    for width, stride in ((1, 256), (2, 256), (8, 256)):
        est = E.estimate_group(aus, width)
        npend = width * stride
        skid = 8 * width + 2 + (width if width > 1 else 0)
        assert est["pend_bits"] == npend
        assert est["group_harness_ffs"] == npend + 64 + skid
        # and the total clears the observed real-FF floor (n_states + the
        # 417-424 single-pattern intercept) plus every new register
        n_states = est["automaton_states"]
        assert est["ffs"] >= n_states + 424 + npend + 64 + skid


def test_ac_s2_2_sized_group_fits_the_rp_budget_at_dpb1():
    """253 slots of ~17 states each (the AC-S2-2 shape) at the decided
    datapath_bytes=1 must sit well inside SF2's 80,000 LUT / 160,000 FF."""
    aus = [_auto.build(rb"/index.php?page=", 0, _auto.ENC_BYTES)] * 253
    est = E.estimate_group(aus, 1)
    assert est["luts"] < E.PR_LUTS and est["ffs"] < E.PR_FFS
    assert est["num_patterns"] == 253


# --------------------------------------------------------------------------
# wrapper pairing
# --------------------------------------------------------------------------
def test_wrapper_is_byte_identical_unless_backpressure_is_asked_for():
    assert w.generate_rp_child(HASH) == \
        w.generate_rp_child(HASH, engine_backpressure=False)
    assert "eng_in_ready" not in w.generate_rp_child(HASH)
    assert w.generate_rp_child(HASH, datapath_bytes=8) == \
        w.generate_rp_child(HASH, datapath_bytes=8, engine_backpressure=False)


@pytest.mark.parametrize("dpb", [1, 8])
def test_backpressure_wrapper_connects_and_gates_the_feed(dpb):
    sv = w.generate_rp_child(HASH, datapath_bytes=dpb,
                             engine_backpressure=True)
    assert "wire        eng_in_ready;" in sv
    assert ".in_ready       (eng_in_ready)," in sv
    # HAZARD (a): the WHOLE feed body is frozen, so feed_idx cannot advance
    # past a beat the engine did not take.
    feed = sv[sv.index("ST_FEED: begin"):sv.index("// ---- wait for DONE")]
    assert "if (eng_in_ready) begin" in feed
    body = feed[feed.index("if (eng_in_ready) begin"):]
    for token in ("eng_in_valid <= 1'b1;", "feed_idx <= feed_idx + 16'd",
                  "state <= ST_DRAIN;"):
        assert token in body, token


def test_backpressure_survives_the_jumbo_rewrite():
    sv = w.generate_rp_child(HASH, datapath_bytes=8, max_frame_bytes=9600,
                             engine_backpressure=True)
    assert "eng_in_ready" in sv and "rx_words[feed_addr[13:6]]" in sv


# --------------------------------------------------------------------------
# the SNORT-PF seam: RuleGroup -> circuit
# --------------------------------------------------------------------------
def test_group_circuit_bakes_the_sr7_group_hash_not_the_fallback():
    """A group's baked identity MUST be the SR7 group hash (which covers the
    sidecar and the port class), never the emitter's pattern-list fallback —
    otherwise two groups with equal anchors and different gid:sid lists share
    a CIRC_ID and SR14 verifies the wrong group."""
    import pyro.snort.groups as G
    import pyro.snort.triage as T
    from pyro.snort.rules import parse_rule

    def mk(sid, content):
        return parse_rule(
            'alert tcp any any -> any $HTTP_PORTS (msg:"m"; content:"%s"; '
            'sid:%d; rev:1;)' % (content, sid), sid)

    def triaged(*rules):
        return [(r, T.triage_rule(r)) for r in rules]

    grp = G.pack_groups(triaged(mk(1, "aaa"), mk(2, "bbb")))[0]
    c = G.group_circuit(grp)
    assert c.pattern_hash16 == grp.group_hash(g.GENERATOR_VERSION,
                                              g.HARNESS_VERSION, 1)
    assert c.num_patterns == grp.n_slots == 2
    # ... and the fallback identity is NOT what got baked
    assert c.circ_id != g.generate_group([rb"aaa", rb"bbb"]).circ_id
    # slot order == pattern_id order == sidecar order
    assert list(grp.sidecar()) == list(range(c.num_patterns))


def test_group_circuit_lowers_nocase_in_bytes_mode():
    """The S1 silicon defect: a str-mode nocase lowering over-approximates to
    'any code point' and still passes completeness-only tests."""
    import pyro.snort.groups as G
    slot = G.GroupSlot(
    0, b"abc", True, (G.RuleRef(
        1, 1, "raw-anchor", "", 1),))
    grp = G.RuleGroup("$HTTP_PORTS", 0, (slot,))
    c = G.group_circuit(grp)
    assert c.enc == _auto.ENC_BYTES
    assert c.over_approx == ()          # exact 2-byte ASCII fold (R15)
    au = c.automata[0]
    assert au.n_states == 4             # a 3-byte literal, not an any-cp blob


def test_group_circuit_carries_tombstones_into_the_circuit():
    import pyro.snort.groups as G
    slots = (
        G.GroupSlot(0, b"aaa", False, (G.RuleRef(1, 1, "raw-anchor", "", 1),)),
        G.GroupSlot(1, b"bbb", False, ()),          # tombstone (SR6)
        G.GroupSlot(2, b"ccc", False, (G.RuleRef(1, 3, "raw-anchor", "", 3),)),
    )
    c = G.group_circuit(G.RuleGroup("any", 0, slots))
    assert c.num_patterns == 3 and c.automata[1] is None


# --------------------------------------------------------------------------
# The dangerous pairing (AC-S2-3 guard): a group engine + a deaf wrapper
# --------------------------------------------------------------------------
# An unconnected ``.in_ready`` output is legal Verilog, so this pairing
# elaborates, synthesizes, meets timing, and then drops a byte on every stall
# — false negatives with nothing on the wire and no counter to see them.  The
# engine cannot detect it (it cannot see the wrapper) and the wrapper cannot
# (it cannot see the engine), so the check lives at the one place both texts
# exist: the pr_bitstream build path.
def test_check_engine_pairing_passes_the_single_pattern_child():
    """The historical pairing must stay silent (R47b: nothing moves)."""
    engine = g.generate(PAT).rtl
    w.check_engine_pairing(engine, w.generate_rp_child(HASH))


def test_check_engine_pairing_passes_a_gated_group_child():
    engine = g.generate_group(GRP).rtl
    wrapper = w.generate_rp_child(HASH, engine_backpressure=True)
    assert "eng_in_ready" in wrapper
    w.check_engine_pairing(engine, wrapper)


def test_check_engine_pairing_rejects_a_group_engine_on_a_deaf_wrapper():
    engine = g.generate_group(GRP).rtl
    with pytest.raises(ValueError, match="in_ready"):
        w.check_engine_pairing(engine, w.generate_rp_child(HASH))


def test_group_engine_actually_declares_in_ready():
    """Guard the guard: if the port is ever renamed the check goes blind."""
    assert w._declares_in_ready(g.generate_group(GRP).rtl)
    assert not w._declares_in_ready(g.generate(PAT).rtl)
    # A commented-out port declaration must not count as a declaration.
    assert not w._declares_in_ready("  // output in_ready;\n")


def _pr_toolchain(tmp_path):
    """A pr_bitstream VivadoToolchain whose vivado is never actually run: the
    pairing guard fires before any tool time is spent."""
    from pyro.synth import ToolchainConfig
    from pyro.synth.toolchain import VivadoToolchain

    vdir = tmp_path / "vivado_install"
    (vdir / "bin").mkdir(parents=True)
    exe = vdir / "bin" / "vivado"
    exe.write_text("#!/bin/sh\nexit 1\n")
    exe.chmod(0o755)
    static = tmp_path / "static.dcp"
    static.write_bytes(b"locked static substrate")
    ref = tmp_path / "ref.dcp"
    ref.write_bytes(b"reference routed dcp")
    return VivadoToolchain(ToolchainConfig(
        kind="vivado", vivado_dir=str(vdir), pr_bitstream=True,
        static_dcp=str(static), reference_dcp=str(ref)))


def test_pr_build_path_derives_backpressure_from_the_engine_rtl(tmp_path):
    """A group-engine job that never set the flag gets a GATED wrapper anyway.

    ``engine_backpressure`` is not an independent build input — the engine text
    either declares ``in_ready`` or it does not — so ``_run_pr`` derives it and
    the deaf-wrapper pairing becomes unreachable rather than merely detected.
    The job therefore proceeds past the guard to the (deliberately failing)
    fake vivado.
    """
    import dataclasses
    from pyro.synth import SynthesisFailed
    from pyro.synth.residency import _job_from_circuit

    job = dataclasses.replace(_job_from_circuit(g.generate(PAT)),
                              rtl=g.generate_group(GRP).rtl)
    assert job.engine_backpressure is None          # nobody set it
    with pytest.raises(SynthesisFailed) as exc:     # the fake vivado, not us
        _pr_toolchain(tmp_path)._run_pr(job)
    assert "in_ready" not in str(exc.value)


def test_pr_build_path_refuses_a_contradicting_backpressure_flag(tmp_path):
    """An explicit flag that disagrees with the RTL is a CONFIG error."""
    import dataclasses
    from pyro.synth.toolchain import ConfigurationError
    from pyro.synth.residency import _job_from_circuit

    job = dataclasses.replace(_job_from_circuit(g.generate(PAT)),
                              engine_backpressure=True)   # engine has no port
    with pytest.raises(ConfigurationError, match="in_ready"):
        _pr_toolchain(tmp_path)._run_pr(job)


def test_pr_build_path_refuses_a_datapath_width_mismatch(tmp_path):
    """The OTHER seam axis: a dpb=8 engine under a dpb=1 wrapper.

    Measured under xsim: this elaborates and simulates cleanly and returns
    ZERO matches on every corpus (the engine's ``in_keep`` is left unconnected
    and ties low).  ``_run_pr`` takes the wrapper's width from
    ``job.datapath_bytes`` while the engine's lives inside ``job.rtl``, so the
    two can disagree exactly the way ``in_ready`` could.
    """
    import dataclasses
    from pyro.synth.toolchain import ConfigurationError
    from pyro.synth.residency import _job_from_circuit

    job = dataclasses.replace(              # width says 1, RTL says 8
        _job_from_circuit(g.generate(PAT)),
        rtl=g.generate_group(GRP, datapath_bytes=8).rtl, datapath_bytes=1)
    with pytest.raises(ConfigurationError, match="datapath width mismatch"):
        _pr_toolchain(tmp_path)._run_pr(job)


def test_a_pairing_error_is_not_a_cacheable_r65_negative():
    """R65 entries are PERMANENT and keyed without the pairing inputs (SR9).

    Caching a configuration error would poison the very key the corrected job
    has to reuse, so the worker must report it without touching the cache.
    """
    import inspect
    from pyro.synth import SynthesisFailed
    from pyro.synth.toolchain import ConfigurationError
    from pyro.synth import service as SVC

    # Distinct type: it must NOT be swept up by the SynthesisFailed handler.
    assert not issubclass(ConfigurationError, SynthesisFailed)
    src = inspect.getsource(SVC._worker_main)
    cfg_at = src.index("except ConfigurationError")
    fail_at = src.index("except SynthesisFailed")
    assert cfg_at < fail_at, "ConfigurationError must be caught first"
    body = src[cfg_at:fail_at]
    assert "put_failure" not in body, "a config error must not be cached (R65)"
    assert "STATUS_MISCONFIGURED" in body


def test_pr_build_path_still_accepts_a_single_pattern_job(tmp_path):
    """The guard must not fire on the historical pairing: the job proceeds far
    enough to reach the (deliberately failing) fake vivado, not the guard."""
    from pyro.synth import SynthesisFailed
    from pyro.synth.residency import _job_from_circuit

    job = _job_from_circuit(g.generate(PAT))
    with pytest.raises(SynthesisFailed) as exc:
        _pr_toolchain(tmp_path)._run_pr(job)
    assert "in_ready" not in str(exc.value)


@pytest.mark.parametrize("dpb", [1, 2, 8])
def test_check_engine_pairing_accepts_only_the_matching_width(dpb):
    """Width axis, both engine kinds, all supported widths."""
    for mk in (lambda n: g.generate_group(GRP, datapath_bytes=n).rtl,
               lambda n: g.generate(PAT, 0, datapath_bytes=n).rtl):
        engine = mk(dpb)
        bp = w._declares_in_ready(engine)
        for wid in (1, 2, 8):
            sv = w.generate_rp_child(HASH, datapath_bytes=wid,
                                     engine_backpressure=bp)
            if wid == dpb:
                w.check_engine_pairing(engine, sv)          # no raise
            else:
                with pytest.raises(ValueError, match="datapath width"):
                    w.check_engine_pairing(engine, sv)
