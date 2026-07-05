"""Synthesis-launch policy + prewarm (R4a/R62).

TDD focus: synthesis launches ONLY when a pattern's HW-eligible dispatch count
reaches N_synth, or via prewarm — never for every pattern — and launches are
deduplicated (R63a).  The policy is deterministic given the call history.
"""

import re

import pytest

import pyro.hdl as hdl
from pyro.synth import BitstreamCache, ResidencyManager, ToolchainConfig


@pytest.fixture
def manager_factory(tmp_path):
    made = []

    def _make(n_synth=5, config=ToolchainConfig(), **kw):
        cache = BitstreamCache(tmp_path / f"m{len(made)}")
        mgr = ResidencyManager(cache, toolchain_config=config,
                               n_synth=n_synth, **kw)
        made.append(mgr)
        return mgr

    yield _make
    for mgr in made:
        mgr.reset()


ENC = hdl.ENC_UTF8


def test_no_launch_below_threshold(manager_factory):
    mgr = manager_factory(n_synth=5)
    for _ in range(4):
        mgr.note_eligible_dispatch("hot[0-9]+", 0, ENC)
    assert mgr.stats()["synth_launched"] == 0


def test_launch_fires_at_threshold(manager_factory):
    mgr = manager_factory(n_synth=5)
    for _ in range(5):
        mgr.note_eligible_dispatch("hot[0-9]+", 0, ENC)
    assert mgr.stats()["synth_launched"] == 1


def test_launch_is_deduped_after_threshold(manager_factory):
    mgr = manager_factory(n_synth=3)
    for _ in range(20):
        mgr.note_eligible_dispatch("hot[0-9]+", 0, ENC)
    # Only one job for the key even though it stayed hot for many calls (R63a).
    assert mgr.stats()["synth_launched"] == 1


def test_prewarm_launches_regardless_of_count(manager_factory):
    mgr = manager_factory(n_synth=1000)
    assert mgr.prewarm("cold[0-9]+", 0) is True
    assert mgr.stats()["synth_launched"] == 1


def test_prewarm_ignores_fallback_only(manager_factory):
    mgr = manager_factory()
    # Backreference -> fallback-only (R10): prewarm silently ignores it (R62).
    assert mgr.prewarm(r"(a)\1", 0) is False
    assert mgr.stats()["synth_launched"] == 0


def test_prewarm_dedups_with_in_flight(manager_factory):
    mgr = manager_factory()
    mgr.prewarm("dup[0-9]+", 0)
    mgr.prewarm("dup[0-9]+", 0)
    assert mgr.stats()["synth_launched"] == 1


def test_canonically_equal_patterns_share_launch(manager_factory):
    # R4b: "(?i)abc.[0-9]" and ("abc.[0-9]", re.I) share the circuit key, so a
    # dispatch of one counts toward the other's launch threshold.
    mgr = manager_factory(n_synth=4)
    for _ in range(2):
        mgr.note_eligible_dispatch("(?i)abc.[0-9]", 0, ENC)
    for _ in range(2):
        mgr.note_eligible_dispatch("abc.[0-9]", re.I, ENC)
    assert mgr.stats()["synth_launched"] == 1


def test_policy_is_deterministic(manager_factory):
    # Same call history -> same launch outcome across two independent managers.
    outcomes = []
    for _ in range(2):
        mgr = manager_factory(n_synth=3)
        launched = []
        for _ in range(3):
            mgr.note_eligible_dispatch("det[0-9]+", 0, ENC)
            launched.append(mgr.stats()["synth_launched"])
        outcomes.append(launched)
    assert outcomes[0] == outcomes[1] == [0, 0, 1]
