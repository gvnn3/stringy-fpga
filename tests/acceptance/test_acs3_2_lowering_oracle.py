"""AC-S3-2: content-chain lowering + clean-pcre fusion, covered by the oracle.

Spec clause (snort-rule-offload §6, Phase S3): *"Content-chain lowering
(offset/depth/distance/within) and clean-pcre fusion (SF13) are in the
group circuits, each declared in SR4 classes and covered by the oracle."*

Gates, in the two-sided S2 style:

A. **Model ↔ closed-form equality on every lowered slot.**  For every
   non-literal slot in every corpus group, exemplar subjects (min- and
   max-width gaps, derived from the pattern's parse tree) produce the same
   window sets from the automaton/model as from the independent stdlib-re
   enumerator.  Device-free, exhaustive over the lowered slot population.

B. **SR12 chunk/segment-boundary completeness for chains.**  A floating
   chain match split across the daemon's chunk boundary survives via the
   group's overlap tail (which AC-S3-2 generalized from ``max_anchor − 1``
   to ``max floating span − 1``); an admission-lowered ``\\A`` slot's match
   is wholly inside chunk 0 by construction.

C. **The Snort differential** (SR16) on a chain sub-corpus: content-only
   tcp chain rules, payloads crafted to satisfy the FULL rule, replayed
   through Snort 3 — ``alerted ⊆ nominated`` absolutely (these are all
   ``raw-anchor``), FPs recorded by SR4 class.  Runs when the Snort
   arbiter is present, else skips honestly.

D. **The gates bite** (sabotage): a window narrowed by one byte and a
   dropped chain fragment are both caught by gate A's equality; the
   sid-509 ``\\A``-on-tcp defect class is pinned as inadmissible.

E. **SR4 declarations**: chain groups declare their classes; per-rule
   dropped lists shrink exactly by what was lowered; ``tail_span`` bounds
   the max exemplar.

On-hardware clauses for the lowered circuits belong to AC-S3-1's full
build (this file is the device-free oracle side; SR18 discipline applies
there, not here).
"""

import os
import re as _stdre

import pytest

import snortpf_s2_support as S
from pyro._circuit_model import GroupCircuitModel, _scan_windows
from pyro.hdl import automaton as _auto
from pyro.snort import groups as G
from pyro.snort import lowering as L
from pyro.snort import triage as T

pytestmark = pytest.mark.skipif(
    not os.path.exists(S.CORPUS), reason="community ruleset not vendored")


# --------------------------------------------------------------------------
# Fixtures: the corpus packing and the lowered-slot population
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def triaged():
    return T.triage_file(S.CORPUS)


@pytest.fixture(scope="module")
def corpus_groups(triaged):
    return G.pack_groups(triaged)


@pytest.fixture(scope="module")
def lowered_slots(corpus_groups):
    """Every non-literal (chain / prefix / fused) slot, with its group."""
    out = []
    for g in corpus_groups:
        for slot in g.slots:
            if not slot.tombstone and not S.slot_is_literal(slot):
                out.append((g, slot))
    return out


def slot_windows(slot, data: bytes):
    au = _auto.build(slot.pattern_eff, slot.flags_eff, _auto.ENC_BYTES)
    return {(w.start, w.end) for w in _scan_windows(au, data, 0)}


def oracle_slot_windows(slot, data: bytes):
    return set(S.lowered_occurrences(slot.pattern_eff, slot.flags_eff, data))


# --------------------------------------------------------------------------
# Gate 0: the population is what the unit canary says it is
# --------------------------------------------------------------------------
def test_lowered_population_is_present_and_counted(lowered_slots):
    anchored = sum(1 for _g, s in lowered_slots
                   if s.pattern_eff.startswith(b"(?s:\\A"))
    # 372 lowered RULES (245 chains + 91 \A + 36 fused) dedup onto 181
    # slots — rule families share identical chains and differ only in
    # dropped conjuncts.  Measured 2026-07-28; canary, not law.
    assert len(lowered_slots) == 181
    assert anchored == 83                # the PDU-aligned (sid-509) admission


# --------------------------------------------------------------------------
# Gate A: model ↔ closed-form equality on every lowered slot
# --------------------------------------------------------------------------
def test_every_lowered_slot_matches_the_closed_form_oracle(lowered_slots):
    assert lowered_slots, "no lowered slots — AC-S3-2 is vacuous"
    filler = b"\x00" * 7
    for _g, slot in lowered_slots:
        for maximal in (False, True):
            ex = S.slot_exemplar(slot, maximal)
            anchored = slot.tail_span_eff == 0
            # \A slots only admit position 0; floating slots get offsets.
            subjects = [ex, ex + filler] if anchored else \
                       [ex, filler + ex, filler + ex + filler]
            for data in subjects:
                got = slot_windows(slot, data)
                want = oracle_slot_windows(slot, data)
                assert got == want, (
                    "model/oracle divergence on slot %d of %r\n"
                    "pattern=%r subject=%r\nmodel=%r oracle=%r"
                    % (slot.index, _g.name, slot.pattern_eff, data,
                       sorted(got), sorted(want)))


