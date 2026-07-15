"""AC-3-2: automatic tier-based dispatch is transparent — the silent
fallback->resident upgrade edge, made observable on this device-free host by
the R67 v2.5.0 seams.  (R4a, R16, R51, R51b, R51b-strict, R53, R62, R64, R66,
R67)

What is proven, and how it stays honest:

  1. THE UPGRADE EDGE (strict run).  Under ``set_strict_residency(True)``
     (R51b-strict: the R51b device-free precedence of R7 is suspended, so R51
     step 5 binds exactly as on hardware), a HW-eligible hot pattern's
     dispatches count as genuine ``fallback`` while its circuit is
     cold/synthesizing, and flip to ``model`` (the resident stand-in, R7) once
     ``await_synthesis`` reports ``"resident"`` — a SINGLE monotone flip in
     the per-call dispatch-counter classification, with ``synth_launched``
     firing exactly once at the PYRO_N_SYNTH boundary (R4a/R68) and
     ``fallback_after_error``/``device_errors``/``hardware`` provably flat
     (the upgrade is routing, not an error path, and no device exists here).
  2. BYTE-IDENTITY THROUGHOUT (R16/R53).  Every dispatch on BOTH sides of the
     flip — and every dispatch of the default-mode control run — returns the
     stock-``re`` result byte-identically (canonicalised match objects
     compared across the worker JSON boundary; the worker never installs, so
     its module-global ``re`` is the genuine stdlib oracle).
  3. DEFAULT-R51B CONTROL RUN.  The same script with the seam left off shows
     WHY the seam exists (R67 rationale): the model serves every tier, all
     dispatches classify ``model``, and no upgrade edge is observable — while
     the tier transition and its lifecycle bookkeeping (R64/R64a) still
     happen, and results are identical to the strict run's.
  4. COLD / FALLBACK-ONLY SERVICE (AC-3-2 second sentence).  A never-eligible
     pattern (backreference) is served by fallback in BOTH modes; a cold
     below-threshold pattern is served by fallback under strict routing (the
     hardware rule) and by the model under default R51b
     — with no synthesis launched by either.
  5. WEDGE-FIX CANARY.  ``pyro._route._RESIDENCY_CONSULT_FAILURES == 0``
     across both tier-transition runs.  §9 discipline note: this is the ONE
     sanctioned diagnostic read in this suite — the counter is deliberately
     NOT part of the R31/R66 shapes; it records residency-consult failures
     that R52/R65 force ``_consult_residency`` to swallow, and the notebook
     entry of 14 Jul 2026 20:26:10 (counter-wedge fix) designates AC-3-2 as
     the place to assert it stays zero, so a silently dead R4a launch policy
     can never pass this AC vacuously.  Everything else here drives public
     surfaces only: pyro.re.search/explain/stats (R31/R66) and the R67 seams.

Hermeticity: synthesis NEVER runs in this pytest process (R63e) — both runs
happen in phase3_workers.py subprocesses on isolated PYRO_CACHE_DIRs via
phase3_support._worker_env (which also strips any leaked PYRO_TOOLCHAIN pin,
so workers always ride the MOCK toolchain).  PYRO_ENABLE_TEST_HOOKS and
PYRO_N_SYNTH reach the workers via extra_env ONLY (subprocess env, sampled at
the worker's pyro import per R35a) — never written into this process's
os.environ; both keys are registered in conftest._ENV_KEYS.  PYRO_NO_NATIVE
passes through untouched (the suite runs natively and pure-Python).
"""
import json
import os
import subprocess
import sys

import pytest

import pyro.testing as _pt
import phase3_support

# --- R71 honesty: SKIP names the missing prerequisite ------------------------
# AC-3-2's upgrade edge is unobservable without the R67 v2.5.0 seams (the spec
# says so explicitly in R51b-strict/R67); on a build predating them the honest
# outcome is SKIP-with-named-prerequisite, not a misleading failure.
_MISSING_SEAMS = [name for name in ("await_synthesis", "set_strict_residency")
                  if not callable(getattr(_pt, name, None))]
