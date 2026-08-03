"""AC-2-5: resident-circuit throughput + loss-regime routing.

SKIP clause (device_usable, R71 — FALSE here): hardware throughput of a resident
circuit meeting R1 (>=1 GiB/s floor, 5 GiB/s target) requires a real device.

Always-asserted absolute clauses (model/fallback path):
  * routing thresholds R3–R5: short-input/one-shot (< S_min, reuse < N_reuse)
    HW-eligible calls route to fallback (R3/R29) with absolute added routing
    overhead <= 2 µs median per call (R3a/R5).

R3b relative loss-regime bound (1.15×): measured per the v2.5.0 normative
protocol (R3b.1–R3b.4, shared implementation in r3b_protocol) — verdict is the
median of >= 5 trials, each trial the median-over->=1000-paired-calls ratio at
the pinned 132-byte ASCII subject (R3b.2), caches primed both sides, warmup kept
in the loss regime by rotating throwaway patterns (R3b.4).  Per R3c the bound
binds only when the §8 R51 routing decision is served by the compiled extension
(R3b.3); the precondition is evaluated truthfully at runtime — when it holds the
1.15× median bound is asserted, and on a pure-Python build the check records a
SKIP whose reason names the build AND states the measured median (never a silent
pass, never a FAIL; R3b is additionally a hard PASS at AC-3-3).
(R1, R2, R3, R3a, R3b, R3c, R5, R59, R71)
"""
import re as stdre
import statistics
import time

import pytest

import pyro.re as pre
import phase2_support
import r3b_protocol

# Short subject (< S_min = 64 KiB); each distinct pattern is called so per-
# pattern
# reuse stays < N_reuse = 32 => every call is a genuine loss-regime routing
# decision through §8 that ends in fallback.
SUBJECT = "the quick brown fox jumps over the lazy dog " * 3
N_PATTERNS = 1500
ABS_BUDGET_NS = 2000        # R3a/R5: <= 2 µs median added routing overhead


def _distinct_patterns(n):
    return [f"ac25tok{i}zz" for i in range(n)]


def test_hardware_throughput_on_device_skips():
    """SKIP: resident-circuit throughput (R1) requires device_usable, FALSE
    (R71)."""
    ok, reason = phase2_support.device_usable()
    if not ok:
        pytest.skip(reason)
    raise AssertionError(
    "device_usable unexpectedly true — implement the >=1 GiB/s "
    "resident-circuit throughput assertion (R1)")


def test_short_oneshot_routes_to_fallback():
    """R3/R29: a short (< S_min) one-shot (reuse < N_reuse) HW-eligible call
    routes
    to fallback — observable as a genuine re.Match (R29)."""
    m = pre.search("ac25_uniq_needle", "find ac25_uniq_needle here")
    assert m is not None
    assert isinstance(m, stdre.Match), (
        "short one-shot HW-eligible call must route to fallback (R51 step 4, "
        "R29)")


@pytest.mark.perf
def test_absolute_routing_overhead_median():
    """R3a/R5: median added routing/decision overhead over stock re <= 2 µs."""
    pats = _distinct_patterns(N_PATTERNS)
    re_c = [stdre.compile(p) for p in pats]
    py_c = [pre.compile(p) for p in pats]
    for i in range(
    len(pats)):          # prime classification caches (reuse->1)
        re_c[i].search(SUBJECT)
        py_c[i].search(SUBJECT)
    # R3b.4-style rotating warmup (the old single-pattern warmup crossed
    # N_reuse at call 33 and warmed the model path, not the measured one).
    r3b_protocol.rotating_warmup("ac25a", subject=SUBJECT)

    diffs = []
    for i in range(len(pats)):
        t0 = time.perf_counter_ns()
        re_c[i].search(SUBJECT)
        t1 = time.perf_counter_ns()
        py_c[i].search(SUBJECT)
        t2 = time.perf_counter_ns()
        diffs.append((t2 - t1) - (t1 - t0))
    median = statistics.median(diffs)
    assert median <= ABS_BUDGET_NS, (
        f"median routing overhead {median} ns exceeds R3a/R5 budget "
        f"{ABS_BUDGET_NS} ns")


@pytest.mark.perf
def test_relative_loss_regime_bound_r3b():
    """R3b (v2.5.0 protocol, R3b.1–R3b.4): below-threshold wall-clock stays
    within 1.15× of stock re, decided by the median of >= 5 trials at the pinned
    132-byte subject (R3b.2), each trial >= 1000 paired calls on distinct
    loss-regime patterns with caches primed both sides (R3b.1) and R3b.4
    rotating warmup.

    The R3c precondition is evaluated truthfully (R3b.3): when the compiled
    extension actively serves the §8 R51 routing decision the bound is a hard
    assertion; on a pure-Python build the check SKIPs with a reason that names
    the build and states the measured median — SKIP and FAIL are both honest,
    a silent pass is a defect (R71 honesty discipline applied to R3b)."""
    native, build_desc = r3b_protocol.native_routing_active()
    # Measure FIRST, unconditionally: the R3c SKIP reason MUST cite the
    # R3b.1/R3b.2 statistic (median of >= 5 trials at 132 B), never a single
    # trial and never nothing (R3b.3).
    verdict, trial_ratios, detail = r3b_protocol.measure_r3b()
    summary = r3b_protocol.format_detail(verdict, trial_ratios, detail)

    if not native:
        pytest.skip(
            f"R3c precondition does not hold — {build_desc}. R3b binds only "
            f"against the compiled-extension build (R3b.3), so this clause "
            f"records an honest SKIP (never a FAIL and never a silent pass); "
            f"R3a (absolute <= 2 µs, asserted above) governs, and R3b is a "
            f"hard PASS requirement at AC-3-3. Measured anyway per "
            f"R3b.1/R3b.2: {summary}; bound {r3b_protocol.R3B_RATIO}×.")

    assert verdict <= r3b_protocol.R3B_RATIO, (
        f"R3b: {build_desc}, so the 1.15× relative bound binds (R3c) and the "
        f"measured R3b.1 verdict exceeds it: {summary}; bound "
        f"{r3b_protocol.R3B_RATIO}×. (Individual trials MAY exceed 1.15× "
        f"without violating R3b; only this median binds.)")
