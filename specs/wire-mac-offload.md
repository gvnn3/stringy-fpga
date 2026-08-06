# Specification: Wire-Packet MAC Digest as a Co-Resident Program (WIRE-MAC)

- **Spec ID:** `wire-mac-offload`
- **Version:** 0.1.0 (initial draft for owner review)
- **Status:** **DRAFT** — not adopted. Per §8 no phase is authorized
  until the owner bumps to 1.0.0 with an adoption note.
- **Owner:** George Neville-Neil (to adopt); drafted by Spec Writer
  2026-08-06
- **Date:** 2026-08-06
- **Depends on:** `specs/python-regex-offload.md` (PYRO) **v3.0.0**
  (shell, R78 frame protocol, R80 boundary, R90 wire-ingest static) and
  `specs/snort-rule-offload.md` (SNORT-PF) **v2.1.0** (the A5 overlay
  engine that becomes program P0, facts SF22/SF23). This spec adds no
  obligations to either; the seven new frame kinds drafted in §4.4 land,
  on adoption, as a PYRO R78 MINOR amendment (marked additive per
  R78.4), never by silent code change.

---

## 0. Summary — the experiment

Every resident `pyro_rp` child to date has held **one** program, and
changing what the fabric does has meant a JTAG partial reconfiguration
(~17–45 s measured, SNORT-PF SF20). This spec runs the next experiment:
**two deliberately diverse programs co-resident in a single PR child,
switched between at run time, in silicon, by a wire-protocol command —
never by reconfiguration.**

- **P0** is the existing A5 table-programmable overlay SNORT engine,
  **unchanged** — same RTL, same tables, same `MATCH_REPLY` semantics.
- **P1** is new: a **SipHash-2-4 per-packet MAC digest engine** that
  computes a keyed 64-bit digest over the transit-invariant bytes of
  each wire packet it is given and reports the digest to the host. It
  filters nothing, verifies nothing, and drops nothing: it *reports*.

A packet-atomic round-robin dispatcher divides the **wire** frame
stream (tuser src `0x0040`) between the two programs according to a
host-set schedule (`SCHED_SET`, §4.4). Host R78 control frames (src
`0x0001`) always go to the codec regardless of schedule state.

**JTAG/PR reconfiguration is explicitly BANNED as a scheduling
mechanism** (MR2). JTAG remains only the load path for the one
two-program child itself. If the experiment succeeds, "which program
runs" becomes a microsecond-scale in-band decision instead of a
45-second reconfiguration — the OS-scheduler analogy SNORT-PF §3.1
began (table = process image) extended one level up (program = process,
dispatcher = scheduler).

This is a **partial-only** change: the R80 boundary, the wiretap static
(PYRO R90), and `hw/pyro_plugin` are untouched.

---

## 1. Ground-truth facts

Facts are numbered `MF1…` in this spec's namespace. All are verified in
the repo or measured on this host; implementers MUST NOT assume
different numbers.

- **MF1 (wire path is live and measured).** The wire-ingest static is
  flashed and verified end-to-end on silicon (SNORT-PF SF22/SF23, PYRO
  R90): CMAC-0 RX frames reach `pyro_rp` tagged tuser src `0x0040`,
  timing closed live (WNS +0.020 ns). Measured per-engine wire ingest
  is ~30.5 MB/s (~477k 64-B frames/s bound; ~245k f/s demonstrated).
- **MF2 (R78 frame protocol).** Control is in-band R78 Ethernet frames,
  EtherType `0x88B5`, 14-byte header `>BBBBHIHH` (magic `0x50`, version
  `0x01`, flags MUST be 0), frames zero-padded to 60 B minimum, payload
  ≤ 1,486 B. Kind values `0x01`–`0x0D` are taken; unknown kinds are
  dropped by older devices (R78.4 additive discipline).
- **MF3 (one-ABI rule).** Every engine presents the `pyro_circuit`
  port list (clk/rst_n, CSR bus, byte-serial `in_*` feed, `res_*`
  result strobe). Which engine is resident is decided by which RTL
  accompanies the generated wrapper in the synthesis filelist — it is
  not a wrapper option. P1 obeys the same rule (MR16).
