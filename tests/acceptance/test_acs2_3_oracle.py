"""AC-S2-3: the SR16 differential oracle + SR12 boundary fuzzing.

Two gates, in this order of authority:

**Gate 1 — the exact two-sided oracle (primary; device-free, Snort-free).**
Every slot of a group is a fixed-length literal, so for any byte stream the
expected nomination set is *computable in closed form* (a case-folded
``bytes.find`` sweep, :func:`snortpf_s2_support.oracle_windows`).  The tests
assert **set equality** — per slot, both directions — against the software
model of the generated circuit.  Containment would be worthless here: the
Phase-S1 silicon defect was a *complete* circuit that nominated at every
offset of every input (a str-mode ``nocase`` lowering), and it passed every
completeness-only check that existed.  ``test_oracle_rejects_a_match_...``
below re-creates that exact circuit and asserts this suite fails it; a
sabotaged (tombstoned) slot proves the other direction.  No false-positive-rate
heuristic is used as a gate anywhere: 23 of this group's 253 anchors are ≤ 5
bytes and fire legitimately and often, so an fp-rate tripwire is either blind
or noisy.

**Gate 1b — the emitted RTL (xsim).**  Gate 1 is exact about the *automata*:
the model consumes ``GeneratedGroup.automata`` and never reads ``.rtl``, so
none of the SR7 hardware (skid, ``pend``, priority encoder, ``in_ready``, the
per-cycle drain, the OVF retire) is exercised by it — verified by replacing
``generator._emit_rtl_group`` with a stub, which left the whole suite green.
``tests/hw/xsim_diff.py`` is therefore invoked *from this module* as the RTL
gate: real R78 frames through ``tb_pyro_rp.v`` under the pinned Vivado's xsim,
every reply compared byte-for-byte, at N=2/4/9 and datapath_bytes 1 and 8.
``test_the_rtl_gate_actually_bites`` sabotages the priority encoder to keep
that gate falsifiable, and
``test_the_rtl_reference_agrees_with_the_group_model``
pins that xsim's reference and gate 1's model are the same semantics for
fixed-length literal slots.  No Vivado is an honest SKIP, never a pass.

**Gate 2 — the Snort differential (SR16).**  Snort arbitrates *rule*
semantics; gate 1 already settled window arithmetic.  Recorded pcaps are
replayed through Snort 3 and through the nomination path, and completeness —
every ``(case, sid)`` Snort alerts on is nominated — is asserted:

* **absolutely for the ``raw-anchor`` sub-tier** (SR2), the only tier SR17
  would ever permit suppression for.  That is 42 of the 256 rules of this
  group; Snort confirms 32 of those sids on this corpus (38 ``(case, sid)``
  alerts) and the gate has zero exceptions — not "few", zero.
* **against a per-buffer budget PINNED AT THE MEASURED VALUE WITH ZERO SLACK
  for ``normalized-buffer``** (the other 214 rules).  Those anchors live in
  buffers Snort normalizes, so by construction (SF11) an encoded request can
  make Snort alert on bytes that never appear on the wire — the corpus
  contains two such cases (``%2d`` / ``%2e`` in a URI) and they are the entire
  budget.  Any increase fails the test.  These rules are *not* suppression
  candidates and SR5 keeps them harmless (a missed nomination only means the
  flow is unfiltered, and Snort sees all traffic anyway).

False positives are a **counted metric per SR4 class**, never a pass/fail
threshold — but the class *distribution* is pinned, because "some class
applies" is a tautology (every one of the 256 rules has a non-empty
``dropped_options`` list, ``flow:`` alone guaranteeing it).

**Anti-vacuity.**  The suite fails if it is under-powered: pinned minimum
counts of distinct alerted sids, true positives, raw-anchor alerts, cases with
alerts, and nominated slots.  An empty or broken corpus, a mis-parsed Snort
output, or a silently-not-running arbiter cannot pass trivially.

**SR18.**  Everything above is device-free.  The on-hardware clause is gated
on ``device_usable`` (and on the resident child actually being *this* group,
SR14) and records the canonical R83 SKIP string when it is not.  A SKIP is
never a PASS.

Re-baselining the pins is spec change control, exactly as for AC-S2-1: the
corpus SHA, the Snort version and the group shape are all pinned, so a pin
that moves means one of them moved.
"""

import os
import struct
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import snortpf_s2_support as S            # noqa: E402
from pyro.hdl import generator as GEN     # noqa: E402
from pyro.snort import groups as G        # noqa: E402
from pyro.snort import triage as T        # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.path.exists(S.CORPUS), reason="community ruleset not vendored")


# --------------------------------------------------------------------------
# Pinned expectations — MEASURED on 2026-07-28 with Snort 3.12.2.0 over the
# profiled corpus snapshot.  Changing any of these is a re-baseline, not a fix.
# --------------------------------------------------------------------------
GROUP_NAME = "$HTTP_PORTS/0"
N_SLOTS = 253
N_RULES = 256
OVERLAP_TAIL = 64                      # SR12: max anchor (65) - 1
N_CASES = 272                          # 19 designed + 253 per-anchor sweep

# SR16 differential (gate 2).
EXPECTED_TRUE_POSITIVES = 251
EXPECTED_ALERTED_SIDS = 210
EXPECTED_RAW_ANCHOR_ALERTS = 38        # (case, sid) alerts on raw-anchor rules
EXPECTED_RAW_ANCHOR_SIDS = 32          # distinct raw-anchor sids Snort confirms
EXPECTED_CASES_WITH_ALERTS = 223
EXPECTED_NOMINATED_SLOTS = 253         # every live slot is exercised

