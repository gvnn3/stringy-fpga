#!/usr/bin/env python3
"""Characterize the FPGA context-switch cost curve (scheduler apparatus).

A scheduler's most basic parameter is what a context switch costs, and so
far we have one scalar: "~16 s".  That is not enough to design a policy.
What a scheduler needs is the cost as a **function** — of what changes, of
what is already resident, of how much of the region is being replaced —
because that function decides which policies are even expressible (see
`docs/studies/fpga-scheduler-tenants.md`).

This measures the real thing on real hardware, decomposed into phases:

  program   Vivado/hw_server JTAG program of the partial bitstream (R85)
  recover   the mandatory in-band recovery (R85a): user reset 0x014 +
            QDMA soft reset 0x00C + onic reload — a raw JTAG program
            leaves the child unreachable, so this is not optional and
            belongs in the switch cost
  probe     ID_REQUEST/ID_REPLY round trip confirming the new child is
            live and is the one expected (SR14)

and correlates the total against bitstream bytes and design size (slots),
which is the question that decides whether switch cost is a constant or a
function of the thing being loaded.

Usage:
    PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 \\
        scripts/switch_cost_curve.py [--reps 3]
"""

import argparse
import glob
import json
import os
import re
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def group_slots_by_tag():
    """tag -> (slots, rules) for the built SNORT-PF groups."""
    from pyro.snort import groups as G
    from pyro.snort import triage as T
    out = {}
    corpus = os.path.join(REPO, "third_party", "snort3-community-rules",
                          "snort3-community.rules")
    for g in G.pack_groups(T.triage_file(corpus)):
        tag = re.sub(r"[^A-Za-z0-9]+", "", g.port_class) + "_%d" % g.index
        out[tag] = (g.n_slots, g.rule_count)
    return out


def discover():
    """Built partials, annotated with design size where we know it."""
    slots = group_slots_by_tag()
    out = []
    for path in sorted(glob.glob(os.path.join(
            REPO, ".superpowers", "pr-builds", "*_partial.bit"))):
        base = os.path.basename(path)
        m = re.match(r"group_[0-9a-f]+_(.+)_partial\.bit$", base)
        n_slots = n_rules = None
        kind = "other"
        if m:
            kind = "snortpf"
            n_slots, n_rules = slots.get(m.group(1), (None, None))
            if n_slots is None and m.group(1).startswith("n"):
                try:
                    n_slots = int(m.group(1)[1:])   # nNN prefix subgroups
                    n_rules = n_slots
                except ValueError:
                    pass
        elif base.startswith("pattern_"):
            kind = "pyro-regex"
        out.append({"path": path, "name": base, "kind": kind,
                    "bytes": os.path.getsize(path),
                    "slots": n_slots, "rules": n_rules})
    return out