if _MISSING_SEAMS:
    pytest.skip(
        "missing prerequisite: pyro.testing lacks the R67 v2.5.0 seam(s) "
        f"{_MISSING_SEAMS} (amendment A2) — the AC-3-2 fallback->resident "
        "upgrade edge is unobservable on a device-free host without them "
        "(R51b-strict/R67)",
        allow_module_level=True)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phase3_workers.py")

# Both runs: R67 gate on (await_synthesis must genuinely wait; the strict seam
# must bind) and the R4a launch boundary pinned at 2 (R68) so the launch fires
# deterministically on the second pre-phase dispatch.
WORKER_ENV = {"PYRO_ENABLE_TEST_HOOKS": "1", "PYRO_N_SYNTH": "2"}

# R66 dispatch-counter vector (per-call attribution is asserted over ALL of
# these, so a dispatch misfiled into an unexpected counter fails loudly).
DISPATCH_COUNTERS = ("hardware", "model", "fallback", "fallback_after_error",
                     "device_errors", "total")

NOT_RESIDENT_TIERS = ("cold", "synthesizing", "warm")


def _run_worker(mode, *, cache_dir, timeout=240):
    """One tier_upgrade worker run, out of process (R63e discipline)."""
    os.makedirs(str(cache_dir), exist_ok=True)
    env = phase3_support._worker_env(cache_dir, WORKER_ENV)
    proc = subprocess.run(
        [sys.executable, WORKER, "tier_upgrade", mode],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=timeout,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"tier_upgrade worker ({mode}) failed rc={proc.returncode}\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    try:
        result = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError):
        raise AssertionError(
            f"tier_upgrade worker ({mode}) emitted no JSON result:\n"
            f"{proc.stdout}\n{proc.stderr}")
    assert result.get("type") == "tier_upgrade", result
    return result


# Worker runs are pure JSON data; module-scoped so the tests share one
# subprocess run per mode without re-synthesizing.  No pyro state crosses
# tests (the runs live in exited subprocesses on isolated caches).
@pytest.fixture(scope="module")
def strict_run(tmp_path_factory):
    cache = tmp_path_factory.mktemp("ac32_strict") / "cache"
    return _run_worker("strict", cache_dir=cache)


@pytest.fixture(scope="module")
def default_run(tmp_path_factory):
    cache = tmp_path_factory.mktemp("ac32_default") / "cache"
    return _run_worker("default", cache_dir=cache)


def _classify(run):
    """Classify every main-phase dispatch as 'fallback' or 'model' from the
    FULL dispatch-counter vector between consecutive stats snapshots.

    Also asserts, per call: exactly one dispatch counted (total+1), hardware
    flat (no device on this host), and the error counters flat — the upgrade
    edge is ordinary ROUTING; a strict-residency fallback must never be
    misfiled as fallback_after_error (R51b-strict/R66).
    """
    kinds = []
    prev = run["baseline"]
    for i, rec in enumerate(run["records"]):
        cur = rec["stats"]
        d = {k: cur[k] - prev[k] for k in DISPATCH_COUNTERS}
        tag = f"[{run['mode']}] record {i} ({rec['phase']}, tier_before={rec['tier_before']})"
        assert d["total"] == 1, f"{tag}: expected exactly one dispatch, deltas={d}"
        assert d["hardware"] == 0, f"{tag}: hardware moved with no device present: {d}"
        assert d["fallback_after_error"] == 0 and d["device_errors"] == 0, (
            f"{tag}: tier routing charged an ERROR counter (R51b-strict says "
            f"never fallback_after_error): {d}")
        if d["fallback"] == 1 and d["model"] == 0:
            kinds.append("fallback")
        elif d["model"] == 1 and d["fallback"] == 0:
            kinds.append("model")
        else:
            raise AssertionError(f"{tag}: unclassifiable dispatch deltas {d}")
        prev = cur
    return kinds


