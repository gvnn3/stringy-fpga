"""AC-S3-2 unit tests: SR3 content-chain lowering + clean-pcre fusion.

The property under test everywhere: the lowered pattern is a **necessary
condition** — it must fire on every input the rule's compiled conjuncts
would fire on (SR3 completeness, absolute), and its windows are supersets
of Snort's.  Precision (fewer false nominations) is best-effort;
completeness is not.
"""

import re as _stdre

import pytest

from pyro.hdl import automaton as _auto
from pyro.snort import groups as G
from pyro.snort import lowering as L
from pyro.snort import triage as T
from pyro.snort.rules import parse_rule


def low(rule_text):
    rule = parse_rule(rule_text, line_no=1)
    res = T.triage_rule(rule)
    assert res.tier == T.TIER_ANCHOR, res.reason
    return rule, res, L.lower_rule(rule, res)


def matches(lowered, buf: bytes) -> bool:
    """Does the lowered slot pattern accept anywhere in ``buf``?  Uses the
    real automaton — the same artifact the circuit compiles."""
    au = _auto.build(lowered.pattern, lowered.flags, _auto.ENC_BYTES)
    from pyro._circuit_model import _scan_windows
    return bool(_scan_windows(au, buf, 0))


RULE = 'alert tcp $EXTERNAL_NET any -> $HOME_NET any (%s sid:1;)'


# ---------------------------------------------------------------------------
# Anchor-only: byte-identical to the S1/S2-proven lowering
# ---------------------------------------------------------------------------
def test_single_content_is_the_legacy_lowering():
    _, _, lo = low(RULE % 'msg:"x"; content:"EVIL";')
    assert lo.pattern == _stdre.escape(b"EVIL")
    assert lo.flags == 0
    assert lo.tail_span == 4
    assert lo.chain_len == 1 and not lo.fused_pcre


def test_single_nocase_content_keeps_slot_level_ignorecase():
    _, _, lo = low(RULE % 'msg:"x"; content:"EviL", nocase;')
    assert lo.pattern == _stdre.escape(b"evil")   # folded storage form
    assert lo.flags == _stdre.IGNORECASE
    assert lo.anchor == b"evil" and lo.nocase


def test_legacy_slot_identity_is_preserved_for_anchor_only_rules():
    """A v1-style positional GroupSlot and the lowered entry must produce
    the same effective (pattern, flags) — cache keys for purely
    anchor-lowered slots survive except for the format-version bump."""
    _, _, lo = low(RULE % 'msg:"x"; content:"EviL", nocase;')
    slot = G.GroupSlot(0, b"evil", True, ())
    assert (lo.pattern, lo.flags) == (slot.pattern_eff, slot.flags_eff)


# ---------------------------------------------------------------------------
# Chains: distance/within windows
# ---------------------------------------------------------------------------
def test_two_content_chain_lowers_with_a_superset_window():
    _, _, lo = low(RULE % 'msg:"x"; content:"AAAA"; '
                          'content:"BBBB", distance 2, within 10;')
    assert lo.chain_len == 2
    assert lo.pattern == b"(?s:AAAA.{2,12}BBBB)"   # hi = D + W (superset)
    # completeness across the true window's extremes:
    assert matches(lo, b"..AAAA" + b"xx" + b"BBBB")          # gap = D
    assert matches(lo, b"AAAA" + b"x" * 12 + b"BBBB")        # gap = D+W
    assert not matches(lo, b"AAAA" + b"x" * 13 + b"BBBB")    # beyond superset
    assert not matches(lo, b"AAAABBBB")                      # gap < D
    assert lo.tail_span == 4 + 12 + 4


def test_anchor_is_the_second_content_chain_extends_backward():
    _, _, lo = low(RULE % 'msg:"x"; content:"AB"; '
                          'content:"LONGESTANCHOR", distance 0, within 4;')
    # anchor = longest positive content (the second); head link is bounded
    assert lo.chain_len == 2
    assert lo.pattern == b"(?s:AB.{0,4}LONGESTANCHOR)"


def test_unbounded_link_stops_the_chain():
    # no within => unbounded forward window => not lowerable
    _, _, lo = low(RULE % 'msg:"x"; content:"AAAA"; '
                          'content:"BB", distance 2;')
    assert lo.chain_len == 1
    assert lo.pattern == b"AAAA"
    assert "content" in lo.dropped


