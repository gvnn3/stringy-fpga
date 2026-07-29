#!/usr/bin/env python3
"""A5 working-set study: is prefilter coverage capacity-limited or latency-limited?

The A5 question (spec `snort-rule-offload` §9/OQ-1) is whether to open an
amendment slot for a *loadable-table* engine — an overlay whose rule content
is written at runtime (microseconds) instead of compiled into a bitstream
(~60 min build + ~16 s JTAG load, both measured in S3).

That decision turns on an empirical question this script answers **before**
any RTL is written:

    Given a traffic stream, how much of the ruleset's achievable nomination
    coverage does a residency policy actually deliver — and is the binding
    constraint the number of resident groups (CAPACITY, which S4's trie
    buys) or the time to change them (LATENCY, which A5's overlay buys)?

If coverage is capacity-limited, A5 is not worth opening: fast swaps of a
too-small resident set do not help.  If it is latency-limited, A5 buys real
coverage that more capacity alone would not.

WHAT IS MEASURED vs WHAT IS ASSUMED
-----------------------------------
Measured (real, from the vendored corpus and the S3 packing — no invention):

  * the 21-group SR6 partition and each group's rule count;
  * each rule's own header predicate, so a rule counts as *relevant* to a
    flow only if its dst-port token actually admits that port (evaluated
    with the daemon's SR13 VarTable).  This is stricter and more honest
    than the group's coarse port class: a `literal`-class group holds rules
    for ports 21/25/445 that can never fire on a port-80 flow, and counting
    them would inflate every denominator;
  * therefore the real value function V(group, port) = how many rules of
    that group could fire on a flow to that port.

Assumed (swept, never fixed at one value — the honesty axis):

  * the traffic mix over ports (Zipf exponent `s`: 0 = uniform,
    ~1.2 = internet-like, 2 = extreme concentration);
  * whether the mix is stationary or shifts in phases (phase length);
  * flow sizes (lognormal), arrival rate.

No claim in the output depends on a single assumed traffic mix: every
headline number is reported as a function of the swept parameters, and the
regime diagnosis (capacity- vs latency-limited) is reported per cell.

METRIC
------
Byte-weighted **relevant-rule coverage**: for each flow, the fraction of the
rules that could fire on it which were resident when it arrived, weighted by
the flow's bytes.  A cache miss is not an error — SR5 guarantees Snort sees
all traffic regardless, so a miss costs coverage only, never correctness.
That is what makes aggressive paging safe to consider at all.

POLICIES
--------
  pin:<name>   statically pin one group (k=1, no swaps) — today's floor
  s3           the shipped SR10 scheduler shape: port-mix driven, k=1,
               L=16 s, 2x hysteresis, dwell — today's actual behaviour
  lru(k,L)     k resident slots, demand-filled on miss with latency L,
               LRU eviction, fills serialized (one config port)
  all          every group resident — the ceiling (what S4's trie buys)

Usage:
    .venv-pyro/bin/python3 scripts/a5_working_set_study.py [--quick]
"""

import argparse
import bisect
import json
import math
import os
import random
import sys
from collections import OrderedDict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

RULES = os.path.join(REPO, "third_party", "snort3-community-rules",
                     "snort3-community.rules")

# Ports the study draws from: every port class that owns groups, plus
# high/ephemeral ports for the `any`-class tail.  Chosen from the corpus's
# own SF8 port profile, not invented.
PORTS = [80, 8080, 443, 22, 21, 25, 53, 110, 143, 139, 445, 111,
         1521, 5060, 3306, 8000, 44444, 33333]

# Measured on this host, 2026-07-29 (docs/notebook.md): JTAG partial +
# in-band wedge recovery.  This is the latency A5 would replace.
L_PR_SECONDS = 16.0


