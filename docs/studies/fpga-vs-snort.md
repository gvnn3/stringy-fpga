# Quantifying matching: FPGA overlay engine vs. userspace Snort

Branch: `pyro-quantify`.  Status: designed; results pending.

## 1. Question

For an identical literal pattern set and byte-identical traffic, what
is the measured difference between matching on the FPGA overlay engine
(the A5 table-programmable Aho-Corasick engine on the U250) and
matching in a userspace Snort process on the host CPU?  Four axes:

- **E1 Throughput** — payload bytes per second through the matcher.
- **E2 Switch cost** — time to change the active rule group (FPGA
  table swap vs. Snort process start with rules compile).
- **E3 Host CPU cost** — CPU-seconds consumed per gigabyte scanned.
- **E4 Match parity** — both sides must agree with the Python oracle
  on which patterns appear in which packets.

The experiment quantifies the claim behind the whole project: that an
FPGA overlay managed with OS-style primitives can do the hot matching
work at hardware rates while the host retains scheduling control.  It
also measures what that costs at switch time, which is the quantity
the scheduler actually trades against.

## 2. Materials

**Pattern groups.**  The same five groups the paper mode uses, drawn
from the Snort 3 community ruleset by the SR1/SR2 triage, spanning the
table-size range 1 KB to 127 KB: `$SIP_PORTS/0`, `$FTP_PORTS/0`,
`$ORACLE_PORTS/1`, `literal/3`, `any/0`.  Each group's anchor set is
lowered two ways from the same source:

1. an A5 table via the existing S2 group emitter (FPGA side), and
2. a Snort 2.9 content-only rules file (one
   `alert tcp any any -> any any (content:"..."; sid:N;)` per
   anchor, non-printables hex-escaped), so Snort's fast-pattern
   matcher searches exactly the same literal set.

**Traffic.**  Synthetic pcap corpora from a seeded generator (pure
stdlib; no scapy).  Payload sizes drawn from a fixed mix (64-1460 B);
match densities 0%, 1%, and 10% of packets carrying one embedded
pattern from the active group at a random offset.  Each packet's pcap
timestamp encodes its index (ts_usec = index mod 1e6, ts_sec carries
the rest), so Snort alert timestamps identify packets exactly.  The
generator also writes `payloads.bin` + `manifest.json` so the FPGA
driver scans byte-identical payloads in the same order.

**Snort.**  Snort 2.9.20 (GRE build 82), extracted from Ubuntu debs
into `build/snort-local/` (no root, gitignored), run in pcap
read-back mode (`-r`, `-k none`), search method `ac-q` (full
Aho-Corasick with queued matches — the closest software analog of the
A5 engine).  Rules compile happens at process start, which is Snort's
equivalent of our table load.

**FPGA.**  The A5 overlay engine, resident child on the U250 at
250 MHz, R45a performance counters (64-bit CYCLES/BYTES, per-scan),
table load over the A5 wire protocol with fail-closed CRC commit.

## 3. Method

**E1, FPGA.**  Load the group table; stream each manifest payload
through the scan path; read CYCLES/BYTES after each scan.  Report
engine throughput as `bytes * 250e6 / cycles` (the engine's own rate,
transport excluded) and, separately, end-to-end wall-clock per scan
(transport included).  The engine's FSM floor is 5 cyc/B, so the
ceiling is 50 MB/s per engine at 250 MHz; the point of reporting both
numbers is that neither is the "true" one — the engine rate scales
with replicated engines, the end-to-end rate is what this one-engine
bring-up delivers.

**E1, Snort.**  `snort -q -r corpus.pcap -c group.conf -A fast` under
`getrusage` accounting; parse Snort's own run summary for run time and
packet count.  A run against the same pcap with an empty rules file
isolates decode/stream overhead from search cost.  N=5 repeats per
cell, median reported, min/max kept.

**E2, FPGA.**  Wall-clock of `load_table` per group (the existing
instrumented path; prior fit 13.9 ms + 0.150 ms/KB).

**E2, Snort.**  Wall-clock from exec to ready (rules parsed, AC
automaton built) per group, measured by running with the same pcap
and subtracting the measured steady-state processing time; N=5.
Snort must compile its automaton at startup every time the rule set
changes — that is the software analog of our table write, and it is
the quantity the scheduler pays on every swap.

**E3.**  Snort: (utime+stime) per payload-GB from getrusage.  FPGA:
host CPU consumed by the feeding driver per payload-GB (the engine
itself is offload; the honest host cost is the transport driver).

**E4.**  Referee is the existing Python oracle model.  For every
(group, density) cell: the oracle's per-packet pattern-hit set is
compared against (a) Snort's alert set recovered from fast-alert
timestamps + sids, and (b) the FPGA nomination set per scan.  Snort
must equal the oracle exactly (content-only rules, one alert per sid
per packet).  The FPGA set must be a superset of the oracle
(over-nomination is benign by design, SR5); any oracle hit missing
from either side fails the cell.

## 4. Controls and known asymmetries

Recorded up front so the numbers are read honestly:

- Snort does L2-L4 decode and stream bookkeeping before search; the
  FPGA engine is handed payload bytes.  We therefore report Snort
  both raw and with the empty-ruleset baseline subtracted, and we
  report the FPGA engine rate and end-to-end rate separately.
- The FPGA scan path in this bring-up is host-fed over the A5
  transport (JTAG-era plumbing replaced by the RP data path, but
  still one engine); NIC-wire ingest is future work, so end-to-end
  FPGA numbers are transport-bound, not engine-bound.
- One Snort process, one core, no multiprocessing; one engine, no
  banking.  Both sides scale by replication; we measure the unit.
- Snort 2.9 (apt), not Snort 3: the search engine being measured
  (full AC) is the relevant comparison and the rules are lowered to
  the common subset both parse.
- Match density changes both sides' costs (AC match-queue drain vs.
  FIFO/OVF on the engine); it is a swept variable, not a nuisance.

## 5. Deliverables

- `pyro/quantify/` — `traffic.py` (pcap+manifest generator),
  `snort_lower.py` (anchors → Snort 2 rules), `snort_bench.py`
  (runner/parser), `fpga_bench.py` (device driver), `parity.py`
  (oracle referee).
- `scripts/pyro_quantify.py` — CLI: `gen`, `snort`, `fpga`,
  `parity`, `figs`, and `software` (everything that runs without
  hardware).
- `scripts/pyro_quantify_figs.py` — paper-style figures:
  throughput vs. group size (both sides), switch time vs. table
  size (both sides), CPU-seconds per GB, parity table.
- Unit tests under `tests/unit/` (generator determinism, lowering
  escapes, alert parsing on fixtures, parity logic on synthetic
  cases).
- Results: `docs/studies/fpga-vs-snort/` (run.json, CSVs, figures)
  and a results section appended to this document.

## 6. Results

Pending.
