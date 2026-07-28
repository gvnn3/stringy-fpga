# Demonstrating the system (state as of 2026-07-28)

What this shows, two tracks sharing one shell:

* **PYRO** (§§2–5): Python `re`-compatible regex matching transparently
  accelerated by a resident pattern-matching circuit on an Alveo U250,
  over two host bindings — a raw-Ethernet control path (`onic` netdev)
  and a QDMA ST char-dev data plane. Reproducible zero-loss throughput
  is **~2.3 GiB/s** (1 queue); multi-queue runs have reached 5.14 GiB/s
  once but are gated by an open EQDMA IP loss issue (§0) and currently
  land at 2.6–3.1 GiB/s on a fresh boot, so the R1 5 GiB/s target is
  not yet certifiable.
* **SNORT-PF** (§8): 256 Snort community rules compiled into ONE
  pattern-set circuit resident in the same dynamic region — the FPGA
  nominates candidate flows, Snort re-verifies. This is the child
  currently on the card (loaded 2026-07-28).

Everything below runs from the repo root on `nf-server06`. The two
privileged scripts (`pyro_dataplane_swap.sh`, `pyro_wedge_recover.sh`) are
NOPASSWD-sudo for this user; nothing else needs root.

## 0. One-time state notes (2026-07-25)

* QSPI holds the **counter-fixed instrumented jumbo shell** (commit
  `46186bf`, `build_timestamp=0x07260427`), activated by the 2026-07-26
  cold cycle and verified on silicon: `lspci -d 10ee: -nn` shows
  `10ee:903f` at `02:00.0` and `h2cstats` reads live counters.
* `.superpowers/pr-builds/pattern_becf73e88b6f1c561308914848c69ab0_x4_partial.bit`
  matches this static (fmax 260.8 MHz) and is the bit to `pyro_hw.py load`.
* **2026-07-28: the resident child is the SNORT-PF 253-slot group**
  (`group_b1a418fc82f8c28708843068f247fc25_full_partial.bit`, §8). To run
  the PYRO demos in §§2–5, load the x4 regex child first (§1 step 3).
* Generator/harness bumped to **2.3.0** on 2026-07-27 (R47b). Partials
  built before the bump — including the x4 child above — are **stale for
  the R47a-checked Python routing path** (§3; the C runtime rejects
  mismatched artifacts with load code 7). Raw control-path `pyro_hw.py`
  demos still answer on them, but for §3/§4 rebuild the child first
  (`pr_build_driver_dpb8_jumbo_x4.py`, ~46 min; env vars as in §8.5).
* Known open issue: the EQDMA5.0 soft IP silently corrupts ~1/1500 H2C
  packets when **2+ TX queues** are active (single-queue is loss-free; see
  `docs/notebook.md` and `docs/amd-support-case-eqdma-h2c-loss.md`). The
  native benchmark carries a retransmit watchdog, so runs complete with
  every frame accounted; `RETX=n` in its output is that mechanism working,
  not a failure. Loss rate grows with accumulated multi-queue traffic —
  a cold boot re-baselines it.

## 1. Bring-up after a (cold) boot

```sh
# 1. Card visible?
lspci -d 10ee: -nn                     # expect 10ee:903f at 02:00.0

# 2. Load the netdev driver + reset the user box (idempotent, safe anytime):
sudo scripts/pyro_wedge_recover.sh     # user reset + QDMA soft reset + onic

# 3. Load a child into the dynamic region over JTAG (~15-40 s; includes
#    automatic in-band recovery). Pick the child for the demo you want:
#    - PYRO regex demos (§§2-5): the x4 frame-parallel child
#    - SNORT-PF demo (§8):       the 253-slot group child
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py load \
    .superpowers/pr-builds/pattern_becf73e88b6f1c561308914848c69ab0_x4_partial.bit
# or:
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py load \
    .superpowers/pr-builds/group_b1a418fc82f8c28708843068f247fc25_full_partial.bit

# 4. Probe — the go/no-go check:
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py probe
# -> device_usable=true — static_shell_id=0x02020000, ...
```

