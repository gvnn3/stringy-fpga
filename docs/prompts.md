# Prompt Log — stringy-fpga

Every user prompt submitted to Claude Code in this project, recorded
automatically by a `UserPromptSubmit` hook. Newest entries at the bottom.

---

## 2026-07-04 07:16:00 (backfilled, approximate time)

This is a new repository for work on a project where we try to use the dynamic region of the attached FPGA to acclerate string matching and regex operations.  Create a docs/ directory ith a notebook.md file and use the /notebook skill to record all expiriements.  The experiments are kept in reverse chronological order and ahve both date and time stamps (hour:minute:sec) when the experiment was recorded.

## 2026-07-04 07:17:00 (backfilled, approximate time)

Record all prompts in a separate file: docs/prompts.md

## 2026-07-04 07:21:00 (backfilled, approximate time)

Should I install jq ?

## 2026-07-04 07:22:00 (backfilled, approximate time)

Installed

## 2026-07-04 07:25:00 (backfilled, approximate time)

Python has its own regex and string implementations. Can regex matching be accerlated from the Python code onto the FGPA installed in this system?  Using the @spec-writer, create a sepcification for a system that transparetnly offloads regular expression parsing from Python to the FGPA.

## 2026-07-05 00:08:00 (backfilled, approximate time)

Add all files and commit.

## 2026-07-05 00:09:00 (backfilled, approximate time)

I will push, you are NEVER to push

## 2026-07-05 00:14:00 (backfilled, approximate time)

Fix up the commits with the right email now wset

## 2026-07-05 00:20:00 (backfilled, approximate time)

Add Python as a submodule of this repo, carry out the development based on the written spec.

## 2026-07-05 03:20:00 (backfilled, approximate time)

For this project each new regex compilation woudl create a new circuit/block for the FPGA dynamic region.

## 2026-07-05 03:55:00 (backfilled, approximate time)

Commit the new spec and start Phase 1

## 2026-07-05 04:20:00 (backfilled, approximate time)

Continue with Phase 1, work through the night if you have to

## 2026-07-05 09:45:00 (backfilled, approximate time)

keep going
Continue and do not stop until it's done.

## 2026-07-05 10:17:30

Continue

## 2026-07-05 10:44:37

<task-notification>
<task-id>abda46f35ce7c58c6</task-id>
<tool-use-id>toolu_01AKWJ6LiKAcbSkfuXRnwSNz</tool-use-id>
<output-file>/tmp/gn262/claude-1361/-home-gn262-Repos-Yale-stringy-fpga/af23c991-00cd-44ae-95a5-b069841d45c6/tasks/abda46f35ce7c58c6.output</output-file>
<status>completed</status>
<summary>Agent "Review Task 1 (spec + quality)" finished</summary>
<note>A task-notification fires each time this agent stops with no live background children of its own. The user can send it another message and resume it, so the same task-id may notify more than once.</note>
<result>I've read the full C runtime, the artifact format, the header against §7.3, and the ctypes conformance tests in language-lawyer mode. Verdict below.

### Spec Compliance

✅ **Header matches §7.3 (ABI 2.0.0) exactly.** `pyro_ctx`/`pyro_circuit` typedefs; `pyro_status` (all 9 values, order); `pyro_encoding`; `pyro_circ_status` (5 values); `pyro_match` (u64/u64/u32/u32 = 24B, `_Static_assert(sizeof==24)`); R39–R42 signatures verbatim; `pyro_caps` field order identical to R42. The two `pyro_ctx_debug_*` functions and the `PYRO_MATCH_*` `#define`s are additions clearly marked "NOT part of the stable ABI" — the spec enumerates a minimum, so this conforms.
✅ **R38 layout / R47 ring / endianness** — LE readers (`rd32`) throughout; result entries written densely from index 0; 24-byte entry static-asserted.
✅ **R44 defined-state on error** — every entry point sets `*out = NULL` / `*out_count = 0` / `*out = PYRO_CIRC_FALLBACK` **before** validating args; verified by `test_generate_bad_descriptor_invalid_and_null`, `test_scan_before_load_is_not_resident`, alignment test.
✅ **R43 ownership/thread-safety** — `buf` in `pyro_scan` is copied into an aligned staging buffer and never retained past the call; per-ctx `pthread_mutex` guards all CSR/resident/model state; scan holds the lock for its whole duration (single-issue R48); distinct ctxs share only a `pthread_once`-initialized read-only CRC table → independent. `test_concurrent_scans_serialize_correctly` (8×50) passes.

✅ **Q1 Memory/UB (the crux) — clean.** I traced the hostile-artifact path: `artifact_build` checks `len ≥ ART_MIN_LEN`, magic, format version, then the **internal CRC trailer**, then `AH_BODY + n_byte·40 + n_eps·8 + n_assert·12 + 4 == len` (exact), and `build_adj` rejects any edge with `src|dst ≥ n_states`. So a CRC-passing-but-structurally-invalid artifact is rejected before any table walk or scan — no OOB. The worklist `stack` is bounded by n_states (each state pushed at most once via the `!bs_test` guard); bitset words = `(n_states+63)/64`; `out[count]` gated by `count &lt; out_cap`. Descriptor parse in `pyro_generate` is exact-length-checked before every read. `aligned_alloc(64, (len+63)&amp;~63)` with a min of 64 (size is a multiple of alignment, C11-legal). CRC-32 is the reflected 0xEDB88320 zlib variant matching `binascii.crc32`. Valgrind reports 0 errors / 0 leaks (66/66), and the `have_model` ownership flag correctly prevents the leak-on-failure / double-free-on-success / reload-double-free cases in `pyro_circuit_load`.

✅ **Q2 Thread safety per R43** — mutex discipline is correct and complete; single-issue enforced; `pyro_circuit_free` clears `ctx-&gt;resident` under the lock. The only unguarded scenarios (free/close a circuit mid-scan from another thread) are documented caller-ownership violations (R43), not the library's responsibility.

✅ **Q3 Conformance tests drive the REAL ABI via ctypes** (not reimplemented logic — they compare native output to `_scan_windows`/stock `re`). Identity mismatch tested with a wrong hash (`wrong = bytes(16)` → `NOT_RESIDENT`); corrupt manifest integrity, incompatible shell (`0xDEADBEEF`), flipped artifact byte (internal CRC), and `FAILED.json → PYRO_E_SYNTH` all tested. **OVF/resume is genuine**: 40 `A`s with `out_cap=7`, resumes via `start_off = last_start + 1`, asserts the reconstructed union equals the unbounded list, and `STATUS.OVF` is asserted separately.

✅ **Q4 Completeness differential** — `test_c_windows_are_complete_for_stock_re` asserts `stock_starts ⊆ c_starts` (correct superset direction, R19 no-false-negatives), and `test_c_windows_match_python_model` asserts C windows are byte-identical to the Task-5 Python model across 10 patterns incl. `\bword\b`, `café`, `\d+`, `a*`, `^ab` — so re-verified host results are byte-identical transitively.

