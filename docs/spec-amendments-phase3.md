# Spec amendments for owner review — Phase 3 slate

- **Target spec:** `specs/python-regex-offload.md` (currently **v2.4.0**, 2026-07-10)
- **Status:** **ADOPTED into v2.5.0** — all four amendments A1–A4 APPROVED by the
  owner (2026-07-15) with the recommended sub-options (A4.4(b) new canonical
  condition inserted first; A4.4(d) neutralized URI examples; A4.5 option (b)
  no-default fail-closed iface) and applied to the spec.
- **Date:** 2026-07-14
- **Author:** spec-writer (drafting), on evidence from the `phase1-pyro` working tree

Four amendments are proposed. **Each is severable**: approve or reject any one
without affecting the others. No amendment renumbers an existing requirement,
changes an AC number, or touches a frozen invariant (C ABI 2.0.0, the `PYROART1`
artifact header, `SHELL_VERSION 0x0A000001`, `PYRO_SHELL_SPEC16 0x0202`, the
R78.5b wire-harness namespace `0x00010000`).

| # | Target | Kind | SemVer if adopted alone | One line |
|---|--------|------|--------------------------|----------|
| **A1** | R3b (+R3c, AC-2-5, AC-3-3) | measurement protocol added; **constant unchanged** | MINOR | Make the 1.15× bound decidable: median-of-≥5 trials, subject pinned at 132 B, binds against the compiled-extension build |
| **A2** | R67 (+R51b, AC-3-2) | seam set extended | MINOR | Add `await_synthesis()` and a strict-residency/simulated-device seam that AC-3-2 cannot be written without |
| **A3** | R73a (new) | scope clarification of an existing gate | MINOR | Scope the PR-link timing gate to the reconfigurable module; the static's permanent −0.427 ns CMAC violation otherwise makes **every** partial build fail |
| **A4** | F2, F3 (+R68, R83, R39, §0, §10.1, P2) | normative facts demoted to configuration | MINOR (see A4 risk) | F2/F3 are **now false**; the netdev name is not a fact at all |

If all four are adopted together the spec goes to **v2.5.0** (MINOR: added
requirements, no interface/AC break).

---

## Amendment A1 — R3b measurement protocol

> **Owner has APPROVED keeping the 1.15× constant.** This amendment does **not**
> change the number. It adds the measurement protocol that makes the number
> decidable, and it records *why* 1.15× is defensible.

### A1.1 Target

§3, requirement **R3b** (spec lines 185–192, including its Rationale paragraph).
Consequential touch-points: **R3c** (lines 193–206), **AC-2-5** (1782–1790),
**AC-3-3** (1870–1877).

### A1.2 Exact current text

```
  - **R3b (relative, native-runtime phases).** From **Phase 1 onward** (i.e.,
    once the loss-regime hot path is served by the native host runtime, L3, or by
    interposition at a level where the delegation cost is native-code cheap),
    PYRO SHALL additionally keep below-threshold wall-clock time within **1.15×**
    of calling CPython `re` directly (routing/decision overhead ≤ 15%).

  Rationale: the 1.15× relative bound is only physically meaningful once the
  decision path is native; expressing it as an absolute bound for Phase 0 (R3a)
  preserves the intent — negligible routing tax — without demanding a ratio that
  is unachievable for a Python wrapper around sub-microsecond C code.
```

### A1.3 Proposed replacement text

```
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
```

### A1.4 Consequential edits (all mechanical; adopt with A1)

- **`tests/acceptance/test_ac2_5_throughput.py:127` — the R3c skip reason is now
  factually false on a native build.** The check's R3c-precondition evaluation
  predates the native router and unconditionally reports "the §8 R51 routing
  decision is served at Python level". With the compiled extension active that
  statement is untrue (and the single-trial ratio it records, ≈1.33×, is an
  artifact of the R3b.4 warmup defect). On A1 adoption this check must
  (a) evaluate the R3c precondition truthfully (native active ⇔ the compiled
  extension is serving the decision), and (b) when the precondition holds,
  measure under the R3b.1–R3b.4 protocol. Until adoption, the skip stands but
  its reason misstates the build — recorded here so it is not mistaken for an
  honest SKIP on native builds.

