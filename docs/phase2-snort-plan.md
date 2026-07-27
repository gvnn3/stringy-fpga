# Phase 2 implementation plan — SNORT-PF dynamic filtering (branch `phase2-snort`)

- **Date:** 2026-07-27
- **Goal (owner's statement):** swap FPGA programs at run time based on the
  Snort rules and what data is seen on the NIC.
- **Governing document:** `specs/snort-rule-offload.md` (SNORT-PF) **v0.1.0
  DRAFT — not yet adopted**. This plan proposes adopting it (with a small
  amendment slate, §2 below) and sequences the work. Per its §11 adoption
  gate, no phase is authorized until the owner bumps it to 1.0.0.
- **Corpus:** `third_party/snort3-community-rules/snort3-community.rules`
  (4,017 alert rules, GPL — license files retained alongside), unpacked
  2026-07-27 on this branch. Matches the corpus the draft spec profiled
  (SF8–SF16).

## 1. Shape of the system (as specified by the draft)

The FPGA is a **candidate-nominating prefilter** in front of full software
Snort. Rules are triaged (SR1/SR2) into header-only (95), anchor-compilable
(3,896 = 97%), and always-forward (26). Anchor-compilable rules are packed
into ~16 **rule groups** by destination-port class (SR6, ≤256 rules/group),
each group compiled to a **pattern-set circuit** — N automata sharing one
harness — through the existing PYRO R63 synthesis service into a partial
bitstream for `pyro_rp`. A host **filter daemon** (SR11–SR15) taps traffic,
evaluates header predicates against site variables, chunks flow payloads
into R78 `MATCH_REQUEST`s (213-byte overlap tail, SR12), and maps
`MATCH_REPLY` windows to `(flow, gid:sid, offset)` nominations that full
Snort re-verifies. The FPGA never issues a verdict in S1–S3 (SR5);
completeness is absolute (SR3, PYRO R19 discipline).

**The "dynamic" part — the owner's stated goal — is SR10 + AC-S3-1:** the
residency manager observes the live port mix and hot-swaps which group's
partial is resident. With one resident child (R64) and prebuilt cached
partials, a swap costs one JTAG load (measured 17–45 s). So "runtime swap
based on what the NIC sees" is **minutes-scale rotation among prebuilt
group bitstreams** — no new load mechanism required. Seconds-scale rule
pushes (a loadable-table engine, "A5") and true line-rate inline filtering
(CMAC tap) are explicitly out of scope of the draft and priced as separate
owner decisions (OQ-1/OQ-2).

## 2. What changed since the draft was written (2026-07-15) — proposed
   amendment slate for adoption

The draft depends on PYRO v2.5.0 and pre-P2d facts. Three of its planks
have moved, all in SNORT-PF's favor. Fold these in as amendments when
adopting (SemVer MINOR; none changes an SR obligation):

1. **SA-1 — transport ceiling is ~100× higher than SF4/Risk-2 assume.**
   PYRO is now v2.7.0: the P2d QDMA ST char-dev data plane (B1/B2) carries
   the same R78 payloads in jumbo (9,556 B) frames at a measured
   **2.3 GiB/s zero-loss (1 queue)** — vs the draft's "tens of MB/s,
   frame-RTT-bound" ceiling reasoned from the 1,518 B lockstep control
   path. The filter daemon should speak the char-dev transport from S2 on.
   Constraint carried with it: **multi-queue H2C silently loses packets**
   (EQDMA IP bug, AMD case filed 2026-07-27, `docs/amd-support-case-*.md`)
   — SNORT-PF binds to **1 queue** until AMD resolves; completeness (SR3)
   forbids running the prefilter over a lossy transport without the
   loss-detection the bench watchdog provides.
2. **SA-2 — engine bandwidth: 8 B/cycle datapath exists.** SF6's
   1 B/cycle ⇒ 250 MB/s ceiling predates the P2e x4 frame-parallel child
   (DATAPATH_BYTES=8, 4 cores, fmax 260.8 MHz measured). Group circuits
   should target the 8 B/cycle harness: ~2 GB/s/core-column, matching SA-1's
   transport. Cost model (SF6/SR8) must be recalibrated at AC-S2-2 anyway.
3. **SA-3 — SF7's CMAC violation shrank.** The 2026-07-26 static rebuild
   closed the CMAC clock group to **WNS −0.015 ns** (vs the recorded
   −0.427 ns; both in the waivable txoutclk group, Ethernet FCS covers it).
   This materially changes OQ-2's feasibility math for a future line-rate
   phase — it does not authorize one.

## 3. Owner decisions needed at adoption (OQ-1…OQ-5), with recommendations

| OQ | Question | Recommendation |
|---|---|---|
| OQ-1 | Open the A5 slot (loadable-table engine, seconds-scale rule pushes)? | **Defer.** Measure S3's real swap cadence first; JTAG rotation may satisfy the goal. Revisit at S3 review. |
| OQ-2 | Line-rate CMAC tap on the roadmap? | **Feasibility spike only** (given SA-3), after S3. No commitment now; host-tap ceiling is 2.3 GiB/s (SA-1), likely sufficient for the experiment series. |
| OQ-3 | Raise GROUP_MAX past 256? | Decide from AC-S2-2 measured utilization, per the spec's own R74 discipline. |
| OQ-4 | Suppression pilot wanted? | **Nomination-only through S3.** Revisit S4 after the differential oracle exists. |
| OQ-5 | Which traffic tap? | S1–S2: pcap replay. S3: AF_PACKET mirror on `ens2`'s control binding (or a second NIC/SPAN if the P2d swap contention proves awkward — see Risk b). |

## 4. Delivery sequence on this branch

Phases and ACs are the spec's (S1–S4); this is the work breakdown.

### S1 — one real rule through the unmodified flow (~days, zero new RTL)
1. `pyro/snort/` package: rule parser + SR1 triage over the full corpus,
   emitting the machine-readable tier report. **Gate: reproduce SF8–SF11's
   counts exactly (95 / 3,896 / 26)** — validates parser against the
   spec's profile before any hardware work (this is AC-S2-1 pulled early;
   it is pure host code and de-risks everything downstream).
