"""Out-of-process workers for AC-3-4 (stats/lifecycle counters under fault and
synthesis-failure injection).  (R52, R61, R65, R66, R67)

TEST SUPPORT, not a test module: each command drives ONLY the public surface
(`pyro.re` search/stats/explain, `pyro.prewarm`, `pyro.refresh_env`,
`pyro.testing.*` — the frozen R67 seam set, nothing added) inside a fresh
process bound to an isolated PYRO_CACHE_DIR, and prints one JSON object for
the parent test to assert against.  Anything that can launch synthesis runs
HERE, never in the pytest process (R63e): the synthesis service's
spawn/forkserver workers stay bounded to this short-lived child.

One deliberate, documented exception to "public surface only": the
``stats_lifecycle`` command ends by calling
``pyro.synth.residency.get_manager().shutdown()`` — an EXISTING internal API
(the same one pyro's own atexit hook calls), not a new seam (R67 stays
frozen).  Rationale: with the single-tenant PR region (pr_partitions=1, R64),
eviction replaces the resident circuit atomically under the manager lock, so
``circuits_resident`` never falls below 1 on the public surface within one
process; shutdown() is the one existing operation that empties the region so
the gauge's FALL (R66: gauges, not monotonic) is observable while the
monotonic counters are provably preserved across it.

Env (PYRO_CACHE_DIR / PYRO_FORCE_MODEL / PYRO_ENABLE_TEST_HOOKS /
PYRO_N_SYNTH / PYRO_NO_NATIVE) is inherited and must be set BEFORE this
process starts (R35a sampling at pyro import); the parent uses
phase3_support._worker_env, which also strips any leaked PYRO_TOOLCHAIN pin
so these workers always ride the MOCK toolchain.
"""
import json
import os
import sys
import time

# Repo root importable when run as a script or re-imported by a spawn /
# forkserver worker of the synthesis service (R63).
_REPO_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

SYNTH_TIMEOUT_S = float(os.environ.get("PYRO_TEST_SYNTH_TIMEOUT", "45"))


def _canon(m):
    """JSON-safe canonical form of a str-subject Match, or None."""
    if m is None:
        return None
    return {
        "span": list(m.span()),
        "group0": m.group(0),
        "groups": list(m.groups()),
        "spans": [list(m.span(i)) for i in range(len(m.groups()) + 1)],
        "lastindex": m.lastindex,
        "lastgroup": m.lastgroup,
        "groupdict": dict(sorted(m.groupdict().items())),
    }


def cmd_stats_lifecycle():
    """One fresh process walking the whole R66 counter lifecycle, snapshotting
    stats() at every labeled step.  The worker ASSERTS NOTHING — the parent
    test owns every assertion over the emitted snapshot sequence."""
    import re as stdre
    import pyro
    import pyro.re as pre

    snaps = []
    results = []

    def snap(label):
        snaps.append({"label": label, "stats": pre.stats()})

    def dispatch(label, pattern, subject):
        m = pre.search(pattern, subject)
        e = stdre.search(pattern, subject)
        results.append({"label": label, "pyro": _canon(m), "stock": _canon(e)})

    def poll_synth(done, label):
        """Deadline-poll stats() (R66 surface only; no new seams, R67),
        snapshotting every observation so the parent can see the
        circuits_synthesizing gauge in flight."""
        end = time.time() + SYNTH_TIMEOUT_S
        while time.time() < end:
            snap(label)
            s = snaps[-1]["stats"]
            if done(s):
                return True
            time.sleep(0.05)
        return False

    snap("baseline")

    # Loss-regime dispatch (R51 step 4: tiny subject, reuse 0) -> fallback.
    dispatch("loss", "acc34loss", "z acc34loss z")
    snap("after_loss")

    # Model-regime dispatch: force the gate open at an R35a sampling point.
    os.environ["PYRO_FORCE_MODEL"] = "1"
    pyro.refresh_env()
    dispatch("model", "acc34model", "z acc34model z")
    snap("after_model")

    # Launch synthesis for pattern A via prewarm (R62); the snapshot taken
    # immediately after the non-blocking return is the gauge-rise candidate.
    pat_a, pat_b = "acc34hotA", "acc34hotB"
    pyro.prewarm(pat_a)
    snap("after_prewarm_a")
    reached_a = poll_synth(
    lambda s: s["synth_succeeded"] >= 1 or s["synth_failed"] >= 1,
     "poll_a")
    snap("synth_a_done")

    # Dispatch A: warm artifact -> mock PR load -> resident (R64), model serve.
    dispatch("resident_a", pat_a, "z acc34hotA z")
    snap("after_resident_a")

    # Pattern B: synthesize, then dispatch -> single-tenant eviction of A
    # (R64).
    pyro.prewarm(pat_b)
    snap("after_prewarm_b")
    reached_b = poll_synth(
    lambda s: s["synth_succeeded"] >= 2 or s["synth_failed"] >= 1,
     "poll_b")
    snap("synth_b_done")
    dispatch("resident_b", pat_b, "z acc34hotB z")
    snap("after_evict")

    # Gauge FALL for circuits_resident (see module docstring): the existing
    # manager shutdown empties the PR region and the in-flight set while
    # leaving the monotonic counters untouched.
    from pyro.synth import residency
    residency.get_manager().shutdown()
    snap("final_shutdown")

    return {
        "snaps": snaps,
        "results": results,
        "reached": {"a": reached_a, "b": reached_b},
        "stats_keys": sorted(pre.stats().keys()),
        "native_router": pyro.native_router(),
        "explain_a": pre.explain(pat_a),
    }


