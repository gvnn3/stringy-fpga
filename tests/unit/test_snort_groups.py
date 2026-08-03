"""Unit tests for SR6 grouping / SR7 identity / SR9 keying (pyro.snort.groups).

Covers the two things later SNORT-PF stages must not be able to diverge on:
the **packing** (port class, stable sid order, GROUP_MAX, anchor dedup,
tombstones) and the **identity** (canonical serialization, group hash,
rp_child_id, cache key) — including the SR13 obligation that deployment
variables never reach the hash.
"""
import os
import re
from collections import Counter

import pytest

from pyro.hdl import automaton as _auto
from pyro.snort import groups as G
from pyro.snort import rules as R
from pyro.snort import triage as T

GEN = 0x00020300
HARN = 0x00020300


def rule(sid, content, dport="$HTTP_PORTS", extra="", src_net="$EXTERNAL_NET",
         dst_net="$HOME_NET", line=None):
    line_no = sid if line is None else line
    text = ('alert tcp %s any -> %s %s ( %scontent:"%s"; sid:%d; )'
            % (src_net, dst_net, dport, extra, content, sid))
    return R.parse_rule(text, line_no)


def triaged(*rs):
    return [(r, T.triage_rule(r)) for r in rs]


def one_group(*rs, group_max=G.GROUP_MAX):
    gs = G.pack_groups(triaged(*rs), group_max=group_max)
    assert len(gs) == 1, [g.name for g in gs]
    return gs[0]


# --- packing (SR6) --------------------------------------------------------

def test_groups_key_on_destination_port_class_first():
    gs = G.pack_groups(triaged(rule(1, "aaa", "$HTTP_PORTS"),
                               rule(2, "bbb", "any"),
                               rule(3, "ccc", "$HTTP_PORTS")))
    assert [g.port_class for g in gs] == ["$HTTP_PORTS", "any"]
    assert [g.rule_count for g in gs] == [2, 1]


def test_port_class_keeps_variables_and_coalesces_literals():
    assert G.port_class(" $HTTP_PORTS ") == "$HTTP_PORTS"
    assert G.port_class("$ORACLE_PORTS") == "$ORACLE_PORTS"
    assert G.port_class("ANY") == "any"
    assert G.port_class("") == "any"
    # Literal ports, lists, ranges and negations all coalesce (SR6 → ~16
    # groups; the raw token gives 180 classes / 190 groups on the corpus).
    for tok in ("25", "21", "[80, 8080]", "!53", "1024:"):
        assert G.port_class(tok) == G.PORT_CLASS_LITERAL


def test_literal_ports_share_a_class_but_keep_their_token():
    entries = G.groupable_entries(triaged(rule(1, "aaa", "25"),
                                          rule(2, "bbb", "21")))
    assert [e.port_class for e in entries] == ["literal", "literal"]
    assert [e.dst_port for e in entries] == ["25", "21"]
    g = one_group(rule(1, "aaa", "25"), rule(2, "bbb", "21"))
    assert g.port_class == "literal" and g.rule_count == 2


def test_slots_follow_stable_sid_order_not_file_order():
    a = rule(30, "thirty", line=1)
    b = rule(10, "ten", line=2)
    c = rule(20, "twenty", line=3)
    g1 = one_group(a, b, c)
    g2 = one_group(c, a, b)          # same rules, shuffled input
    assert [s.anchor for s in g1.slots] == [b"ten", b"twenty", b"thirty"]
    assert g1.canonical_bytes() == g2.canonical_bytes()


def test_group_max_bounds_rules_per_group():
    rs = [rule(i, "anchor%03d" % i) for i in range(1, 11)]
    gs = G.pack_groups(triaged(*rs), group_max=4)
    assert [g.rule_count for g in gs] == [4, 4, 2]
    assert [g.index for g in gs] == [0, 1, 2]
    assert [g.name for g in gs] == ["$HTTP_PORTS/0", "$HTTP_PORTS/1",
                                    "$HTTP_PORTS/2"]


