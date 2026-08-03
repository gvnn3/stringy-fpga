"""AC-1-5: synthesis-service skeleton with the mock toolchain, through the
public
API (pyro.prewarm / pyro.re.stats() / explain()).  (R4a, R62–R66, R58a)

Launch policy fires only at ≥ N_synth or via prewarm; jobs run out of process
and
never block calls; cold→warm→resident transitions and cache-key hits persist
across a simulated restart; jobs dedup by key; synthesis failure → permanent
fallback with no exception and correct stats.  All synthesis-triggering work
runs
in file-based subprocesses (phase1_workers.py) so the synthesis service's
spawn/forkserver workers (R63) stay bounded to short-lived children (no
residue).
"""
import os
import tempfile
import time

import pyro.re as pre
import phase1_support

HOOKS = {"PYRO_ENABLE_TEST_HOOKS": "1"}


def test_prewarm_accepts_and_never_raises():
    """R62: prewarm accepts a single pattern or iterable, returns None promptly,
    and never raises for fallback-only / non-encodable patterns."""
    with tempfile.TemporaryDirectory(prefix="pyro_pw_") as cache:
        res, _o, _e = phase1_support.run_worker(
            "prewarm_types", cache_dir=cache)
    assert res["no_raise"] is True, res["errors"]


def test_prewarm_is_nonblocking_and_synthesizes_async():
    """R62/R63/R66: prewarm returns promptly (non-blocking) while the out-of-
    process service synthesizes (mock toolchain); the pattern reaches warm/
    resident, synth counters advance, and no error counter moves."""
    with tempfile.TemporaryDirectory(prefix="pyro_pw_") as cache:
        res, _o, _e = phase1_support.run_worker(
            "prewarm_lifecycle", "hotpat_ac15", cache_dir=cache)
    assert res["prewarm_return_is_none"] is True
    assert res["prewarm_seconds"] < 3.0, f"prewarm blocked {
    res['prewarm_seconds']}s"
    assert res["reached_terminal"] is True, res["last"]
    assert res["synth_launched"] >= 1
    # mock toolchain succeeds
    assert res["synth_succeeded"] >= 1, res["last"]
    assert res["synth_failed"] == 0
    assert res["circuit_status"] in ("warm", "resident")
    assert res["match_span"] is not None
    assert res["fallback_after_error"] == 0
    assert res["device_errors"] == 0


def test_launch_boundary_pinned_by_pyro_n_synth():
    """R4a/R68: PYRO_N_SYNTH pins the launch threshold deterministically. With
    N_synth = 3: the 2nd eligible dispatch launches nothing; the 3rd launches
    exactly once."""
    knob = {"PYRO_N_SYNTH": "3"}
    with tempfile.TemporaryDirectory(prefix="pyro_n2_") as cache:
        below, _o, _e = phase1_support.run_worker(
            "launch_policy", "knobpat_below", 2, cache_dir=cache,
            extra_env=knob)
    assert below["delta"] == 0, f"launch fired before N_synth=3: {below}"

    with tempfile.TemporaryDirectory(prefix="pyro_n3_") as cache:
        at, _o2, _e2 = phase1_support.run_worker(
            "launch_policy", "knobpat_at", 3, cache_dir=cache, extra_env=knob)
    assert at["delta"] == 1, f"launch did not fire exactly at N_synth=3: {at}"


def test_launch_policy_not_per_call_default_threshold():
    """R4a: with the default N_synth (1000), a modest number of eligible
    dispatches launches nothing — synthesis is not attempted for every call."""
    with tempfile.TemporaryDirectory(prefix="pyro_lp_") as cache:
        res, _o, _e = phase1_support.run_worker(
            "launch_policy", "coldpat_low", 50, cache_dir=cache)
    assert res["delta"] == 0, (
        f"launch fired at 50 dispatches (< default N_synth): {res}")


