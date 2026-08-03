# Spec amendments for owner review — P2d slate (char-dev data plane)

- **Target spec:** `specs/python-regex-offload.md` (was **v2.6.0**, 2026-07-17)
- **Status:** **ADOPTED into v2.7.0** — both amendments B1–B2 APPROVED by the
  owner (2026-07-20) and applied to the spec. The AC-3-3 R1/R2 hardware PASS
  (1.62 GiB/s, W=64, zero loss, 2026-07-20) is the normative record.
- **Date:** 2026-07-19
- **Author:** drafted on evidence from the `phase1-pyro` working tree, the
  19 Jul jumbo-on-silicon measurements (docs/notebook.md #19-jul-2026-144310),
  and an RTL audit of the open-nic qdma_subsystem (see B1.5).

Two amendments are proposed. **Each is severable.** Neither renumbers an
existing requirement, changes an AC number, or touches a frozen invariant
(C ABI 2.0.0, `PYROART1`, `SHELL_VERSION 0x0A000001`, `PYRO_SHELL_SPEC16
0x0202`, the R78 wire format itself).

- **B1**
  - Target: F5, R68, R83, R86.7 (+§10.1 P2)
  - Kind: P2 transport binding made concrete
  - SemVer if adopted alone: MINOR
  - One line: The P2 performance transport is a QDMA ST char-dev carrying the
    **same eth-framed R78 AXIS payloads**; new `PYRO_QDMA_CHARDEV` knob (no-
    default, fail-closed); char-dev-mode R83 transport gate
- **B2**
  - Target: AC-2-5, AC-3-3 (R1 hardware clause)
  - Kind: measurement shape made decidable
  - SemVer if adopted alone: MINOR
  - One line: The R1 floor binds on **windowed aggregate wall-clock throughput
    with zero loss** (the R59/P2a discipline), not on sequential per-frame
    RTT; sequential RTT stays informative

Adopting either bumps the spec to **v2.7.0** (MINOR: added configuration and
measurement protocol; no interface/AC break).

---

## Amendment B1 — P2 performance transport binding (char-dev)

### B1.1 Why now

The jumbo rung (P2c, on silicon 2026-07-19) measured **567.1 MiB/s** pipelined
at W=32, zero loss — 4.66× the 1518-bound plateau but still under the R1
floor. The fixed per-frame cost decomposes as ~6.8 µs kernel/driver floor
+ ~4 µs Python loop; the child scan (~5.3 µs at 9568 B) is additive under the
serializing harness. AF_PACKET cannot cross the floor at the AC-3-3
measurement shape, and AC-2-5/AC-3-3 (v2.5.1) already name QDMA char-devs as
the P2 prerequisite. This amendment makes that transport **specified** rather
than implied.

### B1.2 The binding (normative text proposed)

```
- **R68 (addition).** `PYRO_QDMA_CHARDEV` — the QDMA ST char-dev node
  (e.g. `/dev/qdma02000-ST-0`) for the P2 performance transport. **No spec
  default; fail-closed** (same rule as PYRO_DEVICE_IFACE, v2.5.0): the node
  exists only while the operator has bound the PF to the qdma-pf driver, so
  an unset knob selects the R78 raw-Ethernet control transport and nothing
  is guessed or scanned (R70).

- **R78.12 (transport equivalence — normative).** Both transports carry the
  IDENTICAL byte stream: 14-byte L2 header + R78 frame, zero-padded to the
  60-byte L2 minimum by the sender. The card parses AXIS payload bytes and
  cannot distinguish the host driver. One `write()` on the char-dev is one
  H2C ST packet (tlast at end); C2H replies are re-framed host-side by the
  R78.3 `length` field (a char-dev read has no datagram boundary).

- **R83 (char-dev-mode transport clause).** With `PYRO_QDMA_CHARDEV`
  configured, the transport gate is file accessibility of the node (an fd
  open needs no CAP_NET_RAW); the canonical unmet reason is
  `transport: QDMA char-dev not accessible`. The configured char-dev takes
  precedence over the iface: the PF is single and driver-bound exclusively,
  so the netdev does not exist while the char-dev does.
```

### B1.3 Operational model (informative)

The shell exposes **one PF** (0000:02:00.0). `onic` (netdev, control) and
`qdma-pf` (char-devs, data) bind it mutually exclusively;
`scripts/pyro_dataplane_swap.sh {data|control}` swaps them. In data mode the
R78 control protocol (probe/ID, MATCH, PERF) rides the ST queues — transport
equivalence (R78.12) makes this a null spec change on the wire.

### B1.4 F5 update

F5 currently states char-devs are absent. Adopting B1 rewrites F5 to name the
enablement recipe (dma_ip_drivers 2024.1, `qdma-pf.ko` + `dma-ctl`, built
2026-07-19 for 6.8.0-136-generic) and the swap script as the operator action
that makes them present. F5 remains a **fact about the host**, re-checked at
probe time via the R83 clause, never assumed.

### B1.5 Shell-register caveat the recipe must own (evidence)

The open-nic `qdma_subsystem` gates H2C on a per-function qid window:
`qdma_subsystem_function.sv:185-194` holds `tready` low unless
`qid ∈ [q_base, q_base+num_q)`, and the window register (`QCONF`, shell BAR2
`0x1000 + 0x1000*func`) **resets to num_q=0** — all H2C blocked. The onic
driver programs it (`onic_hardware.c:263-266`); the generic qdma-pf driver
does not know shell registers exist. The swap script therefore writes
`QCONF(0) = (qbase<<16)|num_q` after queue start. C2H needs nothing: the
shell emits standard 8-byte ST completions (`qdma_subsystem_c2h.sv:281-303`)
the generic driver parses, and the all-zero RSS indirection table steers
every reply to `q_base`.

---

## Amendment B2 — R1 hardware-clause measurement shape

### B2.1 The defect

AC-3-3's hardware clause today streams S_MIN sequentially — one MATCH
round-trip at a time — so the statistic it produces is **per-frame RTT**, not
scan throughput. R1 is a *throughput* floor ("sustained scan throughput of
resident circuits"); a sequential shape binds it to transport+child latency
instead. At the jumbo bound a sequential 9568 B round-trip must complete in
≤ 8.9 µs to clear 1 GiB/s — a latency requirement R1 never states. Meanwhile
the spec already blessed the windowed discipline: the P2a/R59 pipelined
measurement (v2.5.1, "the pipelining measurement that reframed P2") is how
every silicon number since 15 Jul has been taken.

### B2.2 Proposed clause (normative text)

```
The R1/R2 hardware demonstration measures **aggregate wall-clock scan
throughput with a W-frame in-flight window** (the R59/P2a credit-loop
discipline): total corpus bytes / wall seconds, streamed through the resident
circuit over the P2 performance transport, with **every frame accounted**
(loss => the run is invalid, never averaged over). W is reported. W=1
(sequential) RTT MAY be reported as informative latency and MUST NOT be used
to claim or deny the R1 floor. The floor and target constants are unchanged
(1 GiB/s floor, 5 GiB/s target).
```

### B2.3 Consequence for the AC test

`test_r1_r2_hardware_win_regime_requires_device` replaces its sequential
`_match_roundtrip` stream with the windowed credit loop (same shape as
`scripts/pyro_pipeline_bench.py`, honest loss accounting), keeps the
`p2_qdma_chardevs` predicate from v2.5.1 verbatim, and keeps every honest-SKIP
disposition. No AC is renumbered; the clause becomes decidable for the
quantity R1 actually bounds.

---

## Recommended adoption

Adopt both. B1 without B2 leaves the R1 clause latency-bound on a transport
whose whole point is throughput; B2 without B1 has no performance transport
to measure on. Together they arm the last unmet Phase-3 clause (AC-3-3
R1/R2) with no change to any constant and no reflash.