## 2. Control-path demo (raw Ethernet, `onic` netdev)

The resident child answers R78 frames on `ens2`. The loaded pattern is
`abc[a-f]{2}` (datapath 8 B/cycle, 4 cores):

```sh
# One MATCH round-trip — returns candidate windows the host re-verifies:
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py match "xyzabcafxyz"
# -> MATCH_REPLY count=1 ... entry[0]: start=0 end=8 pattern_id=0

# On-chip performance counters for the most recent scan (R78.11):
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py perf
# -> CYCLES=... BYTES=...  util=7.98 B/cyc  ~2 GB/s on-chip per core-column
```

## 3. Transparent Python API (the actual product surface)

`pyro.re` is a drop-in stdlib-`re` superset: short one-shot calls route to
CPython (R3/R29, ≤2 µs added overhead); large corpora with pattern reuse
route to the resident circuit (R51):

```python
import pyro.re as re                       # in .venv-pyro

pat = re.compile(r"abc[a-f]{2}")           # HW-eligible, resident in slot 1
corpus = ("x" * 9500 + "abcaf") * 8000     # ≥ S_min = 64 KiB, reused ≥ 32×
for _ in range(40):
    m = pat.search(corpus)                 # routed per §8; byte-identical
print(m.span(), re.stats())                # re.stats() shows the routing
```

## 4. Data-plane swap and the throughput benchmark

The single PF swaps between `onic` (control) and `qdma-pf` char-devs
(data). Swapping to data mode creates 4 ST queue pairs:

```sh
sudo scripts/pyro_dataplane_swap.sh data      # -> /dev/qdma02000-ST-0..3
sudo scripts/pyro_dataplane_swap.sh status    # show current binding
```

The pipelined MATCH benchmark (native C credit loop, retransmit watchdog,
window sweep W=1..64):

```sh
PYRO_QDMA_CHARDEV=/dev/qdma02000-ST-0 PYRO_BENCH_JUMBO=1 \
    .venv-pyro/bin/python3 scripts/pyro_pipeline_bench.py
```

Reference numbers, x4 child, jumbo. **Demo with 1 queue** — it is
loss-free and reproduces run after run. 4-queue numbers depend on how
much multi-queue traffic the boot has already seen (§0) and degrade
within a session; the one-off 5.14 GiB/s peak of 2026-07-25 has not
reproduced since (best 2026-07-26: 2.66 GiB/s) and should not be quoted
as expected performance.

| Config (measured 2026-07-26, fresh cold boot) | Throughput | Notes |
|--------|-----------|-------|
| 1 queue, W=1 | ~0.40 GiB/s | latency-bound |
| 1 queue, W=8 | **~2.2–2.3 GiB/s** | zero-loss, the reliable demo number |
| 4 queues, W=1 | ~0.31 GiB/s | |
| 4 queues, W=16–32 | ~2.5–2.7 GiB/s | best 4q on a fresh boot; expect RETX≥1 |
| 4 queues, after heavy mq traffic | 0.002–0.9 GiB/s, erratic | degraded state, §0; windows can fail outright |

Env knobs:

* `PYRO_BENCH_JUMBO=1` — 9,556 B frames (jumbo shell only; default 1,504 B)
* `PYRO_BENCH_QUEUES=n` — cap TX fan-out (use `1` for the loss-free baseline)
* `PYRO_BENCH_TOTAL_MB=n` — corpus per window (default 4)
* `RETX=n` in output — frames recovered by the watchdog (§0 known issue)

The B2-shape acceptance measurement (32 MiB minimum, W=64, every frame
accounted, metrics emitted) is AC-3-3:

```sh
PYRO_DEVICE_IFACE=ens2 PYRO_QDMA_CHARDEV=/dev/qdma02000-ST-0 \
    .venv-pyro/bin/pytest tests/acceptance/test_ac3_3_benchmark.py -k hardware -rA
```

## 5. H2C loss instrumentation (new in the flashed shell)

After the cold boot, the shell counts every H2C packet and every packet the
QDMA IP flagged `tuser_err` at the ingest boundary (BAR2 `0x5000`/`0x5110`):

