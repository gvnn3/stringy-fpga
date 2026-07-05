"""Pytest configuration and isolation fixtures for the PYRO Phase 0 suite.

Ensures every test starts from a clean, deterministic PYRO state:
  * the interposition shim is uninstalled,
  * PYRO_DISABLE / PYRO_FORCE_MODEL are cleared from os.environ and re-sampled
    via pyro.refresh_env() (R35a sampling point),
  * the compile/reuse caches are purged (so per-pattern reuse counters do not
    leak between tests, keeping R51-step-4 routing deterministic).

Tests that exercise env controls set the variables and call refresh_env()
themselves; this fixture restores the prior os.environ afterwards so the suite
is order-independent and leaves no global residue (brief hard rule).
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

# --------------------------------------------------------------------------
# Hygiene (Task 8 item 9): point the persistent bitstream cache (R4) at a
# per-session temp dir BEFORE `pyro` is first imported (the cache dir, like the
# env flags, is sampled at import — R35a), so synthesis-triggering tests
# (perf warmups crossing N_synth, prewarm) NEVER write to the developer's real
# ~/.cache.  Cleaned up (and asserted residue-free) at session end.
# Subprocess workers set their own PYRO_CACHE_DIR (temp dirs owned by the test).
# --------------------------------------------------------------------------
_OWN_CACHE_DIR = None
if not os.environ.get("PYRO_CACHE_DIR"):
    _OWN_CACHE_DIR = tempfile.mkdtemp(prefix="pyro_acc_cache_")
    os.environ["PYRO_CACHE_DIR"] = _OWN_CACHE_DIR

# Snapshot whether a plausible default cache dir exists, to prove our redirect
# kept the run from writing there.
_DEFAULT_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "pyro")
_DEFAULT_CACHE_PREEXISTED = os.path.exists(_DEFAULT_CACHE)

import pytest

_ENV_KEYS = ("PYRO_DISABLE", "PYRO_FORCE_MODEL")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "perf: performance / routing-overhead tests (AC-0-6, R3a/R5)"
    )


def pytest_sessionfinish(session, exitstatus):
    # Remove our per-session cache dir (leave no tempdir residue).
    if _OWN_CACHE_DIR and os.path.isdir(_OWN_CACHE_DIR):
        shutil.rmtree(_OWN_CACHE_DIR, ignore_errors=True)
    # Prove the redirect held: the default cache dir must not have been created
    # by our run.
    if not _DEFAULT_CACHE_PREEXISTED and os.path.exists(_DEFAULT_CACHE):
        raise AssertionError(
            f"cache residue leaked to {_DEFAULT_CACHE}; PYRO_CACHE_DIR redirect "
            "did not take effect (Task 8 item 9)"
        )


def _safe(fn):
    try:
        fn()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def pyro_isolation():
    import pyro
    import pyro.re as pre

    saved = {k: os.environ.get(k) for k in _ENV_KEYS}

    _safe(pyro.uninstall)
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    _safe(pyro.refresh_env)
    _safe(pre.purge)

    try:
        yield
    finally:
        _safe(pyro.uninstall)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _safe(pyro.refresh_env)
        _safe(pre.purge)