2. Pick one single-literal rule from the 733 raw-content-only set (an
   FTP/SMB literal; selection recorded in the triage report). Compile its
   anchor through the existing generator → R63 `pr_bitstream` → JTAG load.
3. Drive positive/negative pcap-derived corpora via `pyro_hw.py` transport;
   confirm gid:sid mapping and Snort re-verification (AC-S1-1/2). Install
   software Snort 3 on nf-server06 for the verifier and oracle.

### S2 — pattern-set generator + groups + oracle (~2–3 weeks)
4. Extend `pyro.hdl.generator` for N automata/shared harness (SR7) —
   **the one substantive piece of new hardware code in the plan** and its
   riskiest item (Risk 4: the pattern-set clause has never been built).
   Build the 256-rule `$HTTP_PORTS` group first (AC-S2-2); recalibrate the
   SR8 cost model from post-route reality.
5. SR16 differential-oracle harness (pcap replay; Snort-vs-nominations
   equality) + SR12 chunk/segment-boundary fuzzing. Runs device-free
   against the software model in CI; on hardware when usable (SR18 SKIP
   discipline otherwise).

### S3 — the stated goal: full build + traffic-driven swap
6. Build all ~16 groups (overnight, 2 concurrent jobs, ~9.4 GB RAM each);
   populate the bitstream cache keyed per SR9.
7. Filter daemon: AF_PACKET tap → header classify (SR13 variable table) →
   port-mix histogram → **residency scheduler that JTAG-swaps the resident
   group when the observed mix shifts** (SR10, hysteresis to amortize the
   ~45 s swap) → char-dev MATCH pipeline (SA-1) → nomination stream →
   SR19 stats surface. Identity check before every attribution (SR14).
8. Incremental-update drill: simulated weekly diff dirties ≤3 groups
   (AC-S3-3, tombstone stability).

### S4 — (after owner review) ROM-baked shared trie: all 3,896 anchors
   resident at once (kills the 6.4%-instantaneous-coverage cap), and the
   suppression question, both explicitly re-gated on the owner.

## 5. Risks beyond the spec's §8

a. **EQDMA loss bug** (open AMD case): binds the daemon to 1 queue
   (~2.3 GiB/s ceiling) and adds a watchdog/accounting obligation to SR11;
   if AMD's answer forces a shell change, cached partials churn (R82b).
b. **Control/data plane contention:** the port-mix tap (OQ-5) wants the
   `onic` netdev while the MATCH pipeline wants the char-dev — they are
   mutually exclusive bindings of the one PF. S3 likely interleaves
   (sample mix → swap to data → stream), or uses a second capture NIC;
   decide at S3 design time, measure the blind window either way.
c. **JTAG swap fragility:** each rotation is a wedge-recover cycle
   (automatic, but adds seconds and a known failure mode); rotation
   hysteresis must make swaps rare relative to the 45 s cost.

## 6. Immediate next steps (pre-authorization)

Work items 1 (triage parser to the SF-count gate) and 5's device-free
oracle scaffolding are host-only analysis/verification of the corpus —
useful for validating the spec's own ground truths at adoption time and
authorized as analysis, not as SNORT-PF implementation. Everything past
that waits on the owner bumping the spec to 1.0.0 with the §2 slate and
§3 decisions.
