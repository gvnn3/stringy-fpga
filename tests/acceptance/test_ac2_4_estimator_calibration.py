"""AC-2-4: estimator-vs-real P&R calibration + real-synth-failure semantics.

LIVE clause (toolchain_present, R71): for every calibration pattern that
synthesizes on the real vivado path, the L2 estimator agrees with genuine
post-route utilization within the PRE-REGISTERED ESTIMATOR_CALIBRATION_MARGIN
(R74).  For each pattern p, letting est_luts/est_ffs be the estimator's numbers
(public explain() est_resources, R11/R12/R31) and real_luts/real_ffs the genuine
post-route utilization (ooc_metrics manifest, R72c), ALL FOUR must hold:
  1. real_luts <= est_luts          (never under-count LUTs — conservative)
  2. real_ffs  <= est_ffs           (never under-count FFs  — conservative)
  3. est_luts  <= 10 * max(1, real_luts)   (not absurdly loose)
  4. est_ffs   <= 10 * max(1, real_ffs)    (not absurdly loose)

Always-LIVE clauses (no toolchain needed):
  * MAX_REPEAT respected: an over-MAX_REPEAT pattern is classified fallback-only
    by the estimator (R11–R13/R12), yet still byte-identical via fallback;
  * estimator-pass-but-synth-fail: a pattern that passes the estimate but fails
    synthesis becomes PERMANENTLY fallback-only with a diagnostic and NO
    caller-visible error (R65), driven via the public inject_synth_failure seam
    (R67) — deterministic and drivable without a fabricated real failure.
(R11–R13, R65, R73, R74, R77)
"""
import tempfile

import pytest

import oracle
import phase1_support
import phase2_support
from phase2_support import vivado_corpus  # noqa: F401 — shared session fixture

MARGIN = phase2_support.ESTIMATOR_CALIBRATION_MARGIN  # R74 constant (== 10)
HOOKS = {"PYRO_ENABLE_TEST_HOOKS": "1"}


def _est_val(est, key):
    assert isinstance(est, dict) and est, f"missing est_resources: {est!r}"
    assert key in est, f"est_resources lacks {key!r}: {est!r}"
    return int(est[key])


@pytest.mark.parametrize("pattern",
    [p for p,
    _f,
     _s in phase2_support.CALIBRATION_PATTERNS])
def test_estimator_within_calibration_margin_per_pattern(
    vivado_corpus, pattern):
    """LIVE (R74): the four per-pattern calibration clauses hold for every
    successfully-synthesized calibration pattern."""
    entry = vivado_corpus[pattern]
    if entry.get("worker_error"):
        pytest.skip(f"pattern {pattern!r} vivado worker crashed (isolated per "
                    f"pattern): {entry['worker_error']}")
    man = entry["manifest"]
    if man is None or man.get("payload_kind") != "ooc_metrics":
        pytest.skip(f"pattern {pattern!r} did not synthesize on the real path "
                    "(no ooc_metrics manifest); R74 is evaluated only over "
                    "successfully-synthesized patterns, and an empty success "
                    "set "
                    "records a SKIP, never a FAIL (R74, v2.1.1)")
    # R74a: calibration is toolchain-bound.  A PASS may be keyed ONLY to the
    # current pinned toolchain_version (2025.2, 0x19020000); a 2023.1
    # (0x17010000)
    # artifact is NOT evidence for the 2025.2 pin and must not yield a PASS.
    if "toolchain_version" in man and int(
    man["toolchain_version"]) != phase2_support.VIVADO_TOOLCHAIN_VERSION:
        pytest.skip(
            f"pattern {pattern!r} manifest toolchain_version "
            f"{int(man['toolchain_version']):#010x} != pinned 2025.2 "
            f"{phase2_support.VIVADO_TOOLCHAIN_VERSION:#010x}; stale-toolchain "
            "evidence cannot key a calibration PASS (R74a)")
    est = entry["est"]
    est_luts, est_ffs = _est_val(est, "luts"), _est_val(est, "ffs")
    real_luts, real_ffs = int(man["luts"]), int(man["ffs"])

    # Clauses 1-2: conservative (never under-count).
    assert real_luts <= est_luts, (
        f"[{pattern!r}] real_luts {real_luts} > est_luts {est_luts} — "
        f"estimator "
        "under-counted LUTs (R74 clause 1)")
    assert real_ffs <= est_ffs, (
        f"[{pattern!r}] real_ffs {real_ffs} > est_ffs {est_ffs} — estimator "
        "under-counted FFs (R74 clause 2)")
    # Clauses 3-4: not absurdly loose (10x ceiling; max(1,.) guards real==0).
    assert est_luts <= MARGIN * max(1, real_luts), (
        f"[{pattern!r}] est_luts {est_luts} > {MARGIN}*max(1,{real_luts}) "
        "(R74 clause 3)")
    assert est_ffs <= MARGIN * max(1, real_ffs), (
        f"[{pattern!r}] est_ffs {est_ffs} > {MARGIN}*max(1,{real_ffs}) "
        "(R74 clause 4)")


@pytest.mark.parametrize("entry", oracle.OVERCAP,
                         ids=[e[0] for e in oracle.OVERCAP])
def test_max_repeat_over_bound_is_fallback_only(entry):
    """Always-LIVE (model): a bounded repeat expanding beyond MAX_REPEAT is
    estimator-rejected to fallback (no Vivado needed) yet stays byte-identical
    (R11–R13/R12)."""
    import pyro
    import pyro.re as pre
    label, pattern, flags, subject = entry
    info = pre.explain(pattern, flags)
    assert info["eligible"] is False, (
        f"[{label}] over-MAX_REPEAT pattern must be fallback-only (R12)")
    assert info["circuit_status"] == "fallback_only"
    assert info["est_resources"] is None, (
        "a fallback-only pattern carries no resource estimate (R12/R31)")
    import os
    os.environ["PYRO_FORCE_MODEL"] = "1"
    pyro.refresh_env()
    oracle.assert_equivalent(
    pre,
    pattern,
    subject,
    flags,
     label=f"maxrepeat/{label}")


def test_estimate_pass_then_synth_fail_permanent_fallback():
    """Always-LIVE (R65/R67): an injected synthesis failure drives a pattern
    that
    passed the estimate to PERMANENT fallback — synth_failed counted, no
    exception
    to the caller, byte-identical results, NOT counted as
    fallback_after_error."""
    with tempfile.TemporaryDirectory(prefix="pyro_ac24_sf_") as cache:
        res, _o, _e = phase1_support.run_worker(
            "synth_failure", "ac24_estpass_synthfail", cache_dir=cache,
            extra_env=HOOKS)
    assert res["no_exception"] is True, res["errors"]
    assert res["synth_failed"] >= 1, res["last"]
    assert res["circuit_status"] == "fallback_only", res
    assert res["result_eq_stock"] is True
    assert res["fallback_after_error"] == 0, (
        "synthesis failure is routing (R65), not a device error (R52)")
