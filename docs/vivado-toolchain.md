# The Vivado toolchain — operator's guide

Written against `specs/python-regex-offload.md` v2.1.2, branch `phase1-pyro`,
Phase 2 ("real Vivado flow + on-hardware bring-up"). R-numbers below cite the
spec directly; read them there for the full normative text.

## 1. What this is

PYRO compiles each hardware-eligible regex into a bespoke synthesizable
circuit (regex -> automaton -> RTL, L2) and hands it to a background
**synthesis service** (R63). That service can drive two toolchains:

- **`mock`** — the default. Emits a stub artifact and a manifest with
  *configured*, not measured, numbers (`payload_kind == "mock_stub"`, R72a).
  This is the Phase-0/1 behavior and requires no Vivado license.
- **`vivado`** — the real flow this document covers. It takes the generated
  RTL for a pattern's circuit and runs genuine **Vivado out-of-context (OOC)
  synthesis + place-and-route** for the target part, at a 250 MHz clock
  constraint (R73), and writes back an honest manifest (`payload_kind ==
  "ooc_metrics"`, R72) recording real post-route `luts`, `ffs`, `fmax_mhz`,
  and `met_timing` (R72c).

On success the artifact and manifest land in the persistent bitstream cache
(R4/R63d) so a later process finds the pattern **warm** — no re-synthesis —
even after a restart.

### What it is NOT (yet)

This host satisfies only **half** of prerequisite P1 (§11): Vivado 2023.1
synthesizes, places, and routes the target part, but there is **no OpenNIC
partial-reconfiguration (PR) floorplan or PR-bitstream flow**, and the
physical U250 is a third party's live NIC that PYRO **must not** perturb
(R71). Concretely:

- No `payload_kind == "pr_bitstream"` is ever produced — that value is
  **reserved** until an OpenNIC PR floorplan and PR-bitstream generation flow
  exist (`pr_flow_present`, R71/R72a). The OOC artifact the `vivado` toolchain
  does produce is still the model-executable `PYROART1` container (R72), not
  a loadable device bitstream.
- Nothing is ever loaded onto the physical card, and no on-device scan,
  identity check, or throughput measurement happens. All dispatch continues
  to run through fallback or the software model (R51b).
- The R71 live/SKIP matrix (§5 below) is the normative record of exactly
  which clauses this implies are LIVE versus SKIP.

## 2. Configuration

All toolchain configuration is via **spec-named environment variables**
(R68/R70), sampled only at the R35a sampling points — first import of
`pyro`, every call to `pyro.install()`/`pyro.uninstall()`, and every call to
`pyro.refresh_env()` — never on the per-call hot path (R5/R35a). Mid-process
`os.environ` edits take effect only at the next sampling point (R35b).