1. **R3c** — after "*MUST record a **SKIP** — not a FAIL — whose reason states the
   measured ratio…*", append: "*The measured ratio cited in that SKIP MUST be the
   R3b.1/R3b.2 statistic (median of ≥ 5 trials at the pinned 132-byte subject), not
   a single trial.*"
2. **AC-2-5** — the R3b clause: "*…records a **SKIP** whose reason states the
   measured ratio (not a FAIL)*" → "*…states the measured **R3b.1 median-of-≥5**
   ratio at the R3b.2 subject size*".
3. **AC-3-3** — after "*the R3b **relative** 1.15× bound becomes a **hard PASS
   requirement** here*", append: "*measured per the R3b.1/R3b.2 protocol against
   the compiled-extension build (R3b.3); on a pure-Python install the clause
   records the R3c SKIP and AC-3-3 is not claimable as PASS.*"

### A1.5 Evidence

- **The bound is currently asserted from a single trial.**
  `tests/acceptance/test_ac2_5_throughput.py:86-133`
  (`test_relative_loss_regime_bound_r3b`) runs **one** measurement loop, takes
  `ratio = median(pyro_ns) / median(stock_ns)`, and decides PASS/SKIP on that one
  number (`if ratio <= R3B_RATIO: return`, line 120).
- **The margin is smaller than the noise.** Native router: median **≈1.10× ± 0.04**;
  worst trial **≈1.15×**. Run-to-run noise is ±1.5%. A 4-point margin checked once,
  against noise that can move the number by that much and a worst trial that lands
  *on* the bound, is **flaky by construction** — it will PASS and FAIL on the same
  build.
- **The subject size is unpinned and outcome-determining.** The existing test's
  subject is `"the quick brown fox jumps over the lazy dog " * 3` — **132 bytes**
  (line 29), an incidental artifact of the test, not a spec constant. The same
  router measures **≈1.31× at 3 bytes** and **≈1.02× at 1 KiB**. Whether R3b passes
  is therefore currently a property of a test literal. R3b.2 pins the literal the
  measurements were actually taken at, so the recorded 1.10×/1.15× evidence stays
  valid.
- **The native floor is ≈1.01×**, i.e. the physically irreducible routing tax is
  ~1%. This is what makes 15% defensible, and it is precisely the claim R3b's
  original rationale asserted without data.

### A1.6 Risk of adopting

- **The median-of-≥5 statistic hides a bad tail.** A build whose worst trials sit
  at 1.30× while the median holds at 1.10× would PASS. *Assessment:* accepted
  deliberately. R3b is a **routing-tax budget**, not a latency SLO; the tail is
  scheduler/noise-dominated at this timescale, and a tail bound would re-introduce
  the flakiness this amendment exists to remove. If a tail bound is later wanted it
  should be stated as its own requirement (e.g. p95 ≤ some larger constant), not by
  tightening R3b's statistic.
- **Cost: ≥ 5 × 1000 paired calls per R3b check.** Measurably slower acceptance
  runs (seconds, not minutes). Acceptable; the R3b test is already `@pytest.mark.perf`.
- **Pinning 132 bytes bakes a test artifact into the spec.** It is not a principled
  size — it is the size at which the evidence was gathered. *Assessment:* that is
  exactly the honest reason to pin it. The alternative (state the bound at a
  "principled" size we have not measured) would be worse. The spec should say
  plainly that 132 B is the *measured* size; A1's text does.
- **Non-risk to flag explicitly:** this does **not** weaken R3. R3a (absolute
  ≤ 2 µs) governs at all times and is unchanged; A1 touches only the relative bound's
  measurement.

### A1.7 Decision

☑ **APPROVE A1** (owner, 2026-07-15) ☐ REJECT A1 ☐ APPROVE with changes: ______________________

---

## Amendment A2 — R67 seam additions (`await_synthesis`, strict residency)

### A2.1 Target

§9.1, requirement **R67** (spec lines 1527–1547). R67 **enumerates** the seam set
("providing at least:"), so adding a seam is a **normative** change to R67, not an
implementation detail. Consequential: **R51b** (lines 1387–1397), **AC-3-2**
(1866–1869).

### A2.2 Exact current text (R67)