def _side(run, label):
    matches = [s for s in run["side"] if s["label"] == label]
    assert matches, f"[{run['mode']}] worker emitted no {label!r} side record"
    return matches[0]


def _side_delta(rec):
    return {k: rec["after"][k] - rec["before"][k] for k in rec["after"]}


# ==========================================================================
# 1. The silent fallback -> resident upgrade edge (strict residency).
# ==========================================================================
class TestUpgradeEdgeStrict:
    def test_worker_baseline_is_all_zero(self, strict_run):
        """A fresh worker on an isolated PYRO_CACHE_DIR starts with every
        counter at zero — nothing is pre-counted, so every attribution below
        is anchored."""
        base = strict_run["baseline"]
        assert all(v == 0 for v in base.values()), f"non-zero baseline: {base}"
        assert strict_run["records"][0]["tier_before"] == "cold", (
            "isolated cache did not start cold: "
            f"{strict_run['records'][0]['tier_before']}")

    def test_prelaunch_dispatches_fall_back_and_launch_fires_at_n_synth(self, strict_run):
        """R4a/R51b-strict/R68: with N_synth pinned at 2, the first dispatch
        is a genuine fallback that launches NOTHING; the second is a genuine
        fallback AND fires exactly one background synthesis launch."""
        assert strict_run["n_synth"] == 2, (
            "PYRO_N_SYNTH did not reach the worker (env plumbing broke): "
            f"{strict_run['n_synth']}")
        kinds = _classify(strict_run)
        assert kinds[:2] == ["fallback", "fallback"], (
            f"pre-launch dispatches not served by fallback under strict "
            f"residency: {kinds[:2]} — is the R67 gate reaching the worker?")
        rec0, rec1 = strict_run["records"][:2]
        assert rec0["stats"]["synth_launched"] - strict_run["baseline"]["synth_launched"] == 0, (
            f"launch fired before N_synth=2: {rec0['stats']}")
        assert rec1["stats"]["synth_launched"] - rec0["stats"]["synth_launched"] == 1, (
            f"launch did not fire exactly at N_synth=2: {rec1['stats']}")

    def test_upgrade_is_a_single_monotone_flip(self, strict_run):
        """AC-3-2 core: the per-call classification is fallback...fallback |
        model...model — at least one of each, exactly one flip, never a
        regression to fallback after the upgrade (the upgrade is silent AND
        permanent for the run), and every pre-flip dispatch was observed at a
        not-resident tier."""
        kinds = _classify(strict_run)
        assert "fallback" in kinds and "model" in kinds, (
            f"no upgrade edge observed: {kinds} (awaited_tier="
            f"{strict_run['awaited_tier']!r}) — synthesis may never have "
            "completed on the mock toolchain")
        flip = kinds.index("model")
        assert kinds == ["fallback"] * flip + ["model"] * (len(kinds) - flip), (
            f"classification interleaves — not a single silent upgrade: {kinds}")
        for rec in strict_run["records"][:flip]:
            assert rec["tier_before"] in NOT_RESIDENT_TIERS, (
                f"a fallback-classified dispatch saw tier "
                f"{rec['tier_before']!r} — fallback while resident violates "
                "R51 step 5")

    def test_await_synthesis_reports_resident_and_post_dispatches_are_model(self, strict_run):
        """R67: await_synthesis returned the terminal tier 'resident' (not a
        timeout's last-observed tier), and every dispatch made AFTER it
        returned — the 'post' phase — was served by the resident stand-in
        (model, R7) from an observed 'resident' tier."""
        assert strict_run["awaited_tier"] == "resident", (
            f"await_synthesis returned {strict_run['awaited_tier']!r} — "
            "the circuit never reached the resident tier (R4/R31)")
        kinds = _classify(strict_run)
        post = [(rec, kind) for rec, kind in
                zip(strict_run["records"], kinds) if rec["phase"] == "post"]
        assert len(post) == 3, f"expected 3 post-upgrade dispatches, got {len(post)}"
        for rec, kind in post:
            assert kind == "model", (
                f"post-upgrade dispatch classified {kind!r}: the upgrade did "
                f"not stick (stats={rec['stats']})")
            assert rec["tier_before"] == "resident", (
                f"post-upgrade dispatch saw tier {rec['tier_before']!r}")
        assert strict_run["explain_final"]["circuit_status"] == "resident"

    def test_lifecycle_bookkeeping_unchanged_by_strict_mode(self, strict_run):
        """R64/R64a/R66: strict residency suspends only the R51b dispatch
        precedence — the lifecycle really ran: exactly ONE launch for the hot
        pattern (pump-phase dispatches while synthesizing never relaunch,
        R63a dedup), one success, no failures, and the promotion is a real PR
        load with the pattern counted resident."""
        final = strict_run["final_stats"]
        assert final["synth_launched"] == 1, final
        assert final["synth_succeeded"] == 1, final
        assert final["synth_failed"] == 0, final
        assert final["pr_loads"] >= 1, final
        assert final["circuits_resident"] == 1, final
        assert final["circuits_synthesizing"] == 0, final