| Variable | Values | Default | Effect |
|---|---|---|---|
| `PYRO_TOOLCHAIN` | `mock` \| `vivado` | `mock` | Selects the synthesis toolchain (R70). Any unrecognized value is treated as `mock` — fail-safe: PYRO never silently attempts a real flow the operator didn't name. |
| `PYRO_VIVADO` | install directory, e.g. `/usr/local/cad/Vivado/2023.1` | unset | Vivado install directory used when `PYRO_TOOLCHAIN=vivado`. **There is no default and no scanning** of the filesystem, `PATH`, or `XILINX_VIVADO` by library code (R70). If unset or it doesn't resolve to a working Vivado, the adapter is unavailable (`toolchain_present == false`) and every clause that needs it SKIPs (R71). |
| `PYRO_N_SYNTH` | integer | 1000 | Overrides the R4a synthesis-launch threshold (dispatch count before a pattern's circuit is enqueued for synthesis). An invalid value is ignored (default retained). |
| `PYRO_CACHE_DIR` | filesystem path | runtime default | Location of the persistent bitstream cache (R4/R68). |

Operators must set **both** `PYRO_TOOLCHAIN=vivado` and `PYRO_VIVADO=<dir>`
to get the real flow; setting only one leaves the mock toolchain in effect
(fail-safe default).

### Pinning at manager construction (R70b)

The R35a snapshot of `PYRO_TOOLCHAIN`/`PYRO_VIVADO` is re-sampled at every
sampling point, but a running residency/synthesis manager **pins its
`ToolchainConfig` at construction**. A toolchain change re-sampled into the
cached flags takes effect only in a manager built **after** that sampling
point — a fresh process, or an explicit manager reset — not by live re-push
into an already-running manager. This is deliberate: unlike a scalar such as
`PYRO_N_SYNTH`, the toolchain governs a spawned subprocess, and tearing down
a manager mid-flight to swap it would SIGTERM an in-flight real-Vivado
worker and orphan its process tree, bypassing the R77 in-worker kill.
Restart the process (or reset the manager) to pick up a toolchain change.

### Per-job timeout (R77)

The `vivado` adapter enforces its own subprocess deadline and, on expiry,
kills the **entire Vivado process tree** (Vivado plus children), mapping the
job to `SynthesisFailed` -> the permanent-fallback semantics of R65. Default
`VIVADO_JOB_TIMEOUT = 1800 s` (30 min), overridable via the `ToolchainConfig`
per-job-timeout field. The R63e client-side reaper is a bookkeeping backstop
only — it does not itself kill the Vivado subprocess; the adapter-level kill
is authoritative. Either way, the timeout runs entirely out of process and
never blocks or slows a caller (R63): the caller is served by fallback the
whole time.

### Cache-key separation (R75/R75a)

`toolchain_version` is part of the R4 bitstream-cache key and the R47b
manifest. The `vivado` toolchain reports the actual Vivado version, packed
as `(YY<<24)|(RR<<16)|build`; for Vivado 2023.1 this is `0x17010000`
(`YY=23=0x17`, `RR=1`, `build=0`), distinct from the mock's `0x00000100`.
Because this value is part of the cache key, a mock artifact and a vivado
artifact for the same pattern occupy **distinct keys** and never collide:
switching `PYRO_TOOLCHAIN` never serves a mock stub where a real-metrics
artifact is expected, or vice versa. `SHELL_VERSION` (the target shell/
PR-region identifier) stays at the model-harness value `0x0A000001` until a
real PR flow exists (`pr_flow_present == true`), since no real shell/
PR-region has been targeted yet.

## 3. Host prerequisites and quirks

- **Vivado 2023.1** is installed at `/usr/local/cad/Vivado/2023.1` on this
  host and synthesizes/places/routes the target part
  `xcu250-figd2104-2L-e` with no license error (this is what makes
  `toolchain_present == true` here, R71).
- **`libtinfo.so.5` shim.** Vivado's launcher `dlopen`s the SONAME
  `libtinfo.so.5`, which this Ubuntu host doesn't ship (only
  `libtinfo.so.6`). The adapter detects this automatically: if
  `libtinfo.so.5` fails to load, it creates a per-job shim directory
  containing a symlink `libtinfo.so.5 -> <system libtinfo.so.6>` and
  prepends it to the Vivado subprocess's library search path. This is
  transparent to the operator — no manual symlinking required — and only
  happens when the shim is actually needed.
- **Jobs take minutes.** A single OOC synth + place-and-route run is
  minutes long, not seconds (P8). This is why synthesis is asynchronous and
  gated by the R4a launch policy: a caller is never blocked waiting for it.
- **Artifact location.** Artifacts and manifests are written to the
  persistent bitstream cache at `PYRO_CACHE_DIR` if set, else a runtime
  default location (R68). Keying (`pattern_bytes`, `encoding`,
  `effective_flags`, `generator_version`, `toolchain_version`,
  `shell/PR-region_version`) is unchanged from the mock toolchain — R75a's
  version-tagging is what keeps mock and vivado artifacts from colliding in
  the same cache directory.

## 4. Failure semantics

A pattern whose real synthesis fails — doesn't fit the PR-region budget,
fails timing (`met_timing == false` at the R73 250 MHz proxy clock),
the Vivado tool errors, or the R77 per-job timeout fires — becomes
**permanently fallback-only** for the current
`(generator_version, toolchain_version, shell_version)` (R65):

- The failure is recorded as a bitstream-cache **negative entry** plus a
  diagnostic.
