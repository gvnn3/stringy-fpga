#!/usr/bin/env python3
"""Rule-packing study: can SR6 packing raise coverage without new hardware?

Follow-up to `docs/studies/a5-working-set.md` recommendation 2, which
proposed co-locating universally-relevant rules with traffic-specific ones
"to raise the value of a single resident group."

**That recommendation's reasoning is refuted here, and the refutation is
the first thing this script prints.**  With `GROUP_MAX = 256` *rules*,
every port already has a group in which all 256 rules fire (the three
`any`-class groups fire everywhere by construction).  Coverage at k=1 is
therefore `256 / achievable(port)` no matter how the rules are arranged —
no re-packing can push a single 256-rule group past 256 firing rules.
Mixing classes changes which rules, never how many.

Checking that surfaced two levers the study did miss, and this script
measures all of them:

  P0 baseline      port class, sid order, <= 256 **rules**/group (today)
  P1 slot-bound    same, but bounded by 256 **slots** — the actual hardware
                   limit (SF6 MAX_PATTERNS bounds automata, not rules), so
                   anchor dedup buys extra rules for free at identical
                   circuit size
  P2 anchor-aware  co-locate rules that SHARE an anchor before packing, so
                   dedup is maximised rather than incidental
  P3 mixed-class   the study's original idea, measured so its refutation is
                   empirical and not merely argued
  P4 wider         slot-bound at 512 / 1024 slots — the capacity lever,
                   which SR6 permits raising "only on the basis of measured
                   post-route utilization" (we now have that: 253 slots =
                   10,323 LUTs = 12.7% of the 80K budget)

Each variant is scored on: how many rules a resident group holds, the
resulting byte-weighted coverage (reusing the A5 study's traffic model and
metric), the circuit cost, and — the cost that matters and is easy to
forget — **SR6 stability**, i.e. how many groups a weekly ruleset diff
dirties, since a packing that reshuffles on every corpus change would
trade cheap coverage for expensive rebuilds.

Usage:
    .venv-pyro/bin/python3 scripts/rule_packing_study.py
"""

import argparse
import os
import re as _re
import sys
from collections import OrderedDict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

RULES = os.path.join(REPO, "third_party", "snort3-community-rules",
                     "snort3-community.rules")

# Measured post-route anchor point (2026-07-29, $HTTP_PORTS/0 at dpb=1):
# 253 slots / 4,147 byte-edges -> 10,323 LUTs.  Used to extrapolate cost for
# wider groups.  It folds the shared wrapper into the slope, so it
# OVER-estimates larger groups (the wrapper amortises) — conservative in the
# direction that matters for "does it still fit".
LUTS_PER_BYTE_EDGE = 10323.0 / 4147.0
PR_LUT_BUDGET = 80000


# --------------------------------------------------------------------------
# Packing strategies.  These build RuleGroup objects directly rather than
# touching pyro.snort.groups: this is a study, and no product packing should
# change until a variant is chosen.
# --------------------------------------------------------------------------
def _build(port_cls, index, entries, bound):
    from pyro.snort.groups import GroupSlot, RuleGroup
    order, by_key = [], OrderedDict()
    for e in entries:
        k = e.slot_key
        if k not in by_key:
            by_key[k] = []
            order.append(k)
        by_key[k].append(e)
    slots = []
    for i, k in enumerate(order):
        claim = by_key[k][0]
        slots.append(GroupSlot(i, claim.anchor, claim.nocase,
                               tuple(e.ref for e in by_key[k]),
                               pattern=claim.pattern, flags=claim.flags,
                               tail_span=claim.tail_span))
    return RuleGroup(port_cls, index, tuple(slots), bound)