- **MF4 (P0 as built).** The A5 overlay engine: 40,960-state
  Aho-Corasick, 50 URAM + 108 BRAM36, registered CSR reads (data at
  N+2), epoch-attributed `MATCH_REPLY`s. This spec does not modify it.
- **MF5 (rate closure).** Byte-serial SipHash absorption at 250 MHz is
  250 MB/s — 8× the MF1 wire-ingest measurement. Digesting is never
  the bottleneck on this path; no wide datapath is needed for v1.
- **MF6 (dispatcher precedent).** The wrapper's v4 multi-core top
  already contains a packet-atomic skip-busy round-robin RX demux and
  a packet-atomic TX mux, measured on silicon (P2e, fmax 260.8 MHz).
  The WIRE-MAC dispatcher is the same shape keyed on the registered
  tuser wire tag instead of frame kind.
- **MF7 (SipHash reference vectors).** The SipHash paper publishes
  64 reference tags for SipHash-2-4 under key `000102…0f` over
  messages `""`, `"\x00"`, … `"\x00…\x3e"`. These are the algorithm
  ground truth for MR13; no self-derived vectors are acceptable.

---

## 2. Definitions

- **Program:** an engine plus its wrapper-side protocol surface,
  co-resident in the child. Exactly two exist: P0 (SNORT overlay),
  P1 (MAC digest).
- **Wire frame:** a frame entering the child with tuser src `0x0040`
  (CMAC-0 RX). The only frames the dispatcher schedules.
- **Control frame:** a frame with tuser src `0x0001` (QDMA H2C, host).
  Always delivered to the R78 codec; never scheduled.
- **Packet-atomic:** a dispatch decision binds at a frame's first beat
  and never changes mid-frame; no frame is split across programs.
- **Quantum:** the number of consecutive wire frames one program
  receives before the round-robin pointer advances (≥ 1).
- **Digest record:** the 16-byte per-packet result P1 produces:
  `{wire_seq, pkt_len, flags, digest}` (§4.4, kind `0x0E`).
- **Mutable field:** a header byte legitimately rewritten by routers
  in transit (TTL, DSCP/ECN, checksums, …) per the RFC 4302 AH model.
- **Transit-invariant coverage:** the digest input with every mutable
  field zeroed **in place** (masked, not elided) so byte offsets are
  identical at every observation point.
- **Zero-slack invariant:** a counter identity that must hold exactly,
  with no tolerance: `seen == digested + skip_nonip + skip_nokey`.

---

## 3. Architecture

```
             CMAC-0 RX (wire, src 0x0040)      QDMA H2C (src 0x0001)
                        |                              |
                        v                              v
+----------------------- pyro_rp (ONE partial) ----------------------+
|  RR dispatcher (packet-atomic, mode/quantum, wire frames only)     |
|       |                        |                                   |
|       v                        v                                   |
|  P0: A5 overlay engine    P1: pyro_mac_engine_top                  |
|      (unchanged SNORT         SipHash-2-4, byte-serial,            |
|       AC matcher)             mask-and-digest, fail-closed keys    |
|       |                        |                                   |
|       v                        v                                   |
|  R78 codec + reply composer (MATCH_REPLY | MAC_REPORT | ACKs)      |
+--------------------------------|-----------------------------------+
                                 v
                        QDMA C2H --> host daemon
```

Key properties, stated once:

1. **One partial, two programs.** Both engines are instantiated in the
   same child behind one wrapper; residency is no longer exclusive.
   The R80 boundary and the wiretap static are byte-identical to the
   SF22 build — this child re-links against the existing locked DCP.
2. **The dispatcher schedules wire frames only.** The R78 control plane
   (key loads, schedule changes, stats) is lockstep request/reply and
   never subject to the schedule — the host can always reach the child
   no matter which program owns the wire stream.
3. **Dispatch is division, not duplication.** A wire frame goes to
   exactly one program. Under mode 2 each program samples the wire; no
   completeness claim survives that sampling (§6 Non-goals).