✅ **Q6 abi-check** asserts `0x00020000` (meaningful; links the real runtime, not a stub). **Current-tree note (not fixed, per instruction):** `tests/acceptance/test_ac0_8_abi_header.py` still asserts `0x00010000` and inspects the removed 1.0.0 stub, so it now **fails against this tree** — which is exactly what R37's version-history note anticipates (AC-0-8 is a historical checkpoint on `phase0-pyro`); reconciling the acceptance suite is Task 8's scope. The 468-test **unit** gate (the Task-7 hard rule) is green; AC-0-8 lives in `tests/acceptance/`.

### Strengths

- This is genuinely careful systems C: the artifact parser is hardened against malformed input (exact-length + state-range validation *before* allocation), the worklist has a provable O(n_states) bound, and the ownership dance in `pyro_circuit_load` (local `model_t m` + `have_model` transfer flag) is exactly the discipline that keeps it leak- and double-free-free — confirmed by valgrind. Knuth would approve of the CRC/state-bound reasoning; Rams of the `model://`-only surface with device URIs parsing-but-refusing.
- The C model is a faithful twin of `pyro._circuit_model._scan_windows` (same closure/assert semantics, including the safer `pos+1==n` form that avoids the `n-1` underflow), so a generator bug surfaces identically in both — honoring the "no independent re-derivation" principle from Task 5.
- R44 defined-state is applied uniformly (out-pointer cleared before any early return), and the R49 alignment check is realized as a *defined error* rather than `abort()` — the right call for a library that must never kill its host.

### Issues

#### Critical (Must Fix)
None.

#### Important (Should Fix)
None. No memory, UB, thread-safety, or conformance defect found.

**§13 items (Q5) — all category (a): fine pending spec blessing, no code change required.**
1. **Native `pyro_generate` does no classification in `model://`** (pattern is a `PYRODSC1` descriptor). This is a legitimate binding-specific reading — eligibility/hashing is Python L2's job (R8) — and is self-consistent and documented. Spec should add a sentence that L2/host owns eligibility and `pyro_generate` is handle-creation in device-free bindings.
2. **R49 realized as `PYRO_E_INVALID`, not `abort()`.** Arguably *more* correct than a literal assert; R44-clean and test-observable. No change; spec may bless the wording.
3. **R47b/R47a load refusals return `PYRO_E_NOT_RESIDENT`.** This is the correct "must fall back, not a device error" code per R44. Spec should pin it explicitly.

#### Minor (Nice to Have)
- **Test seams in the frozen public header.** `pyro_ctx_debug_force_misalign`/`pyro_ctx_debug_csr_read` are documented as non-ABI but still ship in `include/pyro_rt.h`; a purist "frozen ABI" would place them behind `#ifdef PYRO_TESTING` or a separate internal header. Style/hygiene only.
- **Result-ring endianness.** Entries are written as native-endian structs; R47 specifies LE on-wire. Moot for the in-memory model binding on an LE host (ctypes reads native-matching), but flag for the Phase-2 real-DMA datapath.
- **Hand-rolled `json_get_*`.** Fragile substring scanning; safe here (NUL-terminated, bounded, PYRO-produced manifest gated by CRC+identity) but note that it assumes the fixed compact-JSON shape.
- **`AH_BODY + body + 4` size arithmetic** is overflow-safe on the 64-bit target but could wrap on a 32-bit `size_t`; portability note only.
- `c-&gt;tier` is partly redundant with `poll_tier()`; the per-scan full-input copy into staging is O(n) (behavioral model, already noted as O(n²·states)).

### Assessment

**Task quality: Approved.** The ABI 2.0.0 header matches §7.3 exactly, and `src/pyro_rt.c` is a memory-safe, UB-free, valgrind-clean, thread-safe native runtime that correctly implements the harness contract (R45 CSR + R47a identity trust boundary re-checked before every scan, R47 ring, OVF/resume, single-issue, R49 alignment, R47b integrity/compatibility/identity refusals, R44 defined-state, async lifecycle with `NOT_RESIDENT`/`SYNTH`). The hostile-artifact path is properly hardened. The ctypes conformance suite drives the real ABI comprehensively (genuine OVF-resume, wrong-hash rejection, superset-completeness, eviction, concurrency). The three §13 items are all reasonable, self-consistent binding interpretations needing only a spec sentence — none require a code change. The only cross-cutting note is that `tests/acceptance/test_ac0_8_abi_header.py` now fails by design against this tree, which is Task 8's reconciliation, not a Task-7 defect.

Relevant files: `/home/gn262/Repos/Yale/stringy-fpga/src/pyro_rt.c`, `/home/gn262/Repos/Yale/stringy-fpga/include/pyro_rt.h`, `/home/gn262/Repos/Yale/stringy-fpga/pyro/synth/artifact.py`, `/home/gn262/Repos/Yale/stringy-fpga/pyro/_native.py`, `/home/gn262/Repos/Yale/stringy-fpga/tests/unit/test_native_abi.py`, `/home/gn262/Repos/Yale/stringy-fpga/tests/c/abi_conformance.c`; and for Task 8: `/home/gn262/Repos/Yale/stringy-fpga/tests/acceptance/test_ac0_8_abi_header.py` (now asserts the historical 0x00010000).

Per your CLAUDE.md context-hygiene rule: this closes the Task 7 gate (Approved). Suggested next step — **/clear** before an unrelated task, or **/compact** if you continue on the Phase-1 thread (Task 8 will reconcile the AC-0-8 acceptance checkpoint and likely wire `_native` toward dispatch, inheriting the three §13 rulings and the endianness/test-seam notes).</result>
<usage><subagent_tokens>490837</subagent_tokens><tool_uses>11</tool_uses><duration_ms>353802</duration_ms></usage>
</task-notification>

## 2026-07-05 11:04:53

<task-notification>
<task-id>ae3f099829233696c</task-id>
<tool-use-id>toolu_016give9t19XSpxDPszREqcT</tool-use-id>
<output-file>/tmp/gn262/claude-1361/-home-gn262-Repos-Yale-stringy-fpga/af23c991-00cd-44ae-95a5-b069841d45c6/tasks/ae3f099829233696c.output</output-file>
<status>completed</status>
<summary>Agent "Author Phase 0 acceptance tests" finished</summary>
<note>A task-notification fires each time this agent stops with no live background children of its own. The user can send it another message and resume it, so the same task-id may notify more than once.</note>
<result>Done. Phase 1 acceptance suite authored, committed, smoke-checked, and reported.

**Status: DONE_WITH_CONCERNS**

**Commit:** `942a8ef` — test(phase1): acceptance suite for AC-1-1..AC-1-7 (ABI 2.0.0, synthesis, tiers)

**Suite:** 412 → **450 tests**. Files added under `tests/acceptance/`: `test_ac1_1`…`test_ac1_7`, plus helpers `abi_ctypes.py` (ABI 2.0.0 ctypes binding), `phase1_workers.py` (main-guarded worker harness for the spawn/forkserver synthesis service), `phase1_support.py` (subprocess runner). Modified: `test_ac0_8` (rescoped to ABI 2.0.0, R37) and `conftest.py` (PYRO_CACHE_DIR session temp dir + residue assertion — item 9).

