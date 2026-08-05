# OQ-2 wire-rate ingest feasibility spike (SNORT-PF §3 end-state)

Branch: `phase2-snort`.  Status: in progress, opened 2026-08-04.
Authorization: SNORT-PF §10, **OQ-2 — "FEASIBILITY SPIKE ONLY, after
S3"** (owner decision at adoption, 2026-07-27).  S3 is complete; the
fpga-vs-snort experiment (docs/studies/fpga-vs-snort.md §6) measured
the motivating fact: the overlay engine is ~1,000x faster than the
host-fed A5 transport that feeds it.

## 1. Question

Can this shell carry wire traffic into the overlay engine — i.e. can
the CMAC RX path feed `pyro_rp` — with (a) timing closed on a live
CMAC datapath (SF7/SF19 gate) and (b) the R80 RP boundary preserved,
so existing partials survive as re-links rather than redesigns?

This is a feasibility spike.  No line-rate commitment, no flash
without an explicit owner decision, no spec amendment implied
(adoption would be a MAJOR event across both specs per SNORT-PF §9).

## 2. Ground truth going in

- SF3/SF4: the CMAC datapath is tied off in `pyro_250mhz.sv` (adap_tx
  idle, adap_rx sunk); the only ingress is lockstep R78 over QDMA.
- SF7: permanent post-route setup violation in `cmac_usplus`
  (`txoutclk_out[0]`), −0.427 ns at drafting, accepted only because
  the datapath is dead.  **SF19: the 2026-07-26 rebuild closed the
  same group to −0.015 ns (waivable), still tied off.**  The spike's
  decisive measurement is this number with the datapath LIVE.
- R80: the `pyro_rp` boundary is frozen — clk/rstn + one 512-bit
  AXIS slave + one 512-bit AXIS master, tuser = {dst,src,size}.
- Adapter RX tags CMAC-0 frames `tuser_src = 16'h0040`
  (`packet_adapter_rx.sv:205`); QDMA H2C frames carry the PF bitmask
  (`16'h0001`).  Wire vs host is therefore distinguishable INSIDE the
  existing boundary — no new RP ports needed.
- No transceiver link exists on nf-server06; functional wire tests
  need CMAC near-end loopback (to be configured via the CMAC's
  AXI-Lite window; verified as part of the spike, not assumed).
- R82b: ANY static rebuild invalidates every cached partial (re-link
  against the new locked DCP required).  The production locked DCP in
  `hw/dfx/build/dcp/` must not be overwritten by spike builds.

## 3. Design — the wiretap shell (static v2, spike-only)

All changes are in the box_250mhz plugin; nothing in `cmac_subsystem`
or the RP boundary changes.

```
   tx frame generator ---------------------------> adap_tx (TX live;
   (self-paced, 64 B / 1024 cyc)                    with CMAC near-end
                                                    loopback its frames
            QDMA H2C ----+                          return on RX)
                         v
                 2:1 packet-atomic
                 AXIS arbiter  <----- adap_rx (CMAC RX,
                         |            no longer sunk)
                         v
                      pyro_rp  (boundary unchanged; wire frames
                         |      arrive tuser_src=0x0040, host frames
                         v      tuser_src=0x0001)
                      QDMA C2H  (unchanged)
```

- `WIRE_TAP` parameter on `pyro_250mhz` (default 0 = byte-identical
  tie-off behavior; the production shell build is unaffected).
- `pyro_axis_wire_arb.sv`: 2:1 packet-atomic round-robin arbiter,
  512-bit AXIS + 48-bit tuser, no store-and-forward; the grant
  freezes on presentation (AXIS stability) and releases on the
  accepted tlast beat.  Backpressure propagates to both ports; a
  stalled RP therefore backs up the adapter RX FIFO, whose drop
  counters are the (honest, counted) loss point — spike-acceptable,
  recorded not hidden.
- `pyro_wire_tx_gen.sv`: TX is driven by a self-paced generator (one
  64 B ethertype-0x88B5 frame with an incrementing sequence number
  every 1024 cycles), NOT by mirroring H2C.  Rationale: it makes the
  CMAC TX domain load-bearing for the timing verdict and, under
  near-end loopback, produces autonomous wire RX stimulus — while
  guaranteeing the wire side can never stall host control (a
  H2C-coupled mirror could deadlock the control path if the CMAC
  stopped draining; the R85a lesson says never build that).
