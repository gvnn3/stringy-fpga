# Specification: Transparent Python Regex Offload to OpenNIC FPGA

- **Spec ID:** `python-regex-offload`
- **Version:** 2.5.1
- **Status:** Draft (Phase 0 delivered on `phase0-pyro`; architecture inverted for Phase 1+; Phase 1 green; Phase 2 real-Vivado flow in progress on `phase1-pyro`; Phase 2b on-hardware bring-up enabled — control-frame protocol + PR-shell contract + `pyro.device` specified; PR shell flashed and boot-verified on the U250 2026-07-10; in-band R45a counter read-out (R78.11) added; Vivado toolchain re-pinned to 2025.2; Phase-3 slate A1–A4 adopted 2026-07-15 — R3b measurement protocol, R67 seams extended, R73a PR timing-gate scope, F2/F3 demoted to configuration; JTAG PR wedge root-caused and in-band recovery (R85a) folded into `load_partial` 2026-07-15 — first on-silicon R45a counter read)
- **Owner:** Spec Writer
- **Date:** 2026-07-15

---

## 0. Summary

This document specifies a system, hereafter **PYRO** (PYthon Regex Offload),
that transparently accelerates regular-expression matching in Python by
executing pattern matching on the FPGA installed in this host, with automatic,
correctness-preserving fallback to CPython's `re` module for any pattern or
input the hardware path cannot serve.

The target device is the AMD/Xilinx Alveo U250 running the **OpenNIC** shell
(its BDF is host configuration, F2), QDMA-based, under the in-tree `onic`
kernel driver. Custom
matching logic is placed in the OpenNIC **user plugin / dynamic (250 MHz user
box) region**, which is a **partial-reconfiguration (PR) partition**.

**Central design (owner decision, v2.0.0):** PYRO compiles **each** supported
pattern (or pattern set) into a **bespoke synthesizable hardware circuit** —
regex → automaton → RTL — which is then **synthesized** (Vivado synthesis +
place-and-route) into a **PR bitstream** and loaded into the dynamic region.
There is **no** general-purpose programmable automaton engine and **no**
per-pattern automaton *program blob*; every HW-eligible pattern gets its own
generated circuit. Synthesis is **asynchronous background work**: `re.compile()`
returns immediately and matching is served by the **fallback path** (CPython
`re`, and the software model in testing) until that pattern's circuit becomes
**resident** in the PR region, at which point a background **synthesis service**
hot-swaps it in. A persistent **bitstream cache**, keyed by `(pattern_bytes,
flags, generator_version, toolchain_version, shell/PR-region_version)`, means a
previously synthesized pattern loads in **PR-load time (~O(100 ms))** rather than
**synthesis time (minutes)**. This inverts the pre-2.0.0 design, in which a
programmable engine was configured by loadable data; see the §14 changelog.

"Transparent" means a user can obtain acceleration either by changing an import
(`import pyro.re as re`) or by activating an interposition shim that patches the
standard `re` module in place, with **byte-identical results** to CPython `re`
for the supported subset and silent fallback otherwise. Transparency now
explicitly covers **asynchrony**: the returned results NEVER depend on whether a
circuit has been synthesized or is resident — only timing and diagnostics do
(R53, R16).

The specification is written so that:

- an **implementer** (coder) who cannot ask questions, and
- a **test author** (test-developer) who never sees the implementation,

can each work from this document alone. Requirements are numbered `R1, R2, …`
and acceptance criteria `AC-<phase>-<n>` so that tests and docs can reference
them.

---

## 1. Environment & ground-truth hardware facts

These facts are verified on the target host. The design MUST be grounded in
them; implementers MUST NOT assume different hardware.

- **F1.** Host OS: Linux `6.8.0-124-generic` (Ubuntu), x86-64, Intel C620-chipset
  server.
- **F2 (device identity — the stable part is a fact; the BDF is not).** The FPGA
  card is an AMD/Xilinx Alveo U250 (part `xcu250-figd2104-2L-e`) presenting the
  OpenNIC shell to the host as a PCIe **network controller** (vendor `10ee`, PCI
  class `0280`), driven by the in-tree `onic` kernel driver. Its **BDF and its
  function count are host/build configuration, NOT spec facts**: the number of PFs
  is a *shell build parameter* (the PYRO PR shell is built `pf=cmac=1` ⇒ **one**
  PF), and the BDF is a property of the slot and the PCIe topology. PYRO MUST NOT
  hard-code a BDF or a function count; it reaches the device through configuration
  (R68) and proves it has the right one with the **live probe** (R81/R83), which is
  the only device-identity check the spec relies on.
  *Current host (informative, 2026-07-14): a single PF at **`0000:02:00.0`**
  (`10ee:903f`), driver `onic`. This **supersedes and falsifies** the v2.0.0–v2.4.0
  reading of two functions at `af:00.0`/`af:00.1` (`10ee:903f`/`913f`), which
  described the pre-PR-shell image and is **no longer true**: the second function
  does not exist on the PYRO shell.*
- **F3 (host netdev — CONFIGURATION, NOT A FACT).** The card's port appears as an
  `onic` network interface. **The interface's NAME is host configuration and the
  spec does not state it.** This is a correction of **shape**, not merely of value:
  systemd's predictable-naming policy derives the name from whatever the platform's
  firmware exposes (SMBIOS slot index, ACPI index, or PCI path), so the name is
  **not derivable from the BDF by any rule** — the same card is `enp175s0f0` under
  path-based naming and **`ens2`** on this host under SLOT-based naming. A netdev
  name is therefore not the kind of thing that can be a ground-truth fact of this
  spec; the old F3 asserted one, and that was a category error independent of the
  particular string being wrong. The netdev PYRO binds is supplied by the operator
  via **`PYRO_DEVICE_IFACE`** (R68), which consequently has **no spec default** and
  is **fail-closed** (see the R68 amendment).
  *Current host (informative, 2026-07-14): `ens2`, driver `onic`, PCI
  `0000:02:00.0`.*
- **F4.** The OpenNIC shell provides a **user plugin / dynamic region** clocked
  at 250 MHz intended for custom RTL. This is the region PYRO targets.
- **F5.** As of this writing, **no** Vivado/Vitis/XRT tools are on `PATH`
  (`vivado`, `v++`, `xbutil` not found) and **no** `/dev/xdma*` or `/dev/qdma*`
  character devices exist. Toolchain and data-transport enablement are
  prerequisites (see §11), not assumptions.

> **Note on transport reality (F5):** because only the `onic` netdev path
> currently exists, the spec defines **two** transport bindings (§7): a
> **QDMA character-device binding** (the performance target, requires driver/
> char-dev enablement) and a **raw-Ethernet-frame binding** to the NIC function
> (works with the current `onic` netdev, lower performance). The runtime MUST
> select a binding at initialization and MUST NOT hard-code one.

---

## 2. Definitions

- **Supported subset:** the RE2-like regular-expression language defined in §5.
- **Hardware path / fast path:** matching performed on the FPGA engine.
- **Fallback path:** matching performed by CPython's `re` in-process.
- **Corpus / haystack:** the input string/bytes being searched.
- **Pattern / needle:** the compiled regular expression.
- **Generated circuit:** the pattern-specific synthesizable RTL — and the PR
  bitstream synthesized from it — that recognizes exactly one HW-eligible pattern
  (or one pattern set). Produced by the HDL generator (L2) from the pattern's
  automaton. Replaces the pre-2.0.0 "automaton program" concept.
- **Harness contract:** the fixed CSR/DMA interface (§7.4) that **every**
  generated circuit MUST implement, so the host runtime drives any circuit
  uniformly (input/output DMA, START/DONE, result ring, identity).
- **PR region / dynamic region:** the OpenNIC 250 MHz user-box reconfigurable
  partition into which a generated circuit's PR bitstream is loaded. Unless the
  shell proves multiple PR partitions, it holds **one** resident circuit at a
  time (single-tenant, R64).
- **Resident circuit:** the generated circuit currently loaded in the PR region.
  Hardware dispatch of a pattern REQUIRES that pattern's circuit to be resident
  and its identity verified (R47a).
- **Synthesis service:** the out-of-process background service (R63) that turns a
  generated RTL circuit into a PR bitstream (Vivado synth + P&R + PR bitstream
  generation) and populates the bitstream cache.
- **Bitstream cache:** persistent cache of synthesized PR artifacts and their
  manifests, keyed by `(pattern_bytes, encoding, effective_flags,
  generator_version, toolchain_version, shell/PR-region_version)` (R4/R47a).
- **Circuit lifecycle tiers:** **cold** — no cached artifact; synthesis needed
  (minutes, async, never blocks callers); **warm** — artifact cached; PR-load
  needed (~O(100 ms)); **resident** — loaded in the PR region; dispatch
  immediately (R4).
- **Candidate window:** a `[start, end)` byte span the FPGA reports as a
  probable match, which the CPU may re-verify.
- **Group 0:** the whole-match span, as in CPython `re` (`Match.span(0)`).
- **Byte-identical:** for the same inputs, PYRO returns the same match/no-match
  decision and the same integer offsets and captured substrings that CPython
  `re` would return, per §6.
- **Drop-in (duck-typed compatibility):** a PYRO object is a "drop-in" for a
  CPython `re` type when it provides every documented method and attribute of
  that type (§7.1 R27/R28) with byte-identical behavior (R16), such that code
  invoking those methods/attributes cannot observe a difference. Drop-in
  compatibility is **duck-typed**, not nominal: because CPython's `re.Pattern`
  and `re.Match` are concrete, non-subclassable, non-ABC C types, a PYRO wrapper
  on the hardware/model path **cannot** and is **not required to** satisfy
  `isinstance(obj, re.Pattern)` / `isinstance(obj, re.Match)`. Identity-based
  type checks against these concrete types are a documented, permanent
  limitation (see R36 and §12), not a correctness defect.

---

## 3. Feasibility summary and performance thresholds

Offload is worthwhile only under specific regimes. The following are the design
targets used as measurable acceptance criteria (see phase ACs and §9 benchmark
suite). Thresholds are stated as requirements so tests can assert them; exact
numeric targets may be revised by a version bump if measurement on real hardware
proves them wrong, but they MUST NOT be silently ignored.

- **R1 (win regime — throughput, resident circuit).** Hardware dispatch REQUIRES
  the target pattern's generated circuit to be **resident** (§2, R51). For a
  resident circuit applied to a streamed corpus of total size ≥ **1 MiB**, the
  hardware path SHALL sustain match throughput of at least **5 GiB/s** aggregate
  scan rate, measured end-to-end from host memory to result. A generated circuit
  matches with a dedicated per-pattern datapath in the 250 MHz user box; the
  floor for any resident circuit is **≥ 1 GiB/s**, and simple circuits SHOULD
  reach the 5 GiB/s target with a multi-byte-per-cycle datapath. Throughput is
  measured only for resident circuits; time spent cold (synthesizing) or warm
  (PR-loading) is excluded from the scan-rate metric and accounted separately
  (R4, R2).
- **R2 (win regime — reuse amortization, incl. synthesis).** Amortization now
  includes the **one-time synthesis cost** (cold tier, minutes) and the
  **PR-load cost** (warm tier, ~O(100 ms)), not merely DMA setup. Accordingly:
  - **R2a.** Once a pattern's circuit is **resident**, when that pattern is
    reused across at least **N_reuse = 32** search calls, or applied to one
    corpus of at least **S_min = 64 KiB**, the median end-to-end wall-clock time
    of the hardware path SHALL be ≤ the CPython `re` path for the same work on
    the reference benchmark set (§9).
  - **R2b.** Because synthesis is minutes-long and asynchronous, the **break-even
    reuse** that justifies *launching* synthesis is far higher than N_reuse. The
    synthesis-launch policy (R4a) governs WHEN a circuit is worth building; until
    it is resident, calls are served by fallback at no worse than the loss-regime
    bound (R3). Synthesis and PR-load costs MUST NOT appear as caller-visible
    latency (they run in the background, R63) beyond the fallback cost the caller
    would have paid anyway.