**Smoke results (per brief: collection + oracle self-consistency + ABI loadability only):** 450 collected clean, no warnings; new hand-written corpora re-vs-re 0 failures; `libpyro_rt.so` loads with `pyro_abi_version()==0x00020000`, `pyro_match` sizeof 24; the 21 ABI-domain tests pass. Lifecycle-shaping smokes via public API confirmed prewarm non-blocking, mock synthesis success, warm-cache persistence across a real restart, launch bracket 50→0/1100→1, dedup, and tier equivalence.

**§13 ambiguities: 6** — most significant: `pyro_generate` takes a private L2-produced serialized-automaton **descriptor** (raw regex → `PYRO_E_INVALID`, confirmed), so the pure-ctypes resident-circuit paths (synth→load→resident scan, on-circuit NOT_RESIDENT guard, R47a identity block, register-level OVF/single-issue, R49a alignment seam) aren't constructible from public info — covered behaviorally through the Python surface and flagged; also no public device-error (R52) or synthesis-failure (R65) injection seam (recorded skips), N_synth knob unnamed, and PYRO_CACHE_DIR named by the brief not the spec.

**Report:** appended a "Phase 1 acceptance suite" section to `/home/gn262/Repos/Yale/stringy-fpga/.superpowers/sdd/task-2-report.md` (gitignored working artifact).

Two unrelated pre-existing modifications (`.claude/settings.json`, `docs/prompts.md`) were left unstaged and untouched.

This is a natural checkpoint — recommend **/clear** before the next (unrelated) task.</result>
<usage><subagent_tokens>301400</subagent_tokens><tool_uses>39</tool_uses><duration_ms>1177276</duration_ms></usage>
</task-notification>

## 2026-07-05 11:10:19

<task-notification>
<task-id>ad19850c86105fe5d</task-id>
<tool-use-id>toolu_019FtdXRVb1pwJpr6hgXmRGf</tool-use-id>
<output-file>/tmp/gn262/claude-1361/-home-gn262-Repos-Yale-stringy-fpga/af23c991-00cd-44ae-95a5-b069841d45c6/tasks/ad19850c86105fe5d.output</output-file>
<status>completed</status>
<summary>Agent "Write Python→FPGA regex offload spec" finished</summary>
<note>A task-notification fires each time this agent stops with no live background children of its own. The user can send it another message and resume it, so the same task-id may notify more than once.</note>
<result>Numbering is contiguous (R1–R69), all references resolve. **v2.0.5** complete — file edited in place, not committed.

## Rulings summary

**Root cause addressed:** added a normative **§9.1 "Public test/verification seams"** and updated §9's premise line, so no AC requires reading internals.

1. **Device-error + synthesis-failure injection → R67 (NEW obligation).** Defined a `pyro.testing` namespace: `inject_device_error(kind, count)` (drives R52/R61), `inject_synth_failure(pattern, flags)` (drives R65), `inject_false_positive(pattern, flags, count)` (drives R19 re-verification), and `reset()`. Deterministic, result-preserving (never changes output vs CPython), stats-observable, and gated behind `PYRO_ENABLE_TEST_HOOKS=1` (R35a sampling) to avoid production foot-guns. **Unblocks AC-1-6.**

2. **N_synth knob → `PYRO_N_SYNTH` in R68 (NEW obligation).** Names the override of the R4a launch threshold; tests can set a small value to pin the exact launch boundary. Sampled at R35a points.

3. **`PYRO_CACHE_DIR` → R68 (blessing).** Codified the already-honored cache-location env var in R4/§9.1 so harnesses can isolate cache hygiene.

4. **Descriptor format → ruled option (a) (R69, blessing).** For device-free/`model://` bindings, resident-tier register guarantees (OVF, single-issue, R47a identity, R49a alignment) that need a constructed resident circuit MAY be verified **behaviorally through the public Python surface** (AC-1-3/1-4/1-7); pure-ctypes tests cover everything publicly constructible. Explicitly did **not** freeze the descriptor (b) or require a test-vector artifact (c); added a §12 out-of-scope bullet stating the descriptor is an intentionally-unfrozen L2↔L3 private contract.

5–6. **Process residue → strengthened R63e:** the service MUST clean up worker processes on ctx teardown / interpreter shutdown so tests can assert no residual processes. AC-1-5 now asserts this.

## New implementation obligations (flagged for follow-up)

- **R67** — the `pyro.testing` injection namespace + `PYRO_ENABLE_TEST_HOOKS` gate.
- **R68** — the `PYRO_N_SYNTH` override knob.

Everything else (`PYRO_CACHE_DIR`, R69 behavioral-coverage scope, R63e cleanup) blesses or clarifies existing behavior. The coordinator should route R67 and `PYRO_N_SYNTH` to the coder as a small Phase-1 follow-up before the acceptance suite can flip its recorded skips (AC-1-5, AC-1-6) to live assertions.

Wired references: R52, R61, R65 now point at the R67 seam; R4/R4a name the R68 knobs; AC-1-1/1-2 cite R69; AC-1-5/1-6 cite R67/R68.</result>
<usage><subagent_tokens>214000</subagent_tokens><tool_uses>27</tool_uses><duration_ms>277431</duration_ms></usage>
</task-notification>

## 2026-07-05 11:12:21

<task-notification>
<task-id>bgf2lw6dm</task-id>
<tool-use-id>toolu_019NBKBX7354THEzha2XkD91</tool-use-id>
<output-file>/tmp/gn262/claude-1361/-home-gn262-Repos-Yale-stringy-fpga/af23c991-00cd-44ae-95a5-b069841d45c6/tasks/bgf2lw6dm.output</output-file>
<status>completed</status>
<summary>Background command "Run full 450-test Phase 0+1 acceptance suite" completed (exit code 0)</summary>
</task-notification>

## 2026-07-05 11:14:19

<task-notification>
<task-id>ae3f099829233696c</task-id>
<tool-use-id>toolu_01NDJBRso8ayFckS88ooV5p9</tool-use-id>
<output-file>/tmp/gn262/claude-1361/-home-gn262-Repos-Yale-stringy-fpga/af23c991-00cd-44ae-95a5-b069841d45c6/tasks/ae3f099829233696c.output</output-file>
<status>completed</status>
<summary>Agent "Author Phase 0 acceptance tests" finished</summary>
<note>A task-notification fires each time this agent stops with no live background children of its own. The user can send it another message and resume it, so the same task-id may notify more than once.</note>
<result>Fixed and committed.

**Commit:** `7e36227` — test(phase1): fix JSON tuple/list asymmetry in tier-equivalence comparison

**Confirmation:** The two failures were a test-harness defect, not implementation behavior — the `disabled` tier serves pure stock `re`, and the mismatch was purely `findall`'s multi-group **tuples** (in-process oracle) vs **lists** (worker result crossing JSON). Fix: added `phase1_support.jsonify()` that recursively coerces tuples→lists, applied symmetrically to **both** sides of the cross-process comparison. Re-ran the affected file: **9 passed** (0 failures).