# ==========================================================================
# 2. Byte-identity throughout (R16/R53) — both sides of the flip, both modes.
# ==========================================================================
class TestByteIdentity:
    @pytest.mark.parametrize("run_name", ["strict_run", "default_run"])
    def test_every_dispatch_matches_stock_re(self, run_name, request):
        """R16/R36/R53: every dispatch of the run — cold fallback,
        synthesizing fallback, the promoting call, resident stand-in — equals
        the stock-re canonical match (span/groups/spans/lastindex/lastgroup/
        groupdict), computed in the same worker from the UNPATCHED stdlib."""
        run = request.getfixturevalue(run_name)
        expected = run["expected"]
        assert isinstance(expected, dict) and expected.get("__match__"), (
            f"stock reference is not a match: {expected!r} — the AC would be "
            "vacuous on a None-returning script")
        for i, rec in enumerate(run["records"]):
            assert rec["value"] == expected, (
                f"[{run['mode']}] dispatch {i} ({rec['phase']}, "
                f"tier_before={rec['tier_before']}) diverged from stock re:\n"
                f"  got     ={rec['value']!r}\n  expected={expected!r}")

    def test_strict_and_default_runs_return_identical_values(self, strict_run, default_run):
        """R67's own constraint: enabling the seam MUST NOT change any
        caller-visible result — only the counters and latency differ."""
        assert strict_run["expected"] == default_run["expected"]
        for side_label in ("backref", "cold"):
            assert (_side(strict_run, side_label)["value"]
                    == _side(default_run, side_label)["value"])

    @pytest.mark.parametrize("run_name", ["strict_run", "default_run"])
    def test_side_dispatches_match_stock_re(self, run_name, request):
        run = request.getfixturevalue(run_name)
        for rec in run["side"]:
            assert rec["value"] == rec["expected"], (
                f"[{run['mode']}] side dispatch {rec['label']!r} diverged "
                f"from stock re:\n  got     ={rec['value']!r}\n"
                f"  expected={rec['expected']!r}")


# ==========================================================================
# 3. The default-R51b control run: why the seam exists (R67 rationale).
# ==========================================================================
class TestDefaultModeControl:
    def test_model_serves_every_tier_no_edge_observable(self, default_run):
        """Plain R51b: the model serves the hot pattern at EVERY tier, so all
        dispatches classify 'model' and the upgrade has no dispatch-counter
        edge — the documented reason AC-3-2 needs R51b-strict.  The tier
        transition itself still happens (R64a bookkeeping is live in both
        modes)."""
        kinds = _classify(default_run)
        assert kinds and set(kinds) == {"model"}, (
            f"default-R51b dispatches were not all model-served: {kinds}")
        assert default_run["awaited_tier"] == "resident", (
            f"await_synthesis returned {default_run['awaited_tier']!r} in the "
            "default run")
        final = default_run["final_stats"]
        assert final["synth_launched"] == 1 and final["synth_succeeded"] == 1
        assert final["circuits_resident"] == 1
        assert default_run["explain_final"]["circuit_status"] == "resident"


