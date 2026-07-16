# P2 Data-Plane Enablement — Design (DRAFT)

- **Spec ID:** `p2-dataplane`
- **Version:** 0.1.0 (DRAFT — design track, not yet normative)
- **Parent spec:** `python-regex-offload` v2.5.1 (P2, F5, R1, R76 lineage, R78, R85a)
- **Date:** 2026-07-16
- **Status:** measurement-driven plan; supersedes the assumption that P2 is
  primarily a *transport* (QDMA char-dev) problem

## 0. Summary — the measurement that reframes P2

The parent spec's P2 frames the performance path as "the QDMA char-dev
binding" (F5: char-devs absent). Hardware measurements on the first resident
pattern child (`abc[a-f]{2}`, U250, shell `0x07140219`) show the transport is
**not** the binding constraint:

| Window (frames in flight) | Throughput | µs/frame | loss |
|---|---|---|---|
| 1 (sequential, AC-3-3 shape) | 52.6 MiB/s | 26.7 | 0 |
| 2 | 116.6 MiB/s | 12.1 | 0 |
| 4…64 (plateau) | ~119–120 MiB/s | 11.8 | 0 |

(`scripts/pyro_pipeline_bench.py`, 4 MiB per window size, 1474 B corpus/frame.)

The plateau at **W=2** with zero loss means plain `AF_PACKET` already
saturates the child: while the child scans and replies (`s_axis_tready` low
outside `ST_RX` — the harness serializes), the next frame is already waiting
in the shell FIFO. Adding a faster host transport moves nothing until the
child itself is faster.

**Bottleneck decomposition** (11.8 µs ≈ 2950 cycles @ 250 MHz per 1474 B
frame): scan at the measured 0.88 B/cyc ≈ 1675 cycles; the remaining ~1275
cycles are frame RX (24 beats), parse/classify, reply composition/TX, and
turnaround — all serialized with the scan by the harness FSM.

## 1. Constraint map — what lives where

| Constraint | Location | Change vehicle |
|---|---|---|
| Harness FSM serialization (one frame at a time) | RM (child) | **partial bitstream only** (R79) |
| Engine scan width (1 B/cyc today) | RM (child) | **partial bitstream only** (R79) |
| `MAX_PKT_LEN = 1518` | static shell | shell rebuild + QSPI reflash + new locked DCP |
| R78 `length` bound (1486 B payload, u16) | wire format | spec rev (R78 or a new R76-style data-plane format) |
| `AF_PACKET` per-frame syscalls | host | `PACKET_MMAP` rings or QDMA char-devs (`dma_ip_drivers`, repo reachable) |
| `onic` vs `qdma_pf` driver (single PF) | host | driver swap — mutually exclusive with the netdev control transport |

Key consequence: **the two changes that matter first are partial-only** — they
ride the now-working R85a load path (JTAG + in-band recovery, ~16 s/swap) and
need no reflash and no driver work.

## 2. Revised P2 ladder

- **P2a — pipelined host transport (software only, no RTL).** Put a W≥4
  request window into the host MATCH path (as `pyro_pipeline_bench.py` does).
  Buys 2.3× today (52.6 → ~120 MiB/s), zero hardware risk. Also the transport
  shape P2b needs anyway.
- **P2b — harness v3 + wide engine datapath (partial-only RTL).** Two RM
  changes: (i) overlap RX with scan — stream corpus beats into the engine as
  they arrive instead of buffer-then-scan; (ii) multi-byte-per-cycle matcher
  datapath (8 B/cyc target). Frame cost then ≈ max(RX 24 beats, scan ~185 cyc)
  + reply ≈ **~250 cycles ≈ 1 µs/frame ≈ 1.4 GB/s** — **above the R1 floor
  (1 GiB/s) with the existing static shell, 1518 B frames, and `AF_PACKET`**.
  AC-2-5/AC-3-3's R1 clause arms itself when this lands (v2.5.1 disposition).
- **P2c — jumbo frames + kernel-bypass transport (static + host).** Only after
  P2b: `MAX_PKT_LEN` 9600 shell rebuild (amortizes per-frame overhead ~6×),
  `PACKET_MMAP` or QDMA char-devs once ~1 M frames/s makes per-frame syscalls
  binding, and an R78 revision (or R76-style data-plane format) for >1486 B
  payloads. This is where the original P2 char-dev work re-enters — as the
  *last* rung, in service of the 5 GiB/s R1 target, not the 1 GiB/s floor.

## 3. Risks / open questions

- **Timing at 250 MHz for an 8 B/cyc matcher.** Static `axis_aclk_0` closes at
  +30 ps; the RM's own paths have independent margin (last RM: WNS +0.023 ns
  at 1 B/cyc). Wide comparator trees may need pipelining; LUT growth is
  bounded by the SLR2 pblock (pattern circuits today are 197–410 LUTs — room).
- **R45a counters under overlap.** CYCLES semantics ("most recent scan") need
  a harness-v3 definition when RX and scan overlap (R45a rev).
- **Reply-path share.** At 1 µs/frame the C2H reply stream is ~64 B/frame —
  negligible; MATCH windows with many candidates (OVF) could serialize — the
  R19 nomination discipline (small out_cap) already bounds this.
- **Driver swap tension (P2c).** `qdma_pf` char-devs and the `onic` netdev
  cannot bind the same single PF simultaneously; if char-devs are adopted, the
  R78 control frames ride ST queues too (they are AXIS payloads either way) —
  a spec change to the transport binding (F3/R68/R83 probe wording).

## 4. Immediate next actions

1. P2a: window the host MATCH path (host runtime seam; the same credit loop
   as the bench, honest loss accounting).
2. P2b engine: extend the circuit generator to an N-byte/cycle datapath
   (parameterized; start N=8) — xsim differential vs model first (R7), then a
   real partial through the R73a-gated PR flow, loaded via R85a.
3. P2b harness: v3 FSM with RX/scan overlap; keep the R78 parser unchanged on
   the wire (format is untouched until P2c).
4. Re-run `pyro_pipeline_bench.py` + AC-3-3 after each rung; the R1 clause
   flips from honest SKIP to PASS on its own when the floor is met.
