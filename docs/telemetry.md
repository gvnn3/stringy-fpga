# Telemetry — the definitive inventory

What this system can tell you about itself, where each number actually comes
from, and — just as important — what it cannot tell you. Written against the
A5 overlay child resident on the U250 at `0000:02:00.0` (onic netdev `ens2`,
jumbo shell, `MAX_PKT_LEN 9600`), branch `phase2-snort`. Every figure below is
a measured or code-verified number from the telemetry audits; none is
aspirational.

## 1. Orientation

This system is a **nomination engine, not a verdict engine**: the FPGA scans
traffic against an Aho-Corasick anchor table and *nominates* rules whose
anchors fired; the host (ultimately Snort) re-verifies nominations against the
full rule semantics. That architecture decides what telemetry is *for*. The
device's job is to be sound (never suppress a rule that could fire), so
device-side telemetry exists to answer three operational questions — is the
right table resident (identity/epoch), is the scan path alive and keeping up
(perf counters, match replies), and did a table transfer land intact
(CRC-gated commit status). Precision — how many nominations were false — is
by design a *host-side* quantity, measured offline by
`pyro.overlay.table.precision_delta` and, when wired, by the Snort
re-verification loop. Telemetry here is therefore split into two inventories:
the device surface (everything the R78 wire protocol will carry, backed by
wrapper/engine CSRs) and the host surface (daemon counters, scheduler scores,
triage reports, netdev sysfs). The single most important cross-cutting fact:
**silence is overloaded**. No-reply is the *defined* encoding for "child
predates this wire kind" (R78.4), and it is byte-identical to a wedged RP, a
swapped-out data plane, or a lost frame. Only a preceding successful
`probe_device` makes a `None` meaningful.

## 2. The four owner metrics

### 2.1 Switch time

**What it means here.** How long the filter is degraded while changing what
the device matches. There are two entirely different switch mechanisms with a
~1000× gap between them, and the gap is the reason the A5 overlay exists:

| Mechanism | Measured | What changes |
|---|---|---|
| Overlay table load+commit (2564 B image) | **12.3 ms** | The AC table, in-band over R78 |
| Full 1.64 MB corpus table, wire time | **0.66 ms** at 2.3 GiB/s | (wire component only; wall time adds per-chunk round trips + host CRC) |
| JTAG partial-reconfiguration child swap | **13.6 s** | The bitstream itself |

**Where it comes from.** Host-derived, and only host-derived: neither
`pyro.device.load_table` nor `pyro.snort.scheduler.jtag_load_pipeline` takes
internal timestamps. There is no device-side clock you can read for this.

**How to extract.**

```python
from pyro.telemetry import timed_load_table, TelemetryState
state = TelemetryState()
st = timed_load_table(cfg, image, state)   # wall-clocks pyro.device.load_table
state.view()                               # switch_last_ms, switch count
```

or by hand: `t0 = time.monotonic(); st = load_table(cfg, image);
dt = time.monotonic() - t0`. Exposed as `pyro_switch_last_ms`,
`pyro_switch_count`, `pyro_switch_wire_ms_est`, and (for contrast)
`pyro_switch_pr_baseline_s` on the `/metrics` endpoint.

**Caveats.** The wall time includes one round trip per chunk
(BEGIN + N×DATA + COMMIT, chunk = `max_payload − 12`; use
`MAX_PAYLOAD_JUMBO` on the jumbo shell) plus host-side CRC-32C — it is not
pure wire time. A *refused* commit raises `PyroLoadError`, leaves the old
table active (fail-closed, A5 §5), and is not a switch;
`timed_load_table` records only successes. During a JTAG PR swap the daemon's
only record of the blind window is `stats.unfiltered_seconds` — measured
blind windows run ~14–45 s once wedge recovery is included.

### 2.2 Rules matched

**What it means here.** Nominations, not verdicts: a match entry means "this
slot's anchor occurred in this corpus", and each slot maps to a *list* of
`gid:sid` rules (anchors deduplicate across rules — collapsing to one sid is
an SR3 completeness defect).

**Where it comes from.** Wire, kind 0x04 `MATCH_REPLY`:
`count` (u16 BE, payload[0:2]), `status` (u16 BE, bit0 = OVF), `epoch`
(u32 BE, payload[4:8]), then `count` × 24-byte entries in **little**-endian
`<QQII` = (start, end, pattern_id, flags) inside the big-endian header (R47
packing). The count is the *wrapper's* capture counter (res_wr pulses into
match_mem), not the engine's `A_OUT_COUNT` CSR — those are independent
counters with no wire-side cross-check.