#: Deepest prefix chain among the group's anchors — the R41 ``start_off``
#: resume bound (see ``snortpf_s2_support.max_windows_per_start``).
MAX_WINDOWS_PER_START = 2

#: Completeness misses allowed for the ``normalized-buffer`` sub-tier, keyed by
#: sticky buffer.  ZERO SLACK: asserted by equality, so a new miss fails even
#: if it is "only" one more.  Both entries are the deliberate SF11 blinding
#: cases (percent-encoded URIs that http_inspect normalizes).
NORMALIZED_MISS_BUDGET = {("normalized-buffer", "http_uri"): 2}

#: SR4 over-approximation census (most-specific class per FP, fixed
#: precedence).  ``unclassified`` is pinned at 0, but be honest about what
#: that pin is worth: ``classify_fp``'s last rung (``dropped_conjuncts``)
#: fires whenever ``res.dropped_options`` is non-empty, and EVERY rule in this
#: group has a non-empty list (``flow:`` alone guarantees it), so
#: ``unclassified`` is UNREACHABLE for this group by construction — the same
#: tautology the module docstring says it avoided.  The 0 is kept because it
#: stops being a tautology the moment the group changes, and
#: ``test_unclassified_is_unreachable_by_construction_not_by_luck`` asserts
#: the construction itself, which is the part that carries information today.
EXPECTED_FP_CLASSES = {
    S.FP_HEADER: 2,          # the deliberate port-21 flow (SR13)
    S.FP_CASE_FOLD: 9,       # R15 ASCII fold widened the literal
    S.FP_ANCHOR_STRIP: 48,   # normalized buffer / positional modifiers dropped
    S.FP_DROPPED: 5,         # pcre / byte_test / second content not in circuit
    S.FP_UNCLASSIFIED: 0,
}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def group():
    from pyro.snort import report as SR
    assert SR._sha256_file(S.CORPUS) == SR.PROFILED_CORPUS_SHA256, (
        "corpus drifted from the profiled snapshot — every pin below was "
        "measured against it; re-baseline through spec change control (as "
        "AC-S2-1 does), do not edit the numbers")
    g = S.ac_s2_2_group()
    assert g.name == GROUP_NAME
    assert (g.n_slots, g.rule_count, g.overlap_tail) == (
        N_SLOTS, N_RULES, OVERLAP_TAIL)
    return g


@pytest.fixture(scope="module")
def model(group):
    return S.group_model(group, datapath_bytes=1)


@pytest.fixture(scope="module")
def cases(group):
    cs = S.all_cases(group)
    assert len(cs) == N_CASES
    ports = [c.sport for c in cs]
    assert len(set(ports)) == len(ports), "case client ports must be unique"
    return cs


@pytest.fixture(scope="module")
def nominations(group, model, cases):
    """The chunked (SR11/SR12) nomination path over every case, once."""
    return {c.name: S.nominate(model, c.segments, group.overlap_tail)
            for c in cases}


@pytest.fixture(scope="module")
def snort_alerts(group, cases, tmp_path_factory):
    ok, reason = S.snort_present()
    if not ok:
        pytest.skip(reason)                      # SR18: a SKIP is never a PASS
    work = str(tmp_path_factory.mktemp("acs23_snort"))
    rules = os.path.join(work, "group.rules")
    n = S.write_group_rules(group, rules)
    assert n == N_RULES
    pcap = S.write_pcap(os.path.join(work, "acs23.pcap"), cases)
    alerts = S.run_snort(rules, pcap, work)
    assert alerts, "Snort produced no alerts at all — the arbiter is not " \
                   "actually running (anti-vacuity)"
    return S.alerts_by_case(alerts, cases)


@pytest.fixture(scope="module")
def differential(group, model, cases, snort_alerts, nominations):
    return S.run_differential(group, model, cases, snort_alerts, nominations)


@pytest.fixture(scope="module")
def fuzz(group):
    sub = S.fuzz_subgroup(group, 12)
    return sub, S.group_model(sub, datapath_bytes=1)


# ==========================================================================
# Gate 1 — the exact two-sided oracle (device-free, Snort-free)
# ==========================================================================
def test_exact_oracle_set_equality_per_case(group, model, cases):
    """Model nominations == the closed-form expectation, per slot, both ways."""
    for case in cases:
        got = S.model_windows(model, case.stream)
        want = S.oracle_windows(group, case.stream)
        assert got == want, (
            "case %s: extra=%s missing=%s"
            % (case.name, sorted(got - want)[:5], sorted(want - got)[:5]))
        # Per-slot equality, so a compensating error (one slot's extra window
        # cancelling another's missing one) cannot hide in the totals.
        got_by_slot, want_by_slot = {}, {}
        for w in got:
            got_by_slot.setdefault(w.slot, set()).add((w.start, w.end))
        for w in want:
            want_by_slot.setdefault(w.slot, set()).add((w.start, w.end))
        assert got_by_slot == want_by_slot, case.name


def test_chunked_daemon_path_equals_the_whole_stream_oracle(
        group, cases, nominations):
    """SR11 chunking + SR12 tail lose nothing and invent nothing."""
    for case in cases:
        want = S.oracle_windows(group, case.stream)
        assert nominations[case.name] == want, case.name