4. **P1 reports, the wrapper packages.** The engine emits one result
   strobe per digested frame; the wrapper stamps `wire_seq`, batches
   records into `MAC_REPORT` frames, and counts what it must drop.
   Reports are best-effort with honest loss accounting (MR17);
   control replies are lockstep (MF2).

---

## 4. Requirements

Requirements are numbered `MR1…` in this spec's namespace.

### 4.1 Co-residency and scheduling

- **MR1 (two co-resident programs, partial-only).** The child SHALL
  contain P0 and P1 simultaneously in one `pyro_rp` partial bitstream,
  linked against the unmodified wiretap static within the SF2 RP
  budget (SNORT-PF): 80,000 LUT / 160,000 FF / 160 BRAM36 / 64 URAM.
  No static-shell file, boundary signal, or `pyro_rp_stub` change is
  permitted.
- **MR2 (run-time switching only; JTAG ban).** Program selection SHALL
  be a run-time act performed in silicon via `SCHED_SET` (§4.4).
  Using JTAG/PR reconfiguration to change which program serves the
  wire is PROHIBITED as a scheduling mechanism; a bitstream swap for
  that purpose is a defect against this spec, not a workaround.
- **MR3 (dispatch domain).** The round-robin dispatcher SHALL apply
  ONLY to wire frames (tuser src `0x0040`). Host control frames
  (src `0x0001`) SHALL always be delivered to the R78 codec regardless
  of schedule mode, quantum position, or program busy state.
- **MR4 (packet-atomic round-robin).** Dispatch SHALL be
  packet-atomic: the owning program is chosen at the frame's first
  beat and holds through `tlast`. Modes: 0 = P0 only, 1 = P1 only,
  2 = round-robin with `quantum` wire frames per program per turn
  (quantum ≥ 1). Reset default SHALL be mode 2, quantum 1.
- **MR5 (schedule validation).** `SCHED_SET` with mode > 2 or
  quantum == 0 SHALL be rejected: `SCHED_ACK` status = 1 and the
  active settings unchanged. A valid request takes effect at the next
  wire-frame boundary and is acknowledged with status = 0.

### 4.2 MAC coverage model (RFC 4302 mutable-field discipline)

- **MR6 (digest input; masking, not elision).** The digest input SHALL
  be the frame bytes from the start of the IP header through the end
  of frame (per tuser size), with mutable bytes ZEROED IN PLACE.
  Masking rather than elision keeps every byte offset stable, so two
  observation points compute over identical byte positions. Rationale:
  RFC 4302 (IPsec AH) §3.3.3.1 — fields routers legitimately rewrite
  are excluded from the ICV; everything an endpoint would treat as
  packet identity is covered.
- **MR7 (L2 excluded entirely).** Destination/source MAC, every VLAN
  tag (`0x8100`/`0x88A8`, possibly stacked), and the final EtherType
  SHALL be excluded from the digest. They are parsed only to locate
  the L3 header.
- **MR8 (IPv4 mask set).** Offsets relative to the IPv4 header start:

  Zeroed (mutable):
  - byte 1 — DSCP/ECN
  - bytes 6–7 — flags + fragment offset
  - byte 8 — TTL
  - bytes 10–11 — header checksum
  - bytes 20 … IHL·4−1 — ALL options bytes when IHL > 5
    (set record flag bit5, `ip-options-zeroed`)

  Covered (identity):
  - byte 0 — version/IHL
  - bytes 2–3 — total length
  - bytes 4–5 — identification
  - byte 9 — protocol
  - bytes 12–19 — source and destination addresses
- **MR9 (IPv6 mask set).** Offsets relative to the IPv6 header start:

  Zeroed (mutable):
  - byte 0 — KEEP the version nibble, zero the low nibble
    (traffic-class high bits)
  - bytes 1–3 — traffic-class low bits + flow label
  - byte 7 — hop limit

  Covered (identity):
  - bytes 4–5 — payload length
  - byte 6 — next header
  - bytes 8–39 — source and destination addresses
