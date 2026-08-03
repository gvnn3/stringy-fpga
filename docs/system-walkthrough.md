# System walkthrough

A guided tour of the whole system for a reader arriving cold: what it is,
what it does today (measured), and how it is built, layer by layer. Written
at the end of Phase S2 (branch `phase2-snort`; spec `snort-rule-offload`
v1.0.2, PYRO spec `python-regex-offload` v2.7.0). The companion documents
are `docs/demo.md` (how to drive it), `docs/notebook.md` (the running lab
record), and `docs/phase2-snort-plan.md` (the delivery plan this branch
follows).

> **2026-08-03:** §§1–9 describe the system as it stood at the end of S2
> and are kept as that record. §10 is the current map — the whole tree,
> file by file, reframed around what the project turned out to be about:
> using OS scheduling primitives to manage resources on an FPGA at run
> time. Read §10 first if you are here for the scheduling work.

## 1. What it is

A reconfigurable pattern-matching offload on an AMD/Xilinx Alveo U250
(`xcu250-figd2104-2L-e`, host `nf-server06`, PCIe `0000:02:00.0`) with two
product surfaces sharing one FPGA shell:

- **PYRO** — a drop-in `re`-compatible Python module (`pyro.re`) that
  transparently routes large, reused regex scans to a matching circuit
  generated for that exact pattern and loaded into the FPGA's dynamic
  region. Callers keep CPython `re` semantics; PYRO proves equivalence
  rather than asking for trust.
- **SNORT-PF** — a prefilter that compiles hundreds of Snort community
  rules into a single pattern-set circuit. The FPGA only ever *nominates*
  ("this flow may match rule X"); full Snort re-verifies every nomination.
  Hardware over-approximation can therefore never create a false alert,
  and a dropped constraint only costs re-verification work, never
  correctness.

The load-bearing idea in both surfaces is the same: the hardware is allowed
to say "maybe" cheaply and at line rate, and a software authority (CPython
`re` for PYRO, Snort for SNORT-PF) always has the final word.

## 2. What it does today, measured

- The resident SNORT-PF child holds the `$HTTP_PORTS/0` group: 256 rules
  deduplicated onto 253 anchor slots on one 1 B/cycle shared harness —
  10,147 LUTs / 5,681 FFs (12.7% of the 80K-LUT PR budget), fmax
  250.44 MHz, verified on silicon (33/33 acceptance clauses, including the
  resident-identity check). A MATCH round-trip on a malicious URI nominates
  the right slot with the exact end offset; the sidecar maps slot →
  `[gid:sid, …]`; benign traffic returns silence.
- The PYRO x4 regex child (8 B/cycle, 4 cores, 260.8 MHz) demonstrates
  ~2.3 GiB/s zero-loss host-to-card scanning over one QDMA ST queue.
  Multi-queue is gated by an open EQDMA silent-frame-loss issue (AMD
  support case filed; see `docs/amd-support-case-eqdma-h2c-loss.md`).
- Circuit swap is JTAG partial reconfiguration, ~14–45 s including
  automatic in-band wedge recovery (`scripts/pyro_wedge_recover.sh`).

## 3. The hardware and host

One card, one PCIe physical function, two mutually exclusive data-plane
bindings:

- **Control plane:** the `onic` netdev (`ens2` on this host, operator-
  configured via `PYRO_DEVICE_IFACE` — deliberately no default) carries the
  R78 raw-Ethernet control protocol.
- **Data plane:** the QDMA ST char-dev (`qdma-pf`, the
  `pyro-open-nic-compat` branch build of `dma_ip_drivers`) carries bulk
  subject bytes for throughput.

`scripts/pyro_dataplane_swap.sh {data|control|status}` swaps the one PF
between them. The static shell — an OpenNIC-derived jumbo shell with
QDMA/EQDMA5.0, H2C packet/error counters at BAR2 `0x5000`/`0x5110`, CMAC
tied off (no live wire traffic yet) — lives in QSPI
(`static_shell_id 0x02020000`). One DFX dynamic region, `pyro_rp` (slot 1,
SLR2), receives every generated circuit as a partial bitstream linked
against the locked static checkpoint
(`hw/dfx/build/dcp/static_routed_locked.dcp`).

