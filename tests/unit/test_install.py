"""Interposition + routing/env tests (R31, R33-R36, R51; AC-0-1/AC-0-5/AC-0-6)."""
import re

import pytest

import pyro
import pyro.re as pre


# --- AC-0-1: import-alias surface -----------------------------------------

def test_module_symbols_present():
    for name in ("compile", "search", "match", "fullmatch", "findall",
                 "finditer", "sub", "subn", "split", "escape", "purge",
                 "error", "explain", "stats"):
        assert hasattr(pre, name)
    for f in ("A", "ASCII", "I", "IGNORECASE", "M", "MULTILINE",
              "S", "DOTALL", "X", "VERBOSE", "U", "UNICODE"):
        assert getattr(pre, f) == getattr(re, f)


def test_error_is_re_error():
    assert pre.error is re.error


# --- AC-0-5: install / uninstall ------------------------------------------

def test_install_uninstall_roundtrip():
    orig_search = re.search
    try:
        pyro.install()
        assert re.search is not orig_search
        assert re.search(r"\d+", "ab12").group(0) == "12"
    finally:
        pyro.uninstall()
    assert re.search is orig_search


def test_explain_not_on_patched_re():
    try:
        pyro.install()
        assert not hasattr(re, "explain")   # R31
        assert not hasattr(re, "stats")
    finally:
        pyro.uninstall()


def test_installed_results_identical(monkeypatch):
    monkeypatch.delenv("PYRO_FORCE_MODEL", raising=False)
    ref = re.findall(r"\w+", "the quick brown fox")
    try:
        pyro.install()
        got = re.findall(r"\w+", "the quick brown fox")
    finally:
        pyro.uninstall()
    assert got == ref


# --- R35: env controls ----------------------------------------------------

def test_pyro_disable_forces_fallback(monkeypatch):
    monkeypatch.setenv("PYRO_DISABLE", "1")
    monkeypatch.setenv("PYRO_FORCE_MODEL", "1")
    pre.purge()
    from pyro import _route
    _route.reset_stats()
    m = pre.search(r"(\d+)", "ab 55")
    assert m.group(0) == "55"
    # Disabled: nothing served by the model.
    assert pre.stats()["model"] == 0
    assert pre.stats()["fallback"] >= 1
    # Fallback path returns a stock re.Match (thin path, no wrapper).
    assert isinstance(m, re.Match)


def test_short_input_routes_fallback(monkeypatch):
    monkeypatch.delenv("PYRO_FORCE_MODEL", raising=False)
    monkeypatch.delenv("PYRO_DISABLE", raising=False)
    pre.purge()
    from pyro import _route
    _route.reset_stats()
    # A short one-shot call must route to fallback (R3/R51.4).
    m = pre.search(r"\d+", "x7y")
    assert m.group(0) == "7"
    assert pre.stats()["fallback"] == 1
    assert pre.stats()["model"] == 0
    assert isinstance(m, re.Match)


def test_force_model_overrides_size_gate(monkeypatch):
    monkeypatch.setenv("PYRO_FORCE_MODEL", "1")
    monkeypatch.delenv("PYRO_DISABLE", raising=False)
    pre.purge()
    from pyro import _route
    _route.reset_stats()
    pre.search(r"\d+", "x7y")
    assert pre.stats()["model"] == 1
