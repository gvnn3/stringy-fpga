"""Parent-side helpers for Phase 1 tests: run the file-based worker harness
(phase1_workers.py) in an isolated subprocess with its own PYRO_CACHE_DIR, and
parse its JSON result.  Subprocess isolation keeps any background synthesis
service processes (R63, multiprocessing spawn/forkserver) bounded to a
short-lived child, so the pytest process leaves no residue.
"""
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phase1_workers.py")


def jsonify(obj):
    """Recursively coerce tuples -> lists so a value computed in-process compares
    symmetrically against the same value round-tripped through the worker's JSON
    boundary (JSON has no tuple type; e.g. findall's multi-group tuples arrive as
    lists).  Applied to BOTH sides of a cross-process comparison keeps the oracle
    (stock re, tuples) and the worker result (JSON, lists) type-symmetric."""
    if isinstance(obj, (tuple, list)):
        return [jsonify(x) for x in obj]
    if isinstance(obj, dict):
        return {k: jsonify(v) for k, v in obj.items()}
    return obj


def _worker_env(cache_dir, extra_env):
    env = dict(os.environ)
    env["PYRO_CACHE_DIR"] = str(cache_dir)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    # Start from a clean routing / test-hook config; caller/worker opt in.
    # PYRO_TEST_SYNTH_TIMEOUT (harness poll deadline) is popped for the same
    # reason: workers take it via extra_env, never from ambient os.environ, so
    # no test can skew another test's worker deadline (order-independence).
    # PYRO_NO_NATIVE is deliberately NOT popped: the router selection of the
    # enclosing suite run (native vs pure-Python) must pass through to workers.
    for k in ("PYRO_DISABLE", "PYRO_FORCE_MODEL", "PYRO_ENABLE_TEST_HOOKS",
              "PYRO_N_SYNTH", "PYRO_TEST_SYNTH_TIMEOUT"):
        env.pop(k, None)
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items()})
    return env


def run_worker(command, *args, cache_dir, extra_env=None, timeout=150):
    """Run one worker command in a fresh process bound to `cache_dir`.

    `extra_env` sets spec-named knobs sampled at the worker's import (R35a):
    PYRO_ENABLE_TEST_HOOKS, PYRO_N_SYNTH, PYRO_FORCE_MODEL, etc.
    Returns (parsed_json, raw_stdout, raw_stderr).  Raises on non-zero exit.
    """
    env = _worker_env(cache_dir, extra_env)
    proc = subprocess.run(
        [sys.executable, WORKER, command, *[str(a) for a in args]],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=timeout,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"worker {command} failed rc={proc.returncode}\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    try:
        return json.loads(proc.stdout), proc.stdout, proc.stderr
    except json.JSONDecodeError:
        raise AssertionError(f"worker {command} did not emit JSON:\n{proc.stdout}\n{proc.stderr}")


def run_worker_in_session(command, *args, cache_dir, extra_env=None, timeout=150):
    """Run a worker command in its OWN process group/session (start_new_session)
    so a test can prove R63e — after the worker exits, NO residual synthesis
    service processes remain in its group.

    Returns (parsed_json, pgid).  The caller checks the group is empty.
    """
    import os as _os
    env = _worker_env(cache_dir, extra_env)
    proc = subprocess.Popen(
        [sys.executable, WORKER, command, *[str(a) for a in args]],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=env, cwd=REPO_ROOT, start_new_session=True,
    )
    pgid = _os.getpgid(proc.pid)
    out, err = proc.communicate(timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(
            f"worker {command} failed rc={proc.returncode}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    try:
        return json.loads(out), pgid
    except json.JSONDecodeError:
        raise AssertionError(f"worker {command} did not emit JSON:\n{out}\n{err}")