## 4. Life of a pattern (the PYRO path)

1. **Interception.** `pyro.install()` patches stdlib `re`. Every compile
   and scan is classified (`pyro/_classify.py`): is the pattern
   HW-eligible, is the subject large enough, is the pattern reused enough
   to amortize synthesis?
2. **Generation.** `pyro/hdl/automaton.py` lowers the pattern (bytes or
   UTF-8 encoding) to a byte-level one-hot NFA — literals, classes,
   branches, bounded and unbounded repeats, anchors; ASCII case-fold per
   R15. Anything the fabric cannot represent exactly is either rejected or
   recorded as a declared over-approximation class (R19c).
   `pyro/hdl/generator.py` emits Verilog at 1–16 B/cycle datapath widths.
3. **Identity.** A 128-bit domain-separated hash of the pattern descriptor
   is baked into the circuit's `CIRC_ID0..3` CSRs.
   `GENERATOR_VERSION`/`HARNESS_VERSION` (currently 2.3.0) are pinned in
   both the Python emitter and the C runtime (`src/pyro_rt.c`), which
   rejects mismatches (R47b).
4. **Synthesis.** `pyro/synth/service.py` runs an out-of-process service
   over mock or Vivado toolchains. The Vivado PR flow links the child
   against the locked static, `pr_verify`s it (mandatory gate), and emits
   `partial.bit` + a manifest recording fmax, LUTs, and timing honesty.
   Bitstreams are cached (`pyro/synth/cache.py`,
   `$PYRO_CACHE_DIR` or `~/.cache/pyro/bitstreams`) under a key covering
   pattern bytes, encoding, flags, generator/toolchain/shell versions, and
   datapath width — misconfiguration fails loud before tool time and never
   poisons the cache.
5. **Dispatch.** Scans route to the resident circuit (or its software
   model stand-in), candidate windows come back, and CPython `re`
   re-verifies every window before the caller sees anything (R19).

## 5. Life of a Snort rule (the SNORT-PF path)

All in `pyro/snort/`:

1. **Parse** (`rules.py`): the full Snort 3 grammar for the community
   ruleset — 4,017 alert rules. Parsing is total; a line that fails to
   parse degrades to always-forward, never an exception.
2. **Triage** (`triage.py`, SR1/SR2): a sticky-buffer state machine walks
   each rule's option chain and classifies it into exactly one tier —
   `header-only` (95 rules: decidable from the L3/L4 header, handled
   host-side), `anchor-compilable` (3,896: has a usable positive content
   literal), or `always-forward` (26: pcre-only, byte-test-only, or
   negated-content-only — permanently CPU). The anchor is the declared
   `fast_pattern` content, else the longest positive content. The
   raw-anchor vs normalized-buffer sub-tier is the soundness boundary for
   any future suppression claim and is change-controlled in the spec.
3. **Group** (`groups.py`, SR6): rules pack by destination-port class
   (`$HTTP_PORTS`, `$ORACLE_PORTS`, literal-port families, `any`) in
   stable sid order, ≤ 256 rules/group → 21 groups over 8 classes on the
   current corpus. Anchors dedup, so one slot can serve several rules;
   the sidecar maps `pattern_id → [gid:sid, …]`. Deleted rules leave
   tombstones — slot indices are never reused or renumbered — so a weekly
   ruleset diff dirties the minimum number of groups.
4. **Emit** (`groups.group_circuit` → `generator.generate_group`): N
   automata on one shared harness; slot index IS `pattern_id`; a 256-bit
   pend register + priority encoder serializes same-byte accepts into the
   match ring. The group's canonical byte serialization (rule content
   only — deployment variables like a site's `$HTTP_PORTS` value never
   enter it) feeds the group hash; its low-32 bits are the `rp_child_id`
   the daemon verifies before attributing any nomination (SR14).
5. **Nominate.** The host chunks flow payloads into R78 `MATCH_REQUEST`s
   (≤ 1,474 B, overlap tail for anchors split across boundaries),
   receives `MATCH_REPLY` entries, resolves slots through the sidecar, and
   hands nominations to Snort — which re-verifies every one (SR5:
   nomination, never verdict).

