# Laboratory Notebook — stringy-fpga

Experiments on accelerating string matching and regex operations using the
dynamic (partially reconfigurable) region of the attached FPGA.

**Conventions for this notebook** (these extend the standard /notebook format):

- Entries are kept in **reverse chronological order** — newest experiment first,
  immediately after the Table of Contents.
- Every entry title carries both a **date and a time stamp** in the form
  `DD Mon YYYY HH:MM:SS` recording when the experiment was written up.
- Anchor IDs include the time: `#dd-mon-yyyy-hhmmss`.
- Each entry follows the 5-section format: Hypothesis, How, Observations,
  Data analysis, Ideas for future experiments. Status tags `:complete:` or
  `:in_progress:` follow the title.
- Entries are separated by `---`.

# Table of Contents

1. [EXPERIMENT  5 Jul 2026 02:44:00 PYRO Phase 0 — Software Shim, Classifier, Model](#5-jul-2026-024400) :complete:
2. [EXPERIMENT  4 Jul 2026 07:33:45 FPGA Platform Discovery](#4-jul-2026-073345) :complete:

---

# EXPERIMENT  5 Jul 2026 02:44:00 PYRO Phase 0 — Software Shim, Classifier, Model :complete:

## 1. Hypothesis

Can a pure-Python, transparently-interposable `re` shim (spec
`python-regex-offload`, Phase 0) achieve byte-identical results to CPython
`re` across a spec-derived acceptance suite, with routing overhead ≤ 2 µs,
before any hardware exists?

## 2. How

- **Equipment:** Intel C620 x86_64 server, Ubuntu (Linux 6.8.0-124-generic);
  Xilinx OpenNIC card present but unused (Phase 0 is software-only)
- **Software:** CPython 3.12.3, pytest 9.1.1, GCC 13.3; CPython source added
  as shallow submodule (`third_party/cpython`)
- **Benchmarks:** 412-test acceptance suite (spec-only, independent author);
  219 unit tests; routing-overhead microbenchmark (R3a/R5)

### Key commands

```bash
python3 -m pytest tests/unit/          # implementer suite
python3 -m pytest tests/acceptance/    # independent spec-derived suite
make abi-check                         # frozen C ABI header (AC-0-8)
```

## 3. Observations

| Gate | Result |
|------|--------|
| Unit suite | **219 passed** |
| Acceptance suite | **411 passed, 1 sanctioned skip** (R61 → Phase 1) |
| ABI check | `pyro_abi_version() == 0x00010000` |
| Routing overhead (median) | **0.97 µs** (bound: ≤ 2 µs, R3a/R5) |
| Final review verdict | Ready to merge: **Yes** |

Defects found and fixed during the loop (each with a regression test):
- Per-call `os.environ` reads cost ~1.74 µs, breaking the 2 µs budget →
  spec v1.2.0 (R35a–d cached sampling + `pyro.refresh_env()`).
- Empty-match adjacency (R22): anchored `match()` cannot reconstruct a
  post-empty `finditer` match; `_VerifyError` leaked to callers (R52).
- **Window-truncated `fullmatch` reconstruction silently flipped capture
  groups** for trailing-anchor alternations (`(?P<a>foo)$|(?P<b>foo)` on
  `"foobar"`) — same span, wrong groups; caught by code review, not tests
  (property generator had no anchor atoms).
- Lone-surrogate `str` subjects raised `UnicodeEncodeError` from the model
  path; `bytearray` subjects returned wrong-typed `group(0)` → spec v1.2.1
  (R14a surrogate gate, R51a Phase-0 type/pos/context gates).

## 4. Data analysis

The correctness architecture held: making the Phase-0 model *be* CPython
`re` behind the future §7.3 C-ABI seam means classifier bugs can only
mis-route, never mis-answer — every real defect was at the semantic edges
(zero-width adjacency, anchor context at window boundaries, encoding
corner cases), exactly where a future hardware NFA will also be at risk.
The spec's §13 change-control loop was exercised 4 times (1.0.0 → 1.2.1),
each time converting an implementation discovery into binding spec text
before tests were derived from it. The 1.15× relative fallback bound (old
R3) was proven physically meaningless for a Python-over-C hot path and
replaced by an absolute 2 µs bound (R3a); the measured 0.97 µs leaves
headroom for one more dict lookup, roughly, on this host. Independent
test authorship caught one class of bug (R52 leak via property fuzzing);
adversarial code review caught the one the fuzzer grammar couldn't
generate — both nets were necessary.

## 5. Ideas for future experiments

- Phase 1: C-ABI host runtime + Aho-Corasick engine in the software model;
  raw-Ethernet control-frame format to the onic netdev (needs spec §10.1)
- Validate the FLAG_VERIFIED trust boundary when the real L3 binding lands
  (device must never self-certify windows past re-verification)
- Benchmark stock `re` vs `re2`/`hyperscan` on target corpora to calibrate
  R1/R2 win-regime thresholds before RTL work
- Vivado + open-nic-shell build flow bring-up (P1); QDMA char-dev
  enablement (P2)
- Phase-1 cleanups: single UTF-8 encode on model path; lazy finditer

# EXPERIMENT  4 Jul 2026 07:33:45 FPGA Platform Discovery :complete:

## 1. Hypothesis

What FPGA hardware is installed in this system, what shell/driver does it run,
and can its dynamic region host a regex-matching engine reachable from Python?

## 2. How

- **Equipment:** Intel C620-chipset x86_64 server, Ubuntu (Linux 6.8.0-124-generic)
- **Software:** lspci, lsmod, sysfs inspection (no FPGA vendor tools assumed)
- **Benchmarks:** none — discovery only

### Key commands

```bash
lspci -nn | grep -iE 'xilinx|altera|intel.*fpga'
lspci -k -s af:00.0; lspci -k -s af:00.1
lsmod | grep -iE 'xdma|qdma|onic|xocl|xrt'
ls /sys/class/net/
command -v vivado v++ xbutil
```

## 3. Observations

| Property | Value |
|----------|-------|
| PCIe functions | **af:00.0** (10ee:903f), **af:00.1** (10ee:913f), subsystem 10ee:0007 |
| PCI class | Network controller |
| Kernel driver | **onic** (AMD/Xilinx OpenNIC) |
| Network interfaces | enp175s0f0, enp175s0f1 |
| FPGA char devices | none (`/dev/xdma*`, `/dev/qdma*` absent) |
| Vendor tools on PATH | none (vivado, v++, xbutil all missing) |

## 4. Data analysis

The card runs the **OpenNIC shell** (QDMA-based), which reserves a 250 MHz
user-plugin **dynamic region** for custom RTL — the natural home for a regex
engine. Offload from Python is feasible: patterns compiled to a programmable
automaton loaded into the plugin region, data moved via QDMA (once char-devs
are enabled) or raw Ethernet frames to the NIC function (usable today).
FPGA wins only for large corpora / streaming with pattern reuse; PCIe latency
makes CPU faster for short one-shot matches. Toolchain (Vivado) installation
is a prerequisite for any hardware build. Full design captured in
`specs/python-regex-offload.md` (R1–R61, AC-0-x…AC-3-x).

## 5. Ideas for future experiments

- Install Vivado + OpenNIC build flow; rebuild shell with a stub user plugin
- Enable QDMA char-devs and measure PCIe round-trip latency and DMA bandwidth
- Benchmark CPython `re` vs. `re2`/`hyperscan` on target corpora to set the
  offload break-even thresholds (AC targets in the spec)
- Prototype Phase 0 software-only shim (`fpga_re`) with CPython fallback
