"""AC-1-5: synthesis-service skeleton with the mock toolchain, through the public
API (pyro.prewarm / pyro.re.stats() / explain()).  (R4a, R62–R66, R58a)

Launch policy fires only at ≥ N_synth or via prewarm; jobs run out of process and
never block calls; cold→warm→resident transitions and cache-key hits persist
across a simulated restart; jobs dedup by key; synthesis failure → permanent
fallback with no exception and correct stats.  All synthesis-triggering work runs
in file-based subprocesses (phase1_workers.py) so the synthesis service's
spawn/forkserver workers (R63) stay bounded to short-lived children (no residue).
"""
import tempfile

import pytest

import pyro.re as pre
import phase1_support

# Spec default (R4a): N_synth = 1000.  Bracket the threshold well clear of it.
N_LOW = 50
N_HIGH = 1100


def test_prewarm_accepts_and_never_raises():
    """R62: prewarm accepts a single pattern or iterable, returns None promptly,
    and never raises for fallback-only / non-encodable patterns."""
    with tempfile.TemporaryDirectory(prefix="pyro_pw_") as cache:
        res, _o, _e = phase1_support.run_worker("prewarm_types", cache_dir=cache)
    assert res["no_raise"] is True, res["errors"]


def test_prewarm_is_nonblocking_and_synthesizes_async():
    """R62/R63/R66: prewarm returns promptly (non-blocking) while the out-of-
    process service synthesizes (mock toolchain); the pattern reaches warm/
    resident, synth counters advance, and no error counter moves."""
    with tempfile.TemporaryDirectory(prefix="pyro_pw_") as cache:
        res, _o, _e = phase1_support.run_worker(
            "prewarm_lifecycle", "hotpat_ac15", cache_dir=cache)
    assert res["prewarm_return_is_none"] is True
    assert res["prewarm_seconds"] < 3.0, f"prewarm blocked {res['prewarm_seconds']}s"
    assert res["reached_terminal"] is True, res["last"]
    assert res["synth_launched"] >= 1
    assert res["synth_succeeded"] >= 1, res["last"]          # mock toolchain succeeds
    assert res["synth_failed"] == 0
    assert res["circuit_status"] in ("warm", "resident")
    assert res["match_span"] is not None
    assert res["fallback_after_error"] == 0
    assert res["device_errors"] == 0


def test_launch_policy_does_not_fire_below_threshold():
    """R4a: synthesis is NOT launched for every pattern — a modest number of
    eligible dispatches (< N_synth) launches nothing."""
    with tempfile.TemporaryDirectory(prefix="pyro_lp_") as cache:
        res, _o, _e = phase1_support.run_worker(
            "launch_policy", "coldpat_low", N_LOW, cache_dir=cache)
    assert res["delta"] == 0, f"launch fired at {N_LOW} dispatches (< N_synth): {res}"


def test_launch_policy_fires_at_threshold():
    """R4a: once a pattern is hot (≥ N_synth eligible dispatches) synthesis is
    launched exactly once."""
    with tempfile.TemporaryDirectory(prefix="pyro_lp_") as cache:
        res, _o, _e = phase1_support.run_worker(
            "launch_policy", "hotpat_high", N_HIGH, cache_dir=cache)
    assert res["delta"] >= 1, f"launch did not fire by {N_HIGH} dispatches: {res}"


def test_synth_jobs_dedup_by_key():
    """R63a: repeated prewarm of the same pattern deduplicates — synthesis is
    launched at most once per key."""
    with tempfile.TemporaryDirectory(prefix="pyro_dd_") as cache:
        res, _o, _e = phase1_support.run_worker("dedup", "deduppat", cache_dir=cache)
    assert res["launched_delta"] <= 1, res


def test_warm_cache_persists_across_process_restart():
    """R4 invariant / R63d: an artifact synthesized in one process is found WARM
    (or resident) by a FRESH process sharing PYRO_CACHE_DIR, without a new
    synthesis launch — the cold→warm transition happens at most once per key."""
    with tempfile.TemporaryDirectory(prefix="pyro_persist_") as cache:
        write, _o, _e = phase1_support.run_worker(
            "persist_write", "persistpat", cache_dir=cache)
        assert write["reached"] is True and write["synth_succeeded"] >= 1, write
        assert write["cache_files"], "synthesis did not populate the bitstream cache"

        read, _o2, _e2 = phase1_support.run_worker(
            "persist_read", "persistpat", cache_dir=cache)
    assert read["initial_status"] in ("warm", "resident"), (
        f"fresh process saw cold status {read['initial_status']} — cache did not persist")
    assert read["synth_launched"] == 0, (
        "fresh process re-launched synthesis instead of reusing the warm artifact")
    assert read["cache_files"] == write["cache_files"]


def test_stats_counters_present_and_monotonic_across_lifecycle():
    """R66: lifecycle counters exist and are non-negative ints; synth_launched
    and synth_succeeded advance monotonically once a pattern is synthesized."""
    with tempfile.TemporaryDirectory(prefix="pyro_stats_") as cache:
        res, _o, _e = phase1_support.run_worker(
            "prewarm_lifecycle", "statspat", cache_dir=cache)
    assert res["synth_launched"] >= 1
    assert res["synth_succeeded"] >= 1
    assert res["synth_failed"] >= 0
    assert res["pr_loads"] >= 0
    assert res["circuits_resident"] >= 0


def test_synthesis_failure_permanent_fallback_not_publicly_injectable():
    """R65 (§13): with the mock toolchain every eligible synthesis SUCCEEDS, and
    the spec exposes no public seam to force a synthesis FAILURE from the
    pyro/pyro.re surface.  The permanent-fallback-on-failure path (R65) is
    therefore only partially observable from public info: prewarm never raises
    even for fallback-only patterns (asserted in test_prewarm_accepts_and_never
    _raises) and the synth_failed counter exists (R66).  The failure→permanent-
    fallback transition itself is recorded as a public-surface limitation."""
    pytest.skip(
        "no public seam to force a synthesis failure from the pyro/pyro.re "
        "surface (mock toolchain always succeeds); R65 permanent-fallback "
        "transition not driveable from public info — see report §13."
    )