def build_value_table():
    """V[group_index][port] = rules in that group that could fire on `port`.

    Real: uses each rule's own dst-port token through the daemon's SR13
    variable table.  A rule counts for a port only if its header predicate
    admits it.
    """
    from pyro.snort import daemon as D
    from pyro.snort import groups as G
    from pyro.snort import triage as T

    triaged = T.triage_file(RULES)
    groups = G.pack_groups(triaged)
    vt = D.VarTable()

    # rule (gid,sid) -> its dst-port token, from the parsed rule
    port_tok = {}
    for rule, res in triaged:
        sid = gid = None
        for o in rule.options:
            if o.key == "sid":
                try:
                    sid = int(o.value)
                except (TypeError, ValueError):
                    pass
            elif o.key == "gid":
                try:
                    gid = int(o.value)
                except (TypeError, ValueError):
                    pass
        if sid is not None:
            port_tok[(1 if gid is None else gid, sid)] = rule.dst_port or "any"

    # Split by SPECIFICITY so the headline result can be stress-tested:
    # a rule whose dst-port token is `any` matches every flow but is (in
    # reality) less likely to fire on any particular service, while a
    # port-specific rule is targeted at exactly this traffic.  The study
    # sweeps a weight w over the specific ones rather than assuming a value.
    generic, specific = [], []
    for g in groups:
        grow, srow = {}, {}
        for port in PORTS:
            ng = ns = 0
            for slot in g.slots:
                for r in slot.rules:
                    tok = port_tok.get((r.gid, r.sid), "any")
                    if not vt.port_holds(tok, port):
                        continue
                    if (tok or "any").strip().lower() == "any":
                        ng += 1
                    else:
                        ns += 1
            grow[port], srow[port] = ng, ns
        generic.append(grow)
        specific.append(srow)
    return groups, generic, specific


def weighted(generic, specific, w):
    """value[g][port] under specificity weight `w`, plus the denominator."""
    value = [{p: generic[g][p] + w * specific[g][p] for p in PORTS}
             for g in range(len(generic))]
    achievable = {p: sum(v[p] for v in value) for p in PORTS}
    return value, achievable


# --------------------------------------------------------------------------
# Traffic model (the ASSUMED part — swept, never fixed)
# --------------------------------------------------------------------------
def make_trace(n_flows, skew, phase_s, seed, span_s=3600.0):
    """Flow arrivals: (t, port, bytes).

    `skew` is the Zipf exponent over a per-phase random port ranking.
    `phase_s` <= 0 means stationary (one ranking for the whole trace);
    otherwise the ranking is redrawn every `phase_s` seconds, which is what
    makes fill LATENCY bite — a stationary mix understates it.
    """
    rng = random.Random(seed)
    rate = n_flows / float(span_s)
    weights = [1.0 / ((i + 1) ** skew) for i in range(len(PORTS))]

    def ranking():
        order = PORTS[:]
        rng.shuffle(order)
        return order

    order = ranking()
    cum = []
    acc = 0.0
    for w in weights:
        acc += w
        cum.append(acc)
    total_w = cum[-1]

    trace = []
    t = 0.0
    phase_end = phase_s if phase_s > 0 else float("inf")
    for _ in range(n_flows):
        t += rng.expovariate(rate)
        if t >= phase_end:
            order = ranking()
            phase_end += phase_s
        x = rng.random() * total_w
        port = order[bisect.bisect_left(cum, x)]
        nbytes = int(min(2_000_000, max(64, rng.lognormvariate(8.0, 1.6))))
        trace.append((t, port, nbytes))
    return trace


# --------------------------------------------------------------------------
# Residency policies
# --------------------------------------------------------------------------
class Policy:
    def resident_at(self, t):
        raise NotImplementedError

    def observe(self, t, port, value_row, nbytes=1):
        pass


class PinPolicy(Policy):
    def __init__(self, idx):
        self.idx = idx

    def resident_at(self, t):
        return {self.idx}


class AllPolicy(Policy):
    def __init__(self, n):
        self.all = set(range(n))

    def resident_at(self, t):
        return self.all


class LRUPolicy(Policy):
    """k slots, demand-filled on miss with latency L, LRU eviction.

    Fills are serialized: one configuration port, so a fill in flight
    blocks the next.  That models both the PR path (one region) and an
    overlay (one table-write channel), and it is what makes a small k with
    a large L thrash.
    """

    def __init__(self, k, latency, n_groups, warm=()):
        self.k = k
        self.L = latency
        self.n = n_groups
        self.resident = OrderedDict((i, 0.0) for i in list(warm)[:k])
        self.pending = None          # (group, ready_time)
        self.fills = 0
        self.fill_seconds = 0.0

    def _settle(self, t):
        if self.pending and t >= self.pending[1]:
            g, _ready = self.pending
            self.pending = None
            if g not in self.resident:
                while len(self.resident) >= self.k:
                    self.resident.popitem(last=False)   # evict LRU
                self.resident[g] = t

    def resident_at(self, t):
        self._settle(t)
        return set(self.resident)

    def observe(self, t, port, value_row, nbytes=1):
        self._settle(t)
        for g in list(self.resident):
            if value_row[g] > 0:
                self.resident.move_to_end(g)            # touch: it served
        if self.pending is not None:
            return
        # Demand: the highest-value non-resident group for this flow.
        best, best_v = None, 0
        for g in range(self.n):
            if g in self.resident:
                continue
            if value_row[g] > best_v:
                best, best_v = g, value_row[g]
        if best is None or best_v <= 0:
            return
        # Free capacity: fill unconditionally (a spare slot costs nothing to
        # use).  Only when full does a candidate have to beat the LRU victim,
        # and it must beat it strictly, so equal-value groups never thrash.
        if len(self.resident) < self.k:
            self.pending = (best, t + self.L)
            self.fills += 1
            self.fill_seconds += self.L
            return
        victim_v = value_row[next(iter(self.resident))]   # LRU end
        if best_v > victim_v:
            self.pending = (best, t + self.L)
            self.fills += 1
            self.fill_seconds += self.L