def test_the_sr12_tail_is_load_bearing(group, model):
    """Same corpus with tail=0 MUST lose the split anchor.

    Proves the previous test is not passing for a trivial reason (e.g. every
    request happening to contain whole anchors anyway).
    """
    anchor = max((s for s in group.slots if not s.tombstone),
                 key=lambda s: s.length)
    filler = b"A" * 100
    stream = filler + anchor.anchor + filler
    # A chunk size that guillotines the anchor in half.
    cut = len(filler) + anchor.length // 2
    segs = (stream[:cut], stream[cut:])
    with_tail = S.nominate(model, segs, group.overlap_tail, chunk_bytes=cut)
    without = S.nominate(model, segs, 0, chunk_bytes=cut)
    want = S.oracle_windows(group, stream)
    assert with_tail == want
    assert any(w.slot == anchor.index for w in want)
    assert not any(w.slot == anchor.index for w in without), (
        "tail=0 still found the split anchor — the split is not real, so the "
        "SR12 test would pass vacuously")


def test_ring_order_is_end_then_pattern_id(group, model, cases):
    """Hardware writer order (R47/SR7): (end, pattern_id, start) ascending."""
    seen_multi = 0
    for case in cases:
        entries, _ovf = model.scan(case.stream, 0, 1 << 40)
        keys = [(m.end, m.pattern_id, m.start) for m in entries]
        assert keys == sorted(keys), case.name
        if len(entries) > 1:
            seen_multi += 1
    assert seen_multi >= 5, "no multi-entry case — ordering check is vacuous"


def test_oracle_rejects_a_match_everything_circuit(group):
    """The S1 silicon defect, re-created: str-mode ``nocase`` lowering.

    ``generate_group`` with *str* patterns and ``re.IGNORECASE`` (no
    ``re.ASCII``) takes automaton.py's OA_CROSS_LENGTH_CASEFOLD path and
    over-approximates to "any code point" — complete, and matching everything.
    The oracle MUST fail it.  If this test ever passes trivially (i.e. the
    sabotaged circuit produces the right set), the gate is not a gate.
    """
    import re as _re

    from pyro._circuit_model import GroupCircuitModel
    from pyro.hdl import automaton as _auto

    slots = [s for s in group.slots if s.nocase][:3]
    sub = G.RuleGroup(group.port_class, 901,
                      tuple(G.GroupSlot(i, s.anchor, s.nocase, s.rules)
                            for i, s in enumerate(slots)),
                      group.group_max)
    broken = GEN.generate_group(
        [_re.escape(s.anchor.decode("latin-1")) for s in slots],
        [_re.IGNORECASE] * len(slots),
        datapath_bytes=1, enc=_auto.ENC_UTF8)
    bad = GroupCircuitModel(broken)
    bad.resident = True

    benign = S._get(b"/index.html")
    want = S.oracle_windows(sub, benign)
    got = S.model_windows(bad, benign)
    assert want == set(), "the benign corpus must contain no anchor"
    assert got != want, (
        "the match-everything circuit passed the oracle — the gate is broken")
    assert len(got) > 20, (
        "expected the documented match-everything blow-up, got %d" % len(got))


def test_oracle_catches_a_missing_nomination(group, model, cases):
    """The other direction: a silently dropped slot must fail the oracle."""
    case = next(c for c in cases if c.name == "raw_pccs")
    windows = S.model_windows(model, case.stream)
    assert windows, "sabotage target case produces no nomination"
    victim = sorted(windows)[0].slot
    sabotaged = {w for w in windows if w.slot != victim}
    assert sabotaged != S.oracle_windows(group, case.stream), (
        "dropping slot %d went unnoticed — the oracle is one-sided" % victim)


def test_tombstoned_slots_never_nominate(group):
    """SR6: a tombstone keeps its ``pattern_id`` and can never match.

    The live AC-S2-2 group has no tombstone (nothing has been deleted from the
    corpus yet), so one is constructed from real slots — a skip here would be
    a hole in the oracle exactly where ``pattern_id`` stability lives.
    """
    live = [s for s in group.slots if not s.tombstone][:6]
    slots = []
    for i, s in enumerate(live):
        # Slot 2 loses every rule it served: index retained, rules emptied.
        slots.append(G.GroupSlot(i, s.anchor, s.nocase,
                                 () if i == 2 else s.rules))
    sub = G.RuleGroup(group.port_class, 902, tuple(slots), group.group_max)
    assert sub.slots[2].tombstone
    sub_model = S.group_model(sub, datapath_bytes=1)
    assert sub_model.automata[2] is None, "a tombstone emitted an automaton"

    stream = b"prefix " + b" ".join(s.anchor for s in live) + b" suffix"
    got = S.model_windows(sub_model, stream)
    want = S.oracle_windows(sub, stream)
    assert got == want
    assert 2 not in {w.slot for w in got}, "the tombstoned slot nominated"
    # The survivors keep their own indices either side of the hole — that is
    # the point of a tombstone (pattern_id must never shift under the sidecar).
    assert {w.slot for w in got} == {i for i in range(len(live)) if i != 2}
    # ...and the tombstone's anchor IS in the stream, so the slot stayed
    # silent because it is dead, not because nothing matched it.
    assert slots[2].anchor in stream


def test_adversarial_corpora_match_the_oracle_exactly(group, model):
    """Property fuzz (PYRO R55 pattern) aimed at the shared harness itself.

    Same exact gate, on corpora built to make several slots accept on the same
    byte, to force partial-match restarts, and to walk the whole 256-byte
    alphabet — the shapes the priority encoder / feed stall / skid exist for.
    """
    corpora = S.adversarial_corpora(group)
    total = 0
    for i, data in enumerate(corpora):
        want = S.oracle_windows(group, data)
        got = S.model_windows(model, data)
        assert got == want, ("corpus %d (%d B): extra=%s missing=%s"
                             % (i, len(data), sorted(got - want)[:4],
                                sorted(want - got)[:4]))
        total += len(want)
    assert total >= 100, "adversarial corpora produced only %d windows" % total