**How to extract.** Working parse: `match_on_device()` in
`scripts/pyro_overlay_bringup.py:74`. Per-sid attribution: `pattern_id` is
the slot index in the resident group; map through the group's sidecar
(`RuleGroup.sidecar()` → `{pattern_id: (gid:sid, ...)}`, built from
`pyro.snort.groups.GroupSlot`/`RuleRef`). Aggregated host-side as
`pyro_nominations_total` and `pyro_nominations_by_sid{sid="gid:sid"}`;
daemon-side as `stats.nominations_by_tier`.

**Caveats.**
- The engine caps replies at **OUT_CAP = 61** entries; `count == 61` with OVF
  set means "at least 61". OVF is the OR-latch of engine `A_STATUS` bit3
  sampled during ST_DRAIN. The daemon's response is trim-and-resume
  (`stats.ovf_resumes`) or, on buffer-aligned overflow, wholesale nomination
  of every `\A`-anchored slot's rules (`stats.ovf_anchored_floods`).
- `start` is dead telemetry: the overlay engine hardwires `res_start <= 0`
  ("host derives from end", `pyro_overlay_engine.v:654`). Recompute
  `start = end − len(anchor) + 1` host-side.
- `end` is relative to *this request's* corpus (fresh per MATCH_REQUEST).
- Attribution gate (SR14′): compare the reply's epoch against the epoch the
  *device* reported at commit time, not a fresh model's (which is always 1;
  the device's is cumulative since engine reset). The epoch's low 16 bits
  also ride every entry's flags: `flags = (epoch[15:0] << 16) | 0x0001`.
- No cumulative per-rule/per-slot hit counters exist in fabric; anything
  beyond 61 entries per reply is lost except for the OVF bit.

### 2.3 Packets dropped

**What it means here.** Three distinct layers, only one of which is
instrumented, and the instrumented one is the least dangerous.

**Layer 1 — netdev (instrumented).** Sysfs counters under
`/sys/class/net/ens2/statistics/`: `rx_dropped`, `rx_missed_errors`,
`rx_fifo_errors`, `rx_over_errors`, `rx_nohandler`, `rx_errors` (plus
`rx_crc_errors`/`rx_frame_errors`/`rx_length_errors` for corruption),
`tx_dropped`, `tx_errors`, `tx_fifo_errors`, with `rx_packets`/`rx_bytes`/
`tx_packets`/`tx_bytes` as denominators. Sample as before/after deltas around
a run. Surfaced as `pyro_netdev_rx_dropped` etc. **Caveat:** counters zero on
onic driver reload, which `scripts/pyro_wedge_recover.sh` performs after
every JTAG partial load — deltas spanning a residency swap are invalid.

**Layer 2 — request-vs-reply accounting (host-derived, the honest
substitute).** `WireTransport.scan` returns an empty result on timeout,
indistinguishable from "scanned, zero matches"; the daemon's
`stats.requests` counts *attempts* only. `pyro.telemetry.TelemetryState`
closes this: `record_scan(None)` counts sent-but-unanswered, giving
`pyro_requests_sent_total`, `pyro_replies_received_total`, and
`pyro_request_loss_ratio`. This is inference from silence, not a drop
counter.

**Layer 3 — in-fabric (the honest gap: none).** There is **no in-fabric
dropped-frame counter of any kind**, by design. Non-PYRO frames (bad
EtherType/magic/version) are silently dropped per R78.1 (`ST_CLASSIFY
!is_pyro → drop`); unknown kinds are silently dropped per R78.4; neither path
increments anything. The device cannot report what never arrived and keeps no
RX-frame counter.

**The EQDMA trap.** With ≥ 2 H2C queues the QDMA IP silently swallows frames
with **no `tuser_err` and no netdev counter tick** (measured; degrades to
**13% loss**; 1 queue is clean). Sysfs reading clean is therefore *not*
evidence of no loss. Bench with `PYRO_BENCH_QUEUES=1`, always. See
`docs/amd-support-case-eqdma-h2c-loss.md`.

### 2.4 Rules missed

**What it means here.** This one requires plain speech: **zero hard misses
among resident rules is an oracle-verified invariant (SR3), not a counter.**
There is no "misses" register anywhere in the system, and there could not be
— a device cannot count events it, by definition, did not detect. The claim
"the resident group misses nothing" is established off-device by the
two-sided oracle (AC vs. lowered chains; `precision_delta`'s `missed` field
**must be 0** — checked, not assumed) and by construction in lowering (every
transform is an over-approximation: it may add nominations, never remove
them). `pyro_missed_resident_hard` is emitted as a constant 0 with exactly
this meaning.