**Audit result:** No other sites affected. The only other `run_worker` equality comparisons are in `test_ac1_5` (AC-1-5), all scalars (`==0`) plus `read["cache_files"] == write["cache_files"]` where both sides are `os.listdir` string lists arriving via JSON — already type-symmetric, no tuples involved. The in-process `test_inprocess_tier_equivalence` compares tuple-vs-tuple within one process (no JSON boundary), so it was never affected.

Expected full-suite outcome now: 447 passed, 3 sanctioned skips.</result>
<usage><subagent_tokens>307483</subagent_tokens><tool_uses>4</tool_uses><duration_ms>86700</duration_ms></usage>
</task-notification>

## 2026-07-05 11:21:56

<task-notification>
<task-id>ae070ceabd46bc2d4</task-id>
<tool-use-id>toolu_018oHqGUdPpqT1WHvNNEXRTG</tool-use-id>
<output-file>/tmp/gn262/claude-1361/-home-gn262-Repos-Yale-stringy-fpga/af23c991-00cd-44ae-95a5-b069841d45c6/tasks/ae070ceabd46bc2d4.output</output-file>
<status>completed</status>
<summary>Agent "Implement Task 6: synthesis service" finished</summary>
<note>A task-notification fires each time this agent stops with no live background children of its own. The user can send it another message and resume it, so the same task-id may notify more than once.</note>
<result>Done — R67/R68 follow-up delivered on `phase1-pyro`.

Commit: `d5a19e3` pyro: R67 pyro.testing fault-injection seam + R68 PYRO_N_SYNTH knob

- **R67** `pyro.testing` (gated by cached `PYRO_ENABLE_TEST_HOOKS`, sampled at R35a points; importable-but-inert when off, zero hot-path cost): `inject_device_error`→R52 retry, `inject_false_positive`→spurious windows re-verified away (R19, never leak), `inject_synth_failure`→deterministic R65 permanent fallback, `reset()`. All deterministic, result-preserving vs CPython, observable via `stats()`, not patched onto `re`.
- **R68** `PYRO_N_SYNTH` sampled/validated in `_route.sample_env` (positive int, invalid ignored→default 1000), pushed to the live residency manager at each sampling point, never read on the hot path.
- **R63e** strengthened: added subprocess test asserting no residual worker survives interpreter shutdown.

Results: `pytest tests/unit -q` → **485 passed**; no `~/.cache/pyro` residue, no lingering forkserver worker; required Phase-0 acceptance files (ac0_5/6/2c/6b) green and AC-0-6 routing budget still met. New tests: `test_testing_hooks.py` (10), `test_env_knobs.py` (6), +1 R63e teardown. Report appended at `/home/gn262/Repos/Yale/stringy-fpga/.superpowers/sdd/task-6-report.md`. (Note: an unrelated unstaged `docs/prompts.md` change was left untouched — out of scope.)</result>
<usage><subagent_tokens>321945</subagent_tokens><tool_uses>33</tool_uses><duration_ms>665802</duration_ms></usage>
</task-notification>

## 2026-07-05 11:39:26

<task-notification>
<task-id>ae3f099829233696c</task-id>
<tool-use-id>toolu_011J59dTHayU9YorC1J5fYA4</tool-use-id>
<output-file>/tmp/gn262/claude-1361/-home-gn262-Repos-Yale-stringy-fpga/af23c991-00cd-44ae-95a5-b069841d45c6/tasks/ae3f099829233696c.output</output-file>
<status>completed</status>
<summary>Agent "Author Phase 0 acceptance tests" finished</summary>
<note>A task-notification fires each time this agent stops with no live background children of its own. The user can send it another message and resume it, so the same task-id may notify more than once.</note>
<result>Done. All three recorded skips are flipped to live seam-driven tests.

**Commit:** `2b3db17` — test(phase1): flip recorded skips to live tests via v2.0.5 seams (R67/R68/R69)

**New test count:** 457 collected, **457 passed, 0 skipped** (was 456 passed / 1 skip). No unconditional skips remain.

**Confirmation — all 3 skips flipped to live tests:**
1. **AC-1-6** (`test_ac1_6`): live `pyro.testing.inject_device_error` (device + timeout → R52 fallback-retry, CPython-identical results, `fallback_after_error` and `device_errors` each +count), `inject_false_positive` (no-leak result-identity), `reset()`, and gate-off inertness. Hooks enabled in-process via `refresh_env` (R35a sampling point).
2. **AC-1-5** (`test_ac1_5`): `PYRO_N_SYNTH=3` pins the boundary exactly (2→0 launches, 3rd→exactly 1); live `inject_synth_failure` → R65 permanent fallback (synth_failed counted, no exception, result identical); `PYRO_CACHE_DIR` isolation; R63e no-residual-process teardown via `start_new_session` + empty-process-group check.
3. **`test_ac0_6b`**: R61 Phase-0 skip flipped to a live seam-driven device-error fallback-retry test.

**Smoke/run results:** collection clean (457); AC-1-5 (10) + AC-1-6 (11) + ac0_6b (2) pass locally; **full suite: 457 passed, 0 skipped, 7 min, exit 0** — confirming hermeticity/order-independence with the new `PYRO_ENABLE_TEST_HOOKS`/`PYRO_N_SYNTH` conftest management and `pyro.testing.reset()` teardown.

**Two spec adjudications made during this work** (both in the report §13): the false-positive test asserts only the airtight **no-leak result-identity** invariant (R19a) — R52/R66 don't pin *which* counter a re-verified-away window moves, and the impl routes it through the R52 path variably by API (device_errors/fallback_after_error). The only remaining scope note (not a skip, sanctioned by **R69**) is that resident-tier register-level guarantees are verified behaviorally through the Python surface since the `pyro_generate` descriptor is deliberately L2-private.

Report updated: `/home/gn262/Repos/Yale/stringy-fpga/.superpowers/sdd/task-2-report.md` (v2.0.5 seam section appended).

This is a natural checkpoint — recommend **/clear** before the next (unrelated) task.</result>
<usage><subagent_tokens>360911</subagent_tokens><tool_uses>38</tool_uses><duration_ms>1028586</duration_ms></usage>
</task-notification>

## 2026-07-05 11:46:59

<task-notification>
<task-id>bdzgux5zp</task-id>
<tool-use-id>toolu_014hTpaQm5XR8whFQHmAujsy</tool-use-id>
<output-file>/tmp/gn262/claude-1361/-home-gn262-Repos-Yale-stringy-fpga/af23c991-00cd-44ae-95a5-b069841d45c6/tasks/bdzgux5zp.output</output-file>
<status>failed</status>
<summary>Background command "Controller confirmation run: acceptance + unit + abi-check" failed with exit code 1</summary>
</task-notification>

## 2026-07-05 11:48:13

