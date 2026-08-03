"""Unit tests for the group circuit model
(pyro._circuit_model.GroupCircuitModel).

The SNORT-PF SR7 pattern-set circuit is N automata sharing one harness;
:class:`GroupCircuitModel` is its single software twin.  These tests pin the
behaviour later stages (the emitter, the SR16 oracle, the xsim diff) must
agree with: per-slot windows with ``pattern_id`` = slot, simultaneous matches
on several slots, the ``(end, pattern_id, start)`` ring order, the documented
``start`` convention, R47 overflow, the R41 not-resident guard and R45a perf
counters.
"""
import re

import pytest

from pyro import hdl
from pyro import _circuit_model as cm
from pyro.hdl import automaton as _auto
from pyro.snort import groups as G
from pyro.snort import rules as R
from pyro.snort import triage as T


class FakeGroupCircuit:
    """Minimal object satisfying GroupCircuitModel's documented contract.

    (The SR7 emitter's ``GeneratedCircuit``-shaped group artifact satisfies the
    same contract by construction; the model is duck-typed on ``automata``.)
    """

    def __init__(self, automata, datapath_bytes=1, circ_id=(1, 2, 3, 4),
                 circ_flags=0):
        self.automata = tuple(automata)
        self.datapath_bytes = datapath_bytes
        self.enc = cm.ENC_BYTES
        self.circ_id = circ_id
        self.circ_flags = circ_flags
        self.harness_version = hdl.HARNESS_VERSION
        self.generator_version = hdl.GENERATOR_VERSION
        self.num_patterns = len(self.automata)


def au(literal: bytes, nocase: bool = False):
    return _auto.build(re.escape(literal),
                       re.IGNORECASE if nocase else 0, _auto.ENC_BYTES)


def model(*literals, datapath_bytes=1):
    """A resident group model over the given literal slots."""
    m = cm.GroupCircuitModel(FakeGroupCircuit(
        [au(x) if isinstance(x, bytes) else x for x in literals],
        datapath_bytes=datapath_bytes))
    m.resident = True
    return m


# --- basic slot semantics -------------------------------------------------

def test_pattern_id_is_the_slot_index():
    m = model(b"abc", b"xyz")
    got, ovf = m.scan(b"..xyz..abc..")
    assert ovf is False
    assert got == [cm.GroupMatch(1, 2, 5, 0), cm.GroupMatch(0, 7, 10, 0)]
    assert got[0].slot == 1


def test_no_match_returns_empty_ring():
    m = model(b"abc", b"xyz")
    assert m.scan(b"nothing here") == ([], False)


def test_empty_group_never_matches():
    m = model()
    assert m.n_slots == 0
    assert m.scan(b"anything") == ([], False)


def test_start_is_the_true_match_start_not_the_chunk_start():
    """Documented convention: this model reports the TRUE match start, where
    silicon reports the chunk start (S1 AC-S1-2, docs/notebook.md).  For a
    literal slot the two are reconcilable: start == end - len(anchor)."""
    m = model(b"authorized_keys")
    (entry,), _ = m.scan(b"RETR authorized_keys\r\n")
    assert entry.start == 5 and entry.end == 20
    assert entry.start == entry.end - len(b"authorized_keys")


def test_start_off_is_absolute_and_skips_earlier_matches():
    m = model(b"abc")
    assert m.scan(b"abc..abc", 0)[0] == [cm.GroupMatch(0, 0, 3, 0),
                                         cm.GroupMatch(0, 5, 8, 0)]
    assert m.scan(b"abc..abc", 3)[0] == [cm.GroupMatch(0, 5, 8, 0)]


def test_nocase_slot_matches_the_ascii_fold_only():
    m = model(au(b"abc", nocase=True))
    assert [e.end for e in m.scan(b"..AbC..")[0]] == [5]
    assert m.scan(b"..\xc0bc..")[0] == []       # latin-1 'À' is not 'a'


# --- simultaneous matches on multiple slots -------------------------------

def test_several_slots_match_at_the_same_offset():
    """Nested anchors: 'abcd', 'bcd' and 'cd' all end at the same byte."""
    m = model(b"abcd", b"bcd", b"cd")
    got, ovf = m.scan(b"xxabcd")
    assert ovf is False
    assert [(e.pattern_id, e.start, e.end) for e in got] == [
        (0, 2, 6), (1, 3, 6), (2, 4, 6)]


def test_ring_order_is_end_then_pattern_id():
    """A long anchor that STARTS earlier can END later; the ring is emission
    ordered (by ``end``), because the shared harness writes on accept."""
    m = model(b"aaaaaa", b"bb")
    got, _ = m.scan(b"aaaaaabb")
    assert [(e.pattern_id, e.start, e.end) for e in got] == [
        (0, 0, 6), (1, 6, 8)]
    # slot 0 starts at 0 and slot 1 at 6, but had the long one started later
    # the order would still follow `end`:
    m2 = model(b"zzzz", b"yy")
    got2, _ = m2.scan(b"yyzzzz")
    assert [(e.pattern_id, e.end) for e in got2] == [(1, 2), (0, 6)]


def test_one_slot_can_report_overlapping_windows():
    m = model(b"aa")
    got, _ = m.scan(b"aaaa")
    assert [(e.start, e.end) for e in got] == [(0, 2), (1, 3), (2, 4)]