```sh
sudo scripts/pyro_dataplane_swap.sh h2cstats     # before a bench run
# ... run the benchmark ...
sudo scripts/pyro_dataplane_swap.sh h2cstats     # after
# pkts delta should equal frames written (+retx); err delta counts the
# IP-corrupted packets — the direct measurement for the AMD case.
```

On the pre-instrumentation shell this prints `H2C_STATS unavailable`.

**2026-07-26 status: verified on silicon after the cold cycle.** The
counters account for traffic exactly (1q control run: delta = frames+1
probe frame, to the packet). Headline measurement: across ~28k delivered
packets including heavily degraded multi-queue periods, the err counter
stayed **0** — the QDMA IP never asserts `tuser_err`; lost packets simply
never emerge on the AXIS interface. The err delta is therefore expected to
read 0; the *pkts shortfall* vs frames written (+retx, +1 probe) is the
loss measurement. Details in `docs/notebook.md` and the AMD case doc.

## 6. Recovery cheat-sheet

| Symptom | Fix |
|---|---|
| Probe times out / DMA reads all time out (`tm 10000` in dmesg) | `sudo scripts/pyro_wedge_recover.sh` then re-probe |
| After JTAG partial load, child unreachable | (automatic — `load` runs recovery; manual: same script) |
| Bench numbers degrade run-over-run | reset + fresh `data` swap; if still degraded, cold boot (EQDMA state decay, §0) |
| Back to control path | `sudo scripts/pyro_dataplane_swap.sh control` (pulses resets first — safe against the 2026-07-25 panic) |

## 7. Optional: IOMMU domain experiment mode

For the EQDMA investigation the swap script can switch the card's IOMMU
domain at runtime (drivers are unbound and must be re-swapped afterward):

```sh
sudo scripts/pyro_dataplane_swap.sh iommu PYRO_IOMMU_TYPE=DMA       # strict
sudo scripts/pyro_dataplane_swap.sh iommu PYRO_IOMMU_TYPE=identity  # pt-equiv (experiments only)
sudo scripts/pyro_dataplane_swap.sh iommu PYRO_IOMMU_TYPE=DMA-FQ    # default
sudo scripts/pyro_dataplane_swap.sh data                            # rebind + queues
```

## 8. SNORT-PF demo — 256 Snort rules in one circuit (current resident)

State as of 2026-07-28 (Phase S2 complete, spec `snort-rule-offload` v1.0.2):
the resident child is the **$HTTP_PORTS group 0** pattern-set circuit —
256 community rules deduped onto 253 slots, one shared 1 B/cycle harness,
built through the same PR flow as every PYRO child. Identity
`b1a418fc82f8c28708843068f247fc25`, `rp_child_id 0xfc18a4b1`; post-route
10,147 LUTs / 5,681 FFs, fmax 250.44 MHz (12.7 % of the PR budget). The
FPGA is a **candidate-nominating prefilter**: a hit means "this flow may
match rule X — re-verify with Snort", never an alert by itself (R78.7).

### 8.1 Load and probe

```sh
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py load \
    .superpowers/pr-builds/group_b1a418fc82f8c28708843068f247fc25_full_partial.bit
# -> load_partial OK in ~14 s (in-band recovery included)
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py probe
# -> device_usable=true — static_shell_id=0x02020000, ...
```

### 8.2 Nomination round-trips (all verified on silicon 2026-07-28)

```sh
# A malicious-looking URI — one nomination, exact end offset:
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py match \
    "GET /view-source HTTP/1.0"
# -> MATCH_REPLY count=1 ... entry[0]: start=0 end=16 pattern_id=40 flags=0x1

# A different rule family, shared-anchor slot:
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py match \
    "GET /webspirs.cgi?sp.nextform=foo HTTP/1.0"
# -> count=1 ... end=17 pattern_id=85

# Benign traffic — silence:
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py match \
    "GET /index.html HTTP/1.0"
# -> MATCH_REPLY count=0

# R45a on-chip counters for the last scan (dpb=1 group => ~1 B/cyc):
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py perf
# -> CYCLES=46 BYTES=42  util=0.913 B/cyc
```

