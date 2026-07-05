"""Router <-> residency integration (R51 step 5 amended, R65/R66, AC-1-6).

The dispatch decision consults circuit residency: a *permanently-fallback*
pattern (R65) is routed to fallback as ORDINARY routing (counted `fallback`,
never `fallback_after_error`, AC-1-6), while results stay byte-identical.  The
model-as-resident stand-in (R7) is preserved, and stats() merges the R66
lifecycle counters with the dispatch counters.
"""

import re

import pytest

import pyro
import pyro.re as pre
import pyro.hdl as hdl
from pyro import _route
from pyro.synth import residency as res
from pyro.synth import make_key
from pyro.synth.toolchain import TOOLCHAIN_VERSION, SHELL_VERSION


def _large(needle):
    return "x" * (_route.S_MIN + 64) + " " + needle


@pytest.fixture
def temp_global(tmp_path, monkeypatch):
    """Point the global residency manager at a temp cache dir (hermetic)."""
    monkeypatch.setenv("PYRO_CACHE_DIR", str(tmp_path))
    res.reset_manager()
    mgr = res.get_manager()
    yield mgr
    res.reset_manager()


# --- stats() merges dispatch + lifecycle counters (R66) --------------------
def test_stats_has_lifecycle_and_dispatch_keys(temp_global):
    s = pre.stats()
    for k in ("model", "fallback", "fallback_after_error",
              "synth_launched", "synth_succeeded", "synth_failed",
              "circuits_synthesizing", "circuits_resident",
              "circuits_evicted", "pr_loads"):
        assert k in s


# --- permanent fallback forces ordinary routing (R65/AC-1-6) ---------------
def test_permanent_fallback_routes_as_ordinary_fallback(temp_global):
    _route.reset_stats()
    needle = "permfbneedle"
    # Inject a negative cache entry for this pattern's key (as if synthesis had
    # failed, R65) so the router's residency consult forces fallback.
    key = make_key(hdl.identity.descriptor_key(needle, 0, hdl.GENERATOR_VERSION),
                   TOOLCHAIN_VERSION, SHELL_VERSION)
    temp_global._cache.put_failure(key, "injected failure")

    subject = _large(needle)
    fae0 = pre.stats()["fallback_after_error"]
    m = pre.search(needle, subject)
    # Byte-identical to stock and NOT counted as a device error (R52/AC-1-6).
    assert m.span() == re.search(needle, subject).span()
    s = pre.stats()
    assert s["fallback_after_error"] == fae0      # ordinary routing, not error
    assert s["fallback"] >= 1


def test_permanent_fallback_finditer(temp_global):
    _route.reset_stats()
    needle = r"permfb\d+"
    key = make_key(hdl.identity.descriptor_key(needle, 0, hdl.GENERATOR_VERSION),
                   TOOLCHAIN_VERSION, SHELL_VERSION)
    temp_global._cache.put_failure(key, "injected")
    subject = _large("perm") + " permfb12 permfb345"
    got = [m.span() for m in pre.finditer(needle, subject)]
    ref = [m.span() for m in re.finditer(needle, subject)]
    assert got == ref
    assert pre.stats()["fallback_after_error"] == 0


# --- model-as-resident stand-in preserved (R7): large call still uses model -
def test_large_eligible_call_still_uses_model(temp_global):
    _route.reset_stats()
    subj = "".join("abc 123\n"[i % 8] for i in range(_route.S_MIN + 500))
    got = [m.span(0) for m in pre.finditer(r"\d+", subj)]
    ref = [m.span(0) for m in re.finditer(r"\d+", subj)]
    assert got == ref
    assert pre.stats()["model"] == 1              # model still serves (R7)


# --- pyro.prewarm public API (R62) -----------------------------------------
def test_prewarm_single_pattern(temp_global):
    pyro.prewarm("prewarm_me[0-9]+")
    temp_global.drain(10.0)
    assert temp_global.tier("prewarm_me[0-9]+", 0, hdl.ENC_UTF8) == "warm"


def test_prewarm_iterable_mixed(temp_global):
    # Iterable with an eligible and a fallback-only pattern; never raises (R62).
    pyro.prewarm([r"good[0-9]+", r"(a)\1"])   # 2nd is a backreference -> ignored
    temp_global.drain(10.0)
    assert temp_global.stats()["synth_launched"] == 1


def test_prewarm_never_raises_on_fallback_only(temp_global):
    pyro.prewarm(r"(?P=missing)")     # invalid/backref-style -> silently ignored
    assert temp_global.stats()["synth_launched"] == 0


def test_prewarm_is_not_on_patched_re(temp_global):
    # R62/§7.2: prewarm must NOT appear on the standard re namespace when installed.
    pyro.install()
    try:
        assert not hasattr(re, "prewarm")
    finally:
        pyro.uninstall()


# --- explain() surfaces circuit_status from the live tier (R31) ------------
def test_explain_circuit_status_tracks_tier(temp_global):
    info = pre.explain(r"expl[0-9]+")
    assert info["eligible"] is True
    assert info["circuit_status"] == "cold"
    pyro.prewarm(r"expl[0-9]+")
    temp_global.drain(10.0)
    assert pre.explain(r"expl[0-9]+")["circuit_status"] == "warm"


def test_explain_fallback_only_status(temp_global):
    info = pre.explain(r"(a)\1")   # backreference -> fallback-only
    assert info["eligible"] is False
    assert info["circuit_status"] == "fallback_only"
