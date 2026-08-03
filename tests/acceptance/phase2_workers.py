"""Out-of-process worker harness for the Phase-2 acceptance tests.

TEST SUPPORT (not a test module, not implementation): drives ONLY the public
`pyro` / `pyro.re` surface (R62 prewarm, R66 stats, R31 explain, §7.1 API) and
prints one JSON object to stdout for the parent test to assert against.  A real
importable file with a __main__ guard so the synthesis service's
spawn/forkserver
workers (R63) can re-import it.  Toolchain selection (PYRO_TOOLCHAIN=vivado /
PYRO_VIVADO, R70) and PYRO_CACHE_DIR are inherited from the parent env and
MUST be
set BEFORE importing pyro (R35a sampling at import).

Commands (argv[1]):
  vivado_synth <pattern> [subject]   — enqueue REAL synth, prove non-blocking
  async
  eviction <p1|p2|p3|...> [subject]  — single-tenant PR eviction on the model
  (R64)
"""
import json
import os
import re as stdre
import sys
import time

_REPO_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Real Vivado OOC synth+P&R is minutes; poll generously (R63/R77).
SYNTH_TIMEOUT_S = float(os.environ.get("PYRO_TEST_SYNTH_TIMEOUT", "1200"))


def _canon(m):
    """JSON-safe canonical Match form (span/group0/groups/spans/lastindex/
    lastgroup/groupdict), or None."""
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


def cmd_vivado_synth(pattern, subject=None):
    """AC-2-1/AC-2-3 (LIVE): enqueue a REAL Vivado synthesis for `pattern`,
    prove
    prewarm is non-blocking (R62/R63), that calls PROCEED via fallback and never
    block/raise while synthesis is in flight (R63/AC-2-3), and drive the job
    to a
    terminal state so the parent can read the honest manifest (R72/R73/R75)."""
    import pyro
    import pyro.re as pre
    subject = subject if subject is not None else (
        "zz " + pattern + " zz " + pattern)
    exp = _canon(stdre.search(pattern, subject))

    errors = []
    t0 = time.perf_counter()
    ret = pyro.prewarm(pattern)                      # enqueue synth (R62/R4a)
    prewarm_seconds = time.perf_counter() - t0

    searches_during_synth = 0
    search_all_eq_stock = True
    saw_pre_terminal_status = []
    reached_terminal = False
    last = None
    synth_start = time.perf_counter()
    deadline = synth_start + SYNTH_TIMEOUT_S
    while time.perf_counter() < deadline:
        try:
            e = pre.explain(pattern)
            s = pre.stats()
            status = e["circuit_status"]
            last = (
    status,
    s["synth_launched"],
    s["synth_succeeded"],
     s["synth_failed"])
            # Exercise a real call CONCURRENTLY with synthesis; it MUST proceed
            # via fallback (byte-identical) and never raise (R63/AC-2-3).
            m = pre.search(pattern, subject)
            if status in ("cold", "synthesizing"):
                searches_during_synth += 1
                saw_pre_terminal_status.append(status)
                if _canon(m) != exp:
                    search_all_eq_stock = False
            if s["synth_succeeded"] > 0 or s["synth_failed"] > 0:
                reached_terminal = True
                break
        # noqa: BLE001 — any raise to the caller is a defect
        except BaseException as ex:
            errors.append(repr(ex))
        time.sleep(0.25)
    synth_wall_seconds = time.perf_counter() - synth_start

    s = pre.stats()
    final_m = pre.search(pattern, subject)
    return {
        "pattern": pattern,
        "prewarm_return_is_none": ret is None,
        "prewarm_seconds": prewarm_seconds,
        "searches_during_synth": searches_during_synth,
        "saw_synthesizing_or_cold": bool(saw_pre_terminal_status),
        "search_all_eq_stock": search_all_eq_stock,
        "search_raised": bool(errors),
        "errors": errors,
        "reached_terminal": reached_terminal,
        "last": last,
        "synth_wall_seconds": synth_wall_seconds,
        "synth_launched": s["synth_launched"],
        "synth_succeeded": s["synth_succeeded"],
        "synth_failed": s["synth_failed"],
        "pr_loads": s["pr_loads"],
        "circuit_status": pre.explain(pattern)["circuit_status"],
        "final_eq_stock": _canon(final_m) == exp,
        "cache_files": sorted(os.listdir(os.environ["PYRO_CACHE_DIR"])),
    }


def _poll(pre, pattern, done, timeout=60.0):
    end = time.perf_counter() + timeout
    last = None
    while time.perf_counter() < end:
        e = pre.explain(pattern)
        s = pre.stats()
        last = (e["circuit_status"], s["synth_succeeded"], s["synth_failed"])
        if done(e, s):
            return True, last
        time.sleep(0.05)
    return False, last


def cmd_eviction(patterns_csv, subject=None):
    """AC-2-6 (model, LIVE): single-tenant PR arbitration/eviction (R64)
    exercised
    on the software model.  Load several distinct patterns' circuits toward
    residency; assert the resident gauge never exceeds the single-tenant budget,
    that eviction is counted deterministically, and — the keystone — that
    results
    stay byte-identical to stock re across evict/reload (R53/R64)."""
    # dispatch via the model (R7/R51b)
    os.environ["PYRO_FORCE_MODEL"] = "1"
    import pyro
    import pyro.re as pre
    pyro.refresh_env()

    pats = patterns_csv.split("|")

    def subj_for(p):
        return subject if subject is not None else ("zz " + p + " zz " + p)

    base = pre.stats()
    max_resident_gauge = 0
    # Phase 1: drive each pattern hot -> warm/resident, dispatching once each.
    for p in pats:
        pyro.prewarm(p)
        _poll(pre, p, lambda e, s: e["circuit_status"] in ("warm", "resident")
              or s["synth_succeeded"] > 0 or s["synth_failed"] > 0, timeout=60)
        # dispatch => residency bookkeeping
        pre.search(p, subj_for(p))
        max_resident_gauge = max(
    max_resident_gauge,
     pre.stats()["circuits_resident"])

    # Phase 2: re-access every pattern (some evicted, reloaded) — results MUST
    # be
    # byte-identical to stock re regardless of eviction (R53/R64).
    per_pattern_eq = {}
    for p in pats:
        subj = subj_for(p)
        exp = stdre.search(p, subj)
        act = pre.search(p, subj)
        per_pattern_eq[p] = (
            (act.span() if act else None) == (exp.span() if exp else None))
        max_resident_gauge = max(
    max_resident_gauge,
     pre.stats()["circuits_resident"])

    s = pre.stats()
    return {
        "n_patterns": len(pats),
        "evicted_delta": s["circuits_evicted"] - base["circuits_evicted"],
        "max_resident_gauge": max_resident_gauge,
        "circuits_resident_final": s["circuits_resident"],
        "pr_loads_delta": s["pr_loads"] - base["pr_loads"],
        "all_eq_stock": all(per_pattern_eq.values()),
        "per_pattern_eq": per_pattern_eq,
        "synth_failed": s["synth_failed"],
        "fallback_after_error": s["fallback_after_error"],
    }


_COMMANDS = {
    "vivado_synth": cmd_vivado_synth,
    "eviction": cmd_eviction,
}


def main(argv):
    cmd = argv[1]
    result = _COMMANDS[cmd](*argv[2:])
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
