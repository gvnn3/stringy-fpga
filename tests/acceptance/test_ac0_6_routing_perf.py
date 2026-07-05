"""AC-0-6: routing is deterministic and cheap in absolute terms — short-input/
one-shot calls route to fallback per the §8 decision order, and the added
routing/decision overhead is <= 2 µs median per call; the per-call path consults
only cached env flags (no per-call os.environ read).
(R3a, R5, R35a, R51, R59)

Phase 0 asserts the ABSOLUTE bound (R3a/R5), NOT the 1.15x relative ratio (R3b,
Phase 1+).  Overhead is measured with time.perf_counter_ns, median of many
iterations, warmup, and paired differencing against stock re to cancel per-call
match cost.
"""

import re as stdre
import statistics
import time

import pytest

import pyro.re as pre
import oracle

# distinct HW-eligible literal patterns; one measured call each keeps per-pattern
# reuse at 1 (< N_reuse) so every call is a genuine loss-regime routing decision
# through R51 steps 1..4 that ends in fallback.
N_PATTERNS = 1500
SUBJECT = "the quick brown fox jumps over the lazy dog " * 3  # short (< S_min)
BUDGET_NS = 2000  # R3a/R5: <= 2 µs median added routing/decision overhead


def _distinct_patterns(n):
    return [f"token{i}zz" for i in range(n)]


@pytest.mark.perf
def test_routing_overhead_absolute_median():
    """R3a/R5: median added routing/decision overhead of a pyro loss-regime call
    over stock re is <= 2 µs."""
    pats = _distinct_patterns(N_PATTERNS)
    re_c = [stdre.compile(p) for p in pats]
    py_c = [pre.compile(p) for p in pats]

    # Warmup: prime each measured pattern's classification cache (reuse -> 1,
    # still < N_reuse). Then warm the CPU on a throwaway pattern so the measured
    # patterns' reuse counters stay in the loss regime.
    for i in range(len(pats)):
        re_c[i].search(SUBJECT)
        py_c[i].search(SUBJECT)
    warm_re = stdre.compile("warmupzz")
    warm_py = pre.compile("warmupzz")
    for _ in range(3000):
        warm_re.search(SUBJECT)
        warm_py.search(SUBJECT)

    diffs = []
    for i in range(len(pats)):
        t0 = time.perf_counter_ns()
        re_c[i].search(SUBJECT)
        t1 = time.perf_counter_ns()
        py_c[i].search(SUBJECT)
        t2 = time.perf_counter_ns()
        diffs.append((t2 - t1) - (t1 - t0))

    median_overhead = statistics.median(diffs)
    assert median_overhead <= BUDGET_NS, (
        f"median routing overhead {median_overhead} ns exceeds R3a/R5 budget "
        f"{BUDGET_NS} ns (n={len(diffs)})"
    )


@pytest.mark.perf
def test_module_level_overhead_absolute_median():
    """R3a/R5: same absolute bound for the module-level function surface."""
    pats = _distinct_patterns(N_PATTERNS)
    # Prime compile caches on both sides so we measure routing, not compile (R4).
    for p in pats:
        stdre.search(p, SUBJECT)
        pre.search(p, SUBJECT)

    diffs = []
    for p in pats:
        t0 = time.perf_counter_ns()
        stdre.search(p, SUBJECT)
        t1 = time.perf_counter_ns()
        pre.search(p, SUBJECT)
        t2 = time.perf_counter_ns()
        diffs.append((t2 - t1) - (t1 - t0))

    median_overhead = statistics.median(diffs)
    assert median_overhead <= BUDGET_NS, (
        f"median module-level routing overhead {median_overhead} ns exceeds "
        f"R3a/R5 budget {BUDGET_NS} ns"
    )


def test_short_oneshot_routes_to_fallback():
    """R51 step 4 / R3a / R29: a short (< S_min) one-shot (reuse < N_reuse)
    HW-eligible call routes to the fallback path — observable as a genuine
    re.Match (R29).  Unique pattern keeps reuse at 1."""
    m = pre.search("uniq_oneshot_needle", "find uniq_oneshot_needle here")
    assert m is not None
    assert isinstance(m, stdre.Match), \
        "short one-shot HW-eligible call must route to fallback (R51 step 4, R29)"


def test_routing_is_deterministic():
    """R51/R53: identical inputs and configuration yield identical observable
    results across repeated calls (deterministic routing)."""
    pattern, subject = r"(\w+)@(\w+)", "user@host and a@b"
    first = oracle.canon_match(pre.search(pattern, subject))
    for _ in range(50):
        assert oracle.canon_match(pre.search(pattern, subject)) == first
    # And equals the stock-re oracle.
    assert first == oracle.canon_match(stdre.search(pattern, subject))
