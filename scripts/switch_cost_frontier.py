#!/usr/bin/env python3
"""Where does scheduling stop paying? The switch-cost frontier.

The policy experiment showed value-aware scheduling beating a static pin
by ~3x at the measured 13.6 s partial-reconfiguration cost.  That gain
survives only because the demand phases in that experiment are minutes
long.  This sweep finds the boundary: **as switch cost rises relative to
phase length, at what point does scheduling stop being worth doing?**

The hypothesis worth testing is that the frontier is a *ratio*, not a pair
of numbers — that only ``switch_cost / phase_length`` matters, so the
result is one critical constant rather than a table.  The sweep checks
that by running several absolute phase lengths and seeing whether their
curves collapse onto each other.

Why it matters: it converts "overlays would help" into a requirement.  If
scheduling needs ``s/P < r*`` and you want to serve phases of length P,
then the switch mechanism must deliver ``s < r* * P`` — a number an
implementation can be held to.

Usage:
    .venv-pyro/bin/python3 scripts/switch_cost_frontier.py
"""

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import policy_experiment as PE               # noqa: E402
from pyro.sched import deadline as DL        # noqa: E402
from pyro.sched import tenants as TN         # noqa: E402

TICKS_PER_PHASE = 24        # resolution; phase length is set by tick size
GAIN_THRESHOLD = 20.0       # % gain over static pin we call "worth doing"


def run_point(tenants, budget, phase_s, switch_s, n_phases=6):
    """Gain of the best policy over a static pin at one (P, s) point."""
    tick = phase_s / TICKS_PER_PHASE
    trace = PE.phase_trace(tenants, n_phases=n_phases,
                           phase_ticks=TICKS_PER_PHASE)
    base = PE.simulate(tenants, trace, PE.pol_static_pin, budget,
                       switch_s, tick_s=tick)["capture_mean"]
    best_name, best = None, 0.0
    for name, fn in PE.POLICIES.items():
        if name == "static-pin":
            continue
        r = PE.simulate(tenants, trace, fn, budget, switch_s, tick_s=tick)
        if r["capture_mean"] > best:
            best, best_name = r["capture_mean"], name
    gain = (100 * (best / base - 1)) if base > 0 else float("nan")
    return {"phase_s": phase_s, "switch_s": switch_s, "ratio": switch_s / phase_s,
            "static": 100 * base, "best": 100 * best, "policy": best_name,
            "gain_pct": gain}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    tenants = TN.same_kind_control(3)     # the control set: cleanest signal
    want = sum(t.footprint()["est_luts"] for t in tenants)
    budget = int(want * 0.75)
    print("tenant set: control (same-kind), %d tenants, budget %d/%d LUTs\n"
          % (len(tenants), budget, want))

    ratios = [0.0001, 0.001, 0.01, 0.03, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0]
    phases = [30.0, 120.0, 600.0]

    print("=" * 78)
    print("Does only the RATIO matter? (gain %% over static pin)")
    print("=" * 78)
    print("%-10s | %s" % ("s/P ratio",
                          "  ".join("P=%-6gs" % p for p in phases)))
    print("-" * 78)
    rows = []
    for r in ratios:
        cells = []
        for p in phases:
            pt = run_point(tenants, budget, p, r * p)
            rows.append(pt)
            cells.append("%+7.1f%%" % pt["gain_pct"])
        print("%-10s | %s" % (r, "   ".join(cells)))

    # Collapse check: spread of gain across absolute P at fixed ratio.
    print("\nscale invariance: max spread of gain across P at fixed ratio = "
          "%.1f points" % max(
              max(x["gain_pct"] for x in rows if x["ratio"] == r) -
              min(x["gain_pct"] for x in rows if x["ratio"] == r)
              for r in ratios))

    # The frontier: largest ratio still worth scheduling.
    print("\n" + "=" * 78)
    print("THE FRONTIER (gain >= %.0f%% counts as worth scheduling)"
          % GAIN_THRESHOLD)
    print("=" * 78)
    ok = [r for r in ratios
          if min(x["gain_pct"] for x in rows if x["ratio"] == r)
          >= GAIN_THRESHOLD]
    r_star = max(ok) if ok else None
    bad = [r for r in ratios if r not in ok]
    print("  worth scheduling up to  s/P = %s" % (r_star if r_star else "—"))
    print("  not worth it from       s/P = %s"
          % (min(bad) if bad else "—"))

    if r_star:
        print("\n  requirement implied, at the measured PR cost of %.1f s:"
              % DL.PR_SWITCH_S)
        print("  %-22s %-14s %s" % ("phase length", "max switch", "PR verdict"))
        print("  " + "-" * 56)
        for p, label in [(0.001, "1 ms (per-packet)"),
                         (1.0, "1 s"), (60.0, "1 min"),
                         (600.0, "10 min"), (3600.0, "1 hour")]:
            need = r_star * p
            ok_pr = DL.PR_SWITCH_S <= need
            print("  %-22s %-14s %s"
                  % (label, _fmt(need),
                     "PR works" if ok_pr else "PR TOO SLOW (needs %sx faster)"
                     % _ratio(DL.PR_SWITCH_S / need)))

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump({"rows": rows, "r_star": r_star}, fh, indent=2,
                      sort_keys=True)
            fh.write("\n")
        print("\nwrote %s" % args.json_out)
    return 0


def _fmt(s):
    if s >= 1:
        return "%.1f s" % s
    if s >= 1e-3:
        return "%.1f ms" % (s * 1e3)
    return "%.0f us" % (s * 1e6)


def _ratio(x):
    return "%.0f" % x if x < 1000 else "%.0e" % x


if __name__ == "__main__":
    sys.exit(main())