What *is* countable are the three bounded channels around that invariant:

1. **OVF truncations** — entries beyond OUT_CAP = 61 in one reply are lost as
   *entries* (the daemon's resume/anchored-flood machinery recovers the
   *rules*, soundly). Counted: `stats.ovf_resumes`,
   `stats.ovf_anchored_floods`, `pyro_missed_ovf_truncation_events`.
2. **Non-resident rules** — rules in groups not currently loaded are not
   scanned in hardware at all. On the profiled corpus: 21 groups over 8
   classes; largest group ($HTTP_PORTS/0) is 256 rules in 253 slots.
   Countable as corpus-total minus resident (`pyro_missed_nonresident_rules`
   vs. `pyro_coverage_resident_rules`). The scheduler's `V(g)` scores and
   `unfiltered_seconds` quantify the exposure.
3. **Lowering-dropped rules** — the always-forward tier: rules the compiler
   cannot express as anchors and therefore forwards to the host unfiltered.
   On the profiled corpus: **4017 rules = 95 header-only + 3896
   anchor-compilable + 26 always-forward** (subtiers of anchor-compilable:
   1759 raw-anchor + 2137 normalized-buffer), so
   `pyro_missed_lowering_dropped` = 26. Per-rule reasons are in the triage
   report (`python -m pyro.snort.triage <rules> -o report.json`); per-rule
   dropped *conjuncts* (options the circuit does not realize, still sound)
   are `LoweredSlot.dropped`/`oa_classes` in the group manifest.

## 3. Full inventory — device side

The host has **no CSR access path**. Every CSR (0x0010–0x0084) lives on the
internal wrapper↔engine bus; the host's only surface is the R78 wire kinds
over EtherType 0x88B5. A CSR the wrapper does not copy into a reply is
unobservable on silicon (sim/xsim only). Both views are listed; "wire" rows
are extractable today, "internal" rows are not.

### 3.1 Wire-extractable metrics

