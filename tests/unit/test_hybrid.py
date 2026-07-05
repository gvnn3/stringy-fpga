"""Unit tests for hybrid match objects and routing (R17-R23, R51-R53).

These exercise the model path directly (PYRO_FORCE_MODEL) so that the
FPGA/model-plus-hybrid logic is under test rather than the fallback shortcut.
"""
import re

import pytest

import pyro
import pyro.re as pre
from pyro import _model, _route


@pytest.fixture(autouse=True)
def _force_model(monkeypatch):
    monkeypatch.setenv("PYRO_FORCE_MODEL", "1")
    monkeypatch.delenv("PYRO_DISABLE", raising=False)
    pyro.refresh_env()  # apply env to the cached snapshot (R35d)
    pre.purge()
    _route.reset_stats()
    yield
    pre.purge()


# --- R20: group-0 access must NOT trigger a CPython re-run ----------------

def test_group0_access_no_rerun():
    m = pre.search(r"(\d+)-(\d+)", "xx 42-99 yy")
    assert m.group(0) == "42-99"
    assert m.span(0) == (3, 8)
    assert m.start() == 3 and m.end() == 8
    assert m[0] == "42-99"
    # No subgroup observed yet: the lazy re-run must not have happened.
    assert m._real is None


def test_group_gt0_access_triggers_single_rerun():
    m = pre.search(r"(\d+)-(\d+)", "xx 42-99 yy")
    assert m.group(1) == "42"
    assert m._real is not None          # exactly one re-run now cached
    cached = m._real
    assert m.group(2) == "99"
    assert m._real is cached            # not re-run again


# --- R18/R16: hybrid groups/spans byte-identical to CPython ---------------

@pytest.mark.parametrize("pat,subj", [
    (r"(\d+)-(\d+)", "42-99"),
    (r"(?P<a>\w+)@(?P<b>\w+)", "user@host"),
    (r"(a+)(b+)?", "aaa"),
    (r"(foo|bar)(baz)?", "foobaz"),
])
def test_groups_match_cpython(pat, subj):
    m = pre.search(pat, subj)
    ref = re.search(pat, subj)
    assert m.span(0) == ref.span(0)
    assert m.groups() == ref.groups()
    assert m.groupdict() == ref.groupdict()
    for i in range(ref.re.groups + 1):
        assert m.span(i) == ref.span(i)
        assert m.group(i) == ref.group(i)
    assert m.lastindex == ref.lastindex
    assert m.lastgroup == ref.lastgroup


# --- R23: greedy vs lazy group boundaries (proves R17/R18) ----------------

@pytest.mark.parametrize("pat", [r"(a+)(a+)", r"(a+?)(a+)", r"<(.*)>", r"<(.*?)>"])
def test_greedy_lazy_boundaries(pat):
    subj = "<aaaa>bbbb>"
    m = pre.search(pat, subj)
    ref = re.search(pat, subj)
    if ref is None:
        assert m is None
    else:
        assert m.span(0) == ref.span(0)
        assert m.groups() == ref.groups()


# --- R22: empty-match / finditer sequence identical -----------------------

@pytest.mark.parametrize("pat,subj", [
    (r"a*", "bbb"),
    (r"a*", "baaab"),
    (r"", "abc"),
    (r"\b", "a bc de"),
    (r"x?", "xyx"),
])
def test_finditer_empty_matches(pat, subj):
    got = [x.span(0) for x in pre.finditer(pat, subj)]
    ref = [x.span(0) for x in re.finditer(pat, subj)]
    assert got == ref


# --- fullmatch reproduction incl. lazy quantifier -------------------------

@pytest.mark.parametrize("pat,subj,ok", [
    (r"a+", "aaa", True),
    (r"a+?", "aaa", True),   # forced to consume all despite laziness
    (r"a", "ab", False),
    (r"(\d)(\d)", "12", True),
])
def test_fullmatch(pat, subj, ok):
    m = pre.fullmatch(pat, subj)
    ref = re.fullmatch(pat, subj)
    if not ok:
        assert m is None and ref is None
    else:
        assert m.span(0) == ref.span(0)
        assert m.groups() == ref.groups()


# --- R21: astral-codepoint offsets ----------------------------------------

def test_astral_offsets():
    subj = "a\U0001F600b\U0001F638cd"
    for pat in ["b", "cd", r"\w+", "."]:
        got = [x.span(0) for x in pre.finditer(pat, subj)]
        ref = [x.span(0) for x in re.finditer(pat, subj)]
        assert got == ref, pat


# --- R52: device error -> transparent fallback + counter ------------------

def test_device_error_fallback():
    _route.reset_stats()
    _model.get_model().fail_next_scan = True
    m = pre.search(r"(\d+)", "abc 123")
    assert m.group(0) == "123"
    assert m.group(1) == "123"
    s = pre.stats()
    assert s["fallback_after_error"] == 1
    assert s["device_errors"] == 1


def test_unverified_window_reverified():
    # Fault injection: model reports windows without the verified bit; the
    # router must re-verify them before returning (R19) and results stay
    # correct.
    _model.get_model().unverify_windows = True
    try:
        got = [x.span(0) for x in pre.finditer(r"\d+", "a1b22c333")]
        ref = [x.span(0) for x in re.finditer(r"\d+", "a1b22c333")]
        assert got == ref
    finally:
        _model.get_model().unverify_windows = False


# --- R22/R18/R52 regression: empty-adjacency group reconstruction ---------

@pytest.mark.parametrize("pat,subj", [
    (r"(?P<g>_??)?", "_"),                     # minimized reproducer
    (r"(?P<g>_??)?", "x_y"),
    (r"(?P<g200>_??)?|c{2,}1{2,}c{2}", "\ndaxc\n_b_d "),  # original AC-0-2b triple
])
def test_empty_adjacency_group_reconstruction(pat, subj):
    # finditer yields BOTH an empty match and a non-empty match at the SAME
    # start (CPython empty-adjacency rule, R22).  The non-empty match's lazy
    # group re-run (R18) must reconstruct via a window-anchored fullmatch, not a
    # plain anchored match() (which returns the empty match and mismatched the
    # window, raising _VerifyError to the caller — the fixed defect).
    # Root-caused from the AC-0-2b property suite, seed 0xC0FFEE.
    got = [(m.span(0), m.groups(), tuple(sorted(m.groupdict().items())))
           for m in pre.finditer(pat, subj)]
    ref = [(m.span(0), m.groups(), tuple(sorted(m.groupdict().items())))
           for m in re.finditer(pat, subj)]
    assert got == ref
    # Reconstruction is faithful: no spurious error-fallback, and nothing raised.
    assert pre.stats()["fallback_after_error"] == 0


# --- R53: determinism model vs fallback -----------------------------------

def test_determinism_model_vs_fallback(monkeypatch):
    pat, subj = r"(\w+)\s(\w+)", "hello world"
    model_m = pre.search(pat, subj)
    model_res = (model_m.span(0), model_m.groups())
    monkeypatch.setenv("PYRO_DISABLE", "1")
    pyro.refresh_env()
    pre.purge()
    fb_m = pre.search(pat, subj)
    fb_res = (fb_m.span(0), fb_m.groups())
    assert model_res == fb_res == (re.search(pat, subj).span(0),
                                   re.search(pat, subj).groups())
