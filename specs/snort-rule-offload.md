# Specification: Snort Community-Rule Offload to the PYRO PR Shell (SNORT-PF)

- **Spec ID:** `snort-rule-offload`
- **Version:** 2.0.0 (wire ingest ADOPTED — OQ-2 resolved, 2026-08-05)
- **Status:** **ADOPTED** by the owner 2026-07-27 (see §12), with the
  post-draft facts SF17–SF20 (§1.3) and the §10 OQ decisions recorded at
  adoption. Phases S1–S3 are authorized; S4 requires the further owner
  reviews noted in §6/§10.  **Wire ingest adopted 2026-08-05** (the
  OQ-2 MAJOR event, §12 v2.0.0): the wire-ingest static (PYRO R90) is
  a supported deployment mode at the measured per-engine rate.
- **Owner:** George Neville-Neil (adopted); drafted by Spec Writer 2026-07-15
- **Date:** 2026-07-27
- **Depends on:** `specs/python-regex-offload.md` (PYRO) **v3.0.0** — this spec
  reuses PYRO's shell, transport, synthesis, cache, residency, and honesty
  machinery by requirement-ID reference and adds no obligations to PYRO itself.
  (Drafted against v2.5.0; v2.7.0 deltas are SF17/SF18; the v3.0.0
  wire-ingest surface is R78.13 + R90, adopted here at v2.0.0.)

---

## 0. Summary

This document specifies **SNORT-PF**, a system that compiles the Snort 3
community ruleset (4,017 rules) into **rule-group pattern-set circuits** —
partial bitstreams for the existing `pyro_rp` reconfigurable partition on the
flashed PYRO PR shell — so the FPGA acts as a **candidate-nominating
prefilter** in front of a full software Snort.

