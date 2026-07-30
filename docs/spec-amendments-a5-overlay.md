# Amendment A5 (DRAFT) — overlay table-write protocol and the TABLE_ID identity layer

- **Status:** ☑ **APPROVED by the owner, 2026-07-30.** The loadable-table
  slot is open, with the §2–§5 identity layer as its binding precondition:
  no table-write path may ship without content-and-epoch attestation.
  Implementation began the same day (`pyro/overlay/`). PYRO's v2.0.0
  ruling removed the programmable engine and `snort-rule-offload` §9 puts
  "a programmable rule engine or loadable rule/AC-table blob (runtime
  `TABLE_WRITE`-style frame kinds)" explicitly out of scope pending exactly
  this amendment (provisional **A5**, OQ-1). Nothing here is implemented.
- **Date:** 2026-07-30 · **Branch:** `fpga-scheduler-tenants`
- **Evidence base:** `docs/studies/switch-cost-frontier.md` (why),
  `docs/studies/fpga-scheduler-tenants.md` (switch-cost decomposition),
  SF2/SF14 (memory), P2d (transport).

## 0. Why this, and why now

The frontier study measured that scheduling pays only while
`switch_cost / phase_length ≲ 0.3`. JTAG partial reconfiguration costs
**13.6 s**, which confines it to phases of a minute or longer — one to four
orders of magnitude away from the project's stated goal of swapping
functionality in response to observed traffic. PR's cost is also
irreducible: 83% is bitstream transfer, it is flat in design size, and a
fixed 2.15 s recovery tax follows every load.