```
- **R67 (fault-injection seam — NEW obligation).** PYRO MUST expose a
  test-hook namespace `pyro.testing` providing at least:
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
  - `pyro.testing.reset() -> None` — clear all injected faults.
  Injection MUST be **deterministic**, MUST NOT alter returned results relative
  to CPython `re` (R16 — an injected device error routes to fallback, an injected
  false positive is re-verified away), and MUST be observable via
  `pyro.re.stats()` (R66). To avoid production foot-guns, injection MUST take
  effect only when `PYRO_ENABLE_TEST_HOOKS=1` was sampled (R35a sampling
  discipline); when disabled, the functions are importable but no-ops that raise
  nothing. This seam is a **new implementation obligation** introduced in v2.0.5.
```

### A2.3 Proposed replacement text (R67)

```
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
```

### A2.4 Consequential note under R51b (adopt with A2)

Append to **R51b** (after "*…a not-resident pattern falls back for this call while
synthesis/PR-load proceeds in the background.*"):

```
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
```

**AC-3-2** (optional, recommended) — append the seam citation so the AC is
derivable from the spec alone:

```
  … a cold or fallback-only pattern is served by fallback. The tier-upgrade edge
  is made observable on a device-free host by the R67 seams
  `set_strict_residency` (R51b-strict) and `await_synthesis`. (R4a, R51, R51b,
  R53, R62, R67)
```

### A2.5 Evidence

- **`pyro/testing.py` today provides exactly four seams** (`__all__`, lines 36–41):
  `inject_device_error`, `inject_synth_failure`, `inject_false_positive`, `reset`.
  R67 enumerates precisely these. There is **no** wait/observe seam and **no**
  residency-mode seam anywhere in the tree (`grep -rn
  "strict_residency\|await_synth\|simulated_device" pyro/ tests/` → no hits).
- **AC-3-2 cannot be written against the current seam set.** It asserts a pattern is
  "*silently upgraded from fallback to resident-circuit dispatch*". On this
  (device-free) host R51b (spec 1387–1397) says the model "*MAY serve **any**
  HW-eligible … dispatch at **any** tier … counting as a `model` dispatch*". So
  before *and* after the upgrade the dispatch is a `model` dispatch: the upgrade has
  no observable edge in `pyro.re.stats()`, and a test can only assert it by reading
  internals — which §9 (line 1448: "*Tests MUST NOT read the implementation*")
  forbids. R67's own charter (line 1521: "*Any behavior an AC asks a test to verify
  MUST … be reachable from a public surface*") is what obliges the addition.
- **The wait problem is real too.** Synthesis is out-of-process and minutes-long
  (R63/P8). Without `await_synthesis`, the only spec-legal way to observe the
  transition is a sleep-poll on `pyro.re.explain()["circuit_status"]` — a race, and
  one that would have to be tuned per host.
- **Existing style the new seams follow:** `pyro/testing.py` gates every seam on
  `_route.test_hooks_enabled()` and returns silently when the gate is off (lines
  46–47, 53–54, 64–65); `reset()` is unconditional and idempotent (lines 69–78).
  Both proposed seams are specified to match.

### A2.6 Risk of adopting

- **A second routing mode exists that only tests exercise, and it could drift from
  the hardware path it simulates.** This is the real risk. *Mitigation, and the
  reason the seam is drafted as a **suspension** rather than a mode:*
  strict-residency does not add a rule — it removes the R51b **exception**, leaving
  R51 step 5 (the production hardware rule) in force. The code under test is the
  shipping decision path; what changes is one predicate ("is a device present"). A
  seam that *added* a parallel decision path would carry the drift risk properly and
  should be rejected.
- **Leakage into production.** If `PYRO_ENABLE_TEST_HOOKS=1` were set in production
  *and* something called `set_strict_residency(True)`, a device-free host would route
  everything to fallback. *Impact:* a **performance** regression only — never a
  correctness one (R16/R53 hold in both modes; fallback is always byte-identical).
  *Mitigation:* the R67 gate, plus `reset()` clearing it, plus the fact that both
  conditions must hold simultaneously.
- **`await_synthesis` can mask an asynchrony defect.** A test that awaits cannot
  notice that a *caller* was blocked. *Mitigation:* the proposed text forbids using
  it in an AC that must prove asynchrony, and AC-1-5/AC-2-3 keep their independent
  non-blocking assertions. Worth the owner's attention: this is a genuine sharp
  edge, and the prohibition is only as good as the reviewer who enforces it.
- **Timeout semantics.** `await_synthesis` returning the last-observed tier on
  timeout (rather than raising) means a hung service reads as "still cold", which a
  careless test could treat as a legitimate observation. *Mitigation:* the return
  value is the tier, so a test asserting `== "resident"` fails on timeout, which is
  the right default. An owner who prefers a raise-on-timeout is invited to say so —
  it is a one-word change to the draft.

### A2.7 Decision

☑ **APPROVE A2** (owner, 2026-07-15) ☐ REJECT A2 ☐ APPROVE with changes: ______________________

---

## Amendment A3 — R73a (new): scope of the post-route timing gate

> **This is the blocking one.** Without it, the PR flow **can never succeed** on
> the flashed shell: every per-pattern partial build fails *after* `pr_verify` has
> passed and *after* a good bitstream has been written.

### A3.1 Target

§7.6, **new requirement R73a**, inserted immediately after **R73** (spec lines
1267–1279). R73 itself is **not modified** — R73a scopes it for the PR-link case.
The next free number in §7.6 is R73a (R74 is taken); this follows the spec's
established sub-lettering (R3a/R3b/R3c, R47a/R47b/R47c, R82a–R82d).

### A3.2 Exact current text (R73 — retained unchanged, quoted for context)

```
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
```

### A3.3 Proposed new text (R73a)

```
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
```

### A3.4 Evidence

- **The gate is unscoped in code.** `pyro/synth/toolchain.py`, the `_PR_FLOW_TCL`
  body, line **346**:

  ```tcl
  set _p [get_timing_paths -max_paths 1 -nworst 1 -setup]
  ```

  No `-from`/`-to`/`-through` scoping, no cell filter, no clock filter. This is the
  **global** worst setup path of the whole in-context design. Note the contrast two
  lines above (line 344): utilization *is* already scoped —
  `report_utilization -cells [_rp_cell] -file util.rpt` — with the comment "*scope
  utilization to the RM cell so PR manifests report the PATTERN's resources … not
  static+RM whole-device*". The timing query was left global; A3 finishes the job the
  utilization scoping started.
- **The static carries a permanent violation outside `pyro_rp`.** WNS **−0.427 ns**,
  clock `txoutclk_out[0]`, inside OpenNIC's `cmac_usplus` IP. The R80 boundary (spec
  2148–2171) carries only `clk`/`rstn` and the two AXI-Stream interfaces; PYRO
  instantiates no CMAC logic and ties the datapath off. The violating path has neither
  endpoint in `pyro_rp`.
