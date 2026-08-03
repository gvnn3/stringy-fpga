# AMD support case draft — QDMA (EQDMA5.0 Soft IP) multi-queue H2C ST packet
corruption/loss

Status: ready to file (needs the filer's account/case metadata). All data
below was measured on silicon 2026-07-25/26; full lab log in `docs/notebook.md`.

## Summary

With **two or more H2C ST queues** active on a single PF, the QDMA soft IP
intermittently **drops H2C stream packets without any error indication**:
the packets never appear on the H2C AXIS master interface at all, and
`tuser_err` is never asserted (0 assertions across ~28,000 delivered
packets, measured with counters at the IP↔user-logic boundary — see "Where
the packets go"). Loss rate is ~1/600–1/1500 on a fresh configuration,
degrading with accumulated multi-queue traffic to >13% of packets. The IP
also intermittently freezes the C2H completion path for ≥10 s. With
**one queue the design is loss-free in every configuration we can produce**
(boundary counters exact to the packet), including worst-case
descriptor-fetch settings (`GLBL_DSC_CFG.MAXFETCH=0`). The host `write()`
on the char-dev returns success (H2C CIDX writeback advances), so the loss
is silent from both ends: writeback says sent, the fabric never sees it.

## Environment

- **Card**
  - Value: Alveo U250 (`xcu250-figd2104-2L-e`), PCIe Gen3 x16 (confirmed at 8
    GT/s x16)
- **Shell**
  - Value: OpenNIC shell (open-nic-shell), 1 PF (`10ee:903f`), 1 CMAC,
    MAX_PKT_LEN=9600
- **DMA IP**
  - Value: QDMA Soft IP, driver reports `Device Type: Soft IP`, `IP Type:
    EQDMA5.0 Soft IP`
- **Vivado**
  - Value: 2025.2 (`/usr/local/cad/2025.2/Vivado`)
- **Driver**
  - Value: dma_ip_drivers QDMA PF Linux driver `v2024.1.0.0`
- **Host**
  - Value: Linux 6.8.0-136-generic (Ubuntu 24.04), Intel DMAR IOMMU
- **Queue config**
  - Value: 4 ST queue pairs (bi), desc size 1536, ring 2048, cmptsz 0,
    trigmode every
- **Modes tested**
  - Value: direct interrupt (02:0:2), auto (02:0:0), poll (02:0:1)

## Symptom detail

1. Workload: 4 writer threads, one per H2C ST queue fd, streaming 9,556 B
   (also reproduced with 1,504 B single-descriptor) packets; single C2H
   queue 0 carries responses from user logic (echo-style request/reply).
2. At ≥2 H2C queues, individual request packets vanish between a successful
   `write()` (writeback-confirmed) and the H2C AXIS master interface of the
   IP — boundary packet counters show the lost packets are never delivered
   to user logic, with or without `tuser_err` (see "Where the packets go").
   Rate ~1/600–1/1500 packets at 4 queues on a fresh configuration.
   At 1 queue: 0 losses in >20,000-packet runs, repeated; boundary
   counters account for every packet exactly.
3. Loss also arrives as **episodes**: ~0.5–1 s windows in which every packet
   on one queue is lost, and separately the C2H side stops delivering
   completions for ≥10 s (driver `qdma_request_wait_for_cmpl ... tm 10000`
   timeouts with `cidx 0` pinned).
4. Severity increases monotonically with accumulated **multi-queue** traffic
   across a power-on session and is NOT cleared by: QDMA soft reset
   (`soft_reset_n`), driver reload, queue teardown/re-add, or user-logic
   reset. A full reconfiguration (cold boot from QSPI) restores the initial
   low rate only briefly: on a fresh cold boot (2026-07-26) the first
   4-queue run already lost ~1/340 packets, and after roughly 25k further
   multi-queue packets (~250 MB) individual measurement windows degraded to
   hundreds of retransmissions with throughput collapsing 10–30×. On a
   second cold boot (2026-07-26 PM, instrumented shell) the same pattern
   reproduced with exact packet accounting: first 4-queue run lost 5/3072
   (~1/614); after ~28k further multi-queue packets, 13.2% of sent packets
   (3,727 of 28,208) never reached the AXIS boundary, and in the worst
   measurement window the application-level retransmit watchdog itself was
   exhausted (525 frames permanently lost, throughput 1.6 MiB/s). The decay
   tracks accumulated multi-queue traffic, not wall-clock uptime. Throughout
   — interleaved between degraded 4-queue runs on the same boot — the
   1-queue path stayed at 0 losses over 24k+ packets at full throughput.
5. During the worst episodes the driver has latched
   `GLBL_TRQ_ERR_STS.TCP_CSR_TIMEOUT` (`GLBL_TRQ_ERR_LOG_ADDRESS=0x12EC`)
   — a timeout on the IP's own internal CSR path — and
   `descq_cmpl_err_check ... ERR cmpl entry` completion errors.
6. DMAR (IOMMU) fault log shows the device performing DMA to unmapped
   IOVAs after driver-side timeouts (reads, fault reason 0x06; one write,
   fault reason 0x05) — consistent with the engine retiring stale
   descriptors late.
7. Issuing indirect-context reads (`dma-ctl q dump`) **during** traffic
   sharply increases the loss rate — CSR/context-engine contention with the
   datapath is implicated.

## Ruled out by experiment

- **Driver IRQ re-arm race**
  - Experiment: poll mode, zero MSI-X vectors allocated
  - Result: still lossy
- **Multi-descriptor packet interleave**
  - Experiment: single-descriptor (1504 B) packets
  - Result: still lossy
- **IOMMU lazy-invalidation race**
  - Experiment: strict (`DMA`) domain
  - Result: still lossy
- **IOMMU translation entirely**
  - Experiment: `identity` domain (pt-equivalent)
  - Result: still lossy
- **Writeback accumulation**
  - Experiment: `GLBL_DSC_CFG.WB_ACC_INT` 5→0
  - Result: no change
- **Descriptor prefetch masking**
  - Experiment: `GLBL_DSC_CFG.MAXFETCH` 2→0
  - Result: ~100× WORSE (latency-sensitivity dose-response)
- **Thermal / power**
  - Experiment: sysmon 58.8 °C, VCCINT 0.844 V
  - Result: nominal
- **PCIe link**
  - Experiment: 8 GT/s x16, no AER
  - Result: nominal
- **Host SW regression**
  - Experiment: byte-identical driver + app binaries across good/bad days
  - Result: reproduces
- **JTAG/hw_server interference**
  - Experiment: hw_server killed
  - Result: still lossy

## Where the packets go — measured at the IP boundary (2026-07-26)

We instrumented the shell with two counters directly on the QDMA IP's H2C
AXIS master interface (the `qdma_subsystem_h2c` ingest boundary): total
packets delivered, and packets delivered with `s_axis_qdma_h2c_tuser_err`
asserted. Measured on silicon, fresh cold boot, per-packet accounting
(the single-queue control run matches sent-count exactly, validating the
counters):

- **4-queue, first traffic after cold boot**
  - packets written (incl. retx): 3,072
  - delivered at boundary: 3,067
  - delivered with `tuser_err`: **0**
- **1-queue control**
  - packets written (incl. retx): 3,067
  - delivered at boundary: 3,067 (exact)
  - delivered with `tuser_err`: **0**
- **4-queue, degraded (after ~28k mq packets)**
  - packets written (incl. retx): 28,208
  - delivered at boundary: 24,481
  - delivered with `tuser_err`: **0**

Two conclusions:

1. **`tuser_err` is never asserted** — not once in ~28,000 delivered
   packets, including heavily degraded periods. This is not a
   deliver-with-error-flag failure mode.
2. **The lost packets never appear on the AXIS interface at all.** In the
   first run the shortfall (5) equals exactly the set of retransmitted
   originals; in the degraded run 3,727 packets (13.2%) vanished. H2C CIDX
   writeback advances for these packets (host `write()` succeeds), so the
   IP retires descriptors for data it never delivers.

The loss is therefore entirely internal to the EQDMA5.0 soft IP, between
descriptor fetch/writeback and the H2C AXIS master — silent in both
directions.

## Questions for AMD

1. Are there known EQDMA5.0 Soft IP (Vivado 2025.2) errata for multi-queue
   H2C ST descriptor fetch/engine arbitration under which the engine
   retires a descriptor (CIDX writeback advances) but never emits the
   packet on the H2C AXIS master — with no `tuser_err` and no logged
   error — or for internal CSR (`TCP_CSR_TIMEOUT` @ 0x12EC) timeouts
   under load?
2. Is cumulative state degradation (worsening with traffic, cleared only by
   reconfiguration) a known signature of any errata?
3. Recommended `GLBL_DSC_CFG` / perf-opt register settings for a 4-queue
   H2C ST workload on the soft IP, beyond the driver defaults?
4. Is there a known interaction between the indirect-context access path
   (`dma-ctl q dump` during traffic) and datapath integrity?

## Attachments to include when filing

- `docs/notebook.md` 2026-07-25/26 entries (evidence matrix + timeline +
  boundary-counter accounting tables)
- dmesg extracts: `GLBL_TRQ_ERR` decode, `qdma_request_wait_for_cmpl`
  timeouts, DMAR faults
- Shell build: OpenNIC + 1 DFX partition; QDMA IP tcl:
  `third_party/open-nic-
  shell/src/qdma_subsystem/vivado_ip/qdma_no_sriov_au250.tcl`
