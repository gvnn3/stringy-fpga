# Spec amendments for owner review — Phase S3 slate

- **Target spec:** `specs/snort-rule-offload.md` (currently **v1.0.2**)
- **Status:** PROPOSED — drafted during the S3 build-out (2026-07-28);
  the code conservatively implements the proposed semantics where they are
  strictly narrowing (never a silent widening of a spec claim).
- **Author:** coder, on evidence from the `phase2-snort` working tree
  (AC-S3-2 implementation + the AC-S2-3/AC-S3-2 differential runs)

Four amendments. **Each is severable.** None renumbers a requirement or
touches a frozen invariant. A1 and A2 are the load-bearing pair: they
record what AC-S3-2's implementation *had to decide* to keep SR3's
completeness absolute, and the measured defect that forced one of those
decisions.

| # | Target | Kind | SemVer | One line |
|---|--------|------|--------|----------|
| **A1** | SR12 | generalization of a derived constant | MINOR | Overlap tail = max **floating lowered span** − 1 (was max anchor − 1); recorded per group in the manifest (`max_tail_span`) |
| **A2** | SR3 | admission conditions made normative | MINOR | Chain windows only in RAW buffers; `\A` offset/depth prefixes only for PDU-aligned rules (measured sid-509 miss); span cap 384; gap bound 255 |
| **A3** | SR6 | informative count corrected | PATCH | "~16 groups" → measured **21 groups over 8 classes** (literal-class coalescing; raw-token keying would give 190) |
| **A4** | SR11 (+R78.7 inheritance) | daemon obligation added | MINOR | On a buffer-aligned request that overflows, the daemon MUST nominate every `\A` slot's rules (the resume trim cannot recover them) |

---

## Amendment A1 — SR12 tail derives from the lowered span

**Current text (SR12):** the overlap tail is "**213 bytes** (max anchor −
1, SF10)" and "derived from the loaded ruleset's max anchor".

**Problem:** AC-S3-2 puts content *chains* in the circuits. A chain match
spans anchor + gaps + trailing fragments — up to 309 bytes on the current
corpus — and a match is only guaranteed visible if it fits inside one
request window. A tail derived from the max *anchor* under-covers every
chain whose span exceeds it: a silent completeness hole (SR3 violation)
that no anchor-only analysis can see.

**Proposed:** the tail is `max floating lowered span − 1`, per group,
recorded in the group manifest as `max_tail_span`/`overlap_tail`.
`\A`-anchored slots contribute **zero** (they can only match at buffer
start, wholly inside the buffer-aligned request by the A2 admission
condition). Measured on the current corpus: per-group tails range 5–308
bytes (largest: `any/1`, one 309-byte chain span); the S2 group's tail is
unchanged at 64.

**Risk:** a pathological ruleset could push the tail toward the chunk
size (re-scan overhead → 100%). Bounded by A2's span cap: tail < 384
against 1,474-byte chunks (≤ 26% overhead), enforced at lowering time.

☐ APPROVE A1 ☐ REJECT A1

## Amendment A2 — SR3 admission conditions (the sid-509 lesson)

SR3 says chains and offset/depth are "lowered to the generator's existing
counter/assert machinery" without stating admission conditions. Three are
required for completeness to survive contact with Snort 3, and one of
them was **demonstrated, not theorized**:

1. **Raw buffers only** (chains, prefixes, fusion). Positional windows
   count bytes in the buffer Snort matches in; for normalized sticky
   buffers those are inspector-normalized bytes (%-decode, dechunk,
   gunzip change offsets), so a window lowered against raw bytes can
   MISS. Normalized-buffer rules keep anchor-only circuits; their
   positional conjuncts stay dropped (`anchor_strip`).
2. **`\A` prefixes only for PDU-aligned rules** — `udp`/`icmp`/`ip` with
   no `service` option. *Evidence:* sid 509 (`depth 36`, service:http,
   no buffer selector) — Snort 3 alerts on the anchor sitting deep in a
   POST **body**, far past raw stream offset 36: a raw-cursor depth binds
   to the current **PDU section** of an inspected flow, not to stream
   offset 0. The AC-S2-3 differential caught the resulting miss on
   2026-07-28 (a raw-anchor completeness diff, the absolute gate). TCP
   offset/depth therefore stays a dropped conjunct, pinned by test.
3. **Bounds:** gap windows are `[max(0,distance), distance+within]`
   (a superset of either reading of `within`), rejected — never
   narrowed — above `MAX_LOWER_GAP = 255` (SF6's MAX_REPEAT); floating
   spans capped at `SPAN_CAP = 384` (trim conjuncts to fit, A1's tail
   bound); pcre fuses only clean (SF13), `R`-flagged, cursor-anchored
   (`^`/`A`), bounded-span bodies in raw buffers.

Corpus effect: 245 chain rules, 91 `\A` rules, 36 fusions → 181 lowered
slots; every other rule keeps the S1/S2-proven anchor-only circuit
byte-for-byte.

☐ APPROVE A2 ☐ REJECT A2

## Amendment A3 — SR6's group count, corrected informatively

SR6 says "→ **~16 groups** for the current corpus". Measured: **21 groups
over 8 port classes** under the implemented literal-class coalescing
(every non-variable port token → one `literal` class); keying on raw port
tokens would give 180 classes / 190 groups, almost all tiny. No normative
change — GROUP_MAX and the packing rules stand — just the count.

☐ APPROVE A3 ☐ REJECT A3

## Amendment A4 — the `\A`-slot overflow obligation (SR11)

R78.7's resume is host-side: the wrapper never reads `start_off`, so the
host re-sends the **trimmed** corpus (S2 protocol note). Trimming
re-anchors `\A`: a `\A` slot's window in the truncated remainder of an
overflowed ring is **unrecoverable** by resume. When a buffer-aligned
request (`tail_len == 0`) returns OVF, the daemon SHALL nominate every
`\A` slot's rules for that flow (a sound over-approximation, counted in
SR19 as `ovf.anchored_floods`). Groups currently hold up to 59 `\A`
slots (`literal/3`) against `out_cap = 61`; the flood rule makes the
bound irrelevant to completeness.

☐ APPROVE A4 ☐ REJECT A4

---

If all four are adopted the spec goes to **v1.1.0** (MINOR: added
admission conditions and a daemon obligation; no AC renumbering; the S2
acceptance surface is untouched — `$HTTP_PORTS/0` is automaton-identical
before and after AC-S3-2, its 6 offset/depth rules being tcp/service and
therefore A2-inadmissible).

---

## Open finding OF-1 — the two marginal static boundary flops (evidence, no text proposed)

Six of 21 group links (2026-07-29 sweep) missed R73a.1 by −0.005…−0.120
ns; **every** violating path ends (or starts) at one of two flops in the
locked static — `qdma…c2h_slice…axis_tlast_reg[0]/D` or
`qdma…h2c_slice…axis_tdata_reg[1][71]/CE` — with 79–92% of the path in
routing (one case inter-SLR, +0.200 ns compensation). All six closed
with implementation-strategy escalation (phys_opt → directives →
`SSI_SpreadLogic_high`), now driver flags; but the margin is structural:
every future partial (weekly diffs, S4) rolls the same dice. If the
lottery tax grows, the fix is a static rebuild that registers the slice
boundary (an R80-adjacent change: +1 cycle on the AXIS path, all
identities roll via shell_version). Recorded for the owner; no amendment
proposed while the strategy flags suffice.