- It is **not retried indefinitely**.
- It **never raises an exception** to the caller. The caller keeps getting
  byte-identical results via fallback (R16) — this is routing, not
  `fallback_after_error` (R52's distinction).

Observe this without reading the implementation via `pyro.re.stats()`
(R66), which reports (in addition to the pre-existing hardware/model/
fallback/`fallback_after_error` dispatch counters, R52) the circuit-lifecycle
counters: `synth_launched`, `synth_succeeded`, `synth_failed`,
`circuits_synthesizing`, `circuits_resident` (gauge), `circuits_evicted`,
and `pr_loads`. All counters are monotonic except the two gauges, and stats
observation never perturbs routing.

The same permanent-fallback transition is drivable deterministically from
tests via `pyro.testing.inject_synth_failure(pattern, flags=0)` (R67),
without touching the real Vivado path.

## 5. Phase-2 verification status (AC-2 ledger)

Phase 2's prerequisite P1 is only **partially** satisfied on this host
(§11), so every AC-2 clause is evaluated against three probed availability
predicates (R71) — never assumed:

- `toolchain_present` — **true**: `PYRO_TOOLCHAIN=vivado` and `PYRO_VIVADO`
  resolve to a working Vivado 2023.1 for `xcu250-figd2104-2L-e`.
- `pr_flow_present` — **false**: no OpenNIC PR-partition floorplan or
  PR-bitstream generation flow exists.
- `device_usable` — **false**: the physical U250 runs a third party's live
  OpenNIC NIC image; there is no root, JTAG right, or `/dev/qdma*`, and the
  device must not be reprogrammed or perturbed.

A SKIP always names the missing prerequisite; a PASS comes only from real
execution with its predicate satisfied — a SKIP is never recorded as PASS.

| Clause | Requires | Disposition |
|---|---|---|
| AC-2-1 — real OOC synth+P&R, honest manifest metrics fit budget + met timing, cache warm-reload | `toolchain_present` | **LIVE PASS** |
| AC-2-1 — loadable PR bitstream produced | `pr_flow_present` | SKIP (`pr_flow_present=false`) |
| AC-2-2 — on-device harness/identity/scan over >= 1 MiB | `device_usable` ∧ `pr_flow_present` | SKIP (`device_usable=false`) |
| AC-2-3 — cold->warm real synth is minutes, async, never caller-blocking | `toolchain_present` | **LIVE PASS** |
| AC-2-3 — warm->resident PR-load timing + resident dispatch on device | `device_usable` | SKIP (`device_usable=false`) |
| AC-2-4 — estimator-vs-real P&R within R74 margin (calibration corpus); estimate-pass -> synth-fail -> permanent fallback on the real path | `toolchain_present` | **LIVE PASS** |
| AC-2-5 — routing (R3-R5) asserted on model; native-hot-path R3b bound | (model) / (`R3c` gate) | routing **PASS**; R3b **SKIP** (measured 4.77x — precondition not yet met; becomes hard PASS at AC-3-3) |
| AC-2-5 — resident-circuit throughput on hardware | `device_usable` | SKIP (`device_usable=false`) |
| AC-2-6 — single-tenant PR arbitration, model-side (R64a) | (model) | **LIVE PASS** |
| AC-2-6 — single-tenant PR arbitration on hardware | `device_usable` ∧ `pr_flow_present` | SKIP (`device_usable=false`) |

Notes:

- AC-2-3's async/non-blocking property is additionally asserted against the
  software model in all cases, regardless of `device_usable`.
- AC-2-5's routing measurement (R3b, the 1.15x below-threshold relative
  bound) is gated by its own precondition (R3c): it binds only once the
  loss-regime routing decision is served by native code. While that decision
  is still Python-level, the absolute bound (R3a, <= 2 us median) governs and
  is a firm PASS; the relative bound records a SKIP citing the measured
  ratio (4.77x on this host) rather than a FAIL. R3b becomes a hard PASS
  requirement at AC-3-3 (Phase 3 interposition + benchmarks).
- AC-2-6's model-side eviction (R64a) is a firm, non-vacuous LIVE assertion,
  not a no-op: the device-free residency manager tracks the resident-circuit
  set, promotes circuits to residency, and fires deterministic LRU eviction
  exactly as it would on hardware; only the physical PR-load mechanism and
  its timing are absent.

**Suite result:** 1045 passed, 6 skipped.

## 6. Measured calibration data

Measured on real Vivado 2023.1, part `xcu250-figd2104-2L-e`, 250 MHz OOC
clock constraint (R73). All patterns below **met timing** (`met_timing ==
true`, i.e. post-route WNS >= 0 ns at the 4.000 ns constraint).

| Pattern | LUTs | FFs |
|---|---|---|
| `abc` | 197 | 427 |
| `[a-z]+[0-9]{2,4}` | 197 | 430 |
| `(?:GET\|POST\|PUT) /[a-z/]* HTTP` | 221 | 441 |
| `^ERROR: .*$` | 226 | 431 |
| `[A-Za-z0-9]{60}` | 228 | 484 |
| `[A-Za-z0-9]{200}` | 410 | 626 |

This corpus is the basis for the R74 `ESTIMATOR_CALIBRATION_MARGIN` check
(AC-2-4): for every pattern that synthesizes successfully, the L2 resource
estimator's `est_luts`/`est_ffs` must never under-count the real values
(conservative estimate) and must not exceed 10x the real values (not
absurdly loose). The L2 estimator's own constants, for reference:

- **LUTs:** base 256 + 4 per automaton state + 2 per edge.
- **FFs:** base 448 + 1 per automaton state.

If no corpus pattern synthesizes successfully on a given host, the AC-2-4
calibration clause records a SKIP (`no successful real syntheses to
calibrate against`) rather than a FAIL — an empty set is vacuously
satisfied, not a defect. On this host the calibration set above is
non-empty and all six patterns pass all four R74 clauses.
