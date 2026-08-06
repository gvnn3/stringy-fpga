# Phase 2 implementation plan — SNORT-PF dynamic filtering (branch
`phase2-snort`)

- **Written:** 2026-07-27 (pre-authorization) · **Last updated:** 2026-07-30
- **Goal (owner's statement):** swap FPGA programs at run time based on the
  Snort rules and what data is seen on the NIC.
- **Governing document:** `specs/snort-rule-offload.md` (SNORT-PF), adopted
  at **1.0.0** 2026-07-27 with the §2 slate below, now at **1.0.2**
  (amendment 1.0.1 sid-42886 sub-tier; SF21 group-cost recalibration).
- **Corpus:** `third_party/snort3-community-rules/snort3-community.rules`
  (4,017 alert rules, GPL — license files retained alongside).

## 0. Status — S1, S2 and S3 are complete

- **S1 — one rule through the unmodified flow**
  - Acceptance: AC-S1-1, AC-S1-2
  - State: **PASS** on silicon
- **S2 — pattern-set groups + triage + oracle**
  - Acceptance: AC-S2-1/-2/-3
  - State: **PASS** on silicon (33/33 incl. the resident-identity clause)
- **S3 — full build, residency, incremental updates**
  - Acceptance: AC-S3-1/-2/-3
  - State: **PASS**; 21/21 groups built and `pr_verified`
- **S4 — ROM-baked shared trie + suppression pilot**
  - Acceptance: AC-S4-1/-2
  - State: **not started — owner-gated**

Open for the owner: the **A1–A4 amendment slate** and open finding **OF-1**
(`docs/spec-amendments-s3.md`), and recommendations 2–4 of the working-set
study (`docs/studies/a5-working-set.md`).

Headline measurements now in hand (all on `nf-server06`, this shell):

- **groups for the corpus**
  - planned/assumed: ~16
  - **measured**: **21** over 8 port classes
- **swap cost**
  - planned/assumed: 17–45 s
  - **measured**: **14–16.2 s** incl. in-band wedge recovery
- **per-group build**
  - planned/assumed: ~30–60 min
  - **measured**: **~60 min**; 22.6 h of Vivado for all 21
- **group circuit**
  - planned/assumed: 20–30K LUTs @ dpb=8
  - **measured**: **~10K LUTs @ dpb=1**, fmax 250.7–258.8
- **instantaneous coverage**
  - planned/assumed: 6.4% of the corpus
  - **measured**: **~28%** of the rules that could fire on a given flow

## 1. Shape of the system

The FPGA is a **candidate-nominating prefilter** in front of full software
Snort. Rules are triaged (SR1/SR2) into header-only (95), anchor-compilable
(3,896 = 97%), and always-forward (26). Anchor-compilable rules are packed
into **21 rule groups** by destination-port class (SR6, ≤256 rules/group),
each compiled to a **pattern-set circuit** — N automata sharing one harness
— through the PYRO R63 synthesis service into a partial bitstream for
`pyro_rp`. A host **filter daemon** (SR11–SR15) taps traffic, evaluates
header predicates against site variables, chunks flow payloads into R78
`MATCH_REQUEST`s (SR12 overlap tail), and maps `MATCH_REPLY` windows to
`(flow, gid:sid, offset)` nominations that full Snort re-verifies. The FPGA
never issues a verdict in S1–S3 (SR5); completeness is absolute (SR3, PYRO
R19 discipline).

**The "dynamic" part — the owner's stated goal — is SR10 + AC-S3-1**, and it
is built and verified on silicon: the residency manager observes live
traffic and hot-swaps which group's partial is resident, one JTAG load per
rotation. So "runtime swap based on what the NIC sees" is **rotation among
prebuilt group bitstreams at a ~16 s blind window**, which SR19 accounts
explicitly. Seconds-scale rule pushes ("A5") and line-rate inline filtering
(CMAC tap) remain separate owner decisions (OQ-1/OQ-2).

## 2. Amendment slate adopted with 1.0.0 — and what became of it

1. **SA-1 — char-dev transport ceiling (~2.3 GiB/s).** Adopted as a fact.
   **Not exercised by SNORT-PF:** the daemon speaks the R78 control path on
   the `onic` netdev, because the same PF cannot hold the netdev tap and the
   char-dev data plane at once (Risk b) and S3's traffic volumes never
   approached the control path's ceiling. The char-dev route stays available
   and unused; revisit only if a real tap justifies it.
2. **SA-2 — target the 8 B/cycle harness. *Superseded by measurement.***
   A 256-pattern group fits the 80K-LUT budget only at **dpb=1** (~10K LUTs;
   dpb=8 estimated ~54K and would not leave room). All 21 groups are built
   at dpb=1, ~1 B/cycle. SF21 recalibrated SF6's cost model from post-route
   reality accordingly.
3. **SA-3 — SF7's CMAC violation shrank to −0.015 ns.** Held. It did not,
   however, make partial builds comfortable: see **OF-1**, where six of 21
   group links missed the R73a.1 RP-scoped gate by −0.005…−0.120 ns on
   RM↔static boundary paths and needed implementation-strategy escalation.

## 3. Owner decisions (OQ-1…OQ-5) — current state

- **OQ-1**
  - Question: Open the A5 slot (loadable-table engine)?
  - State: **Answered on the evidence it asked for.** The deferral was
    "measure S3's real swap cadence first." Measured (16 s), then measured
    what it buys: coverage is **capacity-limited, not latency-limited** —
    27/27 scenarios (`docs/studies/a5-working-set.md`). **Do not open A5 on
    coverage grounds.** A separate, unpriced *build-time* argument survives
    (weekly diffs without Vivado). Owner's call.
- **OQ-2**
  - Question: Line-rate CMAC tap on the roadmap?
  - State: Unchanged: feasibility spike only, not started. Host-tap ceiling
    remains sufficient.
- **OQ-3**
  - Question: Raise GROUP_MAX past 256?
  - State: Measured headroom exists (a 253-slot group is 12.7% of the PR
    budget). But the study says group **size** is not the lever — group
    **composition** is (see §6.2). Recommend deciding both together.
- **OQ-4**
  - Question: Suppression pilot wanted?
  - State: Unchanged: **nomination-only through S3**, SR17-gated, S4 question.
    The SR16 differential the gate depends on now exists and has bitten (sid
    509).
- **OQ-5**
  - Question: Which traffic tap?
  - State: Resolved: **AF_PACKET on the control binding**, implemented in
    `pyro/snort/daemon.py`; pcap replay for deterministic runs. Risk b
    sidestepped rather than solved (see §5b).

## 4. Delivery sequence — as planned, as delivered

### S1 — one real rule through the unmodified flow ✅
1. `pyro/snort/` parser + SR1 triage over the corpus. **Reproduced
   SF8–SF11 exactly (95 / 3,896 / 26).**
2. sid 1927 (FTP `authorized_keys`, nocase) compiled through the unmodified
   generator → R63 `pr_bitstream` → JTAG. Rebuilt at generator 2.3.0 after
   the R47b bump.
3. Positive/negative pcap corpora → nomination → Snort 3.12.2.0
   re-verification. **AC-S1-1/-2 PASS.**

### S2 — pattern-set generator + groups + oracle ✅
4. `generate_group`: N automata on one shared harness, slot index ==
   `pattern_id`, tombstones keep their index, 256-bit pend register +
   priority encoder. The `$HTTP_PORTS/0` group (256 rules → 253 slots) built
   and verified on silicon. SR8's cost model recalibrated (SF21).
5. SR16 two-sided oracle — closed-form nomination reference asserted as set
   equality per slot, plus the Snort differential with the FP census pinned
   by equality — and SR12 chunk/segment fuzzing. **AC-S2-1/-2/-3 PASS**,
   33/33 with the hardware clause.

### S3 — full build + traffic-driven swap ✅
6. **All 21 groups built**, 2 concurrent Vivado jobs, SR9 cache populated.
   Four passes were needed: 15 closed timing directly, six required
   implementation-strategy escalation (`--phys-opt`, then directives) —
   Vivado P&R is deterministic, so a retry must vary the strategy. Driver:
   `.superpowers/pr-builds/pr_build_driver_s3_all_groups.py`.
7. **Filter daemon** (`pyro/snort/daemon.py`) + **residency scheduler**
   (`pyro/snort/scheduler.py`) + CLI (`scripts/pyro_snortpf_daemon.py`):
   AF_PACKET tap → SR13 header classify → per-port mix histogram → SR10
   scheduler → R78 MATCH pipeline with SR12 tails and the SR14 identity gate
   → nomination stream → SR19 stats. Verified end to end on silicon.
8. **Weekly-diff drill**: a 33-rule SF15-shaped diff dirties **2** groups
   (≤3 required), tombstones never renumber, 19/21 groups hit the cache.
   **AC-S3-3 PASS.**

Also delivered in S3, beyond the plan:

9. **AC-S3-2 content-chain lowering + clean-pcre fusion** — 245 chains,
   91 `\A` prefixes, 36 fusions → 181 lowered slots, with the admission
   conditions the differential forced (see A2 in the amendment slate; the
   sid-509 miss is the reason TCP `offset/depth` stays a dropped conjunct).
10. **The working-set study** (`docs/studies/a5-working-set.md`) and the
    value-aware scheduler rewrite it prompted.

### S4 — ROM-baked shared trie + suppression pilot ◐ AC-S4-1 met
Authorized by the owner 2026-08-05; AC-S4-1 **met 2026-08-06** (notebook
entry 00:23:08). The 16-byte-capped fold-all trie — 21,332 states,
2,845 patterns, all 3,896 rules — links against the wiretap static at
**WNS +0.020 ns / 250 MHz** in the SLR2 pblock at 24 URAM + 82.5 BRAM
tiles of the 64/160 budget; pr_verify OK; `test_acs4_1_rom_trie.py`
gates the artifacts. Two findings worth their ink: UltraScale+ URAM
cannot be initialized from the bitstream (Synth 8-10226, measured), so
the bitmap is boot-expanded at reset from a tbyte ROM (~0.3 ms); and
the wrapper needed a boot CSR sweep or a ROM child drops all wire
frames until the host polls. The silicon replay passed first run
(notebook: identity/refusal/match-set/wire window all exact; wire
scanning is scan-bound at ~11.2k fps on all-nominating traffic).
The **SR16 oracle over the shared trie ran 2026-08-06** (notebook
entry 02:53:33; `test_acs4_sr16_oracle.py`): closed-form set equality
exact, both sabotages bite, chunk arithmetic proven at every straddle
cut, and the Snort 3 differential over 3,925 cases shows **zero
raw-anchor completeness misses** (350 distinct raw rules confirmed;
the only normalized-buffer misses are the two pinned SF11
percent-encoding cases). That is SR17 condition (b) evidence only.
The **precision delta at cap 16 is measured** (notebook 12:33:54):
vs the lowered chains the trie over-nominates 3.68x, and the 16-byte
prefix cap alone is 50.2% of all trie nominations (fold-all 11.3%,
anchor-only-vs-chains 11.3%) — so a precision push at the
suppression tier should revisit the cap before adding chains.
Still open under S4: the production-static re-link (R82b) and
**AC-S4-2** — the
suppression pilot stays SR17-quadruple-gated on a separate owner
approval, which conditions (a)/(c)/(d) still lack.

## 5. Risks — outcomes

a. **EQDMA multi-queue loss** (open AMD case): **does not bind SNORT-PF
   today**, because the daemon uses the R78 control path rather than the
   char-dev data plane (SA-1). It still binds PYRO throughput work to one
   queue and would return the moment a high-rate tap is built.
b. **Control/data plane contention: sidestepped, not solved.** The one PF
   cannot hold the `onic` netdev and the char-dev at once. S3 chose the
   netdev for both tap and MATCH, so no interleaving was needed and no
   blind window was incurred — at the cost of the SA-1 bandwidth. A genuine
   line-rate tap re-opens this.
c. **JTAG swap fragility: measured and contained.** Every rotation is a
   partial load plus automatic in-band recovery, **14–16.2 s**, exercised
   many times across the build and demo runs without a manual intervention.
   Rotation hysteresis makes swaps rare relative to that cost.
d. **NEW — OF-1: marginal RM↔static boundary paths.** Six of 21 group links
   missed the timing gate by −0.005…−0.120 ns, every one ending at one of
   two flops in the locked static. Closed by strategy escalation, but every
   future partial rolls the same dice. Recorded in
   `docs/spec-amendments-s3.md`; the structural fix is a static rebuild that
   registers the slice boundary, which is an owner-priced change.
e. **NEW — a heuristic can pass its acceptance criterion and still be
   worthless.** AC-S3-1 required the manager to hot-swap by observed port
   mix; it did, on silicon. Nobody had measured whether swapping *helped*
   until the working-set study, which found the shipped scoring was **worse
   than pinning one group** (1.6–10.3% vs 27.9%). Fixed. The general lesson
   is a verification-shape one: *functions-as-specified* and *is-worth-doing*
   are different claims, and only the first was gated.

## 6. Next steps

Nothing below is authorized; these are the open threads in priority order.

1. **Owner review of the A1–A4 slate and OF-1** (`docs/spec-amendments-s3.md`).
   A1/A2 record what AC-S3-2's implementation had to decide to keep SR3
   absolute, including a measured completeness defect — they should not sit
   unadopted indefinitely, because the code already implements the narrowing.
2. **Rule-packing experiment (study rec. 2, host-only, no spec change to
   try).** Uniform 256-rule groups plus a universally-relevant `any` class
   is what makes single-tenant residency a dead end: the three `any` groups
   are worth 256 rules on *every* flow, so no traffic-driven choice can beat
   pinning one. Packing that co-locates universally-relevant rules with
   traffic-specific ones would raise the value of a single resident group
   with **no hardware change**. Measurable with the existing study harness
   before committing to anything. Pairs with OQ-3.
3. **Capacity is the real lever (study rec. 3).** k=4 resident groups reach
   76% coverage, k=8 reaches 84%, all 21 reach 100%. That is the S4 trie's
   case, and it is stronger than the coverage cap in the spec's §5 implies.
   A feasibility spike on the trie's *timing* (the untested part — URAM plus
   bitmap decode at 250 MHz on SLR2, against OF-1's evidence that we are
   already marginal) would de-risk AC-S4-1 cheaply.
4. **If A5 is opened, open it for the build-time argument** and say so:
   rule changes stop being Vivado builds. It does not buy coverage.
5. **Real traffic.** Every coverage number here rests on a swept synthetic
   mix because no production capture was available. A single representative
   capture would either confirm the structural argument or sharpen it, and
   costs nothing but access.
