"""Public fault-injection seam ``pyro.testing`` (spec §9.1 R67).

The seams are gated behind ``PYRO_ENABLE_TEST_HOOKS=1`` (sampled at R35a
points),
deterministic, observable via ``pyro.re.stats()`` (R66), and NEVER change
caller-visible results relative to CPython (R16 — the fallback nets catch every
injected fault).  All env mutation is via ``monkeypatch`` + ``refresh_env`` so
the
cached snapshot is clean for the next test.
"""

import re

import pytest

import pyro
import pyro.re as pre
import pyro.testing as pt
from pyro import _route
from pyro.synth import residency as res

LARGE = "x" * (_route.S_MIN + 64)     # >= S_min so the call reaches the model


@pytest.fixture
def hooks_on(monkeypatch, tmp_path):
    monkeypatch.setenv("PYRO_ENABLE_TEST_HOOKS", "1")
    monkeypatch.setenv("PYRO_CACHE_DIR", str(tmp_path))
    pyro.refresh_env()
    res.reset_manager()
    _route.reset_stats()
    pt.reset()
    yield
    pt.reset()
    res.reset_manager()


# --- namespace shape + interposition exclusion (R67/§7.2) ------------------
def test_namespace_importable_and_callable():
    import pyro.testing
    for name in ("inject_device_error", "inject_synth_failure",
                 "inject_false_positive", "reset"):
        assert callable(getattr(pyro.testing, name))


def test_seam_not_patched_onto_re(hooks_on):
    pyro.install()
    try:
        assert not hasattr(re, "inject_device_error")
        assert not hasattr(re, "testing")
    finally:
        pyro.uninstall()


# --- gate: inert no-op when PYRO_ENABLE_TEST_HOOKS is not set (R67) ---------
def test_gate_off_is_inert(monkeypatch):
    monkeypatch.delenv("PYRO_ENABLE_TEST_HOOKS", raising=False)
    pyro.refresh_env()
    _route.reset_stats()
    pt.inject_device_error("device", 5)       # inert: gate off
    pt.inject_false_positive(r"tok\d+", 0, 5)  # inert
    subj = LARGE + " tok9"
    m = pre.search(r"tok\d+", subj)
    assert m.group(0) == "tok9"
    s = pre.stats()
    assert s["fallback_after_error"] == 0      # nothing injected
    assert s["model"] == 1                       # served by the model (R7)


# --- inject_device_error drives the R52 fallback-retry path (R52/R66) -------
def test_inject_device_error_drives_r52(hooks_on):
    pt.inject_device_error("device", 1)
    subj = LARGE + " tok9"
    m = pre.search(r"tok\d+", subj)
    assert isinstance(m, re.Match)             # genuine fallback object (R29)
    assert m.group(0) == re.search(r"tok\d+", subj).group(0)
    s = pre.stats()
    assert s["fallback_after_error"] == 1
    assert s["device_errors"] == 1


def test_inject_timeout_kind_counts_as_error_fallback(hooks_on):
    pt.inject_device_error("timeout", 2)
    subj = LARGE + " tok9"
    pre.search(r"tok\d+", subj)
    pre.search(r"tok\d+", subj)
    assert pre.stats()["fallback_after_error"] == 2


def test_inject_device_error_count_is_bounded(hooks_on):
    pt.inject_device_error("device", 1)
    subj = LARGE + " tok9"
    pre.search(r"tok\d+", subj)                 # 1st: error -> fallback
    pre.search(r"tok\d+", subj)                 # 2nd: recovered -> model
    s = pre.stats()
    assert s["fallback_after_error"] == 1
    assert s["model"] == 1


# --- inject_false_positive: re-verified away, never leaks (R19) -------------
def test_inject_false_positive_reverified_identical(hooks_on):
    subj = LARGE + " a1 b22 c333"
    pt.inject_false_positive(r"[a-z]\d+", 0, 1)
    got = [m.span() for m in pre.finditer(r"[a-z]\d+", subj)]
    ref = [m.span() for m in re.finditer(r"[a-z]\d+", subj)]
    assert got == ref                           # spurious window did not leak
    assert pre.stats()["fallback_after_error"] >= 1


# --- inject_synth_failure drives R65 permanent fallback (R65/R66) -----------
def test_inject_synth_failure_permanent_fallback(hooks_on):
    pt.inject_synth_failure(r"synf\d+")
    pyro.prewarm(r"synf\d+")                     # launch -> injected failure
    mgr = res.get_manager()
    assert mgr.tier(r"synf\d+", 0, 1) == "fallback_only"
    s = mgr.stats()
    assert s["synth_failed"] == 1
    assert s["synth_launched"] == 1


# --- reset clears all injected faults (R67) --------------------------------
def test_reset_clears_injections(hooks_on):
    pt.inject_device_error("device", 5)
    pt.reset()
    subj = LARGE + " tok9"
    pre.search(r"tok\d+", subj)
    assert pre.stats()["fallback_after_error"] == 0


def test_injection_is_deterministic(hooks_on):
    subj = LARGE + " tok9"
    for _ in range(3):
        pt.reset()
        _route.reset_stats()
        pt.inject_device_error("device", 1)
        pre.search(r"tok\d+", subj)
        assert pre.stats()["fallback_after_error"] == 1