def test_only_anchor_compilable_rules_with_a_sid_are_packed():
    header_only = R.parse_rule(
        'alert icmp any any -> any any ( itype:8; sid:5; )', 1)
    always_fwd = R.parse_rule(
        'alert tcp any any -> any any ( byte_test:4,>,1,0; sid:6; )', 2)
    no_sid = R.parse_rule(
        'alert tcp any any -> any any ( content:"x"; )', 3)
    ok = rule(7, "keep", "any")
    gs = G.pack_groups(triaged(header_only, always_fwd, no_sid, ok))
    assert len(gs) == 1 and gs[0].rule_count == 1
    assert gs[0].sidecar() == {0: ("1:7",)}


# --- dedup (SR6/SR7 sidecar) ---------------------------------------------

def test_identical_anchors_share_one_slot_with_a_rule_list():
    g = one_group(rule(1, "/view-source"), rule(2, "/view-source"),
                  rule(3, "/other"))
    assert g.n_slots == 2 and g.rule_count == 3
    assert g.sidecar() == {0: ("1:1", "1:2"), 1: ("1:3",)}


def test_nocase_anchors_dedup_case_insensitively_and_store_the_fold():
    # Case-SENSITIVE anchors differing only in case stay distinct slots.
    g = one_group(rule(1, "ABC"), rule(2, "AbC"))
    assert g.n_slots == 2
    # With nocase they are one slot, stored as the ASCII fold (R15).
    g3 = G.pack_groups(triaged(
        R.parse_rule('alert tcp any any -> any $HTTP_PORTS '
                     '( content:"ABC",nocase; sid:1; )', 1),
        R.parse_rule('alert tcp any any -> any $HTTP_PORTS '
                     '( content:"aBc",nocase; sid:2; )', 2)))[0]
    assert g3.n_slots == 1
    assert g3.slots[0].anchor == b"abc" and g3.slots[0].nocase is True
    assert g3.sidecar() == {0: ("1:1", "1:2")}


def test_nocase_and_case_sensitive_same_bytes_are_distinct_slots():
    g = G.pack_groups(triaged(
        R.parse_rule('alert tcp any any -> any any '
                     '( content:"abc",nocase; sid:1; )', 1),
        R.parse_rule('alert tcp any any -> any any '
                     '( content:"abc"; sid:2; )', 2)))[0]
    assert g.n_slots == 2
    assert [(s.anchor, s.nocase) for s in g.slots] == [(b"abc", True),
                                                       (b"abc", False)]


# --- canonical serialization + hash (SR7/SR9/SR13) ------------------------

def test_canonical_bytes_are_deterministic_and_domain_separated():
    g = one_group(rule(1, "aaa"), rule(2, "bbb"))
    cb = g.canonical_bytes()
    assert cb == g.canonical_bytes()
    assert cb.startswith(b"PYROGRP\x00")


def test_group_hash_is_16_bytes_and_stable():
    g = one_group(rule(1, "aaa"), rule(2, "bbb"))
    h = G.group_hash(g, GEN, HARN)
    assert len(h) == 16
    assert h == g.group_hash(GEN, HARN)
    # Recomputed from an independently repacked, reordered input.
    g2 = one_group(rule(2, "bbb"), rule(1, "aaa"))
    assert g2.group_hash(GEN, HARN) == h


@pytest.mark.parametrize("mutate", [
    lambda: one_group(rule(1, "aaa"), rule(2, "bbc")),     # anchor changed
    lambda: one_group(rule(1, "aaa"), rule(3, "bbb")),     # sid changed
    lambda: one_group(rule(1, "aaa"), rule(2, "bbb"),
                      rule(4, "ddd")),                     # rule added
    lambda: G.pack_groups(triaged(rule(1, "aaa", "any"),
                                  rule(2, "bbb", "any")))[0],  # port class
])
def test_group_hash_changes_when_content_changes(mutate):
    base = one_group(rule(1, "aaa"), rule(2, "bbb"))
    assert mutate().group_hash(GEN, HARN) != base.group_hash(GEN, HARN)


