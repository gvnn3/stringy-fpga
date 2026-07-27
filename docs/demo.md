# Demonstrating the system (state as of 2026-07-27)

What this shows: Python `re`-compatible regex matching transparently
accelerated by a resident pattern-matching circuit on an Alveo U250, over
two host bindings — a raw-Ethernet control path (`onic` netdev) and a
QDMA ST char-dev data plane. Reproducible zero-loss throughput is
**~2.3 GiB/s** (1 queue); multi-queue runs have reached 5.14 GiB/s once
but are gated by an open EQDMA IP loss issue (§0) and currently land at
2.6–3.1 GiB/s on a fresh boot, so the R1 5 GiB/s target is not yet
certifiable.

Everything below runs from the repo root on `nf-server06`. The two
privileged scripts (`pyro_dataplane_swap.sh`, `pyro_wedge_recover.sh`) are
NOPASSWD-sudo for this user; nothing else needs root.

## 0. One-time state notes (2026-07-25)

* QSPI holds the **counter-fixed instrumented jumbo shell** (commit
  `46186bf`, `build_timestamp=0x07260427`), activated by the 2026-07-26
  cold cycle and verified on silicon: `lspci -d 10ee: -nn` shows
  `10ee:903f` at `02:00.0` and `h2cstats` reads live counters.
* `.superpowers/pr-builds/pattern_becf73e88b6f1c561308914848c69ab0_x4_partial.bit`
  matches this static (fmax 260.8 MHz) and is the bit to `pyro_hw.py load`.
* Known open issue: the EQDMA5.0 soft IP silently corrupts ~1/1500 H2C
  packets when **2+ TX queues** are active (single-queue is loss-free; see
  `docs/notebook.md` and `docs/amd-support-case-eqdma-h2c-loss.md`). The
  native benchmark carries a retransmit watchdog, so runs complete with
  every frame accounted; `RETX=n` in its output is that mechanism working,
  not a failure. Loss rate grows with accumulated multi-queue traffic —
  a cold boot re-baselines it.

## 1. Bring-up after a (cold) boot

```sh
# 1. Card visible?
lspci -d 10ee: -nn                     # expect 10ee:903f at 02:00.0

# 2. Load the netdev driver + reset the user box (idempotent, safe anytime):
sudo scripts/pyro_wedge_recover.sh     # user reset + QDMA soft reset + onic

# 3. Load the x4 frame-parallel child into the dynamic region over JTAG
#    (~40 s; includes automatic in-band recovery):
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py load \
    .superpowers/pr-builds/pattern_becf73e88b6f1c561308914848c69ab0_x4_partial.bit

# 4. Probe — the go/no-go check:
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py probe
# -> device_usable=true — static_shell_id=0x02020000, ...
```

## 2. Control-path demo (raw Ethernet, `onic` netdev)

The resident child answers R78 frames on `ens2`. The loaded pattern is
`abc[a-f]{2}` (datapath 8 B/cycle, 4 cores):

```sh
# One MATCH round-trip — returns candidate windows the host re-verifies:
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py match "xyzabcafxyz"
# -> MATCH_REPLY count=1 ... entry[0]: start=0 end=8 pattern_id=0

# On-chip performance counters for the most recent scan (R78.11):
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py perf
# -> CYCLES=... BYTES=...  util=7.98 B/cyc  ~2 GB/s on-chip per core-column
```

## 3. Transparent Python API (the actual product surface)

`pyro.re` is a drop-in stdlib-`re` superset: short one-shot calls route to
CPython (R3/R29, ≤2 µs added overhead); large corpora with pattern reuse
route to the resident circuit (R51):

```python
import pyro.re as re                       # in .venv-pyro

pat = re.compile(r"abc[a-f]{2}")           # HW-eligible, resident in slot 1
corpus = ("x" * 9500 + "abcaf") * 8000     # ≥ S_min = 64 KiB, reused ≥ 32×
for _ in range(40):
    m = pat.search(corpus)                 # routed per §8; byte-identical
print(m.span(), re.stats())                # re.stats() shows the routing
```

## 4. Data-plane swap and the throughput benchmark

The single PF swaps between `onic` (control) and `qdma-pf` char-devs
(data). Swapping to data mode creates 4 ST queue pairs:

```sh
sudo scripts/pyro_dataplane_swap.sh data      # -> /dev/qdma02000-ST-0..3
sudo scripts/pyro_dataplane_swap.sh status    # show current binding
```

The pipelined MATCH benchmark (native C credit loop, retransmit watchdog,
window sweep W=1..64):

