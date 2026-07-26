# AMD support case draft — QDMA (EQDMA5.0 Soft IP) multi-queue H2C ST packet corruption/loss

Status: ready to file (needs the filer's account/case metadata). All data
below was measured on silicon 2026-07-25/26; full lab log in `docs/notebook.md`.

## Summary

With **two or more H2C ST queues** active on a single PF, the QDMA soft IP
intermittently corrupts/errs H2C stream packets (observed ~1/1500 packets at
4 queues) and intermittently freezes the C2H completion path for ≥10 s. With
**one queue the design is loss-free in every configuration we can produce**,
including worst-case descriptor-fetch settings (`GLBL_DSC_CFG.MAXFETCH=0`).
The host `write()` on the char-dev returns success (H2C CIDX writeback
advances), so the loss is silent from the host's perspective.

## Environment

| Item | Value |
|---|---|
| Card | Alveo U250 (`xcu250-figd2104-2L-e`), PCIe Gen3 x16 (confirmed at 8 GT/s x16) |
| Shell | OpenNIC shell (open-nic-shell), 1 PF (`10ee:903f`), 1 CMAC, MAX_PKT_LEN=9600 |
| DMA IP | QDMA Soft IP, driver reports `Device Type: Soft IP`, `IP Type: EQDMA5.0 Soft IP` |
| Vivado | 2025.2 (`/usr/local/cad/2025.2/Vivado`) |
| Driver | dma_ip_drivers QDMA PF Linux driver `v2024.1.0.0` |
| Host | Linux 6.8.0-136-generic (Ubuntu 24.04), Intel DMAR IOMMU |
| Queue config | 4 ST queue pairs (bi), desc size 1536, ring 2048, cmptsz 0, trigmode every |
| Modes tested | direct interrupt (02:0:2), auto (02:0:0), poll (02:0:1) |

## Symptom detail

1. Workload: 4 writer threads, one per H2C ST queue fd, streaming 9,556 B
   (also reproduced with 1,504 B single-descriptor) packets; single C2H
   queue 0 carries responses from user logic (echo-style request/reply).
2. At ≥2 H2C queues, individual request packets vanish between a successful
   `write()` (writeback-confirmed) and the user logic behind the
   `qdma_subsystem` AXIS interface. Rate ~1/1500 packets at 4 queues.
   At 1 queue: 0 losses in >20,000-packet runs, repeated.
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
   hundreds of retransmissions with throughput collapsing 10–30×. The decay
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

| Hypothesis | Experiment | Result |
|---|---|---|
| Driver IRQ re-arm race | poll mode, zero MSI-X vectors allocated | still lossy |
| Multi-descriptor packet interleave | single-descriptor (1504 B) packets | still lossy |
| IOMMU lazy-invalidation race | strict (`DMA`) domain | still lossy |
| IOMMU translation entirely | `identity` domain (pt-equivalent) | still lossy |
| Writeback accumulation | `GLBL_DSC_CFG.WB_ACC_INT` 5→0 | no change |
| Descriptor prefetch masking | `GLBL_DSC_CFG.MAXFETCH` 2→0 | ~100× WORSE (latency-sensitivity dose-response) |
| Thermal / power | sysmon 58.8 °C, VCCINT 0.844 V | nominal |
| PCIe link | 8 GT/s x16, no AER | nominal |
| Host SW regression | byte-identical driver + app binaries across good/bad days | reproduces |
| JTAG/hw_server interference | hw_server killed | still lossy |

## Where the packets go

`qdma_subsystem_h2c` (OpenNIC) passes `m_axis` data through unchanged and
ignores `s_axis_qdma_h2c_tuser_err` (drop-on-error is an unimplemented TODO
upstream), so err-flagged packets reach user logic with corrupt payload and
fail framing there. We are adding packet/err counters at that boundary to
quantify `tuser_err` assertions directly; counts can be supplied once the
instrumented shell is deployed.

## Questions for AMD

1. Are there known EQDMA5.0 Soft IP (Vivado 2025.2) errata for multi-queue
   H2C ST descriptor fetch/engine arbitration, `tuser_err` assertion rates,
   or internal CSR (`TCP_CSR_TIMEOUT` @ 0x12EC) timeouts under load?
2. Is cumulative state degradation (worsening with traffic, cleared only by
   reconfiguration) a known signature of any errata?
3. Recommended `GLBL_DSC_CFG` / perf-opt register settings for a 4-queue
   H2C ST workload on the soft IP, beyond the driver defaults?
4. Is there a known interaction between the indirect-context access path
   (`dma-ctl q dump` during traffic) and datapath integrity?

## Attachments to include when filing

- `docs/notebook.md` 2026-07-25 entries (evidence matrix + timeline)
- dmesg extracts: `GLBL_TRQ_ERR` decode, `qdma_request_wait_for_cmpl`
  timeouts, DMAR faults
- Shell build: OpenNIC + 1 DFX partition; QDMA IP tcl:
  `third_party/open-nic-shell/src/qdma_subsystem/vivado_ip/qdma_no_sriov_au250.tcl`