- **MR10 (L4 mask set).** Offsets relative to the L4 header start,
  the only L4 bytes zeroed (everything else in the L4 header and the
  ENTIRE payload is covered):
  - TCP — bytes 16–17 (checksum)
  - UDP — bytes 6–7 (checksum)
  - ICMP / ICMPv6 — bytes 2–3 (checksum)
  - unknown L4 protocol — zero nothing; cover all of it and set
    record flag bit4 (`other-L4`)
- **MR11 (non-IP skip; extension headers not walked).** Frames whose
  post-VLAN EtherType is not IPv4/IPv6 (ARP, LLDP, `0x88B5` itself, …)
  SHALL NOT be digested and SHALL count as `skip_nonip`. v1 SHALL NOT
  walk IPv6 extension headers: the next-header field is treated as the
  L4 protocol; if it is not TCP/UDP/ICMPv6, flag bit4 is set and the
  bytes are covered as payload per MR10.
- **MR12 (record flags).** Each digest record carries a u16 flags
  field: bit0 ipv4, bit1 ipv6, bit2 tcp, bit3 udp, bit4 other-L4,
  bit5 ip-options-zeroed, bit6 truncated (frame shorter than the IP
  total-length claim, or ended before the IP header / L4 checksum
  window completed; digest covers only the bytes present).

**Caveats (informative — what the digest can and cannot claim).**
The mask set makes the digest invariant across routers that rewrite
TTL/hop-limit, DSCP/ECN, fragment bookkeeping, and checksums — the
RFC 4302 transit model. It is deliberately NOT invariant across:

- **NAT/NAPT.** Addresses and ports are covered (they are identity
  under the AH model), so any NAT hop changes the digest. Matching
  digests across a NAT boundary is out of scope, not a defect.
- **Fragmentation and reassembly.** Digests are strictly per-packet.
  An in-path fragmenter or reassembler changes packet boundaries, and
  a fragmented datagram's N digests bear no relation to the
  unfragmented datagram's digest. Two observation points must see the
  same packet boundaries for digests to compare. Note also that
  zeroing bytes 6–7 (MR8) means two IPv4 fragments differing only in
  fragment offset digest identically when their payload bytes match.

### 4.3 Algorithm and keying

- **MR13 (SipHash-2-4, reference-exact).** The digest SHALL be
  SipHash-2-4: 128-bit key, 64-bit tag, 2 compression rounds, 4
  finalization rounds. Chosen because it is the standard fast keyed
  PRF for short inputs — small in gates (four 64-bit lanes,
  add/rotate/xor, no S-boxes, no multipliers), byte-serial-friendly,
  and published with reference vectors (MF7). Those vectors MUST pass
  in both the RTL testbench and the Python golden model before any
  other AC clause is evaluated. Byte-serial absorption (1 B/cycle at
  250 MHz) is sufficient per MF5.
- **MR14 (key load and commit).** `MAC_KEY_LOAD` (§4.4) carries a
  `key_id` and a 16-byte key: bytes 0–7 are SipHash k0
  little-endian, bytes 8–15 are k1 little-endian (the SipHash
  reference convention). Commit is acknowledged by `MAC_KEY_ACK`
  carrying `keycheck` = SipHash-2-4 under the just-committed key of
  the exact 16-byte ASCII string `PYROMACKEYCHECK1`, so the host
  proves the device holds the key it thinks it loaded without the key
  ever traveling back.
- **MR15 (fail-closed keying).** Until a key is committed, P1 SHALL
  digest NOTHING. Wire frames dispatched to it while keyless count as
  `skip_nokey` and produce no record. There is no default key, no
  all-zeros fallback, and no digest-without-key diagnostic mode.
  Exactly one key is active at a time; a new commit replaces the old
  atomically at a packet boundary, and `key_id` in every report names
  the key that produced every record in that report.

### 4.4 R78 protocol additions (seven kinds, additive per R78.4)

All seven kinds are ADDITIVE in the R78.4 sense: devices that do not
implement them drop the frames silently, and no existing kind changes
meaning. Header rules are MF2's, unchanged: 14-byte header, flags
MUST be 0, frames padded to 60 B, multi-byte header fields BE.
Payload integers are BE except inside digest records, which are LE
(`<IHHQ`) to match the R78.7 little-endian match-entry precedent.
Normative home on adoption: a PYRO R78 MINOR amendment (§8).

