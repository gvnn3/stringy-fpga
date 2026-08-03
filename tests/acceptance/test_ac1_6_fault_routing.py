"""AC-1-6: fault injection via the public seams (R67) & routing-vs-error
accounting.  (R19, R44, R52, R61, R67, R14a, R51a, R66)

Uses the v2.0.5 `pyro.testing` seams (R67), gated behind
PYRO_ENABLE_TEST_HOOKS=1
sampled at an R35a point (here: refresh_env in-process):
  * inject_device_error → R52 fallback-retry, CPython-identical result,
    fallback_after_error incremented;
  * inject_false_positive → R19 re-verification, spurious windows never leak;
  * inject_synth_failure is exercised in AC-1-5 (needs synthesis).

Key invariant: NOT_RESIDENT / SYNTH tiers and the R51a safety gates are ordinary
fallback ROUTING and MUST NOT be counted as `fallback_after_error` (R44/R14a);
only a genuine (injected) device-error retry (R52) increments it.
"""
import os

import pytest

import pyro
import pyro.re as pre
import oracle


def _enable_hooks(model=True):
    os.environ.pop("PYRO_DISABLE", None)
    os.environ["PYRO_ENABLE_TEST_HOOKS"] = "1"
    if model:
        os.environ["PYRO_FORCE_MODEL"] = "1"
    else:
        os.environ.pop("PYRO_FORCE_MODEL", None)
    pyro.refresh_env()


def _reset(model=False):
    os.environ.pop("PYRO_DISABLE", None)
    os.environ.pop("PYRO_FORCE_MODEL", None)
    if model:
        os.environ["PYRO_FORCE_MODEL"] = "1"
    pyro.refresh_env()


def test_stats_has_all_lifecycle_counters():
    """R66: stats() reports the dispatch + circuit-lifecycle counters, all
    ints."""
    s = pre.stats()
    required = {
    "hardware",
    "model",
    "fallback",
    "fallback_after_error",
    "device_errors",
    "synth_launched",
    "synth_succeeded",
    "synth_failed",
    "circuits_synthesizing",
    "circuits_resident",
    "circuits_evicted",
    "pr_loads",
     }
    assert required.issubset(s.keys()), required - set(s.keys())
    for k in required:
        assert isinstance(s[k], int) and s[k] >= 0, (k, s[k])


def test_routing_never_counts_as_fallback_after_error():
    """R44/R14a/R51a/R52: not-resident routing, loss-regime fallback, and the
    R51a gates (bytearray, surrogate, pos/endpos) are routing, NOT device
    errors — fallback_after_error and device_errors stay flat."""
    _reset(model=False)
    s0 = pre.stats()
    fae0, de0 = s0["fallback_after_error"], s0["device_errors"]

    # loss-regime / not-resident routing (short one-shot eligible)
    for i in range(200):
        pre.search(f"needle{i}", f"a needle{i} z")
    # R51a gates
    pre.search(rb"\w+", bytearray(b"hello world"))          # subject-type gate
    # surrogate gate (R14a)
    pre.search("abc", "x\ud800 abc")
    pat = pre.compile(r"\d+")
    pat.search("a 12 b 34", 2, 7)                           # pos/endpos gate

    s1 = pre.stats()
    assert s1["fallback_after_error"] == fae0, (
        "routing must not bump fallback_after_error")
    assert s1["device_errors"] == de0, (
        "no device error may occur on device-free host")


def test_over_approximation_classes_reverified_byte_identical():
    """R19/R19a: over-approximation-class constructs (full Unicode categories
    via
    \\w on non-ASCII, cross-length case fold ß, \\b at UTF-8 boundaries) return
    byte-identical results — false positives are re-verified away (R19)."""
    _reset(model=True)
    cases = [
        (r"\w+", 0, "café_naïve Ω12 δوд"),          # unicode word categories
        ("ß", pre.IGNORECASE, "straße STRASSE ss ß"),  # cross-length casefold
        # \b at multibyte boundaries
        (r"\bword\b", 0, "wörd word wордy word"),
        (r"\w+", pre.ASCII, "café Ω ascii_only"),      # ASCII-restricted \w
    ]
    for pattern, flags, subject in cases:
        oracle.assert_equivalent(
    pre, pattern, subject, flags, label="overapprox")