| Metric | Source | Extraction | Caveats |
|---|---|---|---|
| device_usable + reason | 0x01/0x02 ID handshake | `pyro.device.probe_device(DeviceConfig(iface='ens2', chardev=None, max_payload=MAX_PAYLOAD_JUMBO, probe_timeout_s=2.0))` → `(bool, reason)` | CAP_NET_RAW gate first; up to 3 × 2.0 s attempts (R84). Only the SPEC16 high 16 bits of static_shell_id are checked (R81) — wrong BUILD16 still passes. |
| static_shell_id (SPEC16<<16 \| BUILD16) | 0x02 ID_REPLY payload[0:4] u32 BE | `struct.unpack('>I', dec.payload[0:4])[0]` (`_parse_id_reply`) | Compile-time constant, not a health signal. BUILD16 defaults 0 for pattern children (S11) unless `PYRO_BUILD16` injected. probe_device surfaces it only inside its reason string. |
| wire harness_version | 0x02 ID_REPLY payload[4:8] u32 BE | hand-rolled: `struct.unpack('>I', dec.payload[4:8])[0]` — no public helper parses past payload[0:4] | Namespace hazard: this is the R45/R78.5b WIRE namespace (0x00010000), distinct from the PYROART1 artifact HARNESS_VERSION (0x00020100+) *and* the overlay engine's CSR-default 0x00020300. Never compare across namespaces. |
| rp_child_id (resident child) | 0x02 ID_REPLY payload[8:12] u32 BE | `struct.unpack('>I', dec.payload[8:12])[0]`; verify via `rp_child_id_from_hash(pattern_hash_hex)` | 0 = default ID stub. Identifies the child *bitstream*, not the table — for the A5 child the table identity is `active_table_id`. |
| slot/seq echo | any reply, PYRO header bytes 4–5 / 6–11 | `dec = decode_frame(frame[14:]); dec.slot, dec.seq` | The only request-reply correlation mechanism; matching is the host's job (`if dec.seq != seq: continue`) — the device detects neither gaps nor duplicates. |
| match count | 0x04 MATCH_REPLY payload[0:2] u16 BE | `struct.unpack('>HH', dec.payload[0:4])` — `scripts/pyro_overlay_bringup.py:74` | Wrapper-counted, capped at min(requested cap, OUT_CAP=61); 61+OVF = "at least 61". |
| OVF bit | 0x04 payload[2:4] u16 BE bit0 | `bool(status & 1)` | OR-latch of engine A_STATUS bit3 across ST_DRAIN; means truncated at 61 — re-scan/resume. Bits 1–15 currently always 0. |
| match epoch (SR14′ gate) | 0x04 payload[4:8] u32 BE | `struct.unpack('>I', dec.payload[4:8])[0]` | 0 = pre-A5 child or no table ever committed. Latched in ST_TBL_STAT, exact because commits serialize through the same single-frame FSM. Compare against the device's commit-time epoch, not a fresh model's. |
| per-match entry | 0x04 payload[8+24i:32+24i] LE `<QQII` | `_s, end, pid, _f = struct.unpack('<QQII', ...)` per entry | LE inside BE header. `start` always 0 (recompute `end−len+1`); `end` relative to this request's corpus; `pattern_id` = slot index → gid:sid list via sidecar; `flags = (epoch[15:0]<<16) \| 1`. |
| scan cycles (R45a) | 0x07 PERF_REPLY payload[0:8] u64 BE (CSR 0x0058/0x005C) | `read_perf_counters(cfg, slot=1)` → `(cycles, bytes)` or None | **Most recent scan only** — wrapper writes CTRL.RESET before every scan (W4); destroyed by the next MATCH_REQUEST. Works on generated children AND (since 2026-07-31) the A5 overlay child, whose CYCLES include feed stalls; a pre-fix overlay child returns the 0x0002030000020300 constant, which `pyro.telemetry` fingerprints and nulls. On a v4 multi-core child, routes to the last-done core. None is not a fault. |
| scan bytes (R45a) | 0x07 payload[8:16] u64 BE (CSR 0x0060/0x0064) | second element of `read_perf_counters` | Same caveats. Counts bytes the *engine* consumed (post W5 clamp), ≤ declared length if the host lied. cycles/bytes gives cyc/B: generated group engines ~1 B/cyc/core; the overlay child ~8–10 cyc/B — now wire-readable (sim-measured 9.28 cyc/B; per-scan overhead shrinks the gap on longer subjects). |
| active_table_id | 0x0C TABLE_STATUS_REPLY payload[0:4] (CSR 0x0068 A_TBL_ACTIVE) | `read_table_status(cfg, slot=1).active_table_id`; non-mutating; every table op also returns full TableStatus | = CRC-32C of the committed image; verify vs `pyro.overlay.table.table_id(image)`. 0 = nothing valid resident, never "unknown". Unchanged by a refused commit. None = child predates A5 §3. |
| shadow_table_id | 0x0C payload[4:8] (CSR 0x006C A_TBL_SHADOW) | `.shadow_table_id` | Live running CRC (`~shadow_crc`) of bytes received so far; must equal declared CRC at commit. Reset to seed (reads 0) by BEGIN/ABORT; meaningful only while load_open. |
| epoch | 0x0C payload[8:12] (CSR 0x0070 A_TBL_EPOCH) | `.epoch` | Increments only on CRC-verified commit; cumulative since engine reset — compare deltas or the device's commit-time value. 0 = never committed. |
| status_flags | 0x0C payload[12:16] (CSR 0x0074 A_TBL_STATUS) | bit0 bytes≠0 (`& 0x1`), bit1 `.load_open`, bit2 `.active_valid`, bit3 commit_err (`& 0x8`) | bit3 clears on the *next* successful commit; the engine's sticky refused-commit flag (A_STATUS bit2) never clears except reset and is not host-visible — the two diverge after refuse-then-succeed. |
| bytes_received | 0x0C payload[16:24] u64 BE (CSR 0x0078 A_TBL_BYTES) | `.bytes_received`; also embedded in every `PyroLoadError` ("device took %d of %d bytes") | 32-bit in hardware, wire high word wrapper-zeroed (fine: largest table ~2.1 MB). Reset by BEGIN/ABORT. Also the sequential-delivery gate: DATA offset ≠ bytes_received → error 8. §3.1 spec divergence: 0x007C is TBL_CTRL in the RTL, not TABLE_BYTES_HI. |
| capacity_states | 0x0C payload[24:28] (CSR 0x0080 A_TBL_CAPS) | `.capacity_states` | Build constant (40960 = 10 URAM banks × 4096 on the full-corpus build; full corpus needs 39,647), not a measurement; < 39647 identifies a non-full-corpus build. Commit also enforces `hdr_n_states ≤ MAX_STATES` in hardware. |
| table error | 0x0C payload[28:32] (wrapper register `tbl_err`) | `.error` — 0 ok; 8 = PYRO_E_TABLE_SEQUENCE (chunk rejected, shadow untouched, restart from BEGIN) | Per-operation, cleared at each op's ST_CLASSIFY. Engine-side commit *refusal* does NOT set it — that shows as flags bit3 + unchanged active_table_id (the fail-closed signature). |
| STATUS/ERROR code | 0x05 payload[0:4] u32 BE | `struct.unpack('>I', ...)` — wrapper emits exactly one code: PYRO_E_NOT_RESIDENT = 7 (op addressed to slot ≠ 1) | The only "wrong slot" signal; indistinguishable from "other-tenant slot empty" by design (single-tenant, R87). `read_perf_counters` maps it to None; `_table_op` raises. |
| A5-capability fingerprint | wire *absence* | `read_table_status` None ⇒ child predates A5 §3; `read_perf_counters` None ⇒ pre-v2.4.0 (or no transport) | Defined expected outcomes, never faults — but silence is overloaded (wedged card / swapped plane / EQDMA drop look identical); only meaningful after a passing probe. |

