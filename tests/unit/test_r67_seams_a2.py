"""R67 v2.5.0 seam extensions (amendment A2): ``await_synthesis`` and
``set_strict_residency`` (R51b-strict).

Covers, per the A2 adoption:
  * seam gating — both new seams are importable-but-inert when
    PYRO_ENABLE_TEST_HOOKS is off (``await_synthesis`` returns the current tier
    immediately; ``set_strict_residency`` does nothing);
  * ``await_synthesis`` returns a terminal tier (``"resident"`` /
    ``"fallback_only"``), genuinely waits for the background service, and on
    timeout returns the last-observed tier (never raises);
  * ``set_strict_residency`` flips the dispatch-counter edge (not-resident =>
    genuine ``fallback``, never ``fallback_after_error``; resident => ``model``)
    while results stay byte-identical, and ``reset()`` restores default R51b.

All env mutation is via ``monkeypatch`` + ``refresh_env`` so the cached snapshot
is clean for the next test (R35a discipline), mirroring test_testing_hooks.py.
"""

import re
import threading
import time

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


@pytest.fixture
def hooks_off(monkeypatch, tmp_path):
    monkeypatch.delenv("PYRO_ENABLE_TEST_HOOKS", raising=False)
    monkeypatch.setenv("PYRO_CACHE_DIR", str(tmp_path))
    pyro.refresh_env()
    res.reset_manager()
    _route.reset_stats()
    yield
    pt.reset()                        # unconditional; also clears strict flag
    res.reset_manager()


# --- namespace shape (R67 v2.5.0) -------------------------------------------
def test_namespace_exports_new_seams():
    assert callable(pt.await_synthesis)
    assert callable(pt.set_strict_residency)
    assert "await_synthesis" in pt.__all__
    assert "set_strict_residency" in pt.__all__


# --- gate off: importable but inert (R67) ------------------------------------
def test_await_synthesis_gate_off_returns_current_tier_immediately(hooks_off):
    t0 = time.monotonic()
    tier = pt.await_synthesis(r"gateoff\d+", timeout=60.0)
    assert time.monotonic() - t0 < 5.0          # no 60 s wait: gate off
    assert tier == "cold"                        # eligible, never launched


def test_set_strict_residency_gate_off_is_inert(hooks_off):
    pt.set_strict_residency(True)                # no-op: gate off
    subj = LARGE + " tok9"
    m = pre.search(r"tok\d+", subj)
    assert m.group(0) == "tok9"
    s = pre.stats()
    assert s["model"] == 1                       # R51b standin still serves
    assert s["fallback"] == 0


# --- await_synthesis: terminal tiers (R4/R31) --------------------------------
def test_await_synthesis_terminal_fallback_only(hooks_on):
    P = r"awfail\d+"
    pt.inject_synth_failure(P)
    pyro.prewarm(P)                              # launch -> injected failure (R65)
    t0 = time.monotonic()
    assert pt.await_synthesis(P, timeout=10.0) == "fallback_only"
    assert time.monotonic() - t0 < 5.0           # already terminal: no wait


def test_await_synthesis_terminal_resident(hooks_on):
    P = r"awres\d+"
    pyro.prewarm(P)
    assert res.get_manager().drain(timeout=30.0)
    pre.search(P, LARGE + " awres1")             # warm -> promote -> resident
    assert pt.await_synthesis(P, timeout=10.0) == "resident"


def test_await_synthesis_ineligible_pattern_is_terminal(hooks_on):
    # A backreference is never HW-eligible: circuit_status is "fallback_only"
    # (R31) and await_synthesis returns it immediately.
    t0 = time.monotonic()
    assert pt.await_synthesis(r"(a)\1", timeout=10.0) == "fallback_only"
    assert time.monotonic() - t0 < 5.0


def test_await_synthesis_waits_for_background_service(hooks_on):
    # Genuine wait: synthesis runs in the background service (R63) and the
    # warm->resident promotion happens on a dispatch from ANOTHER thread, so
    # await_synthesis must actually poll until the terminal tier appears.
    P = r"awwait\d+"
    subj = LARGE + " awwait3"
    stop = threading.Event()

    def dispatcher():
        while not stop.is_set():
            pre.search(P, subj)
            stop.wait(0.02)

    t = threading.Thread(target=dispatcher, daemon=True)
    t.start()
    try:
        pyro.prewarm(P)
        tier = pt.await_synthesis(P, timeout=30.0)
    finally:
        stop.set()
        t.join(10.0)
    assert tier == "resident"


def test_await_synthesis_timeout_returns_last_observed(hooks_on):
    # Eligible but never launched (no prewarm, count << N_synth): the tier stays
    # "cold" forever, so await_synthesis must run out its timeout and return the
    # last-observed tier rather than raising.
    P = r"awcold\d+"
    t0 = time.monotonic()
    tier = pt.await_synthesis(P, timeout=0.3)
    elapsed = time.monotonic() - t0
    assert tier == "cold"
    assert elapsed >= 0.25                       # it genuinely waited
    assert elapsed < 10.0                        # ...but honored the timeout


# --- set_strict_residency: the R51b-strict dispatch edge (AC-3-2) ------------
def test_strict_residency_flips_dispatch_edge(hooks_on):
    P = r"strict\d+"
    subj = LARGE + " strict7"
    pt.set_strict_residency(True)
    _route.reset_stats()

    # Not resident (cold): genuine fallback dispatch — counted as a normal
    # 'fallback', NEVER 'fallback_after_error' (R51b-strict).
    m = pre.search(P, subj)
    assert m.group(0) == "strict7"
    s = pre.stats()
    assert s["fallback"] == 1
    assert s["model"] == 0
    assert s["fallback_after_error"] == 0

    # Synthesize + make resident; the SAME call shape now counts as 'model'.
    pyro.prewarm(P)
    assert res.get_manager().drain(timeout=30.0)
    m2 = pre.search(P, subj)                     # warm -> promote -> resident
    assert m2.group(0) == "strict7"
    s = pre.stats()
    assert s["model"] == 1
    assert s["fallback"] == 1                    # unchanged from the cold call
    assert s["fallback_after_error"] == 0
    # Residency bookkeeping unchanged in strict mode (R64/R64a).
    assert s["circuits_resident"] == 1
    assert s["pr_loads"] >= 1
    assert pt.await_synthesis(P, timeout=5.0) == "resident"


def test_strict_residency_results_byte_identical(hooks_on):
    P = r"[a-z]\d+"
    subj = LARGE + " a1 b22 c333"
    ref = [m.span() for m in re.finditer(P, subj)]

    pt.set_strict_residency(True)
    _route.reset_stats()
    got_strict = [m.span() for m in pre.finditer(P, subj)]
    s = pre.stats()
    assert s["fallback"] >= 1 and s["model"] == 0   # served by genuine fallback

    pt.set_strict_residency(False)
    _route.reset_stats()
    got_default = [m.span() for m in pre.finditer(P, subj)]
    s = pre.stats()
    assert s["model"] >= 1                          # R51b standin restored

    assert got_strict == ref
    assert got_default == ref


def test_reset_restores_default_r51b(hooks_on):
    P = r"rst\d+"
    subj = LARGE + " rst1"
    pt.set_strict_residency(True)
    pt.reset()                                   # clears strict residency (R67)
    _route.reset_stats()
    m = pre.search(P, subj)
    assert m.group(0) == "rst1"
    s = pre.stats()
    assert s["model"] == 1                       # R51b precedence is back
    assert s["fallback"] == 0