## 6. Three views of one artifact

Every circuit has an executable Python model
(`pyro/_circuit_model.py` — `CircuitModel` and the width-independent
`GroupCircuitModel` share one scanner) and a native C model
(`src/pyro_rt.c`) driven by the serialized automaton. RTL, Python model,
and C model are three views of one artifact; differential tests keep them
byte-identical (`tests/hw/xsim_diff.py` drives real R78 frames through the
emitted Verilog under xsim).

Verification of SNORT-PF is two-sided:

- a **closed-form nomination oracle** — an independent implementation that
  computes expected nominations directly from the anchors — asserted as
  set equality per slot; and
- a **Snort differential** (Snort 3.12.2.0 at `/home/gnn/opt/snort3`):
  replay synthesized pcap corpora, assert Snort's alerts on full traffic
  equal its alerts on nominated traffic, with the false-positive census
  pinned by equality per over-approximation class.

The gates are proven to bite: re-created defects and deliberately
sabotaged RTL fail them. On-hardware claims obey SKIP-honesty (SR18):
`device_usable == false` records a canonical SKIP string — a SKIP is never
a PASS, and no simulated PASS is ever recorded.

## 7. Operations

- **Environment:** interpreter `.venv-pyro/bin/python3`; tests run with
  plain `pytest` from the repo root (`tests/unit/`, `tests/acceptance/`,
  `tests/hw/`, `tests/c/`). Hardware clauses key off `PYRO_DEVICE_IFACE`;
  Vivado clauses off `PYRO_VIVADO` (see `docs/vivado-toolchain.md`).
- **Loading a child:** JTAG partial reconfiguration. A partial load wedges
  the RP by design of the flow; recovery is in-band and automatic:
  `scripts/pyro_wedge_recover.sh` (user reset `0x014` + QDMA soft reset
  `0x00C` + `onic` reload).
- **Data-plane swap:**
  `sudo scripts/pyro_dataplane_swap.sh {data|control|status}`.
  Swap back to `onic` only on a healthy RP — swapping onto a wedged card
  panics in `onic_q_handler` (recover first).
- **Bring-up after a power cycle:** check `onic.ko` vermagic against the
  running kernel first; then the recover script + probe restores
  everything (see `docs/device-bringup.md`).
- **Benchmarks:** run with `PYRO_BENCH_QUEUES=1` — ≥ 2 H2C queues silently
  lose frames (the open EQDMA issue; no `tuser_err`, degrades to ~13%
  loss).

## 8. Where things live

| Path | What |
|---|---|
| `specs/python-regex-offload.md` | PYRO spec (v2.7.0) — the R-numbers |
| `specs/snort-rule-offload.md` | SNORT-PF spec — the SR/SF/AC numbers |
| `pyro/hdl/` | automaton, Verilog generator, `rp_wrapper`, identity |
| `pyro/snort/` | rules parser, triage, report, grouping |
| `pyro/synth/` | synthesis service, toolchains, cache, residency |
| `pyro/_circuit_model.py` | Python software models (single + group) |
| `src/pyro_rt.c` | native C runtime/model (frozen ABI 2.0.0) |
| `hw/dfx/` | DFX shell build, locked static DCP |
| `scripts/` | `pyro_hw.py` transport CLI, swap/recover scripts |
| `tests/` | unit / acceptance / hw (xsim) / c |
| `.superpowers/pr-builds/` | Vivado PR drivers + artifacts (crash-fragile) |
| `docs/` | demo, notebook, plans, bring-up, toolchain notes |

## 9. Where it is going

S1 (one real rule on silicon) and S2 (256-rule group on silicon) are
complete. S3 builds all 21 groups, adds the residency daemon that
hot-swaps the resident group by observed port mix (~45 s/swap with
hysteresis), content-chain lowering and clean-pcre fusion in the group
circuits, the SR19 stats surface, and the weekly-diff incremental rebuild
path. S4 — a ROM-baked shared trie holding all 3,896 anchors at once, and
the suppression question — is explicitly gated on owner review.

