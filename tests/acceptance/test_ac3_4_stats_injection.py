"""AC-3-4: `pyro.re.stats()` dispatch + lifecycle counters under fault and
synthesis-failure injection.  (R52, R61, R65, R66, R67)

Four claims, each proven POSITIVELY (a broken counter pipeline fails here):

  1. Shape/behavior: stats() reports exactly the R66 counter set; the eleven
     monotonic counters never decrease across an entire lifecycle walk; the
     two gauges (circuits_synthesizing / circuits_resident) genuinely rise
     AND fall; observing stats() perturbs nothing (R66).
  2. Dispatch attribution: a loss-regime call moves `fallback` (+1) and
     NOTHING else; a model-regime call moves `model` (+1) and nothing else;
     `hardware` is reported as an honest 0 on this device-free host even
     while `model` climbs (it is a distinct counter, never an alias).
  3. Injection: `inject_synth_failure` -> synth_launched+1 AND synth_failed+1
     on the exact launch-policy call, then the R65 negative cache pins the
     pattern to plain `fallback` forever (including across a process restart
     on the same cache), with results byte-identical throughout and
     `fallback_after_error` untouched (failure is ROUTING, not a device
     error); `inject_device_error` -> fallback_after_error+1 and
     device_errors+1 per injected fault with CPython-identical results (R52).
  4. Thread aggregation: N threads x M loss-regime calls fold to EXACTLY
     fallback += N*M after the threads retire — on the native build this
     exercises the per-thread TLS counter blocks and the exited-thread drain
     (g_dead), on the pure-Python build the lock+dict counters.  The suite is
     run natively and under PYRO_NO_NATIVE=1; this file must pass in both.

Synthesis NEVER runs in this pytest process (R63e): everything that can
launch it runs in ac3_4_workers.py subprocesses on isolated PYRO_CACHE_DIRs,
via phase3_support._worker_env (which also strips any leaked PYRO_TOOLCHAIN
pin, so workers always ride the MOCK toolchain — correct and honest for
AC-3-4, which tests the stats machinery, not Vivado).  In-process tests are
limited to paths that provably cannot launch: loss-regime routing never
consults residency, and FORCE_MODEL dispatches stay far below the default
N_synth=1000 (the isolation fixture clears PYRO_N_SYNTH).

Env-knob discipline: this file touches only PYRO_ENABLE_TEST_HOOKS /
PYRO_FORCE_MODEL / PYRO_N_SYNTH, all already registered in
conftest._ENV_KEYS and popped by the worker-env helpers; no new knobs.
Cross-process result comparisons pass both sides through
phase1_support.jsonify (JSON has no tuple).
"""
import json
import os
import subprocess
import sys
import threading

import pytest

import pyro
import pyro.re as pre
import oracle
import phase3_support
from phase1_support import jsonify

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ac3_4_workers.py")

# --- R66 counter taxonomy (the spec-exact key set) -------------------------
DISPATCH_COUNTERS = frozenset({
    "hardware", "model", "fallback", "fallback_after_error",
    "device_errors", "total",
})
LIFECYCLE_COUNTERS = frozenset({
    "synth_launched", "synth_succeeded", "synth_failed",
    "circuits_synthesizing", "circuits_resident", "circuits_evicted",
    "pr_loads",
    # Added by the S2 group work (00143e8): a misconfigured job (e.g. a
    # group engine paired with a backpressure-deaf wrapper) fails loud
    # BEFORE tool time and is counted here, never cached.  R66 is a
    # minimum-set obligation ("in addition to..."), so an added counter is
    # spec-legal; this pin tracks the implementation's full surface.
    "synth_misconfigured",
})
ALL_COUNTERS = DISPATCH_COUNTERS | LIFECYCLE_COUNTERS
GAUGES = frozenset({"circuits_synthesizing", "circuits_resident"})  # R66
MONOTONIC = ALL_COUNTERS - GAUGES


def _delta(before, after):
    return {k: after[k] - before[k] for k in after}


