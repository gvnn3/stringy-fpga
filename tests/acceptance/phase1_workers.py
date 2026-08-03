"""Out-of-process worker harness for the Phase 1 synthesis-lifecycle tests.

This is TEST SUPPORT (not a test module and not implementation): it drives ONLY
the public `pyro` / `pyro.re` surface the spec defines (R62 prewarm, R66 stats,
R31 explain, §7.1 API) and prints a JSON result to stdout for the parent test to
assert against.  It is a real importable file with a `__main__` guard so the
synthesis service's multiprocessing spawn/forkserver workers (R63) can re-import
it safely.  Env (PYRO_CACHE_DIR / PYRO_FORCE_MODEL / PYRO_DISABLE) is inherited
from the parent and must be set BEFORE importing pyro (R35a sampling at import).

Commands (argv[1]) each print one JSON object:
  prewarm_lifecycle <pattern> [subject]
  launch_policy <pattern> <n_dispatches>
  dedup <pattern>
  prewarm_types
  persist_write <pattern>
  persist_read <pattern>
  tier_equiv <pattern> <subject>
"""
import json
import os
import sys
import time

# Ensure the repo root (which holds the `pyro` package) is importable when this
# file is run directly as a subprocess or re-imported by a spawn/forkserver
# worker of the synthesis service (R63).
_REPO_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

SYNTH_TIMEOUT_S = float(os.environ.get("PYRO_TEST_SYNTH_TIMEOUT", "45"))


def _canon(m):
    """JSON-safe canonical form of a str-subject Match (span/groups/lastindex/
    lastgroup), or None."""
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


def _poll_until(pre, pattern, done, timeout=SYNTH_TIMEOUT_S):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        e = pre.explain(pattern)
        s = pre.stats()
        last = (e["circuit_status"], s["synth_launched"], s["synth_succeeded"],
                s["synth_failed"])
        if done(e, s):
            return True, last
        time.sleep(0.05)
    return False, last


def cmd_prewarm_lifecycle(pattern, subject=None):
    import pyro
    import pyro.re as pre
    subject = subject if subject is not None else ("zz " + pattern + " zz")
    t0 = time.perf_counter()
    ret = pyro.prewarm(pattern)
    prewarm_ns = time.perf_counter() - t0
    reached, last = _poll_until(
        pre, pattern,
        lambda e, s: e["circuit_status"] in ("warm", "resident")
        or s["synth_succeeded"] > 0 or s["synth_failed"] > 0,
    )
    s = pre.stats()
    m = pre.search(pattern, subject)
    return {
        "prewarm_return_is_none": ret is None,
        "prewarm_seconds": prewarm_ns,
        "reached_terminal": reached,
        "last": last,
        "circuit_status": pre.explain(pattern)["circuit_status"],
        "synth_launched": s["synth_launched"],
        "synth_succeeded": s["synth_succeeded"],
        "synth_failed": s["synth_failed"],
        "pr_loads": s["pr_loads"],
        "circuits_resident": s["circuits_resident"],
        "match_span": (list(m.span()) if m else None),
        "fallback_after_error": s["fallback_after_error"],
        "device_errors": s["device_errors"],
    }


def cmd_launch_policy(pattern, n):
    # Force the model so that short eligible subjects are dispatched (R51b),
    # each counting as an HW-eligible dispatch toward the launch policy (R4a).
    os.environ["PYRO_FORCE_MODEL"] = "1"
    import pyro
    import pyro.re as pre
    pyro.refresh_env()
    n = int(n)
    launched_before = pre.stats()["synth_launched"]
    subj = "zz " + pattern + " zz"
    for _ in range(n):
        pre.search(pattern, subj)
    launched_after = pre.stats()["synth_launched"]
    return {"n": n, "launched_before": launched_before,
            "launched_after": launched_after,
            "delta": launched_after - launched_before}


def cmd_dedup(pattern):
    import pyro
    import pyro.re as pre
    base = pre.stats()["synth_launched"]
    for _ in range(5):
        pyro.prewarm(pattern)
    return {"launched_delta": pre.stats()["synth_launched"] - base}


def cmd_prewarm_types():
    import pyro
    errors = []
    try:
        assert pyro.prewarm("abc") is None
        assert pyro.prewarm(["abc", "def", r"\d+"]) is None      # iterable
        # fallback-only, ignored
        assert pyro.prewarm(r"(a)\1") is None
        assert pyro.prewarm(["abc", r"(?=x)"]) is None           # mixed
        # non-encodable, ignored
        assert pyro.prewarm("\ud800") is None
    except BaseException as e:  # noqa
        errors.append(repr(e))
    return {"no_raise": not errors, "errors": errors}