An overlay — a resident engine whose **rule content is written at run time
as data** — pays neither the transfer nor the recovery. A 1.33 MB table
(SF14's 16-byte-capped trie over all 3,896 anchors) over the measured
2.3 GiB/s char-dev path is **0.54 ms**, four orders of magnitude below PR.

The obstacle is not bandwidth. It is that **a loadable table breaks the
identity premise the entire verification architecture rests on**, and that
is what this amendment exists to repair.

## 1. The identity problem, precisely

Today `rp_child_id` is the low-32 of a hash over the group's canonical rule
bytes ⊕ versions. The bitstream **is** the rules, so SR14's check —
"resident child id equals the id the sidecar expects" — proves the resident
circuit recognises exactly the rules being attributed.

With an overlay that proof evaporates. The bitstream says *"I am the
literal-matcher engine v1.2"* and says **nothing** about which rules are
loaded. `pattern_id 40` in a `MATCH_REPLY` means whatever the currently
loaded table says it means. SR14 would still pass, and would be verifying
the wrong thing — the most dangerous possible failure, because it looks
green.

Two consequences must both be handled:

1. **Content identity.** Something must attest *which rules* are resident.
2. **Temporal identity.** A reply produced before a table swap must never
   be attributed with the sidecar from after it.

## 2. Design: two-level identity

| layer | attests | mechanism | lifetime |
|---|---|---|---|
| `CIRC_ID` (exists, R47a) | which **engine** | 128-bit hash baked into CIRC_ID0..3 | per bitstream |
| **`TABLE_ID`** (new) | which **rules** | CRC-32C computed on-device over the received table image | per commit |
| **`EPOCH`** (new) | **when** | monotonic counter, incremented on commit, tagged into every reply | per commit |

The engine bitstream becomes generic and near-immutable; the table becomes
the thing that changes and the thing that must be attested.

### 2.1 What is hashed, and by whom

The chain must be auditable end to end:

```
rule set  --(table compiler)-->  table image  --(hash)-->  TABLE_ID
   |                                 |                        |
   +-- canonical bytes in manifest --+                        |
                                     device recomputes over received bytes
```

- **The device** computes CRC-32C incrementally over the table image
  exactly as received, and exposes it as `TABLE_ID`. It can only attest to
  bytes, because bytes are all it sees.
- **The host** records, in the table manifest: the rule set's canonical
  bytes (SR6/SR9 form), the compiler version, the table format version, and
  a **128-bit SHA-256-truncated hash** of the same image — the identity of
  record, in the same discipline R47a already uses for bitstreams.
- Verification is `device CRC == host's CRC of the image it intended to
  send`; the strong hash ties that image to a rule set for audit.

**Threat model, stated plainly.** CRC-32C is *integrity*-grade, not
adversary-grade: it catches torn writes, wrong tables, truncation and
corruption with probability `1 − 2⁻³²`, which is what SR14 needs, since
SR14 exists to prevent *accidental* misattribution. It does **not**
withstand an adversary crafting a collision. A cryptographic hash in fabric
is expensive and buys nothing against a threat this system does not face —
an attacker who can write arbitrary tables to the device has already
defeated everything upstream. The 128-bit host-side hash remains the
identity of record, and this asymmetry is deliberate rather than an
oversight.

### 2.2 Atomicity: shadow, commit, epoch

Writes land in a **shadow** region while the engine keeps matching against
the **active** one. `TABLE_COMMIT` flips them at a frame boundary and
increments `EPOCH`. Every `MATCH_REPLY` carries the epoch it was produced
under, so the host attributes with the sidecar for *that* epoch and a reply
can never straddle a swap.

Memory is the constraint, and it forces a choice:

| option | memory | blind window | fits SF2's 2.25 MB URAM? |
|---|---|---|---|
| full double-buffer, 16 B cap | 2 × 1.33 = 2.66 MB | **zero** | **no** (needs URAM+BRAM) |
| full double-buffer, 8 B cap | 2 × 0.68 = 1.36 MB | zero | yes |
| **single region + quiesce** | 1.33 MB | **~0.6–1.2 ms** | yes |

**Recommendation: start single-region with quiesce.** The blind window is
~0.54 ms of table write plus a verify pass — four orders of magnitude below
PR's 13.6 s, and inside the frontier for any phase length above **2 ms**
(`0.6 ms / 0.3`). Paying 2× URAM to remove a window that is already negligible is
the wrong trade at this stage. Epoch tagging is specified regardless: it
costs a few bits, it guards against protocol bugs during quiesce, and it is
what makes the banked mode a drop-in later.

## 3. Wire protocol (R78 extension)

Kinds `0x01`–`0x07` are taken; these are additive and start at `0x08`.
Header format is unchanged (`>BBBBHIHH`, 14 bytes).

| kind | name | direction |
|---|---|---|
| `0x08` | `TABLE_BEGIN` | host → device |
| `0x09` | `TABLE_DATA` | host → device |
| `0x0A` | `TABLE_COMMIT` | host → device |
| `0x0B` | `TABLE_STATUS_REQUEST` | host → device |
| `0x0C` | `TABLE_STATUS_REPLY` | device → host |
| `0x0D` | `TABLE_ABORT` | host → device |

**`TABLE_BEGIN` (32 B payload)** — opens a load and gates compatibility
before a single byte is transferred:

```
u32 table_format_version    u32 engine_id      (must equal resident CIRC_ID low-32)
u64 total_bytes             u32 expected_crc32c (over the FINAL image)
u16 mode                    u16 reserved        (0 = full, 1 = delta-from-active)
u32 n_states                u32 n_patterns      (both must be <= engine capacity)
```

The device rejects on any mismatch — wrong engine, unknown format, capacity
exceeded — exactly as the C runtime rejects a version-mismatched artifact
today (R47b). Rejection is a `STATUS` error, never a partial acceptance.

**`TABLE_DATA` (12 B + payload)** — `u64 offset, u32 length, bytes[]`.
Offset-addressed, therefore **idempotent and restartable**: a lost or
duplicated chunk is re-sent without restarting the transfer, and the same
mechanism gives delta writes for free.

**`TABLE_COMMIT` (8 B)** — `u32 expected_crc32c, u32 flags`. The device
commits **only if** all declared bytes arrived and its computed CRC matches
the host's. Otherwise it refuses, keeps the active table untouched, and
reports the error. On success it flips active↔shadow and returns the new
epoch.

**`TABLE_STATUS_REPLY` (40 B)** — the observability surface:

```
u32 active_table_id   (0 = none valid)      u32 shadow_table_id (0 = invalid)
u32 epoch                                   u32 status_flags / error code
u64 shadow_bytes_received                   u32 engine_id
u32 table_format_version                    u32 capacity_states
u32 capacity_patterns
```

**`MATCH_REPLY` gains the epoch at payload bytes 4–7**, which are presently
reserved (`count` and `status` occupy 0–3; entries start at byte 8). The
change is therefore **additive and layout-compatible** — existing entry
parsing is untouched, and a device that predates this amendment writes
zero, which the host reads as "no epoch, overlay not in use."

### 3.1 CSR additions

Offsets `0x0068`–`0x0080` are free in the R45 map:

```
0x0068 TABLE_ACTIVE_ID     0x006C TABLE_SHADOW_ID
0x0070 TABLE_EPOCH         0x0074 TABLE_STATUS   (valid bits + error)
0x0078 TABLE_BYTES_LO      0x007C TABLE_BYTES_HI
0x0080 TABLE_CAPS          (capacity: states, patterns)
```

## 4. SR14′ — the revised attribution gate

> **SR14′ (identity before attribution, overlay form).** Before attributing
> any nomination the daemon SHALL confirm **all three**: (i) `rp_child_id`
> equals the expected engine identity; (ii) `TABLE_ACTIVE_ID` equals the
> expected table identity; and (iii) the reply's `epoch` equals the epoch of
> the sidecar being used to attribute it. Failure of **any** routes to "no
> resident ruleset" — all traffic unfiltered — never to a nomination.

Same fail-closed disposition SR14 already has, extended over the two new
dimensions. It is strictly stronger than today's check, because today's
cannot detect a content change at all.

## 5. Fail-closed behaviour

| condition | required behaviour |
|---|---|
| after any reset or reconfiguration | `TABLE_ACTIVE_ID = 0`, epoch = 0 → host must reload. Tables are volatile; the device MUST NOT come up holding stale content that reads as valid. |
| transfer interrupted | shadow stays invalid; `TABLE_COMMIT` refused |
| CRC mismatch at commit | refuse, keep active table, report error |
| `TABLE_ID` readback ≠ expected | host routes to unfiltered (SR14′) |
| engine/format/capacity mismatch | reject at `TABLE_BEGIN`, before any transfer |

The `TABLE_ID = 0 means nothing valid` convention mirrors the existing
forced-non-zero `rp_child_id`, so "zero" is unambiguous in both layers.

## 6. Bandwidth, derived from the frontier

The frontier requires `s ≤ 0.3 · P`. With `s ≈ table_bytes / rate + commit`:

| phase length P | max switch | full 1.33 MB table needs | verdict on measured 2.3 GiB/s |
|---|---|---|---|
| 1 s | 300 ms | 4.4 MB/s | trivial |
| 100 ms | 30 ms | 44 MB/s | comfortable |
| 10 ms | 3 ms | 443 MB/s | **within reach** |
| 1 ms | 0.3 ms | 4.4 GB/s | **exceeds single-queue** |

So the overlay serves phases down to roughly **10 ms** with a full table
rewrite, and below that only via **delta writes** — which the
offset-addressed `TABLE_DATA` supports natively, and which is the common
case anyway (a weekly diff or a working-set page-in touches a small
fraction of the table). Multi-queue would raise the ceiling but is blocked
by the open EQDMA silent-loss issue, so it must not be assumed.

**Transport contention is a real constraint.** The char-dev data plane and
the `onic` netdev are mutually exclusive bindings of the single PF, so bulk
table writes and R78 control cannot currently be concurrent. The protocol
is specified transport-agnostically for that reason: control over R78,
bulk over char-dev when available, and R78-only operation when it is not —
**140 frames** for a full table on this jumbo shell (9,568 B payload), or
903 on a standard 1,518 B one.

## 7. What this changes upstream

**The build model inverts, and this is the operational prize.** Today a
rule change is a Vivado build: 21 group bitstreams, ~60 min each, 22.6 h for
a full corpus rebuild, with 6 of 21 needing timing-strategy escalation.
With an overlay:

- **the engine bitstream is keyed by engine version alone** — *one*
  bitstream, rebuilt only when the engine changes;
- **the table is keyed by canonical rule bytes ⊕ compiler ⊕ format** and is
  built host-side in seconds.

SR9's cache splits accordingly into a tiny bitstream cache and a fast table
cache. A weekly ruleset diff stops being a hardware build. This is exactly
the *build-time* argument the A5 study said was real but unmeasured — it
now has a mechanism, though not yet a measurement.

## 8. What is NOT settled

Stated plainly, because the amendment should not be read as more finished
than it is:

- **The engine does not exist.** A table-driven AC matcher is unbuilt, and
  its timing is the open risk: OF-1 found six of 21 groups marginal at
  ~10K LUTs on RM↔static boundary paths, and a memory-heavy design with
  URAM latency at 250 MHz on SLR2 is a harder placement problem, not an
  easier one. Nothing here proves it closes.
- **The 0.54 ms figure is arithmetic**, from a measured transport rate —
  not a measured overlay. Every number in §2.2 and §6 is reproduced by
  `scripts/overlay_sizing_check.py`.
- **Delta-mode CRC cost is unquantified.** Hashing the whole shadow after
  applying a delta costs a full read pass (~0.66 ms at 8 B/cycle); an
  incremental-CRC scheme would avoid it but needs design.
- **Whether `TABLE_ID` should be stronger than CRC-32C** is an owner call,
  not an engineering one — it is a threat-model judgement (§2.1).
- **This amendment does not authorise suppression.** SR17's four gates are
  untouched; nomination-only remains in force.

## 8a. Implementation findings (measured, 2026-07-30)

The engine was built the same day A5 was approved.  Several §2–§6 figures
were estimates; these are the measured replacements, and two of them
change the picture.

### Table sizing — better than SF14 assumed

State counts reproduce SF14 **exactly** at all three anchor caps (11,078 /
21,841 / 38,700), which is a strong independent check on that fact.  The
image, however, is smaller than SF14's ~64 B/state assumption:

| anchor cap | states | image | vs SF2's 2.25 MB URAM |
|---|---|---|---|
| 8 B | 11,078 | 0.61 MB | fits |
| 16 B | 21,841 | 1.18 MB | fits |
| **uncapped** | 38,700 | **2.08 MB** | **fits** |

At 53–55 B/state the **uncapped** trie fits URAM, where SF14 concluded it
did not.  The 16-byte cap is no longer required on memory grounds.

### Memory placement — the bitmap alone is not enough

Sized against SF2's RP envelope (160 BRAM36 + 64 URAM) at the real
39,647-state corpus table:

| array | size | placement |
|---|---|---|
| bitmap | 256b × 39,647 = 1.21 MB | **40 URAM** (276 BRAM36 if block) |
| out_idx | 64b × 39,647 = 0.30 MB | **10 URAM** (69 BRAM36 if block) |
| base / fail / dense / out_flat | — | 108 BRAM36 measured |

**50 of 64 URAM, 108 of 160 BRAM36.**  Moving only the bitmap leaves the
remainder needing 183 BRAM36, over budget — `out_idx` had to move too.
Both are pinned by test so the arithmetic is not rediscovered at synthesis
time.

### Load time — §6's headline, reproduced at real scale

The 1.64 MB `ci` image loads in **172 jumbo frames**; at the measured
2.3 GiB/s char-dev rate that is **0.66 ms** of wire time.  §0's 0.54 ms
was arithmetic on a 1.33 MB estimate; the real table is larger and the
real number is 0.66 ms.  Still four orders of magnitude below PR's 13.6 s,
so the argument is unchanged.

### Precision — the cost of anchor-only matching, measured

AC matches literal anchors, not AC-S3-2's lowered chains, so the overlay
over-approximates.  Measured against the lowered circuits over the AC-S2-3
corpus: **0 extra nominations** on `$HTTP_PORTS/0` and `any/1`, **6 extra**
on `literal/0` (the group that actually carries chains), and **zero
misses** anywhere — SR3 preserved, as required.

### Identity — CRC-32C, and a caught mismatch

§2.1 specifies CRC-32C.  The first host implementation used Python's
`zlib.crc32`, which is the **IEEE** polynomial, not Castagnoli; the RTL
differential caught the disagreement on its first run (device 0xf2edc7f8
vs host 0xe4924a78).  The host now implements CRC-32C directly and the two
agree bit for bit.  This is exactly the class of silent divergence the
two-level identity exists to prevent, and it is mildly reassuring that it
was caught by construction rather than by luck.

### Timing — closed out of context, and a caught false pass

The full-corpus configuration meets 250 MHz.  Getting there took two
fixes and produced one result worth recording as a method note.

An unbounded URAM cascade chained seven URAM288s combinationally and cost
**−1.297 ns**.  Capping it with `cascade_height=2` and re-registering the
outputs into `d_bitmap_q` / `d_oidx_q` fixed it — but the first run of
that fix reported **+0.383 ns and the number was worthless**.  The new
pipeline registers had picked up a second driver, because they were also
being zeroed in the async-reset block.  Synthesis kept the constant and
discarded the real driver, the bitmap read path became dead code, and
`opt_design` deleted all 50 URAMs.  What got timed was a 768-LUT stub.

Two things about that are worth carrying forward.  First, **simulation
cannot catch it**: the reset branch only executes during reset, so xsim
sees one driver, and the differential passed identically before and
after the fix.  Second, the run looked entirely healthy — it exited 0 and
printed a comfortable positive slack.  Only the utilization report gave
it away.  `scripts/overlay_ooc_timing.tcl` now refuses to report a
verdict unless synthesis raised zero critical warnings *and* the routed
netlist still contains its memories, on the principle that a timing
number means nothing without evidence that the thing timed is the thing
intended.

With one driver restored, measured OOC at 250 MHz on `xcu250-figd2104-2L-e`:

| | |
|---|---|
| WNS | **+0.089 to +0.212 ns** (MET) |
| URAM | 50 of 64 |
| BRAM36 | 108 of 160 |
| LUT / FF | 2,364 / 2,209 |
| critical warnings | 0 |

The slack is quoted as a range deliberately.  Every worst-case path in
this design has **0–1 logic levels and 91–98% route delay**, so the
figure moves by more than 0.1 ns between runs that differ only in which
high-fanout register was replicated.  Out of context there is no pblock,
so placement is free to scatter 50 URAMs and 108 BRAMs across three
SLRs; the spread is routing luck, not design quality.  With zero logic
levels on the worst path, **there is nothing left to optimize in the
RTL** — what remains is placement.

### In context — the number that decides

The engine PR-links against the locked static and **meets timing**:

| | |
|---|---|
| fmax | **255.23 MHz** (target 250) |
| WNS | **+0.082 ns** |
| `met_timing` | **True** |
| `pr_verified` | **True** |
| LUT / FF | 8,605 / 2,927 (engine **plus** `rp_wrapper` harness) |
| partial bitstream | 3,646,240 B |
| link time | 2,844 s |

This is the measurement that was missing, and it says the OOC anxiety was
misplaced.  Constraining placement to the `pyro_rp` pblock on SLR2 —
which concentrates the 50 URAMs and 108 BRAM36s instead of letting them
scatter across three SLRs — cost nothing relative to the unconstrained
OOC range of +0.089…+0.212 ns, and the RM↔static boundary paths (OF-1:
5–120 ps on six of 21 group builds) did not push it negative.  A
route-bound design got *better* routes once its placement was bounded.

So the full-corpus overlay engine, with the uncapped 39,647-state trie
resident in URAM, fits the region and runs at rate.

### Still open

Nothing in the timing argument.  What remains before this can be called
silicon-ready is **on-hardware bring-up**: load the partial, write a real
table through the §3 protocol, and confirm `TABLE_ID`/`EPOCH` attestation
and nomination against the model on live traffic.  The bitstream exists
and is verified; it has not yet been on the card.

## 9. Decision requested

☑ **APPROVE A5** (owner, 2026-07-30) — open the loadable-table slot with
the identity layer of §2–§5 as its precondition, so no implementation can
ship a table-write path without content-and-epoch attestation.

☐ **REJECT A5** — the v2.0.0 no-loadable-data-engine ruling stands, and
FPGA functionality scheduling stays confined to minute-scale PR rotation
(`switch-cost-frontier.md`).

☐ **APPROVE with changes:** ................................................