The honest headline constraint until S4: compilable coverage is 97% of the
corpus, but instantaneous resident coverage is one group ≈ 256 rules
≈ 6.4% under single-tenant residency. Until the shared trie is proven, the
prefilter's value is bounded by how well group rotation tracks the traffic
mix — which is exactly what S3 exists to measure.

## 10. The system file by file — an OS for the FPGA (2026-08-03)

**The thesis the whole tree serves:** treat a partially-reconfigurable
FPGA region the way an OS treats a CPU and its memory: circuits are
*processes*, loading one is an *exec*, swapping content at run time is a
*context switch*, a scheduler decides *what deserves residency*,
identity and epoch counters make attribution safe across switches, and
telemetry is `/proc`. The Snort ruleset is the *workload* that makes
this measurable — 4,017 real rules with naturally shifting demand — not
the product. The dictionary:

| OS primitive | This system |
|---|---|
| CPU + memory | the `pyro_rp` region on SLR2 (SF2 budget) |
| process image | a generated circuit (bitstream) or an overlay *table* (data) |
| exec (heavyweight) | JTAG partial reconfiguration — measured 13.6 s |
| context switch (cheap) | A5 table write — 13.9 ms + 0.150 ms/KB |
| PID / generation | `CIRC_ID` + `TABLE_ID` (CRC-32C) + `EPOCH` |
| scheduler | value-aware residency scoring `V(g)` |
| benign page fault | SR5 over-nomination — Snort re-verifies |
| /proc, perf counters | R45a cycles/bytes CSRs + the telemetry surface |

### `hw/` — the machine itself

- **`hw/dfx/`** — the "motherboard". `build_static.tcl` + `floorplan.tcl`
  build the static OpenNIC-derived shell and carve the one reconfigurable
  partition (`pyro_rp`, SLR2) — the hardware analogue of reserving a
  memory region for user processes. `build_rm.tcl` links a reconfigurable
  module against the *locked* static DCP (processes must fit the ABI of a
  kernel that never rebuilds); `pr_verify.tcl` is the loader's signature
  check; `dfx_build.sh` orchestrates; `platform_manifest.json` pins shell
  identity; `fix_opennic_2025p2.sh` patches upstream for Vivado 2025.2.
- **`hw/pyro_plugin/`** — `pyro_250mhz.sv` + `build_box_250mhz.tcl`
  splice the PYRO box into OpenNIC's 250 MHz user path: the system bus
  between NIC traffic and whatever process is resident.
- **`hw/src/`** — `pyro_id_stub.sv`, `pyro_rp_stub.v`: the *idle
  process*. A region must always contain something that answers identity
  probes; an empty region that reads as valid would be the worst failure,
  so the stub is the init that runs when nothing else does.
- **`hw/rtl/pyro_overlay_engine.v`** — the centerpiece of the cheap
  context switch: a full-corpus Aho–Corasick engine (40,960 states,
  bitmap+popcount transitions in 50 URAM + 108 BRAM36) whose *content is
  written at run time as data* over the same stream corpus bytes arrive
  on. It enforces the OS-style protection in hardware: shadow/active
  regions; commit gated on the host's declared CRC (`A_TBL_EXPECT` — a
  failed commit leaves the working table untouched, added after silicon
  proved the gap); epoch incremented per commit and stamped into every
  match; R45a cycles/bytes counters; a depth-2 input queue implementing
  the wrapper's credit handshake. Meets 253.49 MHz in context.
- **`hw/rtl/pyro_circuit_overlay_top.v`** — the alias that lets the
  engine impersonate a generated `pyro_circuit`: same ports, so the
  loader cannot tell a data-programmable process from a compiled one.

### `pyro/` — the host operating system

**Root: the original product surface (Phase 0–1).** `re.py` is the
drop-in `re`-compatible API; `_classify.py` decides which patterns are
offloadable; `_route.py`/`_thresholds.py` dispatch between hardware and
software with fallback as the safety net; `_match.py`/`_model.py`/
`_circuit_model.py` are the reference matchers every hardware answer is
differentially checked against — the system trusts nothing it cannot
re-derive; `_native.py` binds the C runtime; `testing.py` holds the
sanctioned test seams. These predate the scheduling work but supply its
ground rule: **the FPGA nominates, software verifies**, which is what
makes aggressive scheduling *safe* — a wrong resident process degrades
performance, never correctness.