- **Consequence: the PR flow can never succeed.** In `_run_pr`
  (`pyro/synth/toolchain.py`):
  - line **707**: `met_timing = wns >= 0.0` → with the global WNS this is
    `-0.427 >= 0.0` → **`False`**, for **every** pattern, always;
  - lines **709–712**: `raise SynthesisFailed("PR link timing not met at 250 MHz: WNS=-0.427 ns (R73)")`.

  This fires **after** the `pr_verify` hard gate has already **passed** (marker check,
  line 675; report re-validation, line 682) — i.e. after Vivado has proven the static
  region is bit-identical and the link is sound. Per R65, the pattern then becomes
  **permanently fallback-only**. **AC-2b-3 can never flip LIVE**, and `pr_flow_present`
  can never be attested (R83a step 5 needs a manifest with
  `payload_kind == "pr_bitstream"`, which R82c forbids emitting without a completed
  job).

### A3.5 Also note — a **code bug**, deliberately *not* fixed in the spec

`SynthesisFailed` at **toolchain.py:709–712** is raised **before** the partial
bitstream is read at **:715–718**, and the `finally:` at **:749–750** then runs
`shutil.rmtree(workdir, ignore_errors=True)`. So on this path Vivado has written a
**good, `pr_verify`-passed `pyro_rp_partial.bit`** — and PYRO **destroys it**,
unread, on the way out. Hours of P&R, discarded.

This is an **implementation defect, not a spec defect**, and is listed here only so
it is not lost. It is **independent of A3**: even with R73a adopted, any *future*
`SynthesisFailed` raised between `write_bitstream` and the payload read (e.g. the
utilization-parse failures at :690–696, or a genuine RM-side timing failure) will
destroy a good artifact the same way. Suggested fix, for the coder's queue, not the
owner's: read the payload as soon as the `pr_verify` gate passes, and/or preserve the
workdir on failure under a diagnostic path. **No spec text is proposed for this.**