```sh
PYRO_QDMA_CHARDEV=/dev/qdma02000-ST-0 PYRO_BENCH_JUMBO=1 \
    .venv-pyro/bin/python3 scripts/pyro_pipeline_bench.py
```

Reference numbers, x4 child, jumbo. **Demo with 1 queue** — it is
loss-free and reproduces run after run. 4-queue numbers depend on how
much multi-queue traffic the boot has already seen (§0) and degrade
within a session; the one-off 5.14 GiB/s peak of 2026-07-25 has not
reproduced since (best 2026-07-26: 2.66 GiB/s) and should not be quoted
as expected performance.

| Config (measured 2026-07-26, fresh cold boot) | Throughput | Notes |
|--------|-----------|-------|
| 1 queue, W=1 | ~0.40 GiB/s | latency-bound |
| 1 queue, W=8 | **~2.2–2.3 GiB/s** | zero-loss, the reliable demo number |
| 4 queues, W=1 | ~0.31 GiB/s | |
| 4 queues, W=16–32 | ~2.5–2.7 GiB/s | best 4q on a fresh boot; expect RETX≥1 |
| 4 queues, after heavy mq traffic | 0.002–0.9 GiB/s, erratic | degraded state, §0; windows can fail outright |

Env knobs:

* `PYRO_BENCH_JUMBO=1` — 9,556 B frames (jumbo shell only; default 1,504 B)
* `PYRO_BENCH_QUEUES=n` — cap TX fan-out (use `1` for the loss-free baseline)
* `PYRO_BENCH_TOTAL_MB=n` — corpus per window (default 4)
* `RETX=n` in output — frames recovered by the watchdog (§0 known issue)

The B2-shape acceptance measurement (32 MiB minimum, W=64, every frame
accounted, metrics emitted) is AC-3-3:

```sh
PYRO_DEVICE_IFACE=ens2 PYRO_QDMA_CHARDEV=/dev/qdma02000-ST-0 \
    .venv-pyro/bin/pytest tests/acceptance/test_ac3_3_benchmark.py -k hardware -rA
```

## 5. H2C loss instrumentation (new in the flashed shell)

After the cold boot, the shell counts every H2C packet and every packet the
QDMA IP flagged `tuser_err` at the ingest boundary (BAR2 `0x5000`/`0x5110`):

```sh
sudo scripts/pyro_dataplane_swap.sh h2cstats     # before a bench run
# ... run the benchmark ...
sudo scripts/pyro_dataplane_swap.sh h2cstats     # after
# pkts delta should equal frames written (+retx); err delta counts the
# IP-corrupted packets — the direct measurement for the AMD case.
```

On the pre-instrumentation shell this prints `H2C_STATS unavailable`.

**2026-07-26 status: verified on silicon after the cold cycle.** The
counters account for traffic exactly (1q control run: delta = frames+1
probe frame, to the packet). Headline measurement: across ~28k delivered
packets including heavily degraded multi-queue periods, the err counter
stayed **0** — the QDMA IP never asserts `tuser_err`; lost packets simply
never emerge on the AXIS interface. The err delta is therefore expected to
read 0; the *pkts shortfall* vs frames written (+retx, +1 probe) is the
loss measurement. Details in `docs/notebook.md` and the AMD case doc.

## 6. Recovery cheat-sheet

| Symptom | Fix |
|---|---|
| Probe times out / DMA reads all time out (`tm 10000` in dmesg) | `sudo scripts/pyro_wedge_recover.sh` then re-probe |
| After JTAG partial load, child unreachable | (automatic — `load` runs recovery; manual: same script) |
| Bench numbers degrade run-over-run | reset + fresh `data` swap; if still degraded, cold boot (EQDMA state decay, §0) |
| Back to control path | `sudo scripts/pyro_dataplane_swap.sh control` (pulses resets first — safe against the 2026-07-25 panic) |

## 7. Optional: IOMMU domain experiment mode

For the EQDMA investigation the swap script can switch the card's IOMMU
domain at runtime (drivers are unbound and must be re-swapped afterward):

```sh
sudo scripts/pyro_dataplane_swap.sh iommu PYRO_IOMMU_TYPE=DMA       # strict
sudo scripts/pyro_dataplane_swap.sh iommu PYRO_IOMMU_TYPE=identity  # pt-equiv (experiments only)
sudo scripts/pyro_dataplane_swap.sh iommu PYRO_IOMMU_TYPE=DMA-FQ    # default
sudo scripts/pyro_dataplane_swap.sh data                            # rebind + queues
```