def test_negative_distance_stops_the_chain():
    _, _, lo = low(RULE % 'msg:"x"; content:"AAAA"; '
                          'content:"BB", distance -4, within 10;')
    assert lo.chain_len == 1


def test_oversized_window_stops_the_chain_never_narrows_it():
    _, _, lo = low(RULE % 'msg:"x"; content:"AAAA"; '
                          'content:"BB", distance 0, within 300;')
    assert lo.chain_len == 1              # 300 > MAX_LOWER_GAP: drop, not cap


def test_variable_distance_argument_stops_the_chain():
    _, _, lo = low(RULE % 'msg:"x"; byte_extract:1,0,var; content:"AAAA"; '
                          'content:"BB", distance var, within 10;')
    assert lo.chain_len == 1


def test_cursor_moving_option_between_contents_breaks_adjacency():
    _, _, lo = low(RULE % 'msg:"x"; content:"AAAA"; byte_jump:2,0; '
                          'content:"BB", distance 0, within 8;')
    assert lo.chain_len == 1              # byte_jump moved the cursor


def test_flow_metadata_between_contents_does_not_break_adjacency():
    _, _, lo = low(RULE % 'msg:"x"; content:"AAAA"; flowbits:isset,f; '
                          'content:"BB", within 8;')
    assert lo.chain_len == 2


def test_normalized_buffer_chain_stays_anchor_only():
    _, _, lo = low(RULE % 'msg:"x"; http_uri; content:"/a.php"; '
                          'content:"id=", distance 0, within 8;')
    assert lo.chain_len == 1              # raw-buffer restriction (misses!)
    assert L.OA_ANCHOR_STRIP in lo.oa_classes


def test_per_fragment_nocase_is_a_scoped_fold():
    _, _, lo = low(RULE % 'msg:"x"; content:"AAAA"; '
                          'content:"UsEr", nocase, within 6;')
    assert lo.pattern == b"(?s:AAAA.{0,6}(?i:user))"
    assert lo.flags == 0
    assert matches(lo, b"AAAA..uSeR")
    assert matches(lo, b"AAAA..USER")
    assert not matches(lo, b"aaaa..user")  # first fragment stays exact


# ---------------------------------------------------------------------------
# offset/depth prefixes — PDU-aligned (datagram, no service) rules only.
# The admission is evidence-driven: Snort 3 binds a raw-cursor depth to the
# current PDU *section* of an inspected flow, so a \A window against raw
# stream offset 0 MISSES (sid 509, demonstrated by the AC-S2-3
# differential).  udp/icmp/ip with no service inspector is where
# PDU == datagram == request buffer.
# ---------------------------------------------------------------------------
URULE = 'alert udp $EXTERNAL_NET any -> $HOME_NET any (%s sid:1;)'


def test_offset_depth_lowers_to_an_anchored_prefix_with_zero_tail_span():
    _, _, lo = low(URULE % 'msg:"x"; content:"HELO", offset 4, depth 20;')
    assert lo.pattern == b"(?s:\\A.{4,23}HELO)"
    assert lo.tail_span == 0              # \A-anchored: chunk 0 sees it whole
    assert matches(lo, b"...." + b"HELO")             # start = offset
    assert matches(lo, b"." * 20 + b"HELO")           # start = o+depth-len
    assert not matches(lo, b"." * 24 + b"HELO")       # beyond the superset
    assert not matches(lo, b"..HELO")                 # before offset


def test_tcp_offset_depth_is_never_prefix_lowered():
    # sid-509 lesson: a tcp flow's PDU sections are not stream-aligned.
    _, _, lo = low(RULE % 'msg:"x"; content:"HELO", offset 4, depth 20;')
    assert lo.pattern == b"HELO"
    assert L.OA_ANCHOR_STRIP in lo.oa_classes


def test_service_rule_is_never_prefix_lowered_even_on_udp():
    _, _, lo = low(URULE % 'msg:"x"; content:"HELO", depth 10; service:dns;')
    assert lo.pattern == b"HELO"