- **MR16 (`0x0E MAC_REPORT`, device→host, UNSOLICITED).** Header
  `seq` = the autonomous `wire_seq` of the FIRST record in the frame
  (reports have no request to echo). Payload:

  ```
  off  size  field    enc     meaning
  0    2     count    u16 BE  records in this frame (1..64)
  2    2     status   u16 BE  bit2 wire-origin, ALWAYS set
                              bit0 record loss occurred since
                                   the last report
  4    4     key_id   u32 BE  key for every record below
  8    16*n  records  n = count, layout below
  ```

  Each 16-byte record is little-endian `<IHHQ`:

  ```
  off  size  field     enc     meaning
  0    4     wire_seq  u32 LE  wrapper-stamped wire sequence
  4    2     pkt_len   u16 LE  frame length per tuser size
  6    2     flags     u16 LE  record flags (MR12)
  8    8     digest    u64 LE  SipHash-2-4 tag
  ```

  Max 64 records per frame (8 + 64·16 = 1,032 B payload).
- **MR17 (report flush and best-effort delivery).** The wrapper SHALL
  flush a `MAC_REPORT` when 64 records are pending OR when the flush
  timer — 2^18 axis_aclk cycles (~1.05 ms at 250 MHz) — expires with
  ≥ 1 record pending. Delivery is BEST-EFFORT: if the pending buffer
  overflows because the report path is stalled, records are dropped,
  counted in `records_lost`, and the next report sets status bit0.
  This is the deliberate asymmetry of the design: reports are lossy
  with honest accounting, while every control reply in this section
  (`MAC_KEY_ACK`, `SCHED_ACK`, `MAC_STAT_REPLY`) is lockstep
  request/reply per R78.11 and echoes the request's `seq`.
- **MR18 (`0x0F MAC_KEY_LOAD`, host→device).** Payload:

  ```
  off  size  field   enc      meaning
  0    4     key_id  u32 BE   host-chosen key name
  4    16    key     bytes    0-7 k0 LE, 8-15 k1 LE (MR14)
  ```
- **MR19 (`0x10 MAC_KEY_ACK`, device→host, echoes seq).** Payload:

  ```
  off  size  field     enc     meaning
  0    4     key_id    u32 BE  the just-committed key
  4    8     keycheck  u64 BE  SipHash-2-4("PYROMACKEYCHECK1")
                               under the committed key (MR14)
  ```

  Note `keycheck` is BE (a control-plane field) while record digests
  are LE (MR16) — deliberate, and the golden model MUST encode both.
- **MR20 (`0x11 SCHED_SET`, host→device).** Payload:

  ```
  off  size  field    enc     meaning
  0    1     mode     u8      0 P0-only, 1 P1-only, 2 RR
  1    1     resv     u8      MUST be 0
  2    2     quantum  u16 BE  wire frames per program per
                              turn, >= 1
  ```

  Semantics and validation per MR4/MR5.
- **MR21 (`0x12 SCHED_ACK`, device→host, echoes seq).** Payload:

  ```
  off  size  field    enc     meaning
  0    1     mode     u8      active mode after the request
  1    1     resv     u8      0
  2    2     quantum  u16 BE  active quantum
  4    4     status   u32 BE  0 ok; 1 bad mode/quantum ->
                              settings unchanged
  ```
- **MR22 (`0x13 MAC_STAT_REQUEST` / `0x14 MAC_STAT_REPLY`).**
  `MAC_STAT_REQUEST` has an empty payload. `MAC_STAT_REPLY` echoes
  `seq`; payload is six u32 BE in this order:

  ```
  off  size  field         meaning
  0    4     seen          wire frames dispatched to P1
  4    4     digested      records produced
  8    4     skip_nonip    non-IP frames skipped (MR11)
  12   4     skip_nokey    frames seen while keyless (MR15)
  16   4     reports_sent  MAC_REPORT frames emitted
  20   4     records_lost  records dropped, best-effort path
  ```