### A3.6 Risk of adopting

Stated plainly, because this amendment **narrows a safety gate**:

- **The narrower gate can mask a static-side violation.** With R73a, PYRO will
  happily emit and load a partial into a static shell that does **not** close timing —
  as, today, it demonstrably does not (−0.427 ns). PYRO would be shipping onto a
  known-marginal static. *This is a real reduction in what the per-pattern build
  checks, and the owner should decide it with that in mind.*
- **Why it is nonetheless the right narrowing:**
  1. **The static is not the partial's business, and the partial cannot fix it.** No
     bit PYRO emits can move a path inside `cmac_usplus`. Gating on it does not make
     the static better; it only makes the PR flow impossible. A gate that no
     achievable artifact can pass is not a safety property — it is an outage.
  2. **The static is validated once and then locked.** R82b makes the locked static
     DCP the linking substrate and the static region bit-identical across
     configurations; R82c makes `pr_verify` a **mandatory** gate that proves exactly
     that invariance before any `pr_bitstream` may be claimed. A partial therefore
     **cannot** introduce, worsen, or hide a static-side violation.
  3. **The violation is recorded, not buried.** R73a.4(1) requires the static's
     whole-design timing to be recorded at flash time; R73a.5 names the specific
     −0.427 ns violation in the spec; R73a.6 keeps the whole-design WNS in the job
     diagnostics of every partial build.
- **Residual risk 1 — mis-scoped query.** If the implementation's `-from`/`-to`
  filters catch only one boundary direction, or drop intra-RM paths, a genuine
  **RM-side** violation could escape the gate. *Mitigation:* R73a.1 states both
  directions and intra-cell paths explicitly; R73a.3 makes an empty scoped set a
  failure, so a filter that matches nothing fails loudly instead of passing silently.
  The first real PR job's `timing.rpt` should be read by a human against the scoped
  WNS before the flow is trusted.
- **Residual risk 2 — the CMAC domain is assumed not to interact with the user box.**
  R73a.4(3) rests this on the R80 boundary being thin and frozen (`axis_aclk` only).
  That is true of the flashed image. It would stop being true the moment the boundary
  gains a CMAC-domain signal — hence the explicit "MUST be revisited" in R73a.4.
- **Risk of NOT adopting (for symmetry):** the PR flow remains a dead letter. Every
  partial build burns a full in-context P&R (up to `VIVADO_PR_JOB_TIMEOUT` = 3600 s),
  passes `pr_verify`, writes a good bitstream, **destroys it**, and marks the pattern
  permanently fallback-only (R65). AC-2b-3 stays SKIP forever and `pr_flow_present`
  can never flip true.

### A3.7 Decision

☑ **APPROVE A3** (owner, 2026-07-15) ☐ REJECT A3 ☐ APPROVE with changes: ______________________

---

## Amendment A4 — F2/F3 are false; demote them to per-host configuration

### A4.1 Target

§1, ground-truth facts **F2** and **F3** (spec lines 66–71). Consequential:
**R68** `PYRO_DEVICE_IFACE` (1566–1568), **R83** canonical SKIP string (2253–2265),
**§0** summary (line 19), **R39** `transport_uri` examples (721–722), **§10.1**
preamble (line 1888), **P2** (line 2610).

### A4.2 Exact current text

```
- **F2.** FPGA card exposes two PCIe functions: `af:00.0` (`10ee:903f`) and
  `af:00.1` (`10ee:913f`), subsystem `10ee:0007`, PCI class `0280` ("Network
  controller").
- **F3.** Kernel driver in use is `onic` (AMD/Xilinx OpenNIC). The card runs the
  OpenNIC shell (QDMA transport). Two ports appear as network interfaces
  `enp175s0f0` and `enp175s0f1`.
```

### A4.3 Proposed replacement text

```
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
```

### A4.4 Consequential edits (adopt with A4)

**(a) R68 `PYRO_DEVICE_IFACE` — remove the default.** Current text:

```
  - `PYRO_DEVICE_IFACE` (**NEW obligation, v2.2.2**) — the `onic` netdev name the
    device transport (R86) binds for the AF_PACKET path. **Default `enp175s0f0`**
    (F3). Registered as a knob because the interface name is host/operator-specific.
```

Proposed:

```
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
```

