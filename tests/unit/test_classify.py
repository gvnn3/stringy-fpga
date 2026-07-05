"""Unit tests for the L2 classifier (pyro._classify).

TDD for the HW-eligibility decision (R8-R15, §5). These tests exercise the
pure classification function directly, independent of routing/model.
"""
import re

import pytest

from pyro import _classify


def elig(pattern, flags=0):
    return _classify.classify(pattern, flags)


# --- R9: supported constructs are HW-eligible -----------------------------

@pytest.mark.parametrize("pat", [
    "abc",                       # literals
    "[a-z]", "[^a-z]", r"\d", r"\D", r"\w", r"\W", r"\s", r"\S", ".",
    "a|b|c",                     # alternation
    "a*", "a+", "a?", "a*?", "a+?", "a??",
    "a{3}", "a{2,5}", "a{2,}", "a{2,5}?",
    "^abc$", r"\Aabc\Z", r"\bword\b", r"\Bx\B",
    "(?:abc)",                   # non-capturing group
    "(abc)", "(?P<name>abc)",    # capturing groups (structure accepted)
    "a(b|c)*d",
])
def test_supported_constructs_eligible(pat):
    r = elig(pat)
    assert r.eligible is True, (pat, r.reason)
    assert isinstance(r.states, int)


@pytest.mark.parametrize("flag", [re.I, re.M, re.S, re.A, re.X, re.U])
def test_supported_flags_eligible(flag):
    assert elig("ab.c", flag).eligible is True


# --- R10: unsupported constructs force fallback ---------------------------

@pytest.mark.parametrize("pat", [
    r"(a)\1",                    # backreference (numeric)
    r"(?P<n>a)(?P=n)",           # backreference (named)
    r"(?=foo)",                  # lookahead
    r"(?!foo)",                  # negative lookahead
    r"(?<=foo)",                 # lookbehind
    r"(?<!foo)",                 # negative lookbehind
    r"(a)(?(1)b|c)",             # conditional
    r"(?>ab)",                   # atomic group
    r"a*+",                      # possessive quantifier
])
def test_unsupported_constructs_fallback(pat):
    r = elig(pat)
    assert r.eligible is False, (pat, r.reason)
    assert r.states is None


# --- R12/R13: capacity limits ---------------------------------------------

def test_bounded_repeat_over_max_repeat_fallback():
    # MAX_REPEAT default is 255; a bound above it must be fallback-only.
    assert elig("a{300}").eligible is False
    assert elig("a{0,1000}").eligible is False


def test_bounded_repeat_within_limits_eligible():
    assert elig("a{255}").eligible is True


def test_over_max_states_fallback():
    # 500 * 3 states = 1500 > MAX_STATES(1024)
    assert elig("(abcd){500}").eligible is False


# --- R15: IGNORECASE full folding ----------------------------------------

def test_full_case_fold_fallback_str():
    # 'ß' casefolds to 'ss' (multi-char) -> fallback under IGNORECASE, str mode
    assert elig("ß", re.I).eligible is False


def test_simple_case_fold_eligible():
    assert elig("A", re.I).eligible is True


def test_full_case_fold_without_ignorecase_eligible():
    # Without IGNORECASE, 'ß' is just a literal -> eligible.
    assert elig("ß", 0).eligible is True


def test_bytes_ignorecase_eligible():
    # bytes mode: ASCII folding only, no full folds possible.
    assert elig(b"ABC", re.I).eligible is True


# --- R8: determinism / purity --------------------------------------------

def test_classification_deterministic():
    a = elig("a(b|c)*d{2,5}")
    b = elig("a(b|c)*d{2,5}")
    assert (a.eligible, a.reason, a.states) == (b.eligible, b.reason, b.states)


# --- R14: bytes vs str both classifiable ---------------------------------

def test_bytes_pattern_eligible():
    assert elig(rb"[a-z]+\d").eligible is True
