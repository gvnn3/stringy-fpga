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
open before wire traffic means anything on silicon: CMAC near-end
loopback bring-up, and the engine-rate gap (32-50 MB/s/engine vs
line rate) that banking would have to close.

**Wire-scan path (2026-08-05, partial-only, R79).**  The §3
deferred item is built and verified in sim.  In the generated
`rp_wrapper`: a frame whose tuser src has bit 6 set (0x0040,
adapter CMAC-0 tag) bypasses the R78 codec and streams its FULL
raw bytes (L2 headers included -- over-nomination is benign, SR5)
through the engine; it answers ONLY when it nominates, with a
MATCH_REPLY carrying the child's own slot, a wire-reply sequence
counter, the active epoch (SR14'), and payload status bit2 as the
wire-origin marker.  Two earlier drafts put the marker on existing
normative surface and both would have broken a compliant host: the
header flags byte (R78.3 forbids nonzero flags in version 1 -- the
host decoder rejects the frame outright) and status bit1 (R78.7
defines it as ERR -- the reply would decode but read as errored).
Bit2 is the first genuinely unclaimed bit, which is where an
additive extension belongs.  Wire frames seen before any commit
or while a load is open are dropped (feeding the engine mid-load
would corrupt the
shadow) and counted; PERF_REPLY grows an additive 16-byte
extension (payload bytes 16-31: seen/scanned/drops/noms, u32 BE;
pre-wire children still answer 16 bytes, which
`pyro.device.read_perf_counters(with_wire=True)` reports as
`WireCounters | None`).  The xsim differential
(`tests/hw/overlay_table_diff.py`) now interleaves five raw wire
frames with the table-load sequence and checks all of it against
the host model: drop-before-commit, drop-during-load, nominate
(5/5 matches exact), clean-frame silence, and -- two-sided --
that a CRC-refused commit leaves wire scanning on the surviving
table at the surviving epoch.  Counters reconcile with zero
slack (seen 5 = scanned 3 + drops 2, noms 11).  On the tied-off
production shell the path is inert: QDMA H2C frames carry src
0x0001, so the wire branch never triggers.

**CMAC near-end loopback bring-up (2026-08-05, on silicon,
production shell).**  The §2 "verified, not assumed" item is
verified: `scripts/pyro_cmac_loopback.py` programs near-end PMA
loopback over the CMAC-0 AXI-Lite window (BAR2 0x8000 + 0x090,
`GT_LOOPBACK` bit0) and the PCS **ALIGNS against its own TX** —
`STAT_RX_STATUS` 0xc0 (local faults, no link) -> **0x3**
(status=1, aligned=1) with RS-FEC active — then restores the card
to as-found.  This works on the PRODUCTION shell because the CMAC
and its AXI window live in the static region; only the box-plugin
datapath is tied off.  Three facts earned the hard way, recorded
for the wiretap bring-up: (a) the CMAC-0 subsystem reset (BAR2
0x00C bit4) clears the CMAC AXI register bank to defaults, so
`GT_LOOPBACK` must be written AFTER the reset, not before — the
first attempt wrote it first and the reset silently erased it;
(b) the shell ties the `gt_loopback_in` port to zero but the IP
ORs it with the AXI `ctl_gt_loopback` bit
(`cmac_usplus_0_wrapper.v:2390`), so the AXI path works despite
the tie-off; (c) register access must use whole 32-bit stores —
mmap slice writes may issue byte strobes the CMAC AXI slave
ignores.  Remaining for wire traffic end-to-end: flash the
wiretap shell (G4, owner) and re-run this tool with `--keep`; the
TX generator's frames then return via this loop into `pyro_rp`.

**G4 EXECUTED + wire path end-to-end on silicon (2026-08-05,
owner-authorized).**  QSPI flashed with the wiretap image (erase/
program/verify clean, 16m24s, onic unloaded first, host uptime
unbroken), `overlay_wire.bit` linked against the wiretap static
(WNS +0.020, pr_verify OK, `hw/dfx/build_overlay_rm.sh`), cold
power cycle, `10ee:903f` + build_timestamp 0x08042258 on boot.
`scripts/pyro_wire_e2e.py` then ran the three-phase experiment at
the TX generator's ~245k frames/s under near-end loopback:

- **A (no table):** 734,648 seen = 734,648 drops in 3 s; nothing
  scanned.  Fail-closed, fully counted.
- **B (clean table):** 734,409 seen = 734,409 scanned, 0 noms,
  0 replies captured.  Reply-only-on-nomination holds on silicon.
- **C (matching table, anchor = the generator's constant 14-byte
  prefix):** 733,918 scanned, 733,918 noms; 50/50 captured wire
  MATCH_REPLYs valid — slot 1, epoch 2 (SR14' attribution on live
  wire traffic), pid 0, end 14, header flags 0.

Cumulative counters after the run reconcile with ZERO slack over
8.9M frames: seen 8,857,765 = scanned 8,104,168 + drops 753,597.
A table swap (epoch 2 -> 3) succeeded while wire frames arrived
at full rate — host control and wire traffic coexist through the
arbiter.  The complete OQ-2 path — TX gen -> CMAC loopback ->
adapter -> arbiter -> `pyro_rp` wire-scan -> engine -> wire
MATCH_REPLY -> C2H -> host — is closed on silicon.  64-byte
frames at 245k/s is 15.7 MB/s, inside the 32-50 MB/s engine
rate; line rate still requires banking (priced in §5, unchanged).

**Per-packet match timing (2026-08-05).**  Three numbers decompose
the per-frame cost, all on the 64 B generator frame at 250 MHz
(4 ns/cycle; `scripts/pyro_wire_timing.py`, full run recorded in
the notebook).  (a) **Engine scan, on silicon**, from the R45a
per-scan counters (200 samples each, 0 discarded): the clean table
scans at the floor — median 323 cycles = 5.05 cycles/byte =
1292 ns (max 332 cycles = 1328 ns); the matching table
(3 nominations/frame) runs median 407 cycles = 6.36 cycles/byte =
1628 ns (max 416 cycles = 1664 ns).  (b) **Whole-wrapper
arrival->reply latency**, from the beat-exact xsim measurement on
the same RTL (10 wire frames): wire-frame arrival to first reply
beat min/median 493 cycles (1972 ns), max 522 (2088 ns); to the
last reply beat 494/494/523 cycles (1976/1976/2092 ns).  The
wrapper is busy (arrival until `s_axis_tready` re-asserts) for
495/495/524 cycles (1980/1980/2096 ns) — under the 1024-cycle
generator gap with ~2x headroom, so 245k frames/s is sustainable
with zero drops, as the silicon counters confirm (seen == scanned,
drops delta 0).  (c) **Host MATCH_REQUEST RTT** for contrast (raw
socket, wall clock): 50/50 replies, min/median 19 us, max 38 us —
~12x the in-fabric scan latency.  What each number includes: (a)
is the engine alone — the counters reset at scan start and latch
at scan end, so no ingress or reply cost; (b) adds the full
wrapper pipeline (ingress, codec bypass, engine feed, MATCH_REPLY
emission) but is sim-measured on the silicon-validated RTL; (c)
adds raw socket, driver, and QDMA both ways — transport, not
matching, remains the dominant term for host-fed scans.
