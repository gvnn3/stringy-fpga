# FPGA scheduling apparatus — switch-cost curve and a four-tenant workload

- **Date:** 2026-07-30 · **Branch:** `fpga-scheduler-tenants`
- **Tools:** `scripts/switch_cost_curve.py`, `pyro/sched/tenants.py`
- **Purpose:** apparatus for the real research question — *can OS scheduler
  techniques manage FPGA functionality at run time?* The Snort ruleset is a
  workload for that question, not the objective.

## 1. The switch-cost curve, measured

12 real switches on silicon, phase-decomposed, across designs spanning
**2 to 256 slots** (128×) and 3.50 to 5.09 MB of bitstream:

| artifact | slots | MB | program | recover | probe | **total** |
|---|---|---|---|---|---|---|
| `$FILE_DATA_PORTS/0` | 2 | 3.50 | 11.47 | 2.15 | 0.02 | **13.64** |
| `literal/1` | 182 | 3.54 | 11.37 | 2.16 | 0.02 | **13.55** |
| `$ORACLE_PORTS/0` | 256 | 3.68 | 11.42 | 2.15 | 0.03 | **13.60** |
| PYRO x4 regex | — | 5.09 | 12.15 | 2.15 | 0.02 | **14.32** |

### The finding: switch cost is flat in design size

**128× more slots costs −0.3% switch time.** Partial-reconfiguration cost
does not depend on what you load — only on how many bitstream bytes there
are, and PR bitstreams are sized by the *region*, not the design (all 30
built partials fall within 3.50–3.72 MB; the 5.09 MB regex child is a
different wrapper configuration).

A usable model:

```
switch_cost  ≈  11.3 s  +  0.44 s/MB  +  2.15 s recovery      (± 0.1 s)
                  \____ JTAG program, 83% ____/    \_ fixed tax _/
```

Three consequences for scheduling:

1. **There is no cheap-versus-expensive circuit.** Unlike a CPU context
   switch, whose cost tracks the working set, every FPGA switch here costs
   the same. A scheduler cannot economise by preferring "small" tenants —
   that lever does not exist.
2. **83% is JTAG transfer**, so the only lever on PR switch cost is fewer
   bitstream bytes. There is no algorithmic saving available.
3. **The recovery tax is fixed at 2.15 s and remarkably stable** (spread
   0.03 s over 12 runs). It exists because a raw JTAG program leaves the
   child unreachable (R85a). An overlay table write would not reconfigure
   the fabric at all, so it pays *neither* the 11.3 s program nor the
   2.15 s recovery.

### What this says about quanta

Scheduling efficiency is `q/(q+s)`. At the measured `s = 13.6 s`:

| target efficiency | required quantum (PR) | required quantum (overlay, s≈0.5 ms) |
|---|---|---|
| 50% | 13.6 s | 0.5 ms |
| 90% | 122 s | 4.5 ms |
| 99% | 22 min | 50 ms |

That table is the whole argument for overlays, stated in scheduling terms
rather than coverage terms. **At 13.6 s per switch you cannot implement a
scheduler — only a slow rotation**, which is exactly what S3 has and
exactly why no policy beat a static pin. Time-slicing, demand paging with a
meaningful fault rate, priority preemption and speculative prefetch are not
*expressible* at this switch cost; they become expressible four orders of
magnitude down. Overlays are the apparatus, not an optimisation.

## 2. A workload that can distinguish schedulers

The earlier experiments came out flat partly because Snort rule groups are
homogeneous: 21 groups of ~256 rules, ~equal value, interchangeable. A
workload of identical jobs cannot tell one policy from another.

`pyro/sched/tenants.py` defines four **heterogeneous** tenants:

| tenant | kind | input view | slots | est LUTs | value driven by | latency |
|---|---|---|---|---|---|---|
| `pyro-regex` | regex | host buffers | 1 | 12 | pending scan bytes | batch |
| `ip-match/src` | ip-match | full frame | 8 | 597 | packets | per-packet |
| `header-match` | header | full frame | 94 | 7,898 | packets | per-packet |
| `snortpf/<group>` | pattern-set | L4 payload | 2–256 | 102–10,323 | rules × port mix | per-flow |

**Tenants 3 and 4 needed no new RTL.** AC-S3-2's `\A.{n}` fixed-offset
lowering means "the 4 bytes at frame offset 26" is just
`(?s:\A.{26}\xc0\xa8\x01\x0a)`, and a /24 prefix is that with a wildcard
tail. The existing pattern fabric already subsumes IP and header
classification; it had simply never been asked to. That is a result in its
own right, and it is why a four-tenant mixed workload was a day's work
rather than a quarter's.

Both were verified against synthetic frames driven through the real
automaton, including the tests that matter: that a src-IP matcher does
**not** fire on the same bytes appearing in the dst field (the offset
really discriminates), and that a non-byte-aligned prefix like `/12` is
**dropped loudly** rather than silently widened to `/8` — widening would
be a false match, not an over-approximation the host re-checks.

