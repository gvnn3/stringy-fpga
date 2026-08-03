# Policy experiment — control vs treatment, and what the A5 flat result meant

- **Date:** 2026-07-30 · **Branch:** `fpga-scheduler-tenants`
- **Tool:** `scripts/policy_experiment.py` · **Data:**
  `policy_experiment_data.json`
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

**Scheduling separates from the best fixed set — on both non-degenerate
sets.**

| tenant set | kinds | views | LUT spread | gain at 50% budget | at 75% |
|---|---|---|---|---|---|
| control (same-kind) | 1 | 1 | 1.2× | **+156%** | +45% |
| treatment (matched) | 3 | 2 | 1.0× | **+84%** | +42% |
| treatment (skewed) | 4 | 3 | 658× | **+0%** — no separation | +0% |

Gain is larger at tighter budgets, which is what one would expect:
scheduling matters most under capacity pressure.

**The A5 flat result was interpretation (b): that workload could not
distinguish schedulers.** It was not a fact about the machine. Give the
region tenants that differ in what they want *over time*, and a
value-aware policy beats even the best fixed set by 1.4–2.6×.

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

Normalised capture; `static-set` (best fixed set) is the baseline and
`static-pin` is shown only to expose the packing confound it carries.

Control set (3 × pattern-set, 1.2× spread):

| budget | static-pin | **static-set** | greedy-value | lru | round-robin |
|---|---|---|---|---|---|
| 50% | 31.1% | **33.3%** | **85.5%** | 33.3% | 32.3% |
| 75% | 31.1% | **64.4%** | **93.5%** | 64.4% | 62.9% |

Treatment, footprint-matched (3 kinds, 2 views, 1.0× spread):

| budget | static-pin | **static-set** | greedy-value | lru | round-robin |
|---|---|---|---|---|---|
| 50% | 25.0% | **32.3%** | **59.3%** | 32.3% | 30.5% |
| 75% | 25.0% | **64.6%** | **91.7%** | 64.6% | 61.1% |

Three secondary results worth keeping:

- **LRU delivers exactly nothing (+0.0%) over the best fixed set.** It
  converges to the same resident set and then stops adapting: recency is a
  poor proxy for value when phases rotate, because LRU keeps what was
  recently useful precisely as it stops being useful. Same failure mode as
  the shipped SR10 scheduler — optimising a signal *correlated* with value
  rather than value itself.
- **Round-robin is actively worse (−2 to −6%)**, paying 21 switches to
  greedy's 3–6 to achieve it. At 13.6 s a switch, quantum-based fairness is
  unaffordable; this is the efficiency table from the tenants study showing
  up as a policy result.
- **`capture_min` matters.** Greedy reaches 84–87% *minimum* per tenant at
  75% budget, so no tenant is starved; every other policy's minimum is 0%
  — they serve a fixed subset and abandon the rest. A mean alone hides
  that, and fairness is a first-class scheduling property.

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
- **Scheduling is worth doing on this region** — 1.4–2.6× over the best
  fixed set at a 13.6 s switch cost — but *only because the phases here are
  minutes long*, which is the one regime PR can serve. `switch-cost-frontier.md`
  quantifies exactly that: the gain survives while `s/P ≲ 0.3` and is gone
  by 0.75.
- **Value-aware beats recency-aware beats fair-share**, consistently. The
  same ordering the S3 scheduler fix found, now on an independent workload.
- **Done next, in `switch-cost-frontier.md`:** the frontier is `s/P ≈ 0.3`,
  which puts JTAG PR outside every timescale below a minute and is the
  measured basis for abandoning it as the scheduling mechanism.