- **R3 (loss regime — routing, not slowdown).** For inputs below the offload
  threshold (corpus < `S_min` **and** effective reuse < `N_reuse`), PYRO SHALL
  route to the fallback path with bounded added overhead, measured relative to
  calling CPython `re` directly. The bound applies in two forms:
  - **R3a (absolute, all phases).** The added routing/decision overhead SHALL be
    ≤ **2 µs** median per top-level call (this is the same quantity bounded by
    R5). This form is the governing bound whenever the hot path is not native
    code — in particular for the **Phase-0 pure-Python shim**, where stock
    `re.search` on a short subject is sub-microsecond C code and no Python-level
    wrapper can meet a small *relative* ratio even though its *absolute* added
    cost (~1 µs) is well under this bound.
  - **R3b (relative, native-runtime phases).** From **Phase 1 onward** (i.e.,
    once the loss-regime hot path is served by the native host runtime, L3, or by
    interposition at a level where the delegation cost is native-code cheap),
    PYRO SHALL additionally keep below-threshold wall-clock time within **1.15×**
    of calling CPython `re` directly (routing/decision overhead ≤ 15%). The
    constant **1.15× is retained unchanged** (v2.5.0; see the vindication note
    below). What this amendment adds is the **measurement protocol**: a ratio
    bound whose statistic, trial count, and subject size are unstated is not a
    decidable bound, and R3b was being checked by a single trial at an unpinned
    subject size.
    - **R3b.1 (statistic — median of ≥ 5 trials; normative).** The R3b verdict
      SHALL be `median(trial_ratio_1 … trial_ratio_k)` with **k ≥ 5**. One
      **trial** is a complete measurement run over the R3b.2 subject that yields
      one `trial_ratio = median(PYRO per-call ns) / median(stock-`re` per-call
      ns)`, measured over **≥ 1000 paired calls** on distinct loss-regime patterns
      (each pattern's reuse kept `< N_reuse` and each subject `< S_min`, so every
      call is a genuine §8 below-threshold routing decision), with the
      classification/compile caches **primed on both sides** before timing so the
      ratio reflects the routing path and not compilation (R4). A **single trial
      MUST NOT** decide R3b. An individual trial MAY exceed 1.15× without
      violating R3b; only the median of ≥ 5 trials binds.
    - **R3b.2 (subject size is pinned — normative).** The R3b bound is stated
      **at a subject of exactly 132 bytes** (ASCII; 132 code points in `str` mode).
      The ratio is a strong function of subject size — the routing tax is a
      roughly fixed per-call cost divided by a scan cost that grows with the
      subject — so an unpinned R3b is meaningless: the same router measures
      **≈1.31× at 3 bytes** and **≈1.02× at 1 KiB**. Ratios at other sizes MAY be
      reported as informative, MUST be labelled with their size, and MUST NOT be
      used to claim or deny R3b compliance.
    - **R3b.3 (R3b binds against the compiled-extension build).** R3b's subject is
      the **native** routing/decision hot path (R3c), which exists only in a build
      with the compiled L3 extension present. R3b therefore **binds against the
      compiled-extension build**. A **pure-Python install** (no compiled
      extension) does **not** satisfy the R3c precondition and is **expected to
      fail the ratio**; it MUST record the honest **R3c SKIP-with-measured-ratio**
      (whose reason names the pure-Python build **and** states the measured
      median), and — where R3b is a hard PASS requirement (AC-3-3) — the AC's R3b
      clause MUST NOT be claimed PASS on such a build. A pure-Python install
      **MUST NOT** report R3b as a silent PASS by any route, including by
      degenerate measurement (too few trials, an over-large subject, or a ratio
      taken at a size other than R3b.2). SKIP and FAIL are both honest; a silent
      pass is a defect (R71 honesty discipline, applied to R3b).
    - **R3b.4 (warmup must remain in the loss regime — normative).** Any CPU
      warmup performed before timing MUST itself exercise the **loss-regime
      routing path**: every warmup pattern's reuse count MUST stay `< N_reuse`
      for the duration of warmup (e.g. by rotating ≥ ⌈warmup_calls / (N_reuse−1)⌉
      throwaway patterns), and warmup subjects MUST stay `< S_min`. Rationale
      (measured, 2026-07-14, nf-server06): the previous protocol warmed with a
      **single** throwaway pattern for 3000 calls; that pattern crosses
      `N_reuse = 32` at call 33 and is routed to the **model** verdict for the
      remaining 2967 calls, so the code path being *measured* (the loss-regime
      fast path) is left cold in the branch predictor and icache. Controlled A/B
      on the native router, identical harness, warmup style the only variable:
      single-pattern warmup → **1.26–1.30** (5/5 trials fail); rotating warmup
      (100 patterns × 30 uses) → **1.05–1.09 typical** (median of 5 = 1.094,
      passes). The single-pattern warmup violates its own stated intent ("CPU
      warmup … so the measured patterns' reuse stays low") and measures a
      half-cold path.

  Rationale: the 1.15× relative bound is only physically meaningful once the
  decision path is native; expressing it as an absolute bound for Phase 0 (R3a)
  preserves the intent — negligible routing tax — without demanding a ratio that
  is unachievable for a Python wrapper around sub-microsecond C code.

  **Vindication of the 1.15× constant (v2.5.0, informative-but-load-bearing).**
  Measurement on the native router (132-byte subject, R3b.2) gives a **median of
  ≈1.10× ± 0.04**, with a worst observed trial of **≈1.15×**; a native
  delegation **floor** — the irreducible cost of entering and leaving a native
  routing decision at this subject size — measures **≈1.01×**. Two conclusions
  follow, and both support keeping the constant. First, R3b's own rationale is
  **vindicated**: the tax is ~1% once the decision path is native, so a 15% budget
  is a real budget with genuine headroom, not an impossibility — exactly the claim
  R3b/R3c rest on. Second, the constant is **defensible but not slack**: at
  1.10× median the implementation sits ~4 points inside a 15-point budget, which
  is why the single-trial check was flaky (±1.5% run-to-run noise against a 4-point
  margin, with worst trials touching the bound) and why R3b.1's median-of-≥5
  statistic — not a loosened constant — is the correct repair.
  - **R3c (R3b's native-hot-path precondition is the gate — normative).** The
    R3b relative bound binds **only when its own precondition holds**: the
    loss-regime **routing/decision hot path** (the §8 R51 decision) is itself
    served by native code (L3 or native-cheap interposition). The mere existence
    of an L3 native runtime does **not** satisfy the precondition if the routing
    decision that governs a below-threshold call is still executed at Python level.
    While the routing decision is Python-level, **R3a (absolute ≤ 2 µs)
    governs** and any R3b relative-ratio check MUST record a **SKIP** — not a
    FAIL — whose reason states the measured ratio and that the native routing hot
    path is not yet in place (mirroring the Phase-0 disposition in AC-0-6). The
    measured ratio cited in that SKIP MUST be the R3b.1/R3b.2 statistic (median of
    ≥ 5 trials at the pinned 132-byte subject), not a single trial. R3b
    becomes a **hard PASS requirement at AC-3-3** (Phase 3 interposition +
    benchmarks), the point at which the native routing/dispatch path is a
    deliverable. This does not weaken R3: the negligible-routing-tax intent is
    enforced at all times by the absolute R3a bound.
- **R4 (compile amortization and cache tiers).** PYRO SHALL maintain two distinct
  caches with three service tiers:
  - **Host classification cache (warm, µs-scale).** Keyed by `(pattern_bytes,
    encoding, effective_flags, generator_version)`, holds the eligibility
    decision (R8) and the generated automaton/RTL descriptor. `encoding` is the
    `PYRO_ENC_BYTES`/`PYRO_ENC_UTF8` tag and `effective_flags` are the
    canonicalized post-inline-extraction flags — both per R47a; the host MUST
    canonicalize before lookup. A repeated `compile()` of the same pattern MUST
    be served from this cache in ≤ **50 µs** on the host side. This tier's shape
    matches pre-2.0.0 except for the now-explicit encoding tag and effective-flag
    canonicalization, and governs `re.compile()` latency.
  - **R4b (canonicalization scope).** The R4/R47a **canonicalization** requirement
    (stripped leading global inline flags + effective flags + encoding tag)
    applies to every cache/key where **circuit reuse** is decided: the host
    **classification/descriptor** cache above, and the **circuit-identity** and
    **bitstream** keys (R47a/R47b). It does **not** apply to the L1
    compiled-`Pattern` object cache (§7.1): that cache MAY key on the **raw**
    `(pattern, flags)` as supplied by the caller, because collapsing e.g.
    `(?i)abc` and `("abc", re.I)` — which canonicalize identically — into one
    `Pattern` object would break `Pattern.pattern` fidelity (R27: stock `re`
    reports different `.pattern` strings for those two). Thus two raw-distinct
    patterns MAY share one generated circuit (canonical key) while remaining two
    distinct `Pattern` objects (raw key); this is correct and required.
  - **Bitstream cache (persistent).** Keyed by `(pattern_bytes, encoding,
    effective_flags, generator_version, toolchain_version,
    shell/PR-region_version)`, holds synthesized PR bitstream artifacts and their
    manifests (R47b); `encoding` and `effective_flags` are as in R47a. Its
    filesystem location is the spec-named `PYRO_CACHE_DIR` (R68) when set, else a
    runtime default. Its three tiers are:
    - **cold** — no artifact for the key: synthesis is required (minutes),
      performed asynchronously by the synthesis service (R63); the caller is
      served by fallback meanwhile and is NEVER blocked;
    - **warm** — artifact present but not resident: a PR load is required
      (~O(100 ms)), performed in the background; the caller is served by fallback
      until resident;
    - **resident** — the circuit is loaded in the PR region and identity-verified
      (R47a): the call dispatches to hardware immediately.
  - **R4 invariant.** A given `(key)` transitions cold → warm at most once per
    `toolchain_version`/`shell_version`; a warm artifact loads in PR-load time,
    not synthesis time, across process restarts.
- **R4a (synthesis-launch policy).** Synthesis is expensive; PYRO MUST NOT launch
  it for every pattern. A circuit's synthesis SHALL be launched (enqueued to the
  service, R63) only when at least one of the following holds:
  - the pattern has been dispatched HW-eligible at least **N_synth** times
    (default **N_synth = 1000**, overridable via the spec-named `PYRO_N_SYNTH`,
    R68), i.e. reuse proves the pattern hot; or
  - the pattern is explicitly requested via `pyro.prewarm(patterns, flags=0)`
    (R62), which enqueues synthesis regardless of call count.
  The policy MUST be deterministic given the call history and configuration.
  Launching synthesis MUST NOT block or slow the triggering call (it returns via
  fallback). A pattern whose circuit is not yet resident is always served by
  fallback (R3/R51); the launch policy only decides WHEN building is worthwhile.
- **R5 (decision latency).** The offload-vs-fallback routing decision (§8) SHALL
  add ≤ **2 µs** median overhead per top-level API call relative to the raw
  fallback, excluding actual match work.

**Rationale (informative):** FPGA offload wins when per-byte scan cost dominates
and fixed DMA/setup costs are amortized (large corpora, streaming, many reuses of
the same pattern). CPU wins for short one-shot strings where PCIe round-trip
latency (µs-scale) dwarfs the match itself.

---

## 4. Architecture

PYRO is layered. Each layer has a defined interface so layers can be developed
and tested independently.

```
+-------------------------------------------------------------+
| L0  Transparent interposition shim  (import hook / patcher) |  Phase 3
+-------------------------------------------------------------+
| L1  Python API layer  (pyro.re: compile/match/search/...    |  Phase 0
|     + prewarm/stats/refresh_env)                             |
+-------------------------------------------------------------+
| L2  Pattern compiler + HDL generator                        |  Phase 0/1
|     regex -> AST -> automaton -> synthesizable RTL circuit   |
|     + supported-subset gate + fallback classifier + resource |
|       estimator                                              |
+-------------------------------------------------------------+
| L3  Host runtime library (C ABI) + Python bindings          |  Phase 1
|     - classification + bitstream cache, PR loader,           |
|       synthesis-service client, scheduling, result assembly  |
+-------------------------------------------------------------+
| L4  Transport binding (QDMA char-dev OR raw Ethernet frame) |  Phase 1
+-------------------------------------------------------------+
| L5  Generated per-pattern circuit in OpenNIC PR region      |  Phase 1/2
|     - implements the fixed harness contract (§7.4)           |
|     - one resident circuit at a time (single-tenant PR)      |
+-------------------------------------------------------------+

        Out-of-process, asynchronous (side service):
+-------------------------------------------------------------+
| SS  Synthesis service (R63)                                 |  Phase 1/2
|     RTL -> Vivado synth + P&R -> PR bitstream -> cache       |
|     queue; concurrency 1 PR region; eviction policy         |
+-------------------------------------------------------------+
```

- **R6.** Each layer L0–L4 SHALL expose a stable interface (Python types for
  L0–L2, a C ABI for L3, a byte/register protocol for L4→L5, and a job/artifact
  interface for the synthesis service, §7.5) as specified in §7. A layer MUST be
  testable using a mock of the layer beneath it, and the synthesis service MUST
  be testable with a **mock toolchain** that emits a stub artifact (R63).
- **R7.** The system SHALL include a **software model** that stands in for a
  **generated circuit**: a pure-software reference implementation exercising the
  exact same L3/L4 harness contract (§7.4) so that Phases 0–2 are testable
  without physical hardware or a real toolchain. For any HW-eligible pattern, the
  model MUST produce results identical to the specified hardware behavior of that
  pattern's generated circuit. The model stands in for the "resident" tier (R4)
  when `PYRO_FORCE_MODEL=1` or no device is present. When no physical device is
  present, R7 takes precedence over R51 step 5: the model MAY serve HW-eligible
  dispatches at any tier while tier state is still tracked (R51b).
- **R8.** The pattern compiler (L2) SHALL classify every input pattern as either
  **HW-eligible** (in the supported subset and within resource limits) or
  **fallback-only**, and this classification MUST be a pure function of the
  pattern and flags (deterministic, side-effect free).

---

## 5. Supported regular-expression subset

The hardware path supports an **RE2-like, regular** language. Anything outside
this subset MUST route to fallback (§8). The subset is defined precisely so the
compiler's acceptance decision is unambiguous.

### 5.1 Supported constructs

- **R9.** The following constructs are HW-eligible:
  - Literal characters and literal byte sequences.
  - Character classes: `[...]`, negated `[^...]`, ranges `[a-z]`, and the
    shorthand classes `\d \D \w \W \s \S`, and `.` (any char; honoring
    `re.DOTALL`).
  - Concatenation.
  - Alternation `|`.
  - Quantifiers `*`, `+`, `?`, and bounded `{m}`, `{m,n}`, `{m,}` where the
    upper bound `n` (or `m` when unbounded above) satisfies the repetition limit
    in §5.3. Greedy and non-greedy (`*?`, `+?`, `??`, `{m,n}?`) forms are
    accepted; because group 0 span for a leftmost match is identical regardless
    of greediness only when there are no capturing-group observations, see §6.4
    for the exact rule.
  - Anchors `^`, `$`, `\A`, `\Z`, `\b`, `\B` (subject to §6.5 multiline/anchor
    semantics).
  - Non-capturing groups `(?:...)`.
  - Capturing groups `(...)` and named groups `(?P<name>...)` — **structure**
    is accepted (they define the language), but group *extraction* is handled by
    the hybrid rule in §6.3.
  - Inline/`flags` support for `re.IGNORECASE` (ASCII and Unicode-simple case
    folding per §5.4), `re.MULTILINE`, `re.DOTALL`, `re.ASCII`, `re.VERBOSE`.

### 5.2 Unsupported constructs (force fallback)

- **R10.** The following force the whole pattern to fallback-only:
  - Backreferences (`\1`, `(?P=name)`).
  - Lookahead / lookbehind (`(?=...)`, `(?!...)`, `(?<=...)`, `(?<!...)`).
  - Conditional patterns `(?(id)yes|no)`.
  - Atomic groups / possessive quantifiers `(?>...)`, `a*+` (Python 3.11+).
  - Recursion / subroutine calls.
  - Any construct not enumerated in §5.1.
  - Patterns whose compiled automaton exceeds the resource limits of §5.3.

### 5.3 Resource / capacity limits

- **R11.** The system SHALL advertise, at runtime, the **PR-region resource
  budget** a single generated circuit must fit within (see the capability
  register block and manifest, §7.4): available LUTs, flip-flops, BRAM, and DSP
  in the dynamic region, the harness datapath width, and the generator/harness
  versions. It SHALL also advertise the derived **complexity bounds** the L2
  resource estimator uses to decide fit without a full synthesis: maximum
  automaton states `MAX_STATES`, maximum concurrent patterns per circuit
  `MAX_PATTERNS`, and maximum bounded-repeat expansion `MAX_REPEAT`.
- **R12.** The compiler/estimator (L2) SHALL reject (route to fallback) any
  pattern whose generated circuit is estimated to exceed the PR-region resource
  budget or any advertised complexity bound. Bounded repeats are expanded prior
  to the estimate; a repeat expanding beyond `MAX_REPEAT` is fallback-only. If a
  circuit passes the estimate but a later real synthesis (R63) fails to fit or
  meet timing, the pattern becomes **permanently fallback-only** with a
  diagnostic (R65); this is not a caller-visible error.
- **R13.** Default advertised minimums (software model and Phase-1/2 targets)
  MUST be at least: `MAX_STATES ≥ 1024`, `MAX_PATTERNS ≥ 256`,
  `MAX_REPEAT ≥ 255`. The PR-region resource budget is device/shell-specific and
  is advertised from the manifest (R47b), not hard-coded in software (P5).

### 5.4 Character encoding and case folding

- **R14.** PYRO SHALL support two subject-encoding modes:
  - **bytes mode** (pattern and subject are `bytes`/`bytearray`): the engine
    operates on raw octets; classes and `.` follow CPython bytes semantics.
  - **str mode** (pattern and subject are `str`): the subject is encoded to
    **UTF-8** for transport; the engine matches over UTF-8 code units, and the
    compiler MUST translate Unicode code-point classes/literals to equivalent
    UTF-8 byte automata. All reported offsets MUST be converted back to Python
    `str` code-point indices (§6.2) before returning to the caller.
  - **R14a (surrogate / non-UTF-8-encodable gate).** A Python `str` may contain
    **unpaired surrogates** (e.g., data decoded with `errors="surrogateescape"`
    from files or OS interfaces — a common case in the large-log-corpus regime).
    Such a subject **or** pattern cannot be strictly UTF-8-encoded, yet stock
    CPython `re` matches it. In str mode, if the subject or the pattern fails
    strict UTF-8 encoding (i.e., contains any unpaired surrogate or is otherwise
    not `str.encode("utf-8")`-able), the call is **fallback-only**: the hardware/
    model path MUST NOT see it, and PYRO MUST route to CPython `re`. This is
    ordinary fallback **routing** (R51), not a device error: it MUST NOT raise,
    MUST NOT be counted as `fallback_after_error` (R52), and MUST be counted as a
    normal fallback dispatch in stats (R35/§9). The eligibility decision (R8) MAY
    detect a non-encodable pattern at compile time; a non-encodable subject is
    detected at call time in the R51 decision order (§8, R51 gate d).
- **R15.** For `re.IGNORECASE` in str mode, only **ASCII and Unicode simple
  (1:1) case folding** is HW-eligible. Full/multi-character foldings (e.g., `ß`
  ↔ `ss`) are fallback-only. In bytes mode, IGNORECASE applies ASCII folding
  only (matching CPython).

---

## 6. Correctness requirements

Correctness is the top priority: **an accelerated result MUST never differ from
CPython `re`**. When in doubt, fall back.

- **R16 (byte-identical top-level results).** For any pattern classified as
  HW-eligible and any subject, every PYRO API call in §7.1 SHALL return a result
  that is byte-identical (same match/no-match, same offsets, same captured
  substrings, same group structure) to the equivalent CPython `re` call with the
  same pattern, flags, and arguments. Where this cannot be guaranteed, the call
  MUST fall back.
- **R17 (leftmost-longest vs leftmost-greedy reconciliation).** CPython `re` is a
  backtracking engine with **leftmost-greedy** semantics; RE2/NFA engines are
  naturally **leftmost-longest** (POSIX) for the overall match. The FPGA engine
  MAY compute a candidate match position under either policy, but PYRO MUST
  return the **CPython leftmost-greedy** group-0 span. This is guaranteed by the
  hybrid rule R18: the FPGA locates candidate windows; final group-0 boundaries
  reported to the caller MUST equal what CPython produces.

### 6.1 Hybrid matching model (capture groups)

- **R18 (hybrid extraction).** The FPGA engine reports, for each match, at least
  the group-0 candidate window `[start, end)` (see §7.4 result format). If the
  pattern contains **capturing groups** whose values the caller observes
  (`.group(n>0)`, `.groups()`, `.groupdict()`, `.span(n>0)`, or `\g<n>`/`\n` in a
  replacement/expand template), PYRO SHALL:
  1. Use the FPGA-reported group-0 window (possibly widened per R19) to locate
     the match region, then
  2. Re-run **CPython `re`** anchored at the reported start over that region to
     extract exact group boundaries and to confirm the group-0 span, and
  3. Return the CPython result.

  This "FPGA finds candidate windows, CPU re-runs for groups" design guarantees
  R16 for capture groups while keeping the per-byte scan on the fabric.
- **R19 (candidate soundness/completeness).** The generated circuit (or software
  model), for HW-eligible patterns, MUST be **complete for group 0**: it MUST
  report a candidate window covering every position where CPython would find a
  match, and MUST NOT omit any match. **Completeness (no false negatives) is
  mandatory and absolute.**
  - **R19a (deliberate over-approximation is sanctioned).** The generated
    automaton MAY intentionally **over-approximate** — recognize a *superset* of
    the true match starts and thereby emit **false positives** — as a legitimate
    generator strategy for constructs that are expensive to encode exactly in
    RTL (e.g. full Unicode general categories, cross-length / multi-byte case
    folds, `\b` at UTF-8 code-point boundaries). This is **not a defect**,
    provided:
    1. completeness (R19) still holds — over-approximation never drops a true
       match; and
    2. PYRO **re-verifies every reported window** with CPython (or the software
       model) before returning it to the caller, so caller-visible results remain
       byte-identical (R16). Unverified false positives are a defect; a false
       positive that survives re-verification into a returned result is a defect.
  - **R19b (bounded false-positive rate).** Over-approximation MUST be bounded
    tightly enough not to destroy the win regime (R1/R2): the added
    re-verification cost is CPU work proportional to the false-positive rate, and
    an over-approximation that makes the hardware path slower than plain fallback
    defeats its purpose. The generator SHOULD prefer the tightest encoding that
    fits the PR-region budget (R11/R12); a pattern whose only feasible circuit has
    a ruinous false-positive rate SHOULD be classified fallback-only rather than
    synthesized.
  - **R19c (manifest declares over-approximation classes).** When a generated
    circuit over-approximates, its manifest (R47b) MUST declare the
    **over-approximation classes** it uses (e.g. `unicode_category`,
    `cross_length_casefold`, `word_boundary_utf8`) and, where feasible, an
    estimated false-positive rate, so the benchmark suite (R59) can **attribute
    re-verification cost** and so R19b can be checked. An exact (non-over-
    approximating) circuit declares an empty set.
  - False negatives are never permitted for HW-eligible patterns.
- **R20 (lazy group extraction).** For match objects returned by `search`/`match`
  /`fullmatch`/`finditer`, PYRO MAY defer the CPython group re-run until the
  caller first accesses a group > 0; accessing only `group(0)`/`span(0)`/`start`/
  `end` MUST NOT trigger a re-run. The returned object MUST be a drop-in for
  `re.Match` per §7.1.

### 6.2 Offset semantics

- **R21.** All offsets returned to the caller MUST be in the **caller's units**:
  code-point indices for `str` subjects, byte indices for `bytes` subjects —
  exactly as CPython `re`. Internal UTF-8 byte offsets (R14) MUST be translated
  back. Translation MUST be exact even for astral (≥ U+10000) code points.

### 6.3 Empty matches

- **R22.** Zero-width matches (e.g., pattern `a*` against `"bbb"`, or `^`/`$`
  anchors, or `\b`) MUST replicate CPython semantics exactly, including the
  CPython rule (Python 3.7+) that `findall`/`finditer`/`sub` advance past an
  empty match adjacent to a previous match. `finditer` iteration MUST yield the
  same sequence of (possibly empty) matches CPython yields.

### 6.4 Greediness observability

- **R23.** Because group-0 span can depend on greediness only through captured
  subgroups or alternation ordering, and PYRO returns CPython group-0 spans via
  R17/R18, no additional requirement is needed beyond R16. Test-developer note:
  differential tests (§9) MUST include patterns where greedy vs. lazy changes
  group boundaries to prove R17/R18.

### 6.5 Flags & anchors

- **R24.** `re.MULTILINE` changes `^`/`$` to match at line boundaries; the
  compiled automaton MUST encode this. `$` in non-MULTILINE mode matches at end
  of string and immediately before a trailing `\n` (CPython semantics). `\Z`
  matches only at end of string. All these MUST be byte-identical to CPython.
- **R25.** Flag combinations that the compiler cannot faithfully encode MUST
  route to fallback rather than approximate.

---

## 7. Interfaces

### 7.1 Python API (L1) — `pyro.re`

The module `pyro.re` MUST provide a surface that is a superset-compatible
drop-in for the subset of the standard `re` module listed here.

- **R26 (module-level functions).** The supported host runtime is **CPython
  ≥ 3.11** (the target host runs 3.12). Rationale: the classifier/compiler front
  end (L2) uses the `re._parser` / `re._constants` modules, which exist under
  those names only on CPython 3.11+; the 3.10 `sre_parse`/`sre_constants` aliases
  are not supported and buy nothing for the target. PYRO MAY refuse to import on
  CPython < 3.11 with a clear error. Provide, with signatures and semantics
  matching CPython `re` (Python 3.11+):
  - `compile(pattern, flags=0) -> Pattern`
  - `search(pattern, string, flags=0) -> Match | None`
  - `match(pattern, string, flags=0) -> Match | None`
  - `fullmatch(pattern, string, flags=0) -> Match | None`
  - `findall(pattern, string, flags=0) -> list`
  - `finditer(pattern, string, flags=0) -> iterator[Match]`
  - `sub(pattern, repl, string, count=0, flags=0) -> str|bytes`
  - `subn(pattern, repl, string, count=0, flags=0) -> (str|bytes, int)`
  - `split(pattern, string, maxsplit=0, flags=0) -> list`
  - `escape(pattern)`, `purge()`.
  - The flag constants `A ASCII I IGNORECASE M MULTILINE S DOTALL X VERBOSE`
    (and `U UNICODE` accepted as no-op for str) as aliases of `re`'s values.
  - `error` MUST be `re.error` (same exception type) so callers' `except`
    clauses are unaffected.
- **R27 (Pattern object).** The compiled `Pattern` MUST be a **drop-in**
  (duck-typed compatibility, §2) for `re.Pattern`: it MUST expose
  `search/match/fullmatch/findall/finditer/sub/subn/split` methods with the same
  `pos`/`endpos` parameters and semantics as `re.Pattern`, plus the attributes
  `pattern`, `flags`, `groups`, `groupindex`. Because `re.Pattern` is a concrete,
  non-subclassable C type, `isinstance(p, re.Pattern)` MUST NOT be relied upon on
  the hardware/model path and is a documented limitation (§2, R36, §12); on the
  fallback path the object IS a genuine `re.Pattern` (R29).
- **R28 (Match object).** Returned match objects MUST be a **drop-in**
  (duck-typed compatibility, §2) for `re.Match`: they MUST support
  `group([n...])`, `groups(default=None)`, `groupdict(default=None)`, `start([n])`,
  `end([n])`, `span([n])`, `__getitem__`, `expand(template)`, and the attributes
  `pos`, `endpos`, `lastindex`, `lastgroup`, `re`, `string`. Behavior MUST match
  `re.Match`. Because `re.Match` is a concrete, non-subclassable C type,
  `isinstance(m, re.Match)` MUST NOT be relied upon on the hardware/model path
  and is a documented limitation (§2, R36, §12); on the fallback path the object
  IS a genuine `re.Match` (R29).
- **R29 (semantic equivalence).** For HW-eligible patterns, every method in
  R26–R28 MUST satisfy R16 (byte-identical). For fallback-only patterns, the
  method MUST delegate to CPython `re` and return its result unchanged.
- **R30 (exceptions).** Invalid patterns MUST raise `re.error` with the same
  triggering conditions as CPython (i.e., `pyro.re.compile` raises `re.error`
  iff `re.compile` would). PYRO MUST NOT accept a pattern CPython rejects.
- **R31 (introspection hook).** Provide `pyro.re.explain(pattern, flags=0) ->
  dict` returning at least the keys `{"eligible": bool, "reason": str, "engine":
  "fpga"|"model"|"fallback", "states": int|None}` (existing keys retained for
  compatibility) plus the v2.0.0 circuit-lifecycle keys `{"circuit_status":
  "cold"|"warm"|"resident"|"synthesizing"|"fallback_only", "est_resources":
  dict|None}`. `states` MAY be the estimated automaton state count or `None`.
  Additional keys MAY be present. This is PYRO-specific and MUST NOT exist on the
  standard `re` namespace when interposing (§7.2).
- **R32 (thread safety).** All module-level and `Pattern` methods MUST be safe
  to call concurrently from multiple Python threads. Concurrent searches MUST
  serialize correctly onto the device (or software model) without corrupting
  results (see R48).

### 7.2 Transparent interposition (L0)

- **R33 (import-alias mode).** `import pyro.re as re` MUST be sufficient to get
  acceleration with no other source change, satisfying R26–R30.
- **R34 (patch mode).** `pyro.install()` MUST patch the already-imported standard
  `re` module so that subsequent `re.search(...)` etc. route through PYRO;
  `pyro.uninstall()` MUST fully restore the original `re` behavior. After
  `install()`, `re.<fn>` for a fallback-only pattern MUST be indistinguishable
  from stock `re` (R3, R29).
- **R35 (opt-out / env control).** Environment variable `PYRO_DISABLE=1` MUST
  force all calls to fallback (hardware never touched), for A/B testing and
  incident mitigation. `PYRO_FORCE_MODEL=1` MUST route HW-eligible work to the
  software model instead of the device.
  - **R35a (cached sampling, not per-call reads).** To keep the per-call
    decision overhead within R3a/R5 (≤ 2 µs), PYRO MUST NOT read the process
    environment on the per-call hot path. Instead it MUST **sample**
    `PYRO_DISABLE` and `PYRO_FORCE_MODEL` from `os.environ` into cached internal
    flags at these **deterministic sampling points** only:
    1. first import of the `pyro` package (module initialization);
    2. every call to `pyro.install()` and `pyro.uninstall()`;
    3. every call to the explicit API `pyro.refresh_env()`.
    The per-call decision (R51 step 1) MUST consult only the cached flags.
  - **R35b (documented mid-process semantics).** A variable set **before process
    start** (case 1) or **before `pyro.install()`** (case 2) MUST behave exactly
    as if read per-call — i.e., identical observable behavior to the prior
    version for the common configure-then-run usage. Mutating `os.environ`
    mid-process takes effect only **after the next sampling point** (R35a.2/3).
    This deferred-refresh semantics is intentional and MUST be documented; it
    mirrors CPython precedents such as `re.purge()` and locale caching, where
    cached state is refreshed at explicit points rather than on every operation.
  - **R35c (incident-mitigation guarantee).** If `PYRO_DISABLE=1` is present in
    `os.environ` at **any** sampling point (R35a), PYRO MUST force all subsequent
    top-level calls to the fallback path until the flag is re-sampled with the
    variable absent/unset. Operators mitigating an incident set `PYRO_DISABLE=1`
    and then reach a sampling point — either by calling `pyro.refresh_env()` /
    `pyro.uninstall()` in-process, or by restarting the process — to take effect.
  - **R35d (`refresh_env` API).** `pyro.refresh_env() -> None` MUST re-sample
    both variables from the current `os.environ` into the cached flags and apply
    the new values to all subsequent calls. It MUST be thread-safe (R32) and
    idempotent, and MUST NOT alter any in-flight call's routing decision.
- **R36 (transparency invariant).** With PYRO installed, any program that passes
  its test suite against stock `re` and uses only documented `re` behavior MUST
  produce identical observable output (return values and raised exceptions).
  Differences in timing and in PYRO-private attributes are permitted; differences
  in results are defects. The invariant explicitly covers **asynchrony
  (v2.0.0)**: the observable result of any call MUST NOT depend on a pattern's
  circuit lifecycle tier (cold / synthesizing / warm / resident / permanently
  fallback, R4/R65) — a pattern served by fallback before its circuit is resident
  and by hardware afterward MUST return byte-identical results at every point;
  only latency and `pyro.re.stats()` counters may differ.
  - **R36a (isinstance carve-out — permanent limitation).** The transparency
    invariant explicitly **excludes** identity-based type checks against the
    concrete CPython types `re.Pattern` and `re.Match`. On the hardware/model
    path, `isinstance(p, re.Pattern)` and `isinstance(m, re.Match)` MAY be
    `False` because those types are non-subclassable, non-ABC C types (§2, R27,
    R28). This is a **documented, permanent limitation**, not a defect. It is
    **not** a fallback trigger: the wrapper cannot detect that a caller intends
    an `isinstance` check, so PYRO MUST NOT attempt to route around it. Programs
    that must preserve nominal type identity MUST use `PYRO_DISABLE=1` or the
    fallback path (on which the objects are genuine `re` objects, R29).
    Type checks that use duck typing, `hasattr`, or protocol/structural checks
    are unaffected and remain covered by R36.

### 7.3 Host-runtime C ABI (L3)

The host runtime is a C or Rust library exposing a stable C ABI consumed by the
Python bindings. All functions are `extern "C"`. Integer widths are fixed. The
ABI is versioned.

- **R37 (ABI version).** Symbol `uint32_t pyro_abi_version(void)` returns a
  packed `MAJOR<<16 | MINOR<<8 | PATCH`. This v2.0.0 spec defines ABI **2.0.0**
  (the circuit-oriented ABI below). A caller MUST refuse a library whose MAJOR
  differs from the ABI it expects. **ABI enum stability (normative):** from ABI
  2.0.0 onward, all ABI enums (`pyro_status`, `pyro_encoding`, `pyro_circ_status`,
  R38) are **additive-only** within a MAJOR — existing enumerator values and
  meanings are frozen; new cases MUST take new values and MUST NOT renumber or
  repurpose existing ones. Removing or renumbering a value is a MAJOR break.
  - *Version-history note (does not amend any AC).* The Phase-0 deliverable froze
    a shape-only **stub** header at ABI **1.0.0**; **AC-0-8** validated that stub
    on branch `phase0-pyro` and remains valid there unchanged. Phase 1 supersedes
    the stub with the ABI **2.0.0** defined here; a fresh integrated build reports
    `0x00020000`. AC-0-8's `0x00010000` assertion is a historical Phase-0
    checkpoint against the stub, not an invariant of the shipped system.
- **R38 (types).** The header `pyro_rt.h` (ABI 2.0.0) MUST define:

  ```c
  typedef struct pyro_ctx     pyro_ctx;     /* opaque runtime context      */
  typedef struct pyro_circuit pyro_circuit; /* opaque generated-circuit handle */

  typedef enum {
      PYRO_OK            = 0,
      PYRO_E_UNSUPPORTED = 1,  /* pattern not HW-eligible                    */
      PYRO_E_CAPACITY    = 2,  /* circuit exceeds PR-region resource budget  */
      PYRO_E_DEVICE      = 3,  /* transport/device error                     */
      PYRO_E_INVALID     = 4,  /* bad argument                               */
      PYRO_E_NOMEM       = 5,
      PYRO_E_TIMEOUT     = 6,
      PYRO_E_NOT_RESIDENT= 7,  /* circuit not resident; caller must fall back */
      PYRO_E_SYNTH       = 8   /* synthesis failed; pattern permanently FB    */
  } pyro_status;

  typedef enum {
      PYRO_ENC_BYTES = 0,
      PYRO_ENC_UTF8  = 1
  } pyro_encoding;

  /* circuit lifecycle tier (mirrors R4 / R31) */
  typedef enum {
      PYRO_CIRC_COLD        = 0,  /* no artifact; synthesis needed          */
      PYRO_CIRC_SYNTHESIZING= 1,  /* synthesis in flight (service)          */
      PYRO_CIRC_WARM        = 2,  /* artifact cached; PR-load needed         */
      PYRO_CIRC_RESIDENT    = 3,  /* loaded + identity-verified; dispatchable*/
      PYRO_CIRC_FALLBACK    = 4   /* permanently fallback-only (R65)         */
  } pyro_circ_status;

  /* one reported match window, all offsets in transport units (bytes) */
  typedef struct {
      uint64_t start;   /* inclusive, byte offset into transported buffer */
      uint64_t end;     /* exclusive */
      uint32_t pattern_id;
      uint32_t flags;   /* bit0: verified, bit1: zero_width */
  } pyro_match;
  ```

- **R39 (lifecycle).**
  ```c
  pyro_status pyro_ctx_open(pyro_ctx **out, const char *transport_uri);
  void        pyro_ctx_close(pyro_ctx *ctx);
  ```
  `transport_uri` selects the binding, e.g. `"model://"`, `"qdma://<bdf>/q0"`,
  or `"eth://<iface>"`. On failure returns non-`PYRO_OK` and `*out == NULL`.
- **R40 (generate / synthesize / load).** The former single-step "compile+load"
  becomes an explicit lifecycle across the cache tiers (R4):
  ```c
  /* Create a circuit handle from an already-eligible circuit DESCRIPTOR.
     Precondition (R40a): eligibility/classification is decided in the Python L2
     layer BEFORE this call; `pattern` carries the L2-produced descriptor
     (canonical identity + artifact locator), not raw regex requiring parsing.
     A binding that performs its own on-device resource estimation MAY return
     PYRO_E_UNSUPPORTED/PYRO_E_CAPACITY; device-free/model bindings trust the
     descriptor and do not (that decision stays in L2). */
  pyro_status pyro_generate(pyro_ctx *ctx, const uint8_t *pattern, size_t len,
                            uint32_t flags, pyro_encoding enc,
                            pyro_circuit **out);

  /* Enqueue asynchronous synthesis (cold -> warm). Non-blocking: returns
     immediately (PYRO_OK once enqueued). Idempotent per key. */
  pyro_status pyro_synth_request(pyro_ctx *ctx, pyro_circuit *c);

  /* Poll lifecycle tier without blocking. */
  pyro_status pyro_circuit_status(pyro_ctx *ctx, pyro_circuit *c,
                                  pyro_circ_status *out);

  /* Load a warm artifact into the PR region (warm -> resident), verifying
     identity (R47a). May evict the current resident circuit (R64). Blocks for
     ~O(100 ms) PR-load; callers invoke this off the request hot path. */
  pyro_status pyro_circuit_load(pyro_ctx *ctx, pyro_circuit *c);

  void        pyro_circuit_free(pyro_circuit *c);
  ```
  `flags` mirrors the Python flag bits (§7.1). A caller MUST NOT scan a circuit
  that is not `PYRO_CIRC_RESIDENT`; doing so returns `PYRO_E_NOT_RESIDENT` and
  the caller MUST fall back.
- **R40a (classification is an L2 responsibility; `pyro_generate` precondition).**
  Pattern **classification/eligibility** (the supported-subset gate and resource
  estimate, R8/R11/R12) is owned by the **Python L2 layer** and MUST be performed
  **before** entering the C ABI. The C `pyro_generate` is a **handle-creation**
  step: it trusts its input per the ABI contract and assumes the caller supplies
  an **already-eligible circuit descriptor** (the L2-produced canonical identity +
  artifact locator, per R40's comment), not raw regex text to be parsed in C.
  Consequently, in the **device-free / `model://` binding** `pyro_generate` does
  **not** run a classifier and does **not** return `PYRO_E_UNSUPPORTED`/
  `PYRO_E_CAPACITY`; those results arise only in the Python L2 path (surfaced to
  callers as fallback routing, R8/R51) or in a future binding that performs its
  own on-device estimation. This scoping does not weaken correctness: an
  ineligible pattern never reaches `pyro_generate` because L2 has already routed
  it to fallback.
- **R41 (scan).**
  ```c
  pyro_status pyro_scan(pyro_ctx *ctx, pyro_circuit *c,
                        const uint8_t *buf, size_t len,
                        uint64_t start_off,
                        pyro_match *out, size_t out_cap, size_t *out_count);
  ```
  Requires `c` resident (else `PYRO_E_NOT_RESIDENT`). Scans `buf[0..len)`;
  reports up to `out_cap` matches; sets `*out_count`. If more matches exist than
  `out_cap`, returns `PYRO_OK` with `*out_count == out_cap` and the runtime MUST
  support resumption via `start_off` (streaming). Every returned `pyro_match`
  with `flags` bit0 clear MUST be treated as **unverified** by the caller (Python
  layer re-verifies, R19).
- **R42 (capability query).**
  ```c
  typedef struct {
      uint32_t max_states, max_patterns, max_repeat;
      uint32_t alphabet;          /* 256 for byte datapath                  */
      uint32_t generator_version; /* L2 HDL generator version               */
      uint32_t harness_version;   /* §7.4 harness contract version          */
      uint32_t datapath_bytes;    /* bytes/cycle of the harness datapath     */
      /* PR-region resource budget one circuit must fit (R11) */
      uint32_t pr_luts, pr_ffs, pr_bram_kb, pr_dsps;
      uint32_t pr_partitions;     /* >=1; 1 => single-tenant region (R64)   */
  } pyro_caps;
  pyro_status pyro_caps_get(pyro_ctx *ctx, pyro_caps *out);
  ```
- **R43 (ownership & lifetime).** Buffers passed to `pyro_scan` are borrowed for
  the duration of the call only. `pyro_circuit` is owned by the caller until
  `pyro_circuit_free`. `pyro_ctx` must outlive all `pyro_circuit` created from
  it. The library MUST NOT retain pointers past the call that received them,
  except a loaded circuit's device-resident state (managed until eviction (R64),
  `pyro_circuit_free`, or ctx close). All functions MUST be thread-safe given
  distinct `pyro_ctx`; a single `pyro_ctx` MUST be internally synchronized
  (R32/R48). `pyro_synth_request` and `pyro_circuit_status` MUST be safe to call
  concurrently with `pyro_scan`.
- **R44 (no UB on error).** On any non-`PYRO_OK` return, all `out` pointers MUST
  be left in a defined state (`NULL` for handles, `*out_count == 0` for scan,
  status set to `PYRO_CIRC_FALLBACK` or the last known tier). `PYRO_E_NOT_RESIDENT`
  and `PYRO_E_SYNTH` are **not** device errors for R52 purposes: they are normal
  fallback routing (R51/R65), not `fallback_after_error`.

### 7.4 Hardware/software interface: harness contract (L4↔L5)

This is the **fixed harness contract** that **every generated circuit** (§2)
MUST implement, mapped into the OpenNIC user-plugin address space. It is fixed
across patterns so the host runtime drives any circuit uniformly. It MUST be
honored identically by the software model (R7) so that the model and a real
generated circuit are interchangeable. The pre-2.0.0 `PROG_*` program-blob
registers and the R46 blob format are **obsolete and removed**: circuits are no
longer configured by a data blob; the pattern is baked into the circuit at
synthesis time and identified via the identity block (R47a).

- **R45 (harness register map).** Every generated circuit exposes a control/
  status register (CSR) block at base offset `USER_BAR_BASE` within the user
  plugin's AXI-Lite window. All registers are 32-bit, little-endian. The
  following offsets are **normative** and **identical for all circuits**:

  | Offset  | Name          | Access | Meaning                                   |
  |---------|---------------|--------|-------------------------------------------|
  | 0x0000  | `ID`          | RO     | magic `0x5059524F` ("PYRO")               |
  | 0x0004  | `HARNESS_VER` | RO     | harness contract version (packed)         |
  | 0x0008  | `CAPS0`       | RO     | `datapath_bytes`(low16) `pr_partitions`(hi16) |
  | 0x000C  | `CAPS1`       | RO     | `generator_version` (packed)              |
  | 0x0010  | `CTRL`        | RW     | bit0 START, bit1 RESET, bit2 STREAM       |
  | 0x0014  | `STATUS`      | RO     | bit0 BUSY, bit1 DONE, bit2 ERR, bit3 OVF  |
  | 0x0018  | `CIRC_ID0`    | RO     | pattern-hash word 0 (identity, R47a)      |
  | 0x001C  | `CIRC_ID1`    | RO     | pattern-hash word 1                       |
  | 0x0020  | `CIRC_ID2`    | RO     | pattern-hash word 2                       |
  | 0x0024  | `CIRC_ID3`    | RO     | pattern-hash word 3                       |
  | 0x0028  | `CIRC_FLAGS`  | RO     | baked pattern flags (§7.1) + `NUM_PAT`hi16|
  | 0x002C  | `RESERVED`    | RO     | reads 0                                   |
  | 0x0030  | `IN_ADDR`     | RW     | DMA addr (low32) of input buffer          |
  | 0x0034  | `IN_ADDR_H`   | RW     | DMA addr (high32)                         |
  | 0x0038  | `IN_LEN`      | RW     | input length in bytes                     |
  | 0x0040  | `OUT_ADDR`    | RW     | DMA addr (low32) of result ring           |
  | 0x0044  | `OUT_ADDR_H`  | RW     | DMA addr (high32)                         |
  | 0x0048  | `OUT_CAP`     | RW     | result ring capacity in entries           |
  | 0x004C  | `OUT_COUNT`   | RO     | number of results produced                |
  | 0x0050  | `IRQ_ENABLE`  | RW     | bit0 DONE-irq enable                      |
  | 0x0054  | `IRQ_STATUS`  | RW1C   | bit0 DONE, write-1-to-clear               |
  | 0x0058  | `CYCLES_LO`   | RO     | R45a: BUSY core-clock cycles, low 32      |
  | 0x005C  | `CYCLES_HI`   | RO     | R45a: BUSY core-clock cycles, high 32     |
  | 0x0060  | `BYTES_LO`    | RO     | R45a: input bytes consumed, low 32        |
  | 0x0064  | `BYTES_HI`    | RO     | R45a: input bytes consumed, high 32       |

- **R45a (on-chip performance counters — v2.3.0).** Every generated circuit
  MUST implement two 64-bit read-only counters in the CSR block:
  - `CYCLES` (`0x0058`/`0x005C`) — increments once per core-clock cycle while
    the engine is BUSY (from the cycle after `CTRL.START` is accepted until the
    final input byte is consumed);
  - `BYTES` (`0x0060`/`0x0064`) — the input bytes consumed so far
    (`datapath_bytes` per accepted beat; the final beat may be short).

  Both counters are cleared by `CTRL.START` and by `CTRL.RESET`, and hold their
  values after `STATUS.DONE` until the next START/RESET. The 32-bit halves have
  no latch-on-read-low coherence: while BUSY a 64-bit read MAY tear across the
  halves, so hosts MUST sample the counters only after `STATUS.DONE` where
  exact values are required. **Purpose:** these are the benchmark suite's
  (R59/AC-3-3) attribution seam — `CYCLES × t_clk` is the pure on-chip scan
  cost, so end-to-end host time minus it isolates PCIe/DMA/framing/dispatch
  overhead, and `BYTES / CYCLES` is the achieved datapath utilization. The
  software model (R7) MUST expose the same registers but reports the
  **idealized** values (`BYTES` = bytes consumed, `CYCLES` =
  `ceil(BYTES / datapath_bytes)`); only hardware-measured values are
  performance evidence for R1/R59. Circuits built against harness < 2.1.0 read
  0 at these offsets (the prior default decode): a `CYCLES` of 0 after a
  completed non-empty scan therefore identifies a counter-less circuit, and
  hosts MUST treat its counter values as unavailable, never as measurements.
  This addition bumps `HARNESS_VERSION`/`GENERATOR_VERSION` to `0x00020100`
  (2.1.0): identity hashes (R47a) and cache keys (R4) roll over, so pre-2.1.0
  cached artifacts are stale (rejected by the R47b harness check) and
  re-synthesize on demand. On hardware, where AXI-Lite is tied off at the R80
  boundary, the counters are read out **in-band** via
  `PERF_REQUEST`/`PERF_REPLY` (R78.11, v2.4.0).

- **R47a (circuit-identity register block — trust boundary).** Before dispatching
  any scan, the host runtime MUST read the identity block (`CIRC_ID0..3`,
  `CIRC_FLAGS`) and verify that the resident circuit's baked **pattern hash** and
  flags **exactly match** the pattern it is about to dispatch. The pattern hash
  is a cryptographic hash, ≥ 128-bit, over the following **normative** inputs, in
  this exact set:
  - `pattern_bytes` — the pattern source (bytes, or the `str` pattern's UTF-8
    encoding);
  - **encoding tag** — a 1-byte discriminator distinguishing `PYRO_ENC_BYTES`
    from `PYRO_ENC_UTF8` (§7.3 R38). This input is **required** because the same
    pattern text generates a **different automaton/circuit** in bytes mode vs.
    str/UTF-8 mode (R14); omitting it would let a bytes-mode circuit be
    mis-identified as satisfying a str-mode call, or vice versa;
  - **effective (canonicalized) flags** — the flags **after** inline-flag
    extraction and canonicalization (see below), **not** the caller's raw `flags`
    argument;
  - `generator_version`;
  - `harness_version`.

  **Flag canonicalization (normative).** The host MUST canonicalize flags before
  computing the identity hash and before any cache lookup (R4): inline flags
  (e.g. `(?i)`, `(?ms)`, and scoped `(?i:...)` where applicable) MUST be folded
  into the effective flag set exactly as CPython `re` would apply them, `re.U`
  MUST be normalized to its no-op form for `str`, and the result MUST be a stable
  canonical integer. Two patterns that are semantically identical after inline-
  flag extraction MUST produce the **same** effective flags and therefore the
  **same** identity hash. The `CIRC_FLAGS` register holds these effective flags.
  If they do not match (wrong circuit resident, or PR load not
  complete), the host MUST NOT dispatch and MUST fall back (`PYRO_E_NOT_RESIDENT`,
  R51). This is a **trust boundary**: a circuit proves *which* pattern it
  implements via its baked identity, but the host **still re-verifies every
  reported window** with CPython/model (R19). The identity check guards against
  dispatching to the wrong circuit; the window re-verification guards against a
  correct-identity circuit producing a false positive. A `pyro_match` `flags`
  bit0 (`verified`) set by a circuit is advisory only and does NOT relieve the
  host of R19 re-verification.
- **R47b (PR bitstream artifact + manifest contract).** The synthesis service
  (R63) produces, for each key `(pattern_bytes, encoding, effective_flags,
  generator_version, toolchain_version, shell/PR-region_version)` (encoding and
  effective_flags per R47a), a **PR bitstream artifact** accompanied by a
  **manifest** (a self-describing sidecar, e.g. JSON + a binary bitstream). The
  manifest MUST contain at least:
  - the **pattern hash** (same value baked into `CIRC_ID*`, R47a);
  - the encoding tag, the effective (canonicalized) pattern flags, and
    `generator_version`, `toolchain_version`,
    `harness_version`, and target **shell / PR-region identifier** (the artifact
    is only loadable into a compatible region);
  - **resource utilization** (LUTs, FFs, BRAM, DSP) and achieved timing (Fmax /
    met-timing boolean) from P&R;
  - the **over-approximation classes** the circuit uses and, where feasible, an
    estimated false-positive rate (R19c; empty set for an exact circuit);
  - a **CRC-32/hash of the bitstream payload** for integrity;
  - **`pr_verified: bool`** (added additively, v2.2.2; **default `false`**, JSON
    round-trip preserved exactly like `payload_kind`, R72a): `true` **iff** Vivado
    `pr_verify` ran and passed for this artifact (R82c). It MUST be `false`/absent for
    `mock_stub` and `ooc_metrics` payloads, and MUST be `true` whenever
    `payload_kind == "pr_bitstream"` (the two are consistent by R82c). This is the
    **independent, observable seam** for `pr_verify` success (closing the AC-2b-3
    observability gap: tests read `pr_verified` directly rather than inferring it from
    `payload_kind`).
  - **R47b-consistency (defense-in-depth — normative, v2.2.3).** The consistency
    invariant `pr_verified == (payload_kind == "pr_bitstream")` (R82c) is enforced
    **at the boundary, not only by the producer**: the `pyro.synth.Manifest`
    constructor **and** `Manifest.from_json(...)` MUST **reject** an inconsistent
    manifest by raising a defined exception (a `ValueError`/`pyro.synth` manifest
    error), rather than silently accepting it. This is back-compat safe: every
    pre-v2.2.2 on-disk manifest is `mock_stub`/`ooc_metrics` with `pr_verified`
    absent→`false`, which satisfies the invariant. Chosen over a producer-only
    obligation so a corrupt or hand-edited manifest cannot enter the system; consumers
    additionally remain entitled to reject on load (R47b/R82c).
  Before a PR load (R40 `pyro_circuit_load`), the host MUST verify the manifest's
  shell/PR-region identifier matches the live device, the bitstream integrity
  hash, and the R47a identity; on any of these failing it MUST refuse the load
  and treat the pattern as fallback (not a device error). The obsolete R46
  32-byte `"PROG"` blob header is removed.
  - **R47c (refusal status — normative).** A refused `pyro_circuit_load`
    (shell/PR-region incompatibility, integrity-hash failure, or R47a identity
    mismatch) MUST return **`PYRO_E_NOT_RESIDENT`**: the load did not happen and
    the circuit is not resident, so the caller falls back exactly as for any
    not-resident tier (R40/R51). This is deliberately distinct from
    `PYRO_E_SYNTH` (which means synthesis failed → *permanent* fallback, R65): a
    refusal is transient with respect to a corrected/compatible artifact and does
    **not** mark the pattern permanently fallback-only. Neither is a device error
    for R52 purposes; both are routing (R44). **ABI stability:** the `pyro_status`
    enum (R38) is **additive-only** from ABI 2.0.0 onward — existing values and
    meanings are frozen; a future need for a finer refusal code MUST add a new
    enumerator (never renumber or repurpose `PYRO_E_NOT_RESIDENT`/`PYRO_E_SYNTH`).
- **R47 (result ring entry).** Each result entry is 24 bytes, little-endian:
  `start(8) end(8) pattern_id(4) flags(4)` — matching `pyro_match` (R38). The
  circuit writes entries densely from ring base; on overflow (`OUT_COUNT ==
  OUT_CAP` with more matches pending) it MUST set `STATUS.OVF` and the host MUST
  resume via streaming (R41). `pattern_id` selects among the patterns baked into
  a multi-pattern circuit (0 for a single-pattern circuit).
- **R48 (operation sequence).** A single scan against a **resident, identity-
  verified** circuit (R47a) is: (1) verify identity block matches the target
  pattern, (2) write `IN_*`, `OUT_*`, `OUT_CAP`, (3) write `CTRL.START`, (4) wait
  `STATUS.DONE` (poll or IRQ), (5) read `OUT_COUNT`, DMA the result ring back.
  There is no per-scan program load. The host runtime MUST enforce that only one
  scan is in flight against the (single-tenant) PR region at a time (single-
  issue); concurrency is serialized in L3 (R43, R64). Loading a different
  pattern's circuit is a PR reconfiguration (R40 `pyro_circuit_load`), not part
  of the scan sequence.
- **R49 (endianness/alignment).** All DMA buffers MUST be 64-byte aligned. All
  multi-byte fields are little-endian. The input buffer needs no alignment beyond
  64-byte start. The model/runtime MUST **check** these to catch host bugs.
  - **R49a (check realized as a defined error).** "Check" (formerly "assert") is
    satisfied — and preferably realized — by returning the defined error
    `PYRO_E_INVALID` with the R44 state guarantees (`*out_count == 0`, `out`
    handles `NULL`), rather than `abort()`/`assert()` that would kill the process.
    A defined-error rejection is the production-safe form and is what the
    conformance suite MUST observe; a debug build MAY additionally `assert`, but
    the **normative** contract is the `PYRO_E_INVALID` return. The check MUST
    reject a misaligned or otherwise malformed DMA request before any transfer.
- **R50 (transport-agnostic contract).** The register/DMA semantics above are
  identical regardless of whether L4 reaches the circuit over the QDMA char-dev
  binding or the raw-Ethernet binding; only the mechanism of MMIO/DMA differs.
  In the Ethernet binding, CSR writes and buffer transfers are encapsulated in a
  defined control-frame format (**specified in §10.1, R78**; the R76 deferral is
  lifted as of v2.2.0) but the register meanings are unchanged. PR reconfiguration
  (loading a bitstream artifact) uses the platform PR mechanism; in Phase 2b this
  is **JTAG via `hw_server`** (R85), out of band from the scan datapath, with
  ICAP/MCAP self-reconfiguration deferred (R85).

### 7.5 Synthesis service, prewarm, and diagnostics

- **R62 (`prewarm` API).** `pyro.prewarm(patterns, flags=0) -> None` MUST accept
  a single pattern or an iterable of patterns and, for each HW-eligible one (R8),
  enqueue synthesis (R4a/R63) regardless of call count. It MUST return promptly
  (non-blocking); it MUST NOT wait for synthesis to complete and MUST NOT raise
  for a pattern that is fallback-only or whose synthesis later fails (R65).
  `pyro.prewarm` is PYRO-specific and MUST NOT appear on the standard `re`
  namespace when interposing (§7.2). Fallback-only patterns passed to `prewarm`
  are silently ignored (optionally surfaced via stats).
- **R63 (synthesis service).** Synthesis MUST run in a **separate process** (or
  processes) from the caller, so that a minutes-long Vivado run never blocks or
  slows the Python application. The service:
  - **R63a (job queue).** Accepts synthesis jobs keyed by the R4 bitstream-cache
    key, deduplicates by key (a job already queued/running/cached for a key is
    not re-run), and processes them from a queue. Job admission is governed by
    the launch policy (R4a).
  - **R63b (toolchain).** Runs the real flow (regex-derived RTL → Vivado synth +
    P&R → PR bitstream + manifest, R47b) when the toolchain is present (P1);
    otherwise, and in all tests, runs a **mock toolchain** that emits a **stub
    artifact + manifest** for the software model (R7), so every phase is testable
    without Vivado.
  - **R63c (concurrency vs. PR region).** The service MAY synthesize multiple
    circuits concurrently (subject to host resources / Vivado licenses), but the
    **PR region is single-tenant**: at most **one** circuit is resident at a time
    unless `pr_partitions > 1` is advertised (R42/R64). Loading is serialized.
  - **R63d (cache population).** On success the service writes the artifact +
    manifest to the persistent bitstream cache (R4) so subsequent runs (including
    after process restart) find it **warm**.
  - **R63e (isolation).** A crash, hang, or non-termination of a synthesis job
    MUST NOT affect caller correctness or availability; the caller continues on
    fallback. The service MUST enforce a per-job timeout after which the job is
    abandoned and the pattern treated per R65. The service MUST **clean up its
    worker processes** on `pyro_ctx_close`/ctx teardown and at interpreter
    shutdown, so that a test harness can assert no residual service processes
    survive a completed run.
- **R64 (PR-region arbitration / eviction).** With a single-tenant PR region,
  the runtime MUST implement an **eviction policy** deciding which resident
  circuit to replace when a different pattern's circuit is chosen for residency.
  The policy MUST be deterministic given the access history (default: evict the
  **least-recently-dispatched** resident circuit) and MUST guarantee progress
  (no thrashing loop). A load that would evict a circuit currently mid-scan MUST
  wait for or serialize after that scan (R48/R43). Eviction is not a device error
  and MUST NOT affect results (evicted patterns simply revert to fallback until
  reloaded).
  - **R64a (device-free residency bookkeeping is REQUIRED, not vacuous).** On a
    device-free host, R51b lets the software model serve any tier, but the
    single-tenant **residency and eviction bookkeeping MUST still be exercised**:
    the residency manager MUST track the resident-circuit set (gauge
    `circuits_resident`, R66), promote a chosen circuit to residency, and, on
    promoting a second pattern's circuit against the single-tenant budget
    (`pr_partitions == 1`), **fire the deterministic LRU eviction** (incrementing
    `circuits_evicted`, R66) exactly as it would on hardware. This bookkeeping is a
    real state machine over the model, not a no-op, so the model-side clauses of
    **AC-1-5 and AC-2-6** are **firm, non-vacuous LIVE assertions**. Results remain
    byte-identical across evict/reload cycles (R53). Only the physical PR-load
    mechanism and its timing are absent (and SKIP, R71); the arbitration/eviction
    *logic* is fully live on the model.
- **R65 (synthesis-failure semantics).** If synthesis fails — RTL does not fit
  the PR-region budget, fails timing, the toolchain errors, or the per-job
  timeout (R63e) fires — the pattern MUST become **permanently fallback-only**
  for the current `(generator_version, toolchain_version, shell_version)`: the
  failure MUST be recorded (bitstream cache negative entry + diagnostic /
  `pyro.re.stats()`), MUST NOT be retried indefinitely, and MUST **never** raise
  an exception to the caller. The caller continues to receive byte-identical
  results via fallback (R16, R52 distinction: this is routing, not
  `fallback_after_error`). The failure transition MUST be drivable from tests via
  the public seam `pyro.testing.inject_synth_failure` (R67).
- **R66 (`stats` extensions).** `pyro.re.stats() -> dict` MUST report, in
  addition to the pre-2.0.0 dispatch counters (hardware / model / fallback /
  fallback-after-error, R52), the circuit-lifecycle counters:
  `synth_launched`, `synth_succeeded`, `synth_failed`, `circuits_synthesizing`,
  `circuits_resident`, `circuits_evicted`, and `pr_loads`. Counters MUST be
  monotonic (except gauges `circuits_synthesizing`/`circuits_resident`) and
  thread-safe (R32). Stats MUST be observable without perturbing routing.

### 7.6 Real-toolchain (Vivado) adapter contract (Phase 2)

This subsection is the **normative contract for the real synthesis flow** that the
service (R63) selects, at the `MockToolchain` seam, in place of the mock toolchain
(R63b) when the operator opts in. It exists because Phase 2 begins on a host where
**Vivado is present but the OpenNIC partial-reconfiguration (PR) build flow and an
operable device are not** (§11 P1 is only partially satisfied; see R71). The
rulings below make the Phase-2 ACs executable under exactly that reality: real
out-of-context (OOC) synthesis + place-and-route is performed for the physical
board's part, honest post-route metrics are recorded, and every clause that would
require an absent prerequisite records a **SKIP** (never a PASS). The mock
toolchain remains the default so Phase-0/Phase-1 behavior is byte-identical when no
knob is set.

- **R70 (toolchain-selection knobs).** Toolchain selection is governed by two
  **spec-named** environment knobs, sampled at the R35a sampling points (import,
  `install()`/`uninstall()`, `refresh_env()`) and NEVER on the per-call hot path
  (R5/R35a); they are also registered in R68:
  - `PYRO_TOOLCHAIN` — one of `mock` | `vivado`. **Default `mock`.** `mock`
    selects `MockToolchain` (R63b) — the Phase-0/1 behavior, unchanged. `vivado`
    selects the real OOC adapter (this subsection). Any unrecognized value is
    **invalid** and MUST be treated as `mock` (fail safe: never silently attempt a
    real flow the operator did not name), optionally surfaced via stats/diagnostics.
  - `PYRO_VIVADO` — the Vivado **install directory** (e.g. `/usr/local/cad/2025.2/
    Vivado`, the R70a-pinned install). **No default and NO scanning of the filesystem, `PATH`, or
    `XILINX_VIVADO` by library code**: if `PYRO_TOOLCHAIN=vivado` and `PYRO_VIVADO`
    is unset or does not resolve to a working Vivado, the adapter is **unavailable**
    (`toolchain_present == false`, R71) and the affected AC clauses SKIP. Requiring
    an explicit install dir keeps toolchain selection deterministic and auditable
    and prevents a stray Vivado on `PATH` from perturbing results.
  - **R70a (config crosses the process boundary).** The sampled selection MUST be
    carried into the out-of-process worker (R63) via the existing
    `ToolchainConfig` (a frozen, picklable dataclass). `ToolchainConfig` is
    extended **additively** with at least: a toolchain **kind** (`mock`|`vivado`),
    the resolved **vivado install directory**, the target **part**
    (`xcu250-figd2104-2L-e`, the physical U250's part — R71), the target
    **clock** in MHz (R73), the two named **job timeouts** `job_timeout_s` (1800) and
    `pr_job_timeout_s` (3600) (R77/R84), the **`pr_bitstream: bool`** job-request flag
    (default `False`, R88), and the PR substrate paths **`static_dcp`**/**`reference_dcp`**
    (default `None`, R82d/R88). All new fields carry
    defaults that reproduce the mock behavior, so a `ToolchainConfig()` with no
    overrides is byte-identical to the pre-2.1.0 default. The worker constructs the
    named toolchain from this config; the service/queue/dedup/isolation machinery
    (R63a–R63e) is unchanged.
  - **R70b (toolchain selection is pinned at manager construction).** Although the
    R35a snapshot of `PYRO_TOOLCHAIN`/`PYRO_VIVADO` is refreshed at every sampling
    point (import, `install()`/`uninstall()`, `refresh_env()`), a residency/
    synthesis manager **pins its `ToolchainConfig` at construction**, so a
    re-sampled toolchain change takes effect only in a manager built **after** the
    sampling point (a fresh process, or an explicit manager reset) — not by live
    re-push into an already-running manager. Rationale (consistent with R35b's
    deferred-refresh discipline): unlike a per-dispatch scalar such as
    `PYRO_N_SYNTH` (R68), the toolchain governs a **spawned subprocess**, and
    tearing down a manager mid-flight to swap it would SIGTERM an in-flight
    real-Vivado worker and orphan its process tree, bypassing the R77 in-worker
    kill — strictly worse than deferred pickup. This deferral is intentional and
    MUST be documented (R35b).
  - **R70a-pin (pinned Vivado release — normative, v2.2.1).** The pinned `vivado`
    toolchain is **Vivado 2025.2** installed at **`/usr/local/cad/2025.2/Vivado`**.
    This **supersedes the v2.1.0 pin of 2023.1**, which segfaults at batch-process
    exit on this host's upgraded OS (Ubuntu 24.04 / glibc 2.39, unsupported by
    2023.1): the nonzero exit codes of successfully-completed `launch_runs` children
    mark completed runs FAILED and violate the exit-code-integrity assumption
    underlying R77's process discipline. 2025.2 exits cleanly with no shim,
    officially supports the host OS, and is license-clean for
    `xcu250-figd2104-2L-e` (the permanent `cmac_usplus` license, valid through
    2027.06, covers it). The pinned `toolchain_version` is `0x19020000` (R75). Any
    other Vivado on `PYRO_VIVADO` MAY be used at the operator's risk but is not the
    pinned/validated release; the same-release rule (R82a) binds the static shell
    and all partials to whatever release built the locked static DCP.
  - **R70a-pin.2 (`PINNED_VIVADO_DIR` constant vs. `ToolchainConfig.vivado_dir`
    default — normative, v2.2.2).** The pin above is a **documented module constant**
    `PINNED_VIVADO_DIR == "/usr/local/cad/2025.2/Vivado"`, **not** a default value of
    `ToolchainConfig.vivado_dir`. `ToolchainConfig.vivado_dir` **MUST keep its
    default `None`**: R70 forbids library code from resolving `PYRO_VIVADO` by
    scanning the filesystem/`PATH`, and R70a requires a `ToolchainConfig()` with no
    overrides to be byte-identical to the **mock** toolchain (a non-`None` vivado_dir
    default would silently name a real install). The `vivado` synthesis path
    therefore still requires an explicit `PYRO_VIVADO` (R70); `PINNED_VIVADO_DIR` is
    the **documented resolution source for the JTAG loader** (`load_partial`,
    R86.5) — i.e. where the pinned `hw_server`/`vivado` binaries are found when the
    device config does not override it. This confirms the coder's reading: the pin is
    a constant, not a config default. R70/R70a are unchanged in substance.

- **R71 (partial-P1 live/SKIP matrix — normative).** Phase 2's prerequisite P1 is
  only **partially** satisfied on this host, so the Phase-2 ACs (AC-2-1..AC-2-6)
  are evaluated against three **availability predicates**, each decided only by
  **probing the environment** (never assumed):
  - `toolchain_present` — `PYRO_TOOLCHAIN=vivado` AND `PYRO_VIVADO` resolves to a
    Vivado that synthesizes + places + routes the target part (R70). *Established
    true for **Vivado 2025.2** at `/usr/local/cad/2025.2/Vivado` (the R70a pin;
    v2.2.1), part `xcu250-figd2104-2L-e`, no license error. Vivado 2023.1 was the
    original v2.1.0 pin but segfaults at batch-process exit on this host's upgraded
    OS (Ubuntu 24.04 / glibc 2.39), corrupting exit-code integrity — see R70a and
    the v2.2.1 changelog.*
  - `pr_flow_present` — an OpenNIC PR-partition floorplan (`pblock` + fixed
    static/reconfigurable interface, §7.4) AND a PR-bitstream generation flow exist
    that emit a **genuine loadable partial bitstream** (`payload_kind ==
    "pr_bitstream"`, R72). *Established **false** (no such floorplan or flow
    exists).*
  - `device_usable` — a PYRO-controllable OpenNIC device with PR-load rights
    (ICAP/PCAP/JTAG or `/dev/qdma*`) that PYRO is permitted to reconfigure.
    *Established **false** for purely technical reasons on this host: (a) there is
    **no loadable PYRO PR artifact** — the currently-flashed shell is not PR-capable
    and no PR floorplan/flow exists (`pr_flow_present`, a separate predicate, is
    false); (b) there is **no PYRO-usable transport** for the runtime user — no
    `/dev/qdma*` char devices, and the raw-Ethernet binding needs `CAP_NET_RAW`,
    which the user lacks; (c) the one-time **full-image reprogram** required to move
    to a PR-enabled shell disturbs the PCIe link and needs root cooperation for
    driver unbind / PCIe rescan (or a reboot). The physical U250 is the **owner's
    own board**; ownership, permission-to-perturb, and JTAG access are all
    **non-blockers** — JTAG programmability was verified empirically on 2026-07-06
    (FT4232H USB-JTAG bridge attached, `hw_server` connects, chain enumerates
    `xcu250_0`). Device bring-up (Phase-2b) becomes an available path once a
    PR-enabled shell and PYRO transport exist.*

  **Predicate-flip semantics (v2.2.0).** The exact conditions under which
  `pr_flow_present` and `device_usable` flip **true**, and the canonical SKIP-reason
  string each emits while false, are defined normatively in **R83** (§10.2). The
  literal SKIP strings below record the v2.1.3 disposition; from v2.2.0 the probe
  emits the R83 canonical enumeration of whichever conditions are actually unmet.

  **SKIP discipline (normative).** A clause that requires an **absent** predicate
  MUST record a **SKIP whose reason names the missing prerequisite** (e.g.
  `SKIP: pr_flow_present=false — no OpenNIC PR partition/bitstream flow`, or the
  R83 canonical `device_usable=false — …` enumeration). A SKIP
  MUST NEVER be recorded as PASS. A **PASS MUST come only from real execution** of
  the clause with its predicate satisfied. The following matrix binds each AC-2-*
  clause (see also the per-AC amendments in §10):

  | AC clause | Requires | Disposition on this host |
  |-----------|----------|--------------------------|
  | AC-2-1 real OOC synth+P&R, honest manifest metrics fit budget + met timing, cache warm-reload | `toolchain_present` | **LIVE** |
  | AC-2-1 loadable **PR bitstream** produced | `pr_flow_present` | **SKIP** (pr_flow absent) |
  | AC-2-2 on-device harness/identity/scan over ≥1 MiB | `device_usable` ∧ `pr_flow_present` | **SKIP** (device + pr_flow absent) — model-side correctness is AC-1-3 |
  | AC-2-3 cold→warm real synth is minutes, async, never caller-blocking | `toolchain_present` | **LIVE** |
  | AC-2-3 warm→resident PR-load timing + resident dispatch **on device** | `device_usable` | **SKIP** (device absent); async/non-blocking still asserted on model |
  | AC-2-4 estimator-vs-real P&R within the R74 margin; estimate-pass→synth-fail→permanent fallback on the real path | `toolchain_present` | **LIVE** |
  | AC-2-5 resident-circuit throughput **on hardware** (R1) | `device_usable` | **SKIP** (device absent); routing R3–R5/R3b asserted on model |
  | AC-2-6 single-tenant PR arbitration **on hardware** | `device_usable` ∧ `pr_flow_present` | **SKIP** (device + pr_flow absent); eviction policy asserted on model |

  When `toolchain_present == false` (the default, mock-only configuration), **all**
  real-toolchain clauses (AC-2-1 LIVE row, AC-2-3 LIVE row, AC-2-4) additionally
  SKIP with reason `toolchain_present=false`; the model-side clauses of AC-2-3/2-5/
  2-6 still run. A single shared availability probe SHOULD expose these three
  predicates so every gated assertion cites the same source of truth.

- **R72 (real-toolchain artifact payload honesty).** Because no PR bitstream can be
  produced (`pr_flow_present == false`, R71), the **vivado** toolchain's artifact
  payload remains the **`PYROART1` container** (`pyro.synth.artifact`, FORMAT_VERSION
  1) that the software model harness executes — consistent with the device-free
  model-dispatch ruling (R51b). It is **not** a device bitstream, and the manifest
  MUST say so plainly, so nothing downstream can make a false hardware claim.
  - **R72a (least-invasive honest mechanism: a manifest `payload_kind` field).**
    The honest indication is a new **manifest** field `payload_kind` (a string),
    added **additively** to the R47b `Manifest` dataclass with a **default** so the
    JSON round-trip is preserved (an on-disk manifest lacking the key deserializes
    to the default; a real Vivado flow could later populate it without changing the
    host-side contract). The **C-side `PYROART1` artifact header is ABI-frozen and
    MUST NOT change** (this is a JSON-sidecar field only); ABI 2.0.0 and artifact
    FORMAT_VERSION 1 are untouched. Normative values:
    - `"mock_stub"` — mock toolchain (R63b); metrics are **configured, not
      measured**; not a device bitstream. **This is the default value**, so every
      pre-2.1.0 manifest (all mock) deserializes to exactly what it is.
    - `"ooc_metrics"` — real Vivado OOC synth+P&R; the manifest's `luts`, `ffs`,
      `fmax_mhz`, and `met_timing` are **genuine post-route values** from the real
      reports (R73), but the payload is the model-exec `PYROART1` container, **NOT**
      a loadable device bitstream.
    - `"pr_bitstream"` — a genuine loadable partial-reconfiguration bitstream.
      **Reserved; not produced until `pr_flow_present` becomes true.**
  - **R72b (loader honesty).** The PR loader (`pyro_circuit_load`, R40) MUST treat
    an artifact as an on-device-loadable bitstream **only if** its manifest
    `payload_kind == "pr_bitstream"`; any other value means "no device bitstream
    exists," so on hardware the pattern is served by the model/fallback path and the
    on-device residency clauses SKIP (R71). On a device-free/model host this is moot:
    the model executes the `PYROART1` container regardless of `payload_kind` (R51b),
    and tier/residency bookkeeping is unchanged.
  - **R72c (metric honesty).** A `"ooc_metrics"` manifest MUST record the genuine
    post-route `luts`, `ffs`, `fmax_mhz`, and `met_timing` parsed from the real
    Vivado utilization/timing reports; it MUST NOT copy the estimator's numbers and
    MUST NOT claim `payload_kind == "pr_bitstream"`. Fabricating or estimating these
    fields on the `vivado` path is a defect.

- **R73 (AC-2-1 "meets timing" — the Phase-2 proxy).** The Phase-2 proxy for
  meeting the OpenNIC 250 MHz user-box clock (F4) is a **250 MHz (4.000 ns) clock
  constraint applied to the OOC-synthesized `pyro_circuit`**. The `vivado` adapter
  MUST constrain the circuit clock to 250 MHz, run place-and-route, and set the
  manifest fields from `report_timing_summary`:
  - `met_timing := (post-route worst negative slack WNS ≥ 0 ns)` at the 250 MHz
    constraint;
  - `fmax_mhz := 1000 / (4.000 − WNS_ns)` (the achieved Fmax implied by the WNS at
    the 4 ns target).
  A circuit with `met_timing == false` fails synthesis for AC-2-1/R12 purposes and
  the pattern becomes **permanently fallback-only** (R65). This proxy is a stand-in
  for a real in-shell timing closure and is superseded when `pr_flow_present`
  becomes true (the real static+dynamic timing then governs).

- **R73a (scope of the timing gate for a PR link — normative, v2.5.0).** R73 says
  post-route timing "must be met at the 250 MHz target" without saying **over which
  paths**. For an **out-of-context (OOC) `ooc_metrics` job** the question does not
  arise: the only design in the tool is `pyro_circuit`, so every path is the
  circuit's and R73 is unambiguous. For a **`pr_bitstream` in-context link job**
  (R82/R88) the tool holds the **whole device** — the locked static shell plus the
  reconfigurable module — and an unscoped worst-path query returns the worst path
  **in the entire design**, including paths PYRO does not author, cannot influence,
  and is not permitted to change. This clause scopes the gate.
  - **R73a.1 (the gate is scoped to the reconfigurable module).** For a
    `pr_bitstream` job, the post-route WNS that decides `met_timing` (R73) SHALL be
    taken over exactly the paths of the **reconfigurable module** — those whose
    startpoint **and/or** endpoint lies within the `pyro_rp` cell
    (`HD.RECONFIGURABLE == 1`), evaluated in the **`axis_aclk` / 250 MHz user-box
    clock domain** (F4). **Boundary paths are IN scope**: a path from a static-side
    register into `pyro_rp`, or from `pyro_rp` out to a static-side register,
    crosses the R80 interface and **is changed by the partial**, so it MUST be
    gated. Paths lying **entirely** in the static region — neither endpoint in
    `pyro_rp` — are **OUT of scope** and MUST NOT decide `met_timing` for a
    partial: the partial did not create them and cannot repair them.
  - **R73a.2 (`fmax_mhz` follows the scoped WNS).** `fmax_mhz` for a
    `pr_bitstream` manifest is computed by R73's formula from the **R73a.1 scoped**
    WNS. (This aligns the timing metric with the utilization metric, which is
    already scoped to the RM cell — `report_utilization -cells <rp_cell>` — so that
    a PR manifest describes the **pattern's** circuit throughout, as R74's
    estimator calibration requires.)
  - **R73a.3 (an empty scoped path set is a FAILURE, not a pass).** If the scoped
    query returns **no** setup paths, the job MUST fail (`SynthesisFailed` → R65),
    exactly as R73's unscoped "no paths" case does today. A narrower gate MUST NOT
    be allowed to degrade into no gate.
  - **R73a.4 (what protects the static side — the mitigation, normative).** The
    narrower gate deliberately stops testing static-only paths per partial. That is
    sound **only** because of the following, which together are the mitigation and
    MUST hold:
    1. the static shell is built and validated **once** (R82b), and its **whole-design
       post-route timing MUST be recorded with the flashed image at flash time** and
       kept with it as the static's timing record;
    2. a partial **cannot change** the static: the static region is **bit-identical
       across all configurations** (R82b, locked static DCP as the linking
       substrate), which is exactly the property `pr_verify` **proves** and R82c
       makes a **mandatory hard gate** before any `pr_bitstream` claim; and
    3. the R80 boundary is **thin and frozen** — only `clk`/`rstn` and the two
       512-bit AXI-Stream interfaces cross it, all in the `axis_aclk` domain —
       so a partial cannot reach a static clock domain it does not touch.
    A static-side timing violation is therefore a **static-build** fact, established
    (or knowingly accepted) once at flash time, not a per-pattern fact. If any of
    (1)–(3) ceases to hold — in particular if the R80 boundary ever gains a signal
    in another clock domain — **R73a MUST be revisited before the next partial is
    trusted.**
  - **R73a.5 (the known static violation — recorded, not hidden).** The PYRO static
    shell currently flashed on the U250 carries a **permanent** post-route setup
    violation of **WNS = −0.427 ns** on clock **`txoutclk_out[0]`**, **inside
    OpenNIC's own `cmac_usplus` IP**. It is **entirely outside `pyro_rp`**, in a CMAC
    datapath PYRO **ties off** and instantiates no logic in; no PYRO partial
    introduces it and no PYRO partial can repair it. It is recorded here as a known,
    **accepted** property of the flashed static image (R73a.4(1)), and it MUST be
    re-examined if PYRO ever carries traffic on the CMAC datapath.
  - **R73a.6 (the whole-design WNS is still recorded — SHOULD).** The PR flow SHOULD
    record the **whole-design** post-route WNS in the job diagnostics alongside the
    R73a.1 scoped WNS, so a static-side regression stays visible even though it does
    not gate a partial. If it is carried in the manifest it is an **additive** R47b
    field with a default (R72a discipline) and it **MUST NOT** feed `met_timing`.
  - **R73a.7 (relation to R73).** R73 is otherwise unchanged and continues to govern
    the OOC (`ooc_metrics`) path verbatim. R73's closing sentence — that the proxy is
    "superseded when `pr_flow_present` becomes true (the real static+dynamic timing
    then governs)" — is **clarified**, not reversed: what governs a **partial** is the
    real timing **of the reconfigurable module in context**, which is what R73a.1
    specifies. The static's own timing governs the **static**, once, at flash time.

- **R74 (AC-2-4 pre-registered calibration margin — normative constant).** The
  resource estimator's agreement with real P&R (AC-2-4) is judged against a margin
  **pre-registered here before any calibration data exists**, so tests cite a fixed
  constant rather than a number fit to the data. Define `ESTIMATOR_CALIBRATION_MARGIN`:
  for **every** pattern `p` in the calibration corpus that synthesizes successfully
  on the real (`vivado`) path, letting `est_luts/est_ffs` be the L2 estimator's
  numbers (R11/R12) and `real_luts/real_ffs` the genuine post-route utilization
  (R72c), the estimator is **calibrated/valid** iff all four hold:
  1. `real_luts ≤ est_luts` (the estimate never under-counts LUTs — conservative),
  2. `real_ffs ≤ est_ffs` (the estimate never under-counts FFs — conservative),
  3. `est_luts ≤ 10 × max(1, real_luts)` (not absurdly loose), and
  4. `est_ffs ≤ 10 × max(1, real_ffs)` (not absurdly loose),
  evaluated **per pattern** over the corpus (the AC fails if any successfully-
  synthesized pattern violates any clause). Rationale: the estimator's job is to
  reject over-budget patterns **before** synthesis, so it MUST be a conservative
  **over**-estimate (clauses 1–2 guarantee it never green-lights a circuit that
  then overflows); the 10× ceiling (clauses 3–4) keeps it from rejecting patterns
  that would in fact fit. The `max(1, ·)` guards the degenerate `real == 0` case.
  **Empty success set (normative).** If **no** corpus pattern synthesizes
  successfully on the real path, the calibration clause has nothing to judge and
  MUST record a **SKIP** (with a reason such as `no successful real syntheses to
  calibrate against`), **never a FAIL** — an empty universally-quantified set is
  vacuously satisfied, not a defect.
  Independently, a pattern that **passes** the estimate (is enqueued) but then
  **fails real synthesis** (does-not-fit, `met_timing == false` per R73, tool error,
  or the R77 timeout) MUST become permanently fallback-only with a diagnostic and
  no caller-visible error (R65) — this transition is LIVE on the real path and also
  drivable via `pyro.testing.inject_synth_failure` (R67).
  - **R74a (calibration is toolchain-bound; re-validate under the 2025.2 pin —
    transitional, v2.2.1).** Post-route utilization is a function of the Vivado
    release (R75), so `ESTIMATOR_CALIBRATION_MARGIN` is calibrated **per
    `toolchain_version`**. Any calibration data gathered under the superseded 2023.1
    pin (`0x17010000`) is **not evidence** for the 2025.2 pin (`0x19020000`) and MUST
    NOT be reused to claim AC-2-4 PASS. The AC-2-4 estimator-vs-real clause MAY claim
    **PASS only from real syntheses executed under the pinned 2025.2 toolchain**;
    until such a run exists it records the R74 **empty-success-set SKIP** (never a
    stale PASS). The R4/R75a cache key enforces this mechanically — a
    `0x17010000` artifact and a `0x19020000` artifact occupy distinct keys, so a
    2023.1 result is never served where a 2025.2 result is required.

- **R75 (`toolchain_version` encoding).** The `vivado` toolchain MUST report a
  `toolchain_version` (R47b manifest / R4 key) that encodes the **actual Vivado
  version**, distinct from the mock's `0x00000100`. Encoding (packed 32-bit):
  `(YY << 24) | (RR << 16) | build`, where `YY` is the two-digit release year and
  `RR` the point release. For the **pinned Vivado 2025.2** (R70a-pin, v2.2.1) this
  is **`0x19020000`** (`YY=25=0x19`, `RR=2`, `build=0`) — the **normative pinned
  value**. (Historical example: the superseded 2023.1 pin encoded to `0x17010000`,
  `YY=23=0x17`, `RR=1`.) The adapter SHOULD derive `YY`/`RR` from the tool's own
  version report rather than hard-coding, but MUST pin to the recorded install. `SHELL_VERSION` (the target shell / PR-region identifier) **remains the
  model-harness value** (`0x0A000001`) until a real PR flow exists
  (`pr_flow_present == true`), because no real shell/PR-region has been targeted.
  - **R75a (cache-key separation falls out of R4).** Because `toolchain_version` is
    a component of the R4 bitstream-cache key (and the R47b manifest key), a mock
    artifact (`0x00000100`) and a vivado artifact (`0x19020000`, or a
    differently-versioned Vivado) for the same
    pattern occupy **distinct keys** and never collide — the separation is a direct
    consequence of the existing key, requiring no new mechanism. Switching
    `PYRO_TOOLCHAIN` therefore never serves a mock stub where a real-metrics
    artifact is expected, or vice versa.

- **R77 (`vivado` per-job timeout — the adapter kills its own process tree).** The
  client-side service reaper (R63e) is **bookkeeping only**: it marks a key failed
  and unblocks the residency gauge, but it does **not** kill a hung worker's Vivado
  subprocess. Therefore the `vivado` toolchain MUST enforce **its own** subprocess
  timeout: it MUST launch Vivado with a deadline, and on expiry MUST **kill the
  entire Vivado process tree** (Vivado plus any children) and raise
  `SynthesisFailed`, which maps to the permanent-fallback semantics of R65. The
  default is `VIVADO_JOB_TIMEOUT = 1800 s` (30 min), overridable via the
  `ToolchainConfig` per-job-timeout field (R70a). This adapter-level kill is the
  authoritative one; the R63e reaper remains a backstop. The R63 asynchrony
  guarantees are unchanged: the timeout runs entirely inside the out-of-process
  worker and never blocks or slows any caller (a caller is served by fallback the
  whole time).

  > *Numbering note.* R76 and R78–R89 are assigned in §10.1/§10.2 (the Ethernet
  > control-frame format, Phase-2b bring-up, the `pyro.device` surface, and the PR
  > slot/job/substrate rulings), keeping those rulings adjacent to the material they
  > govern.

---

## 8. Routing / fallback decision logic

- **R51 (decision order).** For each top-level call, PYRO MUST decide as follows,
  in order, and the decision MUST be deterministic:
  1. If the **cached** `PYRO_DISABLE` flag is set → fallback. This flag is read
     from the cached value sampled at the last sampling point (R35a); the process
     environment MUST NOT be read on this per-call path.
  2. Compile-classify the pattern (cached, R4/R8). If not HW-eligible →
     fallback.
  3. If the pattern observes capture groups AND the caller will need them, mark
     for **hybrid** (still HW-eligible; groups via R18).
  4. Estimate work: if `len(subject) < S_min` AND estimated reuse `< N_reuse`
     (per-pattern call counter) → fallback (R3), unless the **cached**
     `PYRO_FORCE_MODEL` flag (R35a) is set.
  5. **Residency check (v2.0.0).** Determine the pattern's circuit tier (R4):
     - If **resident** and its identity block verifies (R47a) → dispatch to the
       generated circuit (hardware). If `PYRO_FORCE_MODEL` is set → dispatch to
       the software model (which stands in for the resident circuit, R7).
     - If **not resident** (cold / synthesizing / warm) → **fallback for this
       call**, and evaluate the synthesis-launch policy (R4a): if the pattern is
       now hot (≥ N_synth eligible dispatches) or was `prewarm`ed, enqueue
       synthesis / a PR load (R63/R64) in the **background**. Launching MUST NOT
       block or slow this call.
     - If synthesis previously failed → the pattern is permanently fallback-only
       (R65) → fallback.
     - **R51b (device-free precedence of R7).** When **no physical device is
       present**, R7 **takes precedence over this step 5**: the software model MAY
       serve **any** HW-eligible, gate-crossed, non-permanent-fallback dispatch at
       **any** tier (cold / synthesizing / warm / resident), counting as a
       `model` dispatch (R66). Residency/tier state is still tracked and reported
       per R4/R66 (launch/lifecycle bookkeeping and the genuine
       `NOT_RESIDENT`/`SYNTH` tier semantics remain live in the residency
       manager), and results MUST be byte-identical across tiers (R36 asynchrony
       clause, R53). **On hardware (Phase 2+), step 5 binds strictly**: a
       not-resident pattern falls back for this call while synthesis/PR-load
       proceeds in the background.
       - **R51b-strict (test-seam suspension — normative, v2.5.0).** The
         device-free precedence granted by R51b is **suspended** while the R67
         seam `pyro.testing.set_strict_residency(True)` is active: step 5 then
         binds strictly on a device-free host exactly as it does on hardware
         (not-resident ⇒ fallback for this call; resident ⇒ dispatch to the model
         standing in for the resident circuit, R7). This exists so that the
         fallback→resident **upgrade** required by **AC-3-2** is observable
         without a device — under plain R51b it is not, because the model serves
         every tier and no dispatch-counter edge exists at the upgrade. The
         suspension is gated by `PYRO_ENABLE_TEST_HOOKS` (R67), MUST NOT be
         reachable in production, and MUST NOT change any caller-visible result
         (R16/R36/R53) — only the R66 dispatch counters and the latency differ.
         R64a's device-free residency/eviction bookkeeping is unchanged and
         remains live in both modes.
  6. If no device and no model available → fallback.
  7. Otherwise (resident + verified) → hardware/model path.
- **R51a (Phase-0 safety gates).** Under R16's "when in doubt, fall back"
  umbrella, the Phase-0 implementation applies the following additional
  fallback gates. They are evaluated **before** dispatching to the hardware/model
  path (logically as part of R51 steps 1–2, ahead of step 6). Each is ordinary
  fallback **routing** (not a device error; counted as a normal fallback
  dispatch, never `fallback_after_error`). Each gate is a **Phase-0** condition
  that a later phase MAY lift individually by spec amendment; until then it holds.
  - **(a) Subject type gate.** If the subject is not **exactly** `str` or `bytes`
    (e.g., `bytearray`, `memoryview`, or any other buffer-protocol object) →
    fallback. Rationale: CPython `re` accepts these but preserves subtle
    return-type and `group(0)` slicing fidelity (e.g., `bytes` vs `bytearray`
    return types, memoryview slicing) that Phase 0 does not reproduce; delegating
    to CPython guarantees byte-identical results and return types (R16, R29).
  - **(b) `pos`/`endpos` gate.** If a `Pattern` method is called with a non-
    default `pos` or `endpos` (i.e., `pos != 0` or `endpos != len(subject)`) →
    fallback. Rationale: Phase 0 does not offset-map anchors/`\A`/`\b` under a
    windowed search; CPython delegation is exact. (Later phases MAY offload with
    correct anchor semantics under R24/§6.5.)
  - **(c) Anchor-context / full-span safety gate.** If the hybrid group
    reconstruction (R18) cannot be performed with the correct anchor context —
    i.e., the CPython re-run needed for groups or for group-0 confirmation cannot
    be guaranteed to see the same surrounding context (line boundaries for
    `^`/`$`/`\b` under `re.MULTILINE`, string edges for `\A`/`\Z`) as a match
    over the full subject would — → fallback. This gate guarantees R17/R18/R19
    are never approximated; when the safe full-span/anchored re-run is available,
    the path proceeds.
  - **(d) Surrogate / non-UTF-8-encodable gate.** In str mode, if the subject or
    pattern is not strictly UTF-8-encodable (unpaired surrogates) → fallback, per
    R14a.
- **R52 (correctness on any device error).** If the hardware path raises
  `PYRO_E_DEVICE`/`PYRO_E_TIMEOUT` or produces a result that fails re-
  verification (R19), PYRO MUST transparently retry on the fallback path and
  return the fallback result. Such an event MUST be counted in diagnostics
  (`pyro.re.stats()`), MUST NOT raise to the caller, and MUST NOT change the
  returned value relative to CPython. This path MUST be drivable from tests via
  the public injection seam `pyro.testing.inject_device_error` (R67), so that
  R52/R61 are verifiable without reading the implementation.
- **R53 (determinism).** Given identical inputs and configuration, the
  *observable result* MUST be independent of whether the hardware (resident
  generated circuit), model, or fallback path served it, and independent of the
  circuit lifecycle tier at the moment of the call (R36 asynchrony clause). Only
  timing/stats may differ.

---

## 9. Test & verification strategy (hooks for test-developer)

These define categories of tests the test-developer derives from the ACs. Tests
MUST NOT read the implementation; they exercise the Python API (§7.1), the C ABI
(§7.3), the register/DMA contract via the software model (§7.4), and the public
test/verification seams (§9.1). Any AC whose verification would otherwise require
reading internals MUST be reachable through a §9.1 seam.

- **R54 (differential oracle).** The primary oracle is CPython's `re`. For a
  large generated corpus of `(pattern, flags, subject)` triples, tests MUST
  assert that every PYRO API result equals the CPython `re` result (R16, R29,
  R36). The triple generator MUST include:
  - hand-written patterns exercising each construct in §5.1;
  - patterns from §5.2 to confirm fallback classification (R10);
  - greedy/lazy pairs (R23), empty-match cases (R22), anchor/multiline cases
    (R24), IGNORECASE folding (R15), and astral-codepoint offset cases (R21).
- **R55 (property-based fuzzing).** A property test MUST generate random
  patterns from the supported grammar (§5.1) and random subjects (including
  bytes and str, UTF-8 edge cases, and adversarial repetition) and assert
  equivalence to CPython `re` (R16). It MUST also generate random arbitrary
  patterns and assert that PYRO never returns a wrong result (either equals
  CPython or, if fallback, equals CPython trivially).
- **R56 (classification tests).** For each of R9/R10/R11–R13, tests MUST assert
  `pyro.re.explain()` returns the correct `eligible`/`reason` and that capacity
  overflow routes to fallback.
- **R57 (ABI conformance).** Tests MUST drive the C ABI (ABI 2.0.0) directly (via
  ctypes/cffi) against the software model: lifecycle (R39), generate/synth-
  request/status/load (R40), scan including overflow/resume and the
  `PYRO_E_NOT_RESIDENT` guard (R41), caps incl. PR-region budget (R42), error
  states incl. `PYRO_E_SYNTH`/`PYRO_E_NOT_RESIDENT` (R44), and thread safety
  (R43/R32).
- **R58 (harness contract tests).** Against the software model implementing §7.4,
  tests MUST assert: `ID`/`HARNESS_VER`/`CAPS` values (R45); the **circuit-
  identity** block and the dispatch-only-if-identity-matches trust boundary,
  including refusal + fallback on identity mismatch and mandatory window re-
  verification regardless of the circuit `verified` flag (R47a); the **PR
  bitstream artifact + manifest** contract — shell/PR-region compatibility check,
  bitstream integrity hash, and load refusal on mismatch (R47b); result-ring
  entry layout and overflow `OVF` behavior (R47); the operation sequence with no
  per-scan program load and single-issue enforcement (R48); alignment assertions
  (R49). Tests MUST confirm the obsolete `PROG_*` registers and `"PROG"` blob are
  absent.
- **R58a (synthesis-service & lifecycle tests).** Using the **mock toolchain**
  (R63b), tests MUST assert: the launch policy fires only at ≥ N_synth or via
  `prewarm` (R4a/R62); synthesis is asynchronous and never blocks a call
  (`compile`/`search` return promptly while a job is queued); the cold→warm→
  resident tier transitions and cache-key hits across a simulated process restart
  (R4/R63d); job dedup by key (R63a); single-tenant residency + deterministic
  eviction with no thrashing (R64); synthesis-failure → permanent fallback with
  no exception and correct stats (R65/R66); and — the correctness keystone — that
  results are **byte-identical across every tier** for the same pattern/subject
  (R36 asynchrony clause, R53), i.e. differential equivalence to CPython `re`
  whether served cold-fallback, model, or resident-circuit.
- **R59 (benchmark suite).** A benchmark harness MUST measure, for a defined
  corpus set, the metrics in R1–R5 and emit machine-readable results. The corpus
  set MUST include: (a) a ≥ 1 MiB log-file corpus with a reused pattern set of
  ≥ 32 patterns; (b) many-short-strings workload for the loss regime (R3);
  (c) a streaming corpus exceeding one `OUT_CAP` to exercise resumption;
  (d) an **over-approximation corpus** — patterns using the over-approximation
  classes declared in manifests (R19c: full Unicode categories, cross-length
  case folds, `\b` at UTF-8 boundaries) — so the harness can measure and
  **attribute the re-verification (CPU) cost** of false positives and check that
  it does not violate R19b's win-regime bound. On a host without hardware,
  benchmarks run against the model and assert only the *routing* thresholds
  (R3–R5), skipping absolute-throughput assertions (R1/R2) with a recorded SKIP,
  never a PASS.
- **R60 (transparency regression).** A test MUST take a corpus of real-world
  Python snippets using stock `re`, run them under `pyro.install()` and under
  stock `re`, and assert identical outputs and exceptions (R33–R36).
- **R61 (fault injection).** Tests MUST simulate device errors/timeouts/false-
  positive windows in the model and assert the fallback-retry path (R52) yields
  CPython-identical results and increments the correct stats counters, driving the
  injection via the public seam of **R67** (not by reading or patching internals).

### 9.1 Public test/verification seams (normative)

§9's premise is that **tests MUST NOT read the implementation**. Any behavior an
AC asks a test to verify MUST therefore be reachable from a public surface. The
following seams are **normative** so the spec-only test author can drive them
without reading code. They are PYRO-specific and MUST NOT appear on the standard
`re` namespace when interposing (§7.2), mirroring R31/R62.

- **R67 (test/verification seam — NEW obligation; extended v2.5.0).** PYRO MUST
  expose a test-hook namespace `pyro.testing` providing at least:
  - `pyro.testing.inject_device_error(kind="device"|"timeout", count=1) -> None`
    — cause the next `count` hardware/model dispatches to raise the corresponding
    device error (`PYRO_E_DEVICE`/`PYRO_E_TIMEOUT`), exercising the R52 fallback-
    retry path and its `fallback_after_error` counter (R66);
  - `pyro.testing.inject_synth_failure(pattern, flags=0) -> None` — cause the
    named pattern's next synthesis (R65) to fail, exercising the permanent-
    fallback transition, the diagnostic, and the `synth_failed` counter (R66);
  - `pyro.testing.inject_false_positive(pattern, flags=0, count=1) -> None` —
    cause the model to emit `count` spurious candidate windows for the pattern,
    exercising R19 re-verification (the spurious windows MUST NOT leak into
    results);
  - **`pyro.testing.await_synthesis(pattern, flags=0, timeout=30.0) -> str`**
    (**NEW, v2.5.0**) — block the **calling test** until the named pattern's
    circuit reaches a **terminal** lifecycle tier (R4/R31: `"resident"` or
    `"fallback_only"`), or until `timeout` seconds elapse, and return the
    `circuit_status` string (R31) actually reached — the last observed tier on
    timeout. It exists because AC-3-2 asserts a **tier transition**, and a
    spec-only test author has no other way to know when the background service
    (R63) has finished: polling `pyro.re.explain()` in a sleep-loop is a race, and
    reading service internals is forbidden by §9's premise. Constraints: it MUST
    NOT make synthesis synchronous, MUST NOT block, slow, or reorder any **other**
    caller's dispatch (R63/R2b asynchrony is unchanged and remains independently
    asserted by AC-1-5/AC-2-3), MUST NOT alter routing or results (R16/R53), and
    MUST be safe to call concurrently (R32). It is a **test observation point, not
    a synchronization primitive of the system**, and an AC that must prove
    asynchrony MUST NOT use it. When the R67 gate is disabled it returns the
    pattern's current tier immediately without waiting.
  - **`pyro.testing.set_strict_residency(enabled=True) -> None`** (**NEW,
    v2.5.0** — the *strict-residency / simulated-device* seam) — while enabled,
    the **R51b device-free precedence of R7 is suspended** and §8 **R51 step 5
    binds strictly, exactly as on hardware**: a HW-eligible, gate-crossed pattern
    whose circuit is **not resident** (cold / synthesizing / warm) is served by
    **fallback** for that call and counted as a normal `fallback` dispatch (R66,
    never `fallback_after_error`), while a **resident** circuit dispatches to the
    software model standing in for it (R7) and is counted as a `model` dispatch.
    Residency/eviction bookkeeping is unchanged (R64/R64a). It exists because
    **AC-3-2 is unobservable without it** on a device-free host: R51b lets the
    model serve *every* tier, so the fallback→resident **upgrade** AC-3-2 requires
    has no observable edge — every dispatch is a `model` dispatch at every tier.
    The seam does not invent new routing semantics; it **suspends an exception**
    (R51b) so the *production* hardware rule (R51 step 5) is the one under test.
    Results MUST remain byte-identical in both modes (R16/R36/R53): only the
    dispatch counters and the latency differ. It is cleared by
    `pyro.testing.reset()`.
  - `pyro.testing.reset() -> None` — clear all injected faults **and disable
    strict residency** (v2.5.0), restoring default R51b behavior.
  Injection MUST be **deterministic**, MUST NOT alter returned results relative
  to CPython `re` (R16 — an injected device error routes to fallback, an injected
  false positive is re-verified away, a strict-residency fallback returns the same
  bytes the model would have), and MUST be observable via
  `pyro.re.stats()` (R66). To avoid production foot-guns, **every** seam in this
  namespace — including the two added in v2.5.0 — MUST take
  effect only when `PYRO_ENABLE_TEST_HOOKS=1` was sampled (R35a sampling
  discipline); when disabled, the functions are importable but no-ops that raise
  nothing (`await_synthesis` returns the current tier immediately;
  `set_strict_residency` does nothing). This seam is a **new implementation
  obligation** introduced in v2.0.5 and **extended in v2.5.0**.
- **R68 (spec-named configuration knobs).** The following configuration overrides
  are **normative** and are sampled at the R35a sampling points (import,
  `install()`/`uninstall()`, `refresh_env()`):
  - `PYRO_N_SYNTH` (**NEW obligation**) — integer override of the R4a synthesis-
    launch threshold `N_synth` (default 1000). Tests MUST be able to set a small
    value to pin the exact launch boundary deterministically. An invalid value is
    ignored (default retained) and MAY be surfaced via stats/diagnostics.
  - `PYRO_CACHE_DIR` (**blessing of existing behavior**) — filesystem path for the
    persistent **bitstream cache** (R4). If unset, the runtime chooses a default
    location. Codified here so test harnesses can isolate and assert cache
    hygiene (cold→warm persistence across process restart, no residue).
  - `PYRO_TOOLCHAIN` (**NEW obligation, v2.1.0**) — `mock` | `vivado`, selecting the
    synthesis toolchain (default `mock`); full semantics in R70. An unrecognized
    value is treated as `mock` (fail safe).
  - `PYRO_VIVADO` (**NEW obligation, v2.1.0**) — the Vivado install directory used
    when `PYRO_TOOLCHAIN=vivado`; no default and no filesystem/`PATH` scanning by
    library code (R70). Unset/unresolvable ⇒ the real toolchain is unavailable
    (`toolchain_present=false`, R71).
  - `PYRO_DEVICE_IFACE` (v2.2.2; **no-default rule added v2.5.0**) — the `onic`
    netdev name the device transport (R86) binds for the AF_PACKET path. **No
    default; fail-closed** (F3): the name is not derivable from any spec fact, and a
    baked-in default silently binds `AF_PACKET` to the wrong — or, as on this host,
    a **nonexistent** — interface. If it is unset **and** the caller supplies no
    `DeviceConfig.iface`, `probe_device` MUST return `(False, <R83 canonical
    reason>)` naming `transport: PYRO_DEVICE_IFACE not configured`; it MUST NOT
    raise, MUST NOT guess, and MUST NOT scan the system for candidate interfaces
    (R70 no-scanning discipline). This is the same fail-loud rule already carried by
    `PYRO_PR_STATIC_DCP` / `PYRO_PR_REFERENCE_DCP`.
  - `PYRO_HW_SERVER` (**NEW obligation, v2.2.2**) — the Vivado `hw_server` URL the
    JTAG loader (R85/R86.5) connects to. **Default `TCP:localhost:3121`** (the
    stock `hw_server` port). Registered as a knob because the server location is
    operator-specific.
  - `PYRO_PR_STATIC_DCP` (**NEW obligation, v2.2.4**) — filesystem path to the
    **locked static shell DCP** (R82b), the in-context linking substrate for
    `pr_bitstream` jobs. **No default; fail-loud** — if a `pr_bitstream` job (R88) is
    requested and this is unset/unresolvable, synthesis is `SynthesisFailed` (R88),
    never a silent OOC fallback. Populates `ToolchainConfig.static_dcp`.
  - `PYRO_PR_REFERENCE_DCP` (**NEW obligation, v2.2.4**) — filesystem path to the
    **reference routed DCP** used by `pr_verify` (R82c). Same **no-default, fail-loud**
    rule (R88). Populates `ToolchainConfig.reference_dcp`. Registered so the residency
    service (constructed from sampled knobs) can reach the substrate artifacts without
    code changes; test/AC harnesses MAY also pass the `ToolchainConfig` fields directly.
  - `PYRO_PR_EVIDENCE_MANIFEST` (**NEW obligation, v2.2.5**) — filesystem path to a
    JSON **PR-flow evidence manifest** (a `pyro.synth.Manifest` sidecar) that the
    operator **asserts was produced by THIS host's PR flow against the
    currently-configured substrate DCPs** (`PYRO_PR_STATIC_DCP` /
    `PYRO_PR_REFERENCE_DCP`). It is the **host-observable proxy** for the one R82d
    conjunct that is otherwise Vivado-only — a passing `pr_verify` (R82c) — and is the
    evidence the availability probe consumes to flip `pr_flow_present` true (R83a).
    **No default; fail-loud/fail-closed** — no filesystem scanning to discover it
    (R70 discipline). Unlike the substrate DCP knobs it does **not** gate a synthesis
    job, so its absence is **not** a `SynthesisFailed` (R88); instead the availability
    probe keeps `pr_flow_present` **false** with the R83a canonical reason rather than
    guessing. Populates `ToolchainConfig.pr_evidence_manifest` (Optional path, default
    `None`; additive with mock-preserving default, R70a). Registered because the
    evidence's location is host/operator-specific; test/AC harnesses MAY also pass the
    `ToolchainConfig` field directly.
  Both device knobs are sampled at the R35a points and carried on the device config
  (R86); the library MUST NOT read them ad hoc. **Spec-fixed (NOT knobs):** the
  expected shell identity `PYRO_SHELL_SPEC16` (`0x0202`, compiled-in per R81), the
  probe/JTAG timeouts (`PYRO_PROBE_TIMEOUT`, `PYRO_JTAG_LOAD_TIMEOUT`, R84), and all
  R78 frame constants are **spec-fixed**; a device config MAY override them for
  tests (R86.6) but they are not operator env knobs. This resolves the R86 sampling
  note: only `PYRO_DEVICE_IFACE`/`PYRO_HW_SERVER` are env-sampled; every other device
  parameter is a spec constant.
  These knobs MUST NOT be read on the per-call hot path (R35a/R5).
- **R69 (device-free ABI-conformance scope — blessing/clarification).** The
  `pyro_generate` descriptor (R40/R40a) is an **L2-private serialized-automaton
  format that is deliberately NOT frozen** (§7.3 keeps classification in L2). Per
  the Task 7 ruling (§12 out-of-scope), pure-ctypes conformance drivers therefore
  cannot construct a *resident* circuit from public information. This is
  **sanctioned**: for device-free/`model://` bindings, the resident-tier register-
  level guarantees that require a constructed resident circuit (result-ring
  `OVF`, single-issue, R47a identity inspection, R49a alignment on a live scan)
  MAY be verified **behaviorally through the public Python surface** (AC-1-3/
  AC-1-4/AC-1-7) rather than via pure-ctypes construction. Pure-ctypes tests
  (R57/R58) cover everything publicly constructible — `pyro_abi_version`, `open`/
  `close`, `caps`, R44 defined-state on every error path (including
  `PYRO_E_INVALID` for misaligned/raw-descriptor inputs, R49a), enum/struct
  layout, `PROG_*`/blob absence, and thread-safety. Both together satisfy
  AC-1-1/AC-1-2 **without reading the implementation**. The spec does **not**
  require freezing the descriptor format (option b) nor shipping a test-vector
  artifact (option c); option (a), behavioral coverage, is the ruling.

---

## 10. Phased delivery plan

Each phase is independently testable. A phase's ACs MUST be satisfiable without
implementing any later phase. Earlier phases use the software model (R7) so that
hardware availability (§11) does not block software progress.

### Phase 0 — Software-only shim, classifier, and fallback

Deliver L1 (`pyro.re`), L2 classifier/compiler-front-end producing the AST and
HW-eligibility decision, the software model of L5 wired through a mock L3/L4 in
pure Python, and full fallback. No FPGA, no C library required yet (the model may
be Python for Phase 0; the C ABI is stubbed but shape-frozen).

- **AC-0-1.** `import pyro.re as re` provides every symbol in R26 with signatures
  matching stock `re` on CPython ≥ 3.11; `pyro.re.error is re.error`. (R26, R30)
- **AC-0-2.** For a differential corpus covering all §5.1 constructs, every
  `search/match/fullmatch/findall/finditer/sub/subn/split` result is byte-
  identical to stock `re`. The corpus MUST include inputs that trigger each
  Phase-0 safety gate (R51a) — `bytearray`/`memoryview` subjects, non-default
  `pos`/`endpos`, and `str` subjects/patterns containing unpaired surrogates
  (R14a) — and assert those route to fallback with byte-identical results and
  return types (not counted as `fallback_after_error`). (R16, R14a, R29, R51a,
  R54)
- **AC-0-3.** Every §5.2 construct and every over-capacity pattern is classified
  fallback-only by `explain()`, and still returns correct results via fallback.
  (R8, R10, R12, R31, R56)
- **AC-0-4.** Capture-group access triggers the hybrid re-run and returns
  byte-identical groups/spans; accessing only group 0 does not re-run. (R18, R20)
- **AC-0-5.** `pyro.install()`/`uninstall()` patch and restore stock `re` with
  identical observable behavior; `PYRO_DISABLE` set at a sampling point forces
  fallback. Env flags are sampled only at import, `install()`/`uninstall()`, and
  `pyro.refresh_env()`; a variable mutated mid-process takes effect only after
  the next sampling point, and `PYRO_DISABLE=1` present before process start or
  before `install()` behaves as if read per-call. (R33–R36, R35a–R35d, R60)
- **AC-0-6.** Routing is deterministic and cheap in absolute terms: short-input/
  one-shot calls route to the fallback path per the §8 decision order, and the
  added routing/decision overhead is ≤ 2 µs median per call. The per-call path
  MUST consult only cached env flags (no per-call `os.environ` read), per R35a.
  Phase 0 asserts the **absolute** loss-regime bound (R3a/R5), not the 1.15×
  relative ratio (R3b), which is scoped to Phase 1+ and is verified by
  AC-2-5/AC-3-3. (R3a, R5, R35a, R51, R59)
- **AC-0-7.** Empty-match, multiline/anchor, IGNORECASE-folding, and astral-
  codepoint offset cases are byte-identical to stock `re`. (R21–R24)
- **AC-0-8.** The frozen `pyro_rt.h` compiles and `pyro_abi_version()` returns
  `0x00010000`. (R37, R38)

### Phase 1 — HDL generator + circuit model + synthesis-service skeleton (mock toolchain)

Deliver: the L2 **HDL generator** (regex → automaton → synthesizable per-pattern
RTL) plus resource estimator (R11/R12); the C-ABI host runtime (L3) with the
bitstream cache, PR loader, and synthesis-service client; the **software model**
standing in for a generated circuit (R7); the **synthesis-service skeleton**
(R63) driven by a **mock toolchain** that emits stub artifacts + manifests
(R63b); the PR-artifact cache with tier transitions (R4); and the Ethernet/QDMA
**transport contract** (§7.4, model binding mandatory). No real Vivado required.
Every AC below is testable **without hardware**.

- **AC-1-1.** The C ABI (ABI 2.0.0, R39–R44) passes ABI-conformance tests against
  the model, including the async generate → synth-request → status → load
  lifecycle and the `PYRO_E_NOT_RESIDENT`/`PYRO_E_SYNTH` guards. Per R69, pure-
  ctypes tests cover all publicly constructible paths; resident-tier guarantees
  that require constructing a resident circuit from the L2-private descriptor MAY
  instead be verified behaviorally via the Python surface (AC-1-3/1-4/1-7). (R57,
  R69)
- **AC-1-2.** The harness contract (R45–R50) is implemented by the model and
  passes contract tests: identity-block trust boundary and dispatch-only-if-match
  (R47a), PR artifact + manifest compatibility/integrity checks (R47b), result-
  ring layout, overflow `OVF`, single-issue, alignment, and absence of the
  obsolete `PROG_*`/blob path. Register-level guarantees not constructible from
  public info in the device-free binding are verified behaviorally per R69. (R58,
  R69)
- **AC-1-3.** The HDL generator lowers every §5.1 construct to a valid per-pattern
  RTL circuit that the model recognizes; over-budget/over-`MAX_*` patterns are
  rejected by the estimator and route to fallback (R11–R13). For a ≥ 1 MiB
  corpus, results from the model circuit are byte-identical to stock `re`,
  including leftmost reconciliation (R16, R17, R19, R9, R12).
- **AC-1-4.** Overflow/resume: a corpus producing more matches than `OUT_CAP`
  returns the complete, correct match list via streaming resumption. (R41, R47)
- **AC-1-5.** The synthesis-service skeleton with the mock toolchain: launch
  policy fires only at ≥ N_synth or via `pyro.prewarm` (R4a/R62), with the exact
  boundary pinned deterministically by setting `PYRO_N_SYNTH` (R68); jobs run out
  of process and never block calls; cold→warm→resident transitions and cache-key
  hits persist across a simulated restart (with an isolated `PYRO_CACHE_DIR`,
  R68); job dedup by key; deterministic single-tenant eviction without thrashing;
  synthesis failure (injected via `pyro.testing.inject_synth_failure`, R67) →
  permanent fallback, no exception, correct `stats()` counters; no residual
  service processes after teardown (R63e). (R62–R66, R67, R68, R58a)
- **AC-1-6.** Fault-injection via the public seams (R67): model false-positive
  windows (`inject_false_positive`) are re-verified and never leak; device errors
  (`inject_device_error`) trigger fallback-retry with CPython-identical output and
  increment `fallback_after_error`; `NOT_RESIDENT`/`SYNTH` are counted as routing,
  not `fallback_after_error`. All drivable without reading the implementation.
  (R19, R44, R52, R61, R67)
- **AC-1-7.** Asynchrony correctness keystone: for the same pattern/subject,
  results are byte-identical whether served cold-fallback, via the model, or via
  a "resident" model circuit — proving R36's asynchrony clause and R53 without
  hardware. (R36, R53, R58a)

### Phase 2 — Real Vivado flow + on-hardware bring-up

Deliver the **real** synthesis flow (generated RTL → Vivado OOC synthesis + P&R →
honest post-route manifest, R47b/R72) integrated into the synthesis service at the
`MockToolchain` seam, selected by `PYRO_TOOLCHAIN=vivado` (R70). On the current
host **P1 is only partially satisfied** (§11 P1; R71): Vivado is present and
synthesizes the target part, but **no OpenNIC PR-partition floorplan / PR-bitstream
flow exists** and **there is no PYRO-loadable artifact or PYRO-usable transport for
the runtime user** (no `/dev/qdma*`, no `CAP_NET_RAW`; the one-time full-image
reprogram to a PR-enabled shell needs root PCIe-rescan cooperation — JTAG access
itself is available and is not a blocker). Accordingly the real OOC-synthesis,
honest-metrics, calibration,
and real-synth-failure clauses are **LIVE**, while every clause requiring a
loadable PR bitstream or an operable device records a **SKIP** (never PASS) whose
reason names the absent prerequisite — the live/SKIP matrix is normative in **R71**.
Requires the toolchain and transport prerequisites (§11 P1/P2).

- **AC-2-1.** With the real toolchain present (`toolchain_present`, R70/R71), a
  HW-eligible pattern is synthesized by **real Vivado OOC synth + place-and-route**
  for the target part; the resulting manifest records **genuine post-route**
  utilization fitting the PR-region budget (R11) and `met_timing` at the 250 MHz
  proxy clock (R73), carries `payload_kind == "ooc_metrics"` (R72), and the
  artifact populates the bitstream cache and reloads **warm** across restarts
  (R4/R63d) — all **LIVE**. The production of a genuine **loadable PR bitstream**
  (`payload_kind == "pr_bitstream"`) requires `pr_flow_present` and → **SKIP** with
  that reason (R71/R72). If `toolchain_present == false` → the whole AC SKIPs with
  reason `toolchain_present=false`. (R11, R47b, R63, R70–R75, P1)
- **AC-2-2.** A synthesized circuit loaded into the PR region passes the harness
  contract on the physical device: identity block verifies (R47a), scans produce
  sound/complete candidate windows re-verified to byte-identical CPython results
  over a ≥ 1 MiB corpus. This clause requires `device_usable` ∧ `pr_flow_present`
  (R71/R83), both **false** on this host → **SKIP** with the R83 canonical reason
  (the combined `device_usable=false — …; pr_flow_present=false — …` enumeration of
  unmet conditions). This clause **flips to LIVE** once the probe reports
  `device_usable` true (valid `ID_REPLY` + `CAP_NET_RAW`, R81/R83) and
  `pr_flow_present` true (R82). The equivalent byte-identical correctness over a
  ≥ 1 MiB corpus is covered on the software model by AC-1-3. (R16, R17, R19, R47a,
  R71, R81–R83, F5)
- **AC-2-3.** Cold→warm→resident timing matches R4's model. With
  `toolchain_present` (R71), the **cold→warm** leg is **LIVE**: real Vivado
  synthesis genuinely takes minutes, runs out of process, and **never blocks or
  slows a caller** (calls are served by fallback throughout, R63). The
  **warm→resident** PR-load timing (~O(100 ms)) and immediate resident dispatch
  require `device_usable` → **SKIP** with reason `device_usable=false`. The
  async/non-blocking property is additionally asserted against the model in all
  cases. (R4, R63, R71, R77)
- **AC-2-4.** Bounded-repeat expansion respects `MAX_REPEAT` (R11–R13, model-side,
  LIVE). With `toolchain_present` (R71), the resource estimator agrees with real
  P&R utilization within the **pre-registered `ESTIMATOR_CALIBRATION_MARGIN`**
  (R74), evaluated per pattern over the calibration corpus — **LIVE**; and a
  pattern that passes the estimate but **fails real synthesis** (does-not-fit,
  `met_timing==false` per R73, tool error, or the R77 timeout) becomes permanently
  fallback-only with a diagnostic and no caller-visible error (R65) — **LIVE** on
  the real path, and also drivable via `pyro.testing.inject_synth_failure` (R67).
  If `toolchain_present == false` → the estimator-vs-real and real-synth-fail
  clauses SKIP with reason `toolchain_present=false`; the injected-failure and
  `MAX_REPEAT` clauses still run. (R11–R13, R65, R73, R74, R77)
- **AC-2-5.** Throughput of a resident circuit meets R1 on hardware (≥ 1 GiB/s
  floor, 5 GiB/s target). This requires `device_usable` (R71) → **SKIP** with
  reason `device_usable=false`. **It additionally requires the P2
  performance-target transport (v2.5.1):** with `device_usable` true but only
  the functional raw-Ethernet control transport present (P2 QDMA char-devs
  absent, F5), the R1 throughput clauses (here and at AC-3-3) are **measured
  anyway** over the control transport (R3b.3 measure-first discipline) and
  record a **SKIP whose reason states the measured control-plane throughput
  and names the missing P2 prerequisite** — never a PASS (the floor is unmet)
  and never a FAIL (what is missing is the P2 data plane, not the circuit). The **absolute** routing/decision bounds (R3a/R5,
  ≤ 2 µs median) are asserted against the model in all cases. The **relative**
  loss-regime bound (R3b, 1.15×) binds **only when its native-hot-path precondition
  holds (R3c)**; while the R51 routing decision is served at Python level it does
  not, so the R3b clause records a **SKIP** whose reason states the measured
  **R3b.1 median-of-≥5** ratio at the R3b.2 subject size
  (not a FAIL), and R3b becomes a hard PASS requirement at **AC-3-3**. (R1, R2, R3,
  R3a, R3b, R3c, R5, R59, R71)
- **AC-2-6.** Single-tenant PR arbitration on hardware: loading a second
  pattern's circuit evicts the first per R64; results remain byte-identical
  across evict/reload cycles. This requires `device_usable` ∧ `pr_flow_present`
  (R71/R83) → **SKIP** with the R83 canonical reason (the combined
  `device_usable=false — …; pr_flow_present=false — …` enumeration); it **flips to
  LIVE** once both predicates flip true (R81–R83).
  The eviction policy and byte-identical results across evict/reload are asserted
  on the model as a **firm, non-vacuous LIVE** clause: the device-free residency
  manager MUST exercise single-tenant residency and fire deterministic LRU eviction
  (R64a). (R64, R64a, R53, R71)

### Phase 2b — On-hardware bring-up (device probe + PR shell)

Deliver the in-band Ethernet control-frame protocol (§10.1, R78/R79), the public
`pyro.device` host surface — `encode_frame`/`decode_frame`/`probe_device`/
`load_partial` (R86) — the host probe that flips `device_usable`/`pr_flow_present`
(R81/R83), the PYRO-built OpenNIC PR shell with the `pyro_rp` reconfigurable
partition and its default ID-stub child (§10.2, R80/R82), and the JTAG
partial-bitstream load path (R85).
The **host-side frame codec** is testable **without hardware** (LIVE); every
on-device clause records a **SKIP** (never PASS) until the live probe answers
(R71/R83). Requires the transport privilege (`CAP_NET_RAW`, P2/P3) and, for the PR
clauses, the flashed PR shell and validated PR flow (P1).

- **AC-2b-1.** The host-side control-frame codec `pyro.device.encode_frame`/
  `decode_frame` (R78/R86) round-trips the **normative test vectors** byte-for-byte
  on their PYRO-header-onward portion (R78 frame offset 14+): encoding an
  `ID_REQUEST`, a `MATCH_REQUEST`, an `ID_REPLY`, a `MATCH_REPLY`, a
  `STATUS`/`ERROR`, a `PERF_REQUEST`, and a `PERF_REPLY` (R78.10(a)–(g)) from
  their field values produces **exactly** the
  R78.10 bytes, and decoding those bytes
  recovers **exactly** the R78-named fields (big-endian PYRO header; little-endian
  embedded `pyro_match` entries in `MATCH_REPLY`). The codec MUST raise
  `PyroFrameError` (R86.1) on a frame whose `length` exceeds the MTU bound
  (payload ≤ 1486) or whose `magic`/`version`/`kind`/`flags` is invalid, and MUST
  ignore trailing zero-padding beyond `length`. This clause is **LIVE** (pure host
  code, no device). (R78, R86)
- **AC-2b-2.** The live device probe `pyro.device.probe_device(config)`
  (R81/R83/R84/R86.4) sends an `ID_REQUEST` on the `onic` netdev and requires a valid
  `ID_REPLY` whose `static_shell_id` high half matches `PYRO_SHELL_SPEC16` within
  `PYRO_PROBE_TIMEOUT`; it returns `(True, …)` only then **and** with `CAP_NET_RAW`
  present, else `(False, <R83 canonical reason>)`. When `CAP_NET_RAW` is absent it
  MUST return `(False, <exact R86.4 non-elided two-condition literal>)` **without**
  raising `PermissionError` or requiring privilege (R86.4). The no-privilege /
  timeout / `SPEC16`-mismatch / valid-`ID_REPLY` paths are all driven **without
  hardware** through the R86.6 `DeviceConfig` seams (`cap_check` /
  `transport_factory`), so the **privilege-free `(False, reason)`** and injected-reply
  paths are **LIVE** and testable on this host now; the real **`(True, …)` /
  `device_usable` flip against a physical board** requires the flashed PR shell +
  `CAP_NET_RAW` → **SKIP** until the probe answers. (R78, R81, R83, R84, R86)
- **AC-2b-3.** A per-pattern PYRO circuit is implemented in-context against the
  **locked static DCP** (R82), `pr_verify` passes and is recorded as the independent
  manifest field **`pr_verified == true`** (R47b/R82c), and the emitted manifest
  carries `payload_kind == "pr_bitstream"` (R72/R82) with the real shell/PR-region
  `SHELL_VERSION`; the partial bitstream loads into `pyro_rp` via JTAG/`hw_server`
  without disturbing the PCIe link (R85), after which the loaded circuit answers
  `ID_REPLY` with a non-zero `rp_child_id` and serves `MATCH_REQUEST`. Requires
  `pr_flow_present` ∧ `device_usable` → **SKIP** on this host (no validated PR flow,
  device not usable); flips to LIVE when both predicates flip true. The JTAG load is
  performed by `pyro.device.load_partial(config, path)`, which raises `PyroLoadError`
  on any JTAG/`hw_server` failure or `PYRO_JTAG_LOAD_TIMEOUT` expiry (R84/R85/R86.5) —
  drivable without hardware via the R86.6 `load_runner` seam. The `pr_verified`
  manifest consistency (R47b/R82c) is checkable on the host without a device.
  (R47b, R72, R80, R82, R83, R85, R86)

### Phase 3 — Transparent interposition + benchmarks

Deliver production-grade L0 interposition, the full benchmark suite (R59), and
the diagnostics surface, integrating the generated-circuit pipeline with
automatic tier-based dispatch and prewarming.

- **AC-3-1.** With `pyro.install()`, the transparency-regression suite (R60)
  passes on a corpus of real `re`-using programs: identical outputs and
  exceptions vs. stock `re`, including patterns that transition tiers mid-run.
  (R36, R60)
- **AC-3-2.** Automatic tier-based dispatch is transparent: a hot pattern is
  prewarmed/synthesized in the background and silently upgraded from fallback to
  resident-circuit dispatch with byte-identical results throughout; a cold or
  fallback-only pattern is served by fallback. The tier-upgrade edge
  is made observable on a device-free host by the R67 seams
  `set_strict_residency` (R51b-strict) and `await_synthesis`. (R4a, R51, R51b,
  R53, R62, R67)
- **AC-3-3.** The benchmark suite emits machine-readable metrics for R1–R5 and,
  on hardware, demonstrates the win regime (R1/R2, resident circuits) and the
  loss-regime routing (R3), and records synthesis/PR-load costs separately from
  scan throughput. Without hardware, R1/R2 are SKIP, R3–R5 PASS. This is the phase
  at which the native routing/dispatch hot path is a deliverable, so the R3b
  **relative** 1.15× bound becomes a **hard PASS requirement** here (its R3c
  precondition now holds) rather than a SKIP as in earlier phases, measured per
  the R3b.1/R3b.2 protocol against the compiled-extension build (R3b.3); on a
  pure-Python install the clause records the R3c SKIP and AC-3-3 is not claimable
  as PASS. (R1–R5, R3b, R3c,
  R59)
- **AC-3-4.** `pyro.re.stats()` reports the dispatch counters (hardware / model /
  fallback / fallback-after-error) **and** the lifecycle counters (synth
  launched/succeeded/failed, synthesizing, resident, evicted, pr_loads); fault
  and synthesis-failure injection increment the correct counters without altering
  results. (R52, R61, R65, R66)

### 10.1 Ethernet control-frame format (Phase-2b enablement)

This subsection **lifts the R76 deferral** (v2.2.0). In Phase 2b all PYRO device
communication is carried **in-band as raw Ethernet frames** on the `onic` netdev
named by `PYRO_DEVICE_IFACE` (F3/R68). The user box's **AXI-Lite window is tied off** in Phase 2b
(no MMIO CSR path): every §7.4 register meaning the host needs is reached through
the frame protocol below. Sending/receiving frames uses `AF_PACKET` and therefore
requires `CAP_NET_RAW` (P2/P3, R83). The shell's `max_pkt_len` is **1518 bytes**.

- **R76 (control-frame format — deferral, LIFTED in v2.2.0).** The v2.1.0 ruling
  deferred the raw-Ethernet control-frame format to Phase-2b enablement and repaired
  a dangling reference. **That deferral is now lifted:** the normative frame layout
  is specified in **R78** below and R50/P2 are updated to cite it. R76 is retained as
  the disposition record; the substantive contract is R78/R79.

- **R78 (control-frame format — normative).** PYRO device control is a request/reply
  protocol over Ethernet II frames.
  - **R78.1 (EtherType).** The EtherType is **`0x88B5`** (IEEE 802 local
    experimental Ethertype 1), written **big-endian** on the wire (bytes `88 B5`).
    Frames with any other EtherType are not PYRO frames and MUST be ignored by both
    peers.
  - **R78.2 (frame anatomy & endianness).** A PYRO frame is: the 14-byte Ethernet II
    header (`dst_mac[6] src_mac[6] ethertype[2]`), then a **fixed 14-byte PYRO
    control header**, then a kind-specific **payload**, then the Ethernet FCS
    (appended/checked by the NIC, not by PYRO). **All multi-byte fields of the PYRO
    control header and of the request/reply payload framing are big-endian (network
    byte order).** The one exception is the `MATCH_REPLY` result entries, which embed
    the existing **24-byte little-endian `pyro_match`** layout (R47) verbatim so the
    host runtime consumes them without re-marshaling; this mixed-endianness is
    intentional and is called out at R78.7.
  - **R78.3 (PYRO control header — fixed 14 bytes).** Offsets are from the start of
    the frame (the PYRO header begins at frame offset 14):

    | Off | Size | Field      | Endian | Meaning                                            |
    |-----|------|------------|--------|----------------------------------------------------|
    | 14  | 1    | `magic`    | —      | `0x50` (`'P'`) — sanity byte; MUST be `0x50`        |
    | 15  | 1    | `version`  | —      | protocol version; this spec defines **`0x01`**     |
    | 16  | 1    | `kind`     | —      | message kind (R78.4)                               |
    | 17  | 1    | `flags`    | —      | reserved; MUST be `0x00` in version 1              |
    | 18  | 2    | `slot`     | BE     | circuit/pattern slot identifier (0 = ID stub; 1 = the Phase-2b pattern slot, R87)|
    | 20  | 4    | `seq`      | BE     | sequence number; a reply MUST echo its request's   |
    | 24  | 2    | `length`   | BE     | payload length in bytes (excludes the 14B header)  |
    | 26  | 2    | `reserved` | BE     | MUST be `0x0000`                                    |

    The payload begins at frame offset **28**.
  - **R78.4 (message kinds).** `kind` is one of:

    | Value  | Name            | Direction      |
    |--------|-----------------|----------------|
    | `0x00` | *reserved*      | —              |
    | `0x01` | `ID_REQUEST`    | host → device  |
    | `0x02` | `ID_REPLY`      | device → host  |
    | `0x03` | `MATCH_REQUEST` | host → device  |
    | `0x04` | `MATCH_REPLY`   | device → host  |
    | `0x05` | `STATUS`/`ERROR`| device → host  |
    | `0x06` | `PERF_REQUEST`  | host → device  | *(v2.4.0, R78.11)* |
    | `0x07` | `PERF_REPLY`    | device → host  | *(v2.4.0, R78.11)* |

    Any other value is invalid and MUST be dropped (device) or treated as a
    protocol error routing the call to fallback (host, R52). A device built
    before a kind was defined treats it as "any other value" and drops it —
    this is the normative forward-compatibility behavior R78.11 relies on.
  - **R78.5 (`ID_REQUEST` / `ID_REPLY`).** `ID_REQUEST` carries an **empty payload**
    (`length == 0`, `slot == 0`). `ID_REPLY` echoes `seq` and carries a **12-byte**
    payload (`length == 12`):

    | Off (from payload) | Size | Field             | Endian | Meaning                                   |
    |--------------------|------|-------------------|--------|-------------------------------------------|
    | 0                  | 4    | `static_shell_id` | BE     | flashed PR-shell identity (R81)           |
    | 4                  | 4    | `harness_version` | BE     | **resident (R45) harness version** (R78.5b) |
    | 8                  | 4    | `rp_child_id`     | BE     | `0x00000000` = default ID stub; non-zero = loaded pattern circuit id (R78.5a) |

    - **R78.5a (`rp_child_id` derivation — normative, v2.2.4).** For a loaded pattern
      circuit, `rp_child_id` is the **low 32 bits of the R47a pattern hash, forced
      non-zero** (if the low 32 bits are `0`, use `0x00000001`); it is
      **overridable** by the generator but MUST remain non-zero. This blesses the
      implementation: it is deterministic, collision-improbable, and **host-verifiable**
      — the host holds the same pattern hash (R47a) and MAY check that the resident
      child's `rp_child_id` matches the low-32/forced-non-zero of the pattern it
      dispatched, as a cheap wire-level cross-check ahead of (not replacing) the R47a
      identity/window re-verification. The default ID stub (R80) reports `0`.
    - **R78.5b (`harness_version` namespace — normative reconciliation, v2.2.4).** The
      wire `harness_version` field carries the **R45 resident-harness contract
      version** (the `HARNESS_VER` register value; `0x00010000` in the current model
      harness / flashed ID stub). This is a **distinct namespace** from the
      **`PYROART1` artifact-header `HARNESS_VERSION`** (`0x00020100` as of v2.3.0,
      the L2-generator artifact-format version tied to ABI 2.0.0). A pattern child MUST report the
      **wire/R45 value** here (matching the ID stub for wire consistency), **not** the
      artifact value. The two are intentionally different and MUST NOT be "fixed" to
      match each other: one identifies the *resident hardware harness contract*, the
      other the *host-side artifact-container format*. (The `PYROART1` header is
      ABI-frozen, R72a; neither value is changed by this ruling.)

  - **R78.6 (`MATCH_REQUEST`).** `slot` selects the resident circuit; payload framing
    (big-endian), followed by the raw subject chunk:

    | Off | Size | Field       | Endian | Meaning                                             |
    |-----|------|-------------|--------|-----------------------------------------------------|
    | 0   | 8    | `start_off` | BE     | byte offset of this chunk in the logical stream (mirrors `pyro_scan` `start_off`, R41) |
    | 8   | 2    | `out_cap`   | BE     | max result entries the host will accept in the reply |
    | 10  | 2    | `reserved`  | BE     | MUST be `0x0000`                                     |
    | 12  | N    | `corpus`    | —      | raw subject bytes, `N = length − 12`, `N ≤ 1474`    |

  - **R78.7 (`MATCH_REPLY`).** Echoes `seq` and `slot`; payload:

    | Off | Size  | Field      | Endian | Meaning                                             |
    |-----|-------|------------|--------|-----------------------------------------------------|
    | 0   | 2     | `count`    | BE     | number `M` of result entries following              |
    | 2   | 2     | `status`   | BE     | bit0 `OVF` (more matches pending — host resumes via `start_off`, R41/R47), bit1 `ERR` |
    | 4   | 4     | `reserved` | BE     | MUST be `0x00000000`                                |
    | 8   | 24·M  | `entries`  | LE     | `M` result entries, each the 24-byte little-endian `pyro_match` (R47): `start(8) end(8) pattern_id(4) flags(4)` |

    `M ≤ 61` per reply (payload ≤ 1486). Each entry's `flags` bit0 (`verified`) is
    **advisory only**; the host MUST re-verify every window per R19 before returning
    it, exactly as on the DMA path (R47a). On `OVF`, the host resumes the scan with
    `start_off` advanced past the last returned entry (R41).
  - **R78.8 (`STATUS`/`ERROR`).** Payload: `code` (4 bytes BE, mapping to a
    `pyro_status` value, R38) followed by optional UTF-8 diagnostic text
    (`N = length − 4`). A device that receives a `MATCH_REQUEST` for a `slot` that is
    not resident (e.g. the default ID stub) MUST reply `STATUS`/`ERROR` with
    `code = PYRO_E_NOT_RESIDENT` (7); the host then falls back (R51). The normative
    wire vector for this exact reply is R78.10(e).
  - **R78.9 (MTU, minimum length, padding).** The **total** Ethernet frame
    (`dst+src+ethertype + 14B PYRO header + payload + 4B FCS`) MUST NOT exceed
    **1518** bytes; equivalently the PYRO `length` (payload) MUST be **≤ 1486**. A
    frame shorter than the 60-byte Ethernet L2 minimum (excluding FCS) MUST be
    **zero-padded to 60 bytes** by the sender; the `length` field alone delimits the
    real payload, and the receiver MUST ignore trailing padding beyond `length`.
    There is **no PYRO-level fragmentation**: a corpus larger than one
    `MATCH_REQUEST` is chunked by the host and reassembled by result-`start_off`
    (R41), not by IP-style fragmentation.
  - **R78.10 (normative test vectors).** The following frames are **normative**; the
    host codec MUST produce and parse them byte-for-byte (AC-2b-1). MACs are example
    locally-administered addresses (host `02:00:00:00:00:01`, device
    `02:00:00:00:00:02`); frame validity does **not** depend on the specific MACs.
    Bytes are shown in hex, in wire order; where a frame is under 60 bytes it is
    zero-padded to 60 on the wire (padding not shown).

    **(a) `ID_REQUEST`** (host → device; `seq = 1`, empty payload):
    ```
    02 00 00 00 00 02  02 00 00 00 00 01  88 B5   ; eth: dst, src, ethertype
    50 01 01 00  00 00  00 00 00 01  00 00  00 00 ; PYRO: magic ver kind flags | slot | seq | length | resv
    ```
    (28 meaningful bytes; zero-padded to 60 on the wire.)

    **(b) `ID_REPLY`** (device → host; echoes `seq = 1`;
    `static_shell_id = 0x02025A3C`, `harness_version = 0x00010000`,
    `rp_child_id = 0` = default ID stub):
    ```
    02 00 00 00 00 01  02 00 00 00 00 02  88 B5
    50 01 02 00  00 00  00 00 00 01  00 0C  00 00
    02 02 5A 3C  00 01 00 00  00 00 00 00           ; payload: static_shell_id | harness_version | rp_child_id
    ```
    (40 meaningful bytes; zero-padded to 60 on the wire. `static_shell_id >> 16 ==
    0x0202 == PYRO_SHELL_SPEC16` ⇒ the probe accepts it, R81/R83.)

    **(c) `MATCH_REQUEST`** (host → device; `slot = 1`, `seq = 2`, `start_off = 0`,
    `out_cap = 4`, corpus `"abcabc"` = `61 62 63 61 62 63`):
    ```
    02 00 00 00 00 02  02 00 00 00 00 01  88 B5
    50 01 03 00  00 01  00 00 00 02  00 12  00 00
    00 00 00 00 00 00 00 00  00 04  00 00           ; payload: start_off(8) | out_cap | resv
    61 62 63 61 62 63                                ; corpus "abcabc"
    ```
    (46 meaningful bytes; zero-padded to 60 on the wire. `length = 0x12 = 18`.)

    **(d) `MATCH_REPLY`** (device → host; echoes `slot = 1`, `seq = 2`; the resident
    circuit for pattern `bc` reports two windows `[1,3)` and `[4,6)`; `count = 2`,
    `status = 0` (no OVF); entries are 24-byte little-endian `pyro_match`,
    `pattern_id = 0`, `flags = 0` ⇒ **unverified**, host re-verifies per R19):
    ```
    02 00 00 00 00 01  02 00 00 00 00 02  88 B5
    50 01 04 00  00 01  00 00 00 02  00 38  00 00
    00 02  00 00  00 00 00 00                        ; payload: count | status | resv
    01 00 00 00 00 00 00 00  03 00 00 00 00 00 00 00  00 00 00 00  00 00 00 00 ; entry0: start=1 end=3 pid=0 flags=0 (LE)
    04 00 00 00 00 00 00 00  06 00 00 00 00 00 00 00  00 00 00 00  00 00 00 00 ; entry1: start=4 end=6 pid=0 flags=0 (LE)
    ```
    (84 bytes; `length = 0x38 = 56`. This round-trip — (c)→(d) — is the normative
    `MATCH` vector for AC-2b-1.)

    **(e) `STATUS`/`ERROR`** (device → host; the exact reply the **default RP child
    (R80) ID stub** produces to the `MATCH_REQUEST` (c) — no pattern resident, so
    `code = PYRO_E_NOT_RESIDENT = 7`; echoes `slot = 1`, `seq = 2`; no diagnostic
    text, `length = 4`):
    ```
    02 00 00 00 00 01  02 00 00 00 00 02  88 B5
    50 01 05 00  00 01  00 00 00 02  00 04  00 00
    00 00 00 07                                      ; payload: code = PYRO_E_NOT_RESIDENT (7, BE)
    ```
    (32 meaningful bytes; zero-padded to 60 on the wire. The exchange (c)→(e) is the
    normative `STATUS`/`ERROR` vector for AC-2b-1 and for the RP-child xsim testbench;
    it is byte-consistent with (c) by MAC-swap, `slot`/`seq` echo, and `kind = 0x05`.
    A `STATUS`/`ERROR` frame MAY carry UTF-8 diagnostic text after `code` (R78.8); the
    ID stub emits none, so this vector's `length` is exactly `4`.)

    **(f) `PERF_REQUEST`** (host → device; `slot = 1`, `seq = 3`, empty payload —
    v2.4.0, R78.11):
    ```
    02 00 00 00 00 02  02 00 00 00 00 01  88 B5
    50 01 06 00  00 01  00 00 00 03  00 00  00 00
    ```
    (28 meaningful bytes; zero-padded to 60 on the wire.)

    **(g) `PERF_REPLY`** (device → host; echoes `slot = 1`, `seq = 3`; example
    counter values from the (c) scan — corpus `"abcabc"`, 6 bytes fed one
    byte/cycle ⇒ `cycles = 6`, `bytes = 6`. The **byte layout** is normative;
    the counter **values** are measurements and are illustrative here, exactly
    as the window values in (d) are):
    ```
    02 00 00 00 00 01  02 00 00 00 00 02  88 B5
    50 01 07 00  00 01  00 00 00 03  00 10  00 00
    00 00 00 00 00 00 00 06   00 00 00 00 00 00 00 06 ; payload: cycles(8 BE) | bytes(8 BE)
    ```
    (44 meaningful bytes; `length = 0x10 = 16`; zero-padded to 60 on the wire.
    The round-trip (f)→(g) is the normative `PERF` vector for AC-2b-1.)
  - **R78.11 (`PERF_REQUEST` / `PERF_REPLY` — R45a counter read-out, v2.4.0).**
    `PERF_REQUEST` carries an **empty payload** (`length == 0`); `slot` selects the
    resident circuit exactly as in R78.6. A resident pattern child replies
    `PERF_REPLY`, echoing `seq` and `slot`, with a **16-byte** payload:

    | Off | Size | Field    | Endian | Meaning                                            |
    |-----|------|----------|--------|----------------------------------------------------|
    | 0   | 8    | `cycles` | BE     | R45a `CYCLES` (`{CYCLES_HI,CYCLES_LO}`) of the most recent scan |
    | 8   | 8    | `bytes`  | BE     | R45a `BYTES` (`{BYTES_HI,BYTES_LO}`) of the most recent scan    |

    A `PERF_REQUEST` for a slot that is not resident MUST get `STATUS`/`ERROR`
    `PYRO_E_NOT_RESIDENT` (mirroring R78.8). **Semantics:** the child's responder is
    single-threaded (one request is parsed only after the previous reply is sent), so
    the engine is never BUSY when a `PERF_REQUEST` is served; the returned values are
    therefore the R45a post-`DONE` stable counters of the most recent completed scan
    (`0/0` if no scan has run since the partial was configured), read **coherently**
    (no R45a half-tearing) and **non-destructively** (the counters clear only on the
    next scan's `CTRL.START`/`CTRL.RESET`, R45a). **Compatibility:** kinds
    `0x06`/`0x07` are additive. A child built before v2.4.0 (artifact
    `harness_version < 0x00020200`) — including the flashed default ID stub — drops
    `PERF_REQUEST` per R78.4; the host MUST treat a reply timeout as **"counters
    unavailable"** (the wire analogue of R45a's harness < 2.1.0 zero-read rule),
    never as a device fault, and MUST NOT route the *scan* path to fallback because
    of it. Support is host-detectable a priori from the loaded artifact's manifest
    `harness_version` (R47b). Per R79 this is a **partial-bitstream-only** change:
    the static shell and the R80 boundary are untouched, and no reflash is required.

- **R79 (frame parsing lives inside `pyro_rp`).** The PYRO control-frame parser and
  responder are implemented **inside the reconfigurable partition `pyro_rp`**
  (§10.2, R80), **not** in the static OpenNIC shell. Consequence (normative): a
  change to the R78 protocol (new `kind`, new payload field, `version` bump) is a
  **partial-bitstream change only** and MUST NOT require rebuilding or reflashing the
  static shell. The static shell's sole responsibilities on the datapath are to
  carry frames to/from `pyro_rp` over the R80 AXI-Stream boundary and to own PCIe;
  it is protocol-agnostic. This keeps the frozen static image (R80/R81) stable across
  protocol evolution and is the reason the boundary (R80) — not the protocol — is the
  thing that must be permanent.

### 10.2 Phase-2b on-hardware bring-up (PR shell, probe, PR flow)

Ground truth for Phase 2b (owner-verified, 2026-07-06): the U250 at
`0000:af:00.0/1` is the **owner's own board**; reprogramming is permitted. The board
is reflashed with a **PYRO-built OpenNIC PR shell**: Xilinx `open-nic-shell` at
commit `ce85c8d`, ported to **Vivado 2025.2** (R70a-pin, v2.2.1), with a `pyro` user-box
plugin containing a reconfigurable partition **`pyro_rp`** (Xilinx DFX,
`HD.RECONFIGURABLE` + `Pblock`). Per-pattern PYRO circuits become **PARTIAL**
bitstreams for `pyro_rp` (R72 `payload_kind == "pr_bitstream"`). JTAG programming is
verified working as this user (FT4232H bridge; `hw_server` enumerates `xcu250_0`).

- **R80 (reconfigurable-partition boundary contract — thin & permanent).** The
  interface between the static shell and `pyro_rp` is **frozen for the life of the
  flashed static image** so that every partial bitstream links against the same
  locked static DCP (R82). The boundary is **thin**: exactly
  - `clk` — the 250 MHz user-box clock (F4), and `rstn` — synchronous active-low
    reset;
  - one **512-bit AXI-Stream slave** carrying host→device (QDMA **H2C**) frames into
    `pyro_rp`: `s_axis_tdata[511:0]`, `s_axis_tkeep[63:0]`, `s_axis_tlast`,
    `s_axis_tuser[47:0]` (16-bit `size`, 16-bit `src`, 16-bit `dst`), `s_axis_tvalid`,
    `s_axis_tready`;
  - one **512-bit AXI-Stream master** carrying device→host (QDMA **C2H**) frames out
    of `pyro_rp`: `m_axis_tdata[511:0]`, `m_axis_tkeep[63:0]`, `m_axis_tlast`,
    `m_axis_tuser[47:0]` (`size`/`src`/`dst` **pass-through** as needed for egress),
    `m_axis_tvalid`, `m_axis_tready`.

  The box's **AXI-Lite is tied off** in Phase 2b (all control is in-band, R78). No
  other signal crosses the boundary; adding one is a **static-shell change** (a new
  flash + new locked DCP), not a partial-bitstream change. The **default child**
  shipped inside the flashed static image is an **ID stub**: a minimal circuit that
  parses frames (R79), answers `ID_REQUEST` with `ID_REPLY` (`rp_child_id == 0`,
  R78.5) carrying the shell identity (R81), and replies `STATUS`/`ERROR`
  `PYRO_E_NOT_RESIDENT` to any `MATCH_REQUEST` (no pattern resident, R78.8). The ID
  stub is what lets `device_usable` flip true **before** any pattern circuit is built
  (R83): a freshly flashed board answers the probe.

- **R81 (static shell identity — `static_shell_id`).** The flashed PR shell carries a
  32-bit **`static_shell_id`** returned in `ID_REPLY` (R78.5). It is derived as
  `(SPEC16 << 16) | BUILD16`, where **`SPEC16 = (spec_MAJOR << 8) | spec_MINOR`** of
  the spec version the shell was built against (for this spec, `2.2` ⇒ `0x0202`) and
  **`BUILD16`** is the low 16 bits of the shell build's Unix epoch **minutes**
  (`epoch_seconds // 60`, mod 2¹⁶) — a build-identifying discriminator. The host
  runtime is compiled with an expected **`PYRO_SHELL_SPEC16`** (`0x0202` for this
  spec). The probe (R83/R84) MUST verify **`(static_shell_id >> 16) ==
  PYRO_SHELL_SPEC16`** exactly before declaring the device usable, and MUST record
  the full 32-bit value in `pyro.re.stats()`/diagnostics; the low 16 `BUILD16` bits
  identify the specific flashed image and are **not** required to equal a fixed
  constant. **`static_shell_id` is distinct from `SHELL_VERSION`** (R45/R75, the
  manifest/cache-key shell/PR-region identifier, still `0x0A000001` for the model
  harness): `static_shell_id` is the **wire probe identity**, `SHELL_VERSION` is the
  **artifact-compatibility key**. `SHELL_VERSION` is unchanged by this bump and
  becomes the real static-shell/PR-region identifier only when `pr_flow_present`
  flips true and a `pr_bitstream` is emitted (R75/R82, a future bump).

- **R82 (PR link flow — same tool, `pr_verify` mandatory, locked static is the
  substrate).** Normative rules for producing partial bitstreams for `pyro_rp`:
  - **R82a (single tool release).** The static shell **and** every partial bitstream
    MUST be produced by the **same Vivado release** — the R70a-pinned **2025.2**
    (`toolchain_version == 0x19020000`, R75). A partial bitstream whose recorded
    `toolchain_version` differs from the static image's MUST be **refused** at load
    and the pattern treated as fallback (not a device error), exactly as an R47b
    compatibility failure → `PYRO_E_NOT_RESIDENT` (R47c).
  - **R82b (locked static DCP is the linking substrate).** The implementation-locked
    static shell **DCP** (with `pyro_rp` as `HD.RECONFIGURABLE`) is the substrate
    against which every `pyro_rp` child is implemented **in context**. Partial
    bitstreams are generated from that locked static so the static region is
    bit-identical across all configurations.
  - **R82c (`pr_verify` is mandatory before claiming `pr_bitstream`).** Vivado
    **`pr_verify`** (comparing the static region across the routed configurations)
    MUST **pass** before the synthesis service may emit a manifest with
    `payload_kind == "pr_bitstream"` (R72). If `pr_verify` was not run or does not
    pass, the manifest MUST NOT claim `pr_bitstream` — it stays `ooc_metrics` (R72,
    honest metrics, no device claim) or the job fails (R65). Fabricating a
    `pr_bitstream` claim without a passing `pr_verify` is a defect (R72c honesty). A
    passing `pr_verify` MUST be recorded in the **independent manifest field
    `pr_verified: bool`** (R47b, v2.2.2): `pr_verified == true` iff `pr_verify`
    passed, and `pr_verified` MUST be consistent with `payload_kind` (`true` exactly
    when `payload_kind == "pr_bitstream"`; `false`/absent for `mock_stub` and
    `ooc_metrics`). This gives tests a specced seam to verify `pr_verify` ran without
    inferring it from `payload_kind` alone (AC-2b-3). The `pr_verified`/`payload_kind`
    consistency is enforced defensively at manifest construction and `from_json`
    (R47b-consistency, v2.2.3), not merely by the producer.
  - **R82d (`pr_flow_present` artifacts + their locations).** `pr_flow_present`
    (R71/R83) is the conjunction of: the **locked static DCP** (R82b;
    `ToolchainConfig.static_dcp`, from `PYRO_PR_STATIC_DCP`, R68) present and validated
    on the host, the **reference routed DCP** for `pr_verify`
    (`ToolchainConfig.reference_dcp`, from `PYRO_PR_REFERENCE_DCP`, R68), a `pyro_rp`
    floorplan (`Pblock` + `HD.RECONFIGURABLE`, R80 boundary), and a PR-generation flow
    that produces a genuine loadable partial bitstream with a passing `pr_verify`
    (R82c). The substrate paths are **config-in with no filesystem scanning** (R70
    discipline) and **fail-loud** when a `pr_bitstream` job needs an absent one (R88).
    Of these four conjuncts, the first three have host-observable proxies (the locked
    static DCP carries the `pyro_rp` floorplan/`HD.RECONFIGURABLE`/`Pblock`, so its
    presence/readability attests both the static substrate and the floorplan; the
    reference routed DCP's presence/readability attests the `pr_verify` reference); the
    **fourth** — a flow that actually produced a genuine loadable partial with a
    **passing `pr_verify`** — has **no** host-observable proxy except a manifest the
    flow emitted. The spec-named host-observable evidence of that conjunct is therefore
    the **`PYRO_PR_EVIDENCE_MANIFEST`** (R68) manifest asserting
    `payload_kind == "pr_bitstream"` with `pr_verified == true`, consumed by the
    availability predicate R83a.

- **R83 (R71 predicate-flip semantics + canonical SKIP string — normative,
  supersedes the v2.1.3 literal).** Amends R71's dispositions with the exact
  flip conditions and the canonical SKIP reason:
  - **`pr_flow_present` flips true** iff the R82d artifacts are all present and
    validated on the host, made **host-observable** by the concrete evidence predicate
    **R83a** (v2.2.5). While any condition is unmet it is **false** and clauses
    requiring it SKIP with the R83a canonical reason
    `pr_flow_present=false — <first unmet R83a condition>` (this **supersedes** the
    earlier placeholder `<first missing R82d artifact>` wording).
  - **`device_usable` flips true** iff **both**: (i) a **live probe** sends
    `ID_REQUEST` and receives a valid `ID_REPLY` whose `static_shell_id` satisfies
    the R81 `SPEC16` check within `PYRO_PROBE_TIMEOUT` (R84); **and** (ii) transport
    privilege **`CAP_NET_RAW`** is present (AF_PACKET send/recv on the `onic` netdev,
    P2/P3). If either is absent, `device_usable` is **false**.
  - **Canonical SKIP string (normative).** When `device_usable == false`, the probe
    MUST emit `device_usable=false — ` followed by a comma-separated enumeration, in
    this fixed order, of **exactly the unmet conditions** among:
    1. `transport: PYRO_DEVICE_IFACE not configured`
    2. `probe: no valid ID_REPLY (no reply within PYRO_PROBE_TIMEOUT, or static_shell_id SPEC16 mismatch)`
    3. `transport: CAP_NET_RAW absent`
    A clause requiring `device_usable ∧ pr_flow_present` appends
    `; pr_flow_present=false — <first unmet R83a condition>` when that predicate is
    also false. This **supersedes** the fixed v2.1.3 literal
    (`… no loadable PR artifact …, no PYRO transport …, full reprogram needs root
    PCIe-rescan cooperation`); the enumeration now reflects the *actual* probe
    result. **On the current host** `device_usable == false` with reason
    `device_usable=false — probe: no valid ID_REPLY (no reply within PYRO_PROBE_TIMEOUT, or static_shell_id SPEC16 mismatch), transport: CAP_NET_RAW absent`
    (PR shell not yet flashed; `CAP_NET_RAW` not granted).
  - A **SKIP is never PASS**; a device clause PASSes only from real execution against
    a probe-confirmed `device_usable == true` (R71 honesty, unchanged).

- **R83a (`pr_flow_present` evidence predicate — normative, v2.2.5).** R83's first
  bullet said `pr_flow_present` flips true when the R82d artifacts are "present and
  validated on the host," but the decisive validation — a **passing `pr_verify`**
  (R82c) — is a Vivado-only fact with no host-observable trace of its own. This
  sub-clause gives the availability probe the **exact, host-observable predicate** it
  MUST implement so it stops reporting a conservative `false` on a host where the full
  PR flow demonstrably works. `pr_flow_present == true` **iff all** of the following
  hold, evaluated in this **fixed order** (the first that fails is the one named in the
  canonical `pr_flow_present=false — …` reason):
  1. **static DCP** — `ToolchainConfig.static_dcp` is configured (from
     `PYRO_PR_STATIC_DCP`, R68) **and** the path exists and is readable (attests the
     R82b locked static substrate **and** the R80/`pyro_rp` floorplan it carries).
     Else: `static DCP not configured/unreadable (PYRO_PR_STATIC_DCP)`.
  2. **reference DCP** — `ToolchainConfig.reference_dcp` is configured (from
     `PYRO_PR_REFERENCE_DCP`, R68) **and** the path exists and is readable (attests the
     R82c `pr_verify` reference). Else:
     `reference DCP not configured/unreadable (PYRO_PR_REFERENCE_DCP)`.
  3. **evidence manifest configured** — `ToolchainConfig.pr_evidence_manifest` is
     configured (from `PYRO_PR_EVIDENCE_MANIFEST`, R68) **and** the path exists and is
     readable. Else:
     `evidence manifest not configured/unreadable (PYRO_PR_EVIDENCE_MANIFEST)`.
  4. **evidence manifest parses** — the file loads via the **R47b-consistency-enforcing
     loader** `pyro.synth.Manifest.from_json(...)` **without raising** (so a corrupt or
     internally-inconsistent manifest — one violating
     `pr_verified == (payload_kind == "pr_bitstream")` — is rejected at this step, not
     silently accepted). Else: `evidence manifest not parseable`.
  5. **evidence attests a verified PR bitstream** — the parsed manifest carries
     **`payload_kind == "pr_bitstream"` and `pr_verified == true`**. (By
     R47b-consistency these two agree once step 4 succeeds, so either check suffices;
     the probe checks **both** defensively.) Else:
     `evidence manifest not pr_verified (no verified pr_bitstream attested)`.

  The predicate does **no filesystem scanning** and reads **no `os.environ`** itself
  (R70/R5/R35a): all three paths arrive on the `ToolchainConfig` from the R68 knobs
  sampled at the R35a points. The probe **never raises** for an unmet condition — a
  missing/unreadable/unparseable/not-`pr_verified` evidence input yields
  `(false, "pr_flow_present=false — <first unmet condition above>")`, exactly the
  conservative fail-closed disposition.

  **Honesty rationale (why an operator-asserted manifest is legitimate evidence).** A
  manifest with `payload_kind == "pr_bitstream"` and `pr_verified == true` can only be
  emitted by the synthesis service **after** a `pr_verify` pass (R82c), and
  R47b-consistency makes that claim un-fabricable at the boundary (a hand-edited
  inconsistent manifest is rejected at `from_json`). It is therefore the **honest
  host-observable proxy** for the Vivado-only `pr_verify` fact. Naming it via
  `PYRO_PR_EVIDENCE_MANIFEST` is an **operator act of exactly the same kind** as
  configuring `PYRO_PR_STATIC_DCP`/`PYRO_PR_REFERENCE_DCP`: the operator asserts "this
  artifact belongs to this host's currently-configured PR flow." Universalized (Kant),
  the rule is uniform across all three substrate inputs — the probe trusts operator
  configuration of file paths and validates what it can machine-check (existence,
  readability, parse, consistency, the `pr_verified` claim), and asserts nothing it
  cannot observe. It does **not** claim `device_usable` (a separate live-probe +
  `CAP_NET_RAW` predicate, R83) and it does **not** silently promote SKIP to PASS.

  **Staleness caveat (SHOULD, not MUST — and why).** The evidence binds to the
  **currently-configured substrate**: a manifest is only meaningful against the same
  locked static / reference DCPs it was produced from. Operators **SHOULD** regenerate
  the evidence manifest (re-run a `pr_bitstream` job → `pr_verify`) **after rebuilding
  or reflashing the static shell** (a new locked static DCP), because a stale manifest
  from an older static would misrepresent the current flow. This is an **advisory
  SHOULD, not an enforced MUST**, for an honesty reason: the host **cannot presently
  machine-detect staleness** — the manifest's `shell_version` is still the R89
  **placeholder** (`SHELL_VERSION == 0x0A000001`), not yet the real PR-region identity,
  so there is nothing to cross-check the evidence against the configured static DCP. A
  MUST the probe cannot enforce would be dishonest. **Forward path:** when R89 lands and
  the `pr_bitstream` manifest's `shell_version` becomes the **real PR-region identity**
  (relatable to `static_shell_id`, R81), R83a gains a machine-checkable sixth
  condition — `evidence manifest.shell_version` matches the PR-region identity of the
  configured `static_dcp` — at which point staleness becomes probe-detectable and this
  SHOULD is promoted to an enforced MUST. That promotion is **deferred** to the R89
  bump; until then R83a is exactly conditions 1–5.

- **R84 (probe and PR-job timeouts — normative constants).**
  - **`PYRO_PROBE_TIMEOUT = 500 ms`** per `ID_REQUEST` attempt, with **3 attempts**
    (fresh `seq` each) before the probe concludes `device_usable == false`. A
    `MATCH_REQUEST`/`MATCH_REPLY` round-trip that exceeds `PYRO_PROBE_TIMEOUT` is a
    device timeout → `PYRO_E_TIMEOUT`, routed to fallback (R52), not a crash.
  - **`VIVADO_PR_JOB_TIMEOUT = 3600 s` (60 min)** — the per-job timeout for
    **`pr_bitstream`** synthesis jobs, which include a full in-context place-and-route
    **link** against the locked static plus `pr_verify` (R82) and are heavier than the
    OOC-only `ooc_metrics` job governed by **R77** (`VIVADO_JOB_TIMEOUT = 1800 s`).
    R77's discipline is otherwise unchanged and applies to PR jobs verbatim: the
    adapter enforces the deadline **inside the out-of-process worker** and, on expiry,
    **kills the entire Vivado process tree** and raises `SynthesisFailed` →
    permanent-fallback (R65); the R63e reaper is the bookkeeping backstop. Neither
    timeout ever blocks or slows a caller (R63/R77 asynchrony unchanged).
  - **Two named `ToolchainConfig` timeout fields (normative wording, v2.2.4).** The
    two synthesis-job timeouts are carried as **two distinct named fields** (R70a),
    not one adapter-selected field: **`job_timeout_s` (default `1800`, the R77
    `ooc_metrics`/OOC default)** and **`pr_job_timeout_s` (default `3600`, the
    `VIVADO_PR_JOB_TIMEOUT` PR default)**. The adapter selects `pr_job_timeout_s`
    when the job requests `pr_bitstream` (R88, `ToolchainConfig.pr_bitstream == True`),
    else `job_timeout_s`. This supersedes the earlier ambiguous "single per-job-timeout
    field, adapter-selected default" wording; both fields carry mock-preserving
    defaults (R70a). (Reaper windows are correspondingly mode-tiered — PR/OOC/mock —
    outside this ruling.)
  - **`PYRO_JTAG_LOAD_TIMEOUT = 600 s` (10 min) — normative constant, v2.2.2.** The
    deadline for the R85 JTAG `program_hw_devices` step in `load_partial` (R86.5),
    which is distinct from and much shorter than the synthesis-job timeouts above (it
    programs an already-built bitstream, not a P&R run). On expiry the loader MUST
    apply **R77 kill discipline** — terminate the `hw_server`/programming process
    tree — and raise `PyroLoadError` (R86.1/R86.5). Overridable via the device config
    (R86.6) for tests; the default is the spec constant. A JTAG-load timeout is a
    load failure (transient, → `PYRO_E_NOT_RESIDENT`/fallback via R47c), **not** a
    permanent synthesis failure (R65).

- **R85 (Phase-2b PR-load path is JTAG; ICAP/MCAP deferred).** In Phase 2b,
  `pyro_circuit_load` on hardware (R40) for a `pr_bitstream` artifact (R72/R82) is
  performed **out of band via JTAG** using Vivado `hw_server` (verified working on the
  owner's board). This is safe with respect to the live PCIe link: the **static shell
  owns PCIe** and is bit-identical across configurations (R82b), so partial
  reconfiguration of `pyro_rp` over JTAG **does not disturb** the PCIe link. (The
  pre-2.5.1 claim that the `onic` netdev is also undisturbed was **half-true**: the
  netdev survives the JTAG program itself, but the child comes up wedged — R85a —
  and the R85a recovery transiently detaches/re-attaches the driver, so the netdev's
  MAC changes across a load; nothing may key on it.) The async/non-blocking load
  contract (R63) and single-tenant residency/eviction semantics (R64) are unchanged;
  only the load *mechanism* is JTAG.
  **ICAP/MCAP-based (in-band, self-hosted) partial reconfiguration is explicitly
  deferred** to a later phase — like R76 deferred the frame format — because it
  requires additional shell plumbing (an ICAP/MCAP controller reachable from the host
  path) that the `ce85c8d` PR shell does not yet expose. Until that ruling is lifted
  under a future version bump, the **only** sanctioned on-device PR-load mechanism is
  the JTAG/`hw_server` path, and no AC may assume ICAP/MCAP self-reconfiguration.

- **R85a (JTAG PR wedge + mandatory in-band recovery — NEW, v2.5.1, HW-proven
  2026-07-15).** A raw JTAG `program_hw_devices` partial load reconfigures `pyro_rp`
  correctly but leaves the child **unreachable**: the shell has no DFX decoupler and
  performs no coordinated static-side reset during the ~17–40 s reconfig, so (i) the
  box-side AXIS interface desyncs, and (ii) the RP's floating outputs emit garbage
  frames that leave the **QDMA C2H stream engine stuck mid-frame**. Power-on masks
  both only because it resets static, RP, and interface together. Recovery is
  **in-band** (no power cycle) and MUST perform, in order:
  1. detach the `onic` driver (quiesce host-side queue state);
  2. **user-box reset** — BAR2 `0x014` bit 0 (`system_config`), which drives
     `pyro_250mhz`'s `generic_reset` and resets `pyro_rp` **and** its static-side
     box logic together;
  3. **QDMA subsystem soft reset** — BAR2 `0x00C` bit 0, which drives the QDMA IP's
     `soft_reset_n` **only** (`sys_rst_n` is the edge-connector `pcie_rstn`), so the
     PCIe link survives — this clears the stuck C2H stream state;
  4. re-attach the driver and bring the netdev up (fresh queue contexts).
  The canonical implementation is **`scripts/pyro_wedge_recover.sh`** (root; hosts
  grant it to the operator via a `NOPASSWD` sudoers entry scoped to exactly that
  script). Neither step 2 nor step 3 **alone** recovers the child (both proven
  insufficient in isolation on HW). A recovery failure is **transient** in the R84
  sense (maps to not-resident/fallback upstream, never permanent fallback). With
  R85a recovery, the JTAG path is a **complete, sanctioned PR-load mechanism**; the
  R85 ICAP/MCAP deferral stands but is no longer a blocker for on-device residency.

- **R86 (host-side device API surface — `pyro.device`, NEW obligation, v2.2.1).**
  PYRO MUST expose a public module **`pyro.device`** so the coder and the
  test-developer derive the device-plumbing surface from this spec independently. It
  is **PYRO-specific** and MUST NOT appear on the standard `re` namespace when
  interposing (§7.2), mirroring R31/R62/§9.1. All four functions are **config-in with
  no ad-hoc environment reads**: the two operator-environment values — the netdev
  name (`PYRO_DEVICE_IFACE`) and the `hw_server` URL (`PYRO_HW_SERVER`) — are the
  **only** env-sampled inputs (R68, sampled at the R35a points) and are carried on
  the passed device config; every other device parameter (expected
  `PYRO_SHELL_SPEC16` per R81, the R84 timeouts, all R78 frame constants) is a
  **spec-fixed constant** with the literal default the coder used, overridable on the
  config for tests only (R86.6). The functions MUST NOT read `os.environ` themselves
  (R5/R35a discipline).
  - **R86.1 (exception taxonomy).** The module defines a base
    **`PyroDeviceError(Exception)`** and subclasses **`PyroFrameError(PyroDeviceError)`**
    (malformed frame in encode/decode) and **`PyroLoadError(PyroDeviceError)`**
    (JTAG/PR-load failure). These are the **only** exception types the R86 functions
    raise; they MUST NOT leak `PermissionError`, `OSError`, or transport internals to
    callers (wrap them).
  - **R86.2 (`encode_frame`).**
    `encode_frame(kind, slot, seq, payload, flags=0) -> bytes` builds the **PYRO
    control header + payload** portion of a frame per R78 (R78 frame offset 14
    onward — i.e. the Ethernet *payload*; the 14-byte Ethernet L2 header
    `dst/src/0x88B5`, plus FCS and any 60-byte-minimum zero-padding, are the
    transport/NIC's responsibility, R78.9, and are deliberately not this function's
    concern since frame validity is MAC-independent, R78.10). `kind` is an R78.4
    value; `slot`/`seq` are integers; `payload` is the already-serialized payload
    `bytes`; `flags` MUST be `0` in version 1. It sets `magic=0x50`, `version=0x01`,
    `length=len(payload)`, `reserved=0`, encoding all header fields **big-endian**
    (R78.2/R78.3). It MUST raise `PyroFrameError` if `len(payload) > 1486` (R78.9),
    if `flags != 0`, or if `kind` is not a **sendable** kind. The **sendable kinds
    are exactly `0x01`–`0x07`** (R78.4; `0x06`/`0x07` added by v2.4.0, R78.11).
    **`0x00` is `reserved` and is NOT a message
    kind** — `encode_frame` MUST reject it (confirming the coder's reading, v2.2.2);
    likewise any value `> 0x07` or otherwise undefined in R78.4 is rejected.
  - **R86.3 (`decode_frame`).** `decode_frame(data: bytes) -> result` parses the PYRO
    control header + payload (the same slice `encode_frame` returns) and returns a
    structured result whose fields are named exactly for the R78 header:
    `magic, version, kind, flags, slot, seq, length, payload` (with `payload` the raw
    `bytes` of the kind-specific body; typed sub-parsing of `ID_REPLY`/`MATCH_REPLY`
    payloads MAY be offered by additional helpers but is not required of
    `decode_frame`). It MUST raise **`PyroFrameError`** if `magic != 0x50`, if
    `version != 0x01`, if `flags != 0`, if `length` exceeds the R78.9 bound (1486),
    or if `length` is inconsistent with the available bytes (fewer payload bytes than
    `length`). Trailing bytes beyond `length` (Ethernet zero-padding) MUST be
    ignored, not treated as payload (R78.9). `encode_frame`/`decode_frame` MUST
    round-trip the R78.10 normative vectors byte-for-byte on their PYRO-header-onward
    portion (AC-2b-1).
  - **R86.4 (`probe_device`).** `probe_device(config) -> (usable: bool, reason: str)`
    implements the R83/R84 device-usability probe: it sends `ID_REQUEST` (up to 3
    attempts, `PYRO_PROBE_TIMEOUT` each, R84), validates the `ID_REPLY`
    `static_shell_id` `SPEC16` (R81), and checks `CAP_NET_RAW`. It returns
    `(True, reason)` only when both conditions hold (the reason string MAY record the
    observed full `static_shell_id`), else `(False, reason)` where `reason` is the
    **R83 canonical `device_usable=false — …` enumeration** of the unmet conditions.
    **`probe_device` MUST NEVER require elevated privilege to return `(False,
    reason)`:** if `CAP_NET_RAW` is absent it MUST detect that **before** attempting
    any privileged `AF_PACKET` operation and MUST NOT raise `PermissionError` or
    crash. When `CAP_NET_RAW` is absent the probe cannot send `ID_REQUEST`, so
    **both** R83 conditions are unmet and the returned reason MUST be the exact,
    **non-elided** two-condition canonical literal (converged with R83's current-host
    string):
    `device_usable=false — probe: no valid ID_REPLY (no reply within PYRO_PROBE_TIMEOUT, or static_shell_id SPEC16 mismatch), transport: CAP_NET_RAW absent`.
    It returns, never raises, for any "not usable" condition (including timeout and
    `SPEC16` mismatch); it MAY raise `PyroFrameError` only on a genuinely malformed
    reply frame, which the caller treats as not-usable.
  - **R86.5 (`load_partial`).** `load_partial(config, partial_bitstream_path) -> None`
    implements the R85 JTAG partial-bitstream load via `hw_server`. On success it
    returns `None`; on any failure (`hw_server` unreachable, JTAG chain mismatch,
    incompatible or corrupt bitstream, load error) it MUST raise **`PyroLoadError`**
    with a diagnostic message. It MUST NOT disturb the PCIe link (R85). Artifact
    admissibility (`payload_kind == "pr_bitstream"`, same-release, integrity —
    R72b/R82) is enforced by `pyro_circuit_load` (R40) upstream; `load_partial`
    performs the JTAG mechanism and surfaces mechanism failures as `PyroLoadError`
    (mapping, in the residency manager, to the R47c not-resident / R65 semantics as
    appropriate). It MUST bound the `program_hw_devices` step by
    `PYRO_JTAG_LOAD_TIMEOUT` (R84) and, on expiry, kill the programming process tree
    and raise `PyroLoadError`.
    **R85a recovery is part of the load (v2.5.1):** after a successful programming
    step, `load_partial` MUST run the R85a recovery command (`config.recover_cmd`,
    default the spec constant `PYRO_RECOVER_CMD` — `sudo -n <repo>/scripts/
    pyro_wedge_recover.sh`, non-interactive so an absent sudoers grant fails fast
    and loud) **through the same runner seam** (`load_runner`, R86.7 contract) and
    bounded by the same `jtag_load_timeout_s`; a nonzero rc or runner failure raises
    `PyroLoadError` naming the recovery step (transient per R85a — the fabric holds
    the new child; only reachability failed). `recover_cmd=None` disables the step
    (explicit config injection, R86.6 discipline — for hosts/tests where the load
    mechanism itself is faked or recovery is operator-managed). A load is
    **successful only if both steps succeed**; `load_partial` performs no probe of
    its own (probing needs `CAP_NET_RAW`, which the load path deliberately does not
    require — R86.4 remains the reachability oracle).
  - **R86.6 (`DeviceConfig` type + spec-sanctioned private test seams — NEW, v2.2.2).**
    The device functions take a public **`pyro.device.DeviceConfig`** — a frozen
    dataclass whose fields are **config-in with no env reads**, each **defaulted from
    a spec constant** so `DeviceConfig()` is fully usable without introspection
    (from v2.5.0, `iface` is the deliberate exception — it has no spec constant to
    default from, F3/R68):
    - `iface: Optional[str]` — netdev name, default from `PYRO_DEVICE_IFACE` (R68)
      sampled at R35a; **no spec default — `None` when unconfigured, fail-closed**
      (F3, R68 no-default rule, v2.5.0): `probe_device` then returns `(False, <R83
      canonical reason>)` naming `transport: PYRO_DEVICE_IFACE not configured`;
    - `hw_server: str` — default from `PYRO_HW_SERVER` (R68), spec default
      `"TCP:localhost:3121"`;
    - `expected_spec16: int` — default `PYRO_SHELL_SPEC16` = `0x0202` (R81);
    - `probe_timeout_s: float` — default `PYRO_PROBE_TIMEOUT` = `0.5` (R84);
    - `probe_attempts: int` — default `3` (R84);
    - `jtag_load_timeout_s: float` — default `PYRO_JTAG_LOAD_TIMEOUT` = `600.0` (R84);
    - `vivado_dir: str` — default `PINNED_VIVADO_DIR` (R70a-pin.2) for locating the
      pinned `hw_server`/`vivado` used by `load_partial`;
    - `recover_cmd: Optional[tuple[str, ...]]` — the R85a recovery command run by
      `load_partial` after a successful program step (v2.5.1); default the spec
      constant `PYRO_RECOVER_CMD` (`sudo -n` + the repo's
      `scripts/pyro_wedge_recover.sh`, resolved relative to the installed package —
      a pinned location, not filesystem scanning, R70); `None` disables recovery.

    Only `iface`/`hw_server` derive from env knobs; the rest are spec constants a test
    MAY override on the config. **Spec-sanctioned private test seams:** because R67's
    `pyro.testing` scope covers only model/synth fault injection, the device paths are
    made hardware-free-testable via **three private, defaulted `DeviceConfig` seam
    fields** the **test-developer MAY use normatively**:
    - `cap_check: Callable[[], bool]` — overrides the `CAP_NET_RAW` detection
      (exercises the no-privilege `(False, reason)` path, R86.4); returns `True` iff
      the caller holds `CAP_NET_RAW`;
    - `transport_factory: Callable[[DeviceConfig], Transport]` — supplies a frame
      transport implementing the **R86.7 `Transport` protocol** (exercises timeout /
      `SPEC16`-mismatch / valid-`ID_REPLY` paths without a NIC);
    - `load_runner: Callable[..., tuple[int, str]]` — supplies a JTAG runner
      implementing the **R86.7 `load_runner` contract** (exercises `PyroLoadError` /
      timeout without `hw_server`).
    Each MUST **default to the real production implementation** so a `DeviceConfig()`
    with no overrides is byte-identical to production behavior (mirroring R63b's mock
    seam and R70a's mock-preserving defaults). These seams need **no**
    `PYRO_ENABLE_TEST_HOOKS` gate (they are explicit config injection, not ambient
    process-wide hooks, so there is no production foot-gun); passing a non-default
    value is the deliberate act. They are the specced way to drive the
    no-privilege / timeout / `SPEC16`-mismatch / load-failure paths of AC-2b-1..2b-3
    without hardware.
  - **R86.7 (seam callable protocols — normative, v2.2.3).** The seam callables of
    R86.6 have the following **normative contracts** (converged behaviorally by coder
    and test-developer; spelled out here so future work needs no rediscovery):
    - **`Transport` protocol** — the object `transport_factory` returns. It carries
      **full Ethernet frames including the 14-byte L2 header** (`dst/src/0x88B5`), so
      it is the layer that prepends/strips the L2 header around the R86.2/R86.3 PYRO
      portion. Methods:
      - `send(frame: bytes) -> None` — transmit one complete Ethernet frame
        (caller supplies the full frame; the transport does not add or remove L2
        header bytes). Zero-padding to the 60-byte minimum (R78.9) is the transport's
        responsibility.
      - `recv(timeout: float) -> bytes | None` — return the next received complete
        Ethernet frame, or **`None` if no frame arrives within `timeout`** seconds
        (the sentinel `None` means *no-frame-within-timeout*, distinct from an empty
        frame). A `None` at each of `probe_attempts` attempts drives the
        `probe: no valid ID_REPLY` disposition (R84/R86.4).
      - `close() -> None` — release the transport; idempotent.
    - **`load_runner` contract** — a callable that runs the JTAG programming step and
      returns **`(rc: int, output: str)`**, where `rc` is the process exit code and
      `output` the combined tool log. **A nonzero `rc` MUST map to `PyroLoadError`**
      (with `output` in the diagnostic message); `rc == 0` is success. A run that
      exceeds `jtag_load_timeout_s` (R84/R86.5) is a timeout → the process tree is
      killed and `PyroLoadError` is raised (it need not surface as an `rc`). This
      keeps `load_partial`'s failure mapping (R86.5) identical whether the real or a
      fake runner is used.

- **R87 (Phase-2b slot map + multi-slot deferral — normative, v2.2.4).** The `slot`
  field (R78.3) has a fixed Phase-2b layout:
  - **slot `0` — the default ID stub** (R80): answers `ID_REQUEST`, replies
    `STATUS`/`ERROR` `PYRO_E_NOT_RESIDENT` to any `MATCH_REQUEST` (R78.8/R78.10(e));
  - **slot `1` — THE single-tenant pattern slot.** Because the PR region is
    single-tenant (R64, `pr_partitions == 1`), exactly **one** pattern circuit is
    resident at a time and it occupies **slot 1**. Pattern children default to
    `SLOT = 1`; `MATCH_REQUEST`s for a resident pattern target slot 1, and the
    resident child's `ID_REPLY` reports its `rp_child_id` (R78.5a) at slot 1.

  **Multi-slot layout is deferred (own ruling, like R76/R85).** A layout with more
  than one concurrently-resident pattern slot (`slot ≥ 2`) requires a shell advertising
  `pr_partitions > 1` (R42/R64) and is **out of scope for Phase 2b** (consistent with
  §12's "multiple concurrent resident circuits" exclusion). Until this deferral is
  lifted under a future version bump, `slot` values other than `0`/`1` are undefined
  and a device MUST reply `STATUS`/`ERROR` `PYRO_E_NOT_RESIDENT` to a `MATCH_REQUEST`
  for any `slot ≥ 2`.

- **R88 (PR-vs-OOC job request mechanism — normative, v2.2.4).** A synthesis job
  requests a genuine partial bitstream via the **`ToolchainConfig.pr_bitstream: bool`**
  field (**default `False`**, construction-pinned per R70b; additive with
  mock-preserving default, R70a). This blesses the implementation:
  - `pr_bitstream == False` → the OOC path (R72 `ooc_metrics`/`mock_stub`), unchanged
    Phase-0/1/2 behavior.
  - `pr_bitstream == True` → the PR link flow (R82): implement `pyro_rp` in-context
    against `static_dcp`, run `pr_verify` against `reference_dcp`, and — only on a
    passing `pr_verify` (R82c) — emit `payload_kind == "pr_bitstream"` with
    `pr_verified == true` (R47b).
  - **Fail-loud, never a silent downgrade (normative).** If `pr_bitstream == True` but
    a required substrate (`static_dcp`/`reference_dcp`, R82d) is **absent or
    unresolvable**, the job MUST raise `SynthesisFailed` → permanent-fallback (R65).
    It MUST **NOT** silently fall back to `ooc_metrics`: an operator who asked for a
    device bitstream and cannot get one must see a failure (honesty, R72c), not a
    quietly non-loadable artifact. On this host, where the substrate DCPs do not yet
    exist, a `pr_bitstream` request is therefore `SynthesisFailed` and every on-device
    clause continues to SKIP (R71/R83), consistent with `pr_flow_present == false`.

- **R89 (`pr_bitstream` manifest `shell_version` → PR-region identity — forward
  ruling, v2.2.4).** A `pr_bitstream` manifest currently records
  `shell_version == SHELL_VERSION` (`0x0A000001`, the model-harness value, R45/R75).
  This is a **placeholder**: once a real static shell / PR-region is targeted and
  validated (`pr_flow_present == true`, R82d), the `pr_bitstream` manifest's
  `shell_version` MUST become the **real PR-region identity** (derived from the flashed
  static image, relatable to `static_shell_id`, R81), so the R47b load-time
  shell/PR-region compatibility check (R47b/R47c) binds against the actual region.
  Setting that real value is **deferred** to the version bump that flips
  `pr_flow_present` true (nothing consumes it yet on this host); until then
  `SHELL_VERSION` stays `0x0A000001` per R75 and this ruling reserves the obligation so
  the placeholder is not mistaken for the final contract. This does **not** change
  `SHELL_VERSION` now (frozen-invariant respected).

---

## 11. Prerequisites, risks, and open items

- **P1 (toolchain — Phase 2 critical path).** The v2.0.0 design makes per-pattern
  synthesis the core of the hardware path, so the Vivado toolchain and the
  OpenNIC `open-nic-shell` **partial-reconfiguration** build flow are now the
  **critical path for Phase 2** (not an optional add-on). Required: Vivado
  matching the OpenNIC shell version; a floorplanned PR partition for the 250 MHz
  user box with a fixed static/reconfigurable interface (`pblock` + the harness
  contract, §7.4); and a PR bitstream generation flow. **Status at Phase-2 start
  (R71):** the Vivado half is now **present** — Vivado **2025.2**
  (`/usr/local/cad/2025.2/Vivado`, the R70a-pin re-pinned from 2023.1 in v2.2.1)
  synthesizes, places, and routes the target part
  `xcu250-figd2104-2L-e` with no license error — but the OpenNIC **PR floorplan and
  PR-bitstream flow remain absent**, so there is no loadable PYRO artifact, and the
  runtime user has no PYRO-usable transport (no `/dev/qdma*`, no `CAP_NET_RAW`); the
  one-time full-image reprogram to a PR-enabled shell needs root PCIe-rescan
  cooperation (JTAG access to the owner's own board is available and is not a
  blocker). P1 is therefore **partially satisfied**: real
  OOC synthesis is LIVE (R70–R75), while PR-bitstream and on-device clauses SKIP.
  **Phase 0 and Phase 1 MUST proceed entirely without any of it** via the software
  model and mock toolchain (R7/R63b); Phase 2 clauses are gated by the R71 live/SKIP
  matrix (a SKIP names the absent prerequisite; a PASS comes only from real
  execution).
- **P2 (transport enablement).** The performance-target QDMA char-dev binding
  requires the QDMA PF/queue setup and `/dev/qdma*` (or equivalent) char devices,
  which do not currently exist (F5). Until then, the **raw-Ethernet-frame
  binding** to the `onic` netdev (F3/R68) is the functional transport; its control-frame
  format is **specified in §10.1 (R78)** (v2.2.0, R76 lifted, R50) and it requires
  `CAP_NET_RAW` for the `AF_PACKET` path (the gate on `device_usable`, R83).
- **P3 (privilege).** MMIO/DMA and raw-frame transport typically require root or
  specific capabilities. The runtime MUST detect insufficient privilege and fall
  back to the model/CPU with a clear diagnostic rather than crashing (R52).
- **P4 (PCIe budget).** Round-trip DMA latency is µs-scale; the loss-regime
  thresholds (R3) exist precisely because of this. Bandwidth over PCIe Gen3 x16
  (~16 GB/s theoretical) bounds R1's throughput targets; if measured bandwidth
  cannot meet R1 on real hardware, the spec version MUST be bumped with revised
  numbers rather than the AC silently downgraded.
- **P5 (OpenNIC user-box constraints).** The 250 MHz user box (F4) and QDMA
  interface impose width/latency constraints on the RTL engine; the automaton
  state/pattern limits (R11–R13) will be tuned to fit and re-advertised via the
  caps registers (R42/R45), not hard-coded in software.
- **P6 (Unicode fidelity).** Full Unicode case folding and property escapes are
  intentionally out of the HW subset (R15); risk is that a large fraction of
  real-world `str` patterns fall back. Mitigation: measure fallback rate in the
  benchmark suite and treat a high rate as a compiler-coverage backlog item, not
  a correctness defect.
- **P7 (Python runtime version).** PYRO requires **CPython ≥ 3.11** (R26): the
  L2 classifier depends on the `re._parser`/`re._constants` modules introduced
  under those names in 3.11. The target host runs CPython 3.12. Importing under
  an older interpreter is unsupported and PYRO MAY refuse to import with a clear
  error.
- **P8 (synthesis latency).** Vivado synth + P&R for a single circuit is
  **minutes**, dwarfing any match. This is the reason synthesis is async and
  gated by the launch policy (R4a) and the win-regime economics restated in R2.
  Risk: patterns that are hot but short-lived may never pay back synthesis;
  mitigation is the N_synth threshold, `prewarm` for known-hot patterns, and the
  persistent bitstream cache (R4) so cost is paid once per key across restarts.
- **P9 (PR-region capacity).** The dynamic region is a fixed-size PR partition;
  a generated circuit must fit its LUT/FF/BRAM/DSP budget and meet 250 MHz timing
  (R11/R47b). Complex patterns may not fit → permanent fallback (R12/R65). Risk:
  the fit rate of real-world patterns is unknown; mitigation is the resource
  estimator (reject early) and measuring fallback-due-to-capacity in benchmarks.
- **P10 (single-tenant region arbitration).** With one PR partition, only one
  pattern's circuit is resident at a time; the eviction policy (R64) can thrash
  under many competing hot patterns, and PR reload (~O(100 ms)) is far costlier
  than a scan. Risk mitigated by LRU eviction, the residency check preferring the
  resident circuit, and (future) multi-partition shells (`pr_partitions > 1`).
- **P11 (Vivado licensing / version pinning).** Synthesis requires valid Vivado
  licenses and a **pinned** toolchain version; `toolchain_version` and
  `shell/PR-region_version` are part of the bitstream-cache key (R4) and manifest
  (R47b) precisely because artifacts are not portable across tool/shell versions.
  Risk: license contention limits synthesis concurrency (R63c); a toolchain
  upgrade invalidates the cache (cold rebuilds). Mitigation: pin versions, key
  the cache on them, and stage upgrades.

---

## 12. Out of scope

- Regex *replacement-template* execution on the FPGA (`sub` rewrites happen on
  the host; only the match scan is offloaded).
- Backreferences, lookaround, conditionals, atomic/possessive groups, recursion
  (permanently fallback, §5.2).
- Full Unicode case folding, `\p{...}` Unicode property classes, and locale-
  dependent matching on the hardware path (fallback).
- Accelerating non-`re` engines (`regex` third-party module, `hyperscan`
  bindings) — PYRO targets the stdlib `re` API surface only.
- Multi-tenant/QoS scheduling of the FPGA across processes (single-process
  serialization only in this version). Cross-process sharing of the PR region or
  of the synthesis service among unrelated applications is out of scope; the
  bitstream cache MAY be shared read-only but arbitration is single-process.
- **Multiple concurrent resident circuits.** This version assumes a single-tenant
  PR region (one resident circuit at a time, R64). Exploiting a shell with
  `pr_partitions > 1` to hold several circuits simultaneously is a future
  extension, not a v2.0.0 deliverable.
- **Full-chip (non-PR) bitstream rebuilds** and generating/altering the OpenNIC
  static shell itself. PYRO only produces **partial** bitstreams for the
  pre-defined dynamic-region partition; building the shell/static region is a
  prerequisite (P1), not a PYRO function.
- **Speculative/whole-corpus synthesis.** PYRO does not synthesize circuits for
  patterns that are neither hot (R4a) nor `prewarm`ed; there is no attempt to
  predict or pre-synthesize arbitrary future patterns.
- **A frozen/public `pyro_generate` descriptor format.** The serialized-automaton
  descriptor passed across the C ABI (R40/R40a) is an **L2↔L3 private contract,
  intentionally not frozen or documented for external construction** (freezing it
  would couple the ABI to the generator internals). Consequently, pure-ABI
  conformance drivers verify device-free resident-tier guarantees behaviorally
  through the Python surface (R69), not by hand-building descriptors. Freezing the
  format, or shipping a public test-vector artifact, is explicitly **not** a
  v2.0.x deliverable.
- **Nominal type identity of match/pattern objects on the accelerated path.**
  `isinstance(obj, re.Pattern)` / `isinstance(obj, re.Match)` are not guaranteed
  on the hardware/model path because those CPython types are concrete and
  non-subclassable; "drop-in" means duck-typed compatibility only (§2, R27, R28,
  R36a). Callers requiring nominal identity must use the fallback path
  (`PYRO_DISABLE=1`). This is a permanent limitation, not a roadmap item.

---

## 13. Change control

Any ambiguity surfaced by the coder or test-developer MUST be resolved by
amending this spec and bumping its version (SemVer: MAJOR for interface/AC
breaks, MINOR for added requirements, PATCH for clarifications). Agents derive
their work from this file, not from each other. A failing differential test
against an HW-eligible pattern is a coder/engine defect; a test that contradicts
§5/§6 is a test-developer defect; a genuinely underspecified behavior is a spec
defect and returns here.

---

## 14. Changelog

All amendments are recorded here per §13. Versioning is SemVer: MAJOR for
interface/AC breaks, MINOR for added requirements, PATCH for clarifications.

- **2.5.1** (2026-07-15) — *R85a: JTAG PR wedge root-caused, in-band recovery
  folded into `load_partial` (MINOR — added requirement), spec-writer.* HW-proven
  same day: recovery restored a JTAG-loaded pattern child to responding and
  produced the **first on-silicon R45a counter read** (`CYCLES=17 BYTES=15` for a
  15-byte scan). No interface/AC break; frozen invariants untouched.
  - **R85a (NEW).** The wedge mechanism (no decoupler + stuck QDMA C2H stream
    state) and the mandatory 4-step in-band recovery (driver detach → user-box
    reset BAR2 `0x014`.0 → QDMA soft reset BAR2 `0x00C`.0 → driver re-attach).
    Neither reset alone suffices (proven). Recovery failure is R84-transient.
  - **R85 (amended).** The "netdev undisturbed" half of the old claim corrected:
    the PCIe link claim stands; the netdev is transiently detached during R85a
    recovery and its MAC changes across a load.
  - **R86.5 (amended).** `load_partial` runs `config.recover_cmd` (new R86.6
    field, default `PYRO_RECOVER_CMD` = `sudo -n scripts/pyro_wedge_recover.sh`)
    through the `load_runner` seam after a successful program step; nonzero rc →
    `PyroLoadError`; `recover_cmd=None` disables; still no probe inside the load.
  - **AC-2-5/AC-3-3 (clarified).** The R1 hardware-throughput clauses require
    the P2 performance transport in addition to `device_usable`; over the
    functional raw-Ethernet control transport they measure first, then SKIP
    naming the missing P2 prerequisite and the measured control-plane rate.
- **2.5.0** (2026-07-15) — *Phase-3 slate: R3b measurement protocol, R67 seam
  additions, R73a PR timing-gate scope, F2/F3 demoted to configuration (MINOR —
  added requirements), spec-writer.* No interface/AC break, no renumbering; frozen
  invariants untouched (C ABI 2.0.0, `PYROART1`, `SHELL_VERSION 0x0A000001`,
  `PYRO_SHELL_SPEC16 0x0202`, wire harness namespace `0x00010000`, existing AC
  numbers).
  - **R3b (amended; constant UNCHANGED).** The 1.15× bound is retained and its
    rationale vindicated by measurement (native floor ≈1.01×). Adds R3b.1
    (median of ≥ 5 trials — the single-trial check was flaky against a 4-point
    margin), R3b.2 (subject pinned at 132 bytes — the same router measures 1.31×
    at 3 B and 1.02× at 1 KiB, so an unpinned bound is meaningless), R3b.3 (binds
    against the compiled-extension build; a pure-Python install records an honest
    R3c SKIP/FAIL, never a silent pass). R3c/AC-2-5/AC-3-3 cite the protocol.
  - **R67 (extended).** Adds `await_synthesis()` (terminal-tier observation point)
    and `set_strict_residency()` (suspends R51b's device-free precedence so R51
    step 5 binds strictly). Both under the existing `PYRO_ENABLE_TEST_HOOKS` gate;
    `reset()` clears strict residency. Without them **AC-3-2 is unobservable** on a
    device-free host. New sub-clause **R51b-strict**.
  - **R73a (NEW).** Scopes R73's post-route timing gate, for a `pr_bitstream` link,
    to the **reconfigurable module's** paths (`pyro_rp` cell + R80 boundary paths,
    `axis_aclk` domain) rather than the whole design. Without it every partial build
    fails on the static's permanent −0.427 ns `cmac_usplus` violation — outside
    `pyro_rp`, in a datapath PYRO ties off — **after** `pr_verify` has passed, so the
    PR flow can never succeed. Mitigation is normative: the static is built and
    validated once, its timing recorded at flash time, and it is provably unchanged
    by any partial (R82b locked static + R82c mandatory `pr_verify`). The known
    violation is recorded in-spec (R73a.5), and the whole-design WNS is still logged
    (R73a.6).
  - **F2/F3 (demoted from normative facts to per-host configuration).** Both were
    **false**: the card is a single PF at `0000:02:00.0` (`10ee:903f`; the shell is
    built `pf=cmac=1`), and the netdev is `ens2`. F3 was wrong in **shape**, not just
    value — a netdev name is not derivable from the BDF by any rule, so it cannot be
    a spec fact. `PYRO_DEVICE_IFACE` (R68) loses its `enp175s0f0` default and becomes
    **no-default, fail-closed**; R83's canonical string gains
    `transport: PYRO_DEVICE_IFACE not configured`. §0/R39/§10.1/P2 stop naming
    `af:00.0`/`enp175s0f0`.
- **2.4.0** (2026-07-10) — *R78.11 in-band PERF counter read-out (MINOR — added
  requirement), spec-writer.* Closes the R45a **observability gap on hardware**:
  the v2.3.0 counters exist in the R45 CSR block, but AXI-Lite is tied off at
  the R80 boundary and no R78.4 kind read them, so on the real device they were
  unreachable (observable only in the model and xsim) and R59/AC-3-3 could not
  attribute on-chip scan cost. Per R79 this is a **partial-bitstream-only**
  change — the flashed static shell (boot-verified 2026-07-10) and the R80
  boundary are untouched; no reflash. No interface/AC break, no renumbering;
  frozen invariants untouched (C ABI 2.0.0, `PYROART1`, `SHELL_VERSION
  0x0A000001`, `PYRO_SHELL_SPEC16 0x0202`, wire harness namespace `0x00010000`
  (R78.5b), existing AC numbers).
  - **R78.4 (additive).** New kinds `0x06` `PERF_REQUEST` / `0x07` `PERF_REPLY`;
    explicit note that pre-definition devices drop unknown kinds (the
    forward-compatibility behavior R78.11 relies on).
  - **R78.11 (NEW).** Empty-payload request; 16-byte big-endian
    `cycles(8)|bytes(8)` reply; `STATUS`/`ERROR` `PYRO_E_NOT_RESIDENT` for a
    non-resident slot; served only between scans ⇒ coherent post-DONE values,
    non-destructive read; pre-2.4.0 children (incl. the flashed ID stub) drop
    the request and the host MUST treat the timeout as counters-unavailable,
    never a device fault.
  - **R78.10 (additive).** Normative vectors **(f)** `PERF_REQUEST` /
    **(g)** `PERF_REPLY`; AC-2b-1 now spans (a)–(g).
  - **R86.2.** Sendable kinds now exactly `0x01`–`0x07`.
  - **Engine RTL fixes (2.2.0, found by the R78.11 xsim gate).** Three latent
    generator bugs, invisible until the counters became wire-observable:
    (1) `CTRL.START` was level-triggered and never cleared, so the engine
    **restarted itself** the cycle after `DONE`, wiping both R45a counters and
    reducing `DONE` to a one-cycle pulse — R45a's hold-after-DONE was
    unimplementable; START now fires on the bit's **rising edge** only.
    (2) A window's exclusive `end` was reported **+1 too large** (accepts are
    harvested from the closure of the registered state, one cycle after the
    accepting byte, when `byte_index` already equals the correct `end`).
    (3) The **final byte's accept was dropped** (`running` cleared at
    `in_last` before the accept became visible) — a missed-match correctness
    bug on the device path; a one-cycle `finishing` harvest now catches it and
    then latches `DONE`. Wire-proven by the pattern-child xsim tb (ID, MATCH,
    PERF pre/post, non-resident PERF) against host-codec-generated vectors.
  - **Version rollover.** `HARNESS_VERSION`/`GENERATOR_VERSION` → `0x00020200`
    (2.2.0), mirrored in `src/pyro_rt.c`: identity hashes (R47a), cache keys
    (R4), and the R47b manifest harness check roll over, so pre-2.2.0 artifacts
    (including any 2.1.0 rebuilds) are stale and re-synthesize on demand.
    The R74 estimator is untouched: the PERF responder lives in the RP-child
    **wrapper** (§10.2), which is outside the estimator's engine
    (`pyro_circuit`) scope; the engine's emitted RTL changes only its
    `HARNESS_VER` localparam value (no resource change).
- **2.3.0** (2026-07-08) — *R45a on-chip performance counters (MINOR — added
  requirement), spec-writer.* Closes the benchmark-attribution gap: the R45 CSR
  block had no on-chip cycle/byte counters, so R59/AC-3-3 could only measure
  end-to-end throughput and could not separate on-chip scan cost from
  PCIe/DMA/framing/dispatch overhead. No interface/AC break, no renumbering;
  frozen invariants untouched (C ABI 2.0.0, `PYROART1` container format,
  `SHELL_VERSION 0x0A000001`, `PYRO_SHELL_SPEC16 0x0202`, wire harness
  namespace `0x00010000` (R78.5b), R78 wire protocol, existing AC numbers).
  - **R45 table (additive).** Four new normative RO offsets: `CYCLES_LO/HI`
    (`0x0058`/`0x005C`), `BYTES_LO/HI` (`0x0060`/`0x0064`).
  - **R45a (NEW).** Counter semantics: cleared on `CTRL.START`/`CTRL.RESET`,
    stable after `STATUS.DONE`, halves MAY tear while BUSY (sample after DONE);
    the model (R7) reports idealized values — only hardware values are R1/R59
    evidence; harness < 2.1.0 circuits read 0 at these offsets, so
    `CYCLES == 0` after a completed non-empty scan means "counters
    unavailable", never a measurement.
  - **Version rollover.** `HARNESS_VERSION`/`GENERATOR_VERSION` → `0x00020100`
    (2.1.0), mirrored in `src/pyro_rt.c`. Identity hashes (R47a), descriptor
    cache keys (R4), and the R47b manifest harness check roll over: pre-2.1.0
    cached artifacts (including this host's built `ab+c` pattern partial) are
    stale and re-synthesize on demand. The R83a `pr_flow_present` evidence
    predicate is unaffected (it checks parse/`payload_kind`/`pr_verified`, not
    harness equality). Estimator intercepts bumped (+64 LUTs / +64 FFs) to keep
    R74's `real <= est` conservative over the recorded corpus data pending the
    R74a 2025.2 recalibration.
- **2.2.5** (2026-07-06) — *`pr_flow_present` evidence predicate (PATCH — one
  clarification + one additive knob/field), spec-writer.* Closes a real gap the
  test-developer flagged: R83 flipped `pr_flow_present` true when the R82d artifacts
  were "present and validated," but the decisive validation — a passing `pr_verify`
  (R82c) — is **Vivado-only**, so with no host-observable evidence the availability
  probe stayed conservatively `false` **forever**, even on a host where the full PR
  flow demonstrably works (this host now has the complete R82d artifact set and the
  first real `pr_bitstream` job saves its manifest alongside the partial `.bit`). No
  interface/AC break, no renumbering, frozen invariants untouched (C ABI 2.0.0,
  `PYROART1`, `SHELL_VERSION 0x0A000001`, `PYRO_SHELL_SPEC16 0x0202`, existing AC
  numbers).
  - **R68 knob `PYRO_PR_EVIDENCE_MANIFEST` (NEW, additive).** No-default,
    fail-closed/no-scanning path to a `pyro.synth.Manifest` JSON sidecar the operator
    asserts THIS host's PR flow produced against the currently-configured substrate
    DCPs. Populates `ToolchainConfig.pr_evidence_manifest` (Optional, default `None`,
    mock-preserving per R70a). Unlike the substrate DCP knobs it gates the availability
    **report**, not a job, so its absence keeps `pr_flow_present` false (R83a), **not**
    a `SynthesisFailed` (R88). Amended R82d to name it as the host-observable proxy for
    the fourth R82d conjunct (a flow with a passing `pr_verify`).
  - **R83a (NEW sub-clause — the `pr_flow_present` evidence predicate).** Spells out
    the exact host-observable predicate the probe MUST implement: `pr_flow_present ==
    true` iff (1) `static_dcp` configured+readable, (2) `reference_dcp`
    configured+readable, (3) `pr_evidence_manifest` configured+readable, (4) it parses
    via the R47b-consistency-enforcing `Manifest.from_json`, and (5) it carries
    `payload_kind == "pr_bitstream"` **and** `pr_verified == true` — evaluated in that
    fixed order, the first failure naming the canonical reason. Fail-closed, never
    raises, no env reads/scanning. States the **honesty rationale** (an operator
    asserting an un-fabricable, boundary-validated manifest is the same Kant-universal
    act as configuring the DCP paths) and the **staleness caveat** as an advisory
    **SHOULD** (regenerate after reflashing the static shell) — deliberately not a MUST
    because staleness is not yet machine-detectable (manifest `shell_version` is still
    the R89 placeholder); the SHOULD is promoted to an enforced MUST under the R89 bump
    that makes `shell_version` the real PR-region identity.
  - **R83 wording.** Updated R83's first bullet and its combined-enumeration append to
    reference the R83a canonical reason `pr_flow_present=false — <first unmet R83a
    condition>`, superseding the placeholder `<first missing R82d artifact>` literal.
- **2.2.4** (2026-07-06) — *`pr_bitstream`-mode dispositions (PATCH — clarifications +
  additive config fields/knobs), spec-writer.* Six gaps from the PR-flow
  implementation round (RP-child wrapper generator + `VivadoToolchain` PR flow); no
  interface/AC break, no renumbering, frozen invariants untouched (C ABI 2.0.0,
  `PYROART1`, `SHELL_VERSION 0x0A000001`, existing AC numbers).
  - **R78.5a (item 1 — `rp_child_id` derivation, blessed).** A loaded pattern circuit's
    `rp_child_id` is the **low 32 bits of the R47a pattern hash, forced non-zero**,
    overridable; deterministic, collision-improbable, host-verifiable from the same
    hash. Default ID stub reports `0`.
  - **R87 (item 2 — Phase-2b slot map + multi-slot deferral).** Fixed slot `0` = ID
    stub, **slot `1` = THE single-tenant pattern slot** (R64); `slot ≥ 2` (multi-slot,
    needs `pr_partitions > 1`) is **deferred** with this ruling number; a device replies
    `PYRO_E_NOT_RESIDENT` to `slot ≥ 2`. Updated R78.3.
  - **R78.5b (item 3 — `harness_version` namespace reconciliation).** The wire
    `harness_version` field carries the **R45 resident-harness version** (`0x00010000`),
    a **distinct namespace** from the **`PYROART1` artifact `HARNESS_VERSION`**
    (`0x00020000`). Pattern children report the wire/R45 value; the two MUST NOT be
    unified. Neither value changed.
  - **R88 (item 4 — PR-vs-OOC job request, blessed).** `ToolchainConfig.pr_bitstream:
    bool = False` (construction-pinned, R70b) requests the PR flow. **`pr_bitstream ==
    True` with an absent substrate DCP is `SynthesisFailed` (R65), never a silent
    `ooc_metrics` downgrade** (honesty). On this host such a request fails and device
    clauses keep SKIPping (`pr_flow_present == false`).
  - **R68 knobs + R82d (item 5 — substrate locations, registered).** Registered
    **`PYRO_PR_STATIC_DCP`** (→ `ToolchainConfig.static_dcp`) and
    **`PYRO_PR_REFERENCE_DCP`** (→ `reference_dcp`), **no default, fail-loud** (R88), so
    the residency service reaches the locked static / reference routed DCPs from
    sampled knobs without code; harnesses MAY also pass the config fields directly.
    Amended R82d to name them.
  - **R84 wording (item 6 — two named timeout fields).** Replaced the ambiguous
    "single adapter-selected per-job-timeout" with **two named `ToolchainConfig`
    fields**: `job_timeout_s` (1800, OOC/R77) and `pr_job_timeout_s` (3600, PR); the
    adapter picks by `pr_bitstream`. Recorded the new additive fields in R70a.
  - **R89 (awareness item — `pr_bitstream` `shell_version` → PR-region identity,
    forward ruling).** The current `pr_bitstream` manifest `shell_version ==
    SHELL_VERSION` is a **placeholder**; when a real PR-region is targeted
    (`pr_flow_present == true`) it MUST become the real PR-region identity (relatable to
    `static_shell_id`, R81) so the R47b compatibility check binds. Deferred to that
    bump; `SHELL_VERSION` unchanged now (R75).
- **2.2.3** (2026-07-06) — *v2.2.2-round dispositions (PATCH — clarifications + one
  normative test vector), spec-writer.* Three notes from the `pyro.device`
  implementation round; no interface/AC break, no renumbering, frozen invariants
  untouched. Context: the RP ID-stub RTL now exists and passed xsim byte-exact
  against R78.10(a)/(b) under 2025.2; the AC-2b host suite is committed (74 passed / 2
  skipped).
  - **R86.7 (test-dev note 1 — seam callable protocols).** Spelled out the R86.6 seam
    contracts normatively: the **`Transport` protocol** (`send(frame)` /
    `recv(timeout) -> bytes | None` with `None` = *no-frame-within-timeout* /
    `close()`; frames are **full Ethernet frames including the 14-byte L2 header**),
    and the **`load_runner`** contract (returns `(rc, output)`; **nonzero `rc` maps to
    `PyroLoadError`**; timeout kills the tree and raises). Fixed `load_runner`'s type
    from `-> None` to `-> (int, str)` in R86.6.
  - **R47b-consistency (test-dev note 2 — chose defense-in-depth).** Ruled that the
    `pr_verified == (payload_kind == "pr_bitstream")` invariant (R82c) MUST be enforced
    at the boundary: the `pyro.synth.Manifest` constructor **and** `from_json` MUST
    **reject** inconsistent manifests (raise a defined error), not merely rely on the
    producer. Back-compat safe (all pre-v2.2.2 manifests satisfy it); consumers still
    entitled to reject on load. Updated R47b/R82c.
  - **R78.10(e) (coder-A/RTL note 3 — STATUS/ERROR wire vector).** Added the missing
    normative test vector: the `STATUS`/`ERROR` `PYRO_E_NOT_RESIDENT` (code 7) reply
    the default RP child (R80) produces to the R78.10(c) `MATCH_REQUEST` — byte-derived
    by MAC-swap, `slot`/`seq` echo, `kind = 0x05`, `length = 4`, payload
    `00 00 00 07`. Wired into R78.8 and AC-2b-1; picked up by both the RP-child xsim
    testbench and `test_ac2b1_codec.py`.
- **2.2.2** (2026-07-06) — *`pyro.device` dispositions (PATCH — clarifications +
  small additive fields), spec-writer.* Resolves five coder notes and three
  test-developer notes against v2.2.1's R86; no interface/AC break, no renumbering,
  frozen invariants untouched (C ABI 2.0.0, `PYROART1` FORMAT_VERSION 1,
  `SHELL_VERSION 0x0A000001` semantics, all existing AC numbers). Additive manifest
  field only (`pr_verified`, back-compat JSON round-trip like `payload_kind`).
  - **R70a-pin.2 (coder note 1 — confirmed).** The 2025.2 pin is a documented module
    constant `PINNED_VIVADO_DIR`, **not** a `ToolchainConfig.vivado_dir` default:
    `vivado_dir` keeps default `None` (R70 forbids library-side `PYRO_VIVADO`
    resolution; R70a requires `ToolchainConfig()` ≡ mock). `PINNED_VIVADO_DIR` is the
    R86.5 JTAG-loader resolution source. Confirms the coder's reading.
  - **R86.6 (coder note 2 + test-dev note B — `DeviceConfig` + private seams).** Named
    the public frozen `pyro.device.DeviceConfig` (fields all defaulted from spec
    constants; config-in, no env reads). Blessed three **spec-sanctioned private
    `DeviceConfig` seam fields** — `cap_check` / `transport_factory` / `load_runner`
    — that default to production impls and that the test-developer MAY use normatively
    to drive the no-privilege / timeout / `SPEC16`-mismatch / load-failure paths
    without hardware; no `PYRO_ENABLE_TEST_HOOKS` gate needed (explicit config
    injection, not ambient hooks).
  - **R68 device knobs (coder note 3).** Registered `PYRO_DEVICE_IFACE` (default
    `enp175s0f0`, F3) and `PYRO_HW_SERVER` (default `TCP:localhost:3121`) as the
    **only** env-sampled device inputs (R35a). Ruled `PYRO_SHELL_SPEC16`, the R84
    timeouts, and R78 frame constants **spec-fixed** (config-overridable for tests,
    not env knobs). Reconciled R86's blanket sampling note accordingly.
  - **R84 `PYRO_JTAG_LOAD_TIMEOUT = 600 s` (coder note 4).** New normative constant
    for the R85 `program_hw_devices` step (distinct from the synthesis-job timeouts);
    R77 kill discipline on expiry → `PyroLoadError`; a load timeout is transient
    (`PYRO_E_NOT_RESIDENT`/fallback), not permanent (R65). Wired into R86.5/R86.6.
  - **R86.2 (coder note 5 + test-dev note).** Confirmed `0x00` is `reserved` and **not
    a sendable message kind**: `encode_frame` sendable kinds are exactly `0x01`–`0x05`
    (R78.4); `0x00` and any undefined value raise `PyroFrameError`.
  - **R86.4 (test-dev note A).** Replaced the elided reason example with the exact,
    **non-elided** two-condition canonical literal (converged with R83's current-host
    string), so the literal is normative.
  - **R47b/R82c `pr_verified: bool` (test-dev note C).** Added an additive manifest
    field (default `false`, JSON round-trip preserved) that is `true` **iff**
    `pr_verify` passed, consistent with `payload_kind` (`true` exactly for
    `pr_bitstream`; `false`/absent for `mock_stub`/`ooc_metrics`) — an independent,
    host-observable seam closing the AC-2b-3 `pr_verify` observability gap. Updated
    AC-2b-2/AC-2b-3.
- **2.2.1** (2026-07-06) — *Vivado re-pin to 2025.2 + `pyro.device` API surface
  (MINOR — added requirement + toolchain re-pin), spec-writer.* Two items; no frozen
  invariant touched (C ABI 2.0.0, `PYROART1` FORMAT_VERSION 1, `SHELL_VERSION
  0x0A000001` semantics, all existing AC numbers unchanged).
  - **Toolchain re-pin 2023.1 → 2025.2 (empirical, orchestrator-verified 2026-07-06).**
    Vivado **2023.1 segfaults at batch-process exit** on this host after the OS
    upgrade to Ubuntu 24.04 / glibc 2.39 (unsupported by 2023.1; `libtinfo.so.5` and
    TERM/locale shims do not help). The nonzero exit codes of successfully-completed
    `launch_runs` children mark completed runs **FAILED** (this killed the first
    PR-shell build *after* CMAC synthesis had actually succeeded) — a violation of the
    exit-code-integrity assumption underlying R77's process discipline. **Vivado
    2025.2** at **`/usr/local/cad/2025.2/Vivado`** exits cleanly with no shim,
    officially supports the host OS, and is license-clean for `xcu250-figd2104-2L-e`
    (the permanent `cmac_usplus` license, good through 2027.06, covers it). Amended:
    **R70a-pin** (new normative sub-clause — pinned release 2025.2 at that path,
    superseding 2023.1); **R75** pinned `toolchain_version = 0x19020000`
    (`YY=25=0x19`, `RR=2`, `build=0`), 2023.1's `0x17010000` retained only as a
    historical example; **R75a** and **R82a** updated to the new value/release; swept
    the R70/R71-toolchain_present/§10.2-intro/§11-P1 references. Historical changelog
    entries (2.1.0, 2.2.0) retain original wording as record.
  - **R74a (calibration is toolchain-bound — transitional honesty ruling).** Post-route
    utilization depends on the Vivado release, so `ESTIMATOR_CALIBRATION_MARGIN` is
    calibrated **per `toolchain_version`**. Any AC-2-4 calibration data gathered under
    2023.1 (`0x17010000`) is **not evidence** for the 2025.2 pin (`0x19020000`) and
    MUST NOT be reused to claim PASS; AC-2-4 MAY PASS only from real syntheses under
    the pinned 2025.2 toolchain and otherwise records the R74 empty-success-set SKIP.
    The R4/R75a cache key enforces this mechanically (distinct keys).
  - **R86 (`pyro.device` host-side API surface — NEW obligation).** Public,
    PYRO-specific module so coder and test-developer derive the device-plumbing
    imports from the spec independently: `encode_frame(kind, slot, seq, payload,
    flags=0) -> bytes` and `decode_frame(bytes) -> result` (fields named for the R78
    header; `PyroFrameError` on bad `magic`/`version`/`flags`/`length`);
    `probe_device(config) -> (usable, reason)` implementing R83/R84 with the canonical
    reason string and the **privilege-free `(False, reason)`** guarantee (never raises
    `PermissionError` when `CAP_NET_RAW` is absent); `load_partial(config, path) ->
    None` implementing the R85 JTAG load (raises `PyroLoadError` on failure). Exception
    taxonomy `PyroDeviceError`/`PyroFrameError`/`PyroLoadError` (R86.1). All four are
    **config-in with no ad-hoc `os.environ` reads** (R5/R35a discipline). Wired into
    AC-2b-1/2b-2/2b-3 and the Phase-2b deliverables. **New implementation obligations:**
    the R70a-pin re-pin, R86 (`pyro.device`); R74a is a transitional honesty ruling.
- **2.2.0** (2026-07-06) — *Phase-2b on-hardware bring-up (MINOR — added
  requirements), spec-writer.* Defines everything R71 recorded as absent so
  device bring-up can proceed on the owner's own U250 (reflashed with a PYRO-built
  OpenNIC PR shell: `open-nic-shell` @ `ce85c8d`, Vivado 2023.1, `pyro_rp`
  DFX/`HD.RECONFIGURABLE` partition; JTAG programming verified). **No frozen
  invariant is touched:** C ABI 2.0.0 (`0x00020000`), the `PYROART1` artifact header
  (FORMAT_VERSION 1), `SHELL_VERSION 0x0A000001` semantics, and all existing AC
  numbering are unchanged; every on-device clause still records **SKIP** (never
  PASS) until the live probe answers. Added §10.1 (filled) and §10.2:
  - **§10.1 filled; R76 LIFTED.** The Phase-2b deferral of the raw-Ethernet
    control-frame format (R76) is lifted; the normative format is **R78**.
  - **R78 (control-frame format — NEW obligation).** In-band Ethernet protocol,
    EtherType **`0x88B5`**, fixed 14-byte big-endian PYRO header (`magic`/`version`/
    `kind`/`flags`/`slot`/`seq`/`length`), kinds `ID_REQUEST`/`ID_REPLY`/
    `MATCH_REQUEST`/`MATCH_REPLY`/`STATUS`-`ERROR`, `MATCH_REPLY` entries embedding
    the 24-byte little-endian `pyro_match` (R47), MTU ≤ 1518 (payload ≤ 1486),
    60-byte-minimum zero-padding, no PYRO-level fragmentation. Includes **normative
    hex test vectors** for `ID_REQUEST` and a full `MATCH` round-trip (tests derive
    from these, AC-2b-1).
  - **R79 (parsing lives inside `pyro_rp`).** The frame parser/responder is in the
    reconfigurable partition, not the static shell, so a protocol change is a
    **partial-bitstream change only** and never reflashes the static image.
  - **R80 (RP boundary — thin & permanent).** Frozen boundary: `clk`/`rstn`, one
    512-bit AXIS slave (H2C in), one 512-bit AXIS master (C2H out) with `tkeep`/
    `tlast`/16-bit `tuser` size·src·dst; AXI-Lite tied off. Default child = an **ID
    stub** answering `ID_REQUEST` (so a freshly flashed board answers the probe) and
    `STATUS`/`ERROR` `PYRO_E_NOT_RESIDENT` to `MATCH_REQUEST`.
  - **R81 (`static_shell_id`).** 32-bit `(SPEC16<<16)|BUILD16` build identity in
    `ID_REPLY`; probe verifies `SPEC16 == PYRO_SHELL_SPEC16` (`0x0202`) before
    declaring the device usable. Distinct from `SHELL_VERSION` (unchanged this bump).
  - **R82 (PR link flow).** Static shell and partial bitstreams MUST use the same
    Vivado release (2023.1); the **locked static DCP** is the in-context linking
    substrate; **`pr_verify` is MANDATORY** before any manifest may claim
    `payload_kind == "pr_bitstream"` (R72). Defines the `pr_flow_present` artifacts.
  - **R83 (predicate-flip semantics; canonical SKIP string).** `pr_flow_present`
    flips true on validated R82d artifacts; `device_usable` flips true only on a live
    `ID_REPLY` with matching `static_shell_id` **and** `CAP_NET_RAW`. New canonical
    SKIP string enumerates exactly the unmet conditions, **superseding** the fixed
    v2.1.3 literal. Amended R71 and the AC-2-2/AC-2-6 SKIP references.
  - **R84 (timeouts).** `PYRO_PROBE_TIMEOUT = 500 ms` × 3 attempts; match round-trip
    timeout → `PYRO_E_TIMEOUT`/fallback. **`VIVADO_PR_JOB_TIMEOUT = 3600 s`** for
    `pr_bitstream` jobs (full P&R link + `pr_verify`), distinct from R77's 1800 s OOC
    default; R77's kill-the-process-tree discipline applies to PR jobs verbatim.
  - **R85 (JTAG PR-load; ICAP/MCAP deferred).** Phase-2b on-device
    `pyro_circuit_load` is out-of-band **JTAG via `hw_server`**; the static shell owns
    PCIe (bit-identical across configs) so PR of `pyro_rp` does not disturb the link.
    ICAP/MCAP self-reconfiguration is **explicitly deferred** (its own ruling).
  - **New ACs (do not renumber existing):** Phase-2b **AC-2b-1** (host frame codec vs.
    R78 test vectors — **LIVE**, no device), **AC-2b-2** (live probe / `device_usable`
    flip — SKIP until it answers), **AC-2b-3** (`pr_bitstream` via locked static +
    `pr_verify`, JTAG load — SKIP until `pr_flow_present ∧ device_usable`). Updated
    R50/P2 to cite R78. **New implementation obligations:** R78–R85 and AC-2b-1.
- **2.1.3** (2026-07-06) — *Board-ownership & device-blocker rationale correction
  (PATCH — factual clarification), spec-writer.* The project owner clarified that the
  Alveo U250 at PCI `af:00.0` is the **owner's own board** and that reprogramming it
  is permitted; the earlier "third party's live NIC that MUST NOT be perturbed"
  rationale for `device_usable == false` was factually wrong and is removed. **No
  semantic change:** `device_usable` remains **false**, and every R71 predicate
  definition, live/SKIP matrix disposition, AC, and requirement is unchanged. The
  prohibition on perturbing the device is **lifted in principle** — device bring-up
  (Phase-2b) is now an available path gated on technical prerequisites, not on
  permission. Rewrote the rationale everywhere it appeared (the R71 `device_usable`
  predicate definition, the R71 SKIP example, the Phase-2 intro, the AC-2-2/AC-2-6
  SKIP reason strings, and §11 P1) to the **actual technical blockers**: (a) no
  loadable PYRO PR artifact — the flashed shell is not PR-capable and no PR
  floorplan/flow exists (`pr_flow_present`, a separate predicate, false); (b) no
  PYRO-usable transport for the runtime user — no `/dev/qdma*` char devices and the
  raw-Ethernet binding needs `CAP_NET_RAW`, which the user lacks; (c) the one-time
  full-image reprogram to a PR-enabled shell disturbs the PCIe link and needs root
  cooperation for driver unbind / PCIe rescan (or a reboot). **JTAG programmability
  was verified empirically on 2026-07-06** (FT4232H USB-JTAG bridge attached,
  `hw_server` connects, chain enumerates `xcu250_0`) — ownership,
  permission-to-perturb, and JTAG access are all **non-blockers**. Canonical SKIP
  reason wording set to `device_usable=false — no loadable PR artifact
  (pr_flow_present=false), no PYRO transport (no /dev/qdma*, no CAP_NET_RAW), full
  reprogram needs root PCIe-rescan cooperation`. Historical changelog entries (e.g.
  2.1.0) retain their original wording as record.
- **2.1.2** (2026-07-06) — *Toolchain-selection pinning (PATCH — clarification),
  spec-writer.* Surfaced by code review + coder analysis; no interface/AC break, no
  new implementation obligation. Added **R70b**: a residency/synthesis manager
  **pins its `ToolchainConfig` at construction**, so a mid-process re-sample of
  `PYRO_TOOLCHAIN`/`PYRO_VIVADO` takes effect only in a manager built after the
  sampling point (fresh process or explicit reset), not by live re-push. Rationale
  (consistent with R35b deferred-refresh): the toolchain governs a spawned
  subprocess, so tearing a manager down mid-flight would SIGTERM an in-flight
  Vivado worker and orphan its process tree, bypassing the R77 in-worker kill —
  strictly worse than deferred pickup.
- **2.1.1** (2026-07-06) — *Phase-2 test-author adjudications (PATCH —
  clarifications), spec-writer.* Three ambiguities surfaced by the test-developer
  against v2.1.0; no interface/AC break, no new implementation obligation.
  - **R3c (R3b native-hot-path precondition is the gate).** The R3b relative 1.15×
    loss-regime bound binds **only** when the §8 routing/decision hot path is itself
    native-cheap; the mere existence of an L3 native runtime does not satisfy it
    while the routing decision is Python-level. Until then **R3a (absolute ≤ 2 µs)
    governs** and any R3b check records a **SKIP-with-measured-ratio**, never a FAIL
    (consistent with AC-0-6). Pinned R3b's hard-PASS binding to **AC-3-3** (native
    routing path is a Phase-3 deliverable). Reworded **AC-2-5** (dropped "in all
    cases": absolute bounds always asserted; relative bound SKIPs under R3c) and
    **AC-3-3** (R3b becomes hard here).
  - **R74 empty success set.** Added one sentence: if no corpus pattern synthesizes
    successfully, the AC-2-4 calibration clause records a **SKIP** (vacuously
    satisfied), **never a FAIL**.
  - **R64a (device-free residency bookkeeping REQUIRED).** On a device-free host the
    software model MUST still exercise single-tenant residency and fire the
    deterministic LRU eviction (`circuits_resident`/`circuits_evicted` per R66), so
    the model-side clauses of **AC-1-5/AC-2-6** are firm, non-vacuous LIVE
    assertions; only the physical PR-load mechanism/timing is absent (SKIP, R71).
    Referenced from AC-2-6.
- **2.1.0** (2026-07-06) — *Phase-2 real-Vivado rulings (MINOR — added
  requirements), spec-writer.* Makes the Phase-2 ACs executable on a host where P1
  is only **partially** satisfied (Vivado present; no OpenNIC PR flow; the physical
  U250 is a third party's live NIC that MUST NOT be perturbed). No interface/AC
  break: the C ABI 2.0.0 and the `PYROART1` artifact header (FORMAT_VERSION 1) are
  untouched; the mock toolchain stays the default so Phase-0/1 behavior is
  byte-identical when unset. Added §7.6 (real-toolchain adapter contract) and §10.1:
  - **R70/R70a (toolchain-selection knobs).** `PYRO_TOOLCHAIN=mock|vivado` (default
    `mock`) and `PYRO_VIVADO=<install dir>` (no default, no library-side scanning),
    sampled at the R35a points and registered in R68; the selection crosses the
    worker boundary via an **additively** extended `ToolchainConfig` (kind, vivado
    dir, part, target clock, per-job timeout) with mock-preserving defaults.
  - **R71 (partial-P1 live/SKIP matrix).** Three probed predicates
    (`toolchain_present`, `pr_flow_present`, `device_usable`) with a normative
    per-clause matrix. Real OOC synth + honest metrics + cache warm-reload + AC-2-4
    calibration are **LIVE**; every PR-bitstream / on-device clause **SKIPs** with a
    reason naming the absent prerequisite. SKIP is never PASS; PASS only from real
    execution.
  - **R72/R72a–c (payload honesty).** The `vivado` payload stays the model-exec
    `PYROART1` container (R51b), and honesty is carried by a new **manifest**
    `payload_kind` field (`mock_stub` default | `ooc_metrics` | reserved
    `pr_bitstream`) — a JSON-sidecar addition with a default that preserves the
    round-trip and leaves the ABI-frozen artifact header untouched. `ooc_metrics`
    manifests MUST record genuine post-route metrics; the loader treats only
    `pr_bitstream` as an on-device-loadable bitstream.
  - **R73 (AC-2-1 timing proxy).** A 250 MHz (4.000 ns) OOC clock constraint;
    `met_timing := (post-route WNS ≥ 0)`, `fmax_mhz := 1000/(4.000 − WNS_ns)`.
  - **R74 (AC-2-4 pre-registered margin).** Normative `ESTIMATOR_CALIBRATION_MARGIN`
    fixed **before** calibration data exists: per successfully-synthesized pattern,
    `real_luts ≤ est_luts`, `real_ffs ≤ est_ffs`, `est_luts ≤ 10·max(1,real_luts)`,
    `est_ffs ≤ 10·max(1,real_ffs)`.
  - **R75/R75a (`toolchain_version` encoding).** `vivado` reports
    `(YY<<24)|(RR<<16)|build` (2023.1 ⇒ `0x17010000`), distinct from the mock's
    `0x00000100`; `SHELL_VERSION` stays the model-harness value until a real PR flow
    exists. Cache-key separation between mock and vivado artifacts falls out of the
    existing R4 key.
  - **R76 (§10.1 deferral).** The R50/P2 "control-frame format (Phase 1 §10.1)"
    dangling reference is dispositioned: the frame format is **deferred to
    Phase-2b/3 hardware enablement** (safe because all device clauses SKIP);
    §10.1 stub added and R50/P2 references repaired.
  - **R77 (`vivado` per-job timeout).** The adapter MUST enforce its own subprocess
    timeout and **kill the Vivado process tree** on expiry → `SynthesisFailed`/R65
    (default 30 min, overridable via `ToolchainConfig`); the R63e reaper is a
    bookkeeping backstop; R63 asynchrony unchanged.
  - Amended AC-2-1..AC-2-6, the Phase-2 intro, and §11 P1 to bind the R71 matrix.
    **New implementation obligations:** R70/R70a, R72a (`payload_kind`), R73, R74,
    R75, R77; R71/R76 are dispositions/rulings; R76 repairs a dangling reference.
- **2.0.5** (2026-07-05) — *Testability rulings (PATCH), Phase 1 acceptance-suite
  author.* Several ACs were only verifiable by reading internals, contradicting
  §9's "tests MUST NOT read the implementation" premise. Added **§9.1 public
  test/verification seams** and wired them in:
  - **R67 (fault-injection seam — NEW obligation).** `pyro.testing` namespace with
    `inject_device_error` (R52/R61), `inject_synth_failure` (R65),
    `inject_false_positive` (R19), and `reset`; deterministic, result-preserving,
    stats-observable, gated behind `PYRO_ENABLE_TEST_HOOKS=1`. Unblocks AC-1-6 and
    the R52/R61/R65 verification.
  - **R68 (spec-named config knobs).** `PYRO_N_SYNTH` (**NEW obligation**) pins the
    R4a launch threshold; `PYRO_CACHE_DIR` (**blessing** of existing behavior)
    names the persistent bitstream-cache location (R4). Both sampled at R35a
    points, never on the hot path.
  - **R69 (device-free ABI-conformance scope — blessing).** Ruled option (a): the
    `pyro_generate` descriptor is an L2-private, intentionally-unfrozen format
    (added §12 out-of-scope bullet), so resident-tier register guarantees for the
    device-free binding MAY be verified **behaviorally via the Python surface**
    (AC-1-3/1-4/1-7); pure-ctypes tests cover all publicly constructible paths.
    Did **not** freeze the descriptor (option b) or require a test-vector artifact
    (option c).
  - Strengthened R63e (service MUST clean up worker processes on teardown, so
    tests can assert no residue). Updated AC-1-1/1-2 (R69), AC-1-5 (R67/R68),
    AC-1-6 (R67), R52/R61/R65 (seam references), and the §9 premise. **New
    implementation obligations:** R67 and the `PYRO_N_SYNTH` knob of R68; all
    else blesses/clarifies existing behavior.
- **2.0.4** (2026-07-05) — *Rulings (PATCH), Task 7 (C ABI 2.0.0 native runtime).*
  No interface/AC break; blesses the native runtime. (1) **R40a (classification is
  an L2 responsibility).** Pattern eligibility/classification is owned by Python
  L2 and performed before the C ABI; `pyro_generate` is a handle-creation step
  whose `pattern` argument is an already-eligible **descriptor**, not raw regex.
  The device-free/`model://` binding therefore does not classify and does not
  return `PYRO_E_UNSUPPORTED`/`PYRO_E_CAPACITY` (those stay in L2); updated R40's
  code comment accordingly. Correctness is preserved — ineligible patterns never
  reach `pyro_generate`. (2) **R49a (alignment check as a defined error).**
  R49's "assert" is satisfied — and preferably realized — by returning
  `PYRO_E_INVALID` with R44 state guarantees rather than `abort()`/`assert()`;
  the defined-error form is the normative, production-safe contract (debug builds
  MAY additionally assert). (3) **R47c (refusal status) + R37 ABI-enum
  stability.** A refused `pyro_circuit_load` (shell/PR incompatibility, integrity
  failure, or R47a identity mismatch) MUST return **`PYRO_E_NOT_RESIDENT`** —
  blessed as distinct from `PYRO_E_SYNTH` (transient refusal vs. permanent
  synthesis failure); both are routing, not device errors. Codified that the ABI
  enums are **additive-only** within a MAJOR from ABI 2.0.0 onward (R37).
- **2.0.3** (2026-07-05) — *Ratification (PATCH), Task 6 review.* No interface/AC
  break; blesses the implemented router. Added **R51b (device-free precedence of
  R7)** and a mirroring sentence in R7: when **no physical device is present**,
  R7 takes precedence over R51 step 5 — the software model MAY serve any
  HW-eligible, gate-crossed, non-permanent-fallback dispatch at any tier (counted
  as `model`), with residency/tier state still tracked and reported (R4/R66) and
  results byte-identical across tiers (R36/R53). On hardware (Phase 2+), step 5
  binds strictly (not resident → fallback while synthesis proceeds in
  background). Reconciles R51 step 5 with the Phase-0/R7 invariant that
  `stats()["model"] > 0` for eligible gate-crossing calls on a device-free host
  (AC-1-7).
- **2.0.2** (2026-07-05) — *Clarification (PATCH), Task 5 fix round.* No
  interface/AC break. Added **R4b (canonicalization scope)**: the R4/R47a
  canonicalization (stripped leading global inline flags + effective flags +
  encoding tag) applies wherever **circuit reuse** is decided — the host
  classification/descriptor cache and the R47a/R47b circuit-identity and
  bitstream keys — but the **L1 compiled-`Pattern` object cache MAY key on raw
  `(pattern, flags)`** to preserve `Pattern.pattern` fidelity (R27: stock `re`
  reports different `.pattern` for `(?i)abc` vs `("abc", re.I)`). Two raw-distinct
  patterns may therefore share one generated circuit while remaining two distinct
  `Pattern` objects.
- **2.0.1** (2026-07-05) — *Clarifications (PATCH), Phase 1a implementation.* No
  interface/AC break. (1) **R47a identity hash inputs** made explicit: the hash
  now enumerates the **encoding tag** (`PYRO_ENC_BYTES` vs `PYRO_ENC_UTF8`) —
  required because the same pattern text generates a different automaton in bytes
  vs. str/UTF-8 mode (R14) — and **effective (canonicalized) flags** after
  inline-flag extraction, not the caller's raw flags; added a normative flag-
  canonicalization rule and required the host to canonicalize before lookup. The
  same two inputs were mirrored into both R4 cache keys, the R47b manifest key,
  and the §2 bitstream-cache definition (closing the identical gap there).
  (2) **R19 over-approximation** codified: added R19a sanctioning deliberate
  over-approximation (recognizing a superset of true match starts, emitting
  re-verified false positives) as a legitimate generator strategy for constructs
  expensive to encode exactly in RTL — **not a defect** provided completeness
  holds (no false negatives, ever) and every window is re-verified to keep
  results byte-identical; R19b bounds the false-positive rate so it does not
  destroy the win regime; R19c requires the manifest (R47b) to declare the
  over-approximation classes + estimated FP rate; and R59(d) adds an
  over-approximation benchmark corpus to attribute re-verification cost.
- **2.0.0** (2026-07-05) — *Architecture inversion (MAJOR), project-owner
  decision.* Owner intent, verbatim: "each new regex compilation creates a new
  circuit/block for the FPGA dynamic region." The programmable/loadable-program
  engines (pre-2.0.0 Phase-1 Aho-Corasick-as-blob and Phase-2 programmable NFA)
  are **dropped as hardware deliverables** and replaced by **per-pattern
  synthesized circuits** loaded into the OpenNIC PR region. Changes:
  - **§0/§2/§4:** central design rewritten to per-pattern generated circuit +
    async background synthesis + PR hot-swap; new definitions (generated circuit,
    harness contract, PR region, resident circuit, synthesis service, bitstream
    cache, cold/warm/resident tiers); architecture layers L2 (regex→RTL
    generator), L3 (bitstream cache + PR loader + synth client), L5 (per-pattern
    circuit), and a side synthesis service (SS).
  - **R1/R2:** win-regime economics restated — hardware dispatch requires a
    **resident** circuit; amortization now includes minutes-long synthesis;
    added R2a/R2b.
  - **R4 rewrite + R4a:** explicit host-classification cache (µs, unchanged) vs.
    persistent bitstream cache with cold/warm/resident tiers; new synthesis-launch
    policy (N_synth or `prewarm`).
  - **R11–R13:** capacity reframed as PR-region resource budget (LUT/FF/BRAM/DSP)
    + complexity bounds; estimator rejects over-budget patterns; real-synthesis
    non-fit → permanent fallback.
  - **§7.3 (ABI 2.0.0):** `pyro_prog`→`pyro_circuit`; `pyro_compile`→
    `pyro_generate` + async `pyro_synth_request`/`pyro_circuit_status`/
    `pyro_circuit_load`; new `PYRO_E_NOT_RESIDENT`/`PYRO_E_SYNTH` and
    `pyro_circ_status`; caps now advertise PR-region budget and generator/harness
    versions. ABI major bumped to 2.0.0 (AC-0-8's 1.0.0 stub check is a Phase-0
    historical checkpoint, left unchanged per the "don't break AC-0-*" constraint;
    see R37 note).
  - **§7.4:** `PROG_*` registers and the R46 "PROG" blob format **removed**;
    added the fixed **harness contract**, the **circuit-identity** register block
    (R47a, trust boundary — circuit proves identity, host still re-verifies
    windows R19), and the **PR bitstream artifact + manifest** contract (R47b:
    pattern hash, caps, utilization/timing, shell/PR-region compatibility,
    integrity hash).
  - **§7.5 (new):** synthesis service (out-of-process, queue, dedup, mock
    toolchain, single-tenant concurrency, isolation — R63), `pyro.prewarm` (R62),
    PR-region eviction policy (R64), synthesis-failure = permanent fallback,
    never an exception (R65), and `pyro.re.stats()` lifecycle counters (R66).
  - **R36/R53:** transparency invariant and determinism now explicitly cover
    **asynchrony** — results never depend on the circuit tier; only timing/stats
    do.
  - **§8 R51:** decision order gains a residency check + background synthesis
    launch; **§9** R57/R58 updated and R58a added (synthesis-service & tier
    equivalence tests); **§10** rephased — Phase 1 = HDL generator + circuit model
    + synthesis-service skeleton (mock toolchain) + PR-artifact cache + transport
    contract (all hardware-free); Phase 2 = real Vivado flow + on-hardware
    bring-up; Phase 3 = interposition + benchmarks. **§11** P1 rewritten (Vivado
    + open-nic-shell PR flow = Phase 2 critical path), added P8–P11 (synthesis
    latency, PR capacity, single-tenant arbitration, Vivado licensing/version
    pinning); **§12** updated.
  - **Correctness architecture untouched:** byte-identical-or-fallback (R16),
    hybrid group extraction (R17–R20), routing gates (R51/R51a), device-error
    fallback (R52), env sampling (R35a–d) all preserved. **Phase 0 (AC-0-1..0-8)
    is untouched and remains green on `phase0-pyro`** — its "fallback until
    hardware exists" behavior is now also the steady-state cold-start behavior.
- **1.2.1** (2026-07-05) — *Clarifications (PATCH).* Surfaced by the final
  whole-branch review; no AC/interface breaks. (1) Added R14a: in str mode a
  subject or pattern that is not strictly UTF-8-encodable (unpaired surrogates,
  e.g. `surrogateescape`-decoded log data) is fallback-only — stock `re` matches
  it but UTF-8 encoding would raise; this is plain fallback routing, not a device
  error and not `fallback_after_error`. (2) Added R51a enumerating the Phase-0
  safety gates applied under R16's fall-back-when-in-doubt umbrella: (a) subject
  not exactly `str`/`bytes` (bytearray/memoryview → fallback, for group(0)
  return-type fidelity), (b) non-default `pos`/`endpos` → fallback, (c) the
  anchor-context/full-span safety gate for hybrid reconstruction, (d) the
  surrogate gate from R14a; framed as Phase-0 conditions later phases may lift
  individually. (3) Pinned the supported runtime to **CPython ≥ 3.11** in R26
  (classifier uses `re._parser`/`re._constants`, 3.11+ only; target host runs
  3.12) and added prerequisite P7; updated AC-0-1/AC-0-2 references. (4) Bumped
  the header Date to 2026-07-05.
- **1.2.0** (2026-07-05) — *Requirement change (MINOR).* Surfaced by the Task 1
  fix round: on the target host a public `os.environ.get` costs ~0.86 µs, so
  reading both `PYRO_DISABLE` and `PYRO_FORCE_MODEL` per call (~1.74 µs, measured
  total 2.79 µs) cannot meet R3a/R5's ≤ 2 µs budget, and the previous private-
  dict workaround was removed at review as fragile. Amended R35 (added R35a–R35d)
  so env flags are sampled into cached internal flags only at deterministic
  points — package import, `pyro.install()`/`uninstall()`, and a new explicit
  `pyro.refresh_env()` API — and the per-call decision (R51 step 1/4) consults
  only the cached flags. Documented the deferred mid-process refresh semantics
  (mirrors `re.purge()`/locale caching) and preserved the incident-mitigation
  guarantee (`PYRO_DISABLE=1` at any sampling point forces fallback thereafter).
  R3a/R5 numeric bounds unchanged (2 µs); the amendment makes them achievable.
  Updated AC-0-5 and AC-0-6 accordingly.
- **1.1.1** (2026-07-05) — *Clarification (PATCH).* Surfaced by the Task 1 code
  review: CPython's `re.Pattern`/`re.Match` are concrete, non-subclassable,
  non-ABC C types, so an accelerated wrapper cannot satisfy
  `isinstance(obj, re.Pattern)` / `isinstance(obj, re.Match)`. Defined "drop-in"
  in §2 as duck-typed method/attribute compatibility (not nominal identity);
  amended R27 and R28 to reference it; added R36a carving `isinstance` checks
  against these concrete types out of the R36 transparency invariant as a
  documented, permanent limitation (explicitly not a fallback trigger, since the
  intent cannot be detected); added the corresponding §12 out-of-scope note. No
  interface or AC break.
- **1.1.0** (2026-07-04) — *Requirement change (MINOR).* Surfaced by the Phase-0
  coder: R3's 1.15× relative loss-regime bound is unachievable for the pure-
  Python shim, where stock `re.search` on a short subject is sub-microsecond C
  code. Split R3 into R3a (absolute ≤ 2 µs median added overhead, all phases,
  aligned with R5) and R3b (1.15× relative, scoped to Phase 1+ native runtime).
  Updated AC-0-6 to assert the absolute bound (R3a/R5) and deterministic routing
  (R51) rather than the relative ratio.
- **1.0.0** (2026-07-04) — Initial specification: feasibility thresholds,
  L0–L5 architecture, RE2-like supported subset, hybrid capture-group
  correctness model, Python API / C ABI / register+DMA interface contracts,
  routing/fallback logic, test strategy, four-phase delivery plan, prerequisites
  and risks.