def test_depth_alone_lowers_and_offset_alone_does_not():
    _, _, lo = low(URULE % 'msg:"x"; content:"HELO", depth 10;')
    assert lo.pattern == b"(?s:\\A.{0,9}HELO)"
    _, _, lo2 = low(URULE % 'msg:"x"; content:"HELO", offset 10;')
    assert lo2.pattern == b"HELO"         # unbounded window: dropped conjunct
    assert "content" not in lo2.dropped   # the content itself IS lowered...
    assert L.OA_ANCHOR_STRIP in lo2.oa_classes  # ...its position is not


def test_window_past_chunk0_is_not_lowered():
    _, _, lo = low(URULE % 'msg:"x"; content:"HELO", offset 1400, depth 100;')
    assert lo.pattern == b"HELO"          # o+depth > CHUNK_BYTES: inadmissible


def test_normalized_buffer_offset_depth_is_not_lowered():
    _, _, lo = low(RULE % 'msg:"x"; http_uri; content:"/x", offset 0, depth 4;')
    assert lo.pattern == _stdre.escape(b"/x")


# ---------------------------------------------------------------------------
# pcre fusion
# ---------------------------------------------------------------------------
def test_clean_anchored_relative_pcre_fuses():
    _, _, lo = low(RULE % 'msg:"x"; content:"USER "; '
                          'pcre:"/^[a-z]{1,8}\\x0d/R";')
    assert lo.fused_pcre
    assert lo.pattern == b"(?s:USER\\ (?:[a-z]{1,8}\\x0d))"
    assert matches(lo, b"USER root\x0d")
    assert not matches(lo, b"USER 1234\x0d")
    assert lo.tail_span == 5 + 9


def test_unanchored_relative_pcre_does_not_fuse():
    _, _, lo = low(RULE % 'msg:"x"; content:"USER "; pcre:"/[a-z]{4}/R";')
    assert not lo.fused_pcre
    assert "pcre" in lo.dropped


def test_non_relative_pcre_does_not_fuse():
    _, _, lo = low(RULE % 'msg:"x"; content:"USER "; pcre:"/^[a-z]{4}/";')
    assert not lo.fused_pcre


def test_unbounded_quantifier_pcre_does_not_fuse():
    _, _, lo = low(RULE % 'msg:"x"; content:"USER "; pcre:"/^[a-z]+x/R";')
    assert not lo.fused_pcre              # unbounded span


def test_backref_pcre_never_fuses():
    _, _, lo = low(RULE % 'msg:"x"; content:"A "; '
                          'pcre:"/^(\\x22|\\x27)a\\1/R";')
    assert not lo.fused_pcre              # SF13 class backref, not clean


def test_pcre_i_flag_becomes_a_scoped_group():
    _, _, lo = low(RULE % 'msg:"x"; content:"CMD "; pcre:"/^abc/Ri";')
    assert lo.fused_pcre
    assert lo.pattern == b"(?s:CMD\\ (?i:abc))"
    assert matches(lo, b"CMD ABC")


def test_pcre_in_normalized_buffer_does_not_fuse():
    _, _, lo = low(RULE % 'msg:"x"; http_uri; content:"/a"; pcre:"/^x/R";')
    assert not lo.fused_pcre


# ---------------------------------------------------------------------------
# Span cap + totality
# ---------------------------------------------------------------------------
def test_span_cap_trims_links_instead_of_dropping_the_rule():
    body = ('msg:"x"; content:"AAAA"; '
            'content:"BBBB", within 250; content:"CCCC", within 250;')
    _, _, lo = low(RULE % body)
    # full chain would span 4+250+4+250+4 = 512 > SPAN_CAP: trailing link
    # trimmed, first link kept
    assert lo.chain_len == 2
    assert lo.tail_span == 4 + 250 + 4 <= L.SPAN_CAP


def test_lowering_is_deterministic():
    body = 'msg:"x"; content:"AAAA"; content:"BB", distance 1, within 9;'
    _, _, a = low(RULE % body)
    _, _, b = low(RULE % body)
    assert a == b