def test_deduped_slot_serves_every_rule_via_the_sidecar():
    """End-to-end: two rules share an anchor, one slot, both nominated."""
    triaged = [(r, T.triage_rule(r)) for r in (
        R.parse_rule('alert tcp any any -> any $HTTP_PORTS '
                     '( content:"/view-source"; sid:1; )', 1),
        R.parse_rule('alert tcp any any -> any $HTTP_PORTS '
                     '( content:"/view-source"; sid:2; )', 2),
        R.parse_rule('alert tcp any any -> any $HTTP_PORTS '
                     '( content:"/other"; sid:3; )', 3))]
    group = G.pack_groups(triaged)[0]
    m = cm.GroupCircuitModel(FakeGroupCircuit(G.group_automata(group)))
    m.resident = True
    got, _ = m.scan(b"GET /view-source HTTP/1.0")
    assert [e.pattern_id for e in got] == [0]
    sidecar = group.sidecar()
    assert sidecar[got[0].pattern_id] == ("1:1", "1:2")


def test_tombstoned_slot_never_matches_but_keeps_its_pattern_id():
    m = model(b"aaa", None, b"ccc")     # slot 1 tombstoned (no automaton)
    assert m.n_slots == 3
    got, _ = m.scan(b"aaa ccc")
    assert [e.pattern_id for e in got] == [0, 2]


# --- R47 overflow ---------------------------------------------------------

def test_overflow_sets_ovf_and_truncates_in_ring_order():
    m = model(b"ab", b"b")
    got, ovf = m.scan(b"abab")
    assert [(e.pattern_id, e.end) for e in got] == [(0, 2), (1, 2),
                                                    (0, 4), (1, 4)]
    trunc, ovf = m.scan(b"abab", out_cap=2)
    assert ovf is True
    assert [(e.pattern_id, e.end) for e in trunc] == [(0, 2), (1, 2)]
    assert m.csr_read(cm.CSR_STATUS) & cm.ST_OVF
    assert m.csr_read(cm.CSR_OUT_COUNT) == 2


def test_no_overflow_when_capacity_is_sufficient():
    m = model(b"abc")
    got, ovf = m.scan(b"abcabc", out_cap=2)
    assert ovf is False and len(got) == 2
    assert not m.csr_read(cm.CSR_STATUS) & cm.ST_OVF
    assert m.csr_read(cm.CSR_STATUS) & cm.ST_DONE


def test_zero_out_cap_is_overflow_not_no_match():
    """S1 protocol note 1: out_cap=0 returns count=0 with OVF — 'no room',
    not 'no match'."""
    m = model(b"abc")
    got, ovf = m.scan(b"abc", out_cap=0)
    assert got == [] and ovf is True


def test_negative_out_cap_is_a_defined_error():
    m = model(b"abc")
    with pytest.raises(ValueError):
        m.scan(b"abc", out_cap=-1)


# --- harness contract -----------------------------------------------------

def test_scan_requires_resident():
    m = cm.GroupCircuitModel(FakeGroupCircuit([au(b"abc")]))
    with pytest.raises(cm.NotResident):
        m.scan(b"abc")


def test_csr_block_and_identity():
    circ = FakeGroupCircuit([au(b"abc"), au(b"xyz")], circ_id=(9, 8, 7, 6),
                            circ_flags=0x0002_0000)
    m = cm.GroupCircuitModel(circ)
    assert m.csr_read(cm.CSR_ID) == hdl.ID_MAGIC
    assert m.csr_read(cm.CSR_HARNESS_VER) == hdl.HARNESS_VERSION
    assert m.csr_read(cm.CSR_CAPS1) == hdl.GENERATOR_VERSION
    assert m.csr_read(cm.CSR_CIRC_ID0) == 9
    assert m.csr_read(cm.CSR_CIRC_ID3) == 6
    assert m.identity_block() == (9, 8, 7, 6, 0x0002_0000)
    assert (m.csr_read(cm.CSR_CIRC_FLAGS) >> 16) == m.n_slots


def test_perf_counters_report_idealized_scan_cost():
    m = model(b"abc", datapath_bytes=8)
    m.scan(b"x" * 100)
    assert m.csr_read(cm.CSR_BYTES_LO) == 100
    assert m.csr_read(cm.CSR_CYCLES_LO) == 13     # ceil(100/8)


def test_perf_counters_respect_start_off():
    m = model(b"abc")
    m.scan(b"x" * 100, start_off=40)
    assert m.csr_read(cm.CSR_BYTES_LO) == 60


# --- agreement with the single-pattern model ------------------------------

def test_single_slot_group_agrees_with_circuitmodel():
    """A 1-slot group is the Phase-S1 child; the two models must not diverge
    (there is one group model, and it inherits CircuitModel's conventions)."""
    ctx = cm.CircuitContext()
    circ = ctx.generate(b"authorized_keys", re.IGNORECASE)
    ctx.load(circ)
    corpus = b"RETR AuthoRized_Keys\r\nRETR authorized_keys\r\n"
    windows, _ = circ.scan(corpus)
    # accepts a single-pattern GeneratedCircuit as the degenerate 1-slot group
    grp = cm.GroupCircuitModel(circ.circuit)
    grp.resident = True
    entries, _ = grp.scan(corpus)
    assert [(w.start, w.end) for w in windows] == [(e.start, e.end)
                                                   for e in entries]
    assert all(e.pattern_id == 0 for e in entries)
    assert [e.as_window() for e in entries] == list(windows)