def test_adversarial_corpora_survive_random_segmentation(group, model):
    """The same corpora, re-cut into random TCP segments and chunk sizes."""
    for i, data in enumerate(S.adversarial_corpora(group)[:15]):
        want = S.oracle_windows(group, data)
        for chunk in (16, 64, 257, S.CHUNK_BYTES):
            segs = S.random_segmentation(data, seed=i * 97 + chunk)
            got = S.nominate(model, segs, group.overlap_tail,
                             chunk_bytes=chunk)
            assert got == want, ("corpus %d chunk %d: %s"
                                 % (i, chunk, sorted(got ^ want)[:4]))


def test_overflow_resume_recovers_every_nomination(fuzz):
    """R41/R78.7: an ``OVF`` reply is "no room", never "no match".

    Under a deliberately tiny ``out_cap`` the daemon must resume by
    ``start_off`` and end up with the *same* set the exact oracle predicts.
    Without the resume it returns a strict subset — the S1 protocol note-1
    trap, which on the wire looks exactly like a clean "no further matches".
    """
    sub, sub_model = fuzz
    data = b"".join(s.anchor for s in sub.slots) + b"/" * 20
    want = S.oracle_windows(sub, data)
    assert len(want) > 8, "corpus too small to overflow a tiny ring"
    entries, overflowed = sub_model.scan(data, 0, 2)
    assert overflowed and len(entries) == 2
    assert {S.Window(m.pattern_id, m.start, m.end) for m in entries} < want, (
        "the truncated ring is not a strict subset — nothing to resume for")
    for out_cap in (2, 3, 5):
        got = S.nominate_with_resume(sub_model, sub, (data,), out_cap=out_cap)
        assert got == want, (
            "out_cap=%d resume lost %s / invented %s"
            % (out_cap, sorted(want - got)[:4], sorted(got - want)[:4]))


def test_r41_resume_bound_is_measured_and_pinned(group, model):
    """The ``start_off`` resume is complete only above a measurable bound.

    Windows share a start offset exactly when one anchor is a prefix of
    another; a truncation that splits such a group is unrecoverable by
    ``start_off`` alone.  For the AC-S2-2 group the deepest prefix chain is
    2 (``/whois_raw.cgi`` ⊂ ``/whois_raw.cgi?``) against R78.7's ``out_cap``
    ceiling of 61, so the daemon is safe — but the bound is *checked*, and the
    loss below it is *demonstrated*, so it never becomes folklore.
    """
    depth, chain = S.max_windows_per_start(group)
    assert depth == MAX_WINDOWS_PER_START, (depth, chain)
    assert depth < 61, "R78.7 out_cap ceiling no longer covers the group"

    # Demonstrate the loss at out_cap below the bound, on the real pair.
    victims = sorted(chain)
    data = b"x" * 8 + group.slots[victims[0]].anchor + b"y" * 8
    want = S.oracle_windows(group, data)
    assert len({w.start for w in want}) < len(want), (
        "the chosen corpus has no start-tie, so the demonstration is vacuous")
    got = S.nominate_with_resume(model, group, (data,), out_cap=1)
    assert got < want, (
        "out_cap=1 recovered everything — the documented resume bound is "
        "wrong, which is better news than the docstring but must be fixed")
    assert S.nominate_with_resume(model, group, (data,),
                                  out_cap=MAX_WINDOWS_PER_START) == want


def test_resume_bound_covers_the_nocase_over_uppercase_quadrant():
    """The bound must hold for rulesets the AC-S2-2 corpus cannot reach.

    ``max_windows_per_start`` compares a *folded* longer anchor against the
    *raw* shorter one.  When the longer slot is ``nocase`` and the shorter is
    case-sensitive **with uppercase bytes** — ``CFUSION_ENCRYPT``/i over
    ``CFUSION_``, an ordinary Snort shape — the raw comparison returns False
    and the reported depth is short, so a daemon resuming at exactly the value
    the function reports drops a nomination with nothing on the wire.  The
    AC-S2-2 group never enters that quadrant (its true depth is 2, equal to
    the pinned value), which is why the test above cannot see this.

    The contract asserted here is the function's own: resuming at exactly the
    reported bound is complete, and one below it is not.
    """
    ref = (G.RuleRef(1, 900001, "raw-anchor", "pkt_data", 1),)
    anchors = [(b"cfusion_encrypt", True), (b"CFUSION", False),
               (b"CFUSION_", False), (b"CFUSION_E", False)]
    slots = tuple(G.GroupSlot(i, a, nc, ref)
                  for i, (a, nc) in enumerate(anchors))
    sub = G.RuleGroup("$HTTP_PORTS", 901, slots)
    depth, chain = S.max_windows_per_start(sub)
    assert depth == 4 and sorted(chain) == [0, 1, 2, 3], (depth, chain)

    data = b"q=CFUSION_ENCRYPT&z=1"
    sub_model = S.group_model(sub)
    want = S.oracle_windows(sub, data)
    assert len(want) == 4 and len({w.start for w in want}) == 1
    # exactly the reported bound is complete ...
    assert S.nominate_with_resume(
    sub_model, sub, (data,), out_cap=depth) == want
    # ... and one below it is not (so the bound is tight, not merely safe)
    assert S.nominate_with_resume(
        sub_model, sub, (data,), out_cap=depth - 1) < want