**`pyro/hdl/` — compiling process images.** `automaton.py` turns
patterns into NFAs; `generator.py` emits synthesizable Verilog per
pattern/group (the compiler back end); `estimator.py` predicts
LUT/FF/BRAM footprint — the "how much memory does this process need"
question every admission decision consults; `identity.py` computes the
hashes that become `CIRC_ID`/`rp_child_id` (PIDs derived from content,
so identity cannot lie); `rp_wrapper.py` generates the harness that
gives every process the same ABI — R78 wire protocol in, R45 CSR
handshake down, R47 results out — and, since A5, the table-load
transport (`ST_TBL_*`) and the latency-agnostic `ST_PERF`. Its
anchored, count-asserted template rewrites are the discipline that lets
one wrapper serve every engine family.

**`pyro/synth/` — the exec path.** `toolchain.py` drives Vivado end to
end (OOC or PR-link against the locked static) — the loader that turns
source into an executable image; `manifest.py`/`artifact.py` record
what was built from what, so a bitstream is never trusted on its
filename; `cache.py` memoizes builds (identical images are reused);
`service.py` runs synthesis asynchronously; `residency.py` is the
original residency manager — the first "scheduler", whose coverage was
later measured at worse-than-static and superseded.

**`pyro/snort/` — the workload.** `rules.py` parses Snort 3 rules;
`triage.py` classifies each as anchor-compilable / header-only /
always-forward (what *can* be a process); `lowering.py` collapses
content chains to anchors + admission conditions, over-approximating
only in the safe direction (SR3: never miss), including the measured
`_pdu_aligned` guard a real differential forced; `groups.py` packs
rules into 21 header-class groups with tombstoned slots and the
slot→`gid:sid` sidecar — the process table, with stable `pattern_id`s
so attribution never shifts; `scheduler.py` is the value-aware
scheduler — `V(g) = Σ_ports decayed_bytes × rules-that-can-fire` with a
2× challenger bar, the fix that took coverage from 1.6–10.3% to within
half a point of the static ceiling; `daemon.py` is the long-running
kernel loop: port-mix histogram (demand sensing), nomination pipeline
with epoch-checked attribution, VarTable for site policy, `Stats` for
accounting; `report.py` renders summaries.

**`pyro/overlay/` — the cheap context switch.** `table.py` builds and
serializes AC tables (`TABLE_ID` = CRC-32C over the image;
`CaseSplitTable` for sound case-insensitivity); `model.py` executes the
*serialized image* exactly as the fabric does — including shadow/commit/
epoch residency semantics — so host and silicon can disagree only if
one of them is wrong, never because the spec was ambiguous. This pair
caught the IEEE-vs-Castagnoli CRC mismatch by construction.

**`pyro/sched/` — the scheduling theory made concrete.** `tenants.py`
defines heterogeneous tenants (pattern, exact-IP, fixed-offset header,
case-split) plus `same_kind_control()`/`matched_treatment()` so
experiments separate scheduling effects from tenant-diversity effects;
`gang.py` is the gang-scheduled pipeline tenant (stages worthless
unless co-resident — `wasted_luts()` is the cost of partial residency);
`deadline.py` carries the deadline tenant, `preemptible_under()`, and
the measured `PR_SWITCH_S = 13.6`. A scheduler with one kind of job is
not a scheduler; this directory is why the claims generalize.

