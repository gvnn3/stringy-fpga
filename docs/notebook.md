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

1. [EXPERIMENT 15 Jul 2026 03:44:16 Phase-3 Slate v2.5.0 + First Real HW Partial — R73a Gate Bug Caught by Its Own Safety Net, and JTAG PR Wedges the RP](#15-jul-2026-034416) :complete:
2. [EXPERIMENT 14 Jul 2026 20:26:10 Counter Wedge Fixed — a Circular-Import Corpse, and Why the Workaround Failed](#14-jul-2026-202610) :complete:
3. [EXPERIMENT 14 Jul 2026 18:35:59 AC-3-1 + AC-3-4 Land — Sabotage-Verified Suites, and a Counter-Wedge Bug Found](#14-jul-2026-183559) :complete:
4. [EXPERIMENT 14 Jul 2026 16:23:15 Native Routing Hot Path (R3c) — R3b Reachable at ~1.09×, Warmup Defect Found in the Recipe](#14-jul-2026-162315) :complete:
5. [EXPERIMENT 14 Jul 2026 08:49:52 PR Shell Rebuilt From Source on nf-server06 — New Card, New Flash, device_usable=true](#14-jul-2026-084952) :complete:
6. [EXPERIMENT  9 Jul 2026 10:59:06 U250 QSPI Flash — PYRO PR Shell User Image](#9-jul-2026-105906) :complete:
7. [EXPERIMENT  6 Jul 2026 14:05:00 PYRO Phase 2b — PR Shell + First pr_bitstream Partial](#6-jul-2026-140500) :complete:
8. [EXPERIMENT  6 Jul 2026 02:50:21 PYRO Phase 2 — Real Vivado Flow, Estimator Calibration](#6-jul-2026-025021) :complete:
9. [EXPERIMENT  5 Jul 2026 12:05:02 PYRO Phase 1 — Per-Pattern Circuits, Synthesis Service, C ABI](#5-jul-2026-120502) :complete:
10. [EXPERIMENT  5 Jul 2026 02:44:00 PYRO Phase 0 — Software Shim, Classifier, Model](#5-jul-2026-024400) :complete:
11. [EXPERIMENT  4 Jul 2026 07:33:45 FPGA Platform Discovery](#4-jul-2026-073345) :complete:

---

# EXPERIMENT 15 Jul 2026 03:44:16 Phase-3 Slate v2.5.0 + First Real HW Partial — R73a Gate Bug Caught by Its Own Safety Net, and JTAG PR Wedges the RP :complete:

## 1. Hypothesis

The four owner-approved Phase-3 amendments (A1–A4) can be adopted as spec
v2.5.0 and implemented without regression; with A3's PR-timing-gate scope
fixed, the long-dead PR flow can finally produce a loadable pattern partial,
which — loaded onto the flashed shell — will read the R45a CYCLES/BYTES
counters over the R78.11 PERF path for the first time on real silicon.

## 2. How

Multi-agent workflow (single-writer git per wave; the sabotage-race lesson from
the previous entry honoured): commit the pending v2.4.0 PERF work → adopt A1–A4
as v2.5.0 → implement A1/A3/A4 on disjoint files in parallel → A2 seams →
author AC-3-2/AC-3-3 → adversarial review. In parallel, a read-only design
track profiled `~/snort3-community-rules.tar.gz` and drafted an FPGA
rule-filtering spec. Then, by hand: build one real `pr_bitstream`, load it,
read counters.

## 3. Observations

- **v2.5.0 adopted and implemented.** A1 (R3b measurement protocol —
  median-of-≥5 trials, subject pinned at 132 B, rotating loss-regime warmup;
  constant kept at 1.15×), A2 (R67 `await_synthesis` + `set_strict_residency`
  / R51b-strict seams), A3 (R73a PR-gate scope + A3.5 bitstream preservation),
  A4 (F2/F3 demoted; `PYRO_DEVICE_IFACE` no-default/fail-closed; R83 canonical
  string gains `transport: PYRO_DEVICE_IFACE not configured` as condition 1).
  Unit suite **593 passed / 1 skip**; full acceptance **715 passed / 10 skips**
  (AC-3-2 + AC-3-3 among them: 16 passed / 2 honest device-gated skips).
  *Test-brittleness note:* `test_ac2b2_probe::…privilege_free…` hard-asserts the
  pytest interpreter LACKS `CAP_NET_RAW` rather than skipping — it fails if the
  device transport's capability is granted to the venv `python`. Kept
  `CAP_NET_RAW` on `python3` only (device commands) and the cap-free `python`
  for the suite; the AC test should `skip`-under-privilege (recommended, not
  changed here).
- **First real HW partial, and the R73a gate bug it exposed.** A `pr_bitstream`
  job for `abc[a-f]{2}` (est. 358 LUT / 519 FF) ran a full in-context P&R
  against the locked static DCP. The A3 scoped-timing query returned **zero
  paths** → R73a.3 refused to pass an ungated partial and **A3.5 preserved the
  routed bitstream + reports** (instead of the old destroy-on-failure). This is
  exactly the "residual risk 1 — mis-scoped query" A3.6 told us to check on the
  first real job, and the safety nets the amendment mandated for that case both
  fired correctly.
- **Root cause (two bugs, confirmed against the routed checkpoint).**
  (1) `get_timing_paths -from/-to` a **hierarchical** cell returns zero paths —
  its pins are timing *through* points, not start/endpoints; the scope must be
  the RP's **leaf cells** (`-from/-to`) plus its **boundary pins** (`-through`).
  (2) The `GROUP == axis_aclk` filter mis-named the routed clock (it is
  **`axis_aclk_0`**) and was redundant with cell scope. With a correct
  leaf+boundary query the RM's true worst path is **WNS = +0.023 ns** — it
  **meets** 250 MHz — and `pr_verify` reports the checkpoints **compatible**.
  So the partial was always good; only the gate was wrong. Fixed in
  `toolchain.py` (`ac749bb`); the preserved `.bit` loaded without a rebuild.
- **JTAG partial load wedges the RP — the real wall.** Loading the (valid,
  `pr_verify`-passed) partial over JTAG succeeded in ~17 s, but the child then
  answered **no** R78 frame (ID/MATCH/PERF all silent). A **revert test**
  settles it: reloading the *known-good boot ID stub* through the identical
  JTAG path **also** goes silent — so the fault is the JTAG reconfig path, not
  the pattern child. Boot brings the RP up responding; a JTAG
  `program_hw_devices` does not, even for identical bits.
- **Why:** raw JTAG bypasses the static shell's DFX sequencing. `pblock_pyro_rp`
  carries `RESET_AFTER_RECONFIG 1` (GSR resets the RM's own flops), but there is
  **no decoupler holding RP outputs quiet and no coordinated static-side
  interface reset** during the ~17 s reconfig, so the static-side AXIS path
  wedges. `device-bringup.md` §6 already records that the in-band ICAP/MCAP path
  (which *would* sequence decouple+reset via the shell's PR controller) is
  **explicitly deferred** — that missing plumbing is exactly what's needed.
  PCIe/`onic`/`ens2` survive throughout (R85 holds); only the RP responder dies
  → `device_usable=false`. JTAG reload does not restore it; recovery needs a
  **cold power cycle**.
- **On-chip counters remain unread on silicon.** R45a + R78.11 stay model/xsim/
  host-codec verified only. The blocker is partial-reconfig bring-up, **not**
  the counter or PERF path — which is an important distinction: everything from
  the host codec through the wire format is proven; only the on-device
  reconfig-then-respond handshake is missing.
- **Snort design track.** Full parse of the 4,017-rule community set: 97.0%
  have a usable anchor literal (median 12 B), 56% match only
  inspector-normalized sticky buffers (raw-byte prefilter blind → nomination
  only), 74% of PCRE is NFA-clean. Drafted `specs/snort-rule-offload.md`
  (SNORT-PF v0.1.0, DRAFT): rule-group pattern-set circuits as PR partials on
  the unmodified PYRO shell, FPGA nominates over a conjunct-drop
  over-approximation, full Snort re-verifies (PYRO's R19 discipline applied to
  NIDS). Honest headline: 97% *compilable* but ~6.4% *instantaneous resident*
  coverage under single-tenant residency until a ROM-baked shared-anchor trie
  is proven.

## 4. Data analysis

The A3 amendment's design paid off precisely where it was meant to: a narrowed
safety gate that could have silently passed an ungated partial instead failed
loud (R73a.3) and preserved the evidence (A3.5), turning a subtle Tcl-scope bug
into a ten-minute diagnosis rather than a shipped-onto-marginal-static
incident. The deeper finding is that the PR *mechanism* — not the PR *timing
gate*, not the pattern circuit — is the true bring-up frontier: JTAG
`program_hw_devices` reconfigures the fabric but cannot bring a child up
responding the way power-on does, because the shell has no
decoupler/reset-after-reconfig coordination for the out-of-band path. The
counters are one working reconfig handshake away, and no closer.

## 5. Ideas for future experiments

- **Unblock counters — pick one:** (a) add a `dfx_decoupler` + post-reconfig
  reset to the static shell engaged for JTAG loads (shell rebuild + reflash +
  relock DCP); (b) implement the deferred ICAP/MCAP controller so the shell
  sequences decouple+reset in-band; (c) probe the PCIe BAR (resource2) for a
  host-reachable user-box soft-reset to pulse after JTAG load (unverified;
  do not blind-poke).
- **Verify the R73a fix on the next real build** — read the first
  leaf+boundary-scoped `timing.rpt` by hand against the reported RP WNS before
  trusting the gate (A3.6 residual-risk-1 discipline).
- Cold-cycle the card to restore `device_usable=true`, then retry the load once
  a reconfig-reset path exists.

---

# EXPERIMENT 14 Jul 2026 20:26:10 Counter Wedge Fixed — a Circular-Import Corpse, and Why the Workaround Failed :complete:

## 1. Hypothesis

The counter wedge (previous entry §3) can be characterised to a deterministic
trigger, the workaround-defeating observation explained, and a minimal fix
landed whose regression test provably fails on the unfixed code.

## 2. How

Characterise → fix → adversarially verify (two lenses: efficacy incl.
sabotage of the fix; regression on both builds). Full blast-radius audit of
every pyro-internal consumption of the patched `re` surface.

## 3. Observations

- **Mechanism, at file:line precision.** `install()` patches stdlib `re`; the
  first post-install `explain(eligible)` lazily imports `pyro.synth`; that
  cascade imports stdlib `dataclasses` for the *first* time, whose module-level
  `re.compile` now returns a PyroPattern; dataclasses' string-annotation checks
  call it 33+ times, crossing `N_REUSE=32` → model verdict →
  `_consult_residency` re-enters **mid-import** → the nested
  `from .synth import residency` yields a **partially initialized module**,
  whose failed import importlib then evicts from `sys.modules` — but the corpse
  is already cached in `_route._residency`, and the `if res is None` guard
  never replaces it. Every later consult hits
  `AttributeError: no attribute 'get_manager'`, swallowed; the R4a launch
  policy is dead for the process. `uninstall()` does not heal it.
- **Why install-then-dispatch never wedged:** when `_consult_residency` itself
  initiates the import, its *outer* frame's assignment runs last and
  overwrites any nested corpse. Only explain()/stats()/prewarm-initiated first
  imports leave the corpse as the final write. The asymmetry that made the bug
  look flaky.
- **The workaround-defeater, explained.** Prime-before-install only disarms
  the trigger if the priming pattern is *eligible* — `pyro/re.py` imports
  residency inside `if eligible:`. A fallback-only prime (e.g. `(a)\1`)
  returns normally, looks successful, and imports nothing. Also: the trigger
  never fires under pytest, which pre-imports `dataclasses` — why the suite
  was blind to it (the regression test spawns a fresh interpreter).
- **Fix:** (1) re-entrancy-safe cache — `_residency` is assigned only once the
  module proves complete (`hasattr(res, "get_manager")`; the symbol is defined
  at the end of residency.py, so its presence proves the body ran); an
  incomplete module serves the current consult but is never cached. (2) Blast
  radius: `identity.py`/`toolchain.py`/`_circuit_model.py` now take
  `_stock_compile` from `pyro._model` (captured at `import pyro`, provably
  pre-install) instead of compiling through possibly-patched `re` on lazy
  import. (3) The exception swallows that hid the wedge now record into
  `_route._RESIDENCY_CONSULT_FAILURES` — diagnostics, not part of the R31/R66
  shapes. Sabotage-proof: guard removed → regression test fails; restored →
  passes. Trigger on fixed tree: `synth_launched=1`, `circuit_status=resident`.
- Suites: native **1187 / 3**, pure **1150 / 40** — baselines + exactly the
  one new regression test, zero failures. The prime-before-install workaround
  in `phase3_workers` is removed; AC-3-1 still transitions.
- **Process incident worth recording:** the two adversarial verifiers ran
  `git stash` sabotage cycles *concurrently in the same working tree* and
  raced — one verifier observed (correctly, at that instant) the load-bearing
  guard missing. The tree's final state was intact, but `_route.py` was then
  lost to a careless `git checkout` during single-writer re-verification and
  had to be reconstructed by replaying the agents' successful Edit operations
  from their transcripts (applied 19:00 fix + 19:25 rewrite, reversed the
  19:46 sabotage). Verified clean afterwards: guard present, sabotage bites,
  both suites green. Lesson: sabotage-style verification MUST be single-writer;
  concurrent verifiers get read-only trees or worktree isolation.

## 4. Data analysis

Three compounding hazards, each individually survivable: a lazy import
cascade that can be initiated from multiple call sites with different healing
properties; stdlib modules executing pattern compiles at import time through a
patched surface; and a cache-on-first-write of a module object mid-import.
The fix attacks the third (never cache an unproven module) and the second
(internal consumers hold pre-install references), which also makes the first
harmless. The residual disclosed honestly: during the one trigger-window
import, eligible dispatches are still uncounted (dozens of consults fail and
are now *recorded*, not hidden) — the launch policy self-heals immediately
after, which is behaviourally invisible at any realistic `PYRO_N_SYNTH`.

## 5. Ideas for future experiments

- AC-3-2 (post-A2 ruling) should assert `_RESIDENCY_CONSULT_FAILURES == 0`
  across its tier-transition runs — turning the diagnostic into a canary.
- Worktree isolation for any future multi-verifier sabotage workflow.

---

# EXPERIMENT 14 Jul 2026 18:35:59 AC-3-1 + AC-3-4 Land — Sabotage-Verified Suites, and a Counter-Wedge Bug Found :complete:

## 1. Hypothesis

AC-3-1 (R60 transparency regression under `install()`, incl. mid-run tier
transitions) and AC-3-4 (stats/lifecycle counters under fault + synth-failure
injection) can be delivered **without any spec change** — R60 deliberately does
not enumerate its corpus, and the R67 seam set suffices if tier transitions are
deadline-polled rather than awaited (the `await_synthesis` seam is amendment
A2, pending owner review). The known failure mode for this class of test is
the **vacuous pass** — a suite that passes on a broken build — so every
mechanism must carry a positive assertion that it *fired*, and the review
must attempt real sabotage, not code reading.

## 2. How

- **Infra:** fixed the oracle booby-trap (`oracle.py` captured stock callables
  at call time, so under `install()` it compared pyro against pyro —
  vacuously green; it now captures a `_STOCK` table at import and *refuses to
  import* while installed). `phase3_support.py`/`phase3_workers.py`: corpus
  programs run in worker processes with isolated `PYRO_CACHE_DIR`, mock
  toolchain forced (`_worker_env` pops `PYRO_TOOLCHAIN`/`PYRO_VIVADO` so a
  leaked vivado pin can never reach a corpus worker — Vivado is installed on
  this host now, and that leak would launch real hours-long synthesis from a
  unit test).
- **AC-3-1:** `programs.py` — the R60 corpus (the module *is* the corpus
  definition): log-scanner (≥64 KiB subjects — the tier-transition vehicle),
  tokenizer, config parser, `re.sub` with callable repl, walrus, `finditer`/
  `groupdict` dispatch, `except re.error` flow, AC-1-7 bug-derived shapes.
  Full per-call sequence compared stock-vs-installed (jsonify both sides;
  exceptions by type AND `str(e)` AND `re.error` fields). Deliberate
  exclusions (HybridMatch type name, `m.re`, `repr`, pickle — R36a/spec-known)
  documented in the docstring as spec-level, not bugs.
- **AC-3-4:** counter shape/monotonicity (the two gauges rise AND fall),
  dispatch attribution per regime, `hardware` reported honestly as 0 and
  never incremented by model dispatches, `inject_synth_failure` → R65
  permanent-fallback with byte-identical results, fault seams →
  `fallback_after_error`, and N×M threaded loss-regime calls → `fallback ==
  N*M` exactly (exercises the native per-thread TLS blocks + retired-thread
  fold). Both builds.
- **Verification:** three adversarial lenses; the vacuous-pass lens ran REAL
  sabotage — `install()` no-op'd, oracle fix reverted, tier promotion stubbed,
  injection no-op'd — and confirmed each suite FAILS loudly under its
  sabotage, then restored.

## 3. Observations

- Suites (native / `PYRO_NO_NATIVE=1`), clean box, after removing stray state:
  **1186 passed / 3 skipped** and **1149 passed / 40 skipped** — ledgers
  reconcile at 1189; zero failures; no `~/.cache/pyro` residue; no leaked
  workers (earlier forkserver orphans traced to killed pytest runs, not to
  clean runs).
- Tier transition positively observed from test code, both R51-step-4 arms:
  size arm (`log_hunter`, ≥64 KiB, `PYRO_N_SYNTH=2`) sequence
  cold→synthesizing→warm→resident; reuse arm (`field_extractor`, ≥32 reuses,
  first hot snapshot ≥ call 33). `synth_launched > 0 AND synth_succeeded > 0`
  asserted; outputs byte-identical before AND after the flip.
- All three lenses: **refuted=false**. Caveats worth keeping: the
  transition assertions are timing-fragile under heavy host load (45 s poll
  deadline); the conftest `~/.cache/pyro` residue check is vacuous when the
  cache pre-exists (it only detects *creation*).
- **A genuine product bug (NOT fixed here — needs triage): the counter wedge.**
  If the residency manager / HDL estimator is *first constructed while
  `install()` is active* (e.g. the process's first `explain()` happens
  post-install), pyro's own internal `re` usage re-enters the patched module
  during lazy construction, and from then on the R4a launch policy silently
  stops ticking for the whole process: eligible dispatches are no longer
  counted, `circuit_status` stays `cold`, and `explain()`'s internal
  try/except hides the failure. Install-then-dispatch works; the wedge needs
  explain-before-first-dispatch-while-installed. One review run hit it
  **despite** the documented prime-before-install workaround, so the trigger
  surface is broader than currently understood. This lands squarely on
  AC-3-2 (automatic tier dispatch), which cannot honestly pass while the
  wedge exists.

## 4. Data analysis

The oracle booby-trap is the single most consequential fix in this batch:
every future differential test under `install()` would have been silently
vacuous. The sabotage protocol (break the mechanism, demand the test fail,
restore) is cheap — minutes per mechanism — and it is the only review step
that distinguishes an acceptance test from a green rubber stamp; reading the
test code does not. It also produced the one nearest-miss worth recording:
`test_results_byte_identical_throughout_failure_injection` survives an
injection no-op *alone* (result-identity genuinely holds either way), and is
non-vacuous only because its sibling assertions over the same fixture fail —
class-level coverage, acceptable but worth knowing.

The counter wedge is a re-entrancy bug with the same shape as the oracle trap:
pyro consuming its *own* patched surface. Anything pyro-internal that touches
`re` after `install()` is suspect; the fix direction is for pyro's internals
to hold pre-install references (exactly what the oracle fix did for tests).

## 5. Ideas for future experiments

- **Fix the counter wedge** as the opening act of AC-3-2 (blocked on amendment
  A2 for the `await_synthesis`/strict-residency seams). Reproduce it
  deterministically first — the workaround-defeating trigger seen in review is
  not yet characterised.
- AC-3-3 benchmark suite once A1 (R3b.1–R3b.4) is ruled on.
- Harden the conftest residue check: record `~/.cache/pyro`'s mtime/contents
  at session start rather than mere existence.
- Consider a `performance`-governor calibration run before AC-3-3 lands in CI.

---

# EXPERIMENT 14 Jul 2026 16:23:15 Native Routing Hot Path (R3c) — R3b Reachable at ~1.09×, Warmup Defect Found in the Recipe :complete:

## 1. Hypothesis

R3b requires loss-regime wall-clock within **1.15×** of stock CPython `re`; it
becomes a **hard PASS at AC-3-3** (R3c), where a SKIP is forbidden. The Python
router measures **4.53×** (repo recipe) / 6.3× (timer-free) — overhead ~915 ns
against a 26–39 ns budget. Question: is 1.15× physically reachable by a native
(C) routing hot path, and if so, does a *shippable* implementation (thread-safe
counters, kwargs signature, GC support, byte-identical semantics) still fit?

## 2. How

- **Method:** measured spike → adversarial refutation → real implementation →
  adversarial refutation again (three lenses each, verifiers instructed to
  default to *refuted* when uncertain). All timing on `nf-server06` (Xeon E5
  v4, `schedutil` governor), serial runs on a quiet box, medians of ≥5
  process-level trials, noise floor established by stock-vs-stock (±1.5%).
- **Recipe:** `tests/acceptance/test_ac2_5_throughput.py:86-115` verbatim —
  132-byte subject, 1500 non-matching literal patterns, reuse = 2 (< N_REUSE),
  loss regime asserted at runtime via `stats()`.
- **Implementation:** `pyro._fast`, a C extension `Pattern` type
  (`src/pyro_ext.c` + `src/pyro_route.c` + `include/pyro_route.h`): the §8 R51
  decision (S0–S6, transcribed from `_route.py:263-303`) as a **direct C call**
  (no ctypes — one ctypes hop measures 175.9 ns = 4.5× the whole budget);
  fallback delegation via cached bound stock method + `PyObject_Vectorcall`;
  per-thread counters (initial-exec TLS, single `%fs`-relative load — verified
  zero `__tls_get_addr` in the binary); model verdicts hand off to the SAME
  Python `_serve_model_*` the pure-Python router uses (one semantics, no fork).
  Anti-drift: thresholds live once in `pyro/_thresholds.py` (generated C
  header + runtime round-trip test); a 288-point exhaustive decision-table
  equivalence test; full differential vs `PyPattern` (the pure-Python class,
  kept permanently as the behavioural reference). `PYRO_NO_NATIVE=1` selects
  pure Python; CI runs the whole suite both ways.

## 3. Observations

- **Overhead anatomy of the Python router** (ablation, sums to 917.6 ns =
  measured total to 0.4%): `threading.Lock` in `_record()` **435 ns** (11× the
  entire R3b budget on its own); 5 Python frames **208 ns**; Python decision
  arithmetic **175 ns** (missing from all prior analyses); `getattr` **99 ns**.
  The R51 decision itself, in C: **3.5–9 ns**. The decision was never the cost.
- **Native floor:** a C wrapper that does nothing but vectorcall the cached
  stock bound method measures **~1.01×**. Physics is not the obstacle; the
  spec's rationale for 1.15× is vindicated, not falsified.
- **First adversarial pass killed the headline.** Implementation initially
  reported 1.10 median; a verifier reproduced every control yet measured
  **1.29/1.28/1.29/1.12** on the same quiet host — per-process modes at
  pyro ≈277 ns vs ≈337 ns with stock flat.
- **Root cause of the "bimodality": the recipe's own warmup.** It warms with a
  *single* throwaway pattern × 3000 calls; that pattern crosses `N_REUSE=32`
  at call 33 and routes to the **model** verdict for the remaining 2967 calls
  — so for the native router, warmup exercises the Python model-handoff path
  and leaves the *measured* C fast path cold. Controlled A/B, warmup style the
  only variable: single-pattern warmup → **1.26–1.30** (5/5 fail); rotating
  warmup (100 × 30 uses, all < N_REUSE) → **1.05–1.09 typical**, median of 5 =
  **1.094** (passes; residual excursions to ~1.2 correlate with `schedutil`
  frequency drift — stock itself swings 255→330 ns between processes).
- **Second adversarial pass, real implementation:** correctness lens **CLEAN**
  (no divergence vs `PyPattern` anywhere, including pos/endpos edge cases —
  the spike's `search(s, 0, None)` TypeError bug was designed out by
  differencing against `PyroPattern`, not stock). Regression lens found a real
  **segfault**: `gate()` vectorcalled a NULL `_serve_model_*` when the native
  type was imported directly under `PYRO_NO_NATIVE=1` (configure() skipped).
  Fixed: NULL guard → `RuntimeError`; regression test added.
- **Suite, both build shapes, zero failures:** native **1127 passed / 3
  skipped**; `PYRO_NO_NATIVE=1` **1090 passed / 40 skipped** (the native-only
  tests standing down; ledgers reconcile at 1130).
- **A now-false skip reason found:** with the extension active,
  `test_ac2_5_throughput.py:127` still SKIPs claiming "the §8 R51 routing
  decision is served at Python level" — untrue on a native build (and its
  single-trial 1.33× is the warmup artifact). Recorded in the amendments doc
  (A1.4); not changed unilaterally, since it moves a normative bind point.

## 4. Data analysis

**R3b is reachable, marginal, and the constant is right.** A shippable native
router lands at **~1.09–1.10 median** against 1.15×, with a floor of 1.01×.
The margin (~4–5%) is smaller than single-trial excursions under `schedutil`,
which is precisely why the owner-approved resolution amends the *measurement
protocol* (median of ≥5 trials — R3b.1; pinned 132-byte subject — R3b.2;
binds against the compiled build — R3b.3) and not the constant. This session
added **R3b.4**: warmup must remain in the loss regime, with the A/B above as
evidence — the old warmup violates its own stated intent.

**The adversarial layer earned its cost twice.** It killed a wrong headline
(1.055/1.10 → honest ~1.24 under the defective recipe) and found an
interpreter-killing NULL call — both before commit, both on paths a happy-path
verification would have blessed. Conversely it *cleared* the semantics port,
which is where the real danger lived: a fast, subtly-wrong router silently
corrupts user results.

**Measurement lessons for this notebook:** (1) per-process modes with a flat
control are a *warmup/layout* signature, not load — diagnose by controlled
A/B before blaming the host; (2) `perf_counter_ns` self-cost (~112–116 ns on
this box) inflates both sides of a paired ratio and *flatters* it — report
timer-free cross-checks for any claim finer than ~10 ns; (3) on `schedutil`,
per-process CPU frequency is a hidden variable — medians across processes,
never single shots.

## 5. Ideas for future experiments

- **Owner review of `docs/spec-amendments-phase3.md`** (A1 R3b.1–R3b.4 + the
  A1.4 test edits, A2 R67 seams, A3 R73a PR-timing scope, A4 F2/F3). A1's
  adoption converts the R3b machinery from "measured here" to normative; the
  AC-3-3 benchmark suite then implements R3b.1–R3b.4 directly.
- **AC-3-1/3-2/3-4** are unblocked and unaffected by any of this; build next
  (transparency corpus + tier-transition seams + stats-under-injection).
- On a `performance`-governor host the residual trial spread should collapse;
  worth one calibration run if AC-3-3 flakes in CI.
- The `_run_pr` global-WNS bug (A3) still destroys every good pattern partial
  after `pr_verify` passes — hardware partials stay blocked until A3 is ruled
  on. R1/R2 remain honest SKIPs regardless (1 B/cycle datapath ⇒ 0.233 GiB/s
  < R1's own 1 GiB/s floor; not a bring-up problem).

---

# EXPERIMENT 14 Jul 2026 08:49:52 PR Shell Rebuilt From Source on nf-server06 — New Card, New Flash, device_usable=true :complete:

## 1. Hypothesis

The U250 in `nf-server06` boots its **factory golden image** (`10ee:d004`),
not the Phase 2b PYRO PR shell that the 9 Jul entry recorded as flashed and
verified booting. QSPI is on-card and the card was believed to have moved from
`zanetti`, so the shell "should" still be in flash. Two competing explanations:

- **(a)** the image is intact but configuration fails for an environmental
  reason (leading suspect: PCIe aux power — the golden image is a minimal
  low-power design), or
- **(b)** the user image is absent/invalid.

Can `BOOT_STATUS` + the JTAG chain distinguish these **without** a QSPI
readback (which would require a volatile `program_hw_devices` — the operation
that crashed zanetti)? And if (b), can the PR shell be rebuilt **from source**
on this host, given that the original shell tree existed only as an
unversioned build directory on zanetti (`/usr/local/cad/gn262/pyro/`) and is
therefore gone?

## 2. How

- **Equipment:** Alveo U250 (`xcu250-figd2104-2L-e`) at PCI **`0000:02:00.0`**
  (physical slot 2, root port `0000:00:02.0`), QSPI mt25qu01g; host
  **`nf-server06`** (Supermicro X99, Xeon E5 v4, ASPEED BMC — **no iDRAC**),
  Ubuntu 24.04, Linux **6.8.0-134-generic**. On-board USB-JTAG (FT4232H,
  `manufacturer=Xilinx`, `product=A-U250-P64G`), hw_target serial
  **`2133061B901XA`**, FPGA DNA `40020000013B9E220500E485`.
- **Software:** Vivado **2025.2** freshly installed at
  `/usr/local/cad/2025.2/Vivado` (the R70a pin). License node-locked to this
  host's `ens9` MAC `68:05:ca:41:93:34`; covers `XCU250`/`XCU250_bitgen`,
  `PartialReconfiguration`, `cmac_usplus` (permanent). **Enterprise edition
  feature expires 11 Sep 2026.**
- **Sources (all now version-controlled, unlike the zanetti tree):**
  `third_party/open-nic-shell/` (vendored), `hw/pyro_plugin/` (the
  `pyro_250mhz` box replacing stock `p2p_250mhz`), `hw/src/pyro_id_stub.sv`
  (default child, rewritten from the R78 spec + `pyro.hdl.rp_wrapper`'s reply
  constructor), `hw/src/pyro_rp_stub.v` (the frozen R80 boundary as a black
  box), `hw/dfx/` (DFX flow ported from `ebpf-os/integration/dfx`).

### Key commands

```bash
# Diagnosis (READ-ONLY; no reconfiguration of any kind):
vivado -mode batch -source scripts/status.tcl

# Build the PR shell from source (~2.5 h):
hw/dfx/dfx_build.sh --fast          # ID-stub OOC synth + 250MHz gate only
hw/dfx/dfx_build.sh --jobs 16       # full: static -> lock -> partial -> pr_verify

# Flash (live PCIe -- see §4):
PYRO_FLASH_ALLOW_LIVE_PCIE=1 scripts/flash_u250.sh flash \
    hw/dfx/build/dcp/open_nic_shell.bit
```

## 3. Observations

**Diagnosis — hypothesis (b), and for an unanticipated reason.**

- `status.tcl`: `DONE=1`, `EOS=1`, `PLL_lock=1`, `CRC_error=0`, die 50.8 °C,
  VCCINT 0.847 V / VCCAUX 1.828 V / VCCBRAM 0.850 V. The FPGA configures
  **cleanly** and its rails are nominal — **hypothesis (a) (aux power) is
  dead**. PCIe link is Gen3 8.0 GT/s ×16, identical to zanetti's.
- **`BOOT_STATUS.SLR0 = 0x00000d07`** decodes to `STATUS_VALID_0` +
  **`IPROG_0`** + **`FALLBACK_0`** + **`WTO_ERROR_1`**, with `CRC_ERROR` and
  `ID_ERROR` **clear**. The golden multiboot jumped to `0x01002000`, the
  configuration **watchdog timed out**, and it fell back to golden. A clear
  CRC/ID with a watchdog timeout is the signature of an **empty or invalid
  user slot**, not a corrupted image.
- **This is not zanetti's card.** The FT4232H is on the Alveo itself, so its
  serial identifies the board. `ebpf-os/docs/fpga-bs.md` records zanetti's as
  **`2132049BF00YA`**; this one is **`2133061B901XA`**. A *different physical
  U250*, whose QSPI user slot was never written. That fully explains the
  golden fallback and why the 9 Jul image (`5603dd5c…`) is nowhere to be found.

**Build (from scratch, all sources now in git).**

- ID stub OOC synth: **0 errors, 0 critical warnings**, WNS **+2.885 ns** at
  the 4.000 ns (250 MHz) constraint; **32 LUTs / 336 FFs / 0 BRAM**.
- Static DFX assembly saw **exactly one black box** —
  `box_250mhz_inst/pyro_250mhz_inst/g_intf[0].pyro_rp_inst` — i.e. the plugin
  and the `-user_plugin` mechanism worked and only the RP was left empty.
- Floorplan **`CLOCKREGION_X0Y9:CLOCKREGION_X3Y10`** (SLR2), **not** the
  `CLOCKREGION_X5Y7:X5Y8` that `docs/device-bringup.md` quotes — see §4.
- Post-route timing, per clock domain:

  | Clock | WNS (ns) | Contents |
  |---|---|---|
  | `axis_aclk_0` | **+0.030** | QDMA H2C → `pyro_rp` → C2H (our datapath) |
  | `clk_out1_qdma_subsystem_clk_div` | +0.715 | QDMA / AXI-Lite |
  | `pipe_clk` | +0.351 | PCIe |
  | **`txoutclk_out[0]`** | **−0.427** | **OpenNIC `cmac_usplus` lbus2axis** |

  Worst path *inside* `pyro_rp`: setup **+0.482 ns**, hold **+0.021 ns** —
  all MET. The only failing domain is inside OpenNIC's own CMAC IP.
- **`PR_VERIFY_ALL_OK`** — the ID-stub partial verifies against config0.
- Artifacts: `open_nic_shell.bit` 43 MB (sha256 `ee5094ae…`),
  `open_nic_shell.mcs` 118 MB (sha256 `165c2480…`),
  `static_routed_locked.dcp` 76 MB, `partials/id_stub.bit` 3.9 MB.
  Build script computed and logged **BUILD16 = `0xB18A`** — but that is *not*
  what got baked into the shell; see the `BUILD16` defect below.

**Flash — and the zanetti hazard did not reproduce.**

- `program_hw_devices` loaded the SPI-bridge helper ("1 SPI core(s)", 12 s)
  **while the card was live on PCIe at `02:00.0`** — the exact operation that
  raised an uncorrectable root-port FATAL and reset zanetti four times.
- `Erase Operation successful.` → `Program/Verify Operation successful.` →
  `Flash programming completed successfully` — **FLASH_DONE, rc=0, 15m33s**.
- **The host did not crash.** Uptime unbroken across the whole operation.
- Card still enumerates `10ee:d004` (golden) post-flash — **expected**: the
  FPGA only reads QSPI at power-up.

**Cold power cycle — the shell boots, and `device_usable` flips true.**

- **`BOOT_STATUS.SLR0 = 0x00000005`** (was `0x00000d07`): `STATUS_VALID` +
  `IPROG`, with **`FALLBACK` and `WTO_ERROR` now CLEAR**. The multiboot jump to
  `0x01002000` completed. This one register is the whole before/after.
- `02:00.0` enumerates **`10ee:903f`**, PCI class **`0280`** (Network
  controller), subsystem **`10ee:0007`** — the same subsystem the 9 Jul entry
  recorded for the working shell. Gen3 8.0 GT/s ×16. BARs changed from golden's
  32M+64K to OpenNIC's **256K + 4M**.
- **One** physical function, not two (zanetti had `903f` + `913f`): we build
  `pf=cmac=1`, and the driver confirms — `onic: Number of CMAC instances = 1`.
- `onic` driver: **`make` alone produces a broken module.** Stale objects in
  `NetFPGA-PLUS/sw/driver/open-nic-driver` (from the 6.8.0-124 build) survive an
  incremental build; the link stamps the right vermagic but the objects carry
  old symbol CRCs, so `insmod` dies with `disagrees about version of symbol
  netdev_info` / `Unknown symbol ... (err -22)` and the misleading userspace
  message **"Invalid parameters"**. `make clean && make` fixes it (3.5 MB, the
  size ebpf-os recorded). The three kernel-6.8 API fixes were already applied.
- **The netdev is `ens2`**, not `enp2s0f0`: systemd used slot-based naming
  (`onic 0000:02:00.0 ens2: renamed from onic2s0f0`).
- Interface up (**NO-CARRIER, as expected** — CMAC is tied off), `CAP_NET_RAW`
  granted to a copied venv interpreter, then:

  ```
  device_usable = True
  reason = device_usable=true — static_shell_id=0x02023841, transport: CAP_NET_RAW present
  ```

- Protocol round-trip on real silicon:

  | request | reply | spec |
  |---|---|---|
  | `ID_REQUEST` | `ID_REPLY` (0x02), `SPEC16=0x0202`, **`rp_child_id = 0`** | R80/R81 |
  | `MATCH_REQUEST` | `STATUS/ERROR` (0x05), **code 7 = `PYRO_E_NOT_RESIDENT`** | R78.8 |

**Defect found and fixed: `BUILD16` is ASCII garbage in the flashed shell.**

- The shell reports `static_shell_id = 0x02023841`, i.e. **`BUILD16 = 0x3841`**,
  not the `0xB18A` the build script computed and logged.
- Cause: `synth_design -generic BUILD16=0xB18A`. **Vivado's `-generic` does not
  accept a `0x` literal** — it silently binds it as a *string*, and a string in
  a `[15:0]` parameter becomes its ASCII bytes. Vivado logs
  `Parameter BUILD16 bound to: 8A - type: string` and **does not warn**.
  `'8'=0x38`, `'A'=0x41` → `0x3841`. Exactly what the card reports.
- Fixed: pass BUILD16 as a **decimal** integer. Verified —
  `Parameter BUILD16 bound to: 16'b1011001100101110` (= `0xB32E`). `dfx_build.sh`
  now hard-fails if the parameter ever binds as a string again.

## 4. Data analysis

**The card is the story.** Every earlier hypothesis (aux power, corrupt image,
a flash that silently didn't take) was wrong in the same way: they all assumed
continuity of *the board*. The JTAG serial is the cheap invariant that settles
it, and it was never recorded in this notebook — only in `ebpf-os`'s. Worth
making a habit: **record the hw_target serial and FPGA DNA in every entry**,
because BDF, hostname, and netdev names are all properties of the *host*, and
only these identify the *card*.

**`BOOT_STATUS` is the right diagnostic, and it is free.** It answered
"is the user image there?" without a QSPI readback — which matters, because a
readback needs the same volatile `program_hw_devices` bridge the flash does,
i.e. it carries the identical host-crash exposure. Paying a host-reset risk to
*confirm* what a free register already told us would have been a bad trade.
`FALLBACK + IPROG + WTO_ERROR` with `CRC/ID` clear ⇒ nothing loadable at the
multiboot address.

**The live-PCIe crash appears genuinely R740-specific.** nf-server06 absorbed
a volatile JTAG reconfiguration of a live, enumerated endpoint with no fatal
and no reset. That is a single data point, not a proof, and the mechanism
(endpoint identity swapping under a running root port) is real — but the
zanetti workaround (BIOS slot disable via iDRAC) has no equivalent here and is
now, on this evidence, not needed. `flash_u250.sh` still refuses by default;
the risk decision remains an explicit `PYRO_FLASH_ALLOW_LIVE_PCIE=1`.

**The floorplan in the docs is wrong for this shell.** `CLOCKREGION_X5Y7:X5Y8`
overlaps OpenNIC's own packet-adapter pblocks on row Y8 (`X1Y8:X2Y8`,
`X5Y8:X6Y8` — recorded in `ebpf-os/integration/dfx/README.md`). Used the SLR2
region ebpf-os proved on this exact part instead. Pattern circuits measure
197–410 LUTs, so RP area is not the binding constraint; avoiding the collision
is. `hw/dfx/platform_manifest.json` is now the single source of truth.

**The timing failure is real but out of the datapath.** WNS −0.427 ns lives
entirely in OpenNIC's `cmac_usplus` lbus2axis FIFO on the 322 MHz transceiver
clock — the RISK-2 "OpenNIC-on-2025.2 closure" problem ebpf-os documented and
shipped partials on. In the PYRO shell the **CMAC path is tied off entirely**
(`adap_tx` idle, `adap_rx` sunk), which is precisely why bring-up needs no
100G transceiver or link partner: the R78 control protocol loops
H2C → `pyro_rp` → C2H *inside the card*, never reaching a MAC. Caveat for
future work: `axis_aclk_0` closes at only **+30 ps**. The 250 MHz box has
essentially no margin left, so a per-pattern child materially larger than the
ID stub may not close — watch this when the first real pattern partial is built.

**Two silent-corruption failures, same shape.** Both the `BUILD16` bug and the
`onic` build failure share a structure worth naming: **a tool accepted bad input
and produced a plausible artifact instead of an error.** Vivado took
`BUILD16=0xB18A`, decided it was a string, bound `"8A"`, logged it in a form
nobody reads, and emitted a bitstream that synthesizes, routes, passes
`pr_verify`, flashes, boots, and answers the probe — while carrying an identity
that is ASCII text. `make` took stale 6.8.0-124 objects, linked them with a
6.8.0-134 vermagic, and produced an `onic.ko` that is byte-for-byte a valid
module and fails only at `insmod`, where the kernel's honest complaint
("disagrees about version of symbol") is flattened by userspace into the
actively misleading **"Invalid parameters"** — which sends you hunting for a
module parameter that does not exist. Neither failure was caught by anything
except *checking the value on the far side*. The lesson is to assert on what the
tool actually bound, not on what you passed it: `dfx_build.sh` now greps the
synth log for `type: string` and hard-fails, and `make clean` is not optional
when the kernel has moved under a driver tree.

**`ens2` breaks the shape of F3, not just its value.** The spec's F3 fact names
a netdev (`enp175s0f0`) as though it were derivable from the card. It is not:
systemd chose *slot-based* naming here, so the interface is `ens2` — a name that
encodes the physical slot, not the BDF. No amount of re-deriving `enp<bus>s<slot>f<fn>`
from `02:00.0` would have produced it. A netdev name is a property of the host's
naming policy, and the spec should treat it as configuration, not as a fact.

**Root cause of the whole episode: the shell lived outside version control.**
`open-nic-shell` + the `pyro` plugin + the floorplan + the DFX scripts existed
only as a build tree under `/usr/local/cad/gn262/pyro/` on zanetti. Nothing in
`git log --all --diff-filter=A` for this repo has ever contained a `.xdc`, a
shell `.sv`, or a DFX `.tcl` (only `scripts/status.tcl`). The 6 Jul entry
claims a working PR flow and a first partial; none of the machinery that
produced them was committed. It is all now under `hw/` and
`third_party/open-nic-shell/`.

## 5. Ideas for future experiments

- **Spec debt, now urgent (R71/F2/F3).** The spec declares `af:00.0` (F2) and
  `enp175s0f0` (F3) as normative facts, and `pyro/_route.py` / `pyro/device.py`
  default `PYRO_DEVICE_IFACE` to `enp175s0f0`. On this host the card is
  `02:00.0` and the netdev is **`ens2`** — so *all three* are false and the
  library default cannot reach the device. Everything above only works with an
  explicit `PYRO_DEVICE_IFACE=ens2`. This needs a spec decision (re-declare F2/F3,
  or demote them from facts to per-host configuration), not a silent code edit.
  Note `ens2` also falsifies the *shape* of F3, not just its value: systemd's
  slot-based naming means the netdev name is not derivable from the BDF.
- **Reflash to get an honest `BUILD16`.** The running shell reports `0x3841`
  (ASCII `"8A"`). Harmless — R81 only constrains the SPEC16 half — but the
  discriminator fails at its one job: identifying which build is on the card.
  The fix is in `dfx_build.sh`; it costs a rebuild (~2.5 h) + reflash (~18 min).
  Worth folding into the next shell change rather than doing on its own.
- **AC-2b ledger:** AC-2b-2's hardware path (the real `(True, …)` flip against
  the physical board) is now **LIVE**, not SKIP. AC-2b-3 still needs a real
  per-pattern `pr_bitstream` loaded over JTAG.
- Load the ID-stub **partial** (`hw/dfx/build/partials/id_stub.bit`) over JTAG
  against the locked static — the cheapest possible exercise of `load_partial`
  (R86.5), and the first live PR reconfiguration. It should be a no-op
  observationally (same child), which is exactly what makes it a safe first test.
- Then a **real pattern child**: generate via `pyro.hdl.rp_wrapper`, build the
  partial against `static_routed_locked.dcp`, load it, and confirm `ID_REPLY`
  flips `rp_child_id` to the non-zero R78.5a value and `MATCH_REQUEST` returns
  actual matches instead of `PYRO_E_NOT_RESIDENT`.
  **Watch timing:** `axis_aclk_0` closed at only **+30 ps** with the 32-LUT ID
  stub in the RP. A pattern child is 197–410 LUTs. There is no guarantee the
  250 MHz box still closes — this is the most likely next failure.
- Re-run the AC-2-4 estimator calibration corpus under 2025.2 (R74a): the
  §6 table is 2023.1-derived and is not evidence for the current pin.

---

# EXPERIMENT  9 Jul 2026 10:59:06 U250 QSPI Flash — PYRO PR Shell User Image :complete:

## 1. Hypothesis

Can the Phase 2b PYRO PR shell be written persistently to the U250's QSPI
user-image slot using the proven-safe BIOS-Slot-4-disable procedure — without
crashing the host, as every live-slot JTAG reconfiguration has — so that the
card boots the PR-capable shell at the next cold power cycle?

## 2. How

- **Equipment:** Alveo U250 (xcu250-figd2104-2L-e) at PCI af:00.0 (Dell,
  iDRAC9 @ 10.66.3.9), QSPI mt25qu01g; host zanetti, Ubuntu 24.04,
  Linux 6.8.0-124-generic.
- **Software:** Vivado 2025.2 Hardware Manager over USB-FTDI JTAG;
  `scripts/flash_u250.sh` (ported from ebpf-os, commit f07ff28).
- **Image:** Phase 2b full flash `open_nic_shell.mcs` (124 MB, SPIx4,
  size 128, user image @ 0x01002000), sha256 `5603dd5c…0223e93f` — verified
  identical to the artifact recorded in `/usr/local/cad/gn262/pyro/STATUS.md`.
  Golden image at 0x0 untouched (unbrickable: bad user image falls back).

### Key commands

```bash
# Precondition (BIOS Slot 4 disabled via iDRAC, applied at reboot):
lspci -nn | grep -i xilinx        # -> no output; af:00.0 not enumerated
# Erase + program + verify QSPI over JTAG (script interlock re-checks af:00.0):
scripts/flash_u250.sh flash
# Boot verification (after Slot 4 re-enable + cold power cycle):
lspci -d 10ee: -nn                # -> af:00.0 [10ee:903f], af:00.1 [10ee:913f]
# Shell identity: OpenNIC BUILD_TIMESTAMP CSR, BAR2 offset 0x0
sudo python3 -c "import mmap,struct; f=open('/sys/bus/pci/devices/0000:af:00.0/resource2','r+b'); m=mmap.mmap(f.fileno(),4096); print(hex(struct.unpack('<I',m[0:4])[0]))"
```

## 3. Observations

- The script's PCIe interlock passed: `0000:af:00.0` absent from sysfs
  (Slot 4 disable in effect; slot still powered, JTAG reachable).
- Flash-helper bitstream loaded over JTAG (`program_hw_devices`, 12 s;
  "programmed with a design that has **1 SPI core(s)**").
- `Performing Erase Operation... Erase Operation successful.`
- `Performing Program and Verify Operations... Program/Verify Operation
  successful.`
- `INFO: [Labtoolstcl 44-377] Flash programming completed successfully` —
  **FLASH_DONE, rc=0, elapsed 18m10s** (10:39:29 → 10:57:52).
- **Host did not crash** — first successful on-host reconfiguration of this
  card since the 2026-07-06 JTAG crash.
- **10 Jul 00:45 — first post-flash power cycle, slot still disabled.** Host
  was down 00:02→00:45 and came back cleanly on 6.8.0-124, but no Xilinx
  device enumerated *and no root port `ae:00.0`* — only Sky Lake-E uncore
  functions on bus `ae`. The absent root port is the BIOS Slot 4 disable
  signature (a failed card would still show the root port with no link), so
  this power cycle did not include the Slot 4 re-enable; boot verification
  of the new user image has not happened yet.
- **10 Jul 00:56 — Slot 4 re-enabled + power cycle: card boots the new
  image.** Host up at 00:56:16; `af:00.0` [10ee:903f] / `af:00.1`
  [10ee:913f] (subsystem 10ee:0007) enumerated, **link Gen3 8.0 GT/s x16**,
  `onic` driver bound, netdevs `enp175s0f0/f1` present.
- **Shell identity CSR (BAR2 offset 0x0) reads `0x07060612`** — the OpenNIC
  `BUILD_TIMESTAMP`, confirming the running shell is the Phase 2b PYRO PR
  build (see §4).

## 4. Data analysis

The slot-disable procedure works as designed: with Slot 4 un-enumerated at
the BIOS level, the endpoint identity swap during JTAG activity never
reaches root port ae:00.0, so no platform-firmware FATAL. Timing matches
the ebpf-os reference run (~18 min for a full 128 Mb-addressed image with
erase+verify). The R45a CYCLES/BYTES CSR work (spec v2.3.0, harness 2.1.0)
does not stale this image: the counters live in per-pattern harness circuits
delivered later as PR partials over PCIe; the static shell is unchanged.

First boot resolved the remaining risk: the QSPI image configured before
BIOS bus scan and presented valid config space, so the endpoint enumerated
normally — no golden fallback.

Shell identity is positively the Phase 2b PYRO PR build, not the prior
stock 2022.2 OpenNIC image. OpenNIC's `build.tcl` sets `BUILD_TIMESTAMP`
to the build's launch wall-clock formatted `%m%d%H%M` and embedded as
literal hex digits, so `0x07060612` decodes to **Jul 6, 06:12** — exactly
when the PR-shell build launched (`pr_launcher.sh` mtime 2026-07-06
06:12:22 in `/usr/local/cad/gn262/pyro/`), and the flashed
`open_nic_shell.mcs` re-hashes to the same sha256 `5603dd5c…0223e93f`
recorded above. The stock image would report its own, older build time.

## 5. Ideas for future experiments

- **Next:** load the ID-stub partial over PCIe (ICAP) as the first live PR
  test on the now-verified PR shell.
- Rebuild the `ab+c` partial with the 2.1.0 harness (pre-2.1.0 artifacts are
  stale per R45a) and read back the CYCLES/BYTES counters after a real scan
  — first hardware data for R59/AC-3-3 win attribution.
- Automate the slot dance: `racadm set BIOS.IntegratedDevices.Slot4Disable`
  + `jobqueue create ... -r pwrcycle` (still untested).

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
