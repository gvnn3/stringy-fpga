"""Unit tests for the byte-automaton builder (pyro.hdl.automaton, R9/R14).

These exercise the per-construct lowering of §5.1 into the shared automaton IR:
that each construct produces the expected kind of edges, that bytes/str (UTF-8)
modes differ correctly, and that the build is deterministic (R8).
"""
import re

import pytest

from pyro import hdl
from pyro.hdl import automaton as A


def build(pat, flags=0, enc=None):
    return hdl.build_automaton(pat, flags, enc)


def edge_kinds(au):
    kinds = {A.E_BYTE: 0, A.E_ASSERT: 0, A.E_EPS: 0}
    for s in range(au.n_states):
        for e in au.edges[s]:
            kinds[e.kind] += 1
    return kinds


def byte_edge_sets(au):
    return [frozenset(e.payload) for s in range(au.n_states)
            for e in au.edges[s] if e.kind == A.E_BYTE]


# --- R9: literals ---------------------------------------------------------

def test_literal_chain_bytes_mode():
    au = build(b"abc")
    sets = byte_edge_sets(au)
    assert sets == [frozenset({0x61}), frozenset({0x62}), frozenset({0x63})]


def test_str_ascii_literal_single_byte():
    au = build("A")
    assert byte_edge_sets(au) == [frozenset({0x41})]


def test_str_non_ascii_literal_is_utf8_chain():
    # 'é' == U+00E9 == bytes C3 A9 -> two byte edges.
    au = build("é")
    assert byte_edge_sets(au) == [frozenset({0xC3}), frozenset({0xA9})]


def test_astral_literal_is_four_byte_chain():
    # U+1D518 == F0 9D 94 98
    au = build("\U0001D518")
    assert byte_edge_sets(au) == [
        frozenset({0xF0}), frozenset({0x9D}), frozenset({0x94}), frozenset({0x98})]


# --- R9: character classes ------------------------------------------------

def test_range_bytes_mode_exact():
    au = build(b"[a-c]")
    assert byte_edge_sets(au) == [frozenset({0x61, 0x62, 0x63})]


def test_negated_class_bytes_mode_complement():
    au = build(b"[^a]")
    (bs,) = byte_edge_sets(au)
    assert 0x61 not in bs
    assert len(bs) == 255


def test_digit_class_bytes_mode():
    au = build(rb"\d")
    (bs,) = byte_edge_sets(au)
    assert bs == frozenset(b"0123456789")


def test_str_category_admits_multibyte_without_ascii_flag():
    # \w in str mode (no ASCII flag) must be able to consume multi-byte code
    # points (Unicode word chars), so lead-byte ranges for len 2..4 appear.
    au = build(r"\w")
    sets = byte_edge_sets(au)
    lead_bytes = set().union(*sets)
    assert 0xC2 in lead_bytes  # 2-byte lead present
    assert 0xF0 in lead_bytes  # 4-byte lead present


def test_str_category_ascii_flag_stays_single_byte():
    au = build(r"\w", re.A)
    sets = byte_edge_sets(au)
    # ASCII-flag \w is [0-9A-Za-z_] only: every byte edge is a subset of ASCII.
    for bs in sets:
        assert max(bs) < 0x80


# --- R9: anchors ----------------------------------------------------------

def test_anchor_is_zero_width_assert_edge():
    au = build(r"\bx")
    assert edge_kinds(au)[A.E_ASSERT] == 1


def test_caret_dollar_are_asserts():
    au = build("^a$")
    assert edge_kinds(au)[A.E_ASSERT] == 2


# --- R9: alternation / groups / repeats -----------------------------------

def test_alternation_creates_epsilon_branching():
    # (single-char alternation is folded to a class by the parser, so use
    # multi-char branches to keep a real BRANCH node)
    au = build("ab|cd")
    assert edge_kinds(au)[A.E_EPS] >= 2  # one eps into each branch


def test_bounded_repeat_expands_states():
    # {3} expands the body three times -> three byte edges for the literal.
    au = build("a{3}")
    assert len(byte_edge_sets(au)) == 3


def test_star_has_epsilon_loop():
    au = build("a*")
    assert edge_kinds(au)[A.E_EPS] >= 1
    assert len(byte_edge_sets(au)) == 1  # one body copy, reused via loop


def test_greedy_and_lazy_lower_to_same_language_shape():
    # MAX_REPEAT vs MIN_REPEAT recognize the same language (span selection is
    # reconciled later, R17); the byte-edge multiset is identical.
    greedy = byte_edge_sets(build("a+"))
    lazy = byte_edge_sets(build("a+?"))
    assert greedy == lazy


# --- R8: determinism ------------------------------------------------------

def test_build_is_deterministic():
    a = build(r"a(b|c)*d{2,4}")
    b = build(r"a(b|c)*d{2,4}")
    assert a.n_states == b.n_states
    assert a.byte_edges() == b.byte_edges()


def test_byteset_to_ranges_canonical():
    assert A.byteset_to_ranges(frozenset({1, 2, 3, 7, 8})) == ((1, 3), (7, 8))
    assert A.byteset_to_ranges(frozenset()) == ()