### 3.2 Internal-only CSRs (sim/debug; never cross the wire)

| CSR | Contents | Why you cannot read it on silicon |
|---|---|---|
| 0x0014 A_STATUS | bit0 BUSY, bit1 DONE (level, W4 polling-safe), bit2 sticky commit-refused, bit3 OVF | Wrapper-internal polling only (ST_WAIT_BUSY, ST_DRAIN); bit2 never clears except full reset and is never copied into any reply. |
| 0x0018 A_CIRC_ID0 | MAGIC 0x5059524F ("PYRO") | Never read by the wrapper. Host-visible identity is enforced at commit instead: TABLE_BEGIN carries engine_id (image header byte 12, LE, via `_engine_id_of`) and the engine refuses unless it equals 0x0A5E0001. |
| 0x0048 A_OUT_CAP / 0x004C A_OUT_COUNT | cap written per scan (ST_CAP, min(host cap, 61)); engine's own emit count | Wrapper never reads them for a reply — wire count is the wrapper's independent capture counter; divergence is unobservable from the host. |
| 0x0058–0x0064 CYCLES/BYTES | R45a perf counters | Generated children and (since 2026-07-31) the A5 overlay engine — 64-bit, per-scan, CYCLES incl. feed stalls. Pre-fix overlay children fall to default rdata 0x00020300. |
| 0x0084 A_TBL_EXPECT | host-declared CRC from TABLE_BEGIN (frame offset 44), written before load opens (two-stage ST_TBL_OPEN) | Write-only in practice; reads return the engine default. Exists because a self-computed CRC is self-consistent and therefore useless as a check — measured on silicon: an in-flight-corrupted transfer once committed cleanly and destroyed the working table. |
| (contract) | CSR read latency | The overlay engine *registers* csr_rdata: address in cycle N → data in N+2; generated engines are combinational (N+1). The wrapper's table paths insert spacer cycles; ST_PERF assumes N+1 — exactly why PERF against the overlay child is garbage, and why an early ST_TBL_SEQ read "would compare the chunk offset against a stale register and reject valid chunks". Any future CSR consumer must know which engine it faces. |

## 4. Full inventory — host side

