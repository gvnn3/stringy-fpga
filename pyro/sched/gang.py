"""Gang-scheduled tenants: functions that must be co-resident to be useful.

A **gang** is a set of tenants whose value is *superadditive* — the group
delivers something no member delivers alone — so scheduling only part of it
wastes the area spent on the members you did schedule.  That is the classic
coscheduling condition (Ousterhout 1982), and the FPGA instance of it is a
**pipeline**: ``header-match -> snortpf/<group> -> host``.

Why this pipeline is a real gang and not merely an optimisation
---------------------------------------------------------------
A Snort rule is ``header_predicate AND content_predicate``.  Today the
fabric evaluates only the content half; the host evaluates the header half
and *discards* every nomination whose header predicate fails.  With both
stages resident the conjunction is computable on-chip, so those doomed
nominations are never emitted.  Neither stage alone can do that:

* ``snortpf`` alone — correct, but pays full host re-verification (today).
* ``header-match`` alone — headers match constantly, so as a *prefilter*
  it nominates almost everything; its pipeline contribution is zero.  Its
  area is spent and buys nothing until its partner lands.

That asymmetry is the gang property, and it is what makes partial
residency worse than useless rather than merely partial.

The measured shape of the prize (2026-07-30, real corpus)
---------------------------------------------------------
"Waste" = the fraction of a group's rules whose header predicate cannot
hold for a given flow, i.e. nominations the host discards:

=================  ==========  ==========  ==========
group              port 80     port 22     port 1521
=================  ==========  ==========  ==========
``$HTTP_PORTS/0``  0%          100%        100%
``literal/0``      100%        100%        100%
``any/0``          0%          0%          0%
=================  ==========  ==========  ==========

Read that carefully, because it is the non-obvious result: **the gang's
marginal value is highest exactly when its partner is least valuable.**
For ``any/0`` — the highest-coverage group, and the one a good scheduler
picks — the header stage buys *nothing*.  The pipeline is a mitigation for
mismatched residency, not a general win.  A scheduler that gangs
unconditionally would spend 7,898 LUTs to speed up a configuration it
should have fixed by swapping instead.

Input-view compatibility
------------------------
Gang members must agree on what bytes they are fed.  ``header-match``
needs the frame from byte 0 (its offsets are only meaningful there);
``snortpf`` needs reassembled L4 payload.  Those are compatible because
frame ⊇ payload, and :func:`view_meet` encodes that lattice — a gang
mixing ``host`` buffers with ``frame`` bytes is infeasible and says so
rather than silently mis-feeding a matcher.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .tenants import Tenant, TenantDemand

#: Input-view lattice: a view can satisfy anything it contains.
#: "frame" is the whole Ethernet frame; "l4-payload" is derivable from it;
#: "host" is host-side buffers and shares nothing with packet views.
_VIEW_CONTAINS = {
    "frame": {"frame", "l4-payload"},
    "l4-payload": {"l4-payload"},
    "host": {"host"},
}


def view_meet(views: Iterable[str]) -> Optional[str]:
    """The **narrowest** view satisfying every member, or None if infeasible.

    Narrowest, not merely *a* satisfying view: a gang that only needs
    reassembled payload must not be recorded as needing whole frames, or
    the scheduler inherits a constraint the tenants never asked for.
    """
    need = set(views)
    best = None
    for candidate, covered in _VIEW_CONTAINS.items():
        if need <= covered and (best is None or
                                len(covered) < len(_VIEW_CONTAINS[best])):
            best = candidate
    return best


class GangTenant:
    """A set of tenants that must be co-resident to deliver their value."""

    __slots__ = ("name", "members", "stages", "notes", "_pair_value_fn")

    def __init__(self, name: str, members: Sequence[Tenant],
                 pair_value_fn, stages: Optional[Sequence[str]] = None,
                 notes: str = ""):
        if len(members) < 2:
            raise ValueError("a gang needs at least two members")
        view = view_meet(m.input_view for m in members)
        if view is None:
            raise ValueError(
                "infeasible gang: input views %r have no common view — a "
                "fixed-offset matcher fed a payload buffer is silently wrong"
                % sorted({m.input_view for m in members}))
        self.name = name
        self.members = tuple(members)
        self.stages = tuple(stages or [m.name for m in members])
        self.notes = notes
        self._pair_value_fn = pair_value_fn

    # -- shape -------------------------------------------------------------
    @property
    def input_view(self) -> str:
        return view_meet(m.input_view for m in self.members)

    @property
    def member_names(self) -> Tuple[str, ...]:
        return tuple(m.name for m in self.members)

    def footprint(self) -> Dict[str, int]:
        """Summed — a gang is admissible only if the WHOLE thing fits."""
        tot = {"slots": 0, "states": 0, "byte_edges": 0, "est_luts": 0}
        for m in self.members:
            f = m.footprint()
            for k in tot:
                tot[k] += f[k]
        return tot

    def admissible(self, area_luts: int) -> bool:
        return self.footprint()["est_luts"] <= area_luts

    # -- value -------------------------------------------------------------
    def satisfied_by(self, resident: Iterable[str]) -> bool:
        return set(self.member_names) <= set(resident)

    def gang_value(self, demand: TenantDemand) -> float:
        """The superadditive part: what ONLY co-residency delivers.

        Zero unless every member is resident — that is the definition, not
        a modelling convenience.
        """
        return float(self._pair_value_fn(demand))

    def value(self, demand: TenantDemand,
              resident: Optional[Iterable[str]] = None) -> float:
        """Total delivered value given who is actually resident.

        With everyone in: standalone values plus the gang bonus.  With a
        subset: the resident members' standalone values only — the bonus is
        forfeited, which is the cost partial gang scheduling imposes.
        """
        names = set(self.member_names if resident is None else resident)
        standalone = sum(m.value(demand) for m in self.members
                         if m.name in names)
        if self.satisfied_by(names):
            return standalone + self.gang_value(demand)
        return standalone

    def wasted_luts(self, resident: Iterable[str]) -> int:
        """Area held by resident members while the gang is UNSATISFIED.

        The quantity that makes gang scheduling a scheduling problem: it is
        spent, and it buys none of the gang's value.  Zero when the gang is
        whole or entirely absent.
        """
        names = set(resident)
        if self.satisfied_by(names) or not (names & set(self.member_names)):
            return 0
        return sum(m.footprint()["est_luts"] for m in self.members
                   if m.name in names)

    def describe(self) -> dict:
        d = {"name": self.name, "kind": "gang", "stages": list(self.stages),
             "members": list(self.member_names),
             "input_view": self.input_view, "notes": self.notes}
        d.update(self.footprint())
        return d

    def __repr__(self) -> str:
        return "<GangTenant %s members=%d luts=%d>" % (
            self.name, len(self.members), self.footprint()["est_luts"])


# --------------------------------------------------------------------------
# The concrete pipeline
# --------------------------------------------------------------------------
def header_prefilter_pipeline(header_tenant: Tenant, group_tenant: Tenant,
                              group=None) -> GangTenant:
    """``header-match -> snortpf/<group> -> host``.

    The gang bonus is **host re-verifications avoided**: with both stages
    resident, a nomination whose rule header predicate provably cannot hold
    is suppressed on-chip instead of being emitted and discarded by the
    CPU.  Measured directly from the group's own rules against the observed
    port mix — no modelling constant.

    Note the units: this bonus is denominated in *nominations the host does
    not have to re-verify*, which is a different currency from the
    members' own value (coverage, packets).  A scheduler mixing them needs
    an explicit exchange rate; that incomparability is real and is left
    visible rather than papered over.
    """
    from ..snort import daemon as _daemon

    vt = _daemon.VarTable()
    toks: List[str] = []
    if group is not None:
        toks = [r.dst_port for s in group.slots for r in s.rules]
    else:
        # Recover the per-rule port tokens from the tenant's slot labels is
        # not possible (labels are gid:sid), so a group must be supplied
        # for a measured bonus; without it the gang is declared but scores
        # zero rather than guessing.
        toks = []

    def _bonus(d: TenantDemand) -> float:
        mix = d.port_mix or {}
        if not mix or not toks:
            return 0.0
        total = 0.0
        for port, nbytes in mix.items():
            doomed = sum(1 for t in toks if not vt.port_holds(t, port))
            total += nbytes * doomed
        return total

    return GangTenant(
        name="pipeline/%s" % group_tenant.name.replace("snortpf/", ""),
        members=[header_tenant, group_tenant],
        pair_value_fn=_bonus,
        stages=["header-match", group_tenant.name, "host"],
        notes="conjunction of header AND content computed on-chip; bonus is "
              "host re-verifications avoided (measured per port mix)")


def build_pipeline(group_name: str = "literal/0") -> GangTenant:
    """Convenience: the pipeline over one named SR6 group."""
    import os

    from ..snort import groups as _groups
    from ..snort import triage as _triage
    from .tenants import header_match_tenant, snortpf_tenants

    corpus = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))),
        "third_party", "snort3-community-rules", "snort3-community.rules")
    groups = _groups.pack_groups(_triage.triage_file(corpus))
    g = next(x for x in groups if x.name == group_name)
    gt = next(t for t in snortpf_tenants(groups)
              if t.name == "snortpf/%s" % group_name)
    return header_prefilter_pipeline(header_match_tenant(), gt, group=g)
