# Rule-packing study — can SR6 packing raise coverage without new hardware?

- **Date:** 2026-07-30 · **Branch:** `rule-packing-study`
- **Tool:** `scripts/rule_packing_study.py`
- **Origin:** recommendation 2 of `docs/studies/a5-working-set.md`.
- **Result in one line:** the recommendation as written was **wrong**, but
  checking it found a **free +14% relative** correction and a **2.7×**
  capacity lever that the spec already permits on measured evidence.

## 1. The original recommendation is refuted

Recommendation 2 proposed co-locating universally-relevant rules with
traffic-specific ones "to raise the value of a single resident group."
That cannot work, and the reason is arithmetic rather than empirical:

With `GROUP_MAX = 256` **rules**, every port already has a group in which
**all 256 rules fire** — the three `any`-class groups fire on every port by
construction, and each port-specific group fires fully on its own ports.
Coverage at k=1 is therefore `256 / achievable(port)` for *any*
arrangement. Re-packing changes **which** rules are resident, never **how
many**. Measured on the current packing:

| port | achievable rules | best group's firing rules | already maximal? |
|---|---|---|---|
| 80 | 2,700 | 256 | yes |
| 22 | 689 | 256 | yes |
| 25 | 823 | 256 | yes |
| 443 | 720 | 256 | yes |
| 1521 | 996 | 256 | yes |

Implemented and measured anyway (P3 below), because refuting an idea with
an argument is weaker than refuting it with a number: blending universals
evenly through every port-specific group scores **30.3%** — *identical* to
simply fixing the slot bound without any blending. The mixing contributes
exactly zero.

## 2. What checking it found instead

| # | packing | groups | max rules/group | max slots | worst-group LUTs (est.) | cov k=1 | cov k=4 | weekly-diff dirty |
|---|---|---|---|---|---|---|---|---|
| P0 | baseline — ≤256 **rules** (today) | 21 | 256 | 256 | 21,261 | 26.6% | 72.8% | 2/21 |
| P1 | **slot-bound — ≤256 slots** | 20 | 385 | 256 | 21,261 | **30.3%** | 70.0% | 2/20 |
| P2 | anchor-aware, ≤256 slots | 20 | 400 | 256 | 21,557 | 30.9% | 73.0% | **6/20** |
| P3 | mixed-class, ≤256 slots | 21 | 385 | 256 | 21,261 | 30.3% | 69.7% | 2/20 |
| P4a | slot-bound ≤512 slots | 13 | 734 | 512 | 29,538 | 59.5% | 76.9% | 2/13 |
| P4b | **slot-bound ≤1024 slots** | 9 | 1,064 | 1024 | 45,078 | **71.1%** | 88.9% | 2/9 |

### P1 — the free correction: bound by slots, not rules

`GROUP_MAX = 256` bounds **rules**, but the hardware limit (SF6
`MAX_PATTERNS`) bounds **automata**, i.e. **slots** — and anchors dedup, so
the two are not the same number. Today's packing stops at 256 rules while
the circuit still has slot headroom, leaving ~10.5% of the corpus's dedup
on the table.

Bounding by slots instead holds up to **385 rules in the same 256-slot
circuit**: identical worst-case area (21,261 LUTs — the binding group is
anchor-byte-heavy, not slot-heavy), identical SR6 stability (2 groups
dirtied by the weekly diff), one fewer group overall, and **+3.7 points of
coverage (26.6% → 30.3%, +14% relative) for no hardware change at all.**

This is a one-word change to what the bound counts.

### P2 — anchor-aware packing: rejected on stability

Sorting by anchor before packing maximises dedup rather than taking it
incidentally, and it does buy a little more (30.9% vs 30.3%). It also
**triples the rebuild cost of a weekly ruleset diff** — 6 groups dirtied
instead of 2, because the sort key is the anchor, so any anchor change can
migrate rules across groups. Trading ~4 hours of Vivado per weekly update
for 0.6 coverage points is a bad deal. Rejected.

### P4 — the real lever: wider groups

SR6 says `GROUP_MAX` "MAY be raised (512–1024) only on the basis of
measured post-route utilization (SR8, PYRO R74 discipline), by spec
amendment." That measurement now exists, and it is generous: a 253-slot
group is 10,323 LUTs, 12.7% of the 80K budget.

At 1024 slots the corpus packs into **9 groups of ~1,064 rules**, and
single-group coverage goes **26.6% → 71.1%** — a 2.7× improvement — with
the worst group at an estimated 45,078 LUTs (**56% of budget**) and SR6
stability unchanged at 2 dirty groups per weekly diff. At k=4 it reaches
88.9%.

This is the same conclusion the A5 study reached from the other direction:
**coverage is capacity-limited.** The difference is that this lever is
available *now*, on the existing single-tenant region, without the S4 trie
and without A5's overlay.

## 3. Caveats — what is estimated rather than measured

- **The LUT figures are extrapolation, not synthesis.** They scale
  byte-edges by a single measured anchor point (`$HTTP_PORTS/0`: 4,147
  byte-edges → 10,323 LUTs post-route at dpb=1) and fold the shared wrapper
  into the slope, which *over*-estimates larger groups — conservative for
  the "does it fit" question, but no substitute for a build.
- **Timing at 512/1024 slots is unknown, and this is the real risk.** OF-1
  already found six of 21 groups missing the R73a.1 gate by −0.005…−0.120 ns
  on RM↔static boundary paths at ~10K LUTs. A 45K-LUT group is a harder
  placement problem, and the 256-bit pend register plus priority encoder
  must widen to 1024 — priority-encoder depth is the specific thing likely
  to bite the critical path.
- **Any packing change is a one-time full rebuild.** Every group's
  canonical bytes change, so the entire corpus re-synthesises once: ~20 h
  for P1, ~9–13 h for P4. Steady-state weekly cost is unchanged.
- **Coverage uses the A5 study's traffic model** — real ruleset structure,
  swept synthetic traffic. Same caveat, same reasoning.

## 4. Recommendations

1. **Adopt P1 now** (SR6 amendment, one-word semantic change: `GROUP_MAX`
   counts slots, not rules). Free coverage, no new hardware, no stability
   cost, no timing risk — the circuit is the same size it always was.
2. **Spike P4b's timing before committing to it.** Build one 1024-slot
   group through the existing PR flow with the timing-closure flags. If it
   closes, the coverage case is decisive (2.7×) and it makes OQ-3's answer
   obvious. If it does not close, P4a (512 slots, 59.5%) is the fallback and
   is a much smaller step from today's 256.
3. **Reject P2 and P3.** P3's premise is refuted analytically and
   empirically; P2's gain does not pay for its rebuild cost.
4. **Correct recommendation 2 of the A5 study**, which this supersedes.
