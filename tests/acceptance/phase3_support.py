"""Parent-side helpers for the Phase-3 transparency-regression tests (AC-3-1 /
R60) and the stats/lifecycle tests (AC-3-4).

Three jobs, all driven from the pytest (parent) process:

  1. `run_corpus` — launch one named corpus program (phase3_workers.py) in an
     out-of-process worker under EITHER stock ``re`` or ``pyro.install()``,
     bound to an isolated PYRO_CACHE_DIR, with chosen env knobs.  Synthesis
     therefore NEVER runs inside the pytest process (R63e; the session-finish
     residue check in conftest.py enforces this) — the mock toolchain's
     spawn/forkserver workers are bounded to the short-lived child.
  2. `poll_tier` — DEADLINE-POLL the public ``pyro.re.explain()``/``stats()``
     surface for a tier transition.  Polling only: this module adds NO new
     seams to ``pyro.testing`` (R67 is normative; amendment A2 is pending owner
     review, so the seam set is frozen).  The worker imports this helper so
     parent and worker share one polling discipline.
  3. `assert_call_sequences_identical` / `assert_tier_transition` — jsonify-
     based comparison of the per-call output sequences of the stock and
     installed runs (JSON has no tuple: findall's multi-group tuples arrive as
     lists on BOTH sides because both sequences cross the worker JSON
     boundary; `phase1_support.jsonify` keeps any in-process side symmetric
     too), plus the POSITIVE mid-run tier-transition assertion that keeps
     AC-3-1 non-vacuous (cold at call 0, warm/resident later, synth_launched
     > 0 — asserted HERE, from test code, never from inside a corpus program).

Env-knob discipline (conftest _ENV_KEYS trap):
  * Knobs a test passes to a worker go through ``extra_env`` (subprocess env
    only) — never written into the pytest process's os.environ.
  * `_worker_env` starts each worker from a clean routing/toolchain config: it
    pops PYRO_DISABLE / PYRO_FORCE_MODEL / PYRO_ENABLE_TEST_HOOKS /
    PYRO_N_SYNTH / PYRO_TEST_SYNTH_TIMEOUT, and ALSO PYRO_TOOLCHAIN /
    PYRO_VIVADO: Phase-3 corpus workers must ride the MOCK toolchain (the
    default), and a leaked vivado pin (the _PRISTINE_TOOLCHAIN incident) would
    otherwise make a transparency corpus launch REAL multi-hour Vivado
    synthesis.  This module never calls phase2_support.toolchain_present()
    (which WRITES the vivado pin into os.environ).
  * PYRO_NO_NATIVE is deliberately INHERITED (never popped, never registered
    in conftest _ENV_KEYS): it selects the router implementation at pyro
    import and the whole suite is run once natively and once with
    PYRO_NO_NATIVE=1.  Popping or per-test clearing it would silently flip
    worker subprocesses back to the native router during the pure-Python run,
    making that run's coverage vacuous.
"""
import json
import os
import subprocess
import sys
import time

from phase1_support import jsonify  # tuple->list canonicaliser (trap: JSON has no tuple)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phase3_workers.py")

# Parent-side subprocess deadline.  The worker's own tier poll uses
# PYRO_TEST_SYNTH_TIMEOUT (default 45 s, mock toolchain); allow the corpus
# calls + poll + interpreter startup comfortably.
WORKER_SUBPROC_TIMEOUT_S = 240

# Default worker-side poll deadline for a mock-toolchain tier transition.
DEFAULT_TIER_TIMEOUT_S = 45.0

# Tier states that count as "the circuit arrived" (R4/R31 lifecycle).
HOT_TIERS = ("warm", "resident")


def _worker_env(cache_dir, extra_env):
    """Environment for one corpus worker: isolated cache, clean knob baseline.

    See the module docstring for the pop list rationale (mock toolchain is
    mandatory here; PYRO_NO_NATIVE passes through untouched).
    """
    env = dict(os.environ)
    env["PYRO_CACHE_DIR"] = str(cache_dir)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    for k in ("PYRO_DISABLE", "PYRO_FORCE_MODEL", "PYRO_ENABLE_TEST_HOOKS",
              "PYRO_N_SYNTH", "PYRO_TEST_SYNTH_TIMEOUT",
              # Never let a leaked vivado pin reach a Phase-3 corpus worker.
              "PYRO_TOOLCHAIN", "PYRO_VIVADO"):
        env.pop(k, None)
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items()})
    return env


def parse_records(stdout):
    """Parse the worker's JSON-lines stream into {'calls', 'snaps', 'done'}."""
    calls, snaps, done = [], [], None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        kind = rec.get("type")
        if kind == "call":
            calls.append(rec)
        elif kind == "snap":
            snaps.append(rec)
        elif kind == "done":
            done = rec
    if done is None:
        raise AssertionError(f"worker emitted no 'done' record:\n{stdout}")
    return {"calls": calls, "snaps": snaps, "done": done}