**(b) R83 canonical SKIP string — add the unconfigured-interface condition.** The
`device_usable == false` enumeration (spec 2253–2265) currently has two conditions.
Proposed: insert as the **first** condition (you cannot probe an interface you do not
have), renumbering the existing two:

```
    1. `transport: PYRO_DEVICE_IFACE not configured`
    2. `probe: no valid ID_REPLY (no reply within PYRO_PROBE_TIMEOUT, or static_shell_id SPEC16 mismatch)`
    3. `transport: CAP_NET_RAW absent`
```

⚠ **This changes the canonical string** and therefore any test that pins the literal.
Called out as a risk below. If the owner prefers stability of the existing order, the
new condition can be appended as `4.` instead — logically worse (it reports a probe
failure for a probe that never ran), mechanically cheaper.

**(c) §0 summary, line 19** — "*The target device is the Xilinx/AMD PCIe card at
`af:00.0`/`af:00.1` running the **OpenNIC** shell*" → "*The target device is the
AMD/Xilinx Alveo U250 running the **OpenNIC** shell (its BDF is host configuration,
F2)*".

**(d) R39, lines 721–722** — the `transport_uri` examples `"qdma://af:00.0/q0"` and
`"eth://enp175s0f0"` are **illustrative**, but they illustrate two now-false strings.
Replace with `"qdma://0000:02:00.0/q0"` / `"eth://<iface>"`, or neutralize to
`"qdma://<bdf>/q0"` / `"eth://<iface>"` (**recommended** — the ABI takes a URI, and the
spec should not re-import a host fact by example).

**(e) §10.1 preamble, line 1888** — "*on the `onic` netdev (`enp175s0f0`/`f1`, F3)*"
→ "*on the `onic` netdev named by `PYRO_DEVICE_IFACE` (F3/R68)*".

**(f) P2, line 2610** — "*the **raw-Ethernet-frame binding** to `enp175s0f0`/`f1`
(F3)*" → "*…to the `onic` netdev (F3/R68)*".

### A4.5 What this implies for `pyro/device.py` (the default is currently wrong)

Two places bake the stale name in:

- `pyro/device.py:288` — `_sampled_iface()` falls back to `return "enp175s0f0"`, and
  `DeviceConfig.iface` (line 335) defaults from it;
- `pyro/_route.py:125` — `_DEVICE_IFACE_DEFAULT = "enp175s0f0"`.

On this host that default names an interface that **does not exist**. `AF_PACKET`
`bind()` against it (`pyro/device.py:604`) fails with `ENODEV` — an obscure `OSError`
where the spec's contract (R86.4) is a clean `(False, reason)`.

Three options; **(b) is recommended** and is what the A4.4(a) text specifies:

| | Option | Assessment |
|---|--------|------------|
| (a) | Change the default to `"ens2"` | **Rejected.** Fastest, and wrong for exactly the reason F3 was wrong: it re-encodes one host's configuration as a library constant. It will be false again on the next host, and F3's shape problem survives untouched. |
| (b) | **No default.** `DeviceConfig.iface: Optional[str] = None`; `_route.device_iface()` returns `None` when unset; `probe_device` returns `(False, "device_usable=false — transport: PYRO_DEVICE_IFACE not configured, …")` | **Recommended.** Fail-closed, matches the R70 no-scanning discipline and the existing fail-loud DCP knobs, and turns an obscure `ENODEV` into the spec's own contract. Costs the operator one env var — the same act they already perform for `PYRO_PR_STATIC_DCP`. |
| (c) | Derive the netdev from a configured BDF via `/sys/bus/pci/devices/<bdf>/net/` | **Available, deliberately not proposed.** Honest caveat: the name **is** *look-up-able* at runtime from the BDF via sysfs even though it is not *derivable* from it by any naming rule. But this trades one required knob (`PYRO_DEVICE_IFACE`) for another (the BDF) **plus** a filesystem read that sits close to the line R70 draws against library code scanning the system. It is a reasonable convenience **on top of** (b) — never instead of it. Offered for the owner to accept or decline separately. |

### A4.6 Evidence

Measured on the host, 2026-07-14:

```
$ lspci -nn | grep -i xilinx
02:00.0 Network controller [0280]: Xilinx Corporation Device [10ee:903f]

$ ls /sys/class/net/
ens2  ens4f0np0  ens4f1np1  ens9  lo

$ readlink -f /sys/class/net/ens2/device
/sys/devices/pci0000:00/0000:00:02.0/0000:02:00.0

$ basename $(readlink -f /sys/class/net/ens2/device/driver)
onic
```