def _assert_only_moved(before, after, expected, label):
    """The full-vector attribution check: EVERY key's delta is asserted, so a
    counter that moves unexpectedly (e.g. hardware bumped by a model dispatch)
    fails loudly instead of hiding behind a keys-of-interest filter."""
    d = _delta(before, after)
    want = {k: expected.get(k, 0) for k in after}
    assert d == want, (
        f"[{label}] counter attribution wrong:\n  deltas   ={d}\n  expected={want}")


def _run_worker(command, *args, cache_dir, extra_env=None, timeout=240):
    """Run one ac3_4_workers.py command out of process (R63e discipline),
    reusing phase3_support._worker_env: isolated PYRO_CACHE_DIR, clean knob
    baseline, PYRO_TOOLCHAIN/PYRO_VIVADO stripped (mock toolchain mandatory),
    PYRO_NO_NATIVE inherited pass-through (both-builds coverage)."""
    os.makedirs(str(cache_dir), exist_ok=True)
    env = phase3_support._worker_env(cache_dir, extra_env)
    proc = subprocess.run(
        [sys.executable, WORKER, command, *[str(a) for a in args]],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=timeout,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"ac3_4 worker {command} failed rc={proc.returncode}\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"ac3_4 worker {command} did not emit JSON:\n{proc.stdout}\n{proc.stderr}")


# Worker runs are pure JSON data; module-scoped so several tests share one
# subprocess run without re-synthesizing.  No pyro state crosses tests.
@pytest.fixture(scope="module")
def lifecycle_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("ac34_lifecycle")
    return _run_worker("stats_lifecycle", cache_dir=root / "cache")


@pytest.fixture(scope="module")
def synth_failure_runs(tmp_path_factory):
    """Two workers sharing ONE cache dir: the injection run, then the fresh
    restart run that proves R65 permanence from the on-disk negative cache."""
    cache = tmp_path_factory.mktemp("ac34_synthfail") / "cache"
    first = _run_worker(
        "synth_failure_stats", "acc34failpat", cache_dir=cache,
        extra_env={"PYRO_ENABLE_TEST_HOOKS": "1", "PYRO_FORCE_MODEL": "1",
                   "PYRO_N_SYNTH": "2"})
    second = _run_worker(
        "synth_failure_persists", "acc34failpat", cache_dir=cache,
        extra_env={"PYRO_FORCE_MODEL": "1", "PYRO_N_SYNTH": "2"})
    return first, second


def _snap(run, label):
    matches = [s["stats"] for s in run["snaps"] if s["label"] == label]
    assert matches, f"lifecycle worker emitted no {label!r} snapshot"
    return matches[0]


# ==========================================================================
# 1. Shape and behavior of the counter surface (R66).
# ==========================================================================
class TestStatsShape:
    def test_stats_reports_exactly_the_r66_counter_set(self):
        """R66: the dispatch counters (hardware/model/fallback/
        fallback_after_error + device_errors/total, R52) AND the seven
        lifecycle counters — exactly, no more, no fewer, all true ints >= 0."""
        s = pre.stats()
        assert set(s.keys()) == ALL_COUNTERS, (
            f"missing={ALL_COUNTERS - set(s)}, unexpected={set(s) - ALL_COUNTERS}")
        for k, v in s.items():
            assert type(v) is int, f"stats[{k!r}]={v!r} is {type(v).__name__}, not int"
            assert v >= 0, f"stats[{k!r}]={v} is negative"

    def test_stats_observation_is_non_perturbing(self):
        """R66: observing stats MUST NOT perturb routing or the counters —
        repeated stats() calls return identical snapshots (no self-counting)."""
        pre.search("acc34observe", "z acc34observe z")  # make counters non-trivial
        s0 = pre.stats()
        for _ in range(25):
            assert pre.stats() == s0, "stats() observation moved a counter"

    def test_monotonic_counters_never_decrease_across_lifecycle(self, lifecycle_run):
        """R66 monotonicity over the FULL walk (loss -> model -> synth ->
        resident -> evict -> shutdown), including every in-flight poll
        snapshot: the 11 non-gauge counters never tick down — not even across
        the manager shutdown that empties the gauges."""
        snaps = lifecycle_run["snaps"]
        assert len(snaps) >= 8, f"too few snapshots: {[s['label'] for s in snaps]}"
        for prev, cur in zip(snaps, snaps[1:]):
            for k in MONOTONIC:
                assert cur["stats"][k] >= prev["stats"][k], (
                    f"monotonic counter {k} decreased "
                    f"{prev['stats'][k]} -> {cur['stats'][k]} between "
                    f"{prev['label']!r} and {cur['label']!r}")

    def test_gauge_circuits_synthesizing_rises_and_falls(self, lifecycle_run):
        """R66 gauge semantics: circuits_synthesizing is >=1 while the mock
        synthesis is in flight (the snapshot taken immediately after the
        non-blocking prewarm return, or a poll snapshot) and back to 0 once
        the job completes — a counter pinned at 0 or stuck high fails."""
        assert lifecycle_run["reached"]["a"] is True, "synthesis A never completed"
        peak = max(s["stats"]["circuits_synthesizing"] for s in lifecycle_run["snaps"])
        assert peak >= 1, (
            "circuits_synthesizing never rose above 0 despite a launched "
            "mock synthesis — gauge not wired to the in-flight set")
        assert _snap(lifecycle_run, "synth_a_done")["circuits_synthesizing"] == 0
        assert _snap(lifecycle_run, "final_shutdown")["circuits_synthesizing"] == 0

    def test_gauge_circuits_resident_rises_and_falls(self, lifecycle_run):
        """R66 gauge semantics for the PR region: 0 at baseline, 1 after the
        warm artifact is promoted (mock PR load), still 1 after the
        single-tenant eviction swap, and back to 0 after the manager's own
        shutdown() empties the region (the one existing operation that lowers
        it below 1 with pr_partitions=1 — see ac3_4_workers docstring)."""
        assert _snap(lifecycle_run, "baseline")["circuits_resident"] == 0
        assert _snap(lifecycle_run, "after_resident_a")["circuits_resident"] == 1, (
            "warm artifact was not promoted to resident on dispatch (R64)")
        assert _snap(lifecycle_run, "after_evict")["circuits_resident"] == 1
        assert _snap(lifecycle_run, "final_shutdown")["circuits_resident"] == 0, (
            "circuits_resident did not fall when the PR region emptied — "
            "gauge not derived from live residency state")

    def test_stats_key_set_identical_in_process_and_in_worker(self, lifecycle_run):
        """The key set is one surface, not two: every worker snapshot carries
        exactly the same R66 keys the in-process build reports (native and
        pure-Python builds both, since the whole suite runs twice)."""
        assert set(lifecycle_run["stats_keys"]) == ALL_COUNTERS
        for s in lifecycle_run["snaps"]:
            assert set(s["stats"].keys()) == ALL_COUNTERS, s["label"]


# ==========================================================================
# 2. Dispatch attribution (R51/R52/R66).
# ==========================================================================
class TestDispatchAttribution:
    def test_loss_regime_call_moves_fallback_and_nothing_else(self):
        """R51 step 4 / R66: one tiny-subject, zero-reuse call -> fallback+1,
        total+1, and EVERY other counter (incl. all lifecycle counters — the
        loss-regime fast path must never consult residency) provably flat.
        In-process is safe: the fallback path cannot launch synthesis."""
        s0 = pre.stats()
        m = pre.search("acc34lossproc", "z acc34lossproc z")
        assert m is not None and m.span() == (2, 15)
        _assert_only_moved(s0, pre.stats(), {"fallback": 1, "total": 1},
                           "loss-regime")

    def test_model_regime_call_moves_model_and_nothing_else(self, lifecycle_run):
        """R51.6/R66 (worker, mock toolchain): the FORCE_MODEL dispatch moves
        model+1, total+1 and nothing else — in particular fallback stays flat
        and hardware stays 0 (a model dispatch must never be double-counted
        or misfiled as hardware)."""
        before = _snap(lifecycle_run, "after_loss")
        after = _snap(lifecycle_run, "after_model")
        _assert_only_moved(before, after, {"model": 1, "total": 1},
                           "model-regime")

    def test_worker_baseline_and_loss_attribution(self, lifecycle_run):
        """Same attribution proven in the worker process: a fresh process
        starts all-zero (nothing pre-counted), and its loss-regime call moves
        exactly fallback+1/total+1."""
        base = _snap(lifecycle_run, "baseline")
        assert all(v == 0 for v in base.values()), f"non-zero baseline: {base}"
        _assert_only_moved(base, _snap(lifecycle_run, "after_loss"),
                           {"fallback": 1, "total": 1}, "worker-loss")

    def test_hardware_counter_is_an_honest_zero(self, lifecycle_run):
        """AC-3-4: hardware is REPORTED as 0 on this device-free host in every
        snapshot — while model climbs past 0 in the same snapshots — proving
        it is a genuinely distinct counter that model dispatches never touch,
        not an absent key and not an alias of model."""
        final = lifecycle_run["snaps"][-1]["stats"]
        assert final["model"] >= 3, f"model dispatches did not register: {final}"
        for s in lifecycle_run["snaps"]:
            assert s["stats"]["hardware"] == 0, (
                f"hardware moved to {s['stats']['hardware']} at {s['label']!r} "
                "with no device present")

    def test_promotion_eviction_and_pr_loads_attribution(self, lifecycle_run):
        """R64/R66: promoting warm A -> resident costs exactly pr_loads+1 (and
        model+1/total+1 for the serving dispatch); dispatching warm B then
        evicts A: circuits_evicted+1, pr_loads+1, resident count unchanged at
        1 (single tenant)."""
        _assert_only_moved(
            _snap(lifecycle_run, "synth_a_done"),
            _snap(lifecycle_run, "after_resident_a"),
            {"model": 1, "total": 1, "pr_loads": 1, "circuits_resident": 1},
            "promote-A")
        d = _delta(_snap(lifecycle_run, "synth_b_done"),
                   _snap(lifecycle_run, "after_evict"))
        assert d["circuits_evicted"] == 1, f"eviction not counted: {d}"
        assert d["pr_loads"] == 1 and d["model"] == 1 and d["total"] == 1, d
        assert d["circuits_resident"] == 0, "single-tenant swap changed the gauge"

    def test_lifecycle_results_identical_to_stock_throughout(self, lifecycle_run):
        """R53/R36: every dispatch the lifecycle worker made — loss, model,
        resident-tier A, post-eviction B — returned the stock-re result
        byte-identically, whatever the counters were doing (both sides
        jsonify'd; both crossed the same JSON boundary)."""
        assert lifecycle_run["results"], "worker recorded no dispatch results"
        for rec in lifecycle_run["results"]:
            assert rec["pyro"] is not None, f"{rec['label']}: pyro returned None"
            assert jsonify(rec["pyro"]) == jsonify(rec["stock"]), (
                f"{rec['label']}: result diverged from stock re\n"
                f"  pyro ={rec['pyro']}\n  stock={rec['stock']}")


# ==========================================================================
# 3. Synthesis-failure injection (R65/R67) — the real launch path, per call.
# ==========================================================================
class TestSynthFailureInjection:
    def test_launch_call_increments_synth_launched_and_synth_failed(self, synth_failure_runs):
        """R65/R66/R67: with N_synth=2, call-by-call full-vector attribution —
        call 1: model+1 only (below threshold, nothing launched yet);
        call 2: model+1 AND synth_launched+1 AND synth_failed+1 (the armed
        launch fails deterministically; synth_succeeded stays 0 and the
        synthesizing gauge never rises: no job, no worker process)."""
        first, _ = synth_failure_runs
        base = first["baseline"]
        assert all(v == 0 for v in base.values()), f"non-zero baseline: {base}"
        c = first["calls"]
        _assert_only_moved(base, c[0]["stats"], {"model": 1, "total": 1},
                           "call-1 (below N_synth)")
        _assert_only_moved(c[0]["stats"], c[1]["stats"],
                           {"model": 1, "total": 1,
                            "synth_launched": 1, "synth_failed": 1},
                           "call-2 (injected launch failure)")
        assert c[1]["stats"]["synth_succeeded"] == 0
        assert c[1]["stats"]["circuits_synthesizing"] == 0, (
            "injected failure must not leave a phantom in-flight job")

    def test_pattern_becomes_permanently_fallback_only(self, synth_failure_runs):
        """R65 negative cache: every call after the failed launch is a plain
        `fallback` (+1 each, nothing else — model flat, synth_launched flat:
        no retry storm), fallback_after_error stays 0 (failure is ROUTING,
        not a device error, R52 distinction), and explain() reports
        fallback_only."""
        first, _ = synth_failure_runs
        c = first["calls"]
        for prev, cur in zip(c[1:], c[2:]):
            _assert_only_moved(prev["stats"], cur["stats"],
                               {"fallback": 1, "total": 1},
                               f"post-failure call {cur['i']}")
        final = c[-1]["stats"]
        assert final["fallback_after_error"] == 0 and final["device_errors"] == 0
        assert final["synth_launched"] == 1, "R65: synthesis was retried"
        assert first["circuit_status"] == "fallback_only"

    def test_results_byte_identical_throughout_failure_injection(self, synth_failure_runs):
        """AC-3-4 'without altering results': all 5 dispatches spanning the
        model -> injected-failure -> permanent-fallback transition returned
        the stock-re result byte-identically."""
        first, _ = synth_failure_runs
        for rec in first["calls"]:
            assert rec["pyro"] is not None, f"call {rec['i']} returned None"
            assert jsonify(rec["pyro"]) == jsonify(rec["stock"]), (
                f"call {rec['i']} diverged:\n  pyro ={rec['pyro']}\n"
                f"  stock={rec['stock']}")

    def test_negative_cache_survives_process_restart(self, synth_failure_runs):
        """R65 'permanently': a FRESH process on the same PYRO_CACHE_DIR (with
        FORCE_MODEL + N_SYNTH=2 still set, so a broken negative cache WOULD
        relaunch) routes every call to plain fallback with synth_launched
        still 0, identical results, and explain() = fallback_only from disk."""
        _, second = synth_failure_runs
        base = second["baseline"]
        assert all(v == 0 for v in base.values()), f"non-zero baseline: {base}"
        prev = base
        for rec in second["calls"]:
            _assert_only_moved(prev, rec["stats"], {"fallback": 1, "total": 1},
                               f"restart call {rec['i']}")
            assert jsonify(rec["pyro"]) == jsonify(rec["stock"]), rec
            prev = rec["stats"]
        assert prev["synth_launched"] == 0, (
            "fresh process relaunched synthesis for an R65-failed pattern")
        assert second["circuit_status"] == "fallback_only"


# ==========================================================================
# 4. Device-error / false-positive fault injection (R52/R61/R67), in-process
#    (established AC-1-6 idiom: FORCE_MODEL dispatches cannot launch — the
#    isolation fixture cleared PYRO_N_SYNTH, so counts sit far below 1000).
# ==========================================================================
def _enable_hooks_and_model():
    os.environ["PYRO_ENABLE_TEST_HOOKS"] = "1"
    os.environ["PYRO_FORCE_MODEL"] = "1"
    pyro.refresh_env()  # R35a sampling point


class TestFaultInjection:
    @pytest.mark.parametrize("kind", ["device", "timeout"])
    def test_device_error_full_counter_vector_and_results(self, kind):
        """R52/R61/R66: each of n injected device/timeout errors moves
        fallback_after_error+1 AND device_errors+1 AND total+1 — and nothing
        else: model is NOT charged for the failed attempt, plain fallback is
        NOT charged for the retry, hardware stays 0, and every lifecycle
        counter is flat.  Results are CPython-identical throughout."""
        _enable_hooks_and_model()
        n = 3
        pat = f"acc34dev{kind}"
        subj = f"zz {pat} zz {pat}"
        expected = oracle.canon_match(oracle.stock("search")(pat, subj))
        try:
            s0 = pre.stats()
            pyro.testing.inject_device_error(kind, n)
            for i in range(n):
                got = oracle.canon_match(pre.search(pat, subj))
                assert got == expected, f"injected {kind} error altered result #{i}"
            _assert_only_moved(
                s0, pre.stats(),
                {"fallback_after_error": n, "device_errors": n, "total": n},
                f"device-error[{kind}]")
        finally:
            pyro.testing.reset()

    def test_device_error_counters_settle_after_reset(self):
        """R67: after reset() the very next dispatch is an ordinary model
        dispatch again — the error counters stop moving (the injection did
        not poison the counter pipeline)."""
        _enable_hooks_and_model()
        try:
            pyro.testing.inject_device_error("device", 1)
            pre.search("acc34settle", "z acc34settle z")  # consume the fault
            pyro.testing.reset()
            s0 = pre.stats()
            m = pre.search("acc34settle", "z acc34settle z")
            assert m is not None
            _assert_only_moved(s0, pre.stats(), {"model": 1, "total": 1},
                               "post-reset dispatch")
        finally:
            pyro.testing.reset()

    def test_false_positive_injection_never_alters_results(self):
        """R19/R61/R67: with spurious candidate windows armed, all eight APIs
        stay byte-identical to stock re (the no-leak invariant), hardware
        stays 0, and no monotonic counter ticks DOWN.  Which error counter
        the re-verification path charges is deliberately NOT pinned here:
        R52 groups verify-failure with device errors but the spec does not
        pin a counter per API (see the §13 note in test_ac1_6) — pinning one
        would test the implementation du jour, not the spec."""
        _enable_hooks_and_model()
        try:
            s0 = pre.stats()
            cases = [
                ("acc34fp", 0, "xx acc34fp yy acc34fp"),
                (r"\d+", 0, "a1 b22 c333"),
                (r"(a)(b)", 0, "zz ab ab"),
            ]
            for pattern, flags, subject in cases:
                pyro.testing.inject_false_positive(pattern, flags, 6)
                oracle.assert_equivalent(pre, pattern, subject, flags,
                                         label="ac34-fp")
            s1 = pre.stats()
            assert s1["hardware"] == 0
            for k in MONOTONIC:
                assert s1[k] >= s0[k], f"monotonic counter {k} decreased"
            # The two error counters only ever move in lockstep (R52 path).
            assert (s1["fallback_after_error"] - s0["fallback_after_error"]
                    == s1["device_errors"] - s0["device_errors"])
        finally:
            pyro.testing.reset()


# ==========================================================================
# 5. Thread aggregation (R32/R66) — native per-thread TLS blocks and the
#    retired-thread fold, and the pure-Python lock+dict counters.
# ==========================================================================
class TestThreadAggregation:
    def test_n_threads_times_m_calls_fold_to_exact_fallback_count(self):
        """R32/R66: 8 threads x 25 loss-regime calls each (per-thread unique
        pattern, reuse 25 < N_reuse=32, tiny subjects — every call is loss
        regime by construction) then ALL threads retire before stats() is
        read: fallback and total must equal EXACTLY N*M more than baseline.
        On the native build the 8 dead threads' TLS counter blocks must be
        drained into the fold (a lost or double-drained block misses/overshoots
        by a multiple of 25); every other dispatch counter is exactly flat."""
        n_threads, m_calls = 8, 25
        s0 = pre.stats()
        errors = []

        def work(tid):
            try:
                pat = f"acc34thr{tid}"
                subj = f"z acc34thr{tid} z"
                for _ in range(m_calls):
                    if pre.search(pat, subj) is None:
                        raise AssertionError(f"thread {tid}: no match")
            except Exception as e:  # surface into the main thread
                errors.append(e)

        threads = [threading.Thread(target=work, args=(i,))
                   for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        _assert_only_moved(
            s0, pre.stats(),
            {"fallback": n_threads * m_calls, "total": n_threads * m_calls},
            "thread-aggregation")

    def test_counts_from_threads_visible_before_and_after_retirement(self):
        """The fold is not exit-only bookkeeping: counts from a still-LIVE
        thread are already visible to stats() (cross-thread read of the live
        TLS block), and retiring the thread afterwards neither loses nor
        double-counts them."""
        s0 = pre.stats()
        gate_in, gate_out = threading.Event(), threading.Event()

        def work():
            for _ in range(10):
                pre.search("acc34live", "z acc34live z")
            gate_in.set()       # counts made, thread still alive
            gate_out.wait(30)   # parent reads stats before we exit

        t = threading.Thread(target=work)
        t.start()
        assert gate_in.wait(30), "worker thread stalled"
        live = pre.stats()
        assert live["fallback"] - s0["fallback"] == 10, (
            "live thread's counts invisible to stats() aggregation")
        gate_out.set()
        t.join()
        _assert_only_moved(s0, pre.stats(), {"fallback": 10, "total": 10},
                           "retired-thread fold")