### Data provenance, stated plainly

- **`header-match` is real corpus data**: 94 of the 95 header-only rules
  (SF8 — 79 ICMP itype/icode, plus TCP/UDP/IP), currently evaluated
  host-side by the daemon (SR13). The 95th needs a byte-class mask
  (`fragbits`) and is honestly excluded.
- **`ip-match` is structurally real, and its address list is site
  configuration** — the net-variable prefixes the daemon already evaluates,
  plus the handful of literal-IP rule headers the corpus actually carries
  (only 5 exist). It is **not** threat intelligence and is not presented as
  such.

### The scheduling problems this creates

1. **Two-dimensional packing.** Footprints span 12 to 10,323 LUTs, so
   residency is a knapsack over *area*, not a count of slots. All four
   together are 8,713 LUTs — **11% of the 80K budget** — so co-residency is
   feasible today, which means the interesting regime (demand > capacity)
   has to be created deliberately by adding tenants or shrinking the
   region. That is a knob, and having it is the point.
2. **Incomparable currencies.** `pyro-regex` values pending scan bytes;
   `ip-match` values packets; `snortpf` values rules × port mix. There is
   no natural exchange rate, and inventing one silently is precisely the
   value-blind failure the A5 study caught in the shipped scheduler. A
   heterogeneous scheduler needs the exchange rate to be an explicit,
   arguable input.
3. **Input views differ.** Frame tenants need byte 0 of the packet;
   pattern-set tenants need reassembled L4 payload; the regex tenant needs
   host buffers. In OS terms that is an address-space constraint on
   co-residency, and it is not optional — a fixed-offset matcher fed a
   payload buffer is silently wrong.

## 3. Other tenants that could use the Snort data

Ordered by how much new machinery they need. Everything in the first group
is expressible through the existing generator.

**No new RTL — fixed-offset or literal patterns:**

- **TCP-flags / fragbits matcher.** The one header-only rule we dropped,
  plus the 6 `flags:` rules. Needs a byte *class* at a fixed offset
  (`[\x02\x12]` for SYN-ish), which the automaton supports — it just isn't
  wired up in the tenant builder yet.
- **dst-port classifier.** The daemon's SR13 port-variable test as a
  circuit: `\A.{36}` + the two port bytes, one slot per port in the
  variable. Directly removes host work on every packet.
- **DNS query-name extractor.** Literal matching at the DNS payload
  offset; feeds the `dns_query` sticky-buffer rules the triage currently
  classifies as normalized-buffer.
- **TLS SNI / JA3-prefix matcher.** Fixed-offset literals in the
  ClientHello. Snort's `ssl_*` rules are in the corpus.

**Modest new RTL:**

- **The 26 always-forward rules' `byte_test`/`byte_jump` predicates.**
  These are the rules the prefilter gives up on entirely (SR1). A small
  compare-and-branch engine would take them off the CPU — and they are
  precisely the ones with no literal anchor, so nothing else can help them.
- **`detection_filter` / threshold counting.** Snort's rate limiting (3
  header-only rules use it). Per-rule counters with time windows — a
  natural small stateful tenant, and a *different* resource profile
  (memory-bound, not LUT-bound), which enriches the packing problem.
- **Clean-pcre engine.** SF13 counts 799 NFA-compilable pcres; 36 are
  already fused into group circuits, and the rest are dropped conjuncts. A
  wider-datapath regex tenant could take them as a batch.
- **Flowbits state table.** The corpus uses `flowbits` widely and the
  prefilter drops every one. A per-flow bitset is a memory tenant with a
  genuinely different footprint shape.

**The interesting ones for the research, because they break assumptions:**

- **A gang-scheduled pipeline**: `header-match` → `snortpf/<group>` →
  host. Two tenants that must be *co-resident to be useful at all*. Gang
  scheduling is a classic result and this is a real instance of it.
- **A deadline tenant.** Anything on the CMAC path would have a hard
  per-packet latency budget rather than best-effort coverage — mixing
  best-effort and deadline tenants on one region is where scheduling
  theory gets sharp.
- **A second-tenant-of-the-same-kind** (two Snort groups co-resident).
  Trivial to construct, and it isolates capacity effects from heterogeneity
  effects — the control condition for everything above.

## 4. What is now possible that wasn't

With the cost curve measured and a heterogeneous workload defined, the
experiments from the reframe are unblocked:

- **quantum-versus-switch-cost**: the curve is measured; the table in §1 is
  the first output.
- **thrashing**: needs demand > capacity, which the tenant set can now be
  made to produce (add tenants, or bound the region).
- **2D packing / fragmentation**: footprints now genuinely differ.
- **OPT comparison**: the policy harness from the A5 study transfers
  unchanged.
- **partial preemption**: still blocked on overlays — and §1's efficiency
  table is the argument for building them.
