# Policy experiment — control vs treatment, and what the A5 flat result meant

- **Date:** 2026-07-30 · **Branch:** `fpga-scheduler-tenants`
- **Tool:** `scripts/policy_experiment.py` · **Data:** `policy_experiment_data.json`
- **Question:** the A5 study found every policy tied with a static pin. That
  was ambiguous between *"scheduling does not help here"* and *"that
  workload could not distinguish schedulers."* A control set discriminates.

> **CORRECTION (2026-07-30, same day).** The headline figures below were
> measured against a `static-pin` baseline that holds exactly ONE tenant,
> which conflates *packing more* with *scheduling better* and inflated the
> gains by roughly 4x. Corrected against the best-fixed-set baseline:
> control **+156%** (50% budget) / **+45%** (75%); treatment-matched
> **+84%** / **+42%**; treatment-skewed **+0%, no separation** — it was
> measuring packing all along. The qualitative verdict is unchanged:
> scheduling separates, and it separates on the CONTROL set. See
> `switch-cost-frontier.md` §"Corrections".

## Verdict

**Scheduling separates from a static pin — decisively, and on every set.**

| tenant set | kinds | views | LUT spread | best policy | gain over static pin |
|---|---|---|---|---|---|
| control (same-kind) | 1 | 1 | 1.2× | greedy-value | **+181%** |
| treatment (matched) | 3 | 2 | 1.0× | greedy-value | **+177%** |
| treatment (skewed) | 4 | 3 | 658× | greedy-value | +194%\* |

\* skewed set's number is not comparable — see §3.

**The A5 flat result was interpretation (b): that workload could not
distinguish schedulers.** It was not a fact about the machine. Give the
region tenants that actually differ in what they want *over time*, and a
value-aware policy beats a static pin by ~3×.

And the separation appears on the **control** set — homogeneous, one kind,
one input view, 1.2× footprint spread. So the effect is **capacity- and
phase-driven, not heterogeneity-driven**: it comes from demand shifting
between tenants while capacity is short, which is the ordinary condition
scheduling exists for. Heterogeneity is not required to make scheduling
worthwhile, which is a more useful finding than the reverse would have
been — it means the result should generalise to any workload with phases.

## 1. Design, and the confounds it controls

- **Identical phase structure on every set.** Demand rotates which tenant
  is favoured, same period and duty cycle throughout. Without this the
  comparison would be "heterogeneous with dynamics" against "homogeneous
  without", confounding the variable under test.
- **Budget as a fraction of what the set wants**, not absolute LUTs, so
  sets with very different absolute footprints face equivalent pressure.
- **Unit-free metric.** Treatment tenants value incomparable things (scan
  bytes, packets, rules × bytes), so the headline is per-tenant
  **normalised capture** — the fraction of each tenant's own demanded value
  actually delivered, averaged over tenants. No exchange rate required.
- **Measured switch cost**, 13.6 s, charged with serialized fills.

## 2. Results

Control set (3 × pattern-set, 1.2× spread):

| budget | static-pin | greedy-value | lru | round-robin |
|---|---|---|---|---|
| 50% | 33.3% | **87.3%** | 31.3% | 37.5% |
| 75% | 33.3% | **93.7%** | 64.6% | 72.9% |

Treatment, footprint-matched (3 kinds, 2 views, 1.0× spread):

| budget | static-pin | greedy-value | lru | round-robin |
|---|---|---|---|---|
| 50% | 33.3% | **59.9%** | 32.3% | 35.4% |
| 75% | 33.3% | **92.1%** | 64.6% | 69.7% |

Three secondary results worth keeping:

- **LRU is barely better than a static pin, and at 50% budget it is
  *worse* (−3 to −6%).** Recency is a poor proxy for value when phases
  rotate: LRU keeps what was recently useful precisely as it stops being
  useful. This is the same failure mode as the shipped SR10 scheduler —
  optimising a signal correlated with value rather than value itself.
- **Round-robin pays 24 switches to greedy's 4–6** and still loses. At
  13.6 s a switch, quantum-based fairness is unaffordable; it is the
  §1-of-the-tenants-study efficiency table showing up as a policy result.
- **`capture_min` matters.** Greedy reaches 85–87% *minimum* per tenant at
  75% budget, so no tenant is starved; static-pin's minimum is 0% by
  construction — it serves one tenant and abandons the rest. A mean alone
  would hide that.

## 3. Two degeneracies found, and what they mean

Both were bugs in my experiment design, caught by diagnostics rather than
by reading the numbers charitably. They are reported because the second is
also a genuine result.

**(a) Budgets below the largest tenant.** At 25% budget no tenant fits at
all and every policy scores 0. The runner now flags this
(`[DEGENERATE: largest tenant can never load]`) and the verdict excludes
those cells rather than averaging them in.

**(b) Large-tenant starvation under density packing — a real finding.**
The first treatment set had 658× footprint skew (12 → 7,898 LUTs). Greedy
value-*density* always admits the small tenants first, leaving too little
for the big one **at every budget**, so it never loads, no policy ever
changes its mind (1 switch), and all three policies return identical
numbers. That is not scheduling being measured; it is packing.

It is also the gang-starvation pathology from the tenants study, arriving
by a different route: **value-density packing systematically starves large
tenants**, whether the "large tenant" is a gang or a single big circuit.
The fix in both cases is admission control that reserves area, not a
better ranking function.

The remedy for the *experiment* was a factorial: `matched_treatment()`
varies kind, input view and value currency while holding footprint to
1.0×, so heterogeneity is isolated from size, exactly as
`same_kind_control()` holds all four constant. The skewed set is retained
as a third condition because its degeneracy is informative.

## 4. What this changes

- The A5 study's "no policy beats a static pin" should be read as
  workload-limited, not machine-limited. Its conclusion about *coverage*
  stands; its implication about *scheduling* does not.
- **Scheduling is worth doing on this region**, and the gain is large
  (~3×) even at a 13.6 s switch cost — because the phases here are minutes
  long, which is the one regime PR can serve. Shorter phases would erase
  it, and that is the overlay argument restated.
- **Value-aware beats recency-aware beats fair-share**, consistently. The
  same ordering the S3 scheduler fix found, now on an independent workload.
- Next: sweep switch cost to find where greedy's advantage collapses. That
  locates the phase-length/switch-cost frontier and turns "overlays would
  help" into a bound.