class S3ShippedPolicy(Policy):
    """FAITHFUL model of the scheduler actually shipped in S3.

    The deployed heuristic scores **port classes by observed bytes** (the
    decayed PortMixHistogram) and swaps to a group of the winning class —
    it is blind to how many of that group's rules could fire.  Modelled
    honestly, including that blindness, so the study measures what is
    deployed rather than an idealisation of it.
    """

    def __init__(self, n_groups, class_of_group, class_of_port,
                 latency=L_PR_SECONDS, hysteresis=2.0, dwell=300.0,
                 challenge=60.0, half_life=60.0, warm=0):
        self.n = n_groups
        self.cls_of_g = class_of_group          # group idx -> class
        self.cls_of_p = class_of_port           # port -> most-specific class
        self.groups_of_cls = {}
        for g, c in enumerate(class_of_group):
            self.groups_of_cls.setdefault(c, []).append(g)
        self.L = latency
        self.hyst = hysteresis
        self.dwell = dwell
        self.challenge = challenge
        self.half_life = half_life
        self.cur = warm
        self.pending = None
        self.last_swap = -1e18
        self.mix = {}
        self.stamp = 0.0
        self.challenger = None
        self.challenge_since = 0.0
        self.rr = {}
        self.fills = 0
        self.fill_seconds = 0.0

    def _decay(self, t):
        dt = t - self.stamp
        if dt > 0:
            f = 0.5 ** (dt / self.half_life)
            for k in self.mix:
                self.mix[k] *= f
            self.stamp = t

    def _settle(self, t):
        if self.pending and t >= self.pending[1]:
            self.cur = self.pending[0]
            self.pending = None

    def resident_at(self, t):
        self._settle(t)
        return set() if self.pending is not None else {self.cur}

    def observe(self, t, port, value_row, nbytes=1):
        self._settle(t)
        self._decay(t)
        # BYTES per port class — the shipped signal.
        c = self.cls_of_p[port]
        self.mix[c] = self.mix.get(c, 0.0) + nbytes
        if self.pending is not None or not self.mix:
            return
        best_c = max(self.mix, key=lambda x: self.mix[x])
        cur_c = self.cls_of_g[self.cur]
        if best_c == cur_c:
            self.challenger = None
            return
        if self.mix[best_c] < self.hyst * max(self.mix.get(cur_c, 0.0), 1e-9):
            self.challenger = None
            return
        if self.challenger != best_c:
            self.challenger = best_c
            self.challenge_since = t
            return
        if t - self.challenge_since < self.challenge:
            return
        if t - self.last_swap < self.dwell:
            return
        members = self.groups_of_cls.get(best_c, [])
        if not members:
            return
        i = self.rr.get(best_c, -1) + 1
        self.rr[best_c] = i
        self.pending = (members[i % len(members)], t + self.L)
        self.last_swap = t
        self.fills += 1
        self.fill_seconds += self.L
        self.challenger = None


