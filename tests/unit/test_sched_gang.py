"""Gang-scheduled pipeline tenant: superadditivity, partial-residency waste,
input-view feasibility, and the measured shape of the pipeline's prize.
"""

import pytest

from pyro.sched import gang as GA
from pyro.sched import tenants as TN


@pytest.fixture(scope="module")
def pipe():
    import os
    corpus = os.path.join(os.path.dirname(__file__), "..", "..",
                          "third_party", "snort3-community-rules",
                          "snort3-community.rules")
    if not os.path.exists(corpus):
        pytest.skip("community ruleset not vendored")
    return GA.build_pipeline("literal/0")


HTTP = TN.TenantDemand(packets=1e4, port_mix={80: 1e6})
MISMATCH = TN.TenantDemand(packets=1e4, port_mix={22: 1e6, 1521: 5e5})


# --------------------------------------------------------------------------
# The input-view lattice
# --------------------------------------------------------------------------
def test_view_meet_lattice():
    assert GA.view_meet(["frame", "l4-payload"]) == "frame"
    assert GA.view_meet(["l4-payload"]) == "l4-payload"
    assert GA.view_meet(["frame"]) == "frame"
    assert GA.view_meet(["host"]) == "host"
    # host buffers and packet bytes share nothing
    assert GA.view_meet(["host", "frame"]) is None


def test_infeasible_gang_is_refused_not_silently_mis_fed():
    """A fixed-offset matcher handed a payload buffer is silently wrong, so
    an incompatible gang must fail loudly at construction."""
    with pytest.raises(ValueError, match="infeasible gang"):
        GA.GangTenant("bad",
                      [TN.pyro_regex_tenant(), TN.ip_match_tenant()],
                      lambda d: 0.0)


def test_gang_needs_at_least_two_members():
    with pytest.raises(ValueError):
        GA.GangTenant("solo", [TN.ip_match_tenant()], lambda d: 0.0)


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------
def test_pipeline_shape(pipe):
    assert pipe.member_names == ("header-match", "snortpf/literal/0")
    assert pipe.stages == ("header-match", "snortpf/literal/0", "host")
    assert pipe.input_view == "frame"       # superset of l4-payload
    f = pipe.footprint()
    hm = TN.header_match_tenant().footprint()["est_luts"]
    assert f["est_luts"] > hm               # summed, not max
    assert pipe.admissible(f["est_luts"])
    assert not pipe.admissible(f["est_luts"] - 1)


# --------------------------------------------------------------------------
# Superadditivity — the defining property
# --------------------------------------------------------------------------
def test_gang_bonus_requires_every_member(pipe):
    whole = pipe.value(MISMATCH)
    members_only = sum(m.value(MISMATCH) for m in pipe.members)
    assert whole > members_only             # superadditive
    assert whole == pytest.approx(members_only + pipe.gang_value(MISMATCH))
    # any strict subset forfeits the entire bonus
    for subset in (["header-match"], ["snortpf/literal/0"]):
        assert pipe.value(MISMATCH, resident=subset) < whole
        assert not pipe.satisfied_by(subset)
    assert pipe.satisfied_by(pipe.member_names)


def test_partial_residency_wastes_area(pipe):
    """The quantity that makes this a scheduling problem: area held while
    the gang is unsatisfied buys none of the gang's value."""
    hm_luts = TN.header_match_tenant().footprint()["est_luts"]
    assert pipe.wasted_luts(["header-match"]) == hm_luts
    assert pipe.wasted_luts([]) == 0                     # nothing held
    assert pipe.wasted_luts(pipe.member_names) == 0      # gang is whole


# --------------------------------------------------------------------------
# The measured, non-obvious result
# --------------------------------------------------------------------------
def test_gang_buys_nothing_for_a_universally_relevant_group():
    """`any/0`'s rules fire on every port, so NO nomination is header-doomed
    and the header stage's marginal value is exactly zero — the pipeline is
    a mitigation for mismatched residency, not a general win."""
    p = GA.build_pipeline("any/0")
    assert p.gang_value(HTTP) == 0.0
    assert p.gang_value(MISMATCH) == 0.0
    # ...and the area would still be spent
    assert p.footprint()["est_luts"] > \
        next(m for m in p.members if m.kind == "pattern-set") \
        .footprint()["est_luts"]


def test_gang_value_is_largest_when_the_partner_is_least_useful():
    """$HTTP_PORTS/0 on SSH/Oracle traffic: the group alone nominates
    nothing useful, and every nomination it would make is header-doomed."""
    p = GA.build_pipeline("$HTTP_PORTS/0")
    group = next(m for m in p.members if m.kind == "pattern-set")
    assert group.value(MISMATCH) == 0.0          # partner useless here
    assert p.gang_value(MISMATCH) > 0            # gang bonus large
    # on matched traffic the partner is fully useful and the bonus is small
    assert p.gang_value(HTTP) < 0.05 * group.value(HTTP)


def test_gang_value_is_zero_without_a_port_mix(pipe):
    assert pipe.gang_value(TN.TenantDemand()) == 0.0


def test_pipeline_without_a_group_scores_zero_rather_than_guessing():
    """If the per-rule port tokens are not supplied the bonus is not
    estimable; it must report zero, not invent a number."""
    gt = TN.snortpf_tenants()[0]
    p = GA.header_prefilter_pipeline(TN.header_match_tenant(), gt, group=None)
    assert p.gang_value(MISMATCH) == 0.0