# ==========================================================================
# 4. Cold / fallback-only patterns are served by fallback (AC-3-2, sentence 2).
# ==========================================================================
class TestColdAndFallbackOnly:
    def test_fallback_only_pattern_served_by_fallback_in_both_modes(self, strict_run, default_run):
        """A never-HW-eligible pattern (backreference) is a plain fallback
        dispatch in BOTH modes — strict residency changes nothing for it —
        and launches no synthesis."""
        for run in (strict_run, default_run):
            rec = _side(run, "backref")
            d = _side_delta(rec)
            assert d["fallback"] == 1 and d["total"] == 1, (
                f"[{run['mode']}] backref dispatch deltas: {d}")
            assert d["model"] == 0 and d["hardware"] == 0
            assert d["fallback_after_error"] == 0 and d["device_errors"] == 0
            assert d["synth_launched"] == 0, (
                f"[{run['mode']}] a fallback-only pattern launched synthesis")
            assert rec["circuit_status"] == "fallback_only", rec["circuit_status"]

    def test_cold_pattern_served_by_fallback_under_strict_routing(self, strict_run, default_run):
        """A cold, below-threshold pattern: under strict routing (the
        hardware rule R51 step 5) it is served by fallback; under default
        R51b the model stands in.  Neither mode launches synthesis for it
        (one dispatch < N_synth=2, R4a) and it stays cold."""
        strict_rec = _side(strict_run, "cold")
        ds = _side_delta(strict_rec)
        assert ds["fallback"] == 1 and ds["model"] == 0 and ds["total"] == 1, (
            f"strict-mode cold dispatch deltas: {ds}")
        default_rec = _side(default_run, "cold")
        dd = _side_delta(default_rec)
        assert dd["model"] == 1 and dd["fallback"] == 0 and dd["total"] == 1, (
            f"default-mode cold dispatch deltas: {dd}")
        for rec, d in ((strict_rec, ds), (default_rec, dd)):
            assert d["synth_launched"] == 0, (
                "a single below-threshold dispatch launched synthesis (R4a)")
            assert d["fallback_after_error"] == 0 and d["device_errors"] == 0
            assert rec["circuit_status"] == "cold", rec["circuit_status"]


# ==========================================================================
# 5. The counter-wedge canary (notebook 14 Jul 2026 20:26:10).
# ==========================================================================
class TestWedgeCanary:
    def test_no_residency_consult_failures_across_tier_transition_runs(self, strict_run, default_run):
        """pyro._route._RESIDENCY_CONSULT_FAILURES == 0 across BOTH
        tier-transition runs.

        §9 note: this counter is the ONE sanctioned diagnostic read in this
        suite (read once, at the end of each worker).  It is deliberately not
        part of the R31/R66 public shapes: it records residency-consult
        failures that _consult_residency MUST swallow (R52/R65), which is
        exactly how the AC-3-1 counter wedge hid — a corpse module pinned in
        the consult cache killed the R4a launch policy process-wide while
        every result stayed correct.  The notebook entry of 14 Jul 2026
        20:26:10 designates AC-3-2 as the canary site: a nonzero count here
        means dispatches went uncounted during these runs and the observed
        launch/upgrade behavior cannot be trusted."""
        for run in (strict_run, default_run):
            assert run["consult_failures"] == 0, (
                f"[{run['mode']}] {run['consult_failures']} residency "
                "consultation(s) failed and were swallowed during the "
                "tier-transition run — the R4a launch policy was (at least "
                "briefly) dead; see docs/notebook.md 14 Jul 2026 20:26:10")