- **MR23 (zero-slack counter invariant).** At every quiescent point,
  `seen == digested + skip_nonip + skip_nokey` SHALL hold EXACTLY.
  Every wire frame P1 receives is accounted to exactly one bucket; a
  slack of even one frame is a defect (the PYRO R71 discipline —
  a silently unaccounted frame is a silent pass). `records_lost` sits
  outside the identity: it counts produced-then-dropped records, a
  subset of `digested`.

### 4.5 Engine ABI and wrapper split

- **MR24 (one-ABI port list).** `pyro_mac_engine_top` SHALL present
  the exact `pyro_circuit` port list (MF3): clk, rst_n,
  csr_addr[15:0], csr_wdata[31:0], csr_write, csr_rdata[31:0],
  in_valid, in_data[7:0], in_last, in_ready, res_wr, res_start[63:0],
  res_end[63:0], res_pattern_id[31:0], res_flags[31:0]. Result
  mapping, one `res_wr` pulse per digested frame:
  - `res_start` = digest[63:0]
  - `res_end[15:0]` = pkt_len (upper bits 0)
  - `res_pattern_id` = record flags (MR12)
  - `res_flags` = key_id

  Skipped frames produce NO `res_wr`. The WRAPPER stamps `wire_seq`
  and batches records into `MAC_REPORT` frames (MR17); the engine
  never sees sequence numbers or frame composition.
- **MR25 (engine CSR map).** The existing map ends at `0x0084`; P1
  CSRs start at `0x0090`:

  ```
  0x0090-0x009C  KEY0-KEY3     key words, LE word order
                               (KEY0 = key bytes 0-3)
  0x00A0         KEYCTRL       write bit0 commit,
                               write bit1 clear/invalidate
  0x00A4         KEY_ID
  0x00A8/0x00AC  KEYCHECK_LO/HI  valid after commit
  0x00B0         MACSTAT       bit0 key_valid
  0x00B4         SEEN
  0x00B8         DIGESTED
  0x00BC         SKIP_NONIP
  0x00C0         SKIP_NOKEY
  ```

  CSR reads SHALL be registered like the overlay engine's (data valid
  at N+2); the wrapper's two-cycle address hold tolerates either
  regime, and matching P0's keeps one timing story per child.

### 4.6 Verification and honesty

- **MR26 (golden model is authoritative).** A Python golden model
  (parse → mask → SipHash-2-4) SHALL exist, pass the MF7 vectors, and
  define correctness for every digest the RTL produces. Any RTL/model
  digest disagreement is an RTL defect until proven otherwise.
- **MR27 (SKIP-honesty inherited).** Every on-hardware clause in §7
  inherits SNORT-PF SR18 / PYRO R71/R83 verbatim: gated on
  `device_usable`, canonical SKIP strings, a SKIP is never a PASS, no
  simulated PASS ever.

---

## 5. SipHash-2-4 choice (informative)

Alternatives priced and rejected for v1:

- **CRC/xxHash (unkeyed):** no key means no attribution and no
  tamper-evidence — fails the experiment's "keyed digest" premise.
- **HMAC-SHA-256:** cryptographically stronger, but the compression
  function costs far more fabric than four 64-bit ARX lanes, and a
  64-bit transit-attestation tag does not need SHA's margin.
- **Poly1305 / GHASH:** one-time-key or multiplier-heavy; wrong shape
  for a long-lived key over many packets on LUT fabric.

SipHash-2-4 is the published sweet spot: keyed, 64-bit tag, designed
for exactly this input size class, reference vectors in the paper
(MF7), and byte-serial absorption matches the existing 1 B/cycle
engine feed with 8× headroom over the wire rate (MF5). The 64-bit tag
is a collision/forgery bound of 2^-64 per packet — adequate for
measurement attestation, not a signature (see §6).

---

## 6. Non-goals

- **Line-rate coverage.** The per-engine wire bound is MF1's
  ~30.5 MB/s; frames beyond it are dropped-and-counted upstream. Same
  §9 posture as SNORT-PF: banking is a future event, not this spec.
- **Full SNORT wire completeness while scheduling.** Dispatch divides
  the stream (§3 property 3): under mode 2, P0 samples the wire. No
  nomination-completeness claim over wire traffic is made in any mode
  except mode 0. Host-fed `MATCH_REQUEST` scanning is unaffected.
