"""Spec-named configuration knob PYRO_N_SYNTH (spec §9.1 R68).

PYRO_N_SYNTH overrides the R4a launch threshold, is validated (positive int; an
invalid value is ignored), is sampled only at the R35a points, and pins the exact
launch boundary deterministically.  All env mutation is via monkeypatch +
refresh_env so the cached snapshot is clean for the next test.
"""

import pytest

import pyro
import pyro.hdl as hdl
from pyro import _route
from pyro.synth import residency as res

ENC = hdl.ENC_UTF8


@pytest.fixture
def temp_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("PYRO_CACHE_DIR", str(tmp_path))
    yield
    res.reset_manager()


def test_override_applied_to_new_manager(monkeypatch, temp_cache):
    monkeypatch.setenv("PYRO_N_SYNTH", "5")
    pyro.refresh_env()
    res.reset_manager()
    assert res.get_manager()._n_synth == 5


def test_invalid_value_ignored(monkeypatch, temp_cache):
    monkeypatch.setenv("PYRO_N_SYNTH", "notanint")
    pyro.refresh_env()
    res.reset_manager()
    assert res.get_manager()._n_synth == res.N_SYNTH_DEFAULT


def test_nonpositive_value_ignored(monkeypatch, temp_cache):
    monkeypatch.setenv("PYRO_N_SYNTH", "0")
    pyro.refresh_env()
    res.reset_manager()
    assert res.get_manager()._n_synth == res.N_SYNTH_DEFAULT
    monkeypatch.setenv("PYRO_N_SYNTH", "-4")
    pyro.refresh_env()
    res.reset_manager()
    assert res.get_manager()._n_synth == res.N_SYNTH_DEFAULT


def test_override_pushed_to_live_manager_at_sampling_point(monkeypatch, temp_cache):
    pyro.refresh_env()
    res.reset_manager()
    mgr = res.get_manager()
    assert mgr._n_synth == res.N_SYNTH_DEFAULT
    monkeypatch.setenv("PYRO_N_SYNTH", "7")
    pyro.refresh_env()                       # R35a sampling point -> live update
    assert mgr._n_synth == 7


def test_override_pins_launch_boundary(monkeypatch, temp_cache):
    monkeypatch.setenv("PYRO_N_SYNTH", "3")
    pyro.refresh_env()
    res.reset_manager()
    mgr = res.get_manager()
    for _ in range(2):
        mgr.note_eligible_dispatch("hotknob[0-9]+", 0, ENC)
    assert mgr.stats()["synth_launched"] == 0   # below the pinned boundary
    mgr.note_eligible_dispatch("hotknob[0-9]+", 0, ENC)
    assert mgr.stats()["synth_launched"] == 1   # exactly at N_synth=3
    mgr.drain(10.0)


def test_mid_process_change_deferred_until_sampling_point(monkeypatch, temp_cache):
    # R35a/R68: mutating PYRO_N_SYNTH WITHOUT a sampling point must not change the
    # in-force threshold.
    monkeypatch.setenv("PYRO_N_SYNTH", "9")
    pyro.refresh_env()
    res.reset_manager()
    mgr = res.get_manager()
    assert mgr._n_synth == 9
    monkeypatch.setenv("PYRO_N_SYNTH", "2")   # no refresh_env() -> not sampled
    assert mgr._n_synth == 9