<task-notification>
<task-id>a251df5e1df415d44</task-id>
<tool-use-id>toolu_01Vf492SA7KkdtbWf2YaHJnh</tool-use-id>
<output-file>/tmp/gn262/claude-1361/-home-gn262-Repos-Yale-stringy-fpga/af23c991-00cd-44ae-95a5-b069841d45c6/tasks/a251df5e1df415d44.output</output-file>
<status>completed</status>
<summary>Agent "Final whole-branch review" finished</summary>
<note>A task-notification fires each time this agent stops with no live background children of its own. The user can send it another message and resume it, so the same task-id may notify more than once.</note>
<result>Verification complete. I read the six spec amendments, the Phase-1 ACs, §7.4/§7.5/§8, the correctness-critical implementation (circuit model, router, native glue, model fault seams), and empirically stress-tested the headline deliverable.

# PYRO Phase 1 — Whole-Branch Review (6fc39d8..2b3db17)

### Strengths

- **The circuit model executes the actual generated automaton**, not a re-derivation (`_circuit_model._scan_windows` walks `hdl.automaton` edges). This is the right design (Knuth): a generator lowering bug surfaces as a missing/extra window rather than being masked. The R19 over-approximation architecture (superset recognizer + mandatory host re-verification, advisory `verified` bit that never relieves re-verification) is principled and correctly documented.
- **ABI 2.0.0 rescope is clean and coherent.** `pyro_abi_version()==0x00020000`; AC-0-8's `0x00010000` is preserved as a historical Phase-0 checkpoint against the retained stub, with the spec R37 note and the test file both handling the split correctly. No dangling 1.0.0 references.
- **Env-knob discipline preserved.** `PYRO_ENABLE_TEST_HOOKS`/`PYRO_N_SYNTH` are sampled at the R35a points into cached snapshots (`_route.sample_env`), never on the hot path; `apply_n_synth` is pushed to the residency manager only if already imported, so package init and the fallback fast path stay thin (R3a/R5 intact).
- **Spec amendments are well-formed and internally consistent.** 2.0.0 is correctly the sole MAJOR; 2.0.1–2.0.5 are PATCH clarifications/blessings with accurate cross-references (R47a hash inputs, R4b cache-key scope, R51b device-free precedence, R47c/R49a/R40a rulings, R67–R69 seams). R51b genuinely reconciles the old R51 step 5 with the R7 device-free invariant.
- **Fault-injection seams (R67)** are gated, inert-until-armed, deterministic, and result-preserving; the false-positive injector uses an unverifiable window (`end = len(buf)+1`) so R19 re-verification provably drops it. Good.

### Issues

#### Critical (Must Fix)

**C1. `_circuit_model.group0_finditer` is NOT byte-identical to stock `re.finditer` — it drops real non-empty matches (R22/R19/R16), falsifying AC-1-3's central claim.** `pyro/_circuit_model.py:355-371`. After an empty match at position *p*, the loop does `pos = span[1] + 1` and advances past *p*. But CPython's post-3.7 `finditer` sets `must_advance` and **retries at the same *p* demanding a non-empty match** before moving on. Demonstrated false negatives (dropped matches — the one error class R19 forbids):

```
'a??' on 'aa' : circ=[(0,0),(1,1),(2,2)]  stock=[(0,0),(0,1),(1,1),(1,2),(2,2)]  DROPPED=[(0,1),(1,2)]
'.*?' on 'ab' : same shape, DROPPED=[(0,1),(1,2)]
```

A 4000-case differential fuzz over the §5.1 grammar produced 16+ divergences, all lazy/optional-quantifier patterns that prefer an empty match where a non-empty match also starts. AC-1-3 states "results from the model circuit are byte-identical to stock `re`" and AC-1-7 asserts cross-tier byte-identity; the circuit model violates both.

**Blast radius (important mitigation):** this is **not user-facing in Phase 1**. `_route._model_finditer` dispatches through the Phase-0 `_model` (which delegates to `stock.finditer` internally), so `pyro.re.finditer('a??','aa')` returns the correct 5-tuple (verified empirically, counted as `model`). The bug lives only in `group0_finditer`, exercised by the AC-1-7 keystone, unit tests, and the native artifact evaluator — never by production dispatch. But it is the phase's headline deliverable and the designated Phase-2/3 dispatch path; wiring it in later without fixing this ships silent false negatives.

**Fix:** since the host already holds stock `re` and re-verifies anyway, drive finditer enumeration with `stock.finditer(subject)` for the final spans and use the automaton's candidate-start set only to *assert* completeness (R19) — rather than hand-reimplementing the `must_advance` scanner, which is exactly where the bug lives.

**C2. Test-coverage gap masks C1: AC-1-3's "model circuit" differential never exercises the generated circuit for finditer equivalence.** `tests/acceptance/test_ac1_3_generator_differential.py:test_construct_byte_identical_on_model` and `test_one_mib_corpus_byte_identical` run through `pyro.re` under `PYRO_FORCE_MODEL`, which routes to the Phase-0 `_model` (stock-re-delegating), **not** `_circuit_model`. So the acceptance suite validates the wrong component and would pass even if the generated automaton were badly broken. The AC-1-7 keystone *does* call `group0_finditer` directly, but its corpus (`test_ac1_7_keystone.py:21`) contains no lazy-empty-preferring pattern (`a*` is greedy; `colou?r`/`\w+` never prefer empty), so it never triggers `must_advance`. Add lazy-optional patterns (`a??`, `.*?`, `x??`) to both corpora and route at least one differential test through `group0_finditer` directly.

#### Important (Should Fix)

**I1. `_native.py:7` docstring is false and the coordinator's own description inherits the error.** It states the circuit model "**remains the dispatch path**." It does not: `_route._model_single/_model_finditer/_get_prog` all call `_model.get_model()` (Phase-0). The circuit model is wired into **no** dispatch path — only `_native.py` was disclosed as unwired, but `_circuit_model` is equally unwired. This is a Rams/Kant honesty defect: the architecture doc misrepresents which component serves results. Correct the docstring to say the Phase-0 `_model` remains the dispatch model and the circuit model is validated out-of-band, and surface this integration gap explicitly to the human (the "model" dispatches counted in `stats()` do **not** execute the generated automaton).

**I2. Debug seams remain unguarded in the frozen public header** (deferred item i). `include/pyro_rt.h:111,119` export `pyro_ctx_debug_force_misalign` (a state-mutating test hook) and `pyro_ctx_debug_csr_read` with only a comment disclaiming ABI membership. A "frozen" public ABI carrying a test-only mutator is a categorical inconsistency (Kant) — a comment does not stop a caller from linking it. One-line fix: wrap both in `#ifdef PYRO_TESTING`. Cheap enough to do now.

#### Minor

- **M1.** `_circuit_model.scan` (`:243-247`) comments that it honors the R49 64-byte alignment check but only validates `out_cap &gt;= 0`, delegating real alignment to the native binding — fine for the Python model, but the comment overstates it; align the comment with R49a (the C binding returns `PYRO_E_INVALID`; the Python model does not check DMA alignment because buffers are Python-managed).
- **M2.** `_circuit_model.py:34` imports `re._constants` — consistent with the R26/P7 ≥3.11 pin, good, but worth a one-line P7 reference in the module header as `_classify.py` has.
- **M3.** `group0_finditer` recompiles the stock pattern on every call (`_stock_compile(circuit.pattern, circuit.flags)`, `:345`); for the ≥1 MiB reuse regime this should be cached on the circuit. Minor since it's off the dispatch path today.