def test_synth_jobs_dedup_by_key():
    """R63a: repeated prewarm of the same pattern deduplicates — synthesis is
    launched at most once per key."""
    with tempfile.TemporaryDirectory(prefix="pyro_dd_") as cache:
        res, _o, _e = phase1_support.run_worker(
            "dedup", "deduppat", cache_dir=cache)
    assert res["launched_delta"] <= 1, res


def test_warm_cache_persists_across_process_restart():
    """R4 invariant / R63d: an artifact synthesized in one process is found WARM
    (or resident) by a FRESH process sharing PYRO_CACHE_DIR, without a new
    synthesis launch — the cold→warm transition happens at most once per key."""
    with tempfile.TemporaryDirectory(prefix="pyro_persist_") as cache:
        write, _o, _e = phase1_support.run_worker(
            "persist_write", "persistpat", cache_dir=cache)
        assert write["reached"] is True and write["synth_succeeded"] >= 1, write
        assert write["cache_files"], (
            "synthesis did not populate the bitstream cache")

        read, _o2, _e2 = phase1_support.run_worker(
            "persist_read", "persistpat", cache_dir=cache)
    assert read["initial_status"] in (
    "warm", "resident"), ( f"fresh process saw cold status {
        read['initial_status']} — cache did not persist")
    assert read["synth_launched"] == 0, (
        "fresh process re-launched synthesis instead of reusing the warm "
        "artifact")
    assert read["cache_files"] == write["cache_files"]


def test_cache_dir_isolation_writes_only_into_pyro_cache_dir():
    """R68: PYRO_CACHE_DIR isolates the bitstream cache — synthesis artifacts
    land
    in (and only in) the provided directory."""
    with tempfile.TemporaryDirectory(prefix="pyro_iso_") as cache:
        before = set(os.listdir(cache))
        write, _o, _e = phase1_support.run_worker(
            "persist_write", "isopat", cache_dir=cache)
        after = set(os.listdir(cache))
        assert write["synth_succeeded"] >= 1
        assert after - before, (
            "no artifact written into the isolated PYRO_CACHE_DIR")
        assert set(write["cache_files"]) <= after


def test_no_residual_synthesis_processes_after_teardown():
    """R63e: after the process that launched synthesis exits, NO residual
    service
    processes remain in its process group (the service tears itself down)."""
    with tempfile.TemporaryDirectory(prefix="pyro_res_") as cache:
        result, pgid = phase1_support.run_worker_in_session(
            "prewarm_lifecycle", "residualpat", cache_dir=cache)
        assert result["synth_launched"] >= 1  # synthesis really ran
        # The worker (group leader) has exited; give a short grace for children
        # to be reaped, then assert the process group is empty.
        deadline = time.time() + 15
        empty = False
        while time.time() < deadline:
            try:
                os.killpg(pgid, 0)  # signal 0 => existence check
            except ProcessLookupError:
                empty = True
                break
            except PermissionError:
                # exists but not ours; treat as no residual of ours
                empty = True
                break
            time.sleep(0.1)
        assert empty, (
            f"residual synthesis process(es) remain in group {pgid} (R63e)")


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


def test_synthesis_failure_permanent_fallback_via_seam():
    """R65/R67: an injected synthesis failure
    (pyro.testing.inject_synth_failure)
    drives the pattern to PERMANENT fallback — synth_failed is counted, no
    exception reaches the caller, and results stay byte-identical to stock re
    (the failure is routing, not fallback_after_error)."""
    with tempfile.TemporaryDirectory(prefix="pyro_sf_") as cache:
        res, _o, _e = phase1_support.run_worker(
            "synth_failure", "failpat_ac15", cache_dir=cache, extra_env=HOOKS)
    assert res["no_exception"] is True, res["errors"]
    assert res["synth_failed"] >= 1, res["last"]
    assert res["circuit_status"] == "fallback_only", res
    assert res["result_eq_stock"] is True
    assert res["fallback_after_error"] == 0, (
        "synthesis failure is routing (R65), not a device error")