**Central design (proposed, mirroring PYRO's v2.0.0 owner ruling):** each rule
group is compiled to a **bespoke synthesizable circuit** — rule → anchor
content chain (± clean pcre) → automaton → RTL — synthesized through PYRO's
existing R63 synthesis service into a PR bitstream and loaded via the R85 JTAG
path. There is **no programmable rule engine and no loadable rule-table blob**
in Phases S1–S3; rules change only by re-synthesis. A Phase-S4 variant bakes a
shared-prefix anchor trie as **ROM-initialized RAM fixed at synthesis** — still
a compiled circuit, deliberately designed to stay inside the v2.0.0
no-loadable-data-engine ruling. Any future *loadable*-table engine (runtime
table writes over R78) is a separate owner decision requiring an explicit
amendment (provisionally **A5**, §10 OQ-1) through change control; this draft
does **not** propose it and MUST NOT be read as authorizing it.

The FPGA **never issues a verdict** in Phases S1–S3. Each circuit implements a
**necessary condition** of its rule (the anchor content chain), with every
construct the fabric cannot evaluate (pcre backreferences, `byte_test`,
`flowbits`, sticky-buffer normalization) **dropped as a conjunct**. Dropping a
conjunct yields a strict superset of the rule's language — exactly PYRO's
R19a-sanctioned over-approximation. A `MATCH_REPLY` window becomes a
`(flow, gid:sid, offset)` **nomination**; full Snort on the host is the
re-verifier. Completeness is preserved by construction; false positives are
removed by host re-verification; **suppression** (dropping traffic on FPGA
say-so) is prohibited until the Phase-S4 differential-oracle gate passes, and
then only for the raw-anchor rule tier.

Everything runs on the shell exactly as flashed today: single PF, control
in-band as R78 Ethernet frames, AXI-Lite tied off, boundary frozen per R80,
partial bitstreams loaded via JTAG (R85), one resident child at a time (R64).

---

## 1. Ground-truth facts

Facts are numbered `SF1…` in this spec's own namespace. SF1–SF7 are shell and
toolchain facts verified in the PYRO repo; SF8–SF16 are corpus facts measured
by full parse of `snort3-community.rules` (4,017 rules; analysis script
retained with the profile). The design MUST be grounded in these; implementers
MUST NOT assume different numbers.

### 1.1 Shell, transport, and toolchain facts (verified in-repo)

- **SF1 (platform substrate).** The target is the flashed PYRO PR shell on the
  Alveo U250 (`xcu250-figd2104-2L-e`): OpenNIC built `pf=cmac=1` (one PF, one
  CMAC), user clock 250 MHz, reconfigurable partition `pyro_rp` on SLR2 at
  pblock `CLOCKREGION_X0Y9:CLOCKREGION_X3Y10`
  (`hw/dfx/platform_manifest.json`, `boundary_id
  pyro-rp-v1-axis512-tuser48-250mhz`).
- **SF2 (RP resource budget — the binding envelope).** The `pyro_rp` budget is
  **80,000 LUT / 160,000 FF / 160 BRAM36 (~0.72 MB) / 64 URAM (2.25 MB) /
  400 DSP** (`platform_manifest.json` `rp_budget`). *This, not the device-wide
  U250 totals (~9 MB BRAM / 46 MB URAM), is the capacity envelope for every
  resident circuit.* All sizing in this spec closes against SF2.
- **SF3 (frozen thin boundary; CMAC tied off).** Per PYRO R80 the static↔RP
  boundary is exactly clk/rstn + one 512-bit AXIS slave (QDMA H2C) + one
  512-bit AXIS master (QDMA C2H), `tuser = {dst,src,size}`; AXI-Lite is tied
  off; the **CMAC datapath is tied off** — no live-traffic path reaches
  `pyro_rp`. Adding any boundary signal (e.g. a CMAC RX tap) is a
  **static-shell change**: new flash, new locked DCP (R82b), every cached
  partial invalidated, `boundary_id`/`static_shell_id` churn (R81), and it is
  currently out of PYRO's scope (§12 of the PYRO spec). See SF7 and Risk 2.
- **SF4 (control protocol).** All device control is in-band over PYRO R78
  Ethernet frames (EtherType `0x88B5`) on the `onic` netdev named by
  `PYRO_DEVICE_IFACE` (no default; fail-closed — PYRO F3/R68 as amended by
  A4.5(b)). Total frame ≤ **1518 bytes** (R78.9); `MATCH_REQUEST` corpus
  ≤ **1474 bytes** per frame (R78.6); `MATCH_REPLY` carries ≤ 61 24-byte LE
  `pyro_match` entries (R78.7); the protocol is lockstep request/reply and the
  child responder is single-threaded (R78.11). There is no PYRO-level
  fragmentation; the host chunks and reassembles by `start_off` (R41).
- **SF5 (PR load path and measured build costs).** Partials load via
  **JTAG/`hw_server`** (R85; ICAP/MCAP deferred). Measured on this host
  (`hw/dfx/build/_partials.out`): PR in-context link of the ID-stub child —
  opt 1:28, place 14:51, route 9:36, write_bitstream 1:45 ≈ **~30 min wall**,
  peak ~9.4 GB RAM; consistent with `pr_job_timeout_s = 3600` (R84). Static
  shell rebuild (`_static.out`): place 20:45 + route 13:34 + write_bitstream
  14:47 ≈ **~50 min wall**. A partial bitstream is **~4.06 MB**
  (`hw/dfx/build/partials/id_stub.bit`, 4,055,892 bytes).
- **SF6 (generator cost model — calibrated).** `pyro/hdl/estimator.py` prices
  a generated circuit at **`LUTs ≈ 320 + 4·states + 2·edges`** (harness
  intercept 320, +64 for R45a perf counters), **FFs ≈ 512 + 1·state**.
  Measured whole-child circuits run **197–410 LUTs post-route**
  (`docs/vivado-toolchain.md` §6), of which ~200–320 is per-child harness
  floor that amortizes across a pattern set. The generator datapath is
  **1 byte/cycle** (`DATAPATH_BYTES = 1`) ⇒ 250 MB/s engine ceiling at SF1's
  250 MHz. Classifier caps: `MAX_PATTERNS = 256`, `MAX_REPEAT = 255`
  (`pyro/_classify.py`, PYRO R13 minimums).
- **SF7 (known static timing violation — load-bearing for any future inline
  phase).** PYRO R73a.5 records a **permanent post-route setup violation,
  WNS = −0.427 ns on `txoutclk_out[0]` inside OpenNIC's `cmac_usplus` IP**,
  accepted only because the CMAC datapath is tied off. Any phase that carries
  traffic on the CMAC (a line-rate inline filter) makes this violation
  load-bearing; closing it is a **gating prerequisite** for that phase, and
  there is no evidence today that it is closable. (Graft from the Angle-3
  analysis; see §9 Out of scope and Risk 2.)

### 1.1a Post-draft facts (adopted with 1.0.0, 2026-07-27)

Measured between the 0.1.0 draft and adoption; each supersedes the named
clause of SF4–SF7 where they conflict. None changes an SR obligation.

- **SF17 (P2d char-dev transport supersedes SF4's throughput ceiling).**
  PYRO v2.7.0 (amendments B1/B2) binds a QDMA ST char-dev data plane
  carrying the same R78-framed payloads in jumbo (9,556 B) frames:
  measured **2.3 GiB/s zero-loss at 1 queue** on this host (2026-07-26,
  `docs/notebook.md`). SF4's 1,518 B lockstep control path remains the
  control transport; Risk 2's "tens of MB/s, frame-RTT-bound" scan-rate
  bound is superseded for the daemon path. **Constraint:** the EQDMA soft
  IP silently drops H2C packets when ≥2 queues are active (no `tuser_err`;
  AMD support case filed 2026-07-27, `docs/amd-support-case-eqdma-h2c-loss.md`).
  SNORT-PF SHALL bind the daemon transport to **1 queue** and carry the
  loss-accounting watchdog until the AMD case resolves; completeness (SR3)
  over a silently lossy transport is otherwise unprovable.
- **SF18 (8 B/cycle engine harness supersedes SF6's 1 B/cycle ceiling).**
  The P2e x4 frame-parallel child (DATAPATH_BYTES=8, 4 cores) is built and
  measured (fmax 260.8 MHz, on silicon 2026-07-26). Group circuits SHOULD
  target the 8 B/cycle harness (~2 GB/s per core-column at 250 MHz); the
  SF6 LUT cost model is calibrated for 1 B/cycle and MUST be recalibrated
  at AC-S2-2 before it is trusted for 8 B/cycle pattern sets.
- **SF19 (SF7's CMAC violation is smaller than recorded).** The
  2026-07-26 static rebuild closed to **WNS −0.015 ns** in the same
  waivable `txoutclk_out[0]` group (vs −0.427 ns at drafting; both runs
  documented in `docs/notebook.md`). This revises OQ-2's feasibility
  input only; the CMAC datapath remains tied off and out of scope.
- **SF20 (JTAG swap cost, current numbers).** Partial load measured at
  **17 s (single-core, ~4.06 MB) to 44.7 s (x4, ~5.09 MB)** including the
  automatic in-band wedge recovery. SR10's rotation scheduler MUST treat
  a swap as costing ~45 s and apply hysteresis so swap time stays small
  relative to residency time.
- **SF21 (AC-S2-2 measured group costs — SF6 recalibrated, 2026-07-28).**
  The $HTTP_PORTS/0 group (253 slots / 256 rules, 4,400 states / 4,147
  byte edges deduped, dpb=1) built through the real PR flow against the
  locked counter-fixed static: **10,147 LUTs / 5,681 FFs post-route,
  fmax 250.44 MHz, pr_verified, met_timing, 53 min** — **12.7 % of the
  80,000-LUT PR budget** (estimator predicted 26,982 LUTs, R74-conservative
  by 2.7×). PR gates at the same identity discipline: N=32 → 7,800 LUTs /
  254.84 MHz (46 min); N=64 → 7,994 LUTs / 252.21 MHz (45 min). Marginal
  cost ≈ **10.6 LUTs per slot** (~1.4 LUT/byte-edge) on a fixed ~7.6k-LUT
  wrapper+harness floor; build time is static-shell-dominated (46→53 min
  from N=32→253). The 256-bit pend priority encoder cleared timing with
  0.44 MHz margin at N=253 — dpb=1 confirmed as the group width; dpb=8
  groups remain uncalibrated (SF18 caveat stands).
- **SF22 (wire-ingest static closes timing LIVE — SF7/SF19 superseded,
  2026-08-05).** The OQ-2 wiretap static (CMAC-0 RX arbitrated into
  `pyro_rp`, PYRO R90) meets timing with the CMAC datapath live:
  overall WNS +0.020 ns, `txoutclk_out[0]` +0.104 ns (vs −0.427 at
  SF7 drafting, −0.015 waivable tied-off at SF19), pr_verify PASS —
  the R80 boundary survives, partials re-link.  The "closure of SF7"
  precondition this spec priced for any inline phase is MET.
- **SF23 (wire path end-to-end + per-packet timing on silicon,
  2026-08-05).** With the wiretap static flashed and CMAC near-end
  loopback: R78.13 semantics verified at ~245k frames/s over 8.9M
  frames with zero-slack counters (seen == scanned + drops exactly);
  50/50 captured wire MATCH_REPLYs model-exact with live epoch
  attribution.  Per-packet: engine scan floor 5.05 cyc/B (1.29 us
  per 64-B frame), 6.36 cyc/B with 3 nominations (1.63 us); whole
  wrapper arrival→reply 493 cycles (1.97 us, sim on the validated
  RTL); host MATCH RTT 19 us median.  Per-engine bound ~477k 64-B
  frames/s (~30.5 MB/s): 64-B line rate is ~31x beyond one engine —
  banking, not feasibility, is the remaining gap.

### 1.2 Corpus facts (`snort3-community.rules`, 4,017 alert rules, full parse)

- **SF8 (protocol/header shape).** tcp 3,637 (90.5%), udp 211, icmp 125,
  ip 22, http 20, ssl 2. Destination port: `$HTTP_PORTS` 2,001, `any` 765,
  `$ORACLE_PORTS` 291, then literal-port families (25→121, 21→86, 111→63,
  445→49, 139→46, 143→34, 53→28, …). Net/port variables in use: ~13
  (`$EXTERNAL_NET` 3,947, `$HOME_NET` 2,556, `$HTTP_SERVERS` 954,
  `$SQL_SERVERS` 318, …). **95 rules (2.4%) are decidable from the L3/L4
  header alone** (79 icmp itype/icode, 10 tcp, 5 udp, 1 ip).
- **SF9 (literal-prefilter viability).** **3,896 / 4,017 rules (97.0%)** have
  a usable positive anchor literal (declared `fast_pattern` in 2,168, else the
  longest positive content). **26 rules (0.6%) are never
  literal-prefilterable** (pcre-only, byte_test/byte_jump-only, or
  negated-content-only) and must be always-forwarded to Snort. Unique
  best-anchor literal set: **3,199 literals / 57,849 bytes**.
- **SF10 (anchor-length distribution, n = 3,896).** min 1, p10 4, p25 7,
  **median 12**, p75 20, p90 35, **max 214**, mean 16.5. 213 rules (5.5%)
  anchor on < 4 bytes (high nomination rate on random traffic; must be gated
  by their port predicate).
- **SF11 (buffer visibility — the soundness boundary).** Best-anchor buffer:
  **raw/pkt_data 1,759**; HTTP-textual buffers (uri/header/body/cookie/method)
  1,893; file_data 239; dce_stub_data 5. *(Amended 1.0.1: sid 42886's anchor
  follows `http_header:field user-agent`, which in Snort 3 IS a sticky-buffer
  selection — the value narrows the normalized-header cursor to one field —
  so its content is evaluated in the inspector-normalized User-Agent field,
  not pkt_data; 1,760/1,892 → 1,759/1,893. The valued `sip_method:`/
  `sip_stat_code:` forms remain genuine match options and stay raw.)* **2,258
  rules (56%) match only
  inside inspector-normalized sticky buffers** (http_uri 1,692, http_header
  556, file_data 263, …) that exist only after Snort's HTTP inspector
  (dechunk, gunzip, %-decode). A raw-byte prefilter can be blinded by encoding
  for those rules — hence nomination-only for them, forever, absent in-fabric
  normalization (out of scope). Only **733 rules** are fully expressible as
  raw-buffer content-only checks.
- **SF12 (stream semantics).** `flow:established` (or similar) appears in
  **3,618 rules (90%)**: matching is over the reassembled TCP stream. A
  raw-frame prefilter MUST carry a per-flow overlap tail of
  **max_anchor − 1 = 213 bytes** (SF10) to keep completeness across segment
  boundaries, or restrict claims to per-chunk nomination.
- **SF13 (pcre subset).** 1,080 pcre occurrences across 1,027 rules.
  **Clean/NFA-compilable: 799 (74.0%)**; backreferences 239 (22.1%, dominated
  by the mechanically-rewritable `(\x22|\x27)…\1` quote idiom); lookaround 42
  (3.9%, mostly `(?!\n)` — also mechanically rewritable); zero
  possessive/atomic/recursion/conditional. **111 occurrences carry the Snort
  `R` (relative) flag** — anchored at the previous content match, i.e. plain
  literal⋅regex concatenation in an NFA. Of the clean patterns, **63 count
  ≥ 256** in bounded repeats (p90 432, max 1,075) and are rejected by SF6's
  `MAX_REPEAT = 255` today; their rules degrade to anchor-only circuits.
- **SF14 (AC-trie sizing over the best-anchor set).** Trie states:
  8-byte cap **11,078** (~0.68 MB bitmap-compressed at ~64 B/state); 16-byte
  cap **21,841** (~**1.33 MB**); uncapped **38,700** (~2.48 MB). Against
  SF2's 2.25 MB URAM: the 16-byte cap **fits**; the uncapped trie does not
  fit URAM alone (needs URAM+BRAM ≈ 3.0 MB — likely infeasible with routing
  overhead). A 16-byte cap loses almost nothing (median anchor 12 B, SF10).
- **SF15 (ruleset update cadence).** The community ruleset is distributed as
  one monolithic file replaced wholesale, typically weekly-ish; release diffs
  touch tens of rules; occasional emergency single-rule additions.
- **SF16 (deployment variables are site config, not rule content).**
  `$HTTP_PORTS`/`$HOME_NET` etc. (~13 vars, SF8) change per site with no rule
  change. This is the same fact-vs-configuration distinction the owner just
  ratified in PYRO A4 (F2/F3 demoted to configuration): variables MUST be
  treated as data, never baked into bitstreams (SR13).

---

## 2. Definitions

- **Rule:** one Snort 3 `alert` rule: header predicate + ordered option chain.
- **Anchor:** the rule's best positive literal (SF9): the declared
  `fast_pattern` content if any, else the longest positive content.
- **Prefilter circuit:** the generated circuit for a rule — its anchor content
  chain (optionally fused with a clean `R`-flagged pcre, SF13) with all
  non-compilable options **dropped as conjuncts**. Recognizes a strict
  superset of the rule's language (PYRO R19a discipline).
- **Rule group:** an ordered set of ≤ `GROUP_MAX` rules (SR6) compiled into
  one **pattern-set circuit** — N automata sharing one harness — and
  synthesized into one partial bitstream. PYRO §2 already defines a generated
  circuit as recognizing "exactly one HW-eligible pattern **(or one pattern
  set)**"; SNORT-PF is the first exerciser of the pattern-set clause.
- **Nomination:** a `(flow 5-tuple, gid:sid, byte offset)` candidate emitted
  by the host filter daemon when a `MATCH_REPLY` window (PYRO R78.7) maps
  through the group's sidecar table to a rule. Nominations are additive
  metadata for the software Snort; they never suppress or drop traffic in
  Phases S1–S3.
- **Suppression:** using FPGA non-match to *withhold* traffic (or rules) from
  Snort evaluation. Prohibited until the Phase-S4 gate (SR17).
- **Tier:** a rule's disposition class (SR2): `header-only`,
  `anchor-compilable` (sub-tiered `raw-anchor` vs `normalized-buffer`), or
  `always-forward`.
- **Sidecar table:** the host-side `pattern_id → (gid:sid, tier, soundness
  class)` map carried in the group's manifest (never in the fabric).
- **Filter daemon:** the host process that taps traffic, evaluates header
  predicates against the variable table (SF16), chunks flow payloads into
  `MATCH_REQUEST`s, and maps replies to nominations.

---

## 3. Architecture

```
snort3-community.rules
        │  Stage 1: parse + triage (host; reuses PYRO classifier discipline,
        │           R8/R10/explain() shapes)                          [SR1,SR2]
        ▼
per-rule prefilter IR:  header-predicate ⊗ content-chain ⊗ [clean-pcre]
        │  Stage 2: conjunct-drop lowering                            [SR3-SR5]
        ▼
rule-group pattern-set RTL  (pyro.hdl.generator extended: N automata,
        │   shared harness, pattern_id = group-local rule index)      [SR6,SR7]
        ▼
PYRO R63 synthesis service (`pr_bitstream` jobs): OOC synth → in-context
link vs locked static DCP (R82b) → pr_verify (R82c) → partial + manifest
        ▼
bitstream cache (PYRO R4/R63a; key = canonical group bytes ⊕ versions) [SR9]
        ▼
residency manager (PYRO R64, single-tenant) → JTAG load_partial
(R85/R86) → slot 1 (R87)                                              [SR10]
        ▼
host filter daemon: header classify (var table, host-side) → chunk flows
into MATCH_REQUESTs (≤1474 B, 213 B overlap tail) → map MATCH_REPLY
pattern_id → gid:sid → nominate to Snort (R19 re-verify)         [SR11-SR15]
```

Key properties, stated once:

1. **Zero new RTL for the first milestone.** Phase S1 pushes a real Snort
   literal through the existing, unmodified `pyro.hdl.generator` +
   `rp_wrapper` + R63 Vivado `pr_bitstream` flow. The pattern-set extension
   (Stage 3) is the only substantive new hardware code in the whole plan, and
   it reuses the `rp_wrapper` frame parser and multi-beat `MATCH_REPLY` path
   unchanged.
2. **No spec-machinery forks.** SNORT-PF consumes PYRO's synthesis service
   (R63), cache tiers (R4), residency arbitration (R64), synthesis-failure
   semantics (R65), JTAG load (R85/R86), slot map (R87), frame protocol
   (R78/R79), and probe/honesty predicates (R71/R83/R83a) by reference.
3. **The FPGA output is advisory.** Every `MATCH_REPLY` entry's `verified`
   flag is advisory (R78.7); the daemon re-verifies before acting, identical
   to PYRO's R47a/R19 path. Snort continues to see **all** traffic in Phases
   S1–S3; the prefilter only prioritizes and attributes.

### 3.1 As-built block diagram (informative, 2026-08-04)

The Stage flow above is the S1 lineage (per-group partial bitstreams).
As built today, S2/S3 replaced the per-group PR step with the **A5
table-programmable overlay engine**: one partial bitstream is loaded
once, and rule groups are thereafter swapped as table writes over the
wire protocol (13.9 ms + 0.150 ms/KB measured, vs ~13.6 s JTAG PR).

```
snort3-community.rules (4,017 rules)
     |
     v
SR1/SR2 triage -> prefilter IR -> pack_groups (SR6)     [host, cached]
     |                                       ... per rule group ...
     v
S2 group emitter: anchors -> AC trie + failure links + pre-unioned
outputs -> A5 table image (53-55 B/state; TABLE_ID = CRC-32C of image)
     |
     v
pyro.device.load_table: TABLE_BEGIN / DATA* / COMMIT (kinds 0x08-0x0D)
     |   sequential-only DATA; fail-closed commit gate (expected CRC,
     |   engine id, state cap) -> EPOCH increments only on success
     v
+----------------- U250: pyro_rp (PR-loaded ONCE) --------------------+
| rp_wrapper (R78 + A5 FSM, CSR bridge)                               |
|      <->  pyro_overlay_engine: table-programmable Aho-Corasick      |
|           256-bit bitmap + popcount transitions; 50 URAM +          |
|           108 BRAM36; 40,960-state capacity; 5-10 cyc/B @ 250 MHz   |
|           R45a perf counters (CYCLES/BYTES, per-scan)               |
|           every MATCH_REPLY carries the producing table's EPOCH     |
+---------------------------------------------------------------------+
     |
     v
nominations: pattern_id -> gid:sid  (SR5: superset, advisory)
     |
     v
daemon / scheduler: residency + group swap policy (OS-scheduler
primitives: table = process image, EPOCH = generation, swap = context
switch)          -> full software Snort re-verifies (R19)
     |
     v
telemetry collector (pyro-telemetry/1): switch times, scans,
nominations by sid, OVF, perf -> JSON / Prometheus -> dashboard
```

### 3.2 OQ-2 wire-scan path (ADOPTED 2026-08-05; see §12 v2.0.0)

The §10 OQ-2 feasibility spike (design record:
`docs/studies/wire-rate-spike.md`) answered its question YES on
this shell: a wiretap static v2 (CMAC RX arbitrated into `pyro_rp`
alongside QDMA H2C, inside the unchanged R80 boundary) closed
timing at 250 MHz with the CMAC datapath LIVE (overall WNS
+0.020 ns; the SF7/SF19 `txoutclk` group at +0.104), and pr_verify
confirms existing partials re-link rather than redesign.  The
child-side wire-scan path is built and sim-verified; its interface
is normative in **PYRO R78.13** (v2.8.0): raw frames tagged tuser
src `0x0040` bypass the R78 codec, are scanned whole against the
active A5 table, reply only on nomination (`MATCH_REPLY`, own
slot, wire-reply `seq`, SR14' epoch, `status` bit2 `WIRE`), and
are dropped-and-counted when no table is committed or a load is
open; `PERF_REPLY` carries additive wire counters.

Scope, as adopted (v2.0.0, owner decision 2026-08-05): wire ingest
is a **supported deployment mode** — the wire-ingest static (PYRO
R90) alongside the host-fed static, each with its own locked DCP
and partial-compatibility domain (R82b; partials are valid only
against the static they were linked to).  G4 was executed, the
path verified end-to-end on silicon (SF22/SF23).  The adoption
claim ceiling is the MEASURED per-engine rate (~30.5 MB/s, ~477k
64-B frames/s bound; ~245k f/s demonstrated); **line-rate inline
filtering remains out of scope** (§9) pending engine banking,
which carries its own version event.  The host-tap deployment
model (SF17) remains fully supported.

---

## 4. Requirements

Requirements are numbered `SR1…` in this spec's own namespace.

### 4.1 Rule triage and tier classification

- **SR1 (parse + triage).** SNORT-PF SHALL parse every rule into header
  predicate + ordered option chain and classify it into exactly one tier:
  `header-only` (SF8: 95 rules — no payload circuit; host or trivial
  comparator handles), `anchor-compilable` (SF9: 3,896 rules), or
  `always-forward` (SF9: 26 rules — permanently CPU, reusing PYRO R65's
  "permanently fallback" disposition as "permanently CPU"). Triage MUST be
  deterministic given the rules file and MUST emit a machine-readable triage
  report (rule count per tier, per-rule tier + reason) analogous to PYRO
  `explain()` (R31).
- **SR2 (tier classification is change-controlled, not code).** The
  `anchor-compilable` tier is sub-tiered into exactly two values:
  `raw-anchor` (anchor targets raw/pkt_data, SF11 as amended: 1,759 rules)
  vs `normalized-buffer` (2,137 rules, of which 5 are dce_stub_data — those
  5 are inside `normalized-buffer`, visible per-buffer in the report's
  histograms, never a third sub-tier value). A buffer key selects its
  sticky buffer in both bare and valued form (`http_header:field x`,
  `http_param:"x"` narrow the cursor within the buffer); the valued
  `sip_method:`/`sip_stat_code:` forms are match options, not selections
  (amendment 1.0.1). Because the raw/normalized boundary is the **soundness
  boundary for any future suppression claim** (SF11, Risk 3), the
  classification rules for this sub-tier SHALL be stated in this spec's §4.1
  and changed only by spec amendment (§11), never by silent code change.
  *(Grafted from the Angle-1 proposal's governance treatment, per judges.)*

### 4.2 Circuit semantics (the R19 inheritance)

- **SR3 (conjunct-drop over-approximation — completeness is absolute).** Each
  prefilter circuit SHALL implement a necessary condition of its rule: the
  anchor content chain with `offset/depth/distance/within` lowered to the
  generator's existing counter/assert machinery, `nocase` via the existing
  ASCII case-fold (PYRO R15), and optionally the rule's pcre **fused** when it
  is clean (SF13) and `R`-flagged (literal⋅regex concatenation). Every
  construct the fabric cannot evaluate (backreferences, lookaround,
  `byte_test`/`byte_jump`/`byte_extract`/`byte_math`, `flowbits`,
  `isdataat`, negated content, sticky-buffer normalization) SHALL be
  **dropped as a conjunct**, and sticky-buffer `^` anchors SHALL be
  **stripped** (SF13: buffer start is unobservable in raw bytes). Dropping a
  conjunct or stripping an anchor only ever enlarges the recognized language;
  **completeness in the sense of PYRO R19 — the circuit MUST NOT fail to fire
  on any input its rule's compiled conjuncts would fire on — is mandatory and
  absolute.** Bounded repeats beyond `MAX_REPEAT = 255` (SF13: 63 patterns)
  degrade the rule to its anchor-only circuit — still sound.
- **SR4 (manifest declares over-approximation classes — PYRO R19c).** Each
  group manifest SHALL declare its over-approximation classes:
  `dropped_conjuncts`, `anchor_strip`, `case_fold`, `chunk_overlap`, plus
  per-rule dropped-option lists, so re-verification cost is attributable and
  the SR16 oracle can target the weakest classes. An exact circuit declares
  an empty set.
- **SR5 (nomination, never verdict — Phases S1–S3).** The system SHALL treat
  every FPGA match as a **nomination** re-verified by full Snort before any
  operator-visible effect (PYRO R19a clause 2 applied with Snort as the
  verifier). Non-matches SHALL have **no effect whatsoever** — no rule or
  traffic is withheld from Snort. Suppression is governed solely by SR17.

### 4.3 Grouping, generation, capacity

- **SR6 (stable grouping).** Rules SHALL be packed into groups keyed by
  destination-port class first (SF8: `$HTTP_PORTS`, `$ORACLE_PORTS`,
  literal-port families, `any`), then stable sid-order packing to
  `GROUP_MAX = 256` rules/group (SF6 `MAX_PATTERNS`) → **~16 groups** for the
  current corpus. Deleted sids leave **tombstones** rather than triggering
  repack, so a ruleset diff dirties the minimum number of groups (SR9).
  `GROUP_MAX` MAY be raised (512–1024) only on the basis of measured
  post-route utilization (SR8, PYRO R74 discipline), by spec amendment.
- **SR7 (pattern-set generator).** `pyro.hdl.generator` SHALL be extended to
  emit N automata sharing one harness, with `pattern_id` (PYRO R47's 4-byte
  field) carrying the group-local rule index, resolved to gid:sid only via the
  sidecar table (§2). The `rp_wrapper` frame parser, multi-beat `MATCH_REPLY`,
  and slot handling are reused unchanged — one wrapper per group child. The
  child's `rp_child_id` SHALL be the low-32 of the group's canonical hash
  (PYRO R78.5a), and the daemon SHALL cross-check it before attributing any
  nomination (SR14).
- **SR8 (capacity closes against SF2, with measurement).** Estimated group
  cost uses SF6's calibrated model: median 12-byte anchor ≈ ~72 marginal
  LUTs/rule; a 256-rule group ≈ **20–30K LUTs** — inside the 80K budget with
  ≥ 2.5× headroom; FFs (~1/state over a 512 floor) nowhere near 160K; BRAM
  trivial (1,474 B corpus buffer + result ring). These estimates MUST be
  recalibrated from real post-route utilization of the first built group
  (Phase S2) before any `GROUP_MAX` increase, per PYRO R74's
  estimate-vs-measured discipline (`real ≤ est` conservatism).

### 4.4 Synthesis, cache, residency, updates

- **SR9 (cache key and incremental updates).** Each group is one PYRO R63
  `pr_bitstream` job. The bitstream-cache key SHALL be the group's
  **canonical rule-group byte serialization** ⊕ `generator_version` ⊕
  `toolchain_version` ⊕ shell/PR-region version — the PYRO R4/R47a key shape
  with the canonical group bytes standing in for `pattern_bytes`. Deployment
  variables MUST NOT appear in the key or the RTL (SR13). With SF15's cadence
  and SR6's stable grouping, a weekly diff dirties 1–3 groups (~0.5–3 h at
  SF5's measured ~30–60 min/group); a full rebuild is ~16 jobs, ~2 concurrent
  (R63c host-RAM bound, SF5's ~9.4 GB peak) → overnight. Job dedup is PYRO
  R63a, unchanged.
- **SR10 (residency and the synthesis window).** Residency is single-tenant
  slot 1 (PYRO R64/R87). The residency manager SHALL schedule the resident
  group by observed traffic port mix (Phase S3). During any synthesis window
  or while a group is non-resident, affected rules are simply **unfiltered**
  — Snort sees all traffic anyway (SR5), so correctness is unaffected; only
  prefilter coverage dips. Emergency rules are always-forward instantly (zero
  latency, CPU handles them) and gain FPGA coverage one synthesis later.
  Synthesis MUST NOT block or slow the traffic path (PYRO R53/R63
  asynchrony).

### 4.5 Host filter daemon

- **SR11 (transport).** The daemon SHALL speak PYRO R78 verbatim: chunk flow
  payloads into `MATCH_REQUEST`s (corpus ≤ 1,474 B, `start_off` continuation,
  `OVF` resume per R41/R78.7), and consume `MATCH_REPLY` 24-byte LE
  `pyro_match` entries. No new frame kinds are defined by this spec.
- **SR12 (overlap tail).** For `flow:established` rules (SF12) the daemon
  SHALL prepend a per-flow overlap tail of **213 bytes** (max anchor − 1,
  SF10) to each chunk so anchors split across chunk/segment boundaries are
  never missed. The tail length is derived from the loaded ruleset's max
  anchor and recorded in the group manifest.
- **SR13 (deployment variables stay host-side, as data).** The daemon SHALL
  evaluate header predicates (SF8) against a host-side variable table
  (~13 entries: port lists + CIDR sets, SF16) and offer payloads only to the
  groups they are relevant to. Variables MUST NOT be compiled into any
  bitstream (it would key the cache on site config and kill reuse, violating
  the A4 fact-vs-configuration discipline). Header-only rules (SF8: 95) are
  decided entirely here.
- **SR14 (identity before attribution).** Before attributing any nomination,
  the daemon SHALL confirm the resident child via `ID_REQUEST`/`ID_REPLY`
  `rp_child_id` == low-32 of the expected group hash (SR7, PYRO R78.5a). A
  mismatch routes to "no resident group" (all traffic unfiltered), never to
  misattributed nominations.
- **SR15 (evasion tripwires — grafted from Angle 1).** The daemon SHALL
  maintain cheap host-side tripwires that force nomination-independent
  forwarding for flows where anchor visibility is doubtful: observed
  `Content-Encoding: gzip`/`deflate`, chunked transfer encoding, and
  %-encoding density above a threshold on HTTP flows. Tripwired flows count
  against a per-class counter in the stats surface. This is R19a discipline
  applied at the flow level: when the over-approximation premise (anchor
  visible in raw bytes) is known-broken, stop relying on the prefilter for
  that flow.

### 4.6 Verification and honesty

- **SR16 (differential oracle — Snort is to SNORT-PF what CPython is to
  PYRO).** The primary oracle (PYRO R54 pattern) SHALL replay recorded pcaps
  and assert `alerts(Snort, full traffic) == alerts(Snort, nominated ∪
  tripwired ∪ always-forward traffic)` for any tier proposed for suppression.
  Any diff is a completeness defect, full stop. Property fuzzing (PYRO R55
  pattern) SHALL mutate corpora to split anchors across the 1,474 B chunk
  boundary and across TCP segment boundaries (exercising SR12's tail), and
  under case permutation, asserting nomination still fires.
- **SR17 (suppression gate).** Suppression is prohibited except: (a) tier =
  `raw-anchor` (SR2), (b) the SR16 oracle passes on adversarial (encoded,
  fragmented, case-permuted) corpora for that tier, (c) per-flow overlap SR12
  is active, and (d) the owner has approved the suppression pilot by
  amendment. All four are required. Phases S1–S3 claim **nomination only**.
- **SR18 (SKIP-honesty — PYRO R71/R83 inherited verbatim).** Every
  on-hardware claim in §6's acceptance criteria is gated on
  `device_usable == true` via the PYRO live probe; when false, the clause
  SHALL record the **canonical R83 SKIP string** (`device_usable=false — …`
  with the fixed-order condition enumeration, item 1
  `transport: PYRO_DEVICE_IFACE not configured`), and clauses needing the PR
  flow append the R83a `pr_flow_present=false — <first unmet R83a condition>`
  reason. **A SKIP is never a PASS; no simulated PASS ever.** A silent pass
  by degenerate measurement is a defect (PYRO R71 discipline).
- **SR19 (stats surface).** The daemon SHALL export counters mirroring PYRO
  AC-3-4's shape: nominations by tier, re-verification confirms/rejects
  (false-positive rate per SR4 class), tripwire hits per class (SR15),
  resident-group identity + occupancy, unfiltered-window seconds, and
  synthesis lifecycle counters (delegated to PYRO R61/R66).

---

## 5. Coverage (against the 4,017-rule corpus)

- **Anchor-compilable prefilter circuits**
  - Rules: 3,896
  - %: 97.0%
  - Disposition: FPGA nominates; Snort verifies
- **— of which raw/pkt_data anchors (SR2 `raw-anchor`)**
  - Rules: 1,759
  - %: 43.8%
  - Disposition: nomination sound even vs encoding; only suppression-eligible
    tier (SR17)
- **— HTTP-textual / file_data / dce anchors (`normalized-buffer`)**
  - Rules: 2,137
  - %: 53.2%
  - Disposition: nomination best-effort (SF11); tripwires cover the known-
    blind cases (SR15)
- **Header-only**
  - Rules: 95
  - %: 2.4%
  - Disposition: host var-table match (SR13); no payload circuit
- **Never literal-prefilterable**
  - Rules: 26
  - %: 0.6%
  - Disposition: always forwarded (SR1)

**Honest headline constraint:** compilable coverage is 97%, but **instantaneous
resident coverage is one group ≈ 256 rules ≈ 6.4%** under single-tenant
residency (SR10, PYRO R64) until Phase S4's ROM-baked shared-trie child — a
16-byte-capped trie of all 3,896 anchors at ~1.33 MB (SF14) against SF2's
2.25 MB URAM — is proven. Until then the prefilter's value is bounded by
group-rotation heuristics (Risk 1).

---

## 6. Phased delivery plan

Phases are `S1…S4`; acceptance criteria are `AC-S<phase>-<n>`. Every
on-hardware clause carries SR18's SKIP discipline.

### Phase S1 — One real rule through the existing flow (~days, zero new RTL)

Pick one rule from the 733 raw-content-only set (SF11) whose content chain is
a single literal (e.g. an SMB/FTP literal rule); compile it through the
**existing, unmodified** generator + `rp_wrapper` + R63 Vivado `pr_bitstream`
flow; JTAG-load; drive pcap-derived corpora through `MATCH_REQUEST`s using the
existing `scripts/pyro_hw.py` transport.

- **AC-S1-1.** The rule's anchor compiles through the unmodified PYRO flow to
  a manifest with `payload_kind == "pr_bitstream"` and `pr_verified == true`
  (PYRO R82c), plus SR4 over-approximation classes and the sidecar gid:sid
  map. LIVE on this host (host-side artifact checks need no device).
- **AC-S1-2.** On hardware (`device_usable ∧ pr_flow_present`, else SR18
  SKIP): the loaded child answers `ID_REPLY` with the SR7 non-zero
  `rp_child_id`; a positive pcap flow produces `MATCH_REPLY` windows that map
  to the correct gid:sid and survive Snort re-verification; a negative corpus
  produces zero nominations. (PYRO R78, R85, R86; SR3, SR5, SR14)

### Phase S2 — Pattern-set groups + triage + oracle harness (~2–3 weeks)

- **AC-S2-1.** SR1 triage over the full rules file reproduces SF8–SF11's tier
  counts exactly (95 / 3,896 / 26) and emits the machine-readable report.
  LIVE (pure host).
- **AC-S2-2.** The SR7 pattern-set generator builds one 256-rule
  `$HTTP_PORTS` group; estimated resources (SR8) are within budget and the
  estimate is conservative vs post-route reality (`real ≤ est`, PYRO R74);
  measured utilization is recorded to recalibrate `GROUP_MAX`. Synthesis
  clauses LIVE with the toolchain; load/serve clauses on-hardware or SR18
  SKIP.
- **AC-S2-3.** The SR16 differential-oracle harness runs on recorded pcaps
  against the software model (device-free) and on hardware when usable: zero
  completeness diffs for the built group; SR12 chunk-boundary and
  case-permutation fuzzing passes.

### Phase S3 — Full build, residency, incremental updates

- **AC-S3-1.** All ~16 groups build; the SR10 residency manager hot-swaps by
  observed port mix; SR14 identity checks gate every attribution; SR19 stats
  export is live.
- **AC-S3-2.** Content-chain lowering (`offset/depth/distance/within`) and
  clean-pcre fusion (SF13) are in the group circuits, each declared in SR4
  classes and covered by the oracle.
- **AC-S3-3.** A simulated weekly ruleset diff (tens of rules) dirties ≤ 3
  groups (SR6 tombstone stability, SR9 cache keys); untouched groups hit the
  cache (PYRO R63a dedup); the unfiltered window during rebuild is bounded and
  visible in SR19 stats.

### Phase S4 — ROM-baked shared-trie child + suppression pilot (claim gate)

- **AC-S4-1.** A single child with the 16-byte-capped shared-prefix anchor
  trie (SF14: 21,841 states ≈ 1.33 MB, ROM-initialized at synthesis — no
  runtime table writes, respecting the v2.0.0 ruling) meets timing at 250 MHz
  on SLR2 and fits SF2's URAM budget; all 3,896 anchors resident at once.
- **AC-S4-2.** Suppression pilot for the `raw-anchor` tier only, gated on all
  four SR17 conditions including owner approval; the SR16 oracle passes on
  adversarial corpora before any suppression is enabled.

---

## 7. Update model (informative summary; normative parts in SR6/SR9/SR10)

Measured, not guessed (SF5): ~30–60 min wall per group re-synthesis, ~9.4 GB
peak, 2 concurrent workers. Weekly diff → 1–3 dirty groups → 0.5–3 h. Full
rebuild (toolchain/generator bump) → ~16 jobs overnight. During any window,
Snort sees all traffic (SR5/SR10) — the fallback tier is "the CPU does what it
does today", the exact analogue of PYRO's cold-tier fallback (R4).
Emergency rules: instant CPU coverage, FPGA coverage one synthesis later.

---

## 8. Top risks (against evidence)

1. **Single-tenant residency caps instantaneous coverage at ~6.4%.**
   `platform_manifest.json` defines exactly one `pyro_rp` (SF1); discrete
   comparator circuits for all 3,896 anchors need ~58K states ≈ ~350K LUTs by
   SF6's model — 4.4× the 80K budget. Until AC-S4-1 proves the ROM-baked trie
   (1.33 MB vs 2.25 MB URAM — plausible, unmeasured; bitmap-decode timing at
   250 MHz on SLR2 untested), value is bounded by group rotation.
2. **No line-rate path exists on this shell, and building one is expensive.**
   The CMAC datapath is tied off (SF3); the only ingress is the lockstep R78
   protocol (SF4), which bounds effective scan rate far below even the
   250 MB/s engine ceiling (SF6) — likely tens of MB/s, frame-RTT-bound. Real
   inline filtering needs a CMAC tap crossing the R80 boundary: a static-shell
   reflash that invalidates every cached partial (R82b), re-runs the ~50 min
   static P&R (SF5), makes SF7's −0.427 ns `cmac_usplus` violation
   load-bearing with no evidence it is closable, and moves work PYRO §12
   declares out of scope — a MAJOR-bump decision priced in §9/OQ-2, not
   assumed by any phase here. *(Grafted from Angle 3's cost accounting.)*
3. **Sticky-buffer blindness makes suppression unsound for 53% of rules,
   silently.** 2,131 anchors target inspector-normalized buffers (SF11);
   gzip/chunked/%-encoding hides the anchor from raw bytes — a false
   *negative*, the one failure R19 forbids absolutely. Nomination-only
   deployment is immune; the moment operators see "97% coverage" they will
   want suppression. Mitigations are structural: SR2 makes the tier boundary
   change-controlled, SR15 tripwires the known-blind flows, SR17 quadruple-
   gates suppression behind the adversarial oracle and owner approval.
4. **The pattern-set clause is unexercised.** PYRO's §2 pattern-set language
   and `MAX_PATTERNS = 256` (SF6) exist but no pattern-set circuit has ever
   been built; SR8's 20–30K-LUT group estimate rests on the calibrated
   single-pattern model. AC-S2-2's measure-then-recalibrate step is the
   containment.

---

## 9. Out of scope

- **On-FPGA verdicts / alert generation.** The FPGA nominates; Snort alerts.
  (Suppression pilot excepted, SR17-gated, Phase S4 only.)
- **A programmable rule engine or loadable rule/AC-table blob** (runtime
  `TABLE_WRITE`-style frame kinds). This is precisely what PYRO's v2.0.0
  MAJOR decision removed; reintroducing it for Snort tables requires an
  explicit owner amendment (provisional **A5**, OQ-1) through §11 — it is not
  proposed here. The Phase-S4 trie is ROM-baked at synthesis precisely to
  stay on the compiled-circuit side of that line.
- **Line-rate inline filtering / bump-in-the-wire.** The CMAC tap
  itself was adopted at v2.0.0 (PYRO R90, SF22/SF23) — it needed NO
  R80 boundary break, and SF7's violation closed live.  What stays
  out of scope is the LINE-RATE claim: one engine measures ~30.5
  MB/s (~477k 64-B frames/s) vs 14.88M frames/s at 100GbE — the
  ~31x gap is engine banking, a future MAJOR event of its own.
  Wire ingest today is nomination at the per-engine rate with
  honest, counted drops (adapter RX FIFO) beyond it.
- **In-fabric TCP reassembly, HTTP normalization, decompression** (gzip,
  chunked, %-decode). SF11's normalized-buffer rules keep Snort as verifier
  forever; the overlap tail (SR12) is the only stream accommodation.
- **Non-community rulesets** (Talos subscriber, ET Pro) and Snort rule
  actions other than `alert` (the corpus contains only `alert`, SF8-adjacent).
- **`flowbits` cross-rule state, `byte_*` arithmetic, backreference/lookaround
  pcre on the fabric** — permanently host-side conjuncts (SR3), mirroring
  PYRO §5.2's permanent-fallback constructs.
- **Multi-tenant residency** (`pr_partitions > 1`) — same deferral as PYRO
  §12.

---

## 10. Open questions for the owner

**Decisions recorded at adoption (2026-07-27):**

- **OQ-1 — DEFERRED.** No A5 slot is opened now. S3's measured swap
  cadence and unfiltered-window stats (SR19) are the evidence base for
  revisiting at the S3 review. Until then, rules change only by
  re-synthesis (SR9) and residency by JTAG rotation (SR10/SF20).
- **OQ-2 — FEASIBILITY SPIKE ONLY, after S3.** No line-rate commitment.
  SF19's −0.015 ns result makes the spike worthwhile; the host-tap
  deployment model with SF17's 2.3 GiB/s ceiling is the accepted shape
  for S1–S4.  *Outcome (2026-08-05): spike run and answered YES —
  see §3.2 and `docs/studies/wire-rate-spike.md`; child interface
  normative at PYRO R78.13.  G4 executed same day (owner), wire
  path verified end-to-end on silicon (SF23).*  **RESOLVED —
  ADOPTED (owner decision 2026-08-05, the MAJOR event this OQ
  priced): wire ingest is a supported deployment mode at the
  measured per-engine rate (PYRO R90, §3.2, §12 v2.0.0); line rate
  remains out of scope (§9) pending banking.**
- **OQ-3 — DECIDE AT AC-S2-2.** `GROUP_MAX` stays 256 until the first
  group's post-route utilization is measured, per SR8/R74.
- **OQ-4 — NOMINATION-ONLY THROUGH S3.** The SR17 suppression pilot
  remains gated on its four conditions including a fresh owner approval
  at S4; operators are told 97% is *compilable* coverage, not resident
  coverage.
- **OQ-5 — pcap replay for S1–S2; AF_PACKET tap for S3** on the control
  binding of the single PF, accepting the control/data interleave (or a
  second capture NIC if measurement shows the blind window matters —
  decided at S3 design time).

The original questions are retained below for their analysis:

- **OQ-1 (A5 boundary).** Does the owner want a future amendment slot opened
  for a *loadable*-table AC engine (seconds-scale rule pushes over R78,
  vs SR9's 30–60 min re-synthesis)? This draft deliberately excludes it to
  stay inside the v2.0.0 ruling; the Angle-1 analysis shows the update-latency
  upside and the governance path if wanted. Decision needed only before any
  such work, not before S1–S4.
- **OQ-2 (line-rate ambition).** Is a static-shell v2 with a CMAC tap on the
  roadmap at all? If yes, SF7's −0.427 ns violation should be investigated
  (feasibility spike) *before* any S4-successor planning, since all inline
  value is gated on it. If no, SNORT-PF's ceiling is the host-tap deployment
  model and that should be stated as accepted.
- **OQ-3 (GROUP_MAX).** May `GROUP_MAX` rise above 256 (toward 512–1024) if
  AC-S2-2's measured utilization supports it, given PYRO R13 advertises
  `MAX_PATTERNS ≥ 256` as a *minimum*? Fewer groups → higher instantaneous
  coverage pre-S4.
- **OQ-4 (suppression appetite).** Is the SR17 suppression pilot wanted at
  all, or should SNORT-PF be nomination-only permanently? Risk 3 argues the
  operational pressure will arrive; deciding now sets expectations.
- **OQ-5 (traffic tap).** Which host tap feeds the filter daemon (AF_PACKET
  mirror, existing SPAN, pcap replay only)? S1–S2 need only pcap replay; S3's
  port-mix residency policy needs a live tap definition.

---

## 11. Change control

This document follows PYRO §13 verbatim: ambiguities are resolved by amending
this spec and bumping its version (SemVer; MAJOR for interface/AC breaks,
MINOR for added requirements, PATCH for clarifications). Agents derive work
from this file and the PYRO spec, not from each other. Additionally:

- This spec MUST NOT modify PYRO obligations; anything SNORT-PF needs from
  PYRO beyond referenced requirements is a PYRO amendment first.
- SR2's tier-classification rules and SR17's suppression gate are
  amendment-only surfaces (no silent code change).
- **Adoption gate:** version 0.x is a draft; the owner adopts by bumping to
  1.0.0 with an adoption note in the changelog. Until then, no phase is
  authorized.

---

## 12. Changelog

- **2.0.0** (2026-08-05) — ***Wire ingest ADOPTED* (MAJOR — the
  version event OQ-2 priced; owner decision, recorded per §11),
  claude, owner-directed.* The wire-ingest static (PYRO v3.0.0 R90)
  becomes a supported deployment mode alongside the host-fed shell.
  Adds SF22 (live-CMAC timing close — SF7/SF19 superseded; the
  "closure of SF7" precondition MET) and SF23 (end-to-end silicon
  verification + per-packet timing: 5.05 cyc/B scan floor, 1.63 us
  matched 64-B frame, ~477k frames/s per-engine bound).  §3.2
  rescoped from spike to adopted; §9's inline bullet narrowed —
  the CMAC tap is in scope, the LINE-RATE claim is not (the ~31x
  banking gap keeps its own future MAJOR event); §10 OQ-2 marked
  RESOLVED-ADOPTED.  Depends-on raised to PYRO v3.0.0.  No phase
  obligations added: S1-S4 and the host-tap model are unchanged;
  wire ingest is an additional deployment surface with honest,
  counted drops beyond the per-engine rate.
- **1.0.4** (2026-08-05) — *§3.2 OQ-2 wire-scan spike outcome (PATCH —
  informative), claude, owner-directed.* Records the OQ-2 spike
  verdict (timing closed with a live CMAC; R80 boundary survives;
  wire-scan path built and sim-verified) and annotates the §10 OQ-2
  decision with its outcome.  The child-side interface is normative
  in PYRO R78.13 (v2.8.0), not here, per §11's no-PYRO-modification
  rule.  Spike scope is unchanged: no phase obligation, no
  deployment-model change; adoption of wire ingest stays a MAJOR
  event on both specs.
- **1.0.3** (2026-08-04) — *§3.1 as-built block diagram (PATCH —
  informative), claude.* Adds an informative diagram of the system as
  deployed: the A5 overlay-engine path (table swaps at 13.9 ms +
  0.150 ms/KB replacing per-group PR), the resident `pyro_rp`
  contents, nomination/re-verify flow, and telemetry. Notes that the
  §3 Stage flow is the S1 lineage. No normative change.
- **1.0.1** (2026-07-27) — SR2 amendment (per §11: the raw/normalized
  boundary changes only by spec amendment). (a) Snort 3 grammar correction:
  `http_header:field <name>` (and valued `http_param:`/`http_uri:` etc.)
  is a sticky-buffer *selection* whose value narrows the cursor within the
  buffer — sid 42886 moves raw-anchor → normalized-buffer; SF11 best-anchor
  histogram 1,760/1,892 → 1,759/1,893; the valued `sip_method:`/
  `sip_stat_code:` forms remain match options (their five rules stay raw).
  (b) Clarifies SR2's sub-tier vocabulary as exactly two values —
  `raw-anchor` (1,759) and `normalized-buffer` (2,137, incl. the 5
  dce_stub_data, which are reported per-buffer, not as a third sub-tier).
  §5 coverage table updated accordingly. Corpus snapshot unchanged.
- **1.0.0** (2026-07-27) — **ADOPTED by the owner** (per the §11 adoption
  gate; decision recorded in `docs/phase2-snort-plan.md` and
  `docs/prompts.md` 2026-07-27). Adds §1.1a post-draft facts SF17–SF20
  (P2d char-dev transport + EQDMA 1-queue constraint; 8 B/cycle harness;
  revised CMAC WNS; measured JTAG swap costs) and records the §10 OQ
  decisions (A5 deferred; line-rate spike-only post-S3; GROUP_MAX at
  AC-S2-2; nomination-only through S3; pcap→AF_PACKET tap). Phases S1–S3
  authorized.
- **0.1.0** (2026-07-15) — Initial draft for owner review. Compile-to-circuits
  architecture (rule groups as PR partials on the unmodified PYRO shell),
  grounded in the 4,017-rule corpus profile and the flashed-shell facts;
  grafts: change-controlled tier classification + A5 governance boundary
  (Angle 1), evasion tripwires (Angle 1), explicit pricing of the R80
  break / SF7 timing gate for any inline phase (Angle 3). Not adopted.
