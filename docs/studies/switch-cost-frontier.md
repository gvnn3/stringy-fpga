# The switch-cost frontier — and the decision to abandon JTAG

- **Date:** 2026-07-30 · **Branch:** `fpga-scheduler-tenants`
- **Tools:** `scripts/switch_cost_frontier.py`, `scripts/policy_experiment.py`
- **Data:** `switch_cost_frontier_data.json`, `policy_experiment_data.json`

## Verdict

**The frontier is a ratio, not a pair of numbers.** Scheduling gain depends
only on `s/P` — switch cost over demand-phase length — with **zero** spread
across absolute phase lengths from 30 s to 600 s. So one constant governs
the whole design space.

| `s/P` | gain over the best fixed set |
|---|---|
| ≤ 0.01 | **+47%** (saturated — switching is effectively free) |
| 0.1 | +41% |
| 0.2 | +35% |
| **0.3** | **+25%** ← worth doing |
| 0.5 | +13% |
| **0.75** | **+3%** ← gone |
| ≥ 1.0 | +5 – 13%, stdev 6 – 18 → indistinguishable from zero |

**Scheduling pays while `s/P ≲ 0.3`, degrades through 0.5, and is gone by
0.75.** Averaged over 9 randomised traces of 14 phases each.

### What that requires of the switch mechanism

At `r* = 0.3`, the mechanism must deliver `s ≤ 0.3 · P`:

| you want to react on a timescale of | max switch cost | JTAG PR (13.6 s) |
|---|---|---|
| 1 ms (per-packet) | 0.3 ms | **45,000× too slow** |
| 1 s | 0.3 s | **45× too slow** |
| 10 s | 3 s | **4.5× too slow** |
| 1 min | 18 s | works, barely |
| 10 min | 3 min | works comfortably |

**JTAG partial reconfiguration can only serve phases of a minute or
longer.** Everything the project actually set out to do — "swap FPGA
programs at run time based on what data is seen on the NIC" — lives at
seconds or below, where PR is one to four orders of magnitude too slow.

## Decision: JTAG PR is abandoned as the scheduling mechanism

Not as a *loading* mechanism — it remains how a bitstream reaches the
device, and the 21 built group children stay valid artifacts. It is
abandoned as the mechanism a **scheduler** drives, for three independent
reasons now measured rather than asserted:

1. **It is outside the frontier for every interesting timescale** (this
   document). Minute-scale rotation is the only regime it serves.
2. **It cannot host deadline work at all.** Packet-scale slack is
   microseconds against a 2 × 13.6 s evict-and-restore, so a deadline
   tenant admitted under PR is *non-preemptible* and the region becomes
   statically partitioned rather than scheduled
   (`fpga-scheduler-tenants.md` §2b). That is a functional impossibility,
   not an expense.
3. **Its cost is irreducible.** 83% is JTAG bitstream transfer and the
   remainder is a fixed 2.15 s recovery tax; cost is flat in design size
   (128× more slots costs −0.3% time), so there is no algorithmic saving
   to find. The only lever is not using it.

An overlay — rule content written at run time as data, into a circuit that
stays resident — pays neither the transfer nor the recovery. A table write
over the existing char-dev path at the measured 2.3 GiB/s puts a 1 MB
table at ~0.4 ms, i.e. `s/P ≈ 0.4` at *millisecond* phases and effectively
zero above that. **That is the whole space, versus PR's slowest corner.**

## Corrections to earlier numbers in this series

Two errors were found and fixed while producing this, both mine, both
inflating results in the same direction:

**(a) The `static-pin` baseline was unfair, and its use overstated the
policy experiment by roughly 4×.** `static-pin` holds exactly *one*
tenant while every other policy may hold several, so the comparison
conflated *packing more* with *scheduling better*. The tell: apparent
gain stayed above +100% even at switch costs so large that nothing ever
switched. The honest baseline is the best **fixed set** that fits the
budget, chosen in hindsight and never changed — anything that beats it is
genuinely dynamic.

Corrected policy-experiment headline (`s = 13.6 s`, phases ~2 min, so
`s/P ≈ 0.11`):

| set | vs `static-pin` (old, inflated) | vs best fixed set (honest) |
|---|---|---|
| control (same-kind) | +181% | **+156%** at 50% budget, **+45%** at 75% |
| treatment (matched) | +177% | **+84%** at 50% budget, **+42%** at 75% |
| treatment (skewed) | +194% | **+0%** — no separation |

The skewed set collapsing to zero is the clearest confirmation: it was
measuring packing all along, exactly as its degeneracy diagnostic said.
Note also that gain is *larger at tighter budgets* — scheduling matters
most under capacity pressure, which is what one would expect and is
reassuring rather than surprising.

**(b) Phase alignment aliased the frontier.** The first sweep produced a
non-monotonic curve — gain at `s/P = 3` exceeding `s/P = 2` — because
`phase_trace` ignored its seed (star order was a deterministic `i % n`)
and phases were uniform, so switches synchronised with phase boundaries
identically on every run. Fixed by randomising the star sequence and
jittering phase lengths ±30%, then averaging over 9 seeds. The corrected
curve is monotone to the knee, and the residual +5–13% beyond `s/P = 1`
sits within one to two standard deviations of zero.

The substantive conclusions survived both corrections; the magnitudes did
not, and the skewed-set result reversed outright.

## What this does not claim

- The frontier constant `r* ≈ 0.3` comes from one tenant set and one
  demand model (rotating phases, randomised order, jittered length). The
  *shape* — gain saturating below `s/P ≈ 0.01` and vanishing near
  `s/P ≈ 0.75` — is robust across the conditions tested; the exact
  threshold should be re-measured on real traffic.
- The overlay's 0.4 ms figure is arithmetic from a measured transport
  rate, not a measured overlay. Nothing here proves an overlay closes
  timing or that its table-write path works; §1's requirement is what it
  would have to hit.