def run_corpus(program, mode, *, cache_dir, extra_env=None,
               timeout=WORKER_SUBPROC_TIMEOUT_S):
    """Run corpus `program` under `mode` ('stock' | 'installed') out of process.

    `cache_dir` is the worker's isolated PYRO_CACHE_DIR (created if missing;
    normally a pytest tmp_path).  `extra_env` sets spec-named knobs sampled at
    the worker's pyro import (R35a) — e.g. {'PYRO_N_SYNTH': 2} to pin the R4a
    launch boundary.  Returns parse_records() of the worker's stdout.
    """
    assert mode in ("stock", "installed"), mode
    os.makedirs(str(cache_dir), exist_ok=True)
    env = _worker_env(cache_dir, extra_env)
    proc = subprocess.run(
        [sys.executable, WORKER, "corpus", program, mode],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=timeout,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"phase3 corpus worker ({program}, {mode}) failed rc={proc.returncode}\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    try:
        return parse_records(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"phase3 corpus worker ({program}, {mode}) emitted non-JSON lines:\n"
            f"{proc.stdout}\n{proc.stderr}")


def poll_tier(pre, pattern, flags=0, *, want=HOT_TIERS, timeout=None,
              interval=0.05, on_snapshot=None):
    """Deadline-poll the PUBLIC explain()/stats() surface for a tier transition.

    `pre` is the worker's ``pyro.re`` module.  Polls until
    ``explain(pattern, flags)['circuit_status']`` enters `want`, synthesis
    fails terminally (``stats()['synth_failed'] > 0``), or the deadline
    passes.  Returns ``(reached, last_snapshot)`` where last_snapshot is
    ``{'circuit_status': ..., 'stats': ...}``.  `on_snapshot(snapshot)` is
    invoked for every observation so a worker can stream them out.

    Pure polling of R31/R66 surfaces with a deadline — deliberately NOT an
    await seam (R67's seam set is frozen pending amendment A2).
    """
    if timeout is None:
        timeout = float(os.environ.get("PYRO_TEST_SYNTH_TIMEOUT",
                                       str(DEFAULT_TIER_TIMEOUT_S)))
    deadline = time.time() + float(timeout)
    last = None
    while True:
        e = pre.explain(pattern, flags)
        s = pre.stats()
        last = {"circuit_status": e["circuit_status"], "stats": s}
        if on_snapshot is not None:
            on_snapshot(last)
        if e["circuit_status"] in want:
            return True, last
        if s.get("synth_failed", 0) > 0:
            return False, last
        if time.time() >= deadline:
            return False, last
        time.sleep(interval)


# --------------------------------------------------------------------------
# Parent-side assertions.
# --------------------------------------------------------------------------
def assert_call_sequences_identical(stock_result, installed_result, label=""):
    """AC-3-1/R60 core check: identical per-call outputs AND exceptions.

    Both sides are worker `parse_records` results.  Values are compared after
    `jsonify` (tuple/list symmetry, trap 3); exceptions compare by canonical
    type AND str(e) AND — for re.error — msg/pos/pattern (trap 10), exactly as
    the worker encoded them.
    """
    sc, ic = stock_result["calls"], installed_result["calls"]
    assert len(sc) == len(ic), (
        f"[{label}] call-count mismatch: stock={len(sc)} installed={len(ic)}")
    for a, b in zip(sc, ic):
        assert a["label"] == b["label"], (
            f"[{label}] call {a['i']}: label skew {a['label']!r} vs {b['label']!r}"
            " — corpus program is nondeterministic")
        ka = jsonify({"value": a.get("value"), "exc": a.get("exc")})
        kb = jsonify({"value": b.get("value"), "exc": b.get("exc")})
        assert ka == kb, (
            f"[{label}] call {a['i']} ({a['label']}) diverged under install():\n"
            f"  stock    ={ka!r}\n  installed={kb!r}")


def assert_tier_transition(installed_result, *, want=HOT_TIERS, label=""):
    """POSITIVE assertion that a genuine MID-RUN tier transition happened.

    Without this, AC-3-1's "patterns that transition tiers mid-run" clause is
    vacuous (a corpus pinned to fallback forever compares equal trivially).
    Asserted from TEST code over the worker's emitted snapshots — the corpus
    program itself asserts nothing.  Checks, in order:

      * a snapshot at ``at == 0`` (before the first call) reports ``cold``;
      * some later snapshot reports a tier in `want` (warm/resident);
      * that first hot snapshot is strictly mid-run: calls were dispatched
        BOTH before it (its ``at`` > 0) and AFTER it;
      * final stats prove the machinery fired: ``synth_launched > 0`` and
        ``synth_succeeded > 0`` (mock toolchain completes, trap 7).
    """
    snaps = installed_result["snaps"]
    calls = installed_result["calls"]
    done = installed_result["done"]
    tag = f"[{label}] " if label else ""
    assert snaps, tag + "no snapshots emitted — was the worker run installed?"
    at0 = [s for s in snaps if s["at"] == 0]
    assert at0, tag + "no pre-first-call (at=0) snapshot emitted"
    assert at0[0]["circuit_status"] == "cold", (
        tag + f"circuit_status at call 0 is {at0[0]['circuit_status']!r}, "
        "expected 'cold' (isolated PYRO_CACHE_DIR should start cold)")
    hot = [s for s in snaps if s["circuit_status"] in want]
    assert hot, (
        tag + f"tier never reached {want}: statuses observed = "
        f"{sorted(set(s['circuit_status'] for s in snaps))} — the corpus "
        "never crossed R51 step 4 (need >=64 KiB subjects or >=32 reuses "
        "AND a small PYRO_N_SYNTH) or synthesis never completed")
    first_hot_at = hot[0]["at"]
    assert first_hot_at > 0, (
        tag + f"first {want} snapshot at at={first_hot_at}: transition did not "
        "happen mid-run (no calls preceded it)")
    assert any(c["i"] >= first_hot_at for c in calls), (
        tag + "no calls dispatched AFTER the tier transition — the corpus "
        "must keep calling post-transition for AC-3-1's mid-run clause")
    stats = done["stats"]
    assert stats["synth_launched"] > 0, tag + f"synth_launched==0: {stats}"
    assert stats["synth_succeeded"] > 0, tag + f"synth_succeeded==0: {stats}"
    return first_hot_at
