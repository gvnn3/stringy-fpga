#!/usr/bin/env python3
"""Run scheduling policies across the control and treatment tenant sets.

The experiment the apparatus was built for.  The A5 study found every
policy tied with a static pin, but that result was **ambiguous** between
two very different conclusions:

  (a) scheduling does not help on this machine, or
  (b) that workload could not distinguish schedulers.

A control set discriminates them.  If a policy separates from a static pin
on the *homogeneous* set, the effect is capacity-driven; if it separates
only on the *heterogeneous* set, the effect is heterogeneity-driven; if it
separates on neither, (a) holds.

Controlling the confounds
-------------------------
* **Identical phase structure.**  Both sets are driven by demand that
  rotates which tenant is favoured, with the same period and duty cycle.
  Without this we would be comparing "heterogeneous with dynamics" against
  "homogeneous without", which confounds the variable under test.
* **Budget as a fraction of what the set wants**, not an absolute LUT
  count, so the two sets are compared at equivalent capacity pressure
  despite different absolute footprints.
* **Unit-free metric.**  Treatment tenants value incomparable things (scan
  bytes, packets, rules x bytes).  The headline metric is per-tenant
  *normalised capture* — the fraction of each tenant's own demanded value
  that was actually delivered — averaged over tenants.  That needs no
  exchange rate.  A total-value metric under an explicit exchange rate is
  reported alongside, swept, so the dependence is visible rather than
  buried in a constant.

Usage:
    .venv-pyro/bin/python3 scripts/policy_experiment.py
"""

import argparse
import json
import math
import os
import sys
from collections import OrderedDict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from pyro.sched import deadline as DL          # noqa: E402
from pyro.sched import tenants as TN           # noqa: E402

TICK_S = 5.0            # scheduler tick
PHASE_TICKS = 24        # ticks per demand phase (2 min at 5 s)


# --------------------------------------------------------------------------
# Demand: identical phase STRUCTURE for both sets, different content
# --------------------------------------------------------------------------
def phase_trace(tenants, n_phases=6, seed=7, phase_ticks=None):
    """One demand sample per tick, rotating which tenant is favoured.

    Each phase picks a 'star' tenant and shapes demand so that tenant is
    worth the most.  The rotation order and duty cycle are the same for
    every set, so phase dynamics are held constant across control and
    treatment.
    """
    import random
    rng = random.Random(seed)
    pt = PHASE_TICKS if phase_ticks is None else int(phase_ticks)
    # Randomised star order and jittered phase lengths.  A deterministic
    # rotation with uniform phases lets switch cost align with phase
    # boundaries, which produced strongly non-monotonic (aliased) frontier
    # curves - gain at s/P=3 exceeding s/P=2.  Real phases are not uniform
    # and a frontier must not be an artifact of synchronisation.
    stars, prev = [], None
    for _ in range(n_phases):
        choices = [t for t in tenants if t is not prev] or list(tenants)
        prev = rng.choice(choices)
        stars.append(prev)
    trace = []
    for ph, star in enumerate(stars):
        n = max(1, int(round(pt * rng.uniform(0.7, 1.3))))
        for _ in range(n):
            trace.append((ph, _demand_favouring(star, rng)))
    return trace


def _demand_favouring(star, rng):
    """A TenantDemand shaped so `star` scores highest in its own currency."""
    base = 1e4
    hot, cold = 1e6, 1e3
    scan = hot if star.kind == "regex" else cold
    pkts = hot if star.kind in ("ip-match", "header") else cold
    # For pattern-set tenants, put the bytes on a port their rules match.
    mix = {p: cold for p in (80, 22, 21, 110, 1521)}
    if star.kind == "pattern-set":
        port = {"$HTTP_PORTS": 80, "$SSH_PORTS": 22, "$FTP_PORTS": 21,
                "$FILE_DATA_PORTS": 110, "$ORACLE_PORTS": 1521}.get(
                    star.name.split("/")[1].split("/")[0], 80)
        mix[port] = hot
    else:
        mix[80] = base
    return TN.TenantDemand(scan_bytes=scan, packets=pkts, port_mix=mix)


# --------------------------------------------------------------------------
# Policies.  Each returns the DESIRED resident set for this tick.
# --------------------------------------------------------------------------
def pol_static_pin(tenants, demand, resident, state, budget):
    """Pin the hindsight-best tenant forever — the A5 study's champion."""
    if state.get("pin") is None:
        state["pin"] = state["hindsight_best"]
    pick = state["pin"]
    return {pick} if _fits([pick], tenants, budget) else set()


