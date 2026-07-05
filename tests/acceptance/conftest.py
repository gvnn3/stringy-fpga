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
import sys

sys.path.insert(0, os.path.dirname(__file__))

import pytest

_ENV_KEYS = ("PYRO_DISABLE", "PYRO_FORCE_MODEL")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "perf: performance / routing-overhead tests (AC-0-6, R3a/R5)"
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