def test_group_model_agrees_on_a_chain_bearing_group(corpus_groups):
    """Whole-group equality (S2's gate 1 shape) on a group that actually
    holds chains: the model runs all slots, literal and lowered together."""
    g = next(x for x in corpus_groups if x.name == "literal/0")
    model = S.group_model(g)
    chain_slots = [s for s in g.slots
                   if not s.tombstone and not S.slot_is_literal(s)][:8]
    assert chain_slots
    payload = b"\x00".join(S.slot_exemplar(s) for s in chain_slots)
    got = S.model_windows(model, payload)
    want = S.oracle_windows(g, payload)
    assert got == want
    hit_slots = {w.slot for w in got}
    assert hit_slots >= {s.index for s in chain_slots
                         if s.tail_span_eff > 0}


# --------------------------------------------------------------------------
# Gate B: SR12 chunk/segment completeness for chains
# --------------------------------------------------------------------------
def test_chain_match_survives_chunk_boundaries_via_the_overlap_tail(
        corpus_groups):
    g = next(x for x in corpus_groups if x.name == "literal/0")
    floats = [s for s in g.slots if not s.tombstone
              and not S.slot_is_literal(s) and s.tail_span_eff > 0]
    assert floats
    tail = g.overlap_tail
    assert tail == g.max_tail_span - 1
    for slot in floats[:6]:
        ex = S.slot_exemplar(slot, maximal=True)
        assert len(ex) <= slot.tail_span_eff
        # Place the exemplar to straddle the first chunk boundary at every
        # cut position inside it (worst case for SR12).
        for cut in range(1, len(ex)):
            pre = b"\x00" * (S.CHUNK_BYTES - cut)
            stream = pre + ex + b"\x00" * 8
            reqs = S.chunk_requests([stream], tail)
            found = set()
            for req in reqs:
                for s0, e0 in oracle_slot_windows(slot, req.buf):
                    found.add((req.base + s0, req.base + e0))
                au_hits = slot_windows(slot, req.buf)
                assert {(req.base + a, req.base + b) for a, b in au_hits} \
                    >= {(req.base + a, req.base + b) for a, b in
                        oracle_slot_windows(slot, req.buf)}
            assert any(s0 == len(pre) for s0, _e in found), (
                "chain match lost at cut %d for slot %d of %r (tail %d)"
                % (cut, slot.index, g.name, tail))


def test_anchored_slot_match_is_wholly_inside_chunk0(corpus_groups):
    seen = 0
    for g in corpus_groups:
        for slot in g.slots:
            if slot.tombstone or slot.tail_span_eff != 0:
                continue
            ex = S.slot_exemplar(slot, maximal=True)
            assert len(ex) <= L.CHUNK_BYTES     # the admission condition
            got = slot_windows(slot, ex + b"\x00" * 4)
            assert (0, len(ex)) in got
            seen += 1
            if seen >= 12:
                return
    assert seen, "no \\A slots found"


# --------------------------------------------------------------------------
# Gate C: the Snort differential on a chain sub-corpus
# --------------------------------------------------------------------------
def _chain_subcorpus(triaged):
    """Content-only tcp chain rules the corpus can fully satisfy on the
    wire: every dropped conjunct is at most ``flow`` (established/to_server
    holds in the Case machinery's session)."""
    picked = []
    for rule, res in triaged:
        if res.tier != T.TIER_ANCHOR or res.anchor is None:
            continue
        if rule.proto.lower() != "tcp":
            continue
        low = L.lower_rule(rule, res)
        if low.chain_len < 2:
            continue
        if not set(low.dropped) <= {"flow"}:
            continue
        picked.append((rule, res, low))
    return picked


def _case_port(rule) -> int:
    tok = (rule.dst_port or "any").strip()
    if tok.isdigit():
        return int(tok)
    if tok == "$HTTP_PORTS":
        return 80
    if tok.startswith("[") and tok[1:-1].split(",")[0].isdigit():
        return int(tok[1:-1].split(",")[0])
    return 4444                              # 'any' family

