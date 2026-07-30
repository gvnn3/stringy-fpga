"""Deadline tenants (real-time admission) and the same-kind control set."""

import pytest

from pyro.sched import deadline as DL
from pyro.sched import tenants as TN


@pytest.fixture(scope="module")
def hdr():
    import os
    corpus = os.path.join(os.path.dirname(__file__), "..", "..",
                          "third_party", "snort3-community-rules",
                          "snort3-community.rules")
    if not os.path.exists(corpus):
        pytest.skip("community ruleset not vendored")
    return TN.header_match_tenant()


# --------------------------------------------------------------------------
# Real-time arithmetic
# --------------------------------------------------------------------------
def test_utilization_and_slack(hdr):
    t = DL.DeadlineTenant(hdr, period_s=10e-6, wcet_s=4e-6)
    assert t.utilization() == pytest.approx(0.4)
    # implicit deadline == period, so slack is period - wcet
    assert t.max_absence_s() == pytest.approx(6e-6)


def test_rejects_nonsense_parameters(hdr):
    with pytest.raises(ValueError):
        DL.DeadlineTenant(hdr, period_s=0, wcet_s=1e-6)
    with pytest.raises(ValueError):
        DL.DeadlineTenant(hdr, period_s=1e-6, wcet_s=-1)


def test_engine_rate_decides_line_rate_feasibility(hdr):
    """10 GbE is infeasible on the 1 B/cycle engine and feasible on the
    8 B/cycle one — an admission result that follows from measured fmax,
    not from a policy choice."""
    slow = DL.inline_filter_tenant(hdr, 10e9, 1500, datapath_bytes=1)
    fast = DL.inline_filter_tenant(hdr, 10e9, 1500, datapath_bytes=8)
    assert slow.utilization() > 1.0
    assert not slow.schedulable_with().ok
    assert "engine cannot keep up" in slow.schedulable_with().reason
    assert fast.utilization() < 1.0
    assert fast.schedulable_with().ok


def test_one_gig_is_feasible_on_the_narrow_engine(hdr):
    t = DL.inline_filter_tenant(hdr, 1e9, 1500, datapath_bytes=1)
    assert 0.4 < t.utilization() < 0.6
    assert t.schedulable_with().ok


def test_edf_admission_sums_utilization(hdr):
    a = DL.DeadlineTenant(hdr, period_s=10e-6, wcet_s=4e-6)
    b = DL.DeadlineTenant(hdr, period_s=10e-6, wcet_s=5e-6)
    c = DL.DeadlineTenant(hdr, period_s=10e-6, wcet_s=3e-6)
    assert a.schedulable_with([b]).ok                 # 0.9
    assert not a.schedulable_with([b, c]).ok          # 1.2
    assert a.schedulable_with([b]).utilization == pytest.approx(0.9)


# --------------------------------------------------------------------------
# The sharp result
# --------------------------------------------------------------------------
def test_packet_scale_deadlines_are_not_preemptible_under_pr(hdr):
    """Microsecond slack against a 13.6 s switch: eviction is impossible,
    so the tenant is pinned and the region is partitioned, not scheduled."""
    for t in (DL.inline_filter_tenant(hdr, 1e9),
              DL.inline_filter_tenant(hdr, 10e9, datapath_bytes=8),
              DL.host_path_tenant(hdr)):
        assert t.max_absence_s() < 2 * DL.PR_SWITCH_S
        assert not t.preemptible_under()
        assert not t.preemptible_under(DL.PR_SWITCH_S)


def test_a_slow_enough_deadline_would_be_preemptible(hdr):
    """The predicate is about the numbers, not about deadline tenants as a
    class — a minute-scale budget survives a 13.6 s switch."""
    t = DL.DeadlineTenant(hdr, period_s=120.0, wcet_s=1.0)
    assert t.preemptible_under()


def test_host_path_slack_is_measured_not_zero_by_construction(hdr):
    """deadline defaults to the period, not to the service time; the latter
    would force slack to zero and assume the conclusion."""
    t = DL.host_path_tenant(hdr, requests_per_s=10.0)
    assert t.max_absence_s() == pytest.approx(0.1 - DL.R78_RTT_S)
    assert t.max_absence_s() > 0


def test_partition_report_removes_pinned_area_from_the_pool(hdr):
    pinned = DL.inline_filter_tenant(hdr, 1e9)
    rep = DL.partition_report([pinned], area_luts=80000)
    assert rep["n_pinned"] == 1
    assert rep["pinned_luts"] == hdr.footprint()["est_luts"]
    assert rep["schedulable_luts"] == 80000 - rep["pinned_luts"]
    # a preemptible deadline tenant does not pin anything
    loose = DL.DeadlineTenant(hdr, period_s=120.0, wcet_s=1.0)
    assert DL.partition_report([loose])["n_pinned"] == 0


# --------------------------------------------------------------------------
# The control condition
# --------------------------------------------------------------------------
def test_same_kind_control_is_actually_homogeneous():
    ctrl = TN.same_kind_control(3)
    h = TN.homogeneity(ctrl)
    assert h["n"] == 3
    assert h["kinds"] == ["pattern-set"]
    assert h["input_views"] == ["l4-payload"]
    assert h["footprint_spread"] <= 1.5
    assert h["is_control"] is True


def test_mixed_workload_is_measurably_not_a_control():
    h = TN.homogeneity(TN.build_all_tenants(include_snortpf=1))
    assert len(h["kinds"]) > 1
    assert len(h["input_views"]) > 1
    assert h["footprint_spread"] > 10
    assert h["is_control"] is False


def test_control_picks_the_tightest_footprint_window():
    ctrl = TN.same_kind_control(2)
    luts = sorted(t.footprint()["est_luts"] for t in ctrl)
    pool = sorted(t.footprint()["est_luts"] for t in TN.snortpf_tenants())
    best = min(pool[i + 1] - pool[i] for i in range(len(pool) - 1))
    assert luts[1] - luts[0] == best


def test_control_refuses_kinds_without_enough_members():
    with pytest.raises(ValueError):
        TN.same_kind_control(2, kind="ip-match")
    with pytest.raises(ValueError):
        TN.same_kind_control(9999)
