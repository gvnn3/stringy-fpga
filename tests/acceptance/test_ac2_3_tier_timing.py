"""AC-2-3: cold->warm->resident timing matches R4's model.

LIVE clause (toolchain_present, R71): the cold->warm leg is real Vivado synthesis
that genuinely takes minutes, runs OUT OF PROCESS, and NEVER blocks or slows a
caller — calls are served by fallback throughout (R63).  Verified from the
session `vivado_corpus` worker record: prewarm returns promptly (non-blocking),
searches proceed byte-identically while synthesis is in flight, and the synthesis
wall time is minutes-scale.

Cold->warm across a simulated restart is LIVE (cache reload): a fresh process
sharing PYRO_CACHE_DIR finds the artifact warm without re-synthesis (R4/R63d).

Model-side async/non-blocking is asserted in ALL cases (even without the
toolchain) via the mock path (R63/R58a).

SKIP clause (device_usable, R71 — FALSE here): warm->resident PR-load timing
(~O(100 ms)) and immediate resident dispatch ON DEVICE.
(R4, R63, R71, R77)
"""
import tempfile

import pytest

import phase1_support
import phase2_support
from phase2_support import vivado_corpus  # noqa: F401 — shared session fixture


def _primary(corpus):
    """Entry for the first pattern whose vivado worker RAN (per-pattern crash
    isolation); fails clearly if every worker crashed."""
    pat = phase2_support.first_ran_pattern(corpus)
    if pat is None:
        pytest.fail("no calibration pattern's vivado worker ran (all crashed)")
    return pat, corpus[pat]


def test_model_async_prewarm_is_nonblocking_all_cases():
    """Model-side (all cases): prewarm returns promptly while the out-of-process
    service synthesizes; the pattern reaches a terminal tier and no error counter
    moves (R63/R58a).  Runs on the mock toolchain regardless of vivado presence."""
    with tempfile.TemporaryDirectory(prefix="pyro_ac23_model_") as cache:
        res, _o, _e = phase1_support.run_worker(
            "prewarm_lifecycle", "ac23_model_pat", cache_dir=cache)
    assert res["prewarm_return_is_none"] is True
    assert res["prewarm_seconds"] < 3.0, (
        f"prewarm blocked {res['prewarm_seconds']}s — synthesis must be async (R63)")
    assert res["reached_terminal"] is True, res["last"]
    assert res["fallback_after_error"] == 0
    assert res["device_errors"] == 0


def test_live_real_synth_is_async_and_never_blocks_caller(vivado_corpus):
    """LIVE: real Vivado synthesis is async and never blocks/slows a caller — the
    triggering prewarm returns promptly and calls PROCEED via fallback,
    byte-identically, while synthesis runs out of process (R63/AC-2-3)."""
    w = _primary(vivado_corpus)[1]["worker"]
    assert w["prewarm_return_is_none"] is True
    assert w["prewarm_seconds"] < 3.0, (
        f"prewarm blocked {w['prewarm_seconds']}s while enqueuing real synthesis "
        "— must be non-blocking (R63)")
    assert w["search_raised"] is False, (
        f"a caller's search raised during real synthesis (R63): {w['errors']}")
    assert w["saw_synthesizing_or_cold"] is True, (
        "never observed the pattern cold/synthesizing — cannot prove calls were "
        "served during synthesis")
    assert w["searches_during_synth"] >= 1, (
        "no calls proceeded while synthesis was in flight (R63 non-blocking)")
    assert w["search_all_eq_stock"] is True, (
        "a call served during synthesis was NOT byte-identical to stock re (R16)")
    assert w["final_eq_stock"] is True


def test_live_real_synth_genuinely_takes_minutes(vivado_corpus):
    """LIVE: the cold->warm leg is genuine (real Vivado OOC synth+P&R is minutes,
    not instant).  Soft lower bound as evidence of real synthesis work (R63/P1)."""
    w = _primary(vivado_corpus)[1]["worker"]
    assert w["reached_terminal"] is True, w["last"]
    assert w["synth_wall_seconds"] >= 20.0, (
        f"real Vivado synthesis completed in {w['synth_wall_seconds']}s — too fast "
        "to be a genuine OOC synth+P&R (expected minutes) (R63/AC-2-3)")


def test_live_cold_to_warm_persists_across_restart(vivado_corpus):
    """LIVE: cold->warm survives a simulated process restart — a fresh process
    sharing PYRO_CACHE_DIR loads the artifact warm in PR-load time, not synthesis
    time, with no new launch (R4 invariant / R63d)."""
    pat, entry = _primary(vivado_corpus)
    read, _o, _e = phase1_support.run_worker(
        "persist_read", pat, cache_dir=entry["cache"],
        extra_env={"PYRO_TOOLCHAIN": "vivado",
                   "PYRO_VIVADO": phase2_support._vivado_install_dir()},
        timeout=120)
    assert read["initial_status"] in ("warm", "resident"), read
    assert read["synth_launched"] == 0, (
        "restart re-launched synthesis instead of a warm reload (R4/R63d)")


def test_warm_to_resident_on_device_skips():
    """SKIP: warm->resident PR-load timing (~O(100 ms)) and immediate resident
    dispatch require device_usable, FALSE here (R71)."""
    ok, reason = phase2_support.device_usable()
    if not ok:
        pytest.skip(reason)
    raise AssertionError("device_usable unexpectedly true — implement PR-load "
                         "timing + resident-dispatch assertions (R4/R77)")