- **F2 is false on both counts.** One PF, not two (`10ee:913f` is **absent** — the
  PYRO PR shell is built `pf=cmac=1`); at `0000:02:00.0`, not `af:00.0`.
- **F3 is false, and falser than it looks.** The netdev is `ens2`. Note *why*: `ens2`
  is **SLOT**-based systemd naming, `enp175s0f0` was **PATH**-based. The naming scheme
  itself changed with the card/topology, which is the point — **no rule takes you from
  the BDF to the name**. `0000:02:00.0` does not "predict" `ens2`; the firmware's slot
  index does. That is why F3 cannot be repaired by editing the string.
- **The spec's own machinery already agrees with the demotion.** R81/R83 make the
  **live probe** (`ID_REQUEST` → `ID_REPLY`, `SPEC16` check) the authoritative
  device-identity gate. Nothing normative depends on F2's BDF; nothing *should* depend
  on F3's name. A4 removes the last places that do.
- *Incidental, not proposed for amendment:* **F1** records the host kernel as
  `6.8.0-124-generic`; it is now `6.8.0-134-generic`. Nothing in the spec turns on the
  point release. Noted only so the owner is not surprised to find it stale too.

### A4.7 Risk of adopting

- **Removing the `PYRO_DEVICE_IFACE` default is a behavior change** — the closest
  thing in this slate to a break. A caller who relied on `DeviceConfig()` binding a
  netdev with no configuration now gets a `(False, reason)` from `probe_device`.
  *Assessment:* on **this** host that "working" default binds a **nonexistent**
  interface, so the change converts a silent misbind into an explicit, spec-shaped
  reason string. On a host where `enp175s0f0` happened to be right, the operator must
  now set one env var. Fail-closed is the correct trade for a device binding.
- **The R83 canonical SKIP string changes** (new first condition, existing two
  renumbered). Any test pinning the literal must be updated. *Mitigation:* the string
  is already spec-owned and already changed once (v2.2.0 superseded the v2.1.3
  literal); the append-as-`4.` variant in A4.4(b) avoids the renumber at the cost of
  reporting a probe failure for a probe that never ran.
- **Demoting F2/F3 leaves §1 with less concrete grounding**, and §1 opens by promising
  facts the design "MUST be grounded in". *Mitigation:* F2 keeps the parts that are
  genuinely invariant (U250, part number, vendor ID, PCI class, `onic`, OpenNIC shell)
  and drops only the two that are configuration. The device-identity guarantee does not
  weaken — it **strengthens**, moving from a written-down string that goes stale
  silently to the R81 `SPEC16` wire check that is verified on every probe.
- **Six cross-references must move together** (A4.4 a–f). If any is missed, the spec
  keeps asserting `enp175s0f0`/`af:00.0` somewhere, which is the exact failure mode
  this amendment exists to end. Adopt A4 as a unit, or not at all.

### A4.8 Decision

☑ **APPROVE A4** (owner, 2026-07-15; with A4.4(b) insert-first and A4.4(d) neutralized examples; A4.5 option (b)) ☐ REJECT A4 ☐ APPROVE with changes: ______________________

---

## If adopted — changelog entry to add under §14

```
- **2.5.0** (2026-07-__) — *Phase-3 slate: R3b measurement protocol, R67 seam
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
```

---

## Open item for the coder (NOT a spec change) — carried from A3.5

`pyro/synth/toolchain.py` `_run_pr`: `SynthesisFailed` at **:709–712** is raised
**before** the partial bitstream is read at **:715–718**, and the `finally:` at
**:749–750** runs `shutil.rmtree(workdir, ignore_errors=True)`. A **good,
`pr_verify`-passed** `pyro_rp_partial.bit` is therefore **destroyed unread** on every
failure between `write_bitstream` and the payload read. Adopting A3 stops the specific
`met_timing` case from firing, but the destroy-on-failure hazard remains for the
utilization-parse failures (**:690–696**) and for any genuine RM-side timing failure.
Fix belongs in code (read the payload once `pr_verify` passes; and/or preserve the
workdir on failure under a diagnostics path). **No spec text proposed.**