def pol_static_set(tenants, demand, resident, state, budget):
    """Best FIXED set that fits, chosen in hindsight, never changed.

    The honest baseline for "is scheduling worth it".  `static-pin` holds
    exactly one tenant while every other policy may hold several, so a
    comparison against it conflates *packing more* with *changing what is
    resident over time*.  Measured consequence: against static-pin the
    apparent gain stays above +100% even at switch costs so large that
    nothing ever switches — pure packing, mislabelled as scheduling.
    Anything this baseline is beaten by is genuinely dynamic.
    """
    if state.get("static_set") is None:
        import itertools
        best, best_v = frozenset(), -1.0
        names = [t.name for t in tenants]
        for r in range(1, len(names) + 1):
            for combo in itertools.combinations(names, r):
                if not _fits(combo, tenants, budget):
                    continue
                v = sum(state["totals"][n] / state["peak_total"][n]
                        for n in combo if state["peak_total"][n] > 0)
                if v > best_v:
                    best, best_v = frozenset(combo), v
        state["static_set"] = best
    return set(state["static_set"])


def pol_greedy_value(tenants, demand, resident, state, budget):
    """Pack by normalised value density; the obvious policy."""
    scored = []
    for t in tenants:
        v = _norm_value(t, demand, state)
        luts = max(1, t.footprint()["est_luts"])
        scored.append((v / luts, v, t.name, luts))
    scored.sort(reverse=True)
    want, used = set(), 0
    for _d, v, name, luts in scored:
        if v <= 0:
            continue
        if used + luts <= budget:
            want.add(name)
            used += luts
    return want


def pol_lru(tenants, demand, resident, state, budget):
    """Keep what is valuable now; evict least-recently-valuable."""
    lru = state.setdefault("lru", OrderedDict())
    for t in tenants:
        if _norm_value(t, demand, state) > 0:
            lru[t.name] = state["tick"]
    want, used = set(), 0
    for name in sorted(lru, key=lambda n: -lru[n]):
        luts = _by_name(tenants, name).footprint()["est_luts"]
        if used + luts <= budget:
            want.add(name)
            used += luts
    return want