**`pyro/device.py`, `pyro/telemetry.py`.** `device.py` is the syscall
layer: R78 frame codec, probe, `load_partial` (JTAG exec + mandated
in-band recovery), `read_perf_counters`, and the A5 client —
`load_table` (end-to-end identity check against the host's own CRC) and
`read_table_status`. `telemetry.py` is `/proc`: one snapshot schema
covering the four named metrics with honest sourcing (switch time
host-timed; rules matched per-SID via the sidecar; packets dropped from
netdev + sent-vs-answered because no in-fabric drop counter exists;
rules *missed* decomposed into the SR3 invariant, OVF truncations, and
coverage gaps), plus the Prometheus exporter.

### `scripts/` — operations and experiments

- **Operate the machine:** `pyro_hw.py` (probe/load/match/perf CLI),
  `pyro_wedge_recover.sh` (the in-band reboot: user reset + QDMA soft
  reset + driver reload — recovery from the JTAG wedge without a power
  cycle), `pyro_dataplane_swap.sh` (onic↔qdma-pf binding swap),
  `pyro_user_reset.py`, `flash_u250.sh`/`gen_bit_spi.sh`, `setup_*`,
  `status.tcl`.
- **Bring-up and verification:** `pyro_overlay_bringup.py` — the
  on-silicon acceptance run (reset-claims-nothing, identity, epoch,
  model-exact nominations, in-transit-corruption refusal; it found the
  §5 violation); `overlay_ooc_timing.tcl` — timing with validity
  guards, born from the run that reported +0.383 ns on a netlist whose
  URAMs had been deleted; `overlay_sizing_check.py`.
- **The experiment suite** (each backs a study doc):
  `switch_cost_curve.py` (PR cost decomposition),
  `switch_cost_frontier.py` (the `s/P ≈ 0.3` ratio law — the result
  that retired JTAG as a switch mechanism), `policy_experiment.py`
  (policies over control/treatment tenant sets, with the honest
  best-fixed-set baseline), `rule_packing_study.py`,
  `a5_working_set_study.py` (capacity-vs-latency: the study that
  condemned the old scheduler), `pyro_pipeline_bench.py`.
- **The demo/measurement front end:** `pyro_telemetry_demo.py`
  (`--snapshot`/`--watch`/`--serve --drive`/`--demo`/`--paper`) and
  `pyro_paper_figs.py` (camera-ready rendering from `run.json`,
  deliberately hardware-free); `pyro_snortpf_daemon.py`/`pyro_snort.py`
  run the daemon.

### `tests/`, `docs/`, and the rest

**`tests/unit`** (936 green) pin every host component;
**`tests/acceptance`** encode the spec's AC gates with independent
oracles; **`tests/hw`** execute real RTL under xsim — `xsim_diff.py`
(generated engines, byte-exact replies), `tb_overlay_engine_multi.v`
(engine vs model, 85 matches), `overlay_table_diff.py` (the whole A5
wire protocol including the corruption and out-of-order refusals — the
test that exists because the engine once passed while being
unreachable), `tb_pyro_rp_wd.v` (the watchdog that made hangs
diagnosable).

**`docs/`**: `notebook.md` is the lab record — results *and* the
failures that produced them; this file is the cold-reader orientation;
`telemetry.md` the metric inventory with its 29 honest gaps; `demo.md`
the runbook; `spec-amendments-*.md` the governance trail (A5 is the
constitution for loadable content: no table write without attestation);
`studies/` holds each experiment's writeup + raw JSON + `paper-data/`
(three committed silicon runs and camera-ready figures). **`specs/`**
holds the underlying specs the R/SR/SF numbers cite.

**Support:** `src/`+`include/` (the C runtime the `re` API rides),
`third_party/` (the Snort community ruleset — the workload's upstream —
and vendored trees), `web/`+`grafana/` (the live dashboard and its
Grafana/Prometheus form), `amd-case-filing/` (the EQDMA H2C loss
evidence), `build/`, `cong/`, `hd_visual/`, `xsim.dir/` (build and
analysis residue).

### How it composes

Rules (`third_party`) → triage/lowering/packing (`pyro/snort`) → either
a compiled process (`pyro/hdl` → `pyro/synth` → 13.6 s exec) or a table
(`pyro/overlay` → 13.9 ms + 0.150 ms/KB switch) → resident in `pyro_rp`
behind one ABI (`rp_wrapper`) → chosen by `V(g)` from sensed demand
(`daemon`/`scheduler`) → every result attributed via TABLE_ID/EPOCH →
all of it observable (`telemetry`) and measured into figures
(`--paper`). The frontier law says scheduling pays when switch cost is
under ~0.3 of phase length; the tree above is the apparatus that moved
the switch from 13.6 s to 13.9 ms and proved, on silicon, that the OS
analogy holds at the timescales where it matters.
