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


def run_worker(command, *args, cache_dir, timeout=150):
    """Run one worker command in a fresh process bound to `cache_dir`.

    Returns (parsed_json, raw_stdout, raw_stderr).  Raises on non-zero exit.
    """
    env = dict(os.environ)
    env["PYRO_CACHE_DIR"] = str(cache_dir)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    # Start from a clean routing config; the worker sets what it needs.
    env.pop("PYRO_DISABLE", None)
    env.pop("PYRO_FORCE_MODEL", None)
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