def test_group_hash_rolls_with_versions_and_datapath_width():
    g = one_group(rule(1, "aaa"))
    base = g.group_hash(GEN, HARN)
    assert g.group_hash(GEN + 1, HARN) != base
    assert g.group_hash(GEN, HARN + 1) != base
    assert g.group_hash(GEN, HARN, datapath_bytes=8) != base
    # dpb=1 is the identity default — no artificial rollover (P2b discipline).
    assert g.group_hash(GEN, HARN, datapath_bytes=1) == base


def test_deployment_variables_never_enter_the_hash_sr13():
    """The port-class TOKEN is rule content; net/port variable VALUES are site
    configuration and must not change the group identity (SR13/SF16)."""
    a = one_group(rule(1, "aaa", "$HTTP_PORTS", src_net="$EXTERNAL_NET",
                       dst_net="$HOME_NET"))
    b = one_group(rule(1, "aaa", "$HTTP_PORTS", src_net="10.0.0.0/8",
                       dst_net="192.168.0.0/16"))
    assert a.group_hash(GEN, HARN) == b.group_hash(GEN, HARN)
    assert b"192.168" not in b.canonical_bytes()


def test_rp_child_id_is_low32_of_group_hash_and_nonzero():
    g = one_group(rule(1, "aaa"))
    h = g.group_hash(GEN, HARN)
    cid = g.rp_child_id(GEN, HARN)
    assert cid == int.from_bytes(h[:4], "little")
    assert cid != 0


def test_bitstream_key_is_the_r4_key_shape_with_group_bytes():
    from pyro.synth.cache import key_digest
    g = one_group(rule(1, "aaa"))
    key = g.bitstream_key(GEN, HARN, 0x19020000, 0x0A000001)
    assert len(key) == 6
    assert key[0] == g.canonical_bytes()
    assert key[1] == G.ENC_BYTES and key[3] == GEN
    # flags slot is repurposed: dpb in the low 32 bits, harness_version above
    assert key[2] == (0 | (HARN << 32))
    assert key[4:] == (0x19020000, 0x0A000001)
    assert isinstance(key_digest(key), str)
    # A dpb=8 build of the same group must not collide with the dpb=1 one.
    wide = g.bitstream_key(GEN, HARN, 0x19020000, 0x0A000001, datapath_bytes=8)
    assert key_digest(wide) != key_digest(key)
    # ... and neither must a harness-only bump: group_hash mixes harness_version
    # into CIRC_ID0..3 and hence rp_child_id (SR14), so a cached bitstream from
    # the old harness would fail the daemon's load-time identity check.
    bumped = g.bitstream_key(GEN, HARN + 1, 0x19020000, 0x0A000001)
    assert key_digest(bumped) != key_digest(key)
    assert g.rp_child_id(GEN, HARN + 1) != g.rp_child_id(GEN, HARN)


def test_manifest_carries_sidecar_identity_and_sr12_tail():
    g = one_group(rule(1, "aaaaa"), rule(2, "bb"))
    man = g.manifest(GEN, HARN)
    assert man["rule_count"] == 2 and man["n_slots"] == 2
    assert man["group_hash"] == g.group_hash(GEN, HARN).hex()
    assert man["rp_child_id"] == g.rp_child_id(GEN, HARN)
    assert man["max_anchor_len"] == 5 and man["overlap_tail"] == 4
    assert [r["key"] for s in man["slots"]
        for r in s["rules"]] == ["1:1", "1:2"]


# --- tombstones (SR6/SR9) -------------------------------------------------

def test_deleting_a_rule_leaves_a_tombstone_and_keeps_pattern_ids():
    old = G.pack_groups(triaged(rule(1, "aaa"), rule(2, "bbb"),
                                rule(3, "ccc")), group_max=4)
    new = G.repack_with_tombstones(old, triaged(rule(1, "aaa"),
                                                rule(3, "ccc")), group_max=4)
    assert len(new) == 1
    g = new[0]
    assert [s.index for s in g.slots] == [0, 1, 2]
    assert [s.anchor for s in g.slots] == [b"aaa", b"bbb", b"ccc"]
    assert g.slots[1].tombstone is True and g.slots[1].rules == ()
    # ccc keeps pattern_id 2 — a repack would have moved it to 1 (SR6).
    assert g.sidecar()[2] == ("1:3",)
    assert g.slots[0].tombstone is False