| Metric | Source | Extraction | Caveats |
|---|---|---|---|
| SR19 daemon stats | `pyro.snort.daemon.Stats.snapshot` | `daemon.stats.snapshot()` → nominations_by_tier, reverify {confirms, rejects, rejects_by_class}, tripwire_hits, identity_mismatches, ovf {resumes, anchored_floods}, requests, segments, resident {group, rp_child_id, rules}, unfiltered_seconds (includes the open window), swaps | All monotonic except resident gauges. reverify_* have no live producer (nothing calls `Stats.reverified()`) — read 0 unless the Snort loop is wired externally. identity_mismatches increments *without* `Stats._lock` (daemon.py:513) — snapshots can race it. `requests` counts attempts, not answered scans. |
| Port-mix histogram (scheduler input) | `PortMixHistogram.snapshot` | `daemon.histogram.snapshot()` → {dst_port: decayed bytes}; half-life 60 s; fed per client→server segment | max_ports=256, coldest port evicted — long-tail scan traffic under-represented. Decay applied lazily at read. Per-port granularity is load-bearing: class aggregation measured to elect ~1%-coverage groups over ~28% ones (`docs/studies/a5-working-set.md`). |
| Port-mix by class (display only) | `PortMixHistogram.snapshot_by_class` | `snapshot_by_class()` → {port_class: decayed_bytes}; class via `VarTable.most_specific_class` | Explicitly NOT a scheduling input. Inherits snapshot() caveats; 'any' never counted. |
| Residency value V(g) | `pyro.snort.scheduler.ResidencyScheduler.scores` | `scheduler.scores()` → {group: Σ_ports decayed_bytes × rules_that_could_fire} | Only "available" groups appear. `_fire_cache` keyed (group, port), never invalidated — stale if the SR13 VarTable mutates at runtime. Not thread-safe vs concurrent group-list changes. |
| Swap/residency events | `ResidencyScheduler.tick` | `tick()` → new resident name or None; `stats.swaps`; blind time → `stats.unfiltered_seconds` (clock starts before load_fn) | A FAILED load or identity mismatch is silent except unfiltered_seconds — no load_failures counter — and still arms `_last_swap`, delaying retry by min_dwell_s (300 s default). |
| Triage tiers (rules not expressible) | `pyro.snort.report.build_report` | `python -m pyro.snort.triage third_party/snort3-community-rules/snort3-community.rules -o report.json` → aggregates: tiers {95/3896/26}, subtiers {1759/2137}, parse_error_rules (SF15: parse failures → always-forward, never exceptions), pcre, buffer histos, header | "Not expressible" = always-forward tier ⊇ parse_error_rules; per-rule `reason` says why. invariant_failures emitted only when corpus sha256 matches the profiled snapshot — SF8–SF13 oracle numbers do not transfer to other rulesets. Run from repo root with `.venv-pyro`. |
| Per-rule dropped conjuncts / OA classes | `pyro.snort.lowering.lower_rule` → `LoweredSlot` | `.dropped` (SR4 list), `.oa_classes` ⊆ {dropped_conjuncts, anchor_strip, case_fold, chunk_overlap}, `.fused_pcre`, `.chain_len`, `.tail_span`; flows to `RuleRef.dropped/.oa` → `RuleGroup.manifest()` | Lowering is total: any internal surprise degrades to anchor-only, so `dropped` can silently grow — the manifest records what *shipped*, not what was lowerable. chunk_overlap is always present. |
| Group packing shape | `pyro.snort.groups.pack_groups` / `RuleGroup.manifest` | 21 groups / 8 classes on current corpus; largest $HTTP_PORTS/0 = 256 rules → 253 slots. Per group: n_slots, rule_count, max_anchor_len, max_tail_span, overlap_tail (= max_tail_span − 1), `sidecar()`; manifest emits group_hash, rp_child_id, over_approx_classes | pattern_id = slot index; slot → *list* of gid:sid. Tombstones only after `repack_with_tombstones` (index retained, never renumbered); fresh packs have none. Sidecar/dropped/oa are manifest data, never hashed. |
| Table image metrics | `pyro.overlay.table.stats` / `table_id` / `manifest` | `stats(ac, image)` → TableStats(n_states, n_patterns, n_transitions, n_outputs, image_bytes); `table_id` = CRC-32C (Castagnoli, final inversion, forced non-zero — matches the RTL loop); `strong_id` = 128-bit domain-separated SHA-256 (host identity of record) | CRC-32C, **not** `zlib.crc32` (IEEE) — zlib disagreed with the device on first run. table_id 0 reserved. Case-correct group tables (`build_for_group` → CaseSplitTable) run two automata; n_states = ci + cs combined. |
| Overlay precision give-up | `pyro.overlay.table.precision_delta` | `precision_delta(group, subject)` → {ac_nominations, lowered_nominations, extra, missed, sound} | `missed` must be 0 (soundness assertion — checked, not assumed). Per-subject; needs representative traffic. |
| On-device perf (host wrapper) | `pyro.device.read_perf_counters` | `read_perf_counters(cfg, slot=1)` → (cycles, bytes) or None; ~8 cyc/B at 250 MHz is the utilization anchor | Hardware access — verifier agent's domain. None overloaded (pre-v2.4.0 / non-resident / no transport / OSError). Counters restart on PR reconfig; no epoch field → cross-swap deltas invalid. |
| A5 table status (host wrapper) | `pyro.device.read_table_status` | `read_table_status(cfg, slot=1)` → TableStatus or None; `.load_open`, `.active_valid` | Hardware access. None expected against pre-A5 child. epoch *does* increment per commit → usable to detect table churn between reads (unlike perf). |
| Switch timing | caller-timed `load_table` / `pyro.telemetry.timed_load_table` | see §2.1 | No internal timing anywhere; anchors 12.3 ms / 0.66 ms wire / 13.6 s PR. |
| netdev drop counters | `/sys/class/net/ens2/statistics/` | read as integers; before/after deltas | Zeroed on onic reload (every JTAG swap via wedge recovery); blind to EQDMA multi-queue loss (13% with clean counters, measured); ens2 control binding only — says nothing about the tapped data link. |
| OVF/truncation aggregate | `Stats.ovf_resumes` / `ovf_anchored_floods` + MATCH_REPLY bit0 | `snapshot()['ovf']` | Resume re-sends trimmed corpus and re-labels host-side (start_off never read — the hardware-real S2 form); counts resume *events*, not entries lost. |
| Tripwire hits by class | `Stats.tripwire_hits` (fed by `tripwire_class`) | `snapshot()['tripwire_hits']` → {content_encoding, chunked_te, percent_density}; counted once per FLOW, then every segment nominates kind='tripwire' | Deliberately byte-level and over-eager (forces forwarding — always sound). First 2048 bytes of a segment only; percent-density threshold 0.05 on the HTTP request line. |
| Unified snapshot / Prometheus | `pyro.telemetry.collect_snapshot` / `prometheus_text` | `collect_snapshot(cfg=None, ...)` → dict (cfg=None ⇒ host-only, `device.usable: false`, never raises); `prometheus_text(snapshot)` | include_corpus defaults to `cfg is not None` — host-only snapshots skip the multi-second pack_groups. Null values omitted from /metrics, never zeroed; every metric carries HELP text embedding its caveat. |