def test_the_resume_is_something_the_wrapper_can_actually_do(fuzz):
    """R41/R78.7 resume must not depend on a device-side ``start_off``.

    The emitted ``pyro_rp`` never reads the ``MATCH_REQUEST`` ``start_off``
    field (bytes 28..35): ``ST_WAIT_BUSY`` zeroes ``feed_idx`` and ``ST_FEED``
    feeds from ``40 + feed_idx``, so the corpus is always scanned from byte 0
    and every wire caller sends ``start_off = 0``.  A daemon that re-issued the
    IDENTICAL request with a bumped ``start_off`` would get the identical
    truncated ring forever — livelock, or silent loss past ``out_cap``.

    Two assertions.  (1) The wrapper really is deaf to the field, so this test
    re-baselines loudly if hardware ever grows the mechanism.  (2) The resume
    that ``nominate_with_resume`` models — re-send the TRIMMED corpus, treat
    ``start_off`` as a host-side label — is complete on the same corpora.
    """
    from pyro.hdl import rp_wrapper as W

    sv = W.generate_rp_child("00" * 16, engine_backpressure=True)
    assert "start_off" not in sv, (
        "the wrapper now mentions start_off — if the device honours it, the "
        "resume model may be re-stated in terms of a scan origin (and this "
        "test re-baselined); until then the trimmed-corpus resume is the only "
        "one that works on the wire")

    sub, sub_model = fuzz
    data = b"".join(s.anchor for s in sub.slots) + b"/" * 20
    want = S.oracle_windows(sub, data)
    for out_cap in (2, 3, 5):
        # Same buffer, header start_off pinned at 0, payload trimmed by the
        # host — byte-for-byte what scripts/pyro_hw.py can put on the wire.
        got = S.nominate_with_resume(sub_model, sub, (data,), out_cap=out_cap)
        assert got == want, out_cap


def test_corpus_is_not_vacuous(group, cases, nominations):
    """Anti-vacuity for gate 1: the corpora must actually exercise the group."""
    slots = {w.slot for ws in nominations.values() for w in ws}
    total = sum(len(ws) for ws in nominations.values())
    assert len(slots) >= EXPECTED_NOMINATED_SLOTS, (
        "only %d of %d slots nominated anywhere" % (len(slots), N_SLOTS))
    assert total >= 300, "only %d nominations across the whole corpus" % total
    assert any(not S.oracle_windows(group, c.stream) for c in cases), (
        "no negative case — a match-everything circuit could still look right")


# ==========================================================================
# Gate 1b — the EMITTED RTL (xsim; the only gate that executes the Verilog)
# ==========================================================================
# Gate 1 above is exact, but it is exact about the *automata*: `S.group_model`
# builds the circuit and consumes only `.automata`, so `.rtl` is generated and
# thrown away.  Every piece of SR7 hardware — the 1-deep skid, the `pend`
# vector, the priority encoder, `in_ready`, the per-cycle drain, the OVF
# retire — is invisible to it.  That was demonstrated, not argued: replacing
# `generator._emit_rtl_group` with a stub that emits `module pyro_circuit ();`
# left all 23 tests of this module passing.
#
# So this section runs the emitted Verilog.  `tests/hw/xsim_diff.py` builds the
# engine + the `pyro_rp` wrapper, drives real R78 frames through
# `tb_pyro_rp.v` under the pinned Vivado's xsim, and compares every MATCH_REPLY
# byte-for-byte.  It used to be a standalone script no test invoked; it is now
# THE AC-S2-3 RTL gate, and `test_the_rtl_gate_actually_bites` sabotages the
# emitter to prove the gate is not vacuous.  Vivado-absent is an honest SKIP
# (R83/SR18), never a pass.
XSIM = os.path.join(
    os.path.dirname(
        os.path.dirname(
            os.path.abspath(__file__))),
            "hw",
             "xsim_diff.py")