Reading the reply: only `end` is exact on hardware (the harness emits on
accept; `start` is the chunk start — protocol note 2, docs/notebook.md
2026-07-27 late). The true match start is recovered host-side as
`end - len(anchor[pattern_id])` from the sidecar.

### 8.3 Attribution — pattern_id -> Snort rules (the sidecar)

Anchors dedup, so one slot can nominate several rules:

```sh
python3 -c "
import json
s = json.load(open('.superpowers/pr-builds/group_b1a418fc82f8c28708843068f247fc25_full_snortpf_sidecar.json'))
print('slot 40 ->', s['slots']['40'])   # ['1:848', '1:849']  view-source rules
print('slot 85 ->', s['slots']['85'])   # ['1:900', '1:901']  webspirs.cgi rules
print(s['rp_child_id_low32'])           # 0xfc18a4b1"
```

### 8.4 The acceptance gates, live against the resident child

The AC-S2-3 suite is device-aware: without `PYRO_DEVICE_IFACE` it runs
32 tests + 1 honest skip; with it, the SR18 clause also verifies the
resident `rp_child_id` and its nominations — **33 passed, 0 skipped**
(~2 min, includes real xsim runs of the emitted RTL):

```sh
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 -m pytest \
    tests/acceptance/test_acs2_1_triage_oracle.py \
    tests/acceptance/test_acs2_3_oracle.py -q
```

The suite embeds the two-sided oracle (closed-form nomination reference,
set-equality per slot) and the Snort 3.12.2.0 differential: zero
raw-anchor misses; exactly 2 normalized-buffer misses (the SF11
%-encoding blinding cases, pinned by equality); FP census by SR4 class
pinned by equality (header_predicate 2, case_fold 9, anchor_strip 48,
dropped_conjuncts 5, unclassified 0).

### 8.5 AC-S1-2 end-to-end replay (single-rule child + Snort re-verify)

The S1 demo — one FTP rule (sid 1927, `authorized_keys`, nocase) through
pcap -> nomination -> Snort re-verification. Child at generator 2.3.0:

```sh
# Load the S1 child (replaces the group; reload §8.1 afterwards):
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_hw.py load \
    .superpowers/pr-builds/pattern_95b1d1d193f3dd33db89110a8801fed1_x1_partial.bit

# Nominate from pcaps (positive: mixed-case 'RETR AuthoRized_Keys'):
PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 tests/data/snortpf/ac_s1_2.py \
    .superpowers/pr-builds/pattern_95b1d1d193f3dd33db89110a8801fed1_x1_snortpf_sidecar.json \
    /path/to/s1_positive.pcap /path/to/s1_negative.pcap
# -> positive corpus: 1 nomination  1:1927 offset=0..20
#    negative corpus: 0 nomination(s)
#    AC-S1-2: PASS
# (Regenerate pcaps with tests/data/snortpf/make_s1_pcaps.py <pos> <neg>.)

# Snort re-verifies the nomination (needs -c for the variable table):
grep -h "sid:1927" third_party/snort3-community-rules/snort3-community.rules > /tmp/sid1927.rules
LD_LIBRARY_PATH=/home/gnn/opt/snort3/lib:/home/gnn/opt/snort3/lib64 \
    /home/gnn/opt/snort3/bin/snort -q -c /home/gnn/opt/snort3/etc/snort/snort.lua \
    -R /tmp/sid1927.rules -r /path/to/s1_positive.pcap -A alert_fast
# -> [1:1927:8] "PROTOCOL-FTP authorized_keys" ... ; negative pcap: silence
```

### 8.6 Rebuilding the group artifacts

```sh
env PYRO_VIVADO=/usr/local/cad/2025.2/Vivado \
    PYRO_PR_STATIC_DCP=$PWD/hw/dfx/build/dcp/static_routed_locked.dcp \
    PYRO_PR_REFERENCE_DCP=$PWD/hw/dfx/build/dcp/static_full_config0.dcp \
    .venv-pyro/bin/python3 .superpowers/pr-builds/pr_build_driver_s2_group.py \
    [--n-slots 32]        # prefix PR gate (own identity); omit for full group
# ~46 min (N=32) to ~53 min (N=253); emits partial.bit + manifest + sidecar
```

