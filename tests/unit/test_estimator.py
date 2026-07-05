"""Unit tests for the resource estimator (pyro.hdl.estimator, R11-R13).

Boundary tests for the MAX_* complexity bounds and the advertised PR-region
budget, plus consistency with the classifier's eligibility decision (the
estimator must never disagree with _classify on the MAX_* boundaries).
"""
import re

import pytest

from pyro import hdl, _classify
from pyro.hdl import estimator as E


def est(pat, flags=0):
    return hdl.estimate(pat, flags)


# --- R13: advertised minimums --------------------------------------------

def test_budget_meets_r13_minimums():
    b = hdl.budget()
    assert b["max_states"] >= 1024
    assert b["max_patterns"] >= 256
    assert b["max_repeat"] >= 255


def test_budget_advertises_pr_region_resources():
    b = hdl.budget()
    for k in ("pr_luts", "pr_ffs", "pr_bram_kb", "pr_dsps", "pr_partitions"):
        assert k in b and isinstance(b[k], int)
    assert b["pr_partitions"] >= 1  # single-tenant region (R64)


# --- R11/R12: eligible patterns carry resource numbers -------------------

def test_eligible_pattern_reports_resources():
    r = est(r"foo\d+bar")
    assert r.eligible is True
    res = r.resources
    assert res["luts"] > 0 and res["ffs"] > 0
    assert res["automaton_states"] > 0
    assert res["num_patterns"] == 1
    assert res["byte_edges"] >= 1


def test_resources_fit_within_budget_for_eligible():
    b = hdl.budget()
    r = est(r"(abcde){200}")  # largest eligible in the classifier's boundary
    assert r.eligible is True
    assert r.resources["luts"] <= b["pr_luts"]
    assert r.resources["ffs"] <= b["pr_ffs"]


# --- R12/R13: over-bound patterns are rejected ---------------------------

def test_over_max_repeat_rejected():
    r = est("a{300}")
    assert r.eligible is False
    assert r.resources is None


def test_over_max_states_rejected():
    r = est("(abcde){255}")  # 5*255 = 1275 > MAX_STATES(1024)
    assert r.eligible is False
    assert r.reason == "exceeds MAX_STATES"


def test_under_max_states_accepted():
    assert est("(abcde){200}").eligible is True  # 1000 <= 1024


# --- R10: unsupported constructs are fallback-only ------------------------

@pytest.mark.parametrize("pat", [r"(a)\1", r"(?=x)", r"(?<=y)", r"(?>ab)"])
def test_unsupported_constructs_rejected(pat):
    r = est(pat)
    assert r.eligible is False
    assert r.resources is None


# --- consistency with the classifier -------------------------------------

@pytest.mark.parametrize("pat", [
    "abc", r"[a-z]+\d", "a{255}", "a{256}", "(abcde){200}", "(abcde){255}",
    r"(a)\1", "^ab$", r"\bword\b", "é+",
])
def test_estimator_agrees_with_classifier_on_eligibility(pat):
    assert est(pat).eligible == _classify.classify(pat, 0).eligible


def test_estimator_is_pure_and_deterministic():
    a = est(r"a(b|c)*d{2,5}")
    b = est(r"a(b|c)*d{2,5}")
    assert a == b