def test_every_corpus_pattern_compiles_and_respects_the_span_cap():
    import os
    corpus = os.path.join(os.path.dirname(__file__), "..", "..",
                          "third_party", "snort3-community-rules",
                          "snort3-community.rules")
    if not os.path.exists(corpus):
        pytest.skip("community ruleset not present")
    n_chain = n_fused = 0
    for rule, res in T.triage_file(corpus):
        if res.tier != T.TIER_ANCHOR or res.anchor is None:
            continue
        lo = L.lower_rule(rule, res)
        assert lo.tail_span <= L.SPAN_CAP
        _auto.build(lo.pattern, lo.flags, _auto.ENC_BYTES)  # must not raise
        if lo.chain_len > 1:
            n_chain += 1
        if lo.fused_pcre:
            n_fused += 1
    # Measured on the 2026-07 corpus snapshot: 245 chains, 36 fusions.
    # A parser/corpus change may move these; the pin is a canary, not a law.
    assert n_chain == 245
    assert n_fused == 36


# ---------------------------------------------------------------------------
# Group integration: dedup, identity, repack
# ---------------------------------------------------------------------------
def _entries(*rule_texts):
    triaged = [(r, T.triage_rule(r))
               for r in (parse_rule(t, line_no=i + 1)
                         for i, t in enumerate(rule_texts))]
    return G.groupable_entries(triaged), triaged


def test_same_anchor_different_chains_get_distinct_slots():
    a = RULE % 'msg:"x"; content:"AAAA"; sid:1;'
    b = ('alert tcp $EXTERNAL_NET any -> $HOME_NET any (msg:"y"; '
         'content:"AAAA"; content:"BB", within 8; sid:2;)')
    entries, triaged = _entries(a, b)
    gs = G.pack_groups(triaged)
    assert len(gs) == 1
    assert gs[0].n_slots == 2             # v1 would have deduped onto one


def test_canonical_bytes_carry_the_lowered_pattern():
    a = URULE % 'msg:"x"; content:"AAAA"; sid:1;'
    b = URULE % 'msg:"x"; content:"AAAA", depth 30; sid:1;'
    _, ta = _entries(a)
    _, tb = _entries(b)
    ga, gb = G.pack_groups(ta)[0], G.pack_groups(tb)[0]
    assert ga.canonical_bytes() != gb.canonical_bytes()


def test_repack_keeps_placement_iff_lowering_unchanged():
    base = ('alert tcp any any -> any 25 (msg:"a"; content:"AAAA"; sid:1;)',
            'alert tcp any any -> any 25 (msg:"b"; content:"BBBB"; '
            'content:"CC", within 8; sid:2;)')
    _, t0 = _entries(*base)
    g0 = G.pack_groups(t0)
    # same rules again: nothing moves
    _, t1 = _entries(*base)
    g1 = G.repack_with_tombstones(g0, t1)
    assert [s.pattern_eff for s in g1[0].slots] == \
           [s.pattern_eff for s in g0[0].slots]
    assert g1[0].canonical_bytes() == g0[0].canonical_bytes()
    # sid:2's window widens: its lowering changed, so it must move to a new
    # slot and leave a tombstone
    changed = (base[0],
               'alert tcp any any -> any 25 (msg:"b"; content:"BBBB"; '
               'content:"CC", within 12; sid:2;)')
    _, t2 = _entries(*changed)
    g2 = G.repack_with_tombstones(g0, t2)
    slots = g2[0].slots
    assert slots[1].tombstone             # old chain slot emptied, kept
    assert slots[2].pattern_eff == b"(?s:BBBB.{0,12}CC)"


def test_manifest_declares_sr4_classes_and_dropped_lists():
    r = ('alert tcp any any -> any 25 (msg:"a"; flow:established; '
         'content:"AAAA", nocase; byte_test:1,>,0,0; sid:9;)')
    _, t0 = _entries(r)
    g = G.pack_groups(t0)[0]
    from pyro.hdl import generator as gen
    m = g.manifest(gen.GENERATOR_VERSION, gen.HARNESS_VERSION)
    assert set(m["over_approx_classes"]) >= {
        L.OA_DROPPED, L.OA_CASE_FOLD, L.OA_CHUNK_OVERLAP}
    row = m["slots"][0]["rules"][0]
    assert "flow" in row["dropped"] and "byte_test" in row["dropped"]
    assert m["slots"][0]["tail_span"] == 4
    assert m["max_tail_span"] == 4