Corpus inspection without hardware (parse/triage/pack all 4,017 rules):

```sh
.venv-pyro/bin/python3 -m pyro.snort.triage \
    third_party/snort3-community-rules/snort3-community.rules -o /tmp/report.json
.venv-pyro/bin/python3 -c "
from pyro.snort import triage_file, pack_groups
gs = pack_groups(triage_file('third_party/snort3-community-rules/snort3-community.rules'))
print(len(gs), 'groups;', {g.port_class for g in gs})
g = next(g for g in gs if g.port_class=='\$HTTP_PORTS' and g.index==0)
print('AC-S2-2 group:', g.rule_count, 'rules ->', g.n_slots, 'slots')"
```

## 9. S3 — chain lowering, the filter daemon, and the full-build flow

### 9.1 What changed under the hood (AC-S3-2)

Slots are no longer anchor-only: rules with bounded content chains,
PDU-aligned `offset/depth`, or a clean anchored relative pcre compile
richer circuits (181 lowered slots corpus-wide; see
`docs/spec-amendments-s3.md` for the admission rules and the sid-509
story). Inspect any rule's lowering:

```sh
.venv-pyro/bin/python3 - <<'PY'
from pyro.snort import triage, lowering
from pyro.snort.rules import parse_rule
r = parse_rule('alert udp any any -> any 53 (msg:"x"; '
               'content:"abc", depth 16; content:"def", within 8; sid:1;)', 1)
lo = lowering.lower_rule(r, triage.triage_rule(r))
print(lo.pattern, "tail_span:", lo.tail_span, "dropped:", lo.dropped)
# b'(?s:\\A.{0,15}abc.{0,8}def)' tail_span: 0 dropped: ()
PY
```

### 9.2 The filter daemon (device-free replay)

```sh
# Replay any pcap through the real daemon: SR13 classify, SR10 swap,
# SR14 identity, SR15 tripwires, SR12 tails, SR19 stats as JSONL.
.venv-pyro/bin/python3 scripts/pyro_snortpf_daemon.py \
    --pcap /path/to/capture.pcap --tick-interval 0
# stderr: "[daemon] resident -> $HTTP_PORTS/0", one line per nomination;
# stdout: SR19 snapshots (nominations_by_tier, tripwire_hits,
#         unfiltered_seconds, resident identity, swaps, ovf counters)
```

Live tap (control binding must be active; CAP_NET_RAW):

```sh
sudo env PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 \
    scripts/pyro_snortpf_daemon.py --tap --wire --stats-out /tmp/sr19.jsonl
# --wire scans through the resident child over R78 and JTAG-swaps groups
# by port mix (SF20 ~14-45 s blind window, visible in unfiltered_seconds)
```

### 9.3 Building all 21 groups (AC-S3-1, overnight)

```sh
env PYRO_VIVADO=/usr/local/cad/2025.2/Vivado \
    PYRO_PR_STATIC_DCP=$PWD/hw/dfx/build/dcp/static_routed_locked.dcp \
    PYRO_PR_REFERENCE_DCP=$PWD/hw/dfx/build/dcp/static_full_config0.dcp \
    .venv-pyro/bin/python3 .superpowers/pr-builds/pr_build_driver_s3_all_groups.py
# 2 concurrent jobs (~9.4 GB each), SR9 cache-resumable: re-run after a
# crash and only missing groups rebuild. --dry-run shows cache state;
# --only '$HTTP_PORTS/1,any/0' builds a subset. Summary lands in
# .superpowers/pr-builds/s3_build_summary.json
```

### 9.4 The weekly-diff drill (AC-S3-3)

```sh
.venv-pyro/bin/python3 -m pytest tests/acceptance/test_acs3_3_weekly_diff.py -q
# 33-rule diff -> 2 dirty groups, tombstones stable, 19/21 cache hits,
# unfiltered window bounded and SR19-visible
```