def test_false_positive_never_leaks_via_differential():
    """R19: the completeness+re-verify contract means a returned result is never
    a false positive — verified by exhaustive differential over the supported
    corpus on the model path (a superset-recognizer would leak here)."""
    _reset(model=True)
    for label, pattern, flags, subject in oracle.SUPPORTED:
        oracle.assert_equivalent(
    pre,
    pattern,
    subject,
    flags,
     label=f"noleak/{label}")


def test_testing_seam_namespace_present():
    """R67: the pyro.testing seam namespace exposes the required functions."""
    for fn in ("inject_device_error", "inject_synth_failure",
               "inject_false_positive", "reset"):
        assert callable(getattr(pyro.testing, fn, None)
                        ), f"missing pyro.testing.{fn}"


def test_hooks_are_inert_when_gate_off():
    """R67: with PYRO_ENABLE_TEST_HOOKS unset/sampled-off the seam functions are
    importable no-op that raise nothing and do NOT perturb results or
    routing."""
    os.environ.pop("PYRO_ENABLE_TEST_HOOKS", None)
    os.environ["PYRO_FORCE_MODEL"] = "1"
    pyro.refresh_env()
    import re as stdre
    fae0 = pre.stats()["fallback_after_error"]
    # These MUST NOT raise and MUST NOT arm anything.
    pyro.testing.inject_device_error("device", 5)
    pyro.testing.inject_false_positive("abc", 0, 5)
    pyro.testing.inject_synth_failure("abc")
    pyro.testing.reset()
    subj = "z abc z"
    assert pre.search("abc", subj).span() == stdre.search("abc", subj).span()
    assert pre.stats()["fallback_after_error"] == fae0, (
        "gate-off hook perturbed routing")


@pytest.mark.parametrize("kind", ["device", "timeout"])
def test_inject_device_error_fallback_retry_identical(kind):
    """R52/R61/R67: an injected device/timeout error routes the affected
    dispatches to fallback with CPython-identical results and increments
    fallback_after_error by exactly the injected count."""
    import re as stdre
    _enable_hooks(model=True)
    n = 3
    fae0 = pre.stats()["fallback_after_error"]
    de0 = pre.stats()["device_errors"]
    try:
        pyro.testing.inject_device_error(kind, n)
        subj = "zz needle_dev zz needle_dev"
        for _ in range(n):
            m = pre.search("needle_dev", subj)
            assert m.span() == stdre.search("needle_dev", subj).span()
        s = pre.stats()
        assert s["fallback_after_error"] - fae0 == n, (
            f"expected +{n} fallback_after_error, got "
            f"{s['fallback_after_error'] - fae0}")
        assert s["device_errors"] - de0 == n
    finally:
        pyro.testing.reset()


def test_inject_false_positive_never_leaks():
    """R19/R19a/R67: injected spurious candidate windows are re-verified before
    return — they MUST NOT leak into results.  The airtight, spec-guaranteed
    property (AC-1-6: "re-verified and never leak"; R19a: a false positive that
    survives re-verification into a returned result is a defect) is that results
    stay byte-identical to stock re across all eight APIs despite a large
    injected
    false-positive count.

    Note (§13): R52 groups "result fails re-verification" with device errors and
    does not pin WHICH stats counter moves, and the implementation is observed
    to
    route re-verification through the R52 path (bumping device_errors /
    fallback_after_error) for some APIs but not others; this test therefore
    asserts only the no-leak invariant, not a specific counter."""
    _enable_hooks(model=True)
    try:
        cases = [
            ("foo", 0, "xx foo yy foo zz"),
            (r"\d+", 0, "a1 b22 c333"),
            (r"(a)(b)", 0, "zz ab ab"),
            (r"[a-z]+", 0, "AB cd EF gh"),
        ]
        for pattern, flags, subject in cases:
            pyro.testing.inject_false_positive(pattern, flags, 8)
            oracle.assert_equivalent(pre, pattern, subject, flags, label="fp")
    finally:
        pyro.testing.reset()


def test_reset_restores_clean_state():
    """R67: reset() clears injected faults — a dispatch after reset behaves
    normally (no error retry)."""
    import re as stdre
    _enable_hooks(model=True)
    pyro.testing.inject_device_error("device", 10)
    pyro.testing.reset()
    fae0 = pre.stats()["fallback_after_error"]
    subj = "z clean_pat z"
    assert pre.search(
    "clean_pat",
    subj).span() == stdre.search(
        "clean_pat",
         subj).span()
    assert pre.stats()["fallback_after_error"] == fae0, (
        "reset() did not clear injected faults")