class S3ValueAwarePolicy(Policy):
    """A k=1 scheduler that scores by RULE VALUE rather than raw bytes —
    the minimal fix to the shipped heuristic, included to separate 'k=1 is
    hopeless' from 'the shipped signal is the wrong signal'."""

    def __init__(self, n_groups, latency=L_PR_SECONDS, hysteresis=2.0,
                 dwell=300.0, challenge=60.0, half_life=60.0, warm=0):
        self.n = n_groups
        self.L = latency
        self.hyst = hysteresis
        self.dwell = dwell
        self.challenge = challenge
        self.half_life = half_life
        self.cur = warm
        self.pending = None
        self.last_swap = -1e18
        self.mix = {}
        self.stamp = 0.0
        self.challenger = None
        self.challenge_since = 0.0
        self.fills = 0
        self.fill_seconds = 0.0

    def _decay(self, t):
        dt = t - self.stamp
        if dt > 0:
            f = 0.5 ** (dt / self.half_life)
            for k in self.mix:
                self.mix[k] *= f
            self.stamp = t

    def _settle(self, t):
        if self.pending and t >= self.pending[1]:
            self.cur = self.pending[0]
            self.pending = None

    def resident_at(self, t):
        self._settle(t)
        return set() if self.pending is not None else {self.cur}

    def observe(self, t, port, value_row, nbytes=1):
        self._settle(t)
        self._decay(t)
        for g in range(self.n):
            if value_row[g] > 0:
                self.mix[g] = self.mix.get(g, 0.0) + value_row[g]
        if self.pending is not None or not self.mix:
            return
        best = max(self.mix, key=lambda g: self.mix[g])
        if best == self.cur:
            self.challenger = None
            return
        if self.mix[best] < self.hyst * max(self.mix.get(self.cur, 0.0), 1e-9):
            self.challenger = None
            return
        if self.challenger != best:
            self.challenger = best
            self.challenge_since = t
            return
        if t - self.challenge_since < self.challenge:
            return
        if t - self.last_swap < self.dwell:
            return
        self.pending = (best, t + self.L)
        self.last_swap = t
        self.fills += 1
        self.fill_seconds += self.L
        self.challenger = None


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------
def run(trace, value, achievable, policy):
    """Byte-weighted relevant-rule coverage over the trace."""
    n = len(value)
    covered_bytes = 0.0
    total_bytes = 0
    blind_bytes = 0
    for t, port, nbytes in trace:
        row = [value[g][port] for g in range(n)]
        res = policy.resident_at(t)
        denom = achievable[port]
        if denom > 0:
            hit = sum(row[g] for g in res)
            covered_bytes += nbytes * (hit / denom)
            if hit == 0:
                blind_bytes += nbytes
        total_bytes += nbytes
        policy.observe(t, port, row, nbytes)
    return {
        "coverage": covered_bytes / total_bytes if total_bytes else 0.0,
        "blind_byte_frac": blind_bytes / total_bytes if total_bytes else 0.0,
        "fills": getattr(policy, "fills", 0),
        "fill_seconds": getattr(policy, "fill_seconds", 0.0),
    }