def test_tombstoned_group_hash_differs_from_a_fresh_repack():
    """The tombstone is real state: the hash must reflect the retained,
    now-empty slot, not pretend the group was repacked."""
    old = G.pack_groups(triaged(rule(1, "aaa"), rule(2, "bbb"),
                                rule(3, "ccc")), group_max=4)
    kept = triaged(rule(1, "aaa"), rule(3, "ccc"))
    tombstoned = G.repack_with_tombstones(old, kept, group_max=4)[0]
    fresh = G.pack_groups(kept, group_max=4)[0]
    assert tombstoned.group_hash(GEN, HARN) != fresh.group_hash(GEN, HARN)
    assert fresh.n_slots == 2 and tombstoned.n_slots == 3


def test_untouched_group_keeps_its_hash_across_a_diff_sr9():
    """A ruleset diff must dirty only the groups it touches."""
    base = triaged(rule(1, "aaa", "any"), rule(2, "bbb", "$HTTP_PORTS"))
    old = G.pack_groups(base, group_max=4)
    changed = triaged(rule(1, "aaa", "any"), rule(2, "bbbX", "$HTTP_PORTS"))
    new = G.repack_with_tombstones(old, changed, group_max=4)
    old_h = {g.name: g.group_hash(GEN, HARN) for g in old}
    new_h = {g.name: g.group_hash(GEN, HARN) for g in new}
    assert new_h["any/0"] == old_h["any/0"]           # untouched
    assert new_h["$HTTP_PORTS/0"] != old_h["$HTTP_PORTS/0"]


def test_new_rule_appends_without_disturbing_existing_slots():
    old = G.pack_groups(triaged(rule(1, "aaa"), rule(2, "bbb")), group_max=4)
    new = G.repack_with_tombstones(
        old, triaged(rule(1, "aaa"), rule(2, "bbb"), rule(3, "ccc")),
        group_max=4)[0]
    assert [(s.index, s.anchor) for s in new.slots] == [
        (0, b"aaa"), (1, b"bbb"), (2, b"ccc")]


def test_repack_is_idempotent_when_nothing_changed():
    base = triaged(rule(1, "aaa"), rule(2, "bbb"))
    old = G.pack_groups(base, group_max=4)
    new = G.repack_with_tombstones(old, base, group_max=4)
    assert [g.canonical_bytes() for g in new] == [g.canonical_bytes()
                                                  for g in old]


# --- slot lowering (the S1 case-fold lesson) ------------------------------

def test_slot_pattern_is_always_bytes_mode():
    g = G.pack_groups(triaged(
        R.parse_rule('alert tcp any any -> any any '
                     '( content:"AuthoRized_Keys",nocase; sid:1; )', 1)))[0]
    pat, flags = G.slot_pattern(g.slots[0])
    assert isinstance(pat, bytes)          # str mode => match-everything (S1)
    assert flags == re.IGNORECASE
    au = G.slot_automaton(g.slots[0])
    assert au.enc == 0                     # ENC_BYTES
    assert not au.over_approx              # exact ASCII fold (R15)


def test_slot_pattern_escapes_regex_metacharacters():
    # NB: '|' cannot appear in a content literal (it delimits hex runs), so
    # the metacharacter set here is what a real anchor can actually contain.
    g = one_group(rule(1, "a.c*d[e]"))
    pat, flags = G.slot_pattern(g.slots[0])
    assert flags == 0
    assert re.fullmatch(pat, b"a.c*d[e]") and not re.fullmatch(pat, b"abcccde")