def pack_baseline(entries, bound=256):
    """P0 — today's SR6: port class, sid order, <= `bound` RULES/group."""
    buckets = OrderedDict()
    for e in entries:
        buckets.setdefault(e.port_class, []).append(e)
    out = []
    for cls in sorted(buckets):
        es = buckets[cls]
        for i in range(0, len(es), bound):
            out.append(_build(cls, i // bound, es[i:i + bound], bound))
    return out


def _pack_by_slots(entries, slot_max, key=None):
    """Greedy: add rules until the DISTINCT SLOT count would exceed
    `slot_max`.  Slots are the hardware limit (SF6 MAX_PATTERNS)."""
    buckets = OrderedDict()
    for e in entries:
        buckets.setdefault(e.port_class, []).append(e)
    out = []
    for cls in sorted(buckets):
        es = buckets[cls]
        if key is not None:
            es = sorted(es, key=key)
        idx, cur, seen = 0, [], set()
        for e in es:
            k = e.slot_key
            if k not in seen and len(seen) >= slot_max:
                out.append(_build(cls, idx, cur, slot_max))
                idx += 1
                cur, seen = [], set()
            cur.append(e)
            seen.add(k)
        if cur:
            out.append(_build(cls, idx, cur, slot_max))
    return out


def pack_slot_bound(entries, slot_max=256):
    """P1 — sid order preserved (SR6 stability), bounded by slots."""
    return _pack_by_slots(entries, slot_max)


def pack_anchor_aware(entries, slot_max=256):
    """P2 — co-locate shared anchors first, then pack by slots.

    Maximises dedup, at a stability cost this study measures: the sort key
    is the anchor, so an anchor change can move a rule across groups.
    """
    return _pack_by_slots(entries, slot_max,
                          key=lambda e: (e.slot_key, e.sid, e.gid))


def pack_mixed_class(entries, slot_max=256):
    """P3 — the study's original recommendation, implemented to be refuted.

    Deal the universally-relevant (`any` dst-port token) rules round-robin
    across the port-specific groups instead of letting them form their own
    `any`-class groups, so every group is a blend.
    """
    universal = [e for e in entries if e.ref.dst_port.strip().lower() == "any"]
    specific = [e for e in entries if e.ref.dst_port.strip().lower() != "any"]
    if not specific:
        return _pack_by_slots(universal, slot_max)

    # Interleave universals evenly through each port-specific class bucket,
    # so every group is a blend.  Every rule is placed exactly once — a
    # truncating version would refute the idea with a bug rather than with
    # evidence, which is worse than not testing it.
    buckets = OrderedDict()
    for e in specific:
        buckets.setdefault(e.port_class, []).append(e)
    share = len(universal) // max(1, len(buckets))
    blended, pos = [], 0
    for cls in sorted(buckets):
        es = buckets[cls]
        take = universal[pos:pos + share]
        pos += share
        if take:
            step = max(1, len(es) // len(take))
            merged, ti = [], 0
            for i, e in enumerate(es):
                merged.append(e)
                if i % step == 0 and ti < len(take):
                    merged.append(take[ti])
                    ti += 1
            merged.extend(take[ti:])
        else:
            merged = es
        blended.extend(merged)
    leftovers = universal[pos:]          # never dropped: their own groups
    return _pack_by_slots(blended, slot_max) + \
        _pack_by_slots(leftovers, slot_max)


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def shape(groups):
    slots = [g.n_slots for g in groups]
    rules = [g.rule_count for g in groups]
    return {
        "n_groups": len(groups),
        "rules_total": sum(rules),
        "max_rules": max(rules) if rules else 0,
        "mean_rules": sum(rules) / len(rules) if rules else 0,
        "max_slots": max(slots) if slots else 0,
        "mean_slots": sum(slots) / len(slots) if slots else 0,
    }


def circuit_cost(group):
    """Byte-edges and an extrapolated LUT figure for one group."""
    from pyro.hdl import automaton as _auto
    from pyro.snort import groups as G
    edges = 0
    for au in G.group_automata(group):
        if au is None:
            continue
        for st in range(au.n_states):
            for e in au.edges[st]:
                if e.kind == _auto.E_BYTE:
                    edges += 1
    return edges, edges * LUTS_PER_BYTE_EDGE


def value_tables(groups):
    """V[g][port] using each rule's OWN header predicate (the A5 metric)."""
    from a5_working_set_study import PORTS
    from pyro.snort import daemon as D
    vt = D.VarTable()
    value = []
    for g in groups:
        row = {}
        for p in PORTS:
            n = 0
            for s in g.slots:
                if s.tombstone:
                    continue
                for r in s.rules:
                    if vt.port_holds(r.dst_port, p):
                        n += 1
            row[p] = n
        value.append(row)
    achievable = {p: sum(v[p] for v in value) for p in PORTS}
    return value, achievable


def coverage(groups, skew, phase_s, seed, n_flows, span_s, ks=(1, 4)):
    from a5_working_set_study import (LRUPolicy, PinPolicy, make_trace, run)
    value, ach = value_tables(groups)
    n = len(groups)
    trace = make_trace(n_flows, skew, phase_s, seed, span_s)
    cov = [0.0] * n
    for _t, port, nb in trace:
        if ach[port]:
            for g in range(n):
                cov[g] += nb * value[g][port] / ach[port]
    best = max(range(n), key=lambda g: cov[g])
    out = {"pin": 100 * run(trace, value, ach, PinPolicy(best))["coverage"]}
    for k in ks:
        if k == 1:
            continue
        out["k%d" % k] = 100 * run(trace, value, ach,
                                   LRUPolicy(k, 0.001, n, warm=[best]))["coverage"]
    return out


def dirty_under_weekly_diff(packer, triaged, new_triaged, **kw):
    """SR6 stability: how many groups a weekly diff dirties under `packer`.

    Compared by the group's canonical bytes (what the SR9 cache key is a
    function of), so 'dirty' means 'must be rebuilt in Vivado'.
    """
    from pyro.snort import groups as G
    old = packer(G.groupable_entries(triaged), **kw)
    new = packer(G.groupable_entries(new_triaged), **kw)
    old_by = {g.name: g.canonical_bytes() for g in old}
    new_by = {g.name: g.canonical_bytes() for g in new}
    dirty = sum(1 for n, b in new_by.items() if old_by.get(n) != b)
    return dirty, len(new_by)


def make_weekly_diff(path_out):
    """The AC-S3-3 diff shape: 8 deletions + 5 anchor changes + 20 adds."""
    import re
    from pyro.snort import groups as G
    from pyro.snort import triage as T
    triaged = T.triage_file(RULES)
    base = G.pack_groups(triaged)
    g = next(x for x in base if x.name == "$ORACLE_PORTS/1")
    sids = [r.sid for s in g.slots for r in s.rules]
    delete, modify = set(sids[:8]), set(sids[8:13])
    lines = []
    with open(RULES, encoding="utf-8", errors="surrogateescape") as fh:
        for line in fh:
            st = line.strip()
            if not st or st.startswith("#"):
                lines.append(line.rstrip("\n"))
                continue
            m = re.search(r"\bsid:(\d+)", st)
            sid = int(m.group(1)) if m else -1
            if sid in delete:
                continue
            if sid in modify:
                line = line.replace('content:"', 'content:"XZ', 1)
            lines.append(line.rstrip("\n"))
    for i in range(20):
        lines.append('alert tcp $EXTERNAL_NET any -> $HOME_NET $HTTP_PORTS '
                     '(msg:"weekly add %d"; flow:to_server,established; '
                     'content:"/weekly-drill-%04d.cgi"; sid:%d; rev:1;)'
                     % (i, i, 3900000 + i))
    with open(path_out, "w", encoding="utf-8", errors="surrogateescape") as fh:
        fh.write("\n".join(lines) + "\n")
    return T.triage_file(path_out)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flows", type=int, default=6000)
    ap.add_argument("--span-s", type=float, default=3600.0)
    ap.add_argument("--seed", type=int, default=20260730)
    ap.add_argument("--tmp", default="/tmp/rule_packing_weekly.rules")
    args = ap.parse_args()

    from a5_working_set_study import PORTS
    from pyro.snort import groups as G
    from pyro.snort import triage as T

    triaged = T.triage_file(RULES)
    entries = G.groupable_entries(triaged)

    # ---- the refutation, stated with numbers -------------------------
    base = pack_baseline(entries)
    value, ach = value_tables(base)
    print("=" * 74)
    print("REFUTATION of study rec. 2 ('co-locate universal with specific')")
    print("=" * 74)
    print("At GROUP_MAX=256 RULES, every port already has a group in which")
    print("all 256 rules fire, so k=1 coverage is 256/achievable(port) for")
    print("ANY arrangement.  Mixing classes changes WHICH rules are resident,")
    print("never HOW MANY.  Measured, current packing:\n")
    print("  %-7s %-12s %-16s %s" % ("port", "achievable", "best group V", "= 256?"))
    for p in (80, 22, 25, 443, 1521):
        bv = max(value[g][p] for g in range(len(base)))
        print("  %-7d %-12d %-16d %s" % (p, ach[p], bv, "yes" if bv >= 256 else "NO"))
    print("\nSo the real levers are the SLOT bound and group WIDTH, below.\n")

    # ---- variants ----------------------------------------------------
    variants = [
        ("P0 baseline (<=256 rules)", lambda e: pack_baseline(e, 256), {}),
        ("P1 slot-bound (<=256 slots)", lambda e: pack_slot_bound(e, 256), {}),
        ("P2 anchor-aware (<=256 slots)", lambda e: pack_anchor_aware(e, 256), {}),
        ("P3 mixed-class (<=256 slots)", lambda e: pack_mixed_class(e, 256), {}),
        ("P4a slot-bound <=512", lambda e: pack_slot_bound(e, 512), {}),
        ("P4b slot-bound <=1024", lambda e: pack_slot_bound(e, 1024), {}),
    ]

    print("=" * 110)
    print("%-30s %-7s %-9s %-9s %-11s %-8s %-8s" %
          ("packing", "groups", "max rule", "max slot", "est LUTs", "cov k=1", "cov k=4"))
    print("=" * 110)
    results = []
    for name, packer, kw in variants:
        gs = packer(entries)
        sh = shape(gs)
        # Cost is driven by total anchor BYTES (byte-edges), not slot count:
        # report the worst group by estimated LUTs, which is the number that
        # decides "does every group still fit the PR budget".
        luts = max(circuit_cost(g)[1] for g in gs)
        cov = coverage(gs, 1.2, 300.0, args.seed, args.flows, args.span_s)
        fits = "" if luts <= PR_LUT_BUDGET else "  OVER BUDGET"
        print("%-30s %-7d %-9d %-9d %-11s %7.1f%% %7.1f%%%s"
              % (name, sh["n_groups"], sh["max_rules"], sh["max_slots"],
                 "%.0f" % luts, cov["pin"], cov["k4"], fits))
        results.append((name, packer, sh, cov, luts))

    # ---- SR6 stability: what does a weekly diff cost each variant? ----
    print("\n" + "=" * 74)
    print("SR6 STABILITY — groups dirtied by the AC-S3-3 weekly diff")
    print("(dirty = canonical bytes changed = a Vivado rebuild, ~60 min each)")
    print("=" * 74)
    new_triaged = make_weekly_diff(args.tmp)
    print("%-30s %-14s %s" % ("packing", "dirty/total", "rebuild cost"))
    for name, packer, _sh, _cov, _l in results:
        d, tot = dirty_under_weekly_diff(packer, triaged, new_triaged)
        print("%-30s %-14s ~%.1f h" % (name, "%d/%d" % (d, tot), d * 1.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
