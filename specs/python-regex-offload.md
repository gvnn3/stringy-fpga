# Specification: Transparent Python Regex Offload to OpenNIC FPGA

- **Spec ID:** `python-regex-offload`
- **Version:** 1.2.1
- **Status:** Draft (approved for Phase 0 delegation)
- **Owner:** Spec Writer
- **Date:** 2026-07-05

---

## 0. Summary

This document specifies a system, hereafter **PYRO** (PYthon Regex Offload),
that transparently accelerates regular-expression matching in Python by
executing pattern matching on the FPGA installed in this host, with automatic,
correctness-preserving fallback to CPython's `re` module for any pattern or
input the hardware path cannot serve.

The target device is the Xilinx/AMD PCIe card at `af:00.0`/`af:00.1` running the
**OpenNIC** shell (QDMA-based) under the in-tree `onic` kernel driver. Custom
matching logic is placed in the OpenNIC **user plugin / dynamic (250 MHz user
box) region**. PYRO compiles a supported subset of regular expressions to a
**programmable finite-automaton** representation that is loaded into the fabric
at runtime as data (register/DMA configuration), so that adding or changing a
pattern does **not** require a bitstream rebuild in the common case. Partial
reconfiguration (PR) of the dynamic region is specified only as an optional
alternative for capacity overflow.