def test_tombstoned_slot_has_no_automaton():
    old = G.pack_groups(triaged(rule(1, "aaa"), rule(2, "bbb")), group_max=4)
    g = G.repack_with_tombstones(old, triaged(rule(2, "bbb")), group_max=4)[0]
    aus = G.group_automata(g)
    assert aus[0] is None and aus[1] is not None


# --- corpus reproducibility (AC-S2-2 group, pinned) -----------------------

CORPUS = os.path.join(
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(
                os.path.abspath(__file__)))),
                "third_party",
                "snort3-community-rules",
                 "snort3-community.rules")


@pytest.fixture(scope="module")
def corpus_groups():
    if not os.path.exists(CORPUS):
        pytest.skip("community ruleset not present")
    return G.pack_groups(T.triage_file(CORPUS))


def test_corpus_packs_into_the_sr6_group_count(corpus_groups):
    """SR6's '→ ~16 groups' for the current corpus, reproducibly."""
    assert len(corpus_groups) == 21
    assert sum(g.rule_count for g in corpus_groups) == 3896   # SF9
    classes = sorted({g.port_class for g in corpus_groups})
    assert classes == ["$FILE_DATA_PORTS", "$FTP_PORTS", "$HTTP_PORTS",
                       "$ORACLE_PORTS", "$SIP_PORTS", "$SSH_PORTS", "any",
                       "literal"]


def test_ac_s2_2_group_reproduces_the_measured_shape(corpus_groups):
    """The AC-S2-2 group: first 256 $HTTP_PORTS rules in stable sid order.

    Measured facts pinned here so a packing change cannot silently move the
    group the S2 build is sized against.  n_states/byte_edges are the
    **deduped** (per-slot) sums the circuit actually pays; the scoping run's
    4,440 / 4,184 are the per-rule sums, which double-count the three
    duplicated anchors.
    """
    g = [x for x in corpus_groups if x.port_class == "$HTTP_PORTS"][0]
    assert g.name == "$HTTP_PORTS/0"
    assert g.rule_count == 256 and g.n_slots == 253     # 3 duplicate anchors
    assert sum(1 for s in g.slots if len(s.rules) > 1) == 3
    assert sum(1 for s in g.slots if s.length <= 5) == 23
    subtiers = Counter(r.subtier for s in g.slots for r in s.rules)
    assert subtiers == {"normalized-buffer": 214, "raw-anchor": 42}

    aus = G.group_automata(g)
    assert all(a is not None for a in aus)
    n_states = sum(a.n_states for a in aus)
    byte_edges = sum(1 for a in aus for st in range(a.n_states)
                     for e in a.edges[st] if e.kind == _auto.E_BYTE)
    # Identical to the S2 anchor-only shape: this group's chain-eligible
    # rules all fail an AC-S3-2 admission condition (its 6 offset/depth
    # rules are tcp/service-inspected — the sid-509 PDU-alignment defect —
    # so their prefixes stay dropped conjuncts).
    assert (n_states, byte_edges) == (4400, 4147)
    assert max(a.n_states for a in aus) == 66
    assert n_states <= 256 * 1024            # engine-side sanity
    assert g.max_anchor_len == 65 and g.overlap_tail == 64


def test_ac_s2_2_group_estimate_fits_the_real_rp_budget(corpus_groups):
    """SR8 with the corrected SF2 envelope (80k LUT), at the decided dpb=1.

    Group cost model measured by synthesis at N=8/16/32 (dpb=8):
    engine LUTs = 408 + 10.92 * sum(byte_edges), FFs = 485 + sum(states) + 9N,
    plus ~7.3k of shared wrapper.  dpb=1 buys ~4x the LUT budget back, which
    is why the S2 group builds at dpb=1.
    """
    from pyro.hdl import estimator as E
    g = [x for x in corpus_groups if x.port_class == "$HTTP_PORTS"][0]
    aus = G.group_automata(g)
    n_states = sum(a.n_states for a in aus)
    byte_edges = sum(1 for a in aus for st in range(a.n_states)
                     for e in a.edges[st] if e.kind == _auto.E_BYTE)
    engine8 = 408 + 10.92 * byte_edges
    wrapper = 7_300
    ffs = 485 + n_states + 9 * g.n_slots
    assert 44_000 < engine8 < 48_000                  # ~45.7k engine LUTs
    assert 0.6 < (engine8 + wrapper) / E.PR_LUTS < 0.7  # dpb=8: ~66% of 80k
    # dpb=1 — the decided build — is roughly a quarter of the engine cost.
    assert (engine8 / 4 + wrapper) < E.PR_LUTS / 4
    assert ffs < E.PR_FFS
    # The pre-S2 placeholder budget (216k) called this a 25% fit; the real
    # envelope is what makes dpb=1 a decision rather than a preference.
    assert E.PR_LUTS == 80_000


