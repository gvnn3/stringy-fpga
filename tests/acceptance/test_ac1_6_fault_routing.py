"""AC-1-6: fault injection & routing-vs-error accounting.
(R19, R44, R52, R61, R14a, R51a, R66)

Key spec invariant: NOT_RESIDENT / SYNTH tiers and the R51a safety gates are
ordinary fallback ROUTING and MUST NOT be counted as `fallback_after_error`
(R44/R14a); only a genuine device-error retry (R52) increments it.  On a
device-free host every eligible dispatch is served by the model or by fallback
(R51b) with no device error, so `fallback_after_error` and `device_errors` MUST
stay 0 across gate/routing-heavy workloads.

Scope note (§13): the model exposes no public seam to inject a PYRO_E_DEVICE /
PYRO_E_TIMEOUT device error from the pyro/pyro.re surface, so the R52
device-error → fallback-retry counter increment is not driveable from public
info; that sub-item is recorded as a public-surface limitation.  The
false-positive re-verification guarantee (R19/R19a) IS observable: over-
approximation-class patterns still return byte-identical results.
"""
import os

import pytest

import pyro
import pyro.re as pre
import oracle


def _reset(model=False):
    os.environ.pop("PYRO_DISABLE", None)
    os.environ.pop("PYRO_FORCE_MODEL", None)
    if model:
        os.environ["PYRO_FORCE_MODEL"] = "1"
    pyro.refresh_env()


def test_stats_has_all_lifecycle_counters():
    """R66: stats() reports the dispatch + circuit-lifecycle counters, all ints."""
    s = pre.stats()
    required = {
        "hardware", "model", "fallback", "fallback_after_error", "device_errors",
        "synth_launched", "synth_succeeded", "synth_failed",
        "circuits_synthesizing", "circuits_resident", "circuits_evicted",
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
    pre.search("abc", "x\ud800 abc")                        # surrogate gate (R14a)
    pat = pre.compile(r"\d+")
    pat.search("a 12 b 34", 2, 7)                           # pos/endpos gate

    s1 = pre.stats()
    assert s1["fallback_after_error"] == fae0, "routing must not bump fallback_after_error"
    assert s1["device_errors"] == de0, "no device error may occur on device-free host"


def test_over_approximation_classes_reverified_byte_identical():
    """R19/R19a: over-approximation-class constructs (full Unicode categories via
    \\w on non-ASCII, cross-length case fold ß, \\b at UTF-8 boundaries) return
    byte-identical results — false positives are re-verified away (R19)."""
    _reset(model=True)
    cases = [
        (r"\w+", 0, "café_naïve Ω12 δوд"),          # unicode word categories
        ("ß", pre.IGNORECASE, "straße STRASSE ss ß"),  # cross-length casefold
        (r"\bword\b", 0, "wörd word wордy word"),      # \b at multibyte boundaries
        (r"\w+", pre.ASCII, "café Ω ascii_only"),      # ASCII-restricted \w
    ]
    for pattern, flags, subject in cases:
        oracle.assert_equivalent(pre, pattern, subject, flags, label="overapprox")


def test_false_positive_never_leaks_via_differential():
    """R19: the completeness+re-verify contract means a returned result is never
    a false positive — verified by exhaustive differential over the supported
    corpus on the model path (a superset-recognizer would leak here)."""
    _reset(model=True)
    for label, pattern, flags, subject in oracle.SUPPORTED:
        oracle.assert_equivalent(pre, pattern, subject, flags, label=f"noleak/{label}")


def test_device_error_injection_not_publicly_driveable():
    """R52/R61 (§13): no public seam exists on the pyro/pyro.re surface to inject
    a PYRO_E_DEVICE/PYRO_E_TIMEOUT into the model, so the device-error →
    fallback-retry counter path is not driveable from public info.  Recorded as a
    public-surface limitation; the routing-vs-error accounting is covered above."""
    pytest.skip(
        "device-error injection into the model is not exposed on the public "
        "pyro/pyro.re surface (no injection env/API in the spec); R52 counter "
        "increment not driveable from public info — see report §13."
    )
