# Laboratory Notebook — stringy-fpga

Experiments on accelerating string matching and regex operations using the
dynamic (partially reconfigurable) region of the attached FPGA.

**Conventions for this notebook** (these extend the standard /notebook format):

- Entries are kept in **reverse chronological order** — newest experiment first,
  immediately after the Table of Contents.
- Every entry title carries both a **date and a time stamp** in the form
  `DD Mon YYYY HH:MM:SS` recording when the experiment was written up.
- Anchor IDs include the time: `#dd-mon-yyyy-hhmmss`.
- Each entry follows the 5-section format: Hypothesis, How, Observations,
  Data analysis, Ideas for future experiments. Status tags `:complete:` or
  `:in_progress:` follow the title.
- Entries are separated by `---`.

# Table of Contents

1. [EXPERIMENT  6 Jul 2026 14:05:00 PYRO Phase 2b — PR Shell + First pr_bitstream Partial](#6-jul-2026-140500) :complete:
2. [EXPERIMENT  6 Jul 2026 02:50:21 PYRO Phase 2 — Real Vivado Flow, Estimator Calibration](#6-jul-2026-025021) :complete:
3. [EXPERIMENT  5 Jul 2026 12:05:02 PYRO Phase 1 — Per-Pattern Circuits, Synthesis Service, C ABI](#5-jul-2026-120502) :complete:
4. [EXPERIMENT  5 Jul 2026 02:44:00 PYRO Phase 0 — Software Shim, Classifier, Model](#5-jul-2026-024400) :complete:
5. [EXPERIMENT  4 Jul 2026 07:33:45 FPGA Platform Discovery](#4-jul-2026-073345) :complete:

---

# EXPERIMENT  6 Jul 2026 14:05:00 PYRO Phase 2b — PR Shell + First pr_bitstream Partial :complete:

## 1. Hypothesis

Can PYRO build a partial-reconfiguration-enabled OpenNIC shell for the Alveo
U250 and, against its locked static checkpoint, generate a genuine per-pattern
**partial bitstream** that closes timing and passes `pr_verify` — flipping the
R71/R83a `pr_flow_present` predicate true on honest, host-observable evidence?

## 2. How

- **Equipment:** Alveo U250 (xcu250-figd2104-2L-e), owner's board at PCI af:00.0;
  host 72-core, 376 GB RAM, Ubuntu 24.04 / glibc 2.39.
- **Software:** Vivado **2025.2** (`/usr/local/cad/2025.2/Vivado`, re-pinned from
  2023.1 which segfaults at batch exit on this glibc — spec v2.2.1); open-nic-shell
  @ ce85c8d + `pyro` plugin (reconfigurable partition `pyro_rp`, Pblock
  `CLOCKREGION_X5Y7:X5Y8`); CMAC license permanent through 2027.06.
- **Flow:** full DFX — baseline shell build, then static synth + `link` (opt/place/
  route → routed + **locked** static DCP, full flash `.bit`/`.mcs`, ID-stub partial),
  then the `VivadoToolchain` `pr_bitstream` mode against the locked substrate.

### Key commands

```bash
# PR shell (detached, ~2 h): static synth + DFX link
vivado -mode batch -source build_pr.tcl -tclargs -stage link -board au250 \
  -tag pyro_pr -jobs 32 -reference 1
# First real per-pattern partial via the toolchain PR mode
PYRO_TOOLCHAIN=vivado PYRO_VIVADO=/usr/local/cad/2025.2/Vivado \
PYRO_PR_STATIC_DCP=.../pr/pyro_static_locked.dcp \
PYRO_PR_REFERENCE_DCP=.../pr/pyro_static_id_stub_routed.dcp \
  python3 first_pr_job.py     # pattern 'ab+c'
# R83a availability probe with the evidence manifest
PYRO_PR_EVIDENCE_MANIFEST=.../patterns/pattern_a7cb950cc8624276_manifest.json \
  python3 -c "import phase2_support as p; print(p.pr_flow_present())"
```

## 3. Observations

PR shell (ID-stub reference config) and first pattern partial (`ab+c`):

| Artifact | Result |
|----------|--------|
| Baseline shell (2025.2 port) | 0 errors, `.bit`+routed DCP+`.mcs` |
| PR static, timing | **WNS +0.031 ns**, all constraints met |
| Locked static DCP | 101 MB, `lock_design -level routing` |
| Pattern `ab+c` partial | **1,752,132 B**, `pr_verify` = **compatible** |
| Pattern partial, timing | met, **fmax 251.95 MHz** (> 250 target) |
| RP-child resources | **3131 LUT / 1592 FF / 3 BRAM** (0.18 % device) |
| `pr_flow_present` probe | **True** on the real evidence manifest |

RP-child wrapper size, before vs after the RAM reworks:

| Version | LUT | FF | OOC synth | Routes? |
|---------|-----|-----|-----------|---------|
| Byte-array buffers | ~7204 | ~13709 | **50+ min (timeout)** | no |
| Beat-wide RAM buffers | 7204 | 13709 | 59 s | plateau ~34k overlaps |
| + match store → BRAM, hdr snapshot | **3131** | **1592** | **45 s** | **0 overlaps, closes** |

## 4. Data analysis

The end-to-end PR path works: a per-pattern circuit becomes a routed,
`pr_verify`-passing partial bitstream against the locked static, and the
resulting `pr_bitstream`/`pr_verified` manifest is the sole host-observable,
un-fabricable evidence (R47b-consistency rejects inconsistent manifests) that
flips `pr_flow_present` true (R83a).

Two synthesis/routing pathologies gated the result, both the same root cause —
storage expressed as flip-flops instead of memory. (1) A 1536-byte frame buffer
with 64 write ports could not infer as RAM (12 k flops + decode) → 50-min synth;
fixed with beat-wide single-write-port word arrays. (2) A 61×192-bit match
capture in parallel flops (~11.7 k FF) plus contained-routing pressure in a
2-clock-region DFX Pblock → routing plateaued at ~34 k overlaps; fixed by moving
the capture to block RAM, which then forced a beat-0 header-snapshot register to
keep the frame buffers RAM-inferable. Net **9× FF reduction** (13709 → 1592)
took utilization to ~0.2 % and routing closed immediately. Every wire byte
stayed identical across both reworks (xsim 5/5). The estimator predicts only the
engine (288 LUT); the wrapper's fixed parser/buffer/capture overhead dominates —
a calibration input for Phase 3.

Toolchain robustness also validated on real output: the `pr_verify` gate
correctly accepts "compatible / Number of differences : 0" while rejecting real
failures (W6-b), and the RM-scoped `report_utilization -cells` regex handles the
2025.2 "CLB LUTs*" footnote (W3-b). One flow bug fixed: the RP cell must be
located by `HD.RECONFIGURABLE` (its ref-name is gone once black-boxed in the
locked DCP) and re-queried by immutable NAME after each netlist mutation.

## 5. Ideas for future experiments

- Flash the PR shell via JTAG and perform the one root-assisted PCIe rescan to
  flip `device_usable` true; run the on-device ACs live (AC-2b-2/2b-3).
- Load the `ab+c` partial into `pyro_rp` over JTAG (R85) on the live board and
  drive a MATCH_REQUEST end-to-end over the onic netdev.
- Feed measured RP-child utilization back into the estimator (R74a): model the
  fixed wrapper overhead separately from the per-pattern engine.
- Multi-pattern residency: exercise R87 slot ≥ 2 once a multi-partition shell
  floorplan exists.

---

## 1. Hypothesis

Can the Phase-1 mock toolchain be replaced by a real Vivado synthesis flow —
regex-generated RTL through synth + P&R + timing for the physical board's part —
and does the resource estimator agree with real post-route utilization within
the pre-registered R74 margin? Hardware-gated ACs must record SKIP (never PASS)
since the board is unavailable (spec v2.1.x, R71).

## 2. How

- **Equipment:** Alveo U250 (xcu250-figd2104-2L-e) at PCI 0000:af:00.0/1 running
  a third-party OpenNIC shell image (onic driver v0.21) — observed only, never
  touched. Host: Ubuntu (kernel 6.8.0-124), no root, no /dev/qdma*, no OpenNIC
  PR-partition floorplan.
- **Software:** Vivado 2023.1 (/usr/local/cad/Vivado/2023.1; needs a
  libtinfo.so.5→.so.6 shim on this host — the adapter creates its own), CPython
  3.12.3, spec v2.1.0→v2.1.2 (R70–R77, R3c, R64a, R70b), PYRO ABI 2.0.0 frozen.
- **Benchmarks:** 6-pattern calibration corpus through OOC synth+P&R at 250 MHz;
  full acceptance suite AC-0-*..AC-2-* (1051 tests).

### Key commands

```bash
# probe: which Vivado supports the U250 part with a license
LD_LIBRARY_PATH=<shim> /usr/local/cad/Vivado/2023.1/bin/vivado -mode batch -source probe.tcl

# real flow, end to end
PYRO_TOOLCHAIN=vivado PYRO_VIVADO=/usr/local/cad/Vivado/2023.1 \
  python3 -m pytest tests/acceptance -q -rs
gmake lib abi-check
```

## 3. Observations

- Vivado 2019.2 (/tools/Xilinx) has **no UltraScale+ Alveo device support**;
  2023.1 and 2025.2 both synth+place+route xcu250 cleanly (license OK).
- The Phase-1 generated RTL (`pyro_circuit`) went through real synthesis
  **unmodified** — all patterns met 250 MHz timing OOC (WNS +1.7 to +2.4 ns).
- Real post-route utilization vs (recalibrated) estimator, Vivado 2023.1:

| pattern | states | real LUT | est LUT | real FF | est FF | WNS (ns) |
|---|---|---|---|---|---|---|
| `abc` | 4 | 197 | 278 | 427 | 452 | **+2.405** |
| `[a-z]+[0-9]{2,4}` | 9 | 197 | 304 | 430 | 457 | +1.726 |
| `(?:GET\|POST\|PUT) /[a-z/]* HTTP` | 24 | 221 | 388 | 441 | 472 | +2.200 |
| `^ERROR: .*$` | 12 | 226 | 320 | 431 | 460 | +1.884 |
| `[A-Za-z0-9]{60}` | 62 | 228 | 624 | 484 | 510 | — |
| `[A-Za-z0-9]{200}` | 202 | **410** | 1464 | **626** | 650 | — |

- The mock-era estimator (base 2000 LUTs/2000 FFs) violated the pre-registered
  R74 margin on 3 of 4 initial patterns (est/real up to **10.6×** > 10× ceiling);
  recalibrated constants: **256 LUTs + 4/state + 2/edge; 448 FFs + 1/state**.
- Full verification: **1045 passed, 6 skipped, 0 failed** (63:41). ABI 2.0.0
  intact. All 6 skips are R71-mandated, each naming its absent prerequisite.
- Loss-regime routing measured: median pyro 1225 ns vs stock re 257 ns =
  **4.77×** relative (R3a absolute ≤ 2 µs bound PASSES; R3b relative 1.15×
  bound SKIPs per new R3c — routing decision still Python-level).

## 4. Data analysis

The ~200-LUT/~430-FF floor (CSR mux, control FSM, 64-bit offset counters)
dominates small circuits, which is why the mock-era +2000 base overshot the
10× honesty ceiling — pre-registering the R74 margin before peeking at data
did its job by forcing an estimator fix rather than a margin fix. Per-state
scaling is mild (~1.1 LUT/state real vs 4 estimated conservatively); at
MAX_STATES=1024 the estimate (~6.4k LUTs) is ≪ the PR budget (216k), so
MAX_STATES remains the binding eligibility constraint (R12 design intent
preserved). Phase 2's deliverable ledger: real-toolchain clauses of
AC-2-1/2-3/2-4 and the model clauses of AC-2-5/2-6 PASS from real execution;
every PR-bitstream/on-device clause SKIPs honestly (no PR floorplan; board is
a third-party live NIC). The R3b 4.77× measurement confirms the relative
loss-regime bound needs the native routing path — now explicitly deferred to
AC-3-3 (R3c).

## 5. Ideas for future experiments

- Build an OpenNIC shell PR-partition floorplan (needs Vivado 2022.2-era shell
  sources + a pblock for the 250 MHz user box) to un-SKIP AC-2-1's bitstream
  clause — requires a board we are allowed to program.
- Move the §8 R51 routing decision into the L3 native runtime to attack the
  4.77× → ≤1.15× gap (hard requirement at AC-3-3, Phase 3).
- Calibrate BRAM once patterns large enough to infer block RAM appear; today's
  circuits use 0 BRAM (manifest carries the fixed 16 KiB reserve).
- Richer calibration corpus: bounded-repeat counters and case-folded classes to
  stress the FF intercept (only ~24 FF headroom above the observed floor).
- Wall-clock synthesis latency distribution (currently ~4–6 min/pattern OOC) to
  tune the R77 timeout and the R4a launch threshold for interactive workloads.

---

# EXPERIMENT  5 Jul 2026 12:05:02 PYRO Phase 1 — Per-Pattern Circuits, Synthesis Service, C ABI :complete:

## 1. Hypothesis

After the project owner inverted the architecture (spec v2.0.0: every
HW-eligible regex compiles to its own synthesized circuit for the OpenNIC
dynamic region, loaded by partial reconfiguration, with async background
synthesis), can Phase 1 deliver the full software stack — HDL generator,
circuit model, synthesis service, and native C ABI — with byte-identical
results and no hardware?

## 2. How

- **Equipment:** Intel C620 x86_64 server, Ubuntu (Linux 6.8.0-124-generic);
  Xilinx OpenNIC card present but unused (mock toolchain stands in for Vivado)
- **Software:** CPython 3.12.3, pytest 9.1.1, GCC 13.3 (C11), iverilog,
  valgrind; spec evolved v2.0.0 → v2.0.5 via §13 change control
- **Benchmarks:** 506-test spec-only acceptance suite; 519 unit tests;
  4000–6000-case lazy-quantifier fuzz; valgrind on the native runtime

### Key commands

```bash
python3 -m pytest tests/unit/          # 519 passed
python3 -m pytest tests/acceptance/    # 506 passed, 0 skipped
/usr/bin/make abi-check                # pyro_abi_version = 0x00020000
/usr/bin/make valgrind                 # 0 errors, 0 leaks
```

## 3. Observations

| Gate | Result |
|------|--------|
| Unit suite | **519 passed** |
| Acceptance suite | **506 passed, 0 skipped** |
| C ABI | **2.0.0** (0x00020000), valgrind clean |
| Generated RTL | deterministic; iverilog-lints; §7.4 harness CSR map exact |
| Final review verdict | Ready to merge: **Yes** |

Delivered: `pyro/hdl/` (automaton IR, resource estimator, Verilog
generator with R47a identity block and R19c over-approximation metadata);
`pyro/_circuit_model.py` (executes the generated automaton); `pyro/synth/`
(R47b manifests, persistent bitstream cache, mock toolchain, out-of-process
synthesis service, residency manager with LRU eviction); `pyro.prewarm` +
lifecycle stats; `include/pyro_rt.h` + `src/pyro_rt.c` (ABI 2.0.0 model
binding, hardened artifact parser); `pyro.testing` fault-injection seams.

Defects found and fixed by the review loop (regression-tested):
- Circuit-model finditer dropped zero-width matches, then (round 2) missed
  CPython's **must_advance** retry — `'a??'` on `"aa"` dropped real matches
  (R19 false negatives). Fixed by delegating span enumeration to stock
  `re` while the generated automaton remains an unconditional completeness
  oracle (`CompletenessError` on any missed start).
- Scoped `(?m:...)` multiline was not threaded into anchor lowering.
- Per-call `os.environ` reads and debug mutators in the frozen ABI header
  (now `#ifdef PYRO_TESTING`; production build exports zero test symbols).

## 4. Data analysis

The hybrid trust model carried the phase: the automaton only ever needs to
be **complete** (superset of match starts); stock `re` makes results exact.
Both finditer bugs lived in hand-reimplemented CPython iteration semantics
— the lesson (twice) is to delegate enumeration to the oracle and keep the
automaton as a cross-check, not to transcribe CPython's scanner by hand.
Spec §13 change control absorbed six amendments (R19a–c over-approximation
sanction, R47a hash inputs, R51b device-free ruling, R67–R69 public test
seams) without ever breaking Phase 0's ACs. **Important integration note:
production dispatch still runs the Phase-0 model** — the circuit model and
native binding are delivered and validated out-of-band but intentionally
not yet on the dispatch path.

## 5. Ideas for future experiments

- Phase 2 entry criteria (from final review): circuit finditer stays
  must_advance-correct AND RTL anchors (`at_eol`/`at_eob`, `res_start`)
  made real before any generated circuit serves user results
- Wire `_circuit_model`/`_native` into dispatch behind R51b; measure
  cross-tier equivalence on hardware
- Vivado + open-nic-shell PR flow bring-up; replace mock toolchain
- Result-ring LE enforcement on real DMA; JSON manifest parser hardening
- Benchmark suite (R59) on real corpora to validate R1/R2 win regime and
  R19b false-positive-rate bounds

# EXPERIMENT  5 Jul 2026 02:44:00 PYRO Phase 0 — Software Shim, Classifier, Model :complete:

## 1. Hypothesis

Can a pure-Python, transparently-interposable `re` shim (spec
`python-regex-offload`, Phase 0) achieve byte-identical results to CPython
`re` across a spec-derived acceptance suite, with routing overhead ≤ 2 µs,
before any hardware exists?

## 2. How

- **Equipment:** Intel C620 x86_64 server, Ubuntu (Linux 6.8.0-124-generic);
  Xilinx OpenNIC card present but unused (Phase 0 is software-only)
- **Software:** CPython 3.12.3, pytest 9.1.1, GCC 13.3; CPython source added
  as shallow submodule (`third_party/cpython`)
- **Benchmarks:** 412-test acceptance suite (spec-only, independent author);
  219 unit tests; routing-overhead microbenchmark (R3a/R5)

### Key commands

```bash
python3 -m pytest tests/unit/          # implementer suite
python3 -m pytest tests/acceptance/    # independent spec-derived suite
make abi-check                         # frozen C ABI header (AC-0-8)
```

## 3. Observations

| Gate | Result |
|------|--------|
| Unit suite | **219 passed** |
| Acceptance suite | **411 passed, 1 sanctioned skip** (R61 → Phase 1) |
| ABI check | `pyro_abi_version() == 0x00010000` |
| Routing overhead (median) | **0.97 µs** (bound: ≤ 2 µs, R3a/R5) |
| Final review verdict | Ready to merge: **Yes** |

Defects found and fixed during the loop (each with a regression test):
- Per-call `os.environ` reads cost ~1.74 µs, breaking the 2 µs budget →
  spec v1.2.0 (R35a–d cached sampling + `pyro.refresh_env()`).
- Empty-match adjacency (R22): anchored `match()` cannot reconstruct a
  post-empty `finditer` match; `_VerifyError` leaked to callers (R52).
- **Window-truncated `fullmatch` reconstruction silently flipped capture
  groups** for trailing-anchor alternations (`(?P<a>foo)$|(?P<b>foo)` on
  `"foobar"`) — same span, wrong groups; caught by code review, not tests
  (property generator had no anchor atoms).
- Lone-surrogate `str` subjects raised `UnicodeEncodeError` from the model
  path; `bytearray` subjects returned wrong-typed `group(0)` → spec v1.2.1
  (R14a surrogate gate, R51a Phase-0 type/pos/context gates).

## 4. Data analysis

The correctness architecture held: making the Phase-0 model *be* CPython
`re` behind the future §7.3 C-ABI seam means classifier bugs can only
mis-route, never mis-answer — every real defect was at the semantic edges
(zero-width adjacency, anchor context at window boundaries, encoding
corner cases), exactly where a future hardware NFA will also be at risk.
The spec's §13 change-control loop was exercised 4 times (1.0.0 → 1.2.1),
each time converting an implementation discovery into binding spec text
before tests were derived from it. The 1.15× relative fallback bound (old
R3) was proven physically meaningless for a Python-over-C hot path and
replaced by an absolute 2 µs bound (R3a); the measured 0.97 µs leaves
headroom for one more dict lookup, roughly, on this host. Independent
test authorship caught one class of bug (R52 leak via property fuzzing);
adversarial code review caught the one the fuzzer grammar couldn't
generate — both nets were necessary.

## 5. Ideas for future experiments

- Phase 1: C-ABI host runtime + Aho-Corasick engine in the software model;
  raw-Ethernet control-frame format to the onic netdev (needs spec §10.1)
- Validate the FLAG_VERIFIED trust boundary when the real L3 binding lands
  (device must never self-certify windows past re-verification)
- Benchmark stock `re` vs `re2`/`hyperscan` on target corpora to calibrate
  R1/R2 win-regime thresholds before RTL work
- Vivado + open-nic-shell build flow bring-up (P1); QDMA char-dev
  enablement (P2)
- Phase-1 cleanups: single UTF-8 encode on model path; lazy finditer

# EXPERIMENT  4 Jul 2026 07:33:45 FPGA Platform Discovery :complete:

## 1. Hypothesis

What FPGA hardware is installed in this system, what shell/driver does it run,
and can its dynamic region host a regex-matching engine reachable from Python?

## 2. How

- **Equipment:** Intel C620-chipset x86_64 server, Ubuntu (Linux 6.8.0-124-generic)
- **Software:** lspci, lsmod, sysfs inspection (no FPGA vendor tools assumed)
- **Benchmarks:** none — discovery only

### Key commands

```bash
lspci -nn | grep -iE 'xilinx|altera|intel.*fpga'
lspci -k -s af:00.0; lspci -k -s af:00.1
lsmod | grep -iE 'xdma|qdma|onic|xocl|xrt'
ls /sys/class/net/
command -v vivado v++ xbutil
```

## 3. Observations

| Property | Value |
|----------|-------|
| PCIe functions | **af:00.0** (10ee:903f), **af:00.1** (10ee:913f), subsystem 10ee:0007 |
| PCI class | Network controller |
| Kernel driver | **onic** (AMD/Xilinx OpenNIC) |
| Network interfaces | enp175s0f0, enp175s0f1 |
| FPGA char devices | none (`/dev/xdma*`, `/dev/qdma*` absent) |
| Vendor tools on PATH | none (vivado, v++, xbutil all missing) |

## 4. Data analysis

The card runs the **OpenNIC shell** (QDMA-based), which reserves a 250 MHz
user-plugin **dynamic region** for custom RTL — the natural home for a regex
engine. Offload from Python is feasible: patterns compiled to a programmable
automaton loaded into the plugin region, data moved via QDMA (once char-devs
are enabled) or raw Ethernet frames to the NIC function (usable today).
FPGA wins only for large corpora / streaming with pattern reuse; PCIe latency
makes CPU faster for short one-shot matches. Toolchain (Vivado) installation
is a prerequisite for any hardware build. Full design captured in
`specs/python-regex-offload.md` (R1–R61, AC-0-x…AC-3-x).

## 5. Ideas for future experiments

- Install Vivado + OpenNIC build flow; rebuild shell with a stub user plugin
- Enable QDMA char-devs and measure PCIe round-trip latency and DMA bandwidth
- Benchmark CPython `re` vs. `re2`/`hyperscan` on target corpora to set the
  offload break-even thresholds (AC targets in the spec)
- Prototype Phase 0 software-only shim (`fpga_re`) with CPython fallback