def cmd_synth_failure_stats(pattern):
    """R65/R67 via the REAL launch path: with PYRO_ENABLE_TEST_HOOKS=1,
    PYRO_FORCE_MODEL=1 and PYRO_N_SYNTH=2 sampled at import, arm
    inject_synth_failure(pattern) and make 5 identical dispatches, recording
    result + stock-re result + a stats() snapshot after each.  Expected shape
    (asserted by the parent): call 1 -> model (below threshold); call 2 ->
    model + the injected launch fails deterministically (synth_launched+1,
    synth_failed+1, no service process); calls 3..5 -> permanent fallback
    (R65 negative cache), each a plain `fallback` (never
    fallback_after_error)."""
    import re as stdre
    import pyro
    import pyro.re as pre

    baseline = pre.stats()
    pyro.testing.inject_synth_failure(pattern)
    subj = "z " + pattern + " z"
    calls = []
    for i in range(5):
        m = pre.search(pattern, subj)
        e = stdre.search(pattern, subj)
        calls.append({"i": i, "pyro": _canon(m), "stock": _canon(e),
                      "stats": pre.stats()})
    return {
        "baseline": baseline,
        "calls": calls,
        "circuit_status": pre.explain(pattern)["circuit_status"],
        "hooks_enabled_env": os.environ.get("PYRO_ENABLE_TEST_HOOKS"),
    }


def cmd_synth_failure_persists(pattern):
    """R65 permanence: a FRESH process sharing the failed pattern's
    PYRO_CACHE_DIR (negative cache on disk, hooks NOT enabled here) must route
    every dispatch to plain fallback with zero synthesis relaunches and
    stock-identical results.  Env: PYRO_FORCE_MODEL=1, PYRO_N_SYNTH=2 — so if
    the negative cache were broken, the launch policy WOULD refire and
    synth_launched would move (an honest failure, not a vacuous pass)."""
    import re as stdre
    import pyro.re as pre

    baseline = pre.stats()
    subj = "z " + pattern + " z"
    calls = []
    for i in range(3):
        m = pre.search(pattern, subj)
        e = stdre.search(pattern, subj)
        calls.append({"i": i, "pyro": _canon(m), "stock": _canon(e),
                      "stats": pre.stats()})
    return {
        "baseline": baseline,
        "calls": calls,
        "circuit_status": pre.explain(pattern)["circuit_status"],
    }


_COMMANDS = {
    "stats_lifecycle": cmd_stats_lifecycle,
    "synth_failure_stats": cmd_synth_failure_stats,
    "synth_failure_persists": cmd_synth_failure_persists,
}


def main(argv):
    result = _COMMANDS[argv[1]](*argv[2:])
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