def pol_round_robin(tenants, demand, resident, state, budget):
    """Fair share: rotate residency on a fixed quantum, ignoring value."""
    q = state.setdefault("rr_quantum", 6)     # ticks
    idx = (state["tick"] // q) % len(tenants)
    want, used = set(), 0
    for k in range(len(tenants)):
        t = tenants[(idx + k) % len(tenants)]
        luts = t.footprint()["est_luts"]
        if used + luts <= budget:
            want.add(t.name)
            used += luts
    return want


def pol_oracle(tenants, demand, resident, state, budget):
    """Everything that fits, chosen with perfect knowledge of this tick."""
    return pol_greedy_value(tenants, demand, resident, state, budget)


POLICIES = OrderedDict([
    ("static-pin", pol_static_pin),
    ("static-set", pol_static_set),
    ("greedy-value", pol_greedy_value),
    ("lru", pol_lru),
    ("round-robin", pol_round_robin),
])


# --------------------------------------------------------------------------
def _by_name(tenants, name):
    for t in tenants:
        if t.name == name:
            return t
    raise KeyError(name)


def _fits(names, tenants, budget):
    return sum(_by_name(tenants, n).footprint()["est_luts"]
               for n in names) <= budget


def _norm_value(t, demand, state):
    """Value normalised by this tenant's own peak, so cross-currency
    comparison inside a policy is explicit and bounded, never a raw
    magnitude comparison between incomparable units."""
    peak = state["peak"].get(t.name, 0.0)
    if peak <= 0:
        return 0.0
    return t.value(demand) / peak


def simulate(tenants, trace, policy_fn, budget, switch_cost_s,
             tick_s=None):
    """Tick the policy; charge switch cost with serialized fills."""
    dt = TICK_S if tick_s is None else float(tick_s)
    state = {"peak": {}, "tick": 0}
    for t in tenants:
        state["peak"][t.name] = max(
            (t.value(d) for _ph, d in trace), default=0.0)
    totals = {t.name: sum(t.value(d) for _ph, d in trace) for t in tenants}
    state["totals"] = totals
    state["peak_total"] = dict(totals)
    state["hindsight_best"] = max(totals, key=lambda n: totals[n])

    resident, pending, busy_until = set(), None, -1.0
    captured = {t.name: 0.0 for t in tenants}
    demanded = {t.name: 0.0 for t in tenants}
    switches = 0

    for i, (_ph, demand) in enumerate(trace):
        now = i * dt
        state["tick"] = i
        if pending is not None and now >= busy_until:
            resident = pending[1]
            pending = None
        # account BEFORE deciding: value is captured only if resident now
        for t in tenants:
            v = t.value(demand)
            demanded[t.name] += v
            if t.name in resident:
                captured[t.name] += v
        if pending is None:
            want = policy_fn(tenants, demand, resident, state, budget)
            if want != resident:
                pending = (now, set(want))
                busy_until = now + switch_cost_s
                switches += 1

    per_tenant = {}
    for name in captured:
        per_tenant[name] = (captured[name] / demanded[name]
                            if demanded[name] > 0 else None)
    live = [v for v in per_tenant.values() if v is not None]
    return {
        "capture_mean": sum(live) / len(live) if live else 0.0,
        "capture_min": min(live) if live else 0.0,
        "switches": switches,
        "per_tenant": per_tenant,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--switch-cost", type=float, default=DL.PR_SWITCH_S)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    sets = [("control (same-kind)", TN.same_kind_control(3)),
            ("treatment (matched)", TN.matched_treatment()),
            ("treatment (skewed)", TN.build_all_tenants(include_snortpf=1))]

    for label, ts in sets:
        h = TN.homogeneity(ts)
        print("%-22s n=%d kinds=%d views=%d LUT-spread=%.2fx is_control=%s"
              % (label, h["n"], len(h["kinds"]), len(h["input_views"]),
                 h["footprint_spread"], h["is_control"]))
    print()

    results = []
    for label, ts in sets:
        trace = phase_trace(ts)
        want_all = sum(t.footprint()["est_luts"] for t in ts)
        print("=" * 96)
        print("%s  — %d tenants, %d LUTs if all resident, %d ticks (%.0f min)"
              % (label, len(ts), want_all, len(trace),
                 len(trace) * TICK_S / 60))
        print("=" * 96)
        big = max(t.footprint()['est_luts'] for t in ts)
        for frac in (0.25, 0.5, 0.75, 0.95):
            budget = int(want_all * frac)
            adm = [t for t in ts if t.footprint()["est_luts"] <= budget]
            warn = ("   [DEGENERATE: largest tenant (%d LUTs) can never load]"
                    % big) if big > budget else ""
            print("\n  budget = %.0f%% of demand (%d LUTs), switch = %.1f s"
                  "  |  %d/%d tenants individually admissible%s"
                  % (frac * 100, budget, args.switch_cost, len(adm), len(ts),
                     warn))
            print("  %-14s %-12s %-12s %-9s %s"
                  % ("policy", "capture mean", "capture min", "switches",
                     "vs static pin"))
            print("  " + "-" * 74)
            base = None
            for pname, pfn in POLICIES.items():
                r = simulate(ts, trace, pfn, budget, args.switch_cost)
                if pname == "static-set":
                    base = r["capture_mean"]
                delta = ("" if base in (None, 0) else
                         "%+.1f%%" % (100 * (r["capture_mean"] / base - 1)))
                if pname == "static-set":
                    delta = "(baseline)"
                elif pname == "static-pin":
                    delta = "(one tenant only)"
                print("  %-14s %11.1f%% %11.1f%% %9d  %s"
                      % (pname, 100 * r["capture_mean"],
                         100 * r["capture_min"], r["switches"], delta))
                results.append({"set": label, "budget_frac": frac,
                                "policy": pname,
                                "degenerate": bool(big > budget),
                                **{k: v for k, v in r.items()
                                   if k != "per_tenant"}})
        print()

    print("=" * 96)
    print("VERDICT")
    print("=" * 96)
    for label, _ts in sets:
        rows = [r for r in results if r["set"] == label]
        base = {r["budget_frac"]: r["capture_mean"] for r in rows
                if r["policy"] == "static-set"}
        best = {}
        for r in rows:
            if r["policy"].startswith("static"):
                continue
            f = r["budget_frac"]
            if r["capture_mean"] > best.get(f, (0, ""))[0]:
                best[f] = (r["capture_mean"], r["policy"])
        degen = {r["budget_frac"] for r in rows if r["degenerate"]}
        gains = [(100 * (best[f][0] / base[f] - 1), f, best[f][1])
                 for f in sorted(best) if base.get(f) and f not in degen]
        top = max(gains) if gains else (0, 0, "-")
        sep = "SEPARATES" if top[0] > 5 else "no separation"
        print("  %-22s best gain over best-fixed-set: %+.1f%% (%s at %.0f%% budget)"
              "  -> %s%s" % (label, top[0], top[2], top[1] * 100, sep,
                             "   [%d degenerate budget(s) excluded]" % len(degen)
                             if degen else ""))

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(results, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print("\nwrote %s" % args.json_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
