"""Deadline tenants: work with a hard latency budget, not a best-effort value.

Every tenant so far is **best-effort**: missing it costs coverage, and SR5
guarantees correctness regardless.  A deadline tenant is different in kind —
its work must complete within a bounded time or the result is worthless (or
the packet is dropped).  Mixing the two on one region is where scheduling
theory stops being an analogy and starts constraining the design.

The sharp result, which falls straight out of the measured switch cost
---------------------------------------------------------------------
A deadline tenant can tolerate being absent for at most about its deadline.
Packet-scale deadlines are **microseconds**.  The measured PR context switch
is **13.6 s** — six to seven orders of magnitude larger.  Therefore:

    **Under partial reconfiguration, a deadline tenant is not preemptible.**

Once admitted it must stay resident forever, because evicting it misses
every deadline for 13.6 s.  That converts the region from *schedulable* to
*statically partitioned*: the deadline tenant pins its area permanently and
the best-effort tenants timeshare whatever is left.  It is the strongest
statement yet that PR is not a scheduling mechanism — and the clearest
functional (not merely economic) argument for overlays, whose sub-millisecond
switch could sit inside a packet-scale budget.

:meth:`DeadlineTenant.preemptible_under` is that test, and it is the one
place in this codebase where a scheduling decision is forced by physics
rather than by policy.

Honesty about the workload
--------------------------
The *parameters* here are measured on this system (engine byte rates from
silicon fmax, line rates, the R78 round trip).  The *inline* scenario — a
bump-in-the-wire filter with a per-packet deadline — does **not** run on
this shell: the CMAC datapath is tied off (SF3) and no line-rate path
exists (Risk 2).  It is modelled, clearly labelled as such, because its
schedulability arithmetic is exactly what decides whether building that
path is worthwhile.  The ``host-path`` scenario, by contrast, is real and
its latency was measured.
"""

from __future__ import annotations

from typing import Iterable, List, NamedTuple, Optional, Sequence, Tuple

from .tenants import Tenant, TenantDemand

# -- measured on this system (docs/studies/fpga-scheduler-tenants.md) -------
#: Engine throughput, byte/s, from silicon fmax x datapath width.
ENGINE_BPS = {1: 250.44e6, 8: 8 * 260.8e6}
#: Measured PR context switch, seconds.
PR_SWITCH_S = 13.6
#: Measured R78 MATCH round trip, seconds (control path, 2026-07-29 demo).
R78_RTT_S = 0.0438


class Schedulability(NamedTuple):
    ok: bool
    utilization: float
    bound: float
    reason: str


class DeadlineTenant:
    """A tenant whose work carries a hard relative deadline.

    Real-time parameters follow the standard periodic model: work arrives
    every ``period_s``, takes at most ``wcet_s`` to serve, and must finish
    within ``deadline_s`` of arrival.
    """

    __slots__ = ("inner", "period_s", "wcet_s", "deadline_s", "notes")

    def __init__(self, inner: Tenant, period_s: float, wcet_s: float,
                 deadline_s: Optional[float] = None, notes: str = ""):
        if period_s <= 0 or wcet_s <= 0:
            raise ValueError("period and wcet must be positive")
        self.inner = inner
        self.period_s = float(period_s)
        self.wcet_s = float(wcet_s)
        self.deadline_s = float(deadline_s if deadline_s is not None
                                else period_s)
        self.notes = notes

    # -- identity / shape --------------------------------------------------
    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def kind(self) -> str:
        return "deadline:" + self.inner.kind

    @property
    def input_view(self) -> str:
        return self.inner.input_view

    def footprint(self):
        return self.inner.footprint()

    # -- real-time analysis ------------------------------------------------
    def utilization(self) -> float:
        """Fraction of the engine this tenant needs just to keep up."""
        return self.wcet_s / self.period_s

    def max_absence_s(self) -> float:
        """Longest the tenant may be non-resident before a deadline is
        missed: the slack between deadline and service time."""
        return max(0.0, self.deadline_s - self.wcet_s)

    def preemptible_under(self, switch_cost_s: float = PR_SWITCH_S) -> bool:
        """Can the scheduler evict and restore this tenant at all?

        A switch costs ``switch_cost_s`` *twice* in the worst case (out and
        back), so eviction is only viable if the tenant can be absent that
        long.  At PR cost this is False for anything packet-scale — which
        is the whole point.
        """
        return self.max_absence_s() >= 2 * switch_cost_s

    def schedulable_with(self, others: Sequence["DeadlineTenant"] = (),
                         ) -> Schedulability:
        """EDF feasibility on a single engine: total utilization ≤ 1.

        EDF rather than rate-monotonic because the region serves one tenant
        stream at a time and EDF is optimal there; the RM bound is reported
        in the reason when it would differ.
        """
        group: List[DeadlineTenant] = [self] + list(others)
        u = sum(t.utilization() for t in group)
        n = len(group)
        rm_bound = n * (2 ** (1.0 / n) - 1) if n else 1.0
        ok = u <= 1.0
        if ok:
            reason = ("EDF-feasible: U=%.3f <= 1 (RM bound for n=%d is %.3f%s)"
                      % (u, n, rm_bound,
                         "; RM would also admit" if u <= rm_bound
                         else "; RM would REJECT"))
        else:
            reason = ("infeasible: U=%.3f > 1 — the engine cannot keep up, "
                      "no policy fixes this" % u)
        return Schedulability(ok, u, 1.0, reason)

    def describe(self) -> dict:
        d = dict(self.inner.describe())
        d.update({
            "kind": self.kind,
            "period_us": self.period_s * 1e6,
            "wcet_us": self.wcet_s * 1e6,
            "deadline_us": self.deadline_s * 1e6,
            "utilization": self.utilization(),
            "max_absence_us": self.max_absence_s() * 1e6,
            "preemptible_under_pr": self.preemptible_under(),
            "notes": self.notes or d.get("notes", ""),
        })
        return d

    def __repr__(self) -> str:
        return ("<DeadlineTenant %s U=%.3f deadline=%.1fus preemptible=%s>"
                % (self.name, self.utilization(), self.deadline_s * 1e6,
                   self.preemptible_under()))