- Build isolation: spike builds go to `hw/dfx/build-wiretap/` via a
  separate plugin dir (`hw/pyro_plugin_wiretap/`) that reuses the
  same RTL with `WIRE_TAP=1`; the production plugin, build dir, and
  locked DCP are untouched.

Deferred to after the timing verdict (partial-only changes, R79):
the `rp_wrapper` wire-scan path (frames with src 0x0040 bypass the
R78 codec, stream payload bytes through the engine, emit nominations
as MATCH_REPLY-shaped frames with a WIRE flag and the active epoch).

## 4. Gates

1. **G1 (sim):** arbiter passes packet-atomicity, fairness, and
   random-backpressure tests under xsim; `WIRE_TAP=1` box elaborates.
2. **G2 (timing — decisive):** full DFX static build of the wiretap
   shell closes: `pyro_rp` + box_250mhz clean, and the `cmac_usplus`
   WNS with a LIVE datapath is reported.  SF19's −0.015 ns waivable
   is the bar; material regression = spike answers "not closable on
   this shell" and stops.
3. **G3 (pr_verify):** the ID stub re-links against the wiretap
   locked DCP and pr_verify passes (R80 boundary really unchanged).
4. **G4 (owner):** flashing the wiretap shell is an explicit owner
   decision — it invalidates all cached partials (R82b) and carries
   the QSPI/live-PCIe hazard.  The spike stops at the evidence.

## 5. Cost accounting (per SNORT-PF §8 Risk 2)

Paid by this spike: one static P&R (~hours on this host), plugin RTL
+ tb, no flash.  Priced but NOT paid here: static reflash + partial
re-links (R82b), MAJOR version events on both specs, SF7 waiver
review, and the engine-rate problem itself (32–50 MB/s per engine vs
12.5 GB/s line rate — banking/replication is future work; the spike
only moves the feed bottleneck, deliberately).

## 6. Results

**Build 1 (2026-08-04, combinational arbiter).**  Full DFX build
completed; pr_verify PASS (G3 — the R80 boundary really is
unchanged: the ID stub re-linked against the wiretap locked DCP).
The headline G2 number: **the historic CMAC violation CLOSED with
the datapath live** — `txoutclk_out[0]` WNS **+0.045 ns** (vs
−0.427 ns at SF7 drafting, −0.015 ns waivable tied-off at SF19).
`pyro_rp`'s clock group closed at +1.918 ns.  What failed instead
was self-inflicted: `axis_aclk_0` WNS −0.150 ns / TNS −14.4 over
170 endpoints, worst paths (a) QDMA H2C slice FSM ->
`pyro_rp` ingress FSM at 7 LUT levels and (b) the tready fan-back
into the H2C slice CE nets — both the combinational arbiter
stretching an SLR-crossing boundary path that used to be a direct
register hop.  Verdict: the CMAC question is answered YES; the
arbiter needed register isolation.

**Fix.**  `pyro_axis_skid.sv` (2-deep, both directions register-
sourced, full-throughput) instantiated on all three arbiter faces.
G1 sim re-passes.

**Build 2 (2026-08-05, register-isolated arbiter) — TIMING MET.**
Overall static WNS **+0.020 ns**, zero failing endpoints:
`axis_aclk_0` +0.020 (the round-1 failures gone), `txoutclk_out[0]`
**+0.104** and `rxoutclk_out[0]` +0.831 with the CMAC datapath
live, `pyro_rp` group clean.  pr_verify PASS again (G3).  Artifacts
in `hw/dfx/build-wiretap/` (flash image, locked DCP, ID-stub
partial), reproducible via
`hw/dfx/dfx_build.sh --plugin hw/pyro_plugin_wiretap
--out hw/dfx/build-wiretap`.

**Spike verdict (G1-G3 complete).**  OQ-2's feasibility question is
answered YES on this shell: a wire-fed `pyro_rp` closes timing at
250 MHz with a live CMAC datapath, the R80 boundary survives
unchanged (existing partials re-link, not redesign), and the tap
plumbing is proven in sim.  What was priced as possibly-unclosable
(SF7) is measured closed with margin.  Remaining, per §4 G4 and
SNORT-PF §9: flashing is an explicit owner decision (invalidates
all cached partials, R82b; QSPI/live-PCIe hazard), and any adoption
beyond a spike is a MAJOR version event on both specs.  Also still
open before wire traffic means anything on silicon: the rp_wrapper
wire-scan path (partial-only), CMAC near-end loopback bring-up, and
the engine-rate gap (32-50 MB/s/engine vs line rate) that banking
would have to close.