### Deferred-Minors Triage (i–vii)

- **(i) debug seams in frozen header — fix now.** One-line `#ifdef PYRO_TESTING`; a frozen ABI should not export test mutators (see I2).
- **(ii) native-endian result ring in C model binding — defer to Phase 2.** LE is normative only for real DMA; the model binding round-trips within one host. Correct deferral; add a Phase-2 TODO at the write/read site.
- **(iii) hand-rolled JSON manifest parser shape assumptions — defer, with a guard.** Acceptable for mock-toolchain-produced manifests, but before Phase 2 ingests real Vivado output, harden or replace with `json` + schema validation. Defer, ticket it.
- **(iv) 32-bit size_t overflow note — defer to Phase 2.** Target host is x86-64; document the assumption. Fine.
- **(v) double UTF-8 encode on ≥64 KiB model path — defer.** Same cosmetic redundancy I flagged in Phase 0; amortized on the model path. Phase 2.
- **(vi) eager finditer list on model path — defer.** Already commented as an N3 Phase-1 line item; not a correctness issue. Phase 2.
- **(vii) RTL anchor placeholders (at_eol=at_eob=in_last, res_start=0) — defer to Phase 2 (correctly), but verify the model is authoritative.** The Verilog is not executed in Phase 1 (model is authoritative), so these stubs are inert now. **Condition:** Phase 2 must not bring the RTL onto the result path until these anchors are real, or C1's class of anchor/empty bugs will surface in hardware. Flag prominently in the Phase-2 entry criteria.

### Recommendations

1. Fix **C1** by delegating finditer span enumeration to `stock.finditer` with an automaton-completeness cross-check, and **C2** by adding lazy-empty patterns (`a??`, `.*?`, `x??`, `\d??`) to the AC-1-3 and AC-1-7 corpora plus a direct-through-`group0_finditer` differential. These are contained, non-architectural.
2. Correct the **I1** docstring and state plainly, in the merge hand-off to the human, that Phase 1 dispatch still runs the Phase-0 model; the generated circuit is delivered and validated but not yet on the dispatch path (as with `_native`).
3. Apply the **I2 / (i)** `#ifdef PYRO_TESTING` guard now.
4. Add a Phase-2 entry-criterion checklist item: circuit-model finditer must be `must_advance`-correct **and** RTL anchors (vii) must be real before the generated circuit or RTL serves any user result.

### Assessment

**Ready to merge? With fixes.**

**Reasoning:** User-facing correctness holds in Phase 1 (dispatch runs the byte-identical Phase-0 model, verified empirically), the ABI/spec/synthesis scaffolding is strong, and the amendments are coherent — but the phase's headline deliverable, the "CPython-exact" circuit-model `finditer`, provably drops real matches on lazy-empty patterns (C1), and the AC-1-3/AC-1-7 tests pass only because they exercise the wrong component or a gap-free corpus (C2); fix those two contained items (plus the false `_native` docstring and the one-line header guard) and this merges.

Key files: `/home/gn262/Repos/Yale/stringy-fpga/pyro/_circuit_model.py` (C1, `group0_finditer:335-371`), `/home/gn262/Repos/Yale/stringy-fpga/tests/acceptance/test_ac1_3_generator_differential.py` and `/home/gn262/Repos/Yale/stringy-fpga/tests/unit/test_ac1_7_keystone.py` (C2), `/home/gn262/Repos/Yale/stringy-fpga/pyro/_route.py` (dispatch reality), `/home/gn262/Repos/Yale/stringy-fpga/pyro/_native.py` (I1 docstring), `/home/gn262/Repos/Yale/stringy-fpga/include/pyro_rt.h` (I2 debug seams).</result>
<usage><subagent_tokens>241637</subagent_tokens><tool_uses>19</tool_uses><duration_ms>479083</duration_ms></usage>
</task-notification>

## 2026-07-05 11:51:48

<task-notification>
<task-id>ae3f099829233696c</task-id>
<tool-use-id>toolu_01TzEx8bxiUARDmBBS3Tmmrp</tool-use-id>
<output-file>/tmp/gn262/claude-1361/-home-gn262-Repos-Yale-stringy-fpga/af23c991-00cd-44ae-95a5-b069841d45c6/tasks/ae3f099829233696c.output</output-file>
<status>completed</status>
<summary>Agent "Author Phase 0 acceptance tests" finished</summary>
<note>A task-notification fires each time this agent stops with no live background children of its own. The user can send it another message and resume it, so the same task-id may notify more than once.</note>
<result>Done.

**Commit:** `c047a5d` — test: cover CPython must_advance lazy/empty-preferring finditer semantics (R22)

**New test count:** 457 → **506** (+49).

