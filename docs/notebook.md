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

1. [EXPERIMENT 20 Jul 2026 03:15:04 P2d Lands — 1.62 GiB/s Over QDMA Char-Devs, R1 Floor Crossed, AC-3-3 PASSES on Silicon](#20-jul-2026-031504) :complete:
2. [EXPERIMENT 19 Jul 2026 14:43:10 Jumbo Shell Boots From QSPI — 567 MiB/s Pipelined (4.7× over 1518), Two onic MTU Defects Patched](#19-jul-2026-144310) :complete:
3. [EXPERIMENT 16 Jul 2026 06:06:50 P2b Lands — 8 B/cyc Engine on Silicon at 1.97 GB/s On-Chip, Timing Closed at 251.9 MHz](#16-jul-2026-060650) :complete:
4. [EXPERIMENT 16 Jul 2026 04:48:44 R85a Recovery Folded into load_partial, Device-Gated ACs on Silicon, and the Pipelining Measurement That Reframed P2](#16-jul-2026-044844) :complete:
5. [EXPERIMENT 15 Jul 2026 14:23:02 JTAG Wedge Recovered In-Band — User+QDMA Soft-Reset Sequence, and the First R45a Counter Read on Silicon](#15-jul-2026-142302) :complete:
6. [EXPERIMENT 15 Jul 2026 03:44:16 Phase-3 Slate v2.5.0 + First Real HW Partial — R73a Gate Bug Caught by Its Own Safety Net, and JTAG PR Wedges the RP](#15-jul-2026-034416) :complete:
7. [EXPERIMENT 14 Jul 2026 20:26:10 Counter Wedge Fixed — a Circular-Import Corpse, and Why the Workaround Failed](#14-jul-2026-202610) :complete:
8. [EXPERIMENT 14 Jul 2026 18:35:59 AC-3-1 + AC-3-4 Land — Sabotage-Verified Suites, and a Counter-Wedge Bug Found](#14-jul-2026-183559) :complete:
9. [EXPERIMENT 14 Jul 2026 16:23:15 Native Routing Hot Path (R3c) — R3b Reachable at ~1.09×, Warmup Defect Found in the Recipe](#14-jul-2026-162315) :complete:
10. [EXPERIMENT 14 Jul 2026 08:49:52 PR Shell Rebuilt From Source on nf-server06 — New Card, New Flash, device_usable=true](#14-jul-2026-084952) :complete:
11. [EXPERIMENT  9 Jul 2026 10:59:06 U250 QSPI Flash — PYRO PR Shell User Image](#9-jul-2026-105906) :complete:
12. [EXPERIMENT  6 Jul 2026 14:05:00 PYRO Phase 2b — PR Shell + First pr_bitstream Partial](#6-jul-2026-140500) :complete:
13. [EXPERIMENT  6 Jul 2026 02:50:21 PYRO Phase 2 — Real Vivado Flow, Estimator Calibration](#6-jul-2026-025021) :complete:
14. [EXPERIMENT  5 Jul 2026 12:05:02 PYRO Phase 1 — Per-Pattern Circuits, Synthesis Service, C ABI](#5-jul-2026-120502) :complete:
15. [EXPERIMENT  5 Jul 2026 02:44:00 PYRO Phase 0 — Software Shim, Classifier, Model](#5-jul-2026-024400) :complete:
16. [EXPERIMENT  4 Jul 2026 07:33:45 FPGA Platform Discovery](#4-jul-2026-073345) :complete:
---

# EXPERIMENT 20 Jul 2026 03:15:04 P2d Lands — 1.62 GiB/s Over QDMA Char-Devs, R1 Floor Crossed, AC-3-3 PASSES on Silicon :complete:

## 1. Hypothesis

The AC-3-3 R1/R2 hardware clause — Phase 3's last unmet clause — requires the
P2 performance transport (QDMA char-devs, F5) plus ≥ 1 GiB/s wall-clock. The
frame path owns ~10.8 µs/frame on AF_PACKET; a char-dev data plane plus a
native credit loop should reach the child's ~6.5 µs/frame ceiling
(≈ 1.4 GiB/s) and flip the clause from honest SKIP to PASS.

## 2. How

Generic qdma-pf (dma_ip_drivers 2024.1) binds the single PF in place of onic
(`scripts/pyro_dataplane_swap.sh`, NOPASSWD-granted); R78 control frames ride
the ST queues — AXIS payloads either way. Host side: `_CharDevTransport`
(re-framing reader thread, cached per path) + `_fast.dataplane_pipeline`
(compiled windowed credit loop, R3b.3 discipline). Six silicon-found driver
incompatibilities fixed in a local `dma_ip_drivers` branch.

### Key commands

```bash
sudo scripts/pyro_dataplane_swap.sh data     # onic -> qdma-pf + QCONF poke
PYRO_QDMA_CHARDEV=/dev/qdma02000-ST-0 PYRO_BENCH_JUMBO=1 \
    .venv-pyro/bin/python3 scripts/pyro_pipeline_bench.py
PYRO_DEVICE_IFACE=ens2 PYRO_QDMA_CHARDEV=/dev/qdma02000-ST-0 \
    .venv-pyro/bin/python3 -m pytest tests/acceptance/test_ac3_3_benchmark.py
```

## 3. Observations

- **Silicon-found driver incompatibilities** (each bisected on hardware,
  committed as `dma_ip_drivers` branch `pyro-open-nic-compat` 4e12421):
  1. shell QCONF (BAR2 0x1000) resets to num_q=0 — all H2C tready-blocked
     until poked (swap script does it; onic programs it, generic driver
     cannot know it exists);
  2. H2C descriptor DW0[31:0] is open-nic **metadata = total packet length**
     (tlast placement), not cdh_flags/pld_len — the stock fields decode as a
     garbage packet length;
  3. one ST packet = one physically-contiguous descriptor — page-crossing
     user SGLs split frames; fixed with a ≤16K contiguous bounce buffer,
     fire-and-forget (fp_done frees; sync writes cost 18 µs and serialize);
  4. `sgl_map` mapped PAGE_SIZE@0 per sg (in-tree `!! TODO`) — a >4K
     contiguous sg had one page IOMMU-mapped; DMAR read faults halted the
     engine (the intermittent early successes were deferred-unmap luck);
  5. C2H CMPT: open-nic pkt_len at [47:32], desc_used never set — stock
     parser EIO'd every reply;
  6. reads must complete at packet boundaries (completion path AND
     submit-time copy — the latter otherwise strands buffered replies).
- **Zombie reads steal frames**: closing a cdev fd cannot cancel its
  in-driver read; the orphan steals the next transport's first replies
  (exactly LOST=1 per bench window). Fixed: per-path transport cache (a
  queue supports one reader) + `_hard_close` interrupts the reader via
  SIGUSR1 (the driver wait is interruptible and dequeues under lock).
- **The 4.6 ms/frame mystery was arithmetic**: one stolen frame per window
  burned the 2 s recv timeout; real per-frame cost was 43 µs all along.
- **Python was the last wall**: 22 µs/frame interpreted (GIL contention
  sender vs reader). The compiled loop (`src/pyro_dataplane.c`, GIL
  released):

  | Path | µs/frame | Throughput | loss |
  |------|----------|------------|------|
  | AF_PACKET jumbo (19 Jul) | 16.1 | 567 MiB/s | 0 |
  | char-dev, Python loop | 22–24 | ~400 MiB/s | 0 |
  | char-dev, native loop W≥64 | **5.44** | **1.636 GiB/s** | **0** |

  Soak: 3 × 256 MiB (84,270 frames), 1.57–1.62 GiB/s, zero loss, zero
  kernel errors, direct-interrupt mode.
- **AC-3-3: 4/4 PASSED** with PYRO_QDMA_CHARDEV configured — including
  `test_r1_r2_hardware_win_regime_requires_device` (windowed B2 shape,
  zero-loss discipline, chardev predicate met, 1.6 GiB/s ≥ the 1 GiB/s
  floor). First run lost 2/3511 frames to the discovery transport's zombie
  read and honestly SKIPped; the SIGUSR1 fix made it clean.

## 4. Data analysis

5.44 µs/frame against the child's ~6.5 µs/frame model estimate: the loop is
now **child-bound** (wall-clock 1.76 GB/s ≈ 88% of the 1.996 GB/s on-chip
rate R78.11 reports). The remaining gap is frame turnaround, as designed.
The R1 floor is met on the P2 performance transport with 64% headroom; the
5 GiB/s target needs the wide-engine ladder (N=16+) and/or multi-queue —
out of the data plane's hands now. **Phase 3 status: AC-3-1, AC-3-2, AC-3-4
device-free PASS; AC-3-3 4/4 on silicon** — contingent on owner adoption of
the B1/B2 amendment slate (docs/spec-amendments-p2d.md); code and tests
carry v2.7.0-draft markers, the spec file itself is untouched.

## 5. Ideas for future experiments

- Owner review: adopt B1/B2 → spec v2.7.0; then re-emit the AC-3-3 metrics
  as the normative record.
- Upstream candidates: the `sgl_map` offset+len fix is a genuine stock bug
  (their own TODO); the rest is open-nic-specific — consider a Xilinx PR
  for the former.
- 5 GiB/s ladder: N=16 engine PR build + multi-queue TX (replies already
  steer to qid 0 via the zero indirection table).
- The `status=0xffffffff` reset-register reads and the transient
  `eqdma CSR timeout` at queue start remain unexplained (benign so far).
- pyro_hw.py / load_partial recovery in data mode: R85a recover_cmd
  reloads onic — needs a data-mode-aware recovery (swap control -> load ->
  swap data).

---

# EXPERIMENT 19 Jul 2026 14:43:10 Jumbo Shell Boots From QSPI — 567 MiB/s Pipelined (4.7× over 1518), Two onic MTU Defects Patched :complete:

## 1. Hypothesis

The jumbo shell (MAX_PKT_LEN=9600, p2c/R78.9a) flashed to QSPI on 19 Jul 02:23
survives a power cycle and boots on its own, and the larger frame bound lifts
pipelined MATCH throughput well past the 121 MiB/s plateau by amortizing the
~11 µs fixed per-frame cost over 6.4× more corpus bytes per frame.

## 2. How

Power-cycled nf-server06, then: verify PCIe enumeration → `pyro_wedge_recover.sh`
→ probe → `load` the `becf73e8` partial → `pyro_pipeline_bench.py` at both
payload bounds (`PYRO_BENCH_JUMBO=1` for 9568 B).

### Key commands

```bash
sudo scripts/pyro_wedge_recover.sh          # now also sets mtu 9586
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py probe
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py load \
    .superpowers/pr-builds/pattern_becf73e88b6f1c561308914848c69ab0_partial.bit
PYRO_BENCH_JUMBO=1 PYRO_DEVICE_IFACE=ens2 \
    .venv-pyro/bin/python3 scripts/pyro_pipeline_bench.py
```

## 3. Observations

- Card enumerated at 0000:02:00.0 after power-on with `build_timestamp=0x07170514`
  — the 17 Jul 05:14 jumbo build. **QSPI multiboot of the jumbo shell works.**
  Shell ID `0x0202c318` pre-load; BUILD16 reads `0x0000` once a partial is
  resident (SPEC16, the validated half, unchanged).
- First `insmod` failed `Invalid module format`: the reboot silently picked up
  a kernel update (6.8.0-134 → 6.8.0-136-generic). Rebuild of onic.ko fixed it.
- Jumbo bench initially died with `EMSGSIZE`: **two stock onic driver defects**.
  (1) `netdev->max_mtu` never set, so the kernel caps MTU at 1500;
  (2) `onic_change_mtu()` returns 0 without writing `dev->mtu` — and when
  `ndo_change_mtu` is defined the core delegates entirely to it, so
  `ip link set mtu` silently no-ops. Patched both (max_mtu = 9600 − ETH_HLEN
  = 9586; `WRITE_ONCE(dev->mtu, mtu)`), rebuilt, reloaded.
  RX buffers are single 4 KiB pages, so jumbo is TX-only safe — fine, since
  only H2C carries corpus; replies are small.
- Pipelined MATCH, 4 MiB corpus per window size, zero loss at every W:

  | payload bound | W=1 | plateau | best | µs/frame |
  |---------------|-----|---------|------|----------|
  | 1486 B (1518 shell bound) | 66.2 MiB/s | ~121 MiB/s (W≥2) | 121.6 (W=64) | 11.6 |
  | 9568 B (jumbo, R78.9a)    | 282.4 MiB/s | ~560 MiB/s (W≥2) | **567.1 (W=32)** | 16.1 |

## 4. Data analysis

Jumbo buys **4.66×** (567.1/121.6) against a 6.44× payload increase. The gap is
the fixed per-frame cost: marginal byte rate between the two operating points is
(9568−1486) B / (16.1−11.6) µs ≈ **1.80 GB/s — the P2b engine's ~2 B/cyc-of-8
wall-clock rate showing through** — while the frame-size-independent overhead
comes out at ≈10.8 µs/frame from either row (11.6 − 1486/1796 ≈ 16.1 −
9568/1796 ≈ 10.8). At 9568 B the child spends only ~5.3 µs of each 16.1 µs
scanning; transport/framing still owns two-thirds of the budget. Zero-overhead
ceiling at this frame size is ≈1.7 GiB/s, so the frame path — not the engine —
remains the binding constraint, confirming the P2 reframing: a char-dev DMA
data plane is where the next multiple lives.

## 5. Ideas for future experiments

- Upstream-worthy onic patch: `max_mtu`/`change_mtu` fix is generic, not
  PYRO-specific; consider PRing to open-nic-driver.
- Kernel-update hazard: pin or rebuild onic.ko on boot (DKMS?) so a power cycle
  can't strand the card behind a vermagic mismatch.
- Probe why user/shell reset status registers read `0xffffffff` (bit 0 is all
  the script checks; the other 31 bits reading 1 is unexplained).
- P2 char-dev data plane: 10.8 µs/frame fixed cost is the target; even halving
  it at jumbo size would clear 1 GiB/s.

---

# EXPERIMENT 16 Jul 2026 06:06:50 P2b Lands — 8 B/cyc Engine on Silicon at 1.97 GB/s On-Chip, Timing Closed at 251.9 MHz :complete:

## 1. Hypothesis

The one-hot NFA engine can be widened to N bytes/cycle by cascading N
closure+move stages per clock, entirely inside the RM (partial-only, R79);
an 8-wide `abc[a-f]{2}` child will close timing at 250 MHz, reply
byte-identically to the 1-byte child on the wire, and read back R45a
CYCLES ≈ ceil(len/8) on real silicon — retiring the child-side half of the
R1-floor equation before the jumbo (P2c) shell rebuild.

## 2. How

- **Equipment:** U250 on nf-server06, shell `0x07140219`; pinned Vivado 2025.2
- **Software:** `pyro.hdl.generator` `_emit_rtl_wide` (N ∈ {1,2,4,8,16}),
  `rp_wrapper` v3 wide feed (N ∈ {2,4,8}), `datapath_bytes` through
  identity/SynthJob/PR flow; NEW permanent xsim differential
  (`tests/hw/xsim_diff.py` + `tb_pyro_rp.v`); `pr_build_driver_dpb8.py`
- **Method:** emit → xvlog → engine-level xsim → full-frame xsim differential
  (byte-exact replies vs a Python composer; engine-semantics NFA reference
  with per-beat coalescing) → R73a-gated PR build → atomic R85a load →
  on-chip MATCH/PERF → windowed wall-clock bench → capped AC-3-3.

### Key commands

```bash
python3 tests/hw/xsim_diff.py --datapath 8            # byte-exact differential
.venv-pyro/bin/python3 .superpowers/pr-builds/pr_build_driver_dpb8.py
PYRO_DEVICE_IFACE=ens2 python3 scripts/pyro_hw.py load <partial.bit>
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py perf --slot 1
```

## 3. Observations

- **Design decisions.** (i) In-beat accepts COALESCED to the beat's highest
  end — sound for R19 nomination (`start`=0 ⇒ window containment); one entry
  per accepting beat. (ii) Harness v3 = N-byte feed ONLY; ping-pong RX/scan
  overlap deliberately dropped (§1b of the design doc: ~8% at jumbo for the
  riskiest rewrite). (iii) N=1 emission byte-identical (verified vs git HEAD,
  3 patterns) — zero identity/cache rollover; N>1 mixes a `DPB` domain into
  the R47a hash. (iv) `in_keep` contract: contiguous, partial only on
  `in_last` (trailing-stage state dead — the harness resets per scan).
- **xsim differential (permanent, first run):** 10/10 replies byte-exact at
  N=8 — ID, six MATCH shapes (short beat, end-of-corpus, zero matches, 300 B
  many-window, cap-1 OVF), wrong-slot STATUS; PERF `CYCLES=40` for 300 B
  (7.5 B/cyc). Regressed clean at N=1/2/4 and across `foo|bar|baz`,
  `\d+\w*`, `a{2,4}` at N=8.
- **PR build:** SUCCESS in 2817 s — `pr_verified=True`, **`met_timing=True`,
  `fmax = 251.89 MHz`** (RP-scoped WNS ≈ +0.030 ns; the 8-stage cascade
  closes). 3489 LUTs / 1896 FFs (1-byte child: est 358 LUTs — the ~8× move
  logic is real and still ~0.5% of the pblock).
- **On silicon** (atomic load 13.7 s, new hash `becf73e8…`): identical
  windows to the 1-byte child (`end=7`, `end=14` on `xxabcdeyyabcffz`);
  **`CYCLES=4 BYTES=15`** (1-byte child: 17) — exactly xsim; max frame:
  **`CYCLES=187 BYTES=1474` = 7.88 B/cyc = 1.97 GB/s on-chip** =
  ceil(1474/8)+2.
- **Wall-clock unchanged, as predicted:** stripped-loop plateau 194 MiB/s at
  7.2 µs/frame (1-byte child: 187 MiB/s / 7.5 µs) — the child has vanished
  from the wall clock; the ~7 µs/frame host floor is everything. Capped
  AC-3-3: 3 passed + the honest R1 SKIP (51.6 MiB/s sequential per protocol,
  P2 named). Unit suite 604 passed / 1 skip.

## 4. Data analysis

Both halves of the §1b floor equation are now measured on hardware: the host
floor (~7 µs/frame) and a child that scans a max frame in 0.75 µs. At 1518 B
frames the child is invisible — exactly what the floor decomposition
predicted — so P2b's value is all stored in the jumbo rung: at
`MAX_PKT_LEN = 9600`, 9560 B / 7.2 µs ≈ **1.24 GiB/s ≥ the R1 floor**, and
the 1-byte child would have burned 38 µs/frame there. The cascade's timing
result (+0.030 ns at 8 wide, ~same margin as the 1-byte RM) suggests the
closure+move logic is shallow enough that width is not yet the critical
path; the 5 GiB/s target (~24 B/cyc at jumbo, or 8 B/cyc at 322 MHz +
wider) has headroom to explore. The differential harness earned its keep on
day one: every reply byte-exact before any tool time was spent, and it is
now a 30-second regression for every future engine/harness change.

## 5. Ideas for future experiments

- **P2c: the floor-crossing rung** — `MAX_PKT_LEN` 9600 static rebuild +
  reflash + relock; R78 payload-bound rev; rebuild both children; expect
  ≥ 1.2 GiB/s wall-clock and the AC-3-3 R1 clause arming itself.
- Sweep N ∈ {4, 8, 16} PR builds for timing/LUT curves (N=16 needs the
  word-straddle fix in the harness feed first).
- Wire `datapath_bytes=8` into the residency/synthesis default for
  HW-eligible patterns once P2c makes it pay on the wire.
- R45a semantics note for coalesced entries (out_count = accepting beats on
  wide engines) — spec text when P2b is promoted from DRAFT.
- Kernel-bypass host loop (P2d) only after jumbo: 7 µs → ~2 µs/frame would
  put 5 GiB/s in range with a 24 B/cyc engine.

---

# EXPERIMENT 16 Jul 2026 04:48:44 R85a Recovery Folded into load_partial, Device-Gated ACs on Silicon, and the Pipelining Measurement That Reframed P2 :complete:

## 1. Hypothesis

(1) The in-band wedge recovery can be folded into `pyro.device.load_partial`
so a JTAG partial load is one atomic, always-recovered operation; (2) with the
card usable, the two AC-3-3 device-gated skips can run against real silicon
and produce honest dispositions; (3) toward P2, pipelining MATCH frames will
reveal whether the 47 MiB/s sequential throughput is transport-bound or
child-bound — deciding whether QDMA char-devs are the right next investment.

## 2. How

- **Equipment:** U250 on nf-server06 (`0000:02:00.0`, shell `0x07140219`),
  `abc[a-f]{2}` pattern child resident
- **Software:** spec bumped to **v2.5.1** (§13 change control before code);
  `pyro/device.py` recovery fold; new `tests/unit/test_device_load_recovery.py`;
  AC-3-3 clause 2 implemented (was a device-usable tripwire); conftest
  `device_iface` pristine-snapshot fixture; `scripts/pyro_pipeline_bench.py`
- **Method:** amend spec → implement → unit + seam tests → sudoers NOPASSWD
  grant scoped to `pyro_wedge_recover.sh` → two real atomic load+recover swaps
  → capped AC-3-3 run on hardware → windowed-throughput sweep (W=1…64, 4 MiB
  per point)

### Key commands

```bash
PYRO_DEVICE_IFACE=ens2 python3 scripts/pyro_hw.py load <partial.bit>   # now atomic
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 -m pytest tests/acceptance/test_ac3_3_benchmark.py -rs
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_pipeline_bench.py
```

## 3. Observations

- **Spec v2.5.1 adopted.** New **R85a** (wedge mechanism + mandatory 4-step
  in-band recovery; neither reset alone suffices — both proven insufficient in
  isolation); **R86.5** extended (recovery through the same `load_runner` seam,
  `recover_cmd=None` opt-out, default `sudo -n scripts/pyro_wedge_recover.sh`);
  R85's "netdev undisturbed" corrected (MAC changes across a load);
  **AC-2-5/AC-3-3 clarified** (R1 throughput clauses need the P2 performance
  transport; over the control transport: measure first, SKIP naming P2).
- **Atomic loads verified on HW:** ID stub swapped in **16.2 s**, pattern child
  back in **15.9 s** — single call each, child answering immediately after.
  R64 evict/reload therefore also works on silicon. Suites: unit **600/1 skip**
  (+7 recovery tests), acceptance **715/10 skips** — same shape as v2.5.0.
- **Test-harness defect found:** the hermetic `pyro_isolation` fixture scrubs
  `PYRO_DEVICE_IFACE` per test, so hardware clauses could NEVER see the
  operator's iface even on a device host (vacuous skip forever). Fixed with a
  pristine-at-import `device_iface` fixture (the `_PRISTINE_TOOLCHAIN` pattern);
  fail-closed behavior on device-free hosts unchanged.
- **Device-gated ACs on silicon:** R78.11 counter attribution **PASSED** (first
  spec-suite hardware pass); R1/R2 win regime **measured then honest-SKIPped**:
  47.5 MiB/s sequential over the control transport, median frame RTT **28 µs**
  (the CLI's earlier ~40 ms readings were warmup/scheduling noise, not the
  card), floor 1 GiB/s, missing piece named as P2.
- **The P2-reframing measurement** (`pyro_pipeline_bench.py`): W=1 →
  **52.6 MiB/s**; W=2 → **116.6 MiB/s**; W=4…64 → plateau **~119–120 MiB/s**,
  **11.8 µs/frame ≈ 2950 cycles**, zero loss at every window. Saturation at
  W=2 means plain `AF_PACKET` already saturates the child: the harness
  serializes (`s_axis_tready` only in `ST_RX`), so scan (~1675 cyc at the
  measured 0.88 B/cyc) + parse/reply/turnaround (~1275 cyc) bound throughput,
  not the transport.

## 4. Data analysis

The wedge story closed the loop the right way: root cause → in-band fix →
spec text → mechanism folded behind the library seam → proven twice on
silicon. The load path that was the project's wall for a week is now a 16 s
atomic operation, and the change never touched the wire format or the static
shell.

The pipelining sweep is the finding that redirects Phase P2. The natural plan
was "faster transport" (QDMA char-devs, F5); the data says the transport
stopped being the bottleneck at window 2, and the child is the wall — both of
its constraints (serialized FSM, 1 B/cyc engine) live in the **partial**, not
the static. Budget arithmetic says an 8 B/cyc engine with RX/scan overlap
reaches ~1 µs/frame ≈ 1.4 GB/s — **above the R1 floor with the existing
shell, 1518 B frames, and AF_PACKET**. Char-devs re-enter only for the
5 GiB/s target (jumbo frames + kernel bypass). Full ladder in
`specs/p2-dataplane.md` (DRAFT): P2a windowed host path (software, 2.3×
today) → P2b harness v3 + wide datapath (partial-only, crosses the floor) →
P2c jumbo + char-devs (static + driver, the original P2, now last).

Method note: the 28 µs real RTT vs the CLI's 40 ms illustrates why the AC
measures inside a tight loop — one-shot timings through a cold path measure
the host's scheduler, not the device.

## 5. Ideas for future experiments

- P2a: window the host MATCH dispatch path (same credit loop as the bench,
  honest loss accounting); re-run AC-3-3 clause 2 — expect ~120 MiB/s recorded
  in the SKIP statistic.
- P2b: parameterize the circuit generator for N-byte/cycle scan (start N=8);
  xsim differential vs the model (R7), then a real partial through the
  R73a-gated flow, loaded atomically; define R45a CYCLES semantics under
  RX/scan overlap before measuring.
- Watch RM timing at 8 B/cyc (last child closed at +0.023 ns at 1 B/cyc) and
  LUT growth against the SLR2 pblock.
- AC-2-6 (single-tenant evict/reload) is now runnable on hardware via the
  atomic load path — wire it to the `device_iface` fixture like AC-3-3.
- P2c prerequisites when reached: `MAX_PKT_LEN` 9600 rebuild, R78 length-field
  rev (u16 caps payload at 1486), `PACKET_MMAP` vs `dma_ip_drivers` decision
  (repo confirmed reachable), and the F3/R68 transport-binding spec change a
  driver swap would force.

---

# EXPERIMENT 15 Jul 2026 14:23:02 JTAG Wedge Recovered In-Band — User+QDMA Soft-Reset Sequence, and the First R45a Counter Read on Silicon :complete:

## 1. Hypothesis

The previous entry's fix direction (c) — a host-reachable user-box soft reset —
exists, is verifiable in the shell's own RTL rather than by blind-poking, and
can reproduce the power-on reset ordering in-band; if so, a JTAG-wedged RP can
be recovered without a cold power cycle, and the R45a CYCLES/BYTES counters
become readable on real silicon at last.

## 2. How

- **Equipment:** U250 on nf-server06 (`0000:02:00.0`, PYRO PR shell build
  `0x07140219`), after an operator cold power cycle restored the boot state
- **Software:** Linux 6.8.0-134, matching `onic.ko`, Vivado 2025.2 `hw_server`,
  `scripts/pyro_hw.py`, new `scripts/pyro_user_reset.py` and
  `scripts/pyro_wedge_recover.sh`
- **Method:** verify the reset chain in RTL first (never poke unverified);
  validate the register on a healthy card; then the wedge-risking experiment
  with the operator's explicit go: JTAG-load the preserved `abc[a-f]{2}`
  partial → confirm wedge → escalate resets until the child answers.

### Key commands

```bash
sudo python3 scripts/pyro_user_reset.py            # BAR2 0x014 user reset
sudo bash scripts/pyro_wedge_recover.sh            # full in-band recovery
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py probe
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py match "xxabcdeyyabcffz" --slot 1
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py perf --slot 1
```

## 3. Observations

- **The reset register exists and the whole chain is verifiable in RTL.**
  OpenNIC `system_config` (BAR2 `0x000–0xFFF`): `0x014` user reset (WO,
  edge-triggered, self-clearing), `0x018` status (RO). Bit 0 →
  `user_rstn[0]` (`open_nic_shell.sv:472`) → `box_250mhz.mod_rstn[0]`
  (`user_plugin_250mhz_inst.vh:82`) → `pyro_250mhz`'s `generic_reset`
  (100 cycles) → `axil_aresetn`, the reset of **both** `pyro_rp_inst` and the
  box's static-side logic (`pyro_250mhz.sv:163`) — the coordinated
  static+RP interface reset the JTAG path lacks.
- **Healthy-card validation passed:** pulse → boot stub still answers.
  (Status polls always read done: 100 cycles @125 MHz ≈ 0.8 µs, far below
  mmap read latency.)
- **User reset alone does NOT recover a wedged RP.** After JTAG-loading the
  pattern partial (39.9 s, wedge confirmed), the pulse changed nothing.
  Interface counters located the residual state: probe TX +3 (requests leave
  the host), RX +0 (nothing returns), and 5 pre-existing oversized RX frames
  (14,255 B / 5 pkts on a 1500-MTU link, 1 drop) — garbage the C2H path
  emitted while RP outputs floated during reconfig, leaving the **QDMA C2H
  stream engine stuck mid-frame**, outside every reset pulsed so far.
- **The full power-on-equivalent sequence recovers it**
  (`scripts/pyro_wedge_recover.sh`): `rmmod onic` → user reset (`0x014` b0) →
  **QDMA subsystem soft reset** (`0x00C` b0 — drives only the QDMA IP's
  `soft_reset_n`; `sys_rst_n` is `pcie_rstn` from the edge connector, so the
  PCIe link survives, `qdma_subsystem_qdma_wrapper.v:299,458`) → `insmod` +
  link up. Probe: **`device_usable=true`, `static_shell_id=0x02020000`** —
  the *pattern child's* identity (BUILD16=0), not the boot stub's `0x3841`.
  **The JTAG-loaded child answers. No power cycle.**
- **First R45a counter read on real silicon.** `abc[a-f]{2}` on
  `"xxabcdeyyabcffz"` → `MATCH_REPLY count=2` (both candidate windows,
  host-re-verify flags set per R78.7); `PERF` → **`CYCLES=17 BYTES=15`**
  (68 ns, 0.882 B/cyc @250 MHz). Negative control `"no pattern here at all"`
  → `count=0`, `CYCLES=24 BYTES=22` — counters track the most recent scan
  exactly. Probe remains `device_usable=true` afterwards.
- `onic` assigns a fresh random MAC each insmod (`00:0a:35:83:9c:71` →
  `00:0a:35:f3:6f:9a`) — nothing may key on it.

## 4. Data analysis

The wedge was never one fault but two, stacked: the box-side AXIS desync
(cleared by the user reset) and stuck mid-frame state in the QDMA C2H engine
(cleared only by the QDMA soft reset + fresh queue contexts). That is why the
previous entry's power-cycle conclusion held — power-on is the only *single*
action that resets both — and why each reset tried alone looked like a
falsified hypothesis when it was half of the answer. The interface counters
were the decisive instrument: TX advancing while RX stayed frozen split
"child not answering" from "answers not arriving", and the five oversized RX
frames dated the corruption to the reconfig window itself. Method point worth
keeping: every register poke was RTL-verified end-to-end before touching the
card (the previous entry's "do not blind-poke" discipline), which is what made
escalation safe enough to run over a live PCIe link. With
JTAG-load + `pyro_wedge_recover.sh` the project now has a **working
partial-reconfig path** — the shell-rebuild/ICAP fix directions (a) and (b)
drop from "blocking" to "nice to have". R45a/R78.11 are hardware-verified;
the last model-only claims in the perf story are gone.

## 5. Ideas for future experiments

- Fold `pyro_wedge_recover.sh` into `pyro.device.load_partial` (or a
  `load --recover` flag) so a JTAG load is one atomic, always-recovered
  operation; then re-run the AC-3-2/AC-3-3 device-gated skips on real HW.
- Real benchmark next: large corpora through MATCH with CYCLES/BYTES per run
  (R1/R2 win-regime calibration on silicon, not just host-side timing).
- Multi-child residency: build a second pattern partial, exercise slot
  tier-upgrade (AC-3-2) on hardware with load+recover between swaps.
- Root-cause the C2H garbage burst: a `dfx_decoupler` in the next shell spin
  would suppress it at the source (fix direction (a), now unhurried).
- Check whether the QDMA soft reset alone (without the user reset) suffices —
  would simplify the recovery script; not tested because the ordering was
  chosen to mirror power-on.

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

---

# 2026-07-25 — Panic root-cause + multi-queue H2C frame-loss investigation

## Panic (04:00:26, resolved)

`BUG: NULL pointer deref addr 0x80` during onic probe, right after the
qdma-pf→onic swap on a JTAG-wedged card. `onic_q_handler` dereferences
`priv->rx_queue[qid]` (NULL until ndo_open); a wedged RP holds a queue MSI-X
vector pending across the swap and fires it the instant request_irq completes.
`napi` sits at +0x70 in onic_rx_queue, `napi.state` at +0x10 → 0x80. Fixed:
NULL guard in the handler (open-nic-driver 9282346) + the swap-control path
now pulses user[0]/shell[0] resets before insmod (stringy-fpga 8ddee80).
Separately: the Jul 20 09:40 outage shows a silent log stop — power cut, not
a panic.

## x4 core on silicon

`pattern_becf73e88b6f1c561308914848c69ab0_x4_partial.bit` (built 03:54, fmax
259.8 MHz, pr_verified) loads and answers MATCH. Peak measured: **5.27 GB/s =
5.14 GiB/s at W=64** (4 TX queues, jumbo) — above the 5 GiB/s target — but
runs are not reliably loss-free (below), so the R1-target number cannot be
certified yet.

## Multi-queue H2C stochastic frame loss (open)

Symptom: with ≥2 H2C queues active, individual MATCH_REQUEST frames vanish
(~1/1500 at 4q); the reply never exists, the reader then eats the qdma-pf 10 s
request timeout, and the bench window aborts (large LOST numbers are that
abort draining, not additional per-frame loss). Aborted windows leave
in-flight frames that flush later with no reader — runs following aborted
runs look catastrophically worse until a reset + fresh queues.

Evidence matrix (all on silicon today):

| Config                            | Result |
|-----------------------------------|--------|
| 1 queue — every window, any core  | 0 loss (repeated, incl. MAXFETCH=0) |
| ≥2 queues, x4 core, jumbo         | lossy |
| ≥2 queues, N=8 core, jumbo        | lossy (same stack was 0-loss Jul 20) |
| ≥2 queues, small frames (1-desc)  | lossy → not multi-desc interleave |
| Direct-intr / auto / poll mode    | all lossy → not the IRQ re-arm race |
| hw_server killed                  | lossy → not JTAG/TAP interference |
| WB_ACC_INT 5→0                    | lossy (no change) |
| MAXFETCH 2→0                      | ~100× worse → fetch-latency dose-response |
| MAXFETCH 0, 1 queue               | 0 loss → multi-queue is the trigger |
| Sysmon                            | 58.8 °C, VCCINT 0.844 V — not thermal |
| PCIe                              | Gen3 x16, no AER — not the link |

Drop point: `qdma_subsystem_h2c.sv` ("Drop error packets") silently discards
H2C packets the EQDMA5.0 Soft IP flags with `tuser_err`. char-dev write()
returns success (writeback CIDX advances), so the loss is invisible to the
host. DMAR history: 3 read faults (Jul 20 02:25–02:29, fault 0x06) + 1 write
fault (Jul 25 16:05, fault 0x05) from 02:00.0 — device DMA to unmapped IOVAs;
consistent with engine-side stale/erroneous fetches; IOMMU is in translated +
lazy-invalidation mode. dma-ctl context reads during traffic aggravate loss
(CSR/context engine contention) — same engine family as the TCP_CSR_TIMEOUT
(0x12EC) latched on the wedged card at 03:56.

Conclusion: defect (or IOMMU-interaction) in the EQDMA5.0 Soft IP multi-queue
H2C descriptor path. Host software, harness RTL (both old and x4), and all
driver modes exonerated.

Candidate next steps (decision needed):
1. Host retransmission layer in the native loop (seq-idempotent MATCH makes
   retry safe) — converts ~0.1% loss into goodput noise; unblocks the 5 GiB/s
   certification; protocol-level adoption would need a spec amendment.
2. Reboot with `iommu=pt` or `intel_iommu=strict` to split IP bug vs
   IOMMU-latency interaction (pt removes write-protection from the stray-write
   failure mode — do it as a controlled experiment only).
3. Add an err-drop counter to `qdma_subsystem_h2c` in the next static-shell
   spin (needs QSPI reflash) for direct visibility.
4. Try EQDMA IP version bump / AMD support case with the evidence matrix.

## 2026-07-25 evening — steps 1+2 of the follow-up ladder

**1. Retransmit watchdog (cc253c0).** Two-tier: positional hole detection
(2W + MAX_TXQ behind the send watermark, ~0.7 ms at W=64) + 10 ms full-stall
blanket resend, rotated across TX queues. Isolated losses heal at ~1 ms and
windows complete with every frame accounted. NOT sufficient alone: loss
arrives partly as ~1 s per-queue episodes, and a >10 s C2H freeze on queue 0
(the sole reply queue) still aborts the reader at the driver timeout.

**2. IOMMU experiments (runtime domain switch, swap-script `iommu` mode).**
Group 52 default is DMA-FQ (lazy invalidation). Strict (`DMA`): still lossy —
the lazy-unmap race theory is dead. Identity (pt-equivalent, brief controlled
run): still lossy (RETX 1-2 per 438-frame window) — translation removed,
loss persists. **The IOMMU is exonerated as root cause**; the DMAR faults on
record are consequences (device DMA against torn-down mappings after
timeouts), not causes. Domain restored to DMA-FQ.

**New observation: cumulative degradation across soft resets.** Loss/episode
rates have worsened monotonically through the day regardless of user+shell
soft resets, driver reloads, queue re-creation, IOMMU domain, or driver mode.
The morning-after-cold-boot state (1 loss per ~3000 frames) has not been
recoverable since. Soft reset does not reconfigure the static shell —
hypothesis: state decay inside the EQDMA5.0 soft IP (or its clocking) that
only full reconfiguration (QSPI cold boot / full JTAG program) clears.
Next cold boot should re-baseline: expect near-clean multi-queue behavior
initially, degrading with accumulated multi-queue traffic.

## 2026-07-25 late — instrumented shell built, flashed; x4 partial rebuilt

Full DFX build of the instrumented jumbo shell (H2C pkt/err counters,
84d1228): PR_VERIFY_ALL_OK, static timing met, ~2h40m. QSPI flashed over
JTAG with the card live (documented operator risk decision; host survived,
golden image untouched) — activates on next COLD power cycle. x4 partial
rebuilt against the new static: pr_verified, met_timing, fmax 256.9 MHz —
do NOT JTAG-load it before the cold boot. Old-shell DCPs preserved in
hw/dfx/build/dcp.jumbo-20260717.bak. Demo walkthrough: docs/demo.md.
Post-boot: read h2cstats before/after a 4q bench — err delta is the direct
measurement of IP-corrupted packets for the AMD case.

---

# 2026-07-26 — cold boot: instrumented shell live, counter decode dead, re-baseline results

## Cold boot (04:18) and bring-up

The power cycle happened between 04:06 and 04:18. The QSPI image took:
`build_timestamp=0x07251929`, static_shell_id `0x0202f370` (ID stub) —
the instrumented shell is on silicon. Bring-up was by the book: vermagic
ok, `pyro_wedge_recover.sh`, x4 partial load in 29.8 s, probe
`0x02020000`, MATCH round-trip correct. All pr-build artifacts survived
the power cycle (no 0-byte files).

## H2C counters unreadable — decode bug in 84d1228 (fixed, rebuilding)

`h2cstats` reads BAR2 0x5000/0x5110 and got 0xDEADBEEF — the register
file's default. Root cause: `qdma_subsystem_address_map` subtracts
C_SUBSYS_BASE_ADDR (0x4000) before the subsystem register slave
(`qdma_subsystem_address_map.sv:146`), so the register file sees
0x000/0x110, and the case labels added in 84d1228 (`15'h4000`/`15'h4110`)
were unreachable. The counters themselves are in the fabric and counting;
only readback is dead. Fix: decode at 0x000/0x110 (commit 46186bf).
Full DFX rebuild launched 04:2x (`dfx_build_20260726_ctrfix.log`);
needs x4 partial rebuild, QSPI reflash, and ANOTHER cold power cycle.
Lesson: the stub register block had never had an implemented register, so
nothing had ever exercised the base-strip path on silicon.

## Re-baseline benches (current silicon, x4 child, jumbo)

| Run | Result |
|---|---|
| 1q, 4 MiB/window | 0 loss, peak 2.75 GiB/s @ W=32 |
| 4q, 4 MiB/window (first mq traffic since boot) | RETX 1–3 per 438-frame window (~1/340), peak 3.09 GiB/s @ W=64 |
| 4q, 32 MiB/window | heavily degraded: RETX=417 @ W=4, RETX=681 @ W=64, throughput collapse to 31/171 MiB/s in those windows |
| 1q, 32 MiB/window (AFTER the degraded 4q runs) | 0 RETX in 24.6k frames, 2.56 GiB/s — 1q path untouched by the degraded state |

Conclusions that revise yesterday's picture:

1. **Cold boot does NOT durably re-baseline.** The very first 4q run was
   already at ~1/340 (vs the historical fresh-boot 1/3000), and within
   ~25k multi-queue frames (~250 MB) windows were collapsing. Decay is a
   function of accumulated multi-queue H2C traffic, not uptime.
2. **The 1q path is immune even while the mq path is degraded** — same
   boot, interleaved runs. Whatever state accumulates lives strictly in
   the multi-queue arbitration/fetch path of the IP.
3. Yesterday's 5.14 GiB/s @ W=64 did not reproduce (3.09 GiB/s best 4q
   today). Plausibly the same degradation: yesterday's peak was measured
   in the first mq windows after a cold boot with less prior mq traffic.
   R1-certification remains blocked on the loss issue either way.

AMD case doc updated with the cold-boot re-baseline evidence
(symptom §4). Next concrete step stays: counter-fixed shell → flash →
cold cycle → err-count deltas for the case.

## 2026-07-26 morning — counter-fixed shell built and flashed; awaiting cold cycle

Full DFX rebuild with the decode fix (46186bf): PR_VERIFY_ALL_OK, 0 errors,
~2h40m. Static WNS -0.015 ns — single violated path, entirely inside the
CMAC `txoutclk_out[0]` clock group (CMACE4 hard block → tx_slice), the
inherited-waivable 2025.2 condition documented in dfx_build.sh; pyro_rp and
box_250mhz close. (Last night's +0.042 was the lucky end of the same
distribution; Ethernet FCS covers the marginal path.) x4 partial rebuilt
against the new static: pr_verified, met_timing, fmax 260.8 MHz. The
previous x4 bit (matches the 2026-07-25 static) is preserved as
`*_x4_partial.bit.static-20260725.bak`.

QSPI flashed 07:4x over JTAG with the card live (same operator-risk
procedure as 2026-07-25; erase+program+verify clean, host survived, golden
untouched). The fabric now holds the SPI-programmer design — the card is
dead to PCIe until the next cold boot. **Next cold power cycle activates
the counter-fixed shell; then `h2cstats` before/after a 4q bench gives the
direct tuser_err packet count for the AMD case.**

## 2026-07-26 afternoon — cold cycle done; counters live; tuser_err is NEVER asserted

Cold power cycle at ~16:50 activated the counter-fixed shell
(`build_timestamp=0x07260427`). Bring-up was textbook: enumerated at
02:00.0, onic vermagic already matched 6.8.0-136, wedge-recover + probe
clean, x4 partial loaded in 44.7 s, control-path MATCH good.

`h2cstats` now reads real values (decode fix 46186bf verified on
silicon). Counter accounting, all runs jumbo, x4 child, same boot:

| Run | frames+probe+retx sent | pkts delta | err delta | missing |
|---|---|---|---|---|
| 4q, 4 MiB/w (first mq traffic) | 3066+1+5 = 3072 | 3067 | **0** | 5 = exactly the 5 RETX'd originals |
| 1q, 4 MiB/w (control) | 3066+1+0 = 3067 | 3067 | **0** | 0 — exact to the packet |
| 4q, 32 MiB/w (degraded) | 24577+1+3630 = 28208 | 24481 | **0** | 3727 (13.2%) |

(The +1 is the bench's `probe_device` round-trip; the 1q run pins the
counter as exact, which makes the 4q shortfalls trustworthy.)

**Headline: the err counter stayed 0 through ~28k delivered packets
including a heavily degraded window. The EQDMA IP never asserts
`tuser_err`. Lost packets simply never emerge on the H2C AXIS interface
— silent non-delivery inside the IP, not delivered-with-error.** This
kills the "shell ignores tuser_err so corrupt payloads fail framing"
hypothesis (AMD doc "Where the packets go" rewritten accordingly; the
upstream OpenNIC drop-on-error TODO is irrelevant to this bug).

Heavy-run new severity datum: W=2 window collapsed to 1.6 MiB/s with
**LOST=525 — the retransmit watchdog itself gave up**, first permanent
application-visible loss. RETX=1796 at W=32 (49.2 MiB/s) in the same
sweep; windows in between partially recovered (938 MiB/s at W=4) —
episodic, consistent with symptom §3.

Fresh-boot first-mq loss today: 5/3072 ≈ 1/614 (yesterday 1/340, day
before 1/3000) — the fresh-boot baseline itself is noisy.

Bench peaks today: 4q 2.66 GiB/s @ W=32, 1q 2.29 GiB/s @ W=8. No
recurrence of the 5.14 GiB/s number.

Card parked in control mode, probe clean. AMD case doc is now
evidence-complete: direct boundary counts replace the inference in
"Where the packets go". R1 certification still blocked on the mq loss.

## 2026-07-27 — Phase 2 opened: SNORT-PF (branch phase2-snort)

Snort3 community rules (4,017 alert rules) unpacked to
`third_party/snort3-community-rules/`. Found the dormant SNORT-PF draft
spec (`specs/snort-rule-offload.md` v0.1.0, 2026-07-15, unadopted) —
rule-group pattern-set partials, nomination-only prefilter, residency
swap by observed port mix — exactly the stated phase-2 goal. Wrote
`docs/phase2-snort-plan.md`: adoption recommendation with a 3-item
amendment slate (P2d char-dev transport at 2.3 GiB/s revises the draft's
tens-of-MB/s ceiling ~100×; 8 B/cyc x4 harness revises SF6; CMAC WNS now
−0.015 vs the recorded −0.427), OQ-1..5 recommendations, and the S1–S4
work breakdown. Per the spec's §11 gate, implementation waits on the
owner adopting at 1.0.0; the SR1 triage parser (validate against
SF8–SF11 counts) is the authorized-as-analysis first step.

## 2026-07-27 — S1 build attempts (sid 1927): two toolchain lessons

Attempt 1 (x4, 8 B/cyc): synth_design ABORTS (Synth 8-7098, exit 134)
elaborating the generator's per-stage closure loops at NSTATES=151 —
first pattern circuit past ~100 states. Fixed with `-stack 2000` on the
Vivado batch invocation (05c0148); synth then finishes in 64 s.

Attempt 2 (x4): killed at the 7200 s R84 timeout, workdir destroyed
(pre-pr_verify failures rmtree; only post-verify workdirs are preserved
per A3.5). No stage attribution. **Calibration warning for SR8/AC-S2-2:
one 151-state case-folded 15-byte literal at 8 B/cyc × 4 cores does not
close in 2 h on this host, while 8-state abc[a-f]{2} took 55 min. The
SF6 model estimated 1,224 LUTs for this child — clearly not predictive
at dpb=8.** GROUP_MAX=256 sizing rests on that model; expect AC-S2-2 to
force either a repartitioned closure-stage pipeline in the generator or
much smaller groups.

Attempt 3 (running): RP_CORES=1, same 8 B/cyc harness, workdir kept via
rmtree stub + stage monitor for attribution.

Attempt 3 post-mortem: single-core dpb=8 spun exactly like the x4 —
synth "finishes" in 64 s then one thread pegs 100% CPU with zero log
output (killed after 1 h). The spin is per-automaton, in a synthesis
optimization on the closure logic, so core count is irrelevant.
Root datum: NSTATES=151 comes from R15's case-fold construction itself
(~10 NFA states per folded char of the 15-byte literal), not from the
dpb=8 position expansion; but at dpb=1 the closure logic is one stage
(430 RTL lines) vs eight (1,999) — same shape as every circuit built
to date. Attempt 4 (running): dpb=1, single core, stall watchdog armed.
**S2 blocker candidate: the dpb=8 closure codegen must be re-staged
(pipelined/partitioned) before any 8 B/cyc group circuit is feasible.**

## 2026-07-27 evening — S1 built, loaded, and the case-fold lowering lesson

Attempt 4 (dpb=1, x1) BUILT: 3024 s, pr_verified=True, met_timing=True,
fmax 253.6 MHz, luts=7852 ffs=2063 (est was 1224/663 — SF6 under-predicts
6.4x here; another AC-S2-2 calibration datum). JTAG load + wedge-recover
brought it up; ID_REPLY payload = {static_shell_id 0x02020000, harness
0x00010000, rp_child_id 0xcc442279} — byte-swapped low-32 of the pattern
hash 792244cc..., i.e. SR14 identity confirmed on silicon.

**But AC-S1-2 FAILED on the negative corpus: the child nominated a window
at every position of every input.** Root cause is NOT a bug — it is R15
working as documented. `generate()` was called with a *str* pattern and
re.IGNORECASE but no re.ASCII: in str mode that path deliberately
over-approximates to "any code point" (OA_CROSS_LENGTH_CASEFOLD,
automaton.py:230-237) because Unicode simple folds can cross UTF-8
lengths ('K' <-> U+212A). The circuit was complete but matched
everything: NSTATES 151 instead of 16.

Snort content is BYTES. `generate(b"authorized_keys", re.IGNORECASE)`
(or str + re.IGNORECASE|re.ASCII) gives the exact 2-byte fold set:
NSTATES=16, over_approx=(), and in the model the positive yields exactly
[(5,20)] while the negative yields []. The software model reproduced the
hardware's behaviour exactly in both cases — the model/silicon agreement
is itself evidence for the R54-style oracle.

**SR3 amendment candidate for S2:** the spec's "nocase via the existing
ASCII case-fold (PYRO R15)" MUST be lowered in bytes mode; a str-mode
lowering silently produces a match-everything prefilter that still passes
any completeness-only test. Worth an explicit conformance test.

Attempt 5 (running): bytes pattern, dpb=8, x1.

## 2026-07-27 late — S1 COMPLETE: AC-S1-1 and AC-S1-2 both PASS on silicon

Corrected child (bytes-mode nocase, dpb=8, x1): built in 2697 s,
payload_kind=pr_bitstream, pr_verified=True, met_timing=True,
fmax 252.02 MHz, luts=7685 ffs=1983, 3,611,668 B. **AC-S1-1 PASS**
(manifest + SR4 over-approximation classes + sidecar gid:sid map).

JTAG load 14.0 s (fastest yet — no wedge recovery needed this time).
ID_REPLY = {0x02020000, 0x00010000, 0x61c9fde7}: rp_child_id is the
byte-swapped low-32 of pattern hash e7fdc961..., SR14 identity satisfied.

AC-S1-2 run via tests/data/snortpf/ac_s1_2.py (reads C2S TCP payloads out
of the pcaps, MATCH_REQUESTs them, maps pattern_id -> gid:sid via the
sidecar):
  positive: 1 nomination — 192.168.50.10:40222 -> 10.0.0.21:21, 1:1927,
            window b'RETR AuthoRized_Keys' (mixed case, so the R15
            byte-fold is load-bearing)
  negative: 0 nominations
  Snort 3.12.2.0 re-verification: positive fires 1:1927 exactly once,
  negative silent. **AC-S1-2 PASS.**

Two protocol notes for the S3 daemon:
1. `out_cap` in MATCH_REQUEST is NOT optional — sending 0 returns
   count=0 with OVF set (looks like "no match", is really "no room").
2. Hardware `start` is the chunk start (start_off), not the match start:
   silicon reported 0..20 where the software model reports (5,20). Only
   `end` is the exact match end. Nomination offsets MUST be attributed
   from `end`; treat [start,end] as the conservative candidate window
   (R78.7) — the host re-verifier resolves it either way.

Phase S1 is done. S2 opens with three carried-forward blockers recorded
above: SF6 cost model under-predicts ~6x, the dpb=8 codegen spins synth
past ~150 states, and SR3 needs the bytes-mode case-fold conformance test.

## 2026-07-27 night — S2 opens: the synth blocker is a codegen bug, fixed

Root cause of the "synth finishes then spins forever" stall, found by
reading the emitter: the eps/assert closure is emitted as
`for (it = 0; it < NSTATES; it = it + 1)` with **`it` never referenced in
the body** (generator.py:193 and :536) — a pure NSTATES-fold *textual
replication* of the relaxation body. Vivado unrolls it into NSTATES x E
blocking read-modify-writes of one NSTATES-bit vector, a serial
self-referential chain of ~(dpb+1)*N^2*E bit-nodes, and burns
superlinear time in "Cross Boundary and Area Optimization" — a phase
that emits NO log line while running, which is exactly the observed
signature. (The earlier Synth 8-7098 stack abort was the same cause.)

The real fixpoint depth is tiny: `closure_passes(au)` computes it as the
max over singleton start states of the passes to converge. Correctness is
structural, not empirical — the body only sets bits from single source
bits, so it is monotone and *additive*: the closure of a union of start
sets is the union of the closures, hence the worst case is attained at a
singleton. Assertion conditions are taken as always-true (disabling an
edge can only shrink reachability, never lengthen a chain), so the result
upper-bounds every runtime valuation. Measured K over the corpus: 1..4.

Result on the exact circuit that was killed twice at 7,200 s
(151 states, E=45, dpb=8): **synth_design completes in 2 min 3 s,
1,358 LUTs.** Pure `content:` literals have E=0 and emit no closure block
at all — which is why the final S1 child built fine.

Verification: tests/hw/xsim_diff.py (drives real R78 frames through the
generated engine + wrapper under xsim and compares replies byte-for-byte
against the model) PASSES 10/10 on the canonical dpb=8 pattern and on the
K=2/K=3 eps-heavy patterns `(a*)*b`, `((x?)?)?y`, `(a?b?c?d?e?f?)g`.
Note a PRE-EXISTING failure unrelated to this change: `^(?:a|)(?:b|)(?:c|)$`
mismatches 1/10 replies on BOTH the patched and the stock generator
(verified by stashing) — a real model-vs-RTL disagreement on anchored
empty alternations, logged for S2 follow-up.

Emitted text changed => GENERATOR_VERSION/HARNESS_VERSION 2.2.0 -> 2.3.0
(R47b), in pyro/hdl/generator.py AND src/pyro_rt.c (the C runtime pins the
same constants and rejects mismatched artifacts with code 7; `make`
rebuilds). 687 unit tests pass. **Consequence: every cached bitstream and
the flashed S1 child are now stale — AC-S1-2 must be re-established on
the next build.**

Acceptance suite after the 2.3.0 bump: 724 passed, 10 skipped, 1 failed in
1:04:52. The single failure is ENVIRONMENTAL, not a regression:
tests/acceptance/test_ac2b2_probe.py::test_probe_privilege_free_exact_canonical_reason
asserts its own precondition `not has_cap_net_raw()` (line 67) — it is
written for a dev host WITHOUT the capability, and this host's
.venv-pyro/bin/python3 carries the cap_net_raw xattr on purpose so the
probe works unprivileged. It fails before touching any generator code.

## 2026-07-28 — S2 lands: group emitter + oracle committed; AC-S2-2 BUILT

The S2 implementation workflow (7 agents, adversarially reviewed) landed as
commit 00143e8: SR6 packing (`pyro/snort/groups.py`), the SR7 group emitter
(`generate_group` — N automata, one shared harness, slot index IS
pattern_id), `GroupCircuitModel` sharing `_scan_windows` with the
single-pattern model, and the AC-S2-3 two-sided oracle (closed-form
nomination oracle + Snort 3.12.2.0 differential; FP census pinned by
EQUALITY per SR4 class: header_predicate 2, case_fold 9, anchor_strip 48,
dropped_conjuncts 5, unclassified 0; the only completeness misses are the
two SF11 %-encoding blinding cases, also pinned by equality). The review
found 7 real majors, all fixed — the standouts: (1) an ungated wrapper
around a group engine is legal Verilog that silently drops bytes on every
stall; `check_engine_pairing` now guards both the in_ready AND
datapath-width axes and a mismatch raises `ConfigurationError` *without*
R65 cache poisoning; (2) the oracle suite wasn't executing the emitted RTL
— it now runs xsim gates, proven non-vacuous by a sabotaged priority
encoder failing 6/12 replies; (3) `estimate_group` omitted every group
harness register (pend/pend_base/skid). R41 resume bound measured:
MAX_WINDOWS_PER_START=2 for this ruleset (prefix-chain anchors), safe
under the out_cap=61 ceiling with margin. Gates: 790 unit + 32 oracle
tests green; make ABI OK.

**AC-S2-2: the full $HTTP_PORTS/0 group (253 slots / 256 rules) builds
through the real PR flow and meets timing.** Driver
`.superpowers/pr-builds/pr_build_driver_s2_group.py` (SynthJob built
directly from the GeneratedGroup; `--n-slots M` prefix subgroups carry
their own derived identity). Against the locked counter-fixed static:

| build | LUTs | FFs | fmax | wall |
|---|---|---|---|---|
| N=32 gate  (03172668…) | 7,800 | 2,383 | 254.84 | 46 min |
| N=64 gate  (293105d6…) | 7,994 | 2,696 | 252.21 | 45 min |
| N=253 FULL (b1a418fc…) | 10,147 | 5,681 | **250.44** | 53 min |

All three: pr_verified, met_timing. Identity matches the pinned group
hash exactly (rp_child_id 0xfc18a4b1). Marginal cost ≈ 10.6 LUTs/slot on
a ~7.6k fixed wrapper floor — 12.7 % of the 80k budget at N=253, so the
top-risk 256-bit pend priority encoder cleared with 0.44 MHz to spare.
Estimator predicted 26,982 LUTs (R74-conservative 2.7×). SF6 recalibrated
as SF21; spec bumped to 1.0.2. Next: JTAG load + on-silicon AC-S2-2
verification (and AC-S1-2 re-establishment — the 2.3.0 bump staled the
flashed S1 child).

## 2026-07-28 (cont.) — on-silicon: AC-S2-2 group resident; AC-S1-2 re-established at 2.3.0

The 253-slot group partial loaded over JTAG in 14.1 s (in-band recovery
clean), probe good. Live MATCH on `GET /view-source HTTP/1.0` returns one
entry: end=16 (exact — offset 4 + 12-byte anchor), pattern_id=40, and the
sidecar maps slot 40 -> [1:848, 1:849], the two view-source rules sharing
the deduped anchor. The oracle suite with PYRO_DEVICE_IFACE=ens2 runs
**33 passed, 0 skipped** — the SR18 hardware clause verified the resident
rp_child_id (0xfc18a4b1) and its nominations. Sidecar fix along the way:
`rp_child_id_low32` was the big-endian hex prefix of the hash, not the LE
CSR value; driver + all three emitted sidecars corrected (the S1 sidecar
had the same defect).

AC-S1-2 re-established: the flashed S1 child was 2.2.0 (stale per R47b).
Rebuilt sid-1927 at 2.3.0 — new identity 95b1d1d1…, pr_verified,
met_timing, 254.91 MHz, 7,729 LUTs, 46 min — loaded, and the pcap corpora
re-run: positive nominates 1:1927 on the mixed-case payload
(`RETR AuthoRized_Keys`, nocase carried by the R15 bytes fold), negative
0 nominations, Snort re-verifies (alert on positive, silence on
negative). **AC-S1-2: PASS.** Board left with the S2 group child
resident (re-loaded, MATCH re-confirmed).

S2 exit state: AC-S2-1/-2/-3 all pass; spec at 1.0.2 (SF21). S3 opens
with residency/rotation and the host filter daemon.

## 2026-07-28 — System summary: what this is, what it does, how it is built

A periodic orientation entry — the whole system in one place, as of the
end of Phase S2 (branch phase2-snort, spec snort-rule-offload v1.0.2,
PYRO spec v2.7.0).

**What it is.** A reconfigurable pattern-matching offload on an Alveo
U250 (xcu250-figd2104-2L-e, `nf-server06`, 02:00.0) with two product
surfaces sharing one shell: **PYRO**, a drop-in `re`-compatible Python
module (`pyro.re`) that transparently routes large, reused regex scans
to a matching circuit on the FPGA; and **SNORT-PF**, which compiles
hundreds of Snort community rules into a single pattern-set circuit that
acts as a candidate-nominating prefilter — the FPGA says "this flow may
match rule X", and Snort re-verifies every nomination, so hardware
over-approximation can never create a false alert.

**What it does today, measured.** The resident SNORT-PF child holds the
$HTTP_PORTS/0 group: 256 rules deduped onto 253 anchor slots, one
1 B/cycle shared harness, 10,147 LUTs / 5,681 FFs (12.7 % of the 80k
PR budget), fmax 250.44 MHz, verified on silicon (33/33 acceptance,
including the resident-identity clause). A MATCH round-trip on a
malicious URI nominates the right slot with the exact end offset; the
sidecar maps slot → [gid:sid,…]; benign traffic returns silence. The
PYRO x4 regex child (8 B/cycle, 4 cores, 260.8 MHz) demonstrates
~2.3 GiB/s zero-loss host-to-card scanning over one QDMA ST queue
(multi-queue is gated by the open EQDMA silent-loss issue — AMD case
filed). Circuit swap is JTAG partial reconfiguration, ~14–45 s including
automatic in-band wedge recovery.

**How it is built — the layers.**
1. *Shell (static):* an OpenNIC-derived jumbo shell in QSPI (counter-fixed
   build, `static_shell_id 0x02020000`), QDMA/EQDMA5.0 host interface,
   H2C packet/err counters at BAR2 0x5000/0x5110, CMAC tied off (no live
   wire traffic yet). One DFX dynamic region `pyro_rp` (slot 1, SLR2)
   receives every generated circuit as a partial bitstream against the
   locked static (`hw/dfx/build/dcp/static_routed_locked.dcp`).
2. *Generator (`pyro/hdl`):* pattern → byte automaton
   (`automaton.py`, bytes/UTF-8 encodings, R15 exact ASCII case fold) →
   one-hot NFA Verilog (`generator.py`), datapath 1–16 B/cycle. S2 added
   `generate_group`: N automata on one shared harness, slot index ==
   pattern_id, tombstones keep their index; a 256-bit pend register +
   priority encoder serializes same-byte accepts into the ring.
   Identity is a 128-bit domain-separated hash baked into CIRC_ID0..3;
   GENERATOR_VERSION/HARNESS_VERSION (2.3.0) are pinned in both the
   Python emitter and the C runtime, which rejects mismatches (R47b).
3. *Software twins:* every circuit has an executable model
   (`pyro/_circuit_model.py` — `CircuitModel` and the width-independent
   `GroupCircuitModel` share one scanner) and a native C model
   (`src/pyro_rt.c`) driven by the serialized automaton. RTL, Python
   model, and C model are three views of one artifact; differential
   tests keep them byte-identical (xsim_diff drives real R78 frames
   through the emitted Verilog under xsim).
4. *Synthesis & residency (`pyro/synth`):* an out-of-process service
   runs mock or Vivado toolchains; the Vivado PR flow links each child
   against the locked static, pr_verify's it, and emits partial.bit +
   manifest (fmax, LUTs, timing honesty). Bitstreams are cached by a
   descriptor key covering pattern bytes, encoding, flags, versions,
   and datapath width; misconfiguration (e.g. a group engine paired
   with a backpressure-deaf wrapper) fails loud *before* tool time and
   never poisons the cache.
5. *Transport:* R78 raw-Ethernet control protocol (EtherType 0x88B5:
   ID/MATCH/PERF, ≤61 24-byte match entries per reply, OVF + resume
   semantics) over the `onic` netdev, and a QDMA ST char-dev data plane
   for throughput; one PF swaps between them
   (`scripts/pyro_dataplane_swap.sh`).
6. *SNORT-PF pipeline (`pyro/snort`):* parse all 4,017 community rules →
   triage (3,896 anchor-compilable; anchors are fast-pattern content
   literals, everything else becomes a declared over-approximation
   class re-checked host-side) → SR6 packing (port-class groups,
   GROUP_MAX 256, stable identity, tombstone repack for weekly diffs) →
   group circuit + sidecar. Verification is two-sided: a closed-form
   nomination oracle asserted as set equality per slot, plus a Snort
   3.12.2.0 differential with the FP census pinned by equality — and
   the gates are proven to bite (re-created defects and sabotaged RTL
   fail them).

**Where it is going.** S1 (one rule, on silicon) and S2 (256-rule group,
on silicon) are complete. S3 builds all 21 groups, adds the residency
daemon that hot-swaps groups by observed port mix (~45 s/swap, SF20
hysteresis), content-chain lowering, and the weekly-diff incremental
rebuild path. S4 (ROM-baked shared trie, suppression pilot) is gated on
owner review.

## 2026-07-28 — S3 day 1: chain lowering, daemon, scheduler, drill; full build launched

**AC-S3-2 landed first, deliberately** — the lowering changes every
group's canonical bytes (the SR9 cache key), so it had to precede the
21-group build or the overnight Vivado run would be paid twice.

**The lowering** (`pyro/snort/lowering.py`): anchor content chains with
`distance/within` as bounded superset gap windows `[max(0,D), D+W]`,
per-fragment `nocase` as scoped `(?i:)` byte-fold groups, `offset/depth`
as `\A` prefixes, clean `R`-flagged cursor-anchored pcre fused as
literal⋅regex concatenation. Everything inadmissible stays a dropped
conjunct. Corpus: 245 chains, 91 `\A` rules, 36 fusions → 181 lowered
slots; groups.py v2 serializes pattern/flags/tail_span into the identity.

**The differential caught a real soundness bug in the first design.**
The initial `\A` lowering admitted tcp rules; AC-S2-3's Snort
differential immediately reported a **raw-anchor miss** on sid 509
(`depth 36`, service:http): Snort 3 binds a raw-cursor depth to the
current **PDU section** of an inspected flow, not to raw stream offset 0
— the anchor sat deep in a POST body and Snort alerted where our chunk-0
window could not. Exactly what SR16 exists to catch ("any diff is a
completeness defect, full stop"). Fix: `\A` prefixes only for
PDU-aligned rules (udp/icmp/ip, no `service` option); tcp offset/depth
stays a dropped conjunct, pinned by test. After the fix the entire
AC-S2-3 suite is green again and — a pleasing invariant — the
silicon-verified `$HTTP_PORTS/0` is **automaton-identical** to its S2
shape (its 6 offset/depth rules are all tcp/service, so nothing in it
lowered further).

**Oracle coverage** (`test_acs3_2_lowering_oracle.py`, 11 gates): the S2
closed-form oracle grew a stdlib-re longest-end enumerator (independent
of the automaton) + a parse-tree exemplar builder; every one of the 181
lowered slots passes model↔oracle equality at min/max gap widths; SR12
chunk-cut sweeps prove chain matches survive the (generalized) overlap
tail; the Snort differential on a 32-case chain sub-corpus shows **zero
misses**; sabotage (narrowed window, dropped fragment) is caught.

**The daemon** (`pyro/snort/daemon.py` + `scripts/pyro_snortpf_daemon.py`)
and **SR10 scheduler** (`pyro/snort/scheduler.py`): SR13 variable table,
port-mix histogram (most-specific-class signal — $HTTP_PORTS must beat
its $FILE_DATA_PORTS superset on port 80), SR15 tripwires, SR12 per-flow
tails, SR14 identity gate (mismatch → unfiltered, never misattribution),
trimmed-corpus OVF resume plus the new `\A`-flood rule (a buffer-aligned
request that overflows nominates every `\A` slot — the resume trim
cannot recover those windows; `literal/3` holds 59 of them against
out_cap 61). Hysteresis: challenger class needs a 2× lead sustained 60 s,
300 s dwell, round-robin within the dominant class. SR19 stats surface
complete. End-to-end on a synthetic pcap: mix-driven swap to
$HTTP_PORTS/0, dedup slot nominates both its sids, benign silent.

**AC-S3-3 drill green**: a 33-rule SF15-shaped diff dirties exactly 2
groups by SR9 key; tombstones never renumber; 19/21 groups hit a real
BitstreamCache; the rebuild's unfiltered window is SR19-visible.

**AC-S3-1 build launched**: all 21 groups, 2 concurrent Vivado
pr_bitstream jobs (~14 GB resident, on the R63c profile), SR9
cache-resumable. Expected overnight (~8 h at the measured ~46–53
min/group for the big ones). On-silicon serve/identity clauses follow
once artifacts exist; SR18 SKIP discipline until then.

**Owner slate drafted**: `docs/spec-amendments-s3.md` — A1 SR12 tail =
max floating span − 1; A2 the SR3 admission conditions (raw-only chains,
PDU-aligned prefixes with the sid-509 evidence, 255/384 bounds); A3 the
measured 21-group count; A4 the `\A`-overflow daemon obligation.

## 2026-07-29 — S3 day 2: all 21 groups on silicon-ready bitstreams; chain slot verified on hardware; AC-S3-1 green

**The build: 21/21 verified** (`pr_verified`, `met_timing`, fmax
250.69–258.80, median 253.61 MHz; 22.6 h of Vivado over ~17 h wall at
2-way concurrency). It took four passes, and the story is worth its
ink:

1. The plain sweep built 15/21. Six groups missed the R73a.1 RP-scoped
   gate by −0.005…−0.120 ns — every one a route-dominated (79–92%)
   boundary path from the shared `rp_wrapper` into ONE OF THE SAME TWO
   locked static flops (`c2h_slice…axis_tlast_reg[0]/D`,
   `h2c_slice…axis_tdata_reg[1][71]/CE`). The locked static's endpoint
   placement leaves those routes ~zero margin; each partial rolls dice.
2. **Vivado P&R is deterministic** — a failed job re-runs to the
   identical miss, so "just retry" is a no-op. The toolchain grew
   additive closure knobs (`pr_place_directive`/`pr_route_directive`/
   `pr_phys_opt`; default = byte-identical flow — the stock PR flow ran
   no `phys_opt_design` at all). Strategy knobs stay OUT of the SR9 key.
3. `--phys-opt` closed 4/6. `ExtraTimingOpt`+`AggressiveExplore` closed
   $FTP_PORTS/0. `any/2` sat pinned at −5 ps across four strategies and
   finally closed with `SSI_SpreadLogic_high` (251.89 MHz) — consistent
   with the failing route being inter-SLR.

**On silicon**: loaded the built `literal/0` child (14.8 s, in-band
recovery). SR14 identity 0x12d39e4c confirmed over the wire. The RPC
portmapper CHAIN slot (`(?s:\x00\x01\x86\xa0.{4,8}\x00\x00\x00\x03)`,
35 rules on one slot) nominates on hardware at both gap extremes with
exact end offsets, stays silent on an over-wide gap and on benign
traffic — AC-S3-2's lowered constructs verified on silicon, precision
included. Board left with `literal/0` resident.

**Wire transport bug found and fixed** before it could bite: the
daemon's `WireTransport` was written against a `probe_device` API that
returns `(bool, reason)` — `child_id()` could never read a real id, so
wire mode would have sat permanently "unfiltered" while looking healthy.
Rebuilt on the AC-S2-3 silicon gate's proven round-trip; verified live.

**AC-S3-1: 5/5** with the device (build evidence from the operational
cache; mix-driven hot-swap; SR14 gating SR19-visible; stats surface;
resident-child identity on the wire). AC-S3-2 and AC-S3-3 were already
green. **Phase S3's acceptance criteria all pass.** Remaining for the
owner: the docs/spec-amendments-s3.md slate (A1–A4), plus one new
finding recorded there — the two static boundary flops that made six
groups marginal are a static-rebuild question (register the slice
boundary?) if group churn keeps paying lottery tickets.

## 2026-07-29 — Demo run on silicon: S2 nominations, S3 pipeline, live hot-swap

Ran the demo doc end to end against the card after the S3 build, partly
to show the system and partly because a demo is the cheapest way to find
out which of your documentation has quietly gone stale. Both purposes
paid off.

**§8, the SNORT-PF surface.** Loaded the rebuilt `$HTTP_PORTS/0` child
(`e1e145ba…`, 14.0 s incl. in-band recovery); SR14 read back
`0xba45e1e1` matching the host computation. The three canonical
round-trips reproduced the S2 results *exactly* on the v2-format
rebuild — `/view-source` → slot 40 end 16, `/webspirs.cgi` → slot 85
end 17, `/index.html` → count 0 — which is the invariant worth having:
AC-S3-2 changed the group format, the identity, and the bitstream, and
did not change what this group recognizes. R45a counters read 0.923
B/cyc (dpb=1 group, as expected). Sidecar resolved slot 40 → sids
848/849, slot 85 → 900/901; one slot, several rules, because anchors
dedup.

**§9, the daemon through silicon.** A 7-session synthetic capture
replayed through `WireTransport` (real R78 frames, not the model):
5 nominations attributed to named rules, 1 gzip flow tripwired to
forward-regardless (SR15), benign flows silent. SR19 reported the whole
surface live — nominations by tier, tripwire hits per class, zero
identity mismatches, unfiltered seconds.

**The hot-swap, which is the S3 headline.** Scheduler with two real
groups (`$HTTP_PORTS/0`, `$SSH_PORTS/0`), both from the batch build,
availability gated on the artifacts existing:

```
HTTP traffic      mix {HTTP 1159}            -> no swap
shift to SSH      mix {HTTP 1143, SSH 1993}  -> holding (hysteresis)
                  mix {HTTP 1128, SSH 3959}  -> holding
                  mix {HTTP 1112, SSH 5897}  -> holding
                  mix {HTTP  910, SSH 6480}  -> SWAPPED to $SSH_PORTS/0
JTAG load 16.1 s (incl. wedge recovery); SR19 unfiltered_seconds=16.2
post-swap: nomination 1:1324 — an $SSH_PORTS rule
```

Three ticks of a growing SSH lead were **refused** before the fourth
crossed both the 2× margin and the sustain window: the swap is earned,
not reflexive, which is the whole point of SF20 hysteresis against a
~16 s blind window. And the blind window is *counted* — SR19's
`unfiltered_seconds` is the honest price of every rotation, visible
rather than swept up. The post-swap nomination on an SSH rule is the
proof the circuit actually changed, not just the bookkeeping.

**A demo case I got wrong, and the discipline that caught it.** My first
capture's "anchor split across segments" case produced *no* nomination.
Rather than assume a bug (or, worse, assume it was fine), I checked the
reassembled stream: `_get(b"/view-so")[:-2]` + `b"urce HTTP/1.1..."`
never contains `/view-source` at all — I had chopped the tail off the
whole request line, not split the anchor. **Silence was the correct
answer to a badly-posed question.** Rebuilt the case as a genuine
mid-anchor split (seg1 ends `GET /view-so`, seg2 starts `urce?f=x`):
neither segment contains the anchor alone, and the nomination fires off
the 64 B overlap tail. That is SR12 demonstrated; the first version
demonstrated nothing. Worth recording because a chunk-boundary test that
doesn't actually straddle the boundary is exactly the kind of green
light that means nothing — the same failure mode as a completeness test
that never exercises its constraint.

**Doc rot found and fixed.** `demo.md` §8 still cited the pre-AC-S3-2
identity (`b1a418fc…`/`0xfc18a4b1`) and the old `_full_` artifact stem;
the v2 group format rolled the hash and the batch driver renamed the
artifacts. Updated identity, paths, and post-route numbers (10,323 LUTs,
253.29 MHz — the S2 build's 10,147/250.44 were the same RTL, different
P&R roll), and added §9.5 with today's hot-swap and SR12 transcripts.
Board left on `$HTTP_PORTS/0`, answering.

## 2026-07-29 — Working-set study, and the scheduler it condemned

Ran the measurement the overlay discussion called for, on branch
`a5-working-set-study`, before writing any RTL. Question: does making rule
swaps *faster* (A5's loadable-table overlay) buy coverage, or is the
binding constraint how many rules fit resident at once (capacity, what
S4's trie buys)?

**Answer, 27/27 scenarios: capacity.** k = 1/2/4/8/21 resident groups give
28/56/76/84/100% byte-weighted coverage, while dropping fill latency from
16 s to 1 ms at fixed k moves coverage by less than a point — a banked
engine keeps serving while the next set loads, so slow fills delay
improvement rather than costing coverage. **A5 is not justified on coverage
grounds.** Its honest remaining case is build-time (weekly diffs without
Vivado), which this study did not measure and says so explicitly.

Discipline that made the result worth anything: the ruleset structure is
real (21-group packing; each rule's OWN header predicate through the SR13
VarTable, not the coarse port class, which would have inflated every
denominator), and every traffic assumption is *swept* rather than fixed —
skew 0/1.2/2.0, stationary/60 s/300 s phases, and a specificity weight
1/3/10 modelling "HTTP traffic trips HTTP rules." That last sweep is the
one that could have overturned the verdict. It didn't.

**And then the study condemned my own scheduler.** The SR10 residency
manager shipped two days ago measures at **1.6–10.3%** coverage where
simply pinning `any/0` gives **27.9%** — it is *worse than not scheduling
at all*. Confirmed analytically, no simulator: its byte-per-class signal
elects the `literal` class under a mixed load and rotates among groups
worth 0.7–1.6%. Root cause: matching the traffic's port class says nothing
about how many of a group's rules can fire, and `any`-token rules fire on
**every** port, so the three universal groups dominate at k=1. Every swap
it made was a loss plus a 16 s blind window.

Worth being precise about what this does and doesn't invalidate. AC-S3-1
requires the manager to hot-swap by observed port mix; it does, verified on
silicon, and the spec never claimed the heuristic was coverage-optimal. The
acceptance criterion stands. What was never measured until today was
whether the heuristic was any *good* — and it wasn't. Building the thing
and asserting it functions is not the same as asserting it helps.

**Fixed the same day.** `scores()` now ranks groups by
`V(g) = Σ_ports bytes[port] × rules_of_g_that_fire_on(port)`. Three
supporting changes: `RuleRef` carries the rule's own dst-port token (rule
text — resolved against the site's variable table at runtime, never hashed,
never compiled in, so SR13/SF16 are untouched); `PortMixHistogram` counts
per port instead of per class (aggregating first destroys exactly the
information the decision needs); and within-class rotation is **removed** —
swapping 256 resident rules for a different 256 leaves instantaneous
coverage unchanged and pays a blind window for it.

Re-measured on the same traces: **1.6–6.6% → 16.7–27.4%**, within 0.4–0.6
points of the static-pin ceiling (the residual is the warm-up load a pin
doesn't pay). 4–17×.

Verified on silicon, and the behavioural change is the nice part: with an
HTTP burst then SSH-only traffic, the new scheduler *refuses* to swap for
38 s, holding while V(HTTP) decays 295828 → 21216 and V(SSH) climbs to
50443, and swaps only when the challenger clears the 2× bar at ratio 2.38.
The old one made the same swap after ~5 s on raw bytes. Same destination,
earned instead of reflexive — trading a 256-rule group for a 4-rule one is
a bad deal right up until those 256 rules genuinely cannot fire.

849 unit + 64 S2/S3 acceptance green, AC-S2-3 still 33/33 with the silicon
clause. The remaining study recommendations — revisit GROUP_MAX/`any`-class
packing, treat capacity as the real lever, don't open A5 on coverage
grounds — are owner decisions, recorded in docs/studies/a5-working-set.md.

## 2026-07-30 — The reframe: Snort as workload, FPGA scheduling as the subject

A correction to what these experiments are *for*, and everything after it
follows from the correction.

Up to this point the work read as "make SNORT-PF cover more rules." That
had it backwards. The subject is **whether OS scheduler techniques can
swap functionality on an FPGA at run time**; the Snort ruleset is the
workload that makes the question concrete — a large, naturally
partitioned, demand-driven body of work with real traffic to drive it.
Coverage numbers are instrumentation, not the goal. Once stated plainly
this is obvious, but several days of experiments had been optimizing the
instrument.

The analogy the rest of the work runs on: the RP region is memory/CPU, a
circuit is a process, partial reconfiguration is a context switch, an
overlay table write is a *cheap* context switch, and an SR5 miss is a
benign page fault — wrong-but-safe, because the FPGA only ever nominates
and Snort re-verifies.

### The tenants

A scheduler with one kind of job is not a scheduler. Built seven tenants
over `pyro/sched/`, all sharing the Snort corpus as their data but
differing in the shape of their demand:

- pattern match (the existing prefilter), exact IP match, fixed-offset
  packet-header match, case-split matching,
- a **gang-scheduled** pipeline tenant — stages that are worthless unless
  co-resident, which is where `wasted_luts()` comes from,
- a **deadline** tenant with `preemptible_under()`,
- a **same-kind control** tenant, so treatment effects can be separated
  from tenant-heterogeneity effects.

`view_meet` had a bug worth remembering: it returned the *first* superset
view rather than the narrowest, so a payload-only gang could inherit a
frame constraint it never asked for. Fixed to select the minimal view.

### The frontier

The policy experiment initially reported a headline about 4× too large,
because the `static-pin` baseline conflated *packing* with *scheduling* —
it was being beaten partly for reasons that had nothing to do with
scheduling. Replaced with a best-fixed-set baseline: +181% collapsed to
+156%, and on the skewed tenant set the advantage vanished entirely to
+0%. Two further degeneracies had to be handled first (budgets below the
largest tenant zero everything; a 658× footprint skew made density
packing starve the large tenant so every policy came out identical), and
the first frontier sweep was aliased by phase alignment — `phase_trace`
ignored its seed, producing a non-monotonic curve. Randomized star order
plus ±30% jitter, averaged over nine seeds, fixed it.

Result: the frontier is a **ratio**, not a pair of numbers. Scheduling
pays while `s/P ≲ 0.3` (switch cost over phase length), and the curves
for P = 30/120/600 s collapse onto each other, which is the evidence that
it really is scale-invariant.

That single constant is what killed JTAG. At the measured 13.6 s partial
reconfiguration cost, `s/P < 0.3` demands phases longer than ~45 s. Any
workload that shifts faster than that cannot be scheduled by PR at all.
So: **abandon JTAG as the switch mechanism.** The only way to reach the
interesting part of the curve is overlays.

### A5, and the engine

Designed the loadable-table protocol and its identity layer, approved as
A5 (docs/spec-amendments-a5-overlay.md), then built it: host builder and
serializer, a behavioural model that executes the **serialized image**
rather than the in-memory automaton (so a layout bug has somewhere to
show up), and `hw/rtl/pyro_overlay_engine.v`.

Load time at real scale is **0.66 ms** for the 1.64 MB table in 172 jumbo
frames — against 13.6 s for PR, four orders of magnitude. That is the
whole point of the exercise: it moves `s` far enough left that the
frontier stops binding.

Three findings from building it:

The **uncapped** trie is 2.08 MB at 53–55 B/state and fits URAM, where
SF14 concluded it did not. State counts reproduced SF14 exactly
(11,078/21,841/38,700), which makes the disagreement about image size
alone and therefore trustworthy. The 16-byte anchor cap is no longer
required on memory grounds.

Two identity bugs were caught **by construction**, which is the argument
for the two-level identity in the first place: the host used
`zlib.crc32` (IEEE) against the RTL's Castagnoli, and the differential
caught it on its first run.

And a false pass worth recording as method. The URAM cascade fix
reported +0.383 ns; the number was worthless. The new pipeline registers
had two drivers (the memory block, plus an async reset), synthesis kept
the constant and discarded the real one, the bitmap read path became
dead code, and `opt_design` deleted all 50 URAMs. It was timing a
768-LUT stub. **Simulation cannot catch this** — the reset branch only
fires during reset, so xsim sees one driver, and the differential passed
identically before and after the fix. The run exited 0 and printed
comfortable slack; only the utilization report gave it away.
`scripts/overlay_ooc_timing.tcl` now refuses a verdict unless synthesis
raised zero critical warnings *and* the routed netlist still has its
memories.

With that fixed, full corpus meets 250 MHz out of context: 50/64 URAM,
108/160 BRAM36, 2364 LUT / 2209 FF, WNS **+0.089 to +0.212 ns**. Quoted
as a range on purpose — every worst path has 0–1 logic levels and 91–98%
route delay, so with no pblock the figure is routing luck and moves more
than 0.1 ns between runs. There is nothing left to optimize in the RTL;
what remains is placement.

### And in context, it links

The PR link landed while this entry was being written: **255.23 MHz,
`met_timing=True`, `pr_verified=True`**, +0.082 ns against the 250 MHz
target, 8,605 LUT / 2,927 FF including the `rp_wrapper` harness, a
3.6 MB partial bitstream, 2,844 s to build.

The interesting part is that it is *better* than the OOC worry implied.
Confining placement to the `pyro_rp` pblock on SLR2 concentrates the 50
URAMs and 108 BRAM36s instead of letting them scatter across three SLRs,
and for a design that is 91–98% route delay that constraint helps rather
than hurts. The RM↔static boundary paths cost little enough to stay
inside the margin. The lesson to carry: for route-bound designs an
unconstrained OOC run is not a conservative lower bound on the
in-context result — it is just a different, noisier experiment.

So the full-corpus overlay engine, with the uncapped 39,647-state trie
resident in URAM, fits the region and runs at rate. A5's central claim —
that a table write is a context switch four orders of magnitude cheaper
than partial reconfiguration, which is what moves `s/P` off the frontier
— now has a circuit behind it that meets timing.

**Correction, found during bring-up:** that link measured a **4096-state**
engine, not the full corpus. `pyro_circuit_overlay_top.v` hardcoded
`MAX_STATES(4096)` from when the bitmap was still headed for BRAM; the
URAM move removed the reason and the override outlived it. So the OOC
numbers are full-corpus but the in-context one was not, and "the full
corpus fits the region and runs at rate" was not supported. Caught by a
`TABLE_CAPS` read-back of 4096. A parameter that encodes a constraint
should die with the constraint.

**And the engine was unreachable.** A5 §3's wire protocol existed only on
paper. `rp_wrapper` could drive seven CSR addresses, none in
`0x0068`–`0x0080`; the host stopped at kind `0x07`. `TBL_CTRL[LOAD]`
could never be asserted, so no table could ever be written. The engine
had been built, timed, and verified against the model — and none of that
could reveal there was no road to it. I had spent a day making the
destination faster without checking that anything could get there.

Four defects appeared within an hour of driving real frames, all the same
species: an **interface** assumption that held for the generated engines
and quietly did not hold for this one.

- `ST_TBL_OPEN` drove `eng_csr_addr` twice, so the `TBL_CTRL` write
  retargeted to the read-only `TABLE_ACTIVE`.
- This engine registers `csr_rdata` (2 cycles); `ST_PERF` assumes the
  generated engine's combinational read (1 cycle).
- The wrapper drives RESET → OUT_CAP → START → wait BUSY → feed → wait
  DONE. This engine is a streaming scanner and never asserted BUSY, so
  the wrapper parked forever. Sharing a port list is not sharing a
  protocol — which is precisely what the alias comment had asserted.
- `_engine_backpressure`'s docstring *states* that the engine must hold
  the in-flight beat in a skid. This engine had none and is busy ~8
  cycles per byte, so it dropped nearly every byte: the table loaded,
  committed and attested perfectly, and matched **nothing**. A 1-deep
  skid is not enough — the master can be one beat past the ready it saw —
  so the queue is depth 2 with `in_ready` deasserting at depth 1.

The last one turns `in_ready` into a credit rather than "taking it now",
which the engine testbench then violated by holding valid until ready
(enqueueing each byte two or three times). Both sides now say so in
writing.

Worth keeping: every one of these was invisible to the engine
differential, because the engine was never what was broken. And two of
them were documented hazards I had read and not applied.

`tests/hw/overlay_table_diff.py` now drives the whole wire protocol under
xsim and passes — identity end to end, epoch in `MATCH_REPLY`, matches
equal to the model, and an out-of-order chunk refused with the active
table intact. `tb_pyro_rp_wd.v` adds a watchdog, because the shared
beat-player spins forever on a stalled FSM and a hang looked exactly like
a slow run until it cost a 25-minute timeout.

**Open:** the corrected full-corpus link, then on-hardware bring-up.

916 unit tests green; wide RTL differential passes (5 subjects, 85
matches, CRC 0x9c8f7ce9).

## 2026-07-30 (cont.) — Overlay engine on silicon; the road, and a defect it found

The engine is resident on the U250 and matching. `TABLE_CAPS` reads
40960, so this is genuinely the full-corpus build; a 2,564 B table loads
and commits in **12.3 ms**; identity agrees end to end; nominations equal
the model exactly. In context: **251.32 MHz**, `pr_verified`, 11,332 LUT
/ 4,279 FF, 5.1 MB partial.

Getting there took building the road. A5 §3's transport was specified and
implemented on neither side — `rp_wrapper` could drive seven CSR
addresses and none was in `0x0068`–`0x0080`, so `TBL_CTRL[LOAD]` was
unreachable and no table could ever be written. I had spent a day making
the destination faster without checking that anything could reach it.

Then bring-up found the defect that mattered. The engine committed
whatever it received and reported *that* CRC — self-consistent, and
therefore not a check. On the card, a transfer corrupted in flight
committed cleanly, replaced the working table, and advanced the epoch:
exactly what A5 §5 forbids. `A_TBL_EXPECT` now carries the host's
declared CRC and the commit gate compares against it; refusal leaves
`active_id`, `epoch` and `active_valid` untouched.

Two process notes worth more than the result.

**My first version of that test was a tautology.** It corrupted the image
before handing it to `load_table`, so the host computed its expected CRC
from the corrupted bytes and both ends agreed. It reported a violation
that wasn't one, and it would equally have reported *no* violation if the
gate had existed. The corruption has to happen in transit for the
question to mean anything.

**Four of the five bugs were interface assumptions, not logic.** The
double-driven `eng_csr_addr`; a registered `csr_rdata` where `ST_PERF`
assumes combinational; a streaming engine that never asserted BUSY under
a wrapper that waits for it; and a missing skid buffer whose necessity
was written in the docstring of the very rewrite that required it. None
was visible to the engine differential, because the engine was never what
was broken. Two were documented hazards I had read and not applied.

The missing skid is the one to remember: the table loaded, committed and
attested **perfectly**, and matched nothing, because the wrapper commits
a beat before it can observe `in_ready` and the engine dropped nearly
every byte. Everything that reports success reported success.

## 2026-07-30 (cont.) — How the overlay gets used, and where the programs come from

The owner asked the right orienting question — *where do the programs
come from?* — and the answer is worth recording because it is the whole
architecture in one sentence: **a "program" here is a table, not a
bitstream.** It is data written to a resident engine, not gates
reconfigured. Nobody writes an FPGA program by hand, and after the
engine ships nobody runs Vivado either.

The chain, every stage already in the repo:

1. **Source** — `third_party/snort3-community-rules/snort3-community.rules`.
   Ordinary Snort text rules, maintained by the community, refreshed
   weekly. This is the upstream "program source."
2. **Triage** (`pyro/snort/triage.py`) — parse; decide expressibility.
3. **Lowering** (`pyro/snort/lowering.py`) — collapse each content chain
   to one literal anchor + admission conditions. Deliberately
   over-approximate: SR3 makes a miss unacceptable, a spurious
   nomination merely wasteful.
4. **Packing** (`pyro/snort/groups.py`) — rules grouped by header
   predicate into slot-budget-sized sets; the 21 groups.
5. **Compile** (`pyro/overlay/table.py`) — a group's anchors become an
   Aho-Corasick automaton serialized to an image whose CRC-32C *is* its
   `TABLE_ID`. The only FPGA-specific step, and it is a compiler pass:
   no Vivado, no 51-minute link.
6. **Load** (`pyro.device.load_table`) — stream the image to the card.
   Measured: 12.3 ms for a small group end-to-end; 0.66 ms of wire time
   for the full 1.64 MB corpus table.

At runtime the engine stays resident and *which rules it holds* is the
thing that changes. The value-aware scheduler watches the observed port
mix, scores each group by how many of its rules can actually fire on
current traffic, and writes a new table when a challenger clears the 2×
bar. The FPGA nominates; Snort re-verifies; every match carries the
EPOCH so nominations in flight across a swap attribute to the exact
table that produced them — all three properties now verified on the
card.

Why the millisecond matters: the frontier constant `s/P ≈ 0.3` says
JTAG's 13.6 s confines scheduling to phases over ~45 s, which almost no
real traffic honours. A ~1 ms table write needs phases over ~3 ms.
Four orders of magnitude, and it moves functionality scheduling from
"minutes-scale workloads only" into per-burst territory. The mechanism
now exists in fabric rather than in arithmetic.

And the standing caveat, so this entry cannot be misread: the Snort
ruleset is the *workload*. The subject is whether OS scheduler
techniques transfer to run-time FPGA functionality swapping; Snort earns
its place by being large, naturally partitioned, and demand-driven. The
limits that ship today: one table resident at a time (banking is what
would let a swap overlap serving), ~8 cycles/byte scan rate, and
anchor-only matching (costs precision, never completeness — 6 extra
nominations on one group, zero misses anywhere, measured).