## 5. Gaps — what you cannot measure, honestly

Device side:

- **No host CSR access path at all.** Everything the wrapper does not copy
  into a reply (A_CIRC_ID0, A_OUT_COUNT, A_OUT_CAP readback, A_STATUS bit2
  sticky commit-refused, A_TBL_EXPECT) is unobservable on silicon.
- **No in-fabric dropped-frame counters.** Non-PYRO and unknown-kind frames
  drop silently (R78.1/R78.4) with nothing incremented; host-side timeouts
  are the only detection.
- **~~No R45a counters in the A5 overlay engine~~ — FIXED 2026-07-31.**
  The engine now maps 64-bit cycles/bytes at 0x0058–0x0064 (CYCLES counts
  busy cycles *including feed stalls* — the scan latency the host actually
  experiences; BYTES counts queue pops; CTRL.RESET zeroes both, same
  per-scan W4 semantics as generated engines), and ST_PERF holds each
  address two cycles so the read is correct against either CSR-bus latency.
  Measured in sim over the wire: 362 cycles / 39 bytes = 9.28 cyc/B.
  A child built *before* this fix still returns the HARNESS_VER constant
  (0x0002030000020300 in both halves); `pyro.telemetry` fingerprints that
  exact value and nulls it rather than charting it.
- **No per-rule/per-slot hit counters in fabric**; attribution is per-reply
  only, and beyond OUT_CAP=61 only the OVF bit survives.
- **Per-scan counters destroyed by the next scan** (CTRL.RESET before every
  MATCH, W4); no cumulative device-side accumulator.
- **Match `start` is dead** (hardwired 0); reconstruct as `end − len + 1`.
- **No wrapper/engine divergence visibility**: wire match_count and CSR
  OUT_COUNT are independent with no cross-check.
- **No backpressure observability**: group-engine `in_ready` stall duty and
  2-deep skid occupancy are invisible; a mostly-stalled feed looks identical
  on the wire (only PERF cycles, where they exist, hint at it).
- **No transport-loss observability device-side**: EQDMA ≥2-queue loss (13%
  measured, no tuser_err) is invisible; the device keeps no RX counter.
  `PYRO_BENCH_QUEUES=1`.
- **Silence is overloaded** (defined None vs wedged/swapped/lost — probe
  first).
- **bytes_received is 32-bit in hardware** despite the u64 wire field (high
  word zeroed); fine at ~2.1 MB tables, silent truncation hazard only past
  4 GiB (impossible today).
- **No error history**: table error is per-op, commit refusal one bit; no
  log/counter of *which* check (engine_id, n_states, CRC) refused.
- **PERF carries no epoch** — cycles/bytes cannot be attributed to a table
  generation the way matches can.
- **v4 multi-core: per-core telemetry not addressable** (ID/PERF route to
  last-done core; no core-id field in any reply).
- **No environmental/shell telemetry over R78**: temperature, power, clock
  health, PCIe/AER, QDMA queue stats are all shell/driver-side (onic sysfs,
  hw_server); static_shell_id is a compile-time constant, not health.
