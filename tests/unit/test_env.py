"""Cached env-sampling semantics (R35a-R35d, amended R51.1/R51.4, AC-0-5/6).

Env flags are sampled into a cached snapshot only at import / install /
uninstall / refresh_env; the per-call path consults only the cache.  These
tests pin the deferred-refresh behavior.
"""
import re

import pytest

import pyro
import pyro.re as pre
from pyro import _route


def _served_by_model(m):
    # Fallback returns a genuine re.Match; the model path returns a HybridMatch.
    return not isinstance(m, re.Match)


# --- R35a/R35b: mid-run os.environ mutation is deferred until sampling -----

def test_mutation_without_sampling_is_deferred(monkeypatch):
    monkeypatch.setenv("PYRO_FORCE_MODEL", "1")
    monkeypatch.delenv("PYRO_DISABLE", raising=False)
    pyro.refresh_env()
    pre.purge()
    _route.reset_stats()

    m1 = pre.search(r"(\d+)", "ab 55")           # forced -> model
    assert _served_by_model(m1)
    assert pre.stats()["model"] == 1

    # Set PYRO_DISABLE mid-run but do NOT sample: routing must not change yet.
    monkeypatch.setenv("PYRO_DISABLE", "1")
    m2 = pre.search(r"(\d+)", "ab 55")
    assert _served_by_model(m2)                  # still model (cache stale)
    assert pre.stats()["model"] == 2
    assert pre.stats()["fallback"] == 0

    # Now reach a sampling point: the new value applies.
    pyro.refresh_env()
    m3 = pre.search(r"(\d+)", "ab 55")
    assert isinstance(m3, re.Match)              # disabled -> fallback
    assert pre.stats()["model"] == 2
    assert pre.stats()["fallback"] == 1


# --- R35d: refresh_env applies new values ---------------------------------

def test_refresh_env_applies(monkeypatch):
    monkeypatch.delenv("PYRO_FORCE_MODEL", raising=False)
    monkeypatch.delenv("PYRO_DISABLE", raising=False)
    pyro.refresh_env()
    pre.purge()
    _route.reset_stats()
    # No force: a short one-shot goes to fallback.
    assert isinstance(pre.search(r"\d+", "x7y"), re.Match)
    assert pre.stats()["model"] == 0

    monkeypatch.setenv("PYRO_FORCE_MODEL", "1")
    pyro.refresh_env()
    assert _served_by_model(pre.search(r"\d+", "x7y"))
    assert pre.stats()["model"] == 1


# --- R35c: PYRO_DISABLE present at a sampling point forces fallback --------

def test_disable_at_sampling_forces_fallback(monkeypatch):
    monkeypatch.setenv("PYRO_DISABLE", "1")
    monkeypatch.setenv("PYRO_FORCE_MODEL", "1")
    pyro.refresh_env()
    pre.purge()
    _route.reset_stats()
    m = pre.search(r"(\d+)", "ab 55")
    assert isinstance(m, re.Match)               # genuine re.Match on fallback
    assert m.group(0) == "55"
    assert pre.stats()["model"] == 0


# --- R35d: refresh_env is idempotent --------------------------------------

def test_refresh_env_idempotent(monkeypatch):
    monkeypatch.setenv("PYRO_FORCE_MODEL", "1")
    monkeypatch.delenv("PYRO_DISABLE", raising=False)
    pyro.refresh_env()
    snap1 = _route._ENV
    pyro.refresh_env()
    snap2 = _route._ENV
    assert snap1 == snap2 == (False, True)
    pre.purge()
    _route.reset_stats()
    assert _served_by_model(pre.search(r"\d+", "x7y"))


# --- R35a.2: install()/uninstall() are sampling points --------------------

def test_install_is_sampling_point(monkeypatch):
    monkeypatch.setenv("PYRO_DISABLE", "1")
    orig_search = re.search
    try:
        pyro.install()                           # samples -> disabled
        assert re.search is not orig_search
        m = re.search(r"(\d+)", "ab 55")
        assert isinstance(m, re.Match) and m.group(0) == "55"
    finally:
        pyro.uninstall()
    assert re.search is orig_search


def test_uninstall_is_sampling_point(monkeypatch):
    # Prime cache to disabled, then clear the var and let uninstall() re-sample.
    monkeypatch.setenv("PYRO_DISABLE", "1")
    pyro.refresh_env()
    assert _route._ENV[0] is True
    monkeypatch.delenv("PYRO_DISABLE", raising=False)
    monkeypatch.setenv("PYRO_FORCE_MODEL", "1")
    pyro.uninstall()                             # sampling point
    assert _route._ENV == (False, True)