def test_snort_differential_confirms_chain_nominations(triaged, tmp_path):
    if not S.snort_present():
        pytest.skip("snort arbiter not present")
    sub = _chain_subcorpus(triaged)
    assert len(sub) >= 8, "chain sub-corpus unexpectedly small"

    # One synthetic group holding exactly these rules (experiment keying,
    # sanctioned by SR6's class_of seam).
    sub_triaged = [(r, res) for r, res, _ in sub]
    groups = G.pack_groups(sub_triaged, class_of=lambda p: "acs32")
    assert len(groups) == 1
    g = groups[0]
    model = S.group_model(g)

    # Cases: for each rule, min and max exemplar as the client payload.
    cases = []
    port = 41000
    slot_of = {}
    for slot in g.slots:
        for ref in slot.rules:
            slot_of[(ref.gid, ref.sid)] = slot
    for rule, res, low in sub:
        sid = next(int(o.value) for o in rule.options if o.key == "sid")
        slot = slot_of[(1, sid)]
        for maximal in (False, True):
            port += 1
            ex = S.slot_exemplar(slot, maximal)
            cases.append(S.Case("sid%d_%s" % (sid, "max" if maximal
                                              else "min"),
                                port, _case_port(rule), (ex,)))

    rules_path = str(tmp_path / "chains.rules")
    with open(rules_path, "w") as fh:
        fh.write("\n".join(r.raw for r, _res, _l in sub) + "\n")
    pcap = S.write_pcap(str(tmp_path / "chains.pcap"), cases)
    alerts = S.alerts_by_case(
        S.run_snort(rules_path, pcap, str(tmp_path)), cases)
    diff = S.run_differential(g, model, cases, alerts)

    # SR16: completeness is absolute for raw-anchor (all of these are).
    assert diff.misses == (), "chain completeness misses: %r" % (diff.misses,)
    # Non-vacuous, measured 2026-07-28: 13 of 32 cases alert (exemplars on
    # http_inspect-bound ports with \x00 gap filler are often rejected by
    # the inspector before raw-content evaluation — those cases become
    # recorded anchor_strip FPs, never misses).  Every alerted case was
    # nominated.
    assert diff.cases_with_alerts >= 12
    assert diff.true_positives >= diff.cases_with_alerts
    assert sum(diff.fp_by_class.values()) == len(diff.fp_rows)


# --------------------------------------------------------------------------
# Gate D: the gates bite
# --------------------------------------------------------------------------
def test_a_narrowed_window_is_caught_by_the_equality_gate(lowered_slots):
    g, slot = next((g, s) for g, s in lowered_slots
                   if b".{" in s.pattern_eff and s.tail_span_eff > 0)
    m = _stdre.search(rb"\.\{(\d+),(\d+)\}", slot.pattern_eff)
    lo, hi = int(m.group(1)), int(m.group(2))
    assert hi > lo, "need a widenable window for the sabotage"
    narrowed = (slot.pattern_eff[:m.start()]
                + b".{%d,%d}" % (lo, hi - 1)
                + slot.pattern_eff[m.end():])
    bad = slot._replace(pattern=narrowed)
    ex = S.slot_exemplar(slot, maximal=True)     # exercises gap width == hi
    got = slot_windows(bad, ex)
    want = oracle_slot_windows(slot, ex)
    assert got != want, "sabotaged (narrowed) window was not caught"


def test_a_dropped_fragment_is_caught_by_the_equality_gate(lowered_slots):
    g, slot = next((g, s) for g, s in lowered_slots
                   if s.tail_span_eff > 0 and b".{" in s.pattern_eff)
    # Sabotage: replace the whole chain with its head literal only.
    head = S.slot_exemplar(slot)[:4]
    bad = slot._replace(pattern=_stdre.escape(head), flags=0)
    ex = S.slot_exemplar(slot)
    got = slot_windows(bad, ex)
    want = oracle_slot_windows(slot, ex)
    assert got != want


def test_tcp_offset_depth_is_pinned_inadmissible(triaged):
    """The sid-509 defect class stays fixed: no tcp rule may carry a
    ``\\A`` prefix (Snort 3 binds raw-cursor depth to the PDU section of
    an inspected flow, not to stream offset 0 — measured 2026-07-28)."""
    for rule, res in triaged:
        if res.tier != T.TIER_ANCHOR or res.anchor is None:
            continue
        if rule.proto.lower() == "tcp":
            low = L.lower_rule(rule, res)
            assert not low.pattern.startswith(b"(?s:\\A"), \
                "tcp rule %s got a \\A prefix" % rule.raw[:80]


# --------------------------------------------------------------------------
# Gate E: SR4 declarations
# --------------------------------------------------------------------------
def test_sr4_classes_and_dropped_lists_are_declared(corpus_groups):
    from pyro.hdl import generator as gen
    g = next(x for x in corpus_groups if x.name == "literal/0")
    m = g.manifest(gen.GENERATOR_VERSION, gen.HARNESS_VERSION)
    assert set(m["over_approx_classes"]) >= {
        L.OA_DROPPED, L.OA_CASE_FOLD, L.OA_CHUNK_OVERLAP, L.OA_ANCHOR_STRIP}
    assert m["max_tail_span"] == g.max_tail_span
    assert m["overlap_tail"] == g.max_tail_span - 1
    # A chain slot's lowered options are OUT of its rules' dropped lists.
    chain_slot = next(s for s in g.slots
                      if not s.tombstone and not S.slot_is_literal(s)
                      and s.tail_span_eff > 0)
    for ref in chain_slot.rules:
        assert L.OA_CHUNK_OVERLAP in ref.oa


def test_tail_span_bounds_every_lowered_exemplar(lowered_slots):
    for _g, slot in lowered_slots:
        ex = S.slot_exemplar(slot, maximal=True)
        if slot.tail_span_eff > 0:
            assert len(ex) <= slot.tail_span_eff
        else:
            assert len(ex) <= L.CHUNK_BYTES