def scenario(groups, generic, specific, w, skew, phase_s, n_flows, seed,
             span_s=3600.0):
    from pyro.snort import daemon as D
    value, achievable = weighted(generic, specific, w)
    n = len(groups)
    trace = make_trace(n_flows, skew, phase_s, seed, span_s)

    cov = [0.0] * n
    for _t, port, nbytes in trace:
        denom = achievable[port]
        if denom:
            for g in range(n):
                cov[g] += nbytes * value[g][port] / denom
    best_pin = max(range(n), key=lambda g: cov[g])
    universal = [g for g in range(n)
                 if min(value[g][p] for p in PORTS) > 0]
    pin_any = max(universal, key=lambda g: cov[g]) if universal else best_pin

    vt = D.VarTable()
    cls_of_g = [g.port_class for g in groups]
    cls_of_p = {p: vt.most_specific_class(p) for p in PORTS}

    res = {}
    res["pin_best"] = run(trace, value, achievable, PinPolicy(best_pin))
    res["pin_any"] = run(trace, value, achievable, PinPolicy(pin_any))
    res["s3_shipped"] = run(trace, value, achievable,
                            S3ShippedPolicy(n, cls_of_g, cls_of_p,
                                            warm=best_pin))
    res["s3_valueaware"] = run(trace, value, achievable,
                               S3ValueAwarePolicy(n, warm=best_pin))
    res["all"] = run(trace, value, achievable, AllPolicy(n))
    for k in (1, 2, 4, 8):
        for L, tag in ((L_PR_SECONDS, "16s"), (1.0, "1s"), (0.001, "1ms")):
            res["lru_k%d_L%s" % (k, tag)] = run(
                trace, value, achievable, LRUPolicy(k, L, n, warm=[best_pin]))
    return {"skew": skew, "phase_s": phase_s, "w": w,
            "best_pin": groups[best_pin].name, "pin_any": groups[pin_any].name,
            "results": res}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flows", type=int, default=8000)
    ap.add_argument("--span-s", type=float, default=3600.0,
                    help="simulated seconds the trace spans")
    ap.add_argument("--seed", type=int, default=20260729)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    groups, generic, specific = build_value_table()
    n = len(groups)
    print("REAL inputs: %d groups, %d rules. Value = rules whose OWN header "
          "predicate admits the flow's port." % (n, sum(g.rule_count for g in groups)))
    v1, a1 = weighted(generic, specific, 1.0)
    print("  achievable rules by port (w=1): " +
          ", ".join("%d:%d" % (p, a1[p]) for p in PORTS[:6]) + ", ...")

    skews = [0.0, 1.2] if args.quick else [0.0, 1.2, 2.0]
    phases = [0.0, 60.0] if args.quick else [0.0, 60.0, 300.0]
    ws = [1.0, 10.0] if args.quick else [1.0, 3.0, 10.0]

    rows = []
    for w in ws:
        for skew in skews:
            for phase_s in phases:
                rows.append(scenario(groups, generic, specific, w, skew,
                                     phase_s, args.flows, args.seed,
                                     args.span_s))

    hdr = ("%-4s %-5s %-6s | %-8s %-8s | %-10s %-10s | %-8s %-8s %-8s %-8s | %-7s | %s"
           % ("w", "skew", "phase", "pin_best", "pin_any", "s3_shipped",
              "s3_value", "k1/1ms", "k2/1ms", "k4/1ms", "k8/1ms", "all", "regime"))
    print("\n" + hdr)
    print("-" * len(hdr))
    for sc in rows:
        r = sc["results"]
        def c(key):
            return 100 * r[key]["coverage"]
        lat_gain = c("lru_k1_L1ms") - c("lru_k1_L16s")
        cap_gain = c("lru_k8_L1ms") - c("lru_k1_L1ms")
        if cap_gain > 2 * max(lat_gain, 0.1):
            regime = "CAPACITY"
        elif lat_gain > 2 * max(cap_gain, 0.1):
            regime = "LATENCY"
        else:
            regime = "mixed"
        print("%-4.0f %-5.1f %-6s | %7.1f%% %7.1f%% | %9.1f%% %9.1f%% | "
              "%7.1f%% %7.1f%% %7.1f%% %7.1f%% | %6.1f%% | %s"
              % (sc["w"], sc["skew"],
                 ("stat" if sc["phase_s"] == 0 else "%ds" % int(sc["phase_s"])),
                 c("pin_best"), c("pin_any"), c("s3_shipped"),
                 c("s3_valueaware"), c("lru_k1_L1ms"), c("lru_k2_L1ms"),
                 c("lru_k4_L1ms"), c("lru_k8_L1ms"), c("all"), regime))

    print("\nlatency sensitivity at fixed k (does making swaps FAST help?):")
    print("%-4s %-5s %-6s | %-9s %-9s %-9s | %-9s %-9s %-9s"
          % ("w", "skew", "phase", "k1 L=16s", "k1 L=1s", "k1 L=1ms",
             "k4 L=16s", "k4 L=1s", "k4 L=1ms"))
    print("-" * 78)
    for sc in rows:
        r = sc["results"]
        def c(key):
            return 100 * r[key]["coverage"]
        print("%-4.0f %-5.1f %-6s | %8.1f%% %8.1f%% %8.1f%% | %8.1f%% %8.1f%% %8.1f%%"
              % (sc["w"], sc["skew"],
                 ("stat" if sc["phase_s"] == 0 else "%ds" % int(sc["phase_s"])),
                 c("lru_k1_L16s"), c("lru_k1_L1s"), c("lru_k1_L1ms"),
                 c("lru_k4_L16s"), c("lru_k4_L1s"), c("lru_k4_L1ms")))

    print("\ns3 scheduler activity (k=1, L=16s): swaps over the trace")
    for sc in rows:
        r = sc["results"]
        print("  w=%-3.0f skew=%-4.1f %-6s  shipped: %3d swaps (%4.0fs blind) | "
              "value-aware: %3d swaps"
              % (sc["w"], sc["skew"],
                 ("stat" if sc["phase_s"] == 0 else "%ds" % int(sc["phase_s"])),
                 r["s3_shipped"]["fills"], r["s3_shipped"]["fill_seconds"],
                 r["s3_valueaware"]["fills"]))

    print("\nfill activity (k=4): fills / seconds spent filling")
    for sc in rows[:6]:
        r = sc["results"]
        print("  w=%-3.0f skew=%-4.1f %-6s  L=16s: %4d fills %6.0fs busy | "
              "L=1ms: %4d fills %5.2fs busy"
              % (sc["w"], sc["skew"],
                 ("stat" if sc["phase_s"] == 0 else "%ds" % int(sc["phase_s"])),
                 r["lru_k4_L16s"]["fills"], r["lru_k4_L16s"]["fill_seconds"],
                 r["lru_k4_L1ms"]["fills"], r["lru_k4_L1ms"]["fill_seconds"]))

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump({"ports": PORTS, "scenarios": rows}, fh, indent=2,
                      sort_keys=True)
            fh.write("\n")
        print("\nwrote %s" % args.json_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