def cmd_persist_write(pattern):
    import pyro
    import pyro.re as pre
    pyro.prewarm(pattern)
    reached, last = _poll_until(
        pre, pattern,
        lambda e, s: s["synth_succeeded"] > 0 or s["synth_failed"] > 0)
    s = pre.stats()
    return {"reached": reached, "last": last,
            "synth_succeeded": s["synth_succeeded"],
            "synth_failed": s["synth_failed"],
            "circuit_status": pre.explain(pattern)["circuit_status"],
            "cache_files": sorted(os.listdir(os.environ["PYRO_CACHE_DIR"]))}


def cmd_persist_read(pattern):
    # Fresh process, same PYRO_CACHE_DIR: the artifact must be found
    # WARM/RESIDENT
    # without a new synthesis launch (R4 invariant / R63d cache persistence).
    import pyro.re as pre
    s0 = pre.stats()
    status = pre.explain(pattern)["circuit_status"]
    # Give a brief window for a background PR-load (warm->resident) if any.
    reached, last = _poll_until(
        pre, pattern,
        lambda e, s: e["circuit_status"] in ("warm", "resident"),
        timeout=10)
    return {"initial_status": status,
            "final_status": pre.explain(pattern)["circuit_status"],
            "synth_launched": pre.stats()["synth_launched"],
            "launched_at_start": s0["synth_launched"],
            "reached_warm_or_resident": reached,
            "cache_files": sorted(os.listdir(os.environ["PYRO_CACHE_DIR"]))}


def cmd_tier_equiv(pattern, subject):
    """Serve the same (pattern, subject) via three routing configurations and
    return each configuration's canonical result for the parent to compare to
    stock re (R36 asynchrony clause, R53, R58a)."""
    import pyro
    import pyro.re as pre

    def under(env_disable, env_model):
        os.environ.pop("PYRO_DISABLE", None)
        os.environ.pop("PYRO_FORCE_MODEL", None)
        if env_disable:
            os.environ["PYRO_DISABLE"] = "1"
        if env_model:
            os.environ["PYRO_FORCE_MODEL"] = "1"
        pyro.refresh_env()
        return {
            "search": _canon(pre.search(pattern, subject)),
            "findall": pre.findall(pattern, subject),
            "finditer": [_canon(m) for m in pre.finditer(pattern, subject)],
            "sub": pre.sub(pattern, "X", subject),
            "split": pre.split(pattern, subject),
        }

    disabled = under(True, False)     # cold-fallback path (genuine re)
    # Drive the pattern hot / resident via prewarm, then dispatch via model
    # tier.
    os.environ.pop("PYRO_DISABLE", None)
    os.environ["PYRO_FORCE_MODEL"] = "1"
    pyro.refresh_env()
    pyro.prewarm(pattern)
    _poll_until(pre, pattern,
                lambda e, s: e["circuit_status"] in ("warm", "resident")
                or s["synth_succeeded"] > 0 or s["synth_failed"] > 0)
    # model / resident-circuit stand-in (R7/R51b)
    resident = under(False, True)
    model = under(False, True)
    return {"disabled": disabled, "model": model, "resident": resident,
            "circuit_status": pre.explain(pattern)["circuit_status"]}


def cmd_synth_failure(pattern):
    """R67/R65: inject a synthesis failure for `pattern`, prewarm it, and
    confirm
    the permanent-fallback transition (synth_failed counted, no exception,
    result
    still byte-identical to stock re)."""
    import re as stdre
    import pyro
    import pyro.re as pre
    errors = []
    reached, last, eq = False, None, False
    try:
        pyro.testing.inject_synth_failure(pattern)
        pyro.prewarm(pattern)
        reached, last = _poll_until(
            pre, pattern,
            lambda e, s: (s["synth_failed"] > 0
                          or e["circuit_status"] == "fallback_only"))
        subj = "z " + pattern + " z"
        m = pre.search(pattern, subj)
        exp = stdre.search(pattern, subj)
        eq = (
    m.span() == exp.span()) if (
        m and exp) else (
            m is None and exp is None)
        # prewarm/search a second time to prove no retry-storm and still no
        # raise
        pyro.prewarm(pattern)
        pre.search(pattern, subj)
    except BaseException as e:  # noqa
        errors.append(repr(e))
    s = pre.stats()
    return {"no_exception": not errors, "errors": errors, "reached": reached,
            "last": last, "synth_failed": s["synth_failed"],
            "circuit_status": pre.explain(pattern)["circuit_status"],
            "result_eq_stock": eq,
            "fallback_after_error": s["fallback_after_error"]}


_COMMANDS = {
    "prewarm_lifecycle": cmd_prewarm_lifecycle,
    "synth_failure": cmd_synth_failure,
    "launch_policy": cmd_launch_policy,
    "dedup": cmd_dedup,
    "prewarm_types": cmd_prewarm_types,
    "persist_write": cmd_persist_write,
    "persist_read": cmd_persist_read,
    "tier_equiv": cmd_tier_equiv,
}


def main(argv):
    cmd = argv[1]
    result = _COMMANDS[cmd](*argv[2:])
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