**What was added:**
- `oracle.MUST_ADVANCE`: 14 hand-written §5.1-legal lazy/empty-preferring cases — `a??`, `.*?`, `a*?`, `\d*?`, `.??`, empty-branch alternations `(a|)??` / `(?:a|)??`, empty-preferring-then-atom `a*?b?` / `a??b`, and group forms `(a??)(b?)` — each on subjects where an empty and a non-empty match coexist at the same start (the must_advance retry path greedy `a*` doesn't stress).
- Wired into three corpora asserting finditer/findall/sub/split byte-identity: **AC-1-3** model-path differential (all 8 APIs), **AC-0-7** empty-match differential (both routing modes), and **AC-1-7** tier-equivalence (in-process CASES + subprocess cold/model/resident).

**Smoke results (per brief — collection + oracle self-consistency only):**
- Oracle self-consistency: MUST_ADVANCE re-vs-re (stock re both sides) **0 failures**; verified the exact facts (e.g. `finditer('a??','aa')` → `[(0,0),(0,1),(1,1),(1,2),(2,2)]`); jsonify tuple→list/None round-trip confirmed for the subprocess tier-equivalence boundary.
- Collection: **506 tests**, clean.

I did not run these against the implementation, per the brief (the parallel fix for this bug class is landing; controller runs the full suite). Report updated at `/home/gn262/Repos/Yale/stringy-fpga/.superpowers/sdd/task-2-report.md` is unchanged for this increment — the change is self-describing in the commit.</result>
<usage><subagent_tokens>373092</subagent_tokens><tool_uses>9</tool_uses><duration_ms>162226</duration_ms></usage>
</task-notification>

## 2026-07-05 11:57:23

<task-notification>
<task-id>a3715be0b147ee31b</task-id>
<tool-use-id>toolu_01Uo1KvWM39g9GUqwSQw8kjq</tool-use-id>
<output-file>/tmp/gn262/claude-1361/-home-gn262-Repos-Yale-stringy-fpga/af23c991-00cd-44ae-95a5-b069841d45c6/tasks/a3715be0b147ee31b.output</output-file>
<status>completed</status>
<summary>Agent "Implement Task 5: HDL generator" finished</summary>
<note>A task-notification fires each time this agent stops with no live background children of its own. The user can send it another message and resume it, so the same task-id may notify more than once.</note>
<result>Fix round 2 complete on `phase1-pyro`. I reproduced C1 first: `group0_finditer` dropped the `must_advance` non-empty spans CPython yields (`a??` on `"aa"` → mine `[(0,0),(1,1),(2,2)]` vs stock `[(0,0),(0,1),(1,1),(1,2),(2,2)]`), confirmed for `.*?`/`x??`/`\d??` too — R19 false negatives.

Commit: `c16ecab` pyro Phase 1a fixes (round 2): finditer must_advance, frozen-ABI test seams

What changed: **C1** `group0_finditer` now delegates span enumeration to `stock.finditer` (the must_advance oracle) and keeps the generated automaton as the R19 completeness oracle (`CompletenessError` on a missed start — honesty preserved). **C2** lazy-empty patterns added to the keystone corpus + a direct grammar differential + a completeness-defect test. **I1** `_native.py` docstring corrected (wired into no dispatch path). **I2** debug seams wrapped in `#ifdef PYRO_TESTING` (header+src) with Makefile `TESTFLAGS` on `lib`/`valgrind` but not `abi-check`. **M1/M2/M3** R49a comment, P7 note, cached stock compile.

Results: `tests/unit` **519 passed**; `make abi-check` OK (0x00020000); `make valgrind` **0 errors, no leaks**; native+ABI pytest 38 passed; frozen production build exports **0** mutator symbols (test lib 2); 4000-case lazy-quantifier fuzz **0 completeness failures, 0 divergences**. Fix report appended to `/home/gn262/Repos/Yale/stringy-fpga/.superpowers/sdd/task-5-report.md`. (Left `docs/prompts.md` — a session-hook auto-edit — unstaged.)</result>
<usage><subagent_tokens>276798</subagent_tokens><tool_uses>36</tool_uses><duration_ms>508978</duration_ms></usage>
</task-notification>

## 2026-07-05 12:00:22

<task-notification>
<task-id>a251df5e1df415d44</task-id>
<tool-use-id>toolu_01G5FoxLDunesEUiHUasscRb</tool-use-id>
<output-file>/tmp/gn262/claude-1361/-home-gn262-Repos-Yale-stringy-fpga/af23c991-00cd-44ae-95a5-b069841d45c6/tasks/a251df5e1df415d44.output</output-file>
<status>completed</status>
<summary>Agent "Final whole-branch review" finished</summary>
<note>A task-notification fires each time this agent stops with no live background children of its own. The user can send it another message and resume it, so the same task-id may notify more than once.</note>
<result>Verified. Both hunt targets resolved and all dispositions confirmed.

### Findings

**C1 fix — correct and complete.** `group0_finditer` (`pyro/_circuit_model.py`) now delegates span enumeration to `stock.finditer` (the R22 `must_advance` oracle) while executing the generated automaton as the R19 completeness oracle. My reproducers (`a??`, `.*?`, `x??`) and a 6000-case lazy-quantifier fuzz across flags: **0 divergences, 0 spurious CompletenessErrors**.

**Hunt target 1 — the automaton is not skippable.** `cand = set(candidate_starts(circuit, subject))` runs unconditionally before the span loop; I instrumented it and confirmed `candidate_starts` (which calls `_scan_windows` over the automaton edges) fires exactly once even when `stock.finditer` yields **zero** matches. There is no short-circuit. The honesty property is genuinely enforced: `test_group0_finditer_surfaces_a_completeness_defect` corrupts the automaton (drops the start state's edges) and asserts `CompletenessError` raises — so a real generator lowering bug still surfaces rather than silently dropping a match.

**Hunt target 2 — the PYRO_TESTING guard breaks nothing.** I ran both targets via bash: `make abi-check` compiles `src/pyro_rt.c` clean under `-Wall -Werror` **without** `-DPYRO_TESTING`, reports `0x00020000`, and its binary exports **0** debug symbols; `make lib` builds **with** `-DPYRO_TESTING` and exports exactly the 2 mutators the tests need. The `#ifdef` is properly closed in both header and src (before the `extern "C"` brace); the frozen ABI is self-contained. `abi-check` correctly omits `TESTFLAGS`; `lib`/`valgrind` include it.

**C2 wiring real.** `oracle.MUST_ADVANCE` (14 lazy-empty cases) is parametrized into AC-0-7 and AC-1-3 acceptance corpora; unit side adds a direct-through-`group0_finditer` grammar differential (runs the automaton), a lazy-empty byte-identity test, and the completeness-defect test above.

**I1/I2/M1/M3 confirmed.** `_native.py` docstring now states plainly that neither the circuit model nor the native binding is on any dispatch path and the Phase-0 `_model` serves dispatch. `_scan` alignment comment corrected to R49a semantics; stock compile is `lru_cache`d (M3).

No half-done work, no regressions, no build breakage.

**Ready to merge? Yes** — the two hunt risks are both closed (the automaton executes unconditionally as the R19 completeness oracle and cannot be short-circuited; the frozen ABI build excludes the test mutators while the test build includes them), the `must_advance` correctness bug is fixed with fuzz-backed evidence, and the coverage gap that masked it is filled with genuine circuit-model differential and completeness-defect tests.

Relevant files: `/home/gn262/Repos/Yale/stringy-fpga/pyro/_circuit_model.py`, `/home/gn262/Repos/Yale/stringy-fpga/include/pyro_rt.h`, `/home/gn262/Repos/Yale/stringy-fpga/src/pyro_rt.c`, `/home/gn262/Repos/Yale/stringy-fpga/Makefile`, `/home/gn262/Repos/Yale/stringy-fpga/pyro/_native.py`, `/home/gn262/Repos/Yale/stringy-fpga/tests/acceptance/oracle.py`, `/home/gn262/Repos/Yale/stringy-fpga/tests/unit/test_circuit_model.py`.</result>
<usage><subagent_tokens>262125</subagent_tokens><tool_uses>8</tool_uses><duration_ms>145545</duration_ms></usage>
</task-notification>

## 2026-07-05 12:04:42

<task-notification>
<task-id>buob3vl7l</task-id>
<tool-use-id>toolu_01AZxKxLjDa1ZZg5McPTHFEq</tool-use-id>
<output-file>/tmp/gn262/claude-1361/-home-gn262-Repos-Yale-stringy-fpga/af23c991-00cd-44ae-95a5-b069841d45c6/tasks/buob3vl7l.output</output-file>
<status>completed</status>
<summary>Background command "Final full acceptance run (506 tests)" completed (exit code 0)</summary>
</task-notification>

## 2026-07-05 23:15:45

Explain Phase 2

## 2026-07-05 23:27:56

Carry out Phase 2 and then Phase 3.

## 2026-07-05 23:29:18

Carry out Phase 2 using all the agents as well as /knuthramasant

## 2026-07-05 23:53:01

<task-notification>
<task-id>bos2rorek</task-id>
<tool-use-id>toolu_01FGiFokBBJwwCUT7jKBR5BE</tool-use-id>
<output-file>/tmp/gn262/claude-1361/-home-gn262-Repos-Yale-stringy-fpga/af23c991-00cd-44ae-95a5-b069841d45c6/tasks/bos2rorek.output</output-file>
<status>completed</status>
<summary>Background command "Probe xcu250 part+license in Vivado 2023.1 and 2025.2 with libtinfo shim" completed (exit code 0)</summary>
</task-notification>

## 2026-07-06 00:00:56

<task-notification>
<task-id>a7b944a27850c40eb</task-id>
<tool-use-id>toolu_018TBbtsp8RQBugVxsHATq1G</tool-use-id>
<output-file>/tmp/gn262/claude-1361/-home-gn262-Repos-Yale-stringy-fpga/af23c991-00cd-44ae-95a5-b069841d45c6/tasks/a7b944a27850c40eb.output</output-file>
<status>completed</status>
<summary>Agent "Spec-writer: Phase 2 rulings" finished</summary>
<note>A task-notification fires each time this agent stops with no live background children of its own. The user can send it another message and resume it, so the same task-id may notify more than once.</note>
<result>The spec is amended, internally consistent, and version-bumped. Here is the summary of every ruling for briefing the coder and test-developer.

## Deliverable
`/home/gn262/Repos/Yale/stringy-fpga/specs/python-regex-offload.md` — bumped **2.0.5 → 2.1.0** (MINOR: added requirements, no interface/AC break). New content: **§7.6** (rulings R70–R77 except R76), **§10.1** (R76), amended **AC-2-1..AC-2-6**, the Phase-2 intro, **R50/P2**, **R68**, **§11 P1**, and a **§14 changelog** entry.

## Rulings

- **R70 / R70a — toolchain-selection knobs.** `PYRO_TOOLCHAIN=mock|vivado` (default `mock`; unrecognized ⇒ `mock`, fail-safe) and `PYRO_VIVADO=&lt;install dir&gt;` (no default, no library-side `PATH`/filesystem/`XILINX_VIVADO` scanning; unset/unresolvable ⇒ `toolchain_present=false`). Both sampled at the R35a points, never on the hot path; also registered in R68. Selection crosses the worker boundary via **additive** `ToolchainConfig` fields (kind, vivado dir, part `xcu250-figd2104-2L-e`, target clock MHz, per-job timeout), all with mock-preserving defaults so `ToolchainConfig()` is byte-identical to pre-2.1.0.

- **R71 — partial-P1 live/SKIP matrix (normative).** Three probed predicates: `toolchain_present` (true here), `pr_flow_present` (false), `device_usable` (false — third-party live NIC). Normative per-clause table. **LIVE:** real OOC synth+P&amp;R, honest manifest metrics, cache warm-reload, AC-2-4 calibration, real-synth-failure→permanent-fallback. **SKIP** (reason names the absent prerequisite): every PR-bitstream and on-device clause. SKIP is never PASS; PASS only from real execution. A single shared availability probe should expose the three predicates.

- **R72 / R72a–c — payload honesty.** The `vivado` payload stays the model-exec `PYROART1` container (per R51b). Honesty mechanism = **new manifest `payload_kind` string field** (JSON-sidecar only; the ABI-frozen `PYROART1` header and FORMAT_VERSION 1 are untouched). Values: `"mock_stub"` (**default** — preserves JSON round-trip for existing manifests), `"ooc_metrics"` (real post-route metrics, not a device bitstream), reserved `"pr_bitstream"`. The PR loader treats **only** `pr_bitstream` as device-loadable. `ooc_metrics` manifests MUST carry genuine post-route `luts/ffs/fmax_mhz/met_timing` (not the estimator's numbers).
  - *Judgment call:* chose a manifest field over a `circ_flags` bit because `circ_flags` is baked into the frozen artifact header and `CIRC_FLAGS` register; a JSON field is strictly additive and defaulted, so it is the least invasive honest mechanism.

- **R73 — AC-2-1 timing proxy.** 250 MHz (4.000 ns) OOC clock constraint on `pyro_circuit`; `met_timing := (post-route WNS ≥ 0)`, `fmax_mhz := 1000/(4.000 − WNS_ns)` from `report_timing_summary`. `met_timing==false` ⇒ synthesis failure ⇒ R65 permanent fallback.

- **R74 — AC-2-4 pre-registered margin.** Constant `ESTIMATOR_CALIBRATION_MARGIN`, fixed before calibration data: per successfully-synthesized pattern, `real_luts ≤ est_luts` AND `real_ffs ≤ est_ffs` AND `est_luts ≤ 10·max(1,real_luts)` AND `est_ffs ≤ 10·max(1,real_ffs)`.
  - *Judgment call:* used **10×** (per the task ruling) rather than the plan's 8×; the task text is authoritative and 10× is the looser, safer ceiling for pre-registration.

- **R75 / R75a — `toolchain_version` encoding.** `vivado` reports `(YY&lt;&lt;24)|(RR&lt;&lt;16)|build`; 2023.1 ⇒ `0x17010000`, distinct from mock `0x00000100`. `SHELL_VERSION` stays the model-harness value `0x0A000001` until a real PR flow exists. Mock/vivado cache-key separation falls directly out of the existing R4 key (no new mechanism).

- **R76 — §10.1 disposition.** The R50/P2 "control-frame format (Phase 1 §10.1)" dangling reference is dispositioned as **deferred to Phase-2b/3 hardware enablement** (safe because all device clauses SKIP). Added §10.1 stub; repaired R50 and P2 to cite it. Frame format deliberately **not invented** now.

- **R77 — `vivado` per-job timeout.** The adapter enforces its own subprocess timeout and **kills the Vivado process tree** on expiry ⇒ `SynthesisFailed` ⇒ R65; default `VIVADO_JOB_TIMEOUT = 1800 s` (30 min), overridable via `ToolchainConfig`. The R63e service reaper remains a bookkeeping backstop; R63 asynchrony unchanged.

## AC wording amendments
AC-2-1 (real OOC synth LIVE; PR-bitstream sub-clause SKIP), AC-2-2 (full SKIP, device+pr_flow; correctness covered by AC-1-3 on model), AC-2-3 (cold→warm LIVE, warm→resident SKIP), AC-2-4 (cites R74/R73/R77, LIVE), AC-2-5 (SKIP device; routing on model), AC-2-6 (SKIP device+pr_flow; policy on model). Each SKIP reason names the missing predicate.

## Notes for downstream
- **coder:** implement R70/R70a (knobs + `ToolchainConfig` extension), the `vivado` adapter (R72c/R73/R75/R77), and `payload_kind` on `Manifest` (R72a, default `"mock_stub"`). Do not touch the ABI or `PYROART1` header.
- **test-developer:** derive from R71's matrix and the amended AC-2-* clauses; build the shared availability probe (three predicates); assert SKIP-reasons name the absent prerequisite and that no SKIP is recorded as PASS. R74's constant and R73's `met_timing`/`fmax_mhz` formulas are citable directly.

Context hygiene: this spec-amendment task is a natural checkpoint. Recommend **/compact** before the next task (delegating R70–R77 to coder and test-developer builds directly on this).</result>
<usage><subagent_tokens>102245</subagent_tokens><tool_uses>26</tool_uses><duration_ms>407519</duration_ms></usage>
</task-notification>