- **Security protocol.** This is not IPsec AH: no anti-replay, no key
  negotiation, no SA management, no confidentiality, and a 64-bit tag
  is not a signature. The digest attests transit invariance to a
  cooperating observer holding the same key.
- **Guaranteed MAC_REPORT delivery.** Reports are best-effort with
  loss accounting (MR17). A consumer needing every record must poll
  `MAC_STAT_REPLY` and treat `records_lost` honestly.
- **IPv6 extension-header walking, defragmentation, reassembly.**
  Per MR11 and the §4.2 caveats; v1 is strictly per-frame.
- **Per-flow or multiple simultaneous keys.** One active key
  (MR15); `key_id` exists for rotation bookkeeping, not concurrency.
- **Static-shell changes.** Partial-only (MR1); the wiretap static
  and R80 boundary are consumed as-is.

---

## 7. Acceptance criteria

One phase, M. Every on-hardware clause carries MR27's SKIP
discipline.

- **AC-M1 (sim digest equality).** The RTL testbench and the Python
  golden model agree byte-exactly on every digest — after both pass
  the MF7 reference vectors — across the six scenario classes:
  1. IPv4/TCP, plain headers;
  2. IPv4/UDP and IPv4/ICMP;
  3. IPv4 with options (IHL > 5, flag bit5), including nonzero
     fragment-offset variants;
  4. IPv6 TCP/UDP/ICMPv6, including a non-walked extension header
     (flag bit4);
  5. VLAN-tagged: single `0x8100` and stacked `0x88A8`/`0x8100`;
  6. edge cases: unknown L4 (bit4), truncated frames (bit6), non-IP
     skip, and keyless skip — the skips verified by counter, not
     digest.

  LIVE with the toolchain (xsim); no device needed.
- **AC-M2 (link and budget).** The generated two-program child links
  against the unmodified wiretap static locked DCP with WNS ≥ 0 and
  `pr_verify` PASS, inside the SF2 RP budget (MR1), with post-route
  utilization recorded. Synthesis clauses LIVE with the toolchain;
  else the R83a SKIP.
- **AC-M3 (on silicon, both programs live).** With the child loaded
  (`device_usable`, else SR18-style SKIP): a `MAC_KEY_LOAD` commit
  returns the correct `keycheck`; `SCHED_SET` mode 2 acknowledged;
  under live wire traffic the `MAC_STAT_REPLY` zero-slack invariant
  (MR23) holds EXACTLY while P0 SNORT nominations still flow
  (wire `MATCH_REPLY`s with status bit2 and valid epoch), proving
  both programs are serving the wire from one partial with no
  reconfiguration between observations.

---

## 8. Change control

This document follows PYRO §13 verbatim: SemVer, amendments recorded
in the changelog, agents derive work from specs, not from each other.
Additionally:

- This spec MUST NOT modify PYRO or SNORT-PF obligations. The §4.4
  frame kinds become normative in PYRO's R78 by a PYRO MINOR
  amendment at adoption time; until then they are draft layouts.
- The §4.2 mask sets are the digest-compatibility surface: any change
  to a masked or covered byte is at least a MINOR bump here (every
  deployed golden model and key holder is affected).
- **Adoption gate:** version 0.x is a draft; the owner adopts by
  bumping to 1.0.0 with an adoption note. Until then, no phase is
  authorized.

---

## 9. Changelog

- **0.1.0** (2026-08-06) — Initial draft for owner review. The
  co-resident two-program experiment (P0 = A5 SNORT overlay
  unchanged, P1 = SipHash-2-4 wire-packet MAC digest), run-time
  scheduling with the JTAG ban (MR2), packet-atomic RR over wire
  frames only (MR3/MR4), the RFC 4302 mask sets with NAT and
  fragmentation caveats (§4.2), fail-closed keying (MR15), seven
  additive R78 kinds `0x0E`–`0x14` with byte-exact layouts (§4.4),
  the zero-slack counter invariant (MR23), best-effort reports vs
  lockstep control (MR17), and AC-M1..M3. Not adopted.