def timed_load(iface, path):
    """One switch, phase-decomposed.  Returns seconds per phase."""
    from pyro import device as D
    phases = {}

    def runner(cmd, workdir, timeout):
        # The same seam carries both the Vivado JTAG program and the
        # recovery command; tell them apart by the executable.
        label = "recover" if "recover" in " ".join(cmd) else "program"
        t0 = time.perf_counter()
        rc, out = D._default_load_runner(cmd, workdir, timeout)
        phases[label] = time.perf_counter() - t0
        return rc, out

    cfg = D.DeviceConfig(iface=iface, load_runner=runner)
    t_all = time.perf_counter()
    D.load_partial(cfg, path)
    phases["load_total"] = time.perf_counter() - t_all

    t0 = time.perf_counter()
    ok, reason = D.probe_device(D.DeviceConfig(iface=iface))
    phases["probe"] = time.perf_counter() - t0
    phases["probe_ok"] = bool(ok)
    phases["switch_total"] = phases["load_total"] + phases["probe"]
    return phases


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    iface = os.environ.get("PYRO_DEVICE_IFACE")
    arts = discover()
    known = [a for a in arts if a["slots"]]
    known.sort(key=lambda a: a["slots"])

    if args.list or not iface:
        print("%-52s %-10s %-7s %s" % ("artifact", "kind", "slots", "bytes"))
        for a in arts:
            print("%-52s %-10s %-7s %d"
                  % (a["name"][:52], a["kind"],
                     a["slots"] if a["slots"] else "-", a["bytes"]))
        if not iface:
            print("\nPYRO_DEVICE_IFACE not set — listing only (SR18: no "
                  "device, no measurement).")
            return 0
        return 0

    # Sample the size range: smallest, median, largest known-size design,
    # plus a non-SNORT-PF child if one exists (a different circuit shape).
    picks = []
    if known:
        picks = [known[0], known[len(known) // 2], known[-1]]
    other = [a for a in arts if a["kind"] == "pyro-regex"]
    if other:
        picks.append(max(other, key=lambda a: a["bytes"]))
    seen, uniq = set(), []
    for p in picks:
        if p["name"] not in seen:
            seen.add(p["name"])
            uniq.append(p)

    print("Switch-cost curve, %d reps each, on %s\n" % (args.reps, iface))
    print("%-26s %-6s %-9s | %-8s %-8s %-8s | %-8s"
          % ("artifact", "slots", "MB", "program", "recover", "probe", "TOTAL"))
    print("-" * 88)
    rows = []
    for a in uniq:
        for rep in range(args.reps):
            try:
                ph = timed_load(iface, a["path"])
            except Exception as exc:
                print("%-26s FAILED: %s" % (a["name"][:26], exc))
                continue
            rows.append({**{k: a[k] for k in ("name", "kind", "bytes", "slots")},
                         "rep": rep, **ph})
            print("%-26s %-6s %-9.2f | %8.2f %8.2f %8.2f | %8.2f%s"
                  % (a["name"][:26].replace("group_", "").replace("_partial.bit", ""),
                     a["slots"] if a["slots"] else "-",
                     a["bytes"] / 1e6, ph.get("program", float("nan")),
                     ph.get("recover", float("nan")), ph["probe"],
                     ph["switch_total"], "" if ph["probe_ok"] else "  PROBE FAILED"))

    if rows:
        print("\n--- correlation: does switch cost depend on WHAT is loaded? ---")
        by_art = {}
        for r in rows:
            by_art.setdefault(r["name"], []).append(r)
        print("%-30s %-7s %-8s %-9s %-9s"
              % ("artifact", "slots", "MB", "mean tot", "spread"))
        for name, rs in by_art.items():
            tots = [r["switch_total"] for r in rs]
            print("%-30s %-7s %-8.2f %-9.2f %-9.2f"
                  % (name[:30].replace("group_", "").replace("_partial.bit", ""),
                     rs[0]["slots"] or "-", rs[0]["bytes"] / 1e6,
                     sum(tots) / len(tots), max(tots) - min(tots)))
        allt = [r["switch_total"] for r in rows]
        prog = [r.get("program", 0) for r in rows]
        rec = [r.get("recover", 0) for r in rows]
        print("\nacross ALL artifacts: total %.2f-%.2f s (spread %.2f s)"
              % (min(allt), max(allt), max(allt) - min(allt)))
        print("  program phase: %.2f-%.2f s   recover phase: %.2f-%.2f s"
              % (min(prog), max(prog), min(rec), max(rec)))
        smallest = min(rows, key=lambda r: r["slots"] or 0)
        largest = max(rows, key=lambda r: r["slots"] or 0)
        if smallest["slots"] and largest["slots"]:
            print("  %dx more slots (%d -> %d) costs %+.1f%% switch time"
                  % (largest["slots"] // max(1, smallest["slots"]),
                     smallest["slots"], largest["slots"],
                     100 * (largest["switch_total"] / smallest["switch_total"] - 1)))

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(rows, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print("\nwrote %s" % args.json_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