# --------------------------------------------------------------------------
# Concrete scenarios, from measured parameters
# --------------------------------------------------------------------------
def inline_filter_tenant(inner: Tenant, line_rate_bps: float = 1e9,
                         frame_bytes: int = 1500,
                         datapath_bytes: int = 1) -> DeadlineTenant:
    """Bump-in-the-wire filtering: every frame classified before the next.

    **Modelled, not running:** the CMAC datapath is tied off on this shell
    (SF3) so no line-rate path exists (Risk 2).  The arithmetic is real —
    engine rate from silicon fmax — and it is exactly what decides whether
    building that path is worthwhile.
    """
    pps = line_rate_bps / (frame_bytes * 8)
    period = 1.0 / pps
    wcet = frame_bytes / ENGINE_BPS[datapath_bytes]
    return DeadlineTenant(
        inner, period_s=period, wcet_s=wcet, deadline_s=period,
        notes="MODELLED (CMAC tied off, SF3): %.0f pps at %d B, engine "
              "dpb=%d" % (pps, frame_bytes, datapath_bytes))


def host_path_tenant(inner: Tenant, requests_per_s: float = 10.0,
                     rtt_s: float = R78_RTT_S,
                     deadline_s: Optional[float] = None) -> DeadlineTenant:
    """R78 control-path MATCH with a per-request budget.

    Real and measured: the 43.8 ms round trip was observed on silicon.
    The deadline defaults to the **period** (the standard implicit-deadline
    periodic model), not to the round trip itself — setting deadline equal
    to service time would make slack zero by construction and report a
    non-preemptibility result that was assumed rather than measured.
    """
    period = 1.0 / requests_per_s
    return DeadlineTenant(
        inner, period_s=period, wcet_s=rtt_s,
        deadline_s=deadline_s if deadline_s is not None else period,
        notes="MEASURED R78 round trip %.1f ms at %.0f req/s "
              "(implicit deadline = period)" % (rtt_s * 1e3, requests_per_s))


def partition_report(deadline_tenants: Sequence[DeadlineTenant],
                     area_luts: int = 80000,
                     switch_cost_s: float = PR_SWITCH_S) -> dict:
    """How much of the region a set of deadline tenants permanently pins.

    Any tenant that cannot be preempted under ``switch_cost_s`` must stay
    resident for the lifetime of the configuration, so its area leaves the
    schedulable pool entirely.  This is the quantity that turns a
    scheduling problem into a partitioning problem.
    """
    pinned = [t for t in deadline_tenants
              if not t.preemptible_under(switch_cost_s)]
    pinned_luts = sum(t.footprint()["est_luts"] for t in pinned)
    return {
        "switch_cost_s": switch_cost_s,
        "n_deadline": len(deadline_tenants),
        "n_pinned": len(pinned),
        "pinned": [t.name for t in pinned],
        "pinned_luts": pinned_luts,
        "schedulable_luts": max(0, area_luts - pinned_luts),
        "schedulable_frac": max(0.0, (area_luts - pinned_luts) / area_luts),
    }
