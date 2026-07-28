# System walkthrough

A guided tour of the whole system for a reader arriving cold: what it is,
what it does today (measured), and how it is built, layer by layer. Written
at the end of Phase S2 (branch `phase2-snort`; spec `snort-rule-offload`
v1.0.2, PYRO spec `python-regex-offload` v2.7.0). The companion documents
are `docs/demo.md` (how to drive it), `docs/notebook.md` (the running lab
record), and `docs/phase2-snort-plan.md` (the delivery plan this branch
follows).

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
- **Data-plane swap:** `sudo scripts/pyro_dataplane_swap.sh {data|control|status}`.
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
| `specs/snort-rule-offload.md` | SNORT-PF spec (v1.0.2) — the SR/SF/AC numbers |
| `pyro/hdl/` | automaton, Verilog generator, `rp_wrapper`, identity |
| `pyro/snort/` | rules parser, triage, report, grouping |
| `pyro/synth/` | synthesis service, toolchains, cache, residency |
| `pyro/_circuit_model.py` | Python software models (single + group) |
| `src/pyro_rt.c` | native C runtime/model (frozen ABI 2.0.0) |
| `hw/dfx/` | DFX shell build, locked static DCP |
| `scripts/` | `pyro_hw.py` transport CLI, swap/recover scripts |
| `tests/` | unit / acceptance / hw (xsim) / c |
| `.superpowers/pr-builds/` | Vivado PR build drivers + artifacts (crash-fragile: verify after unclean reboot) |
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
