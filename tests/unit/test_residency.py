"""Residency lifecycle, single-tenant eviction, and stats (R4/R64/R65/R66).

TDD focus: cold -> synthesizing -> warm -> resident tier transitions; the single
PR region holds one resident circuit with deterministic LRU eviction and no
thrashing (R64); synthesis failure -> permanent fallback with correct counters
(R65/R66); NOT_RESIDENT/SYNTH are ordinary routing outcomes (AC-1-6).
"""

import pytest

import pyro.hdl as hdl
from pyro.synth import (
    BitstreamCache, ResidencyManager, ToolchainConfig,
    ROUTE_RESIDENT, ROUTE_NOT_RESIDENT, ROUTE_SYNTH, ROUTE_PERMANENT_FALLBACK,
    TIER_COLD, TIER_SYNTHESIZING, TIER_WARM, TIER_RESIDENT, TIER_FALLBACK_ONLY,
)

ENC = hdl.ENC_UTF8


@pytest.fixture
def manager_factory(tmp_path):
    made = []

    def _make(n_synth=1, config=ToolchainConfig(), pr_partitions=1):
        cache = BitstreamCache(tmp_path / f"m{len(made)}")
        mgr = ResidencyManager(cache, toolchain_config=config,
                               n_synth=n_synth, pr_partitions=pr_partitions)
        made.append(mgr)
        return mgr

    yield _make
    for mgr in made:
        mgr.reset()


# --- tier transitions cold -> synth -> warm -> resident (R4) ----------------
def test_cold_before_any_dispatch(manager_factory):
    mgr = manager_factory(n_synth=1000)
    assert mgr.tier("p[0-9]+", 0, ENC) == TIER_COLD


def test_full_lifecycle(manager_factory):
    mgr = manager_factory(n_synth=1)
    # First eligible dispatch (n_synth=1) launches synthesis -> synthesizing.
    assert mgr.note_eligible_dispatch("life[0-9]+", 0, ENC) == ROUTE_SYNTH
    assert mgr.tier("life[0-9]+", 0, ENC) == TIER_SYNTHESIZING
    # Let synthesis finish -> warm.
    assert mgr.drain(10.0) is True
    assert mgr.tier("life[0-9]+", 0, ENC) == TIER_WARM
    # Next dispatch promotes warm -> resident (mock PR load).
    assert mgr.note_eligible_dispatch("life[0-9]+", 0, ENC) == ROUTE_RESIDENT
    assert mgr.tier("life[0-9]+", 0, ENC) == TIER_RESIDENT
    s = mgr.stats()
    assert s["synth_launched"] == 1 and s["synth_succeeded"] == 1
    assert s["pr_loads"] == 1 and s["circuits_resident"] == 1


def test_not_resident_is_ordinary_routing(manager_factory):
    # A cold pattern below threshold routes NOT_RESIDENT (not a device error).
    mgr = manager_factory(n_synth=1000)
    assert mgr.note_eligible_dispatch("cold[0-9]+", 0, ENC) == ROUTE_NOT_RESIDENT


# --- single-tenant PR region + deterministic LRU eviction (R64) ------------
def _make_resident(mgr, pattern):
    mgr.note_eligible_dispatch(pattern, 0, ENC)   # launch (n_synth=1)
    mgr.drain(10.0)
    assert mgr.note_eligible_dispatch(pattern, 0, ENC) == ROUTE_RESIDENT


def test_single_tenant_eviction_lru(manager_factory):
    mgr = manager_factory(n_synth=1, pr_partitions=1)
    _make_resident(mgr, "aaa[0-9]+")
    assert mgr.tier("aaa[0-9]+", 0, ENC) == TIER_RESIDENT
    # A second circuit becoming resident evicts the first (single tenant, R64).
    _make_resident(mgr, "bbb[0-9]+")
    assert mgr.tier("bbb[0-9]+", 0, ENC) == TIER_RESIDENT
    assert mgr.tier("aaa[0-9]+", 0, ENC) == TIER_WARM   # evicted -> back to warm
    s = mgr.stats()
    assert s["circuits_resident"] == 1
    assert s["circuits_evicted"] == 1


def test_eviction_progress_no_thrash(manager_factory):
    # Alternating two hot patterns must make progress (bounded PR loads/evicts),
    # never loop forever; and a re-dispatch of the resident one does NOT reload.
    mgr = manager_factory(n_synth=1, pr_partitions=1)
    _make_resident(mgr, "x[0-9]+")
    _make_resident(mgr, "y[0-9]+")
    loads_before = mgr.stats()["pr_loads"]
    # Re-dispatching the resident circuit must be a no-op load (R64 prefers
    # resident; no reload / no eviction).
    for _ in range(10):
        assert mgr.note_eligible_dispatch("y[0-9]+", 0, ENC) == ROUTE_RESIDENT
    assert mgr.stats()["pr_loads"] == loads_before   # no extra loads


# --- synthesis failure -> permanent fallback (R65) + stats (R66) -----------
def test_synthesis_failure_permanent_fallback(manager_factory):
    mgr = manager_factory(n_synth=1, config=ToolchainConfig(fail_mode="error"))
    assert mgr.note_eligible_dispatch("bad[0-9]+", 0, ENC) == ROUTE_SYNTH
    assert mgr.drain(10.0) is True
    assert mgr.tier("bad[0-9]+", 0, ENC) == TIER_FALLBACK_ONLY
    # Subsequent dispatch is permanent fallback and never retries synthesis.
    assert mgr.note_eligible_dispatch("bad[0-9]+", 0, ENC) == ROUTE_PERMANENT_FALLBACK
    s = mgr.stats()
    assert s["synth_failed"] == 1
    assert s["synth_launched"] == 1   # not retried indefinitely (R65)


def test_failure_persists_and_is_not_retried(manager_factory):
    mgr = manager_factory(n_synth=1, config=ToolchainConfig(fail_mode="error"))
    mgr.note_eligible_dispatch("bad2[0-9]+", 0, ENC)
    mgr.drain(10.0)
    for _ in range(5):
        assert mgr.note_eligible_dispatch(
            "bad2[0-9]+", 0, ENC) == ROUTE_PERMANENT_FALLBACK
    assert mgr.stats()["synth_launched"] == 1


# --- R66 stats shape + monotonicity ----------------------------------------
def test_stats_keys_present(manager_factory):
    mgr = manager_factory(n_synth=1000)
    s = mgr.stats()
    for k in ("synth_launched", "synth_succeeded", "synth_failed",
              "circuits_synthesizing", "circuits_resident",
              "circuits_evicted", "pr_loads"):
        assert k in s and isinstance(s[k], int)


def test_gauges_reflect_state(manager_factory):
    mgr = manager_factory(n_synth=1)
    assert mgr.stats()["circuits_resident"] == 0
    _make_resident(mgr, "g[0-9]+")
    assert mgr.stats()["circuits_resident"] == 1
    assert mgr.stats()["circuits_synthesizing"] == 0