- **Documented spec divergence**: A5 §3.1 lists 0x007C TABLE_BYTES_HI; RTL
  implements TBL_CTRL there and one 32-bit counter at 0x0078
  (`rp_wrapper.py:571` — amend the spec, not the RTL). The offset-addressed
  idempotent TABLE_DATA of §3 is narrowed to strictly-sequential delivery
  (error 8 on any other offset).

Host side:

- **Request loss uncounted in the daemon**: empty ScanResult on timeout ==
  "zero matches"; `stats.requests` is attempts-only. (Closed when scans are
  routed through `TelemetryState.record_scan` → `pyro_request_loss_ratio`.)
- **EQDMA loss silent end to end** — clean sysfs is not evidence; bench with
  `PYRO_BENCH_QUEUES=1`.
- **No built-in switch timing** — caller-measured only (§2.1); blind window
  recorded solely as `unfiltered_seconds`.
- **reverify_* counters have no producer** — the live false-nomination rate
  (what `precision_delta` estimates offline) has no wire-up.
- **Failed group loads uncounted**: exception swallowed, min_dwell_s armed
  (up to 300 s retry delay), trace = growing unfiltered_seconds only.
- **identity_mismatches increments lockless** (daemon.py, `identity_ok`) —
  torn reads possible against snapshot().
- **netdev counters zero on onic reload** — never diff across a swap.
- **read_perf_counters None overloaded + epoch-free** — pair with
  `read_table_status().epoch` or the SR14 child-id check.
- **AF_PACKET tap parses IPv4 TCP/UDP only** — IPv6/VLAN/non-IP never reach
  the histogram, biasing V(g) on links carrying them.
- **Histogram evicts the coldest port at 256** — long tail dropped from the
  scheduler's view (bounded-memory trade, unmeasured).
- **`_fire_cache` never invalidated** — runtime VarTable edits leave V(g) on
  stale port values until restart.
- **No per-group residency accounting** — total unfiltered_seconds and swap
  count exist, but not time-resident-per-group or nominations-per-window
  (which a5-working-set-style coverage analysis needs).
- **PR-build artifacts in /tmp are crash-fragile** — a power cut zeroes
  just-written files; after an unclean reboot check pr-builds outputs for
  0-byte files before trusting any manifest-derived metric.

## 6. Extraction quickstart

All commands from the repo root, always with the pinned venv. Snapshot/watch/
serve degrade to host-only (with a stderr warning) when no interface is
given; they never guess a netdev. `--demo` refuses to run without
`--iface`/`PYRO_DEVICE_IFACE` (exit 2, R68) because it touches the card —
that run belongs to the verifier role.

```sh
# One JSON snapshot (host-only without --iface; device sections null, not zeroed)
.venv-pyro/bin/python3 scripts/pyro_telemetry_demo.py --snapshot

# JSON-lines every 5 s
.venv-pyro/bin/python3 scripts/pyro_telemetry_demo.py --watch 5 --iface ens2

# Dashboard + Prometheus endpoint; a single background collector thread owns
# all hardware access (serializes wire traffic — do not run two)
.venv-pyro/bin/python3 scripts/pyro_telemetry_demo.py --serve 8080 --iface ens2
#   GET /              -> web/pyro_dashboard.html
#   GET /snapshot.json -> current snapshot
#   GET /history.json  -> 300-deep ring
#   GET /metrics       -> Prometheus exposition text

# Full guided run (verifier role only — touches the card):
# probe -> status -> build $SSH_PORTS/0 + literal/0 tables (capacity-checked
# vs 40960) -> timed load A -> 20 scans -> timed swap B -> 10 scans -> swap
# back -> 5 scans -> final JSON with switch stats and per-sid nominations
.venv-pyro/bin/python3 scripts/pyro_telemetry_demo.py --demo --iface ens2
```

Programmatic:

```python
from pyro.telemetry import collect_snapshot, prometheus_text, TelemetryState
snap = collect_snapshot()          # cfg=None: host-only, never raises
print(prometheus_text(snap))       # HELP lines carry each metric's caveat
```

For Grafana, point a Prometheus scrape at `/metrics`; dashboard provisioning
lives under `grafana/` (see that directory's README once provisioned — the
served `web/pyro_dashboard.html` needs no external stack). The corpus triage
report (§4) is the offline companion:
`.venv-pyro/bin/python3 -m pyro.snort.triage
third_party/snort3-community-rules/snort3-community.rules -o report.json`.