def _xsim_diff():
    """Import ``tests/hw/xsim_diff.py`` as a module (it is not a package)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("pyro_xsim_diff", XSIM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_xsim(argv):
    """Run one differential in-process; SKIP (never pass) when Vivado is
    absent."""
    mod = _xsim_diff()
    rc = mod.main(argv)
    if rc == 2:
        pytest.skip("SKIP: device_usable=false — no Vivado at the R70a pin "
                    "(%s); the AC-S2-3 RTL gate did not run"
                    % os.environ.get("PYRO_VIVADO", "pinned default"))
    return rc


#: (name, --group spec, --datapath).  N=2 and N=4 at the shipped dpb=1 are the
#: mandated shapes; the 9-slot case adds a TOMBSTONE (empty element, SR6) and a
#: nocase slot; the dpb=8 case gates the wide cascade and the in_keep lanes.
XSIM_GROUPS = [
    ("n2_dpb1", "abc,bc", 1),
    ("n4_dpb1", "abc,bc,c,ab", 1),
    ("n9_tombstone_nocase_dpb1", "abc,bc,c,ab,a,b,,cab,ABC/i", 1),
    ("n4_dpb8", "abc,bc,c,ab", 8),
]


@pytest.mark.parametrize("name,spec,dpb", XSIM_GROUPS,
                         ids=[c[0] for c in XSIM_GROUPS])
def test_emitted_group_rtl_matches_the_reference_under_xsim(name, spec, dpb):
    """The SR7 Verilog itself, byte-for-byte, on real R78 frames.

    Corpora inside ``xsim_diff`` are chosen for SIMULTANEOUS accepts (``abc``
    makes three slots accept on one byte), back-to-back accepts, accepts on the
    final byte, ``out_cap=1`` truncation and ``out_cap=0`` (every accept
    dropped — HAZARD (b): the entries must still retire from ``pend`` or DONE
    never arrives and the reply never comes).
    """
    assert _run_xsim(["--group", spec, "--datapath", str(dpb)]) == 0


def test_the_rtl_gate_actually_bites():
    """Anti-vacuity for gate 1b: sabotage the emitter, the gate must FAIL.

    Without this, "the emitter survives xsim" is unfalsifiable.  The sabotage
    reverses the SR7 priority encoder's drain direction, so slots that accept
    on the SAME byte retire highest-index-first instead of lowest — the exact
    ring order ``GroupCircuitModel`` pins and the daemon's sidecar attribution
    depends on.  It elaborates, simulates and terminates perfectly, which is
    the point: the gate has to catch a *semantic* defect, not a syntax error.
    (Sabotaging ``in_ready`` instead is caught too, but by hanging the
    simulator to the 600 s subprocess timeout — a slow, less specific signal.)
    """
    mod = _xsim_diff()
    real = GEN._emit_rtl_group

    def sabotaged(*a, **kw):
        rtl = real(*a, **kw)
        broken = rtl.replace(
            "for (pe = NPEND-1; pe >= 0; pe = pe - 1)",
            "for (pe = 0; pe < NPEND; pe = pe + 1)")   # SABOTAGE: ring order
        assert broken != rtl, "sabotage anchor drifted — the test is vacuous"
        return broken

    GEN._emit_rtl_group = sabotaged
    try:
        rc = mod.main(["--group", "abc,bc,c", "--datapath", "1"])
    finally:
        GEN._emit_rtl_group = real
    if rc == 2:
        pytest.skip("SKIP: device_usable=false — no Vivado at the R70a pin; "
                    "the AC-S2-3 RTL gate did not run")
    assert rc == 1, ("the xsim gate PASSED a group engine whose priority "
                     "encoder drains backwards — gate 1b is vacuous")


def test_the_rtl_reference_agrees_with_the_group_model(group, model):
    """The two semantics AC-S2-3 relies on are the same one for these slots.

    ``xsim_diff`` diffs the RTL against ``group_entries`` (a continuous-seeding
    NFA end sweep), while gate 1 validates ``GroupCircuitModel`` (per-start
    leftmost-longest ``_scan_windows``).  Those are genuinely different
    definitions in general — they diverge for ``+``/``*`` slots — so the RTL
    gate would otherwise be diffed against a model this suite never checked.
    Every SNORT-PF slot is a fixed-length literal, for which they coincide;
    this pins that, on the real group's adversarial corpora, by ``end`` and
    ``pattern_id`` (the fields hardware reports exactly — see
    ``GroupCircuitModel``'s note on ``start``).
    """
    mod = _xsim_diff()
    automata = G.group_circuit(group, datapath_bytes=1).automata
    for i, data in enumerate(S.adversarial_corpora(group)[:6]):
        want = sorted(mod.group_entries(automata, data))
        entries, ovf = model.scan(data, 0, 1 << 40)
        assert not ovf
        got = sorted((m.end, m.pattern_id) for m in entries)
        assert got == want, (
            "corpus %d: the xsim reference and the group "
            "model disagree: %s"
            % (i, sorted(set(got) ^ set(want))[:6]))


# ==========================================================================
# SR12 boundary fuzzing (device-free)
# ==========================================================================
def test_chunk_boundary_split_at_every_offset(fuzz):
    """Every split offset of every fuzz anchor, at the real 1,474 B chunk."""
    sub, sub_model = fuzz
    tail = sub.overlap_tail
    checked = 0
    for slot in sub.slots:
        anchor = slot.anchor
        for k in range(1, len(anchor)):
            head = b"x" * (S.CHUNK_BYTES - k)
            stream = head + anchor + b"y" * 40
            got = S.nominate(sub_model, (stream,), tail)
            want = S.oracle_windows(sub, stream)
            assert got == want, (
                "slot %d split at %d: extra=%s missing=%s"
                % (slot.index, k, sorted(got - want)[:3],
                   sorted(want - got)[:3]))
            assert any(w.slot == slot.index for w in got), (
                "slot %d lost at split offset %d" % (slot.index, k))
            checked += 1
    assert checked >= 50, "fuzz did too little work (%d splits)" % checked


def test_tcp_segment_split_at_every_offset(fuzz):
    """SR12 across TCP segment boundaries: two segments, every split point."""
    sub, sub_model = fuzz
    tail = sub.overlap_tail
    for slot in sub.slots:
        anchor = slot.anchor
        for k in range(1, len(anchor)):
            stream = b"GET /a " + anchor + b" HTTP/1.1\r\n"
            cut = stream.index(anchor) + k
            segs = (stream[:cut], stream[cut:])
            got = S.nominate(sub_model, segs, tail)
            want = S.oracle_windows(sub, stream)
            assert got == want, ("slot %d segment-split at %d: %s"
                                 % (slot.index, k, sorted(got ^ want)[:3]))


def test_case_permutation_is_exactly_the_ascii_fold(fuzz):
    """R15, two-sided: nocase slots fire on every ASCII case permutation and
    case-sensitive slots fire on NONE but the exact bytes."""
    sub, sub_model = fuzz
    tail = sub.overlap_tail
    for slot in sub.slots:
        for perm in S.case_permutations(slot.anchor):
            stream = b"prefix " + perm + b" suffix"
            got = S.nominate(sub_model, (stream,), tail)
            assert got == S.oracle_windows(sub, stream), (slot.index, perm)
            fired = any(w.slot == slot.index for w in got)
            if slot.nocase:
                assert fired, ("nocase slot %d missed permutation %r"
                               % (slot.index, perm))
            elif slot.anchor not in stream:
                # Case-sensitive: the fold must NOT have widened the literal.
                assert not fired, (
                    "case-sensitive slot %d fired on %r" % (slot.index, perm))


def test_case_permuted_split_anchors_still_fire(fuzz):
    """The two hazards together: a case-permuted anchor split across chunks."""
    sub, sub_model = fuzz
    tail = sub.overlap_tail
    for slot in sub.slots:
        if not slot.nocase:
            continue
        perm = S.case_permutations(slot.anchor)[1]      # ALL-UPPER
        for k in range(1, len(perm)):
            head = b"x" * (S.CHUNK_BYTES - k)
            stream = head + perm + b"y" * 8
            got = S.nominate(sub_model, (stream,), tail)
            assert got == S.oracle_windows(sub, stream)
            assert any(w.slot == slot.index for w in got), (slot.index, k)


def test_full_group_boundary_split(group, model):
    """The same split property at N=253, on a smaller chunk to stay in
    budget."""
    slot = max((s for s in group.slots if not s.tombstone),
               key=lambda s: s.length)
    chunk = 256
    for k in range(1, slot.length):
        head = b"x" * (chunk - k)
        stream = head + slot.anchor + b"y" * 16
        got = S.nominate(model, (stream,), group.overlap_tail,
                         chunk_bytes=chunk)
        want = S.oracle_windows(group, stream)
        assert got == want, (k, sorted(got ^ want)[:3])
        assert any(w.slot == slot.index for w in got), k


# ==========================================================================
# Gate 2 — the Snort differential (SR16)
# ==========================================================================
def test_raw_anchor_completeness_is_absolute(differential):
    """SR16/SR3: zero completeness diffs for the raw-anchor sub-tier."""
    raw_misses = [m for m in differential.misses if m[3] == T.SUBTIER_RAW]
    assert raw_misses == [], (
        "raw-anchor completeness defect (SR3 is absolute for this tier): %r"
        % (raw_misses,))
    assert differential.alerted_raw_anchor >= EXPECTED_RAW_ANCHOR_ALERTS, (
        "only %d raw-anchor alerts — the absolute gate is under-powered"
        % differential.alerted_raw_anchor)
    assert len(differential.alerted_raw_sids) >= EXPECTED_RAW_ANCHOR_SIDS, (
        "only %d distinct raw-anchor sids confirmed by Snort (of the 42 in "
        "the group) — the absolute gate covers too little"
        % len(differential.alerted_raw_sids))


def test_normalized_buffer_misses_are_within_the_pinned_budget(differential):
    """SF11 blinding, pinned WITH ZERO SLACK (equality, not ``<=``)."""
    budget = {}
    for _case, _gid, _sid, subtier, buffer in differential.misses:
        assert subtier == T.SUBTIER_NORMALIZED, (
            "a non-normalized miss reached the budget test: %s/%s"
            % (subtier, buffer))
        budget[(subtier, buffer)] = budget.get((subtier, buffer), 0) + 1
    assert budget == NORMALIZED_MISS_BUDGET, (
        "normalized-buffer miss census moved: %r (pinned %r); misses=%r"
        % (budget, NORMALIZED_MISS_BUDGET, differential.misses))


def test_false_positive_class_distribution_is_pinned(differential):
    """SR4: FPs are a counted metric, classified most-specific-first."""
    assert differential.fp_by_class == EXPECTED_FP_CLASSES, (
        "SR4 class census moved: %r (pinned %r)"
        % (differential.fp_by_class, EXPECTED_FP_CLASSES))
    assert differential.fp_by_class[S.FP_UNCLASSIFIED] == 0, (
        "a nomination with no over-approximation class is a defect: %r"
        % [r for r in differential.fp_rows if r[3] == S.FP_UNCLASSIFIED])


def test_unclassified_is_unreachable_by_construction_not_by_luck(group):
    """Say out loud why ``FP_UNCLASSIFIED == 0`` cannot fail on this group.

    ``classify_fp``'s catch-all rung fires on any non-empty
    ``dropped_options``, so the count above is 0 for a structural reason, not
    an observed one.  Assert the structure instead — that is the statement
    with content — so the day a group appears whose rules drop nothing, this
    test fails and the ``unclassified`` pin becomes a real gate again.
    """
    empty = [(r.gid, r.sid) for s in group.slots if not s.tombstone
             for r in s.rules
             if not S.rule_by_line(r.line_no)[1].dropped_options]
    assert not empty, (
        "%d rule(s) drop no conjuncts, so FP_UNCLASSIFIED is now REACHABLE "
        "and its pinned 0 is a real assertion — re-read it: %r"
        % (len(empty), empty[:5]))


def test_every_fp_class_is_actually_exercised(differential):
    """The classifier must not be a one-armed if: each defined class is hit."""
    for cls in (S.FP_HEADER, S.FP_CASE_FOLD, S.FP_ANCHOR_STRIP, S.FP_DROPPED):
        assert differential.fp_by_class[cls] > 0, (
            "SR4 class %s never exercised — its precedence rung is untested"
            % cls)


def test_differential_is_not_vacuous(differential, cases):
    """Anti-vacuity for gate 2: pinned minima on the arbiter's own output."""
    assert differential.true_positives >= EXPECTED_TRUE_POSITIVES, (
        "only %d true positives" % differential.true_positives)
    assert len(differential.alerted_sids) >= EXPECTED_ALERTED_SIDS, (
        "only %d distinct sids alerted" % len(differential.alerted_sids))
    assert differential.cases_with_alerts >= EXPECTED_CASES_WITH_ALERTS, (
        "only %d of %d cases produced an alert"
        % (differential.cases_with_alerts, len(cases)))


def test_snort_and_the_daemon_share_one_variable_table(group):
    """SR13: the host-side table mirrors the arbiter's, or the differential is
    comparing two different rulesets."""
    assert 80 in S.HTTP_PORTS and 8080 in S.HTTP_PORTS
    assert 21 not in S.HTTP_PORTS       # the deliberate header_predicate case
    wrong = next(c for c in S.build_cases() if c.name == "wrong_port_ftp")
    index = S.rule_index(group)
    for (gid, sid), (_slot, ref) in list(index.items())[:20]:
        rule, _res = S.rule_by_line(ref.line_no)
        assert not S.header_holds(rule, wrong.flow), (gid, sid)


# ==========================================================================
# SR18 — the on-hardware clause
# ==========================================================================
def _hw_gate(device_iface):
    """(config, reason) — reason is the canonical R83 string when unusable."""
    from pyro import device as pdev
    cfg = pdev.DeviceConfig(iface=device_iface)
    usable, reason = pdev.probe_device(cfg)
    return (cfg if usable else None), reason


def test_on_hardware_nominations_match_the_model(group, model, device_iface):
    """SR16 on silicon: the resident group child must nominate what the model
    does (attributed by ``end``, R78.7 protocol note 2).

    Gated on ``device_usable`` (SR18) *and* on the resident child being this
    group (SR14) — a match against some other resident child would be a
    misattribution dressed up as a pass.
    """
    from pyro import device as pdev

    cfg, reason = _hw_gate(device_iface)
    if cfg is None:
        pytest.skip(reason)                        # canonical R83 SKIP string

    expected_child = group.rp_child_id(GEN.GENERATOR_VERSION,
                                       GEN.HARNESS_VERSION, 1)
    child = _read_rp_child_id(pdev, cfg)
    if child != expected_child:
        pytest.skip(
            "device_usable=true but the resident child is not this group — "
            "rp_child_id=0x%08x, expected 0x%08x (SR14)"
            % (child or 0, expected_child))

    tail = group.overlap_tail
    for case in S.build_cases():
        want = {(w.slot, w.end) for w in S.oracle_windows(group, case.stream)}
        got = set()
        for req in S.chunk_requests(case.segments, tail):
            for _start, end, pid in _hw_match(pdev, cfg, req.buf):
                got.add((pid, req.base + end))
        assert got == want, (
            "silicon/model divergence on %s: extra=%s missing=%s"
            % (case.name, sorted(got - want)[:5], sorted(want - got)[:5]))


def _read_rp_child_id(pdev, cfg):
    """ID_REQUEST/ID_REPLY -> rp_child_id (R78.5a), or None."""
    tr = pdev._make_transport(cfg)
    try:
        eth = (bytes(cfg.dst_mac) + bytes(cfg.src_mac)
               + struct.pack(">H", pdev.ETHERTYPE))
        seq = 11
        tr.send(eth + pdev.encode_frame(pdev.KIND_ID_REQUEST, 0, seq, b""))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            reply = tr.recv(0.5)
            if reply is None:
                continue
            reply = bytes(reply)
            if len(reply) < 14 or struct.unpack(">H", reply[12:14])[0] != \
                    pdev.ETHERTYPE:
                continue
            dec = pdev.decode_frame(reply[14:])
            if dec.kind != pdev.KIND_ID_REPLY or dec.seq != seq:
                continue
            return struct.unpack(">I", dec.payload[8:12])[0]
        return None
    finally:
        tr.close()


def _hw_match(pdev, cfg, corpus, out_cap=61):
    """One MATCH_REQUEST/REPLY round trip (R78.7); [(start, end, pid), ...]."""
    tr = pdev._make_transport(cfg)
    try:
        eth = (bytes(cfg.dst_mac) + bytes(cfg.src_mac)
               + struct.pack(">H", pdev.ETHERTYPE))
        seq = 23
        body = struct.pack(">QHH", 0, out_cap, 0) + corpus
        tr.send(eth + pdev.encode_frame(pdev.KIND_MATCH_REQUEST, 1, seq, body))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            reply = tr.recv(0.5)
            if reply is None:
                continue
            reply = bytes(reply)
            if len(reply) < 14 or struct.unpack(">H", reply[12:14])[0] != \
                    pdev.ETHERTYPE:
                continue
            dec = pdev.decode_frame(reply[14:])
            if dec.kind != pdev.KIND_MATCH_REPLY or dec.seq != seq:
                continue
            count, status = struct.unpack(">HH", dec.payload[0:4])
            assert not (status & 1), "OVF set — out_cap too small (R78.7)"
            out = []
            for i in range(count):
                start, end, pid, _fl = struct.unpack_from("<QQII", dec.payload,
                                                          8 + 24 * i)
                out.append((start, end, pid))
            return out
        raise AssertionError("no MATCH_REPLY within 2 s")
    finally:
        tr.close()