def test_corpus_group_hashes_are_unique_and_stable(corpus_groups):
    hashes = [g.group_hash(GEN, HARN) for g in corpus_groups]
    assert len(set(hashes)) == len(hashes)
    again = G.pack_groups(T.triage_file(CORPUS))
    assert [g.group_hash(GEN, HARN) for g in again] == hashes
    assert len({g.rp_child_id(GEN, HARN)
               for g in corpus_groups}) == len(hashes)


def test_new_rule_joins_an_existing_slot_with_the_same_anchor():
    old = G.pack_groups(triaged(rule(1, "aaa"), rule(2, "bbb")), group_max=4)
    new = G.repack_with_tombstones(
        old, triaged(rule(1, "aaa"), rule(2, "bbb"), rule(3, "aaa")),
        group_max=4)[0]
    assert new.n_slots == 2 and new.rule_count == 3
    assert new.sidecar()[0] == ("1:1", "1:3")


def test_a_rule_is_never_migrated_by_a_duplicate_anchor_elsewhere():
    """Two groups of one class can both hold anchor 'dup'.  Repacking must
    match rules by gid:sid, not by anchor, or rules migrate across groups —
    dirtying two groups and overflowing one past group_max."""
    base = triaged(rule(1, "dup"), rule(2, "bbb"),
                   rule(3, "dup"), rule(4, "ddd"))
    old = G.pack_groups(base, group_max=2)
    assert [g.rule_count for g in old] == [2, 2]
    assert old[0].slots[0].anchor == b"dup" and old[1].slots[0].anchor == b"dup"
    new = G.repack_with_tombstones(old, base, group_max=2)
    assert [g.rule_count for g in new] == [2, 2]
    assert [g.canonical_bytes() for g in new] == [g.canonical_bytes()
                                                  for g in old]


def test_insertion_respects_group_max_and_opens_a_new_group():
    old = G.pack_groups(triaged(rule(1, "aaa"), rule(2, "bbb")), group_max=2)
    new = G.repack_with_tombstones(
        old, triaged(rule(1, "aaa"), rule(2, "bbb"), rule(3, "ccc")),
        group_max=2)
    assert [g.rule_count for g in new] == [2, 1]
    assert [g.index for g in new] == [0, 1]
    assert new[0].group_hash(GEN, HARN) == old[0].group_hash(GEN, HARN)


def test_anchor_change_relocates_and_tombstones_the_old_slot():
    old = G.pack_groups(triaged(rule(1, "aaa"), rule(2, "bbb")), group_max=4)
    new = G.repack_with_tombstones(
        old, triaged(rule(1, "aaa"), rule(2, "BBB")), group_max=4)[0]
    assert [s.anchor for s in new.slots] == [b"aaa", b"bbb", b"BBB"]
    assert new.slots[1].tombstone is True
    assert new.sidecar()[2] == ("1:2",)


def test_corpus_repack_against_itself_is_a_no_op(corpus_groups):
    """SR9: an unchanged ruleset must dirty zero groups."""
    again = G.repack_with_tombstones(corpus_groups,
                                     T.triage_file(CORPUS))
    assert [g.name for g in again] == [g.name for g in corpus_groups]
    assert ([g.group_hash(GEN, HARN) for g in again]
            == [g.group_hash(GEN, HARN) for g in corpus_groups])
