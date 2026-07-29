# A5 working-set study — is prefilter coverage capacity- or latency-limited?

- **Date:** 2026-07-29 · **Branch:** `a5-working-set-study`
- **Tool:** `scripts/a5_working_set_study.py` · **Data:** `a5_working_set_data.json`
- **Question:** OQ-1 deferred the A5 amendment (a loadable-table overlay
  engine, runtime table writes instead of re-synthesis) pending *"S3's
  measured swap cadence."* S3 measured it: **~16 s** per JTAG swap, **~60 min**
  per group build. This study asks whether spending engineering on making
  swaps *faster* (A5) would buy coverage, or whether the binding constraint
  is how many rules fit resident at once (capacity, which S4's trie buys).

## Verdict

**Coverage is capacity-limited, in 27 of 27 scenarios.** Latency is very
nearly irrelevant; capacity is worth 3–4×.

| resident groups k (fast fills) | coverage |
|---|---|
| 1 (today) | 28.1% |
| 2 | 56.2% |
| 4 | 76.4% |
| 8 | 84.3% |
| 21 (all — what S4's trie buys) | 100% |

Against that, dropping fill latency from **16 s to 1 ms at fixed k changes
coverage by less than one point** in essentially every cell — because a
banked engine keeps serving its current rules while the next set loads, so
a slow fill delays improvement rather than costing coverage.

**On coverage grounds alone, A5 is not justified and OQ-1's deferral should
stand.** (A5 retains a separate, untested *operational* argument — see
"What this does not say".)

## The unexpected finding: the shipped S3 scheduler is worse than pinning

| k=1 policy | coverage |
|---|---|
| shipped SR10 scheduler (port-mix by bytes) | **1.6 – 10.3%** |
| statically pinning `any/0` | **27.9%** |
| value-aware k=1 scheduler | 28.1% (correctly declines to swap) |

Confirmed analytically, without the simulator: under a uniform port mix the
shipped heuristic's byte-per-class signal elects the `literal` class and
rotates among `literal/0..3`, whose pinned coverage is **0.7 – 1.6%**;
`any/0` is worth **27.9%**.

**Root cause.** The scheduler matches the *port class of the traffic* and is
blind to how many of a group's rules can actually fire on that traffic. Two
facts it does not know:

1. `any`-class rules match **every** port, so the three `any` groups are
   worth 256/256/171 rules on every flow, while a port-specific group is
   worth 256 on its own ports and **zero** everywhere else.
2. Groups are uniformly ~256 rules (GROUP_MAX), so a traffic-matched group
   is never *better* than a universal one on its own ports — only equal —
   while being far worse on all others.

So at k=1 the optimal policy is "pin a universal group and never swap," and
every swap the shipped scheduler makes is a loss plus a 16 s blind window.

This does **not** invalidate AC-S3-1, which requires that the manager
hot-swaps by observed port mix — it demonstrably does, verified on silicon.
The spec never claimed the heuristic maximises coverage. This study is the
first time its *value* was measured rather than its *function*.

## What was measured vs assumed

**Measured** (real, from the vendored corpus and the S3 packing):

- the 21-group SR6 partition and every group's rule count;
- each rule's **own header predicate** — a rule counts toward a flow only if
  its dst-port token admits that port, evaluated through the daemon's SR13
  `VarTable`. This is stricter than the group's coarse port class, which
  would have inflated every denominator (a `literal` group holds rules for
  ports 21/25/445 that can never fire on a port-80 flow);
- hence V(group, port) = rules of that group that could fire on that flow;
- the 16 s fill latency (measured on silicon, `docs/notebook.md` 2026-07-29).

**Assumed, and therefore swept rather than fixed:**

| parameter | values |
|---|---|
| traffic skew (Zipf exponent over ports) | 0 (uniform), 1.2, 2.0 |
| mix stationarity | stationary, 60 s phases, 300 s phases |
| specificity weight `w` (port-specific rules valued `w`× generic ones) | 1, 3, 10 |

The verdict is unchanged in all 27 combinations. The `w` sweep is the
important one: it is the assumption most likely to overturn the result,
since it models "HTTP attack traffic is likelier to trip HTTP rules than
generic ones." Even at `w=10` the regime stays capacity-limited.

**Metric.** Byte-weighted fraction of the rules that could fire on a flow
which were resident when it arrived. A miss is not an error: SR5 guarantees
Snort sees all traffic regardless, so a miss costs coverage only, never
correctness — which is what makes aggressive paging safe to consider.

## What this does **not** say

- **It does not price A5's operational argument.** A weekly ruleset diff
  today costs 0.5–3 h of Vivado (AC-S3-3); with a loadable table it would be
  seconds, with no synthesis at all. That is a real benefit this study did
  not measure, and it is the honest remaining case for A5 — as a *build-time*
  argument, not a coverage one.
- **The traffic is synthetic.** No production capture was available. The
  structure (rules, groups, header predicates) is real; the mix is
  parameterised and swept. A real capture could shift the numbers but would
  have to overturn the structural facts above to change the verdict.
- **It assumes no per-rule firing-probability data**, because none exists
  here. The `w` sweep is a proxy for that ignorance, not a substitute for
  measuring it.
- **Fills are modelled non-blinding for k > 1** (a banked engine serves the
  old set while the new one loads) and blinding for the k=1 PR path (the
  region really is dark during reconfiguration). That asymmetry is why
  latency looks harmless at k>1 and expensive at k=1.

## Recommended actions, cheapest first

1. **Fix the scheduler's scoring** (hours, no spec change). Score candidate
   groups by rules-that-could-fire under the observed mix instead of bytes
   per port class. The value-aware variant recovers pin-level coverage and
   correctly declines pointless swaps. Until then, **pinning `any/0` beats
   the scheduler** on every traffic mix tested — that is a one-line
   operational mitigation available today.
2. **Revisit GROUP_MAX / the `any`-class split** (SR6, spec amendment).
   Uniform 256-rule groups plus a universally-relevant `any` class is what
   makes k=1 residency a dead end. Packing that co-locates universally-
   relevant rules with traffic-specific ones would raise the value of a
   single resident group without any hardware change.
3. **Treat capacity as the real lever.** k=4 already reaches 76%; k=8
   reaches 84%. If the trie proves out, k=21 is 100% and the entire
   residency question disappears.
4. **Do not open A5 on coverage grounds.** If it is opened, open it for the
   build-time argument, with that stated plainly.