"Transparent" means a user can obtain acceleration either by changing an import
(`import pyro.re as re`) or by activating an interposition shim that patches the
standard `re` module in place, with **byte-identical results** to CPython `re`
for the supported subset and silent fallback otherwise.

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
- **F2.** FPGA card exposes two PCIe functions: `af:00.0` (`10ee:903f`) and
  `af:00.1` (`10ee:913f`), subsystem `10ee:0007`, PCI class `0280` ("Network
  controller").
- **F3.** Kernel driver in use is `onic` (AMD/Xilinx OpenNIC). The card runs the
  OpenNIC shell (QDMA transport). Two ports appear as network interfaces
  `enp175s0f0` and `enp175s0f1`.
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
- **Automaton program:** the data structure loaded into the fabric that
  configures the programmable engine to recognize a pattern set.
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

- **R1 (win regime — throughput).** For a fixed compiled pattern set applied to
  a streamed corpus of total size ≥ **1 MiB**, the hardware path SHALL sustain
  match throughput of at least **5 GiB/s** aggregate scan rate for the
  fixed-string engine (Phase 1) and at least **1 GiB/s** for the programmable
  NFA engine (Phase 2), measured end-to-end from host memory to result, once the
  pattern set is resident.
- **R2 (win regime — reuse amortization).** When a single compiled pattern is
  reused across at least **N_reuse = 32** search calls, or one search over a
  corpus of at least **S_min = 64 KiB**, the median end-to-end wall-clock time of
  the hardware path SHALL be ≤ the CPython `re` path for the same work on the
  reference benchmark set (§9).
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
    of calling CPython `re` directly (routing/decision overhead ≤ 15%).

  Rationale: the 1.15× relative bound is only physically meaningful once the
  decision path is native; expressing it as an absolute bound for Phase 0 (R3a)
  preserves the intent — negligible routing tax — without demanding a ratio that
  is unachievable for a Python wrapper around sub-microsecond C code.
- **R4 (compile amortization).** Compiling a pattern to an automaton program and
  loading it into the fabric (cold) is a one-time cost. PYRO SHALL cache compiled
  automaton programs keyed by `(pattern_bytes, flags, engine_version)` so that a
  repeated `compile()` of the same pattern is served from cache in ≤ **50 µs**
  (warm) on the host side.
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
| L1  Python API layer  (pyro.re: compile/match/search/...)   |  Phase 0
+-------------------------------------------------------------+
| L2  Pattern compiler (regex -> AST -> NFA/DFA -> program)   |  Phase 0/2
|     + supported-subset gate + fallback classifier           |
+-------------------------------------------------------------+
| L3  Host runtime library (C ABI) + Python bindings          |  Phase 1
|     - pattern/program cache, scheduling, result assembly     |
+-------------------------------------------------------------+
| L4  Transport binding (QDMA char-dev OR raw Ethernet frame) |  Phase 1
+-------------------------------------------------------------+
| L5  FPGA user-plugin regex engine (OpenNIC dynamic region)  |  Phase 1/2
|     - Phase 1: Aho-Corasick multi-fixed-string               |
|     - Phase 2: programmable NFA (Thompson / bit-parallel)    |
+-------------------------------------------------------------+
```

- **R6.** Each layer L0–L4 SHALL expose a stable interface (Python types for
  L0–L2, a C ABI for L3, a byte/register protocol for L4→L5) as specified in §7.
  A layer MUST be testable using a mock of the layer beneath it.
- **R7.** The system SHALL include a **software model** of the FPGA engine (a
  pure-software reference implementation of L5 exercising the exact same L3/L4
  contract) so that Phases 0–2 are testable without physical hardware. The
  software model MUST produce results identical to the specified hardware
  behavior for all supported patterns.
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

- **R11.** The engine SHALL advertise, at runtime, its capacity parameters
  (see the capability register block, §7.4): maximum number of automaton states
  `MAX_STATES`, maximum number of concurrent patterns `MAX_PATTERNS`, maximum
  literal/class alphabet width (bytes vs. Unicode), and maximum bounded-repeat
  expansion `MAX_REPEAT`.
- **R12.** The compiler SHALL reject (route to fallback) any pattern whose
  compiled program would exceed any advertised capacity limit. Bounded repeats
  are expanded prior to the state count check; a repeat that would expand beyond
  `MAX_REPEAT` states is fallback-only.
- **R13.** Default advertised minimums (software model and Phase-1 hardware)
  MUST be at least: `MAX_STATES ≥ 1024`, `MAX_PATTERNS ≥ 256`,
  `MAX_REPEAT ≥ 255`.

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
- **R19 (candidate soundness/completeness).** The FPGA engine, for HW-eligible
  patterns, MUST be **sound and complete for group 0**: it MUST report a
  candidate window covering every position where CPython would find a match, and
  MUST NOT omit any match. False positives are permitted **only** if PYRO
  re-verifies every reported window with CPython (or the software model) before
  returning it to the caller; unverified false positives are a defect. False
  negatives are never permitted for HW-eligible patterns.
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
  dict` returning `{"eligible": bool, "reason": str, "engine": "fpga"|"model"|
  "fallback", "states": int|None}` for testing and diagnostics. This is PYRO-
  specific and MUST NOT exist on the standard `re` namespace when interposing
  (§7.2).
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
  in results are defects.
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
  packed `MAJOR<<16 | MINOR<<8 | PATCH`. This spec defines ABI **1.0.0**. A
  caller MUST refuse a library whose MAJOR differs.
- **R38 (types).** The header `pyro_rt.h` MUST define:

  ```c
  typedef struct pyro_ctx  pyro_ctx;    /* opaque runtime context */
  typedef struct pyro_prog pyro_prog;   /* opaque compiled automaton program */

  typedef enum {
      PYRO_OK            = 0,
      PYRO_E_UNSUPPORTED = 1,  /* pattern not HW-eligible */
      PYRO_E_CAPACITY    = 2,  /* exceeds device limits    */
      PYRO_E_DEVICE      = 3,  /* transport/device error   */
      PYRO_E_INVALID     = 4,  /* bad argument             */
      PYRO_E_NOMEM       = 5,
      PYRO_E_TIMEOUT     = 6
  } pyro_status;

  typedef enum {
      PYRO_ENC_BYTES = 0,
      PYRO_ENC_UTF8  = 1
  } pyro_encoding;

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
  `transport_uri` selects the binding, e.g. `"model://"`, `"qdma://af:00.0/q0"`,
  or `"eth://enp175s0f0"`. On failure returns non-`PYRO_OK` and `*out == NULL`.
- **R40 (compile/load).**
  ```c
  pyro_status pyro_compile(pyro_ctx *ctx, const uint8_t *pattern, size_t len,
                           uint32_t flags, pyro_encoding enc, pyro_prog **out);
  void        pyro_prog_free(pyro_prog *p);
  pyro_status pyro_prog_load(pyro_ctx *ctx, pyro_prog *p);   /* resident on dev */
  ```
  `pyro_compile` returns `PYRO_E_UNSUPPORTED`/`PYRO_E_CAPACITY` for
  non-HW-eligible patterns; the caller then uses fallback. `flags` mirrors the
  Python flag bits (§7.1).
- **R41 (scan).**
  ```c
  pyro_status pyro_scan(pyro_ctx *ctx, pyro_prog *p,
                        const uint8_t *buf, size_t len,
                        uint64_t start_off,
                        pyro_match *out, size_t out_cap, size_t *out_count);
  ```
  Scans `buf[0..len)`; reports up to `out_cap` matches; sets `*out_count`. If
  more matches exist than `out_cap`, returns `PYRO_OK` with `*out_count ==
  out_cap` and the runtime MUST support resumption via `start_off` (streaming).
  Every returned `pyro_match` with `flags` bit0 clear MUST be treated as
  **unverified** by the caller (Python layer re-verifies, R19).
- **R42 (capability query).**
  ```c
  typedef struct {
      uint32_t max_states, max_patterns, max_repeat;
      uint32_t alphabet;      /* 256 for byte engine */
      uint32_t engine_version;
      uint32_t engine_kind;   /* 1=aho-corasick, 2=nfa, 0=model */
  } pyro_caps;
  pyro_status pyro_caps_get(pyro_ctx *ctx, pyro_caps *out);
  ```
- **R43 (ownership & lifetime).** Buffers passed to `pyro_scan` are borrowed for
  the duration of the call only. `pyro_prog` is owned by the caller until
  `pyro_prog_free`. `pyro_ctx` must outlive all `pyro_prog` created from it. The
  library MUST NOT retain pointers past the call that received them, except a
  loaded program's device-resident copy (managed until `pyro_prog_free` or ctx
  close). All functions MUST be thread-safe given distinct `pyro_ctx`; a single
  `pyro_ctx` MUST be internally synchronized (R32/R48).
- **R44 (no UB on error).** On any non-`PYRO_OK` return, all `out` pointers MUST
  be left in a defined state (`NULL` for handles, `*out_count == 0` for scan).

### 7.4 Hardware/software interface: register + DMA contract (L4↔L5)

This is the contract between the host transport (L4) and the FPGA engine (L5),
mapped into the OpenNIC user-plugin address space. It MUST be honored identically
by the software model (R7) so that the model and hardware are interchangeable.

- **R45 (register map).** The engine exposes a control/status register (CSR)
  block at a base offset `USER_BAR_BASE` within the user plugin's AXI-Lite
  window. All registers are 32-bit, little-endian. The following offsets are
  **normative**:

  | Offset  | Name          | Access | Meaning                                   |
  |---------|---------------|--------|-------------------------------------------|
  | 0x0000  | `ID`          | RO     | magic `0x5059524F` ("PYRO")               |
  | 0x0004  | `VERSION`     | RO     | engine_version (packed MAJ/MIN/PATCH)     |
  | 0x0008  | `CAPS0`       | RO     | `max_states` (low16) `max_patterns`(hi16) |
  | 0x000C  | `CAPS1`       | RO     | `max_repeat`(low16) `engine_kind`(hi16)   |
  | 0x0010  | `CTRL`        | RW     | bit0 START, bit1 RESET, bit2 STREAM       |
  | 0x0014  | `STATUS`      | RO     | bit0 BUSY, bit1 DONE, bit2 ERR, bit3 OVF  |
  | 0x0018  | `PROG_ADDR`   | RW     | DMA addr (low32) of automaton program     |
  | 0x001C  | `PROG_ADDR_H` | RW     | DMA addr (high32)                         |
  | 0x0020  | `PROG_LEN`    | RW     | program length in bytes                   |
  | 0x0024  | `IN_ADDR`     | RW     | DMA addr (low32) of input buffer          |
  | 0x0028  | `IN_ADDR_H`   | RW     | DMA addr (high32)                         |
  | 0x002C  | `IN_LEN`      | RW     | input length in bytes                     |
  | 0x0030  | `OUT_ADDR`    | RW     | DMA addr (low32) of result ring           |
  | 0x0034  | `OUT_ADDR_H`  | RW     | DMA addr (high32)                         |
  | 0x0038  | `OUT_CAP`     | RW     | result ring capacity in entries           |
  | 0x003C  | `OUT_COUNT`   | RO     | number of results produced                |
  | 0x0040  | `IRQ_ENABLE`  | RW     | bit0 DONE-irq enable                      |
  | 0x0044  | `IRQ_STATUS`  | RW1C   | bit0 DONE, write-1-to-clear               |

- **R46 (program blob format).** The automaton program is a self-describing
  binary blob transferred by DMA. It MUST begin with a 32-byte header:
  `magic(4)=0x50524F47 "PROG"`, `version(4)`, `engine_kind(4)`, `num_states(4)`,
  `num_patterns(4)`, `alphabet(4)`, `blob_len(4)`, `crc32(4)` (CRC-32 over the
  payload). The payload layout is engine-kind-specific and defined in the
  per-phase engine sections (§10). The engine MUST reject a blob whose `crc32`,
  `magic`, or `engine_kind` mismatch by setting `STATUS.ERR` and MUST NOT
  produce results.
- **R47 (result ring entry).** Each result entry is 24 bytes, little-endian:
  `start(8) end(8) pattern_id(4) flags(4)` — matching `pyro_match` (R38). The
  engine writes entries densely from ring base; on overflow (`OUT_COUNT ==
  OUT_CAP` with more matches pending) it MUST set `STATUS.OVF` and the host MUST
  resume via streaming (R41).
- **R48 (operation sequence).** A single scan is: (1) ensure program resident
  (`PROG_*` + program DMA, once per program), (2) write `IN_*`, `OUT_*`,
  `OUT_CAP`, (3) write `CTRL.START`, (4) wait `STATUS.DONE` (poll or IRQ),
  (5) read `OUT_COUNT`, DMA the result ring back. The host runtime MUST enforce
  that only one scan is in flight per engine instance at a time (single-issue),
  or use hardware queues if `engine_kind` advertises multi-queue; concurrency is
  otherwise serialized in L3 (R43).
- **R49 (endianness/alignment).** All DMA buffers MUST be 64-byte aligned. All
  multi-byte fields are little-endian. The input buffer needs no alignment beyond
  64-byte start. The model MUST assert these to catch host bugs.
- **R50 (transport-agnostic contract).** The register/DMA semantics above are
  identical regardless of whether L4 reaches the engine over the QDMA char-dev
  binding or the raw-Ethernet binding; only the mechanism of MMIO/DMA differs.
  In the Ethernet binding, CSR writes and buffer transfers are encapsulated in a
  defined control-frame format (specified at Phase 1 §10.1) but the register
  meanings are unchanged.

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
  5. If no device and no model available → fallback.
  6. Otherwise → hardware/model path.
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
  returned value relative to CPython.
- **R53 (determinism).** Given identical inputs and configuration, the
  *observable result* MUST be independent of whether the hardware, model, or
  fallback path served it. Only timing/stats may differ.

---

## 9. Test & verification strategy (hooks for test-developer)

These define categories of tests the test-developer derives from the ACs. Tests
MUST NOT read the implementation; they exercise the Python API (§7.1), the C ABI
(§7.3), and the register/DMA contract via the software model (§7.4).

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
- **R57 (ABI conformance).** Tests MUST drive the C ABI directly (via ctypes/
  cffi) against the software model: lifecycle (R39), compile/load (R40), scan
  including overflow/resume (R41), caps (R42), error states (R44), and thread
  safety (R43/R32).
- **R58 (register/DMA contract tests).** Against the software model implementing
  §7.4, tests MUST assert: `ID`/`VERSION`/`CAPS` values (R45), program-blob CRC/
  magic rejection (R46), result-ring entry layout and overflow `OVF` behavior
  (R47), operation sequence and single-issue enforcement (R48), alignment
  assertions (R49).
- **R59 (benchmark suite).** A benchmark harness MUST measure, for a defined
  corpus set, the metrics in R1–R5 and emit machine-readable results. The corpus
  set MUST include: (a) a ≥ 1 MiB log-file corpus with a reused pattern set of
  ≥ 32 patterns; (b) many-short-strings workload for the loss regime (R3);
  (c) a streaming corpus exceeding one `OUT_CAP` to exercise resumption. On a
  host without hardware, benchmarks run against the model and assert only the
  *routing* thresholds (R3–R5), skipping absolute-throughput assertions (R1/R2)
  with a recorded SKIP, never a PASS.
- **R60 (transparency regression).** A test MUST take a corpus of real-world
  Python snippets using stock `re`, run them under `pyro.install()` and under
  stock `re`, and assert identical outputs and exceptions (R33–R36).
- **R61 (fault injection).** Tests MUST simulate device errors/timeouts/false-
  positive windows in the model and assert the fallback-retry path (R52) yields
  CPython-identical results and increments the correct stats counters.

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

### Phase 1 — Fixed-string / multi-pattern (Aho-Corasick) engine

Deliver the C-ABI host runtime (L3), one transport binding (L4) — the model
binding is mandatory; the Ethernet binding is the first real target — and an
Aho-Corasick multi-fixed-string engine (`engine_kind=1`) as software model and
(optionally, if toolchain available) as RTL for the OpenNIC user plugin.
Supported patterns this phase: literal strings and alternations of literals
(`foo|bar|baz`), i.e., the regular sublanguage expressible by Aho-Corasick, plus
IGNORECASE over ASCII.

- **AC-1-1.** The C ABI (R39–R44) passes ABI-conformance tests against the model.
  (R57)
- **AC-1-2.** The register/DMA contract (R45–R50) is implemented by the model and
  passes contract tests, including CRC/magic rejection, result-ring layout,
  overflow `OVF`, single-issue, and alignment. (R58)
- **AC-1-3.** For multi-literal pattern sets over a ≥ 1 MiB corpus, `finditer`
  results are byte-identical to stock `re` with an equivalent
  alternation-of-literals pattern, including overlapping-match/leftmost
  semantics reconciled to CPython. (R16, R17, R19)
- **AC-1-4.** Overflow/resume: a corpus producing more matches than `OUT_CAP`
  returns the complete, correct match list via streaming resumption. (R41, R47)
- **AC-1-5.** `pyro_caps_get` reports `engine_kind=1` and limits ≥ R13 minimums;
  patterns exceeding `MAX_PATTERNS` route to fallback. (R11–R13, R42)
- **AC-1-6.** Fault-injection: model-reported false-positive windows are re-
  verified and never leak; device errors trigger fallback-retry with CPython-
  identical output. (R19, R52, R61)
- **AC-1-7 (hardware, conditional).** If the Vivado/OpenNIC toolchain (§11) is
  present, the RTL engine loaded into the user plugin passes AC-1-2/AC-1-3
  against the physical device over the selected transport. If absent, this AC is
  recorded SKIP (never PASS) and all others run against the model. (R7, F5)

### Phase 2 — Programmable NFA engine

Deliver a programmable finite-automaton engine (`engine_kind=2`) — a Thompson
NFA / bit-parallel (e.g., Glushkov + bit-parallel simulation) engine configured
by the automaton program blob, so new patterns load as data (R4), no bitstream
rebuild. Extends supported subset to full §5.1 (classes, quantifiers, bounded
repeats, anchors, groups-structure).

- **AC-2-1.** The compiler (L2) lowers every §5.1 construct to a valid program
  blob (R46) accepted by the engine; §5.2 constructs remain fallback-only. (R9,
  R10, R46)
- **AC-2-2.** For the full differential corpus (R54) and property-based fuzzing
  (R55) over the supported subset, `search/match/fullmatch/findall/finditer/sub/
  subn/split` are byte-identical to stock `re`. (R16, R17, R18)
- **AC-2-3.** Loading a new pattern into a resident engine requires only a
  program-blob DMA + `PROG_*`/`CTRL.START` sequence (no reconfiguration); warm
  compile-cache hit ≤ 50 µs. (R4, R48)
- **AC-2-4.** Bounded-repeat expansion respects `MAX_REPEAT`; over-limit patterns
  route to fallback. (R11–R13)
- **AC-2-5.** Throughput on the NFA engine meets R1's ≥ 1 GiB/s target on
  hardware, or records SKIP without hardware; routing thresholds (R3–R5) hold in
  all cases. (R1, R2, R3, R59)
- **AC-2-6 (PR alternative, optional).** If partial reconfiguration is used for a
  pattern class exceeding the programmable engine's capacity, PYRO MUST still
  return byte-identical results and MUST fall back rather than block if PR is
  unavailable. (R12, R52)

### Phase 3 — Transparent interposition + benchmarks

Deliver production-grade L0 interposition, the full benchmark suite (R59), and
the diagnostics surface, integrating Phases 1–2 engines with automatic engine
selection.

- **AC-3-1.** With `pyro.install()`, the transparency-regression suite (R60)
  passes on a corpus of real `re`-using programs: identical outputs and
  exceptions vs. stock `re`. (R36, R60)
- **AC-3-2.** Automatic engine selection picks Aho-Corasick for literal sets and
  the NFA engine otherwise, transparently; results remain byte-identical. (R51,
  R53)
- **AC-3-3.** The benchmark suite emits machine-readable metrics for R1–R5 and,
  on hardware, demonstrates the win regime (R1/R2) and the loss-regime routing
  (R3). Without hardware, R1/R2 are SKIP, R3–R5 PASS. (R1–R5, R59)
- **AC-3-4.** `pyro.re.stats()` reports counts of hardware / model / fallback /
  fallback-after-error dispatches; fault injection increments the error-fallback
  counter without altering results. (R52, R61)

---

## 11. Prerequisites, risks, and open items

- **P1 (toolchain).** Building the RTL engine for the OpenNIC user plugin
  requires Vivado (matching the OpenNIC shell's version) and the OpenNIC
  `open-nic-shell` build flow. Currently absent (F5). All software phases (0–2
  model path) MUST proceed without it; hardware ACs are conditional (AC-1-7,
  AC-2-5).
- **P2 (transport enablement).** The performance-target QDMA char-dev binding
  requires the QDMA PF/queue setup and `/dev/qdma*` (or equivalent) char devices,
  which do not currently exist (F5). Until then, the **raw-Ethernet-frame
  binding** to `enp175s0f0`/`f1` (F3) is the functional transport; it needs a
  defined control-frame format (Phase 1 §10.1, R50) and likely `CAP_NET_RAW`/root
  or an `AF_XDP`/`AF_PACKET` path.
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
  serialization only in this version).
- Automatic partial-reconfiguration bitstream generation (only listed as an
  optional alternative, AC-2-6).
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
