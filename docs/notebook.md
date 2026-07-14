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

1. [EXPERIMENT 14 Jul 2026 08:49:52 PR Shell Rebuilt From Source on nf-server06 — New Card, New Flash, device_usable=true](#14-jul-2026-084952) :complete:
2. [EXPERIMENT  9 Jul 2026 10:59:06 U250 QSPI Flash — PYRO PR Shell User Image](#9-jul-2026-105906) :complete:
3. [EXPERIMENT  6 Jul 2026 14:05:00 PYRO Phase 2b — PR Shell + First pr_bitstream Partial](#6-jul-2026-140500) :complete:
4. [EXPERIMENT  6 Jul 2026 02:50:21 PYRO Phase 2 — Real Vivado Flow, Estimator Calibration](#6-jul-2026-025021) :complete:
5. [EXPERIMENT  5 Jul 2026 12:05:02 PYRO Phase 1 — Per-Pattern Circuits, Synthesis Service, C ABI](#5-jul-2026-120502) :complete:
6. [EXPERIMENT  5 Jul 2026 02:44:00 PYRO Phase 0 — Software Shim, Classifier, Model](#5-jul-2026-024400) :complete:
7. [EXPERIMENT  4 Jul 2026 07:33:45 FPGA Platform Discovery](#4-jul-2026-073345) :complete:

---

# EXPERIMENT 14 Jul 2026 08:49:52 PR Shell Rebuilt From Source on nf-server06 — New Card, New Flash, device_usable=true :complete:

## 1. Hypothesis

The U250 in `nf-server06` boots its **factory golden image** (`10ee:d004`),
not the Phase 2b PYRO PR shell that the 9 Jul entry recorded as flashed and
verified booting. QSPI is on-card and the card was believed to have moved from
`zanetti`, so the shell "should" still be in flash. Two competing explanations:

- **(a)** the image is intact but configuration fails for an environmental
  reason (leading suspect: PCIe aux power — the golden image is a minimal
  low-power design), or
- **(b)** the user image is absent/invalid.

Can `BOOT_STATUS` + the JTAG chain distinguish these **without** a QSPI
readback (which would require a volatile `program_hw_devices` — the operation
that crashed zanetti)? And if (b), can the PR shell be rebuilt **from source**
on this host, given that the original shell tree existed only as an
unversioned build directory on zanetti (`/usr/local/cad/gn262/pyro/`) and is
therefore gone?

## 2. How

- **Equipment:** Alveo U250 (`xcu250-figd2104-2L-e`) at PCI **`0000:02:00.0`**
  (physical slot 2, root port `0000:00:02.0`), QSPI mt25qu01g; host
  **`nf-server06`** (Supermicro X99, Xeon E5 v4, ASPEED BMC — **no iDRAC**),
  Ubuntu 24.04, Linux **6.8.0-134-generic**. On-board USB-JTAG (FT4232H,
  `manufacturer=Xilinx`, `product=A-U250-P64G`), hw_target serial
  **`2133061B901XA`**, FPGA DNA `40020000013B9E220500E485`.
- **Software:** Vivado **2025.2** freshly installed at
  `/usr/local/cad/2025.2/Vivado` (the R70a pin). License node-locked to this
  host's `ens9` MAC `68:05:ca:41:93:34`; covers `XCU250`/`XCU250_bitgen`,
  `PartialReconfiguration`, `cmac_usplus` (permanent). **Enterprise edition
  feature expires 11 Sep 2026.**
- **Sources (all now version-controlled, unlike the zanetti tree):**
  `third_party/open-nic-shell/` (vendored), `hw/pyro_plugin/` (the
  `pyro_250mhz` box replacing stock `p2p_250mhz`), `hw/src/pyro_id_stub.sv`
  (default child, rewritten from the R78 spec + `pyro.hdl.rp_wrapper`'s reply
  constructor), `hw/src/pyro_rp_stub.v` (the frozen R80 boundary as a black
  box), `hw/dfx/` (DFX flow ported from `ebpf-os/integration/dfx`).

### Key commands

```bash
# Diagnosis (READ-ONLY; no reconfiguration of any kind):
vivado -mode batch -source scripts/status.tcl

# Build the PR shell from source (~2.5 h):
hw/dfx/dfx_build.sh --fast          # ID-stub OOC synth + 250MHz gate only
hw/dfx/dfx_build.sh --jobs 16       # full: static -> lock -> partial -> pr_verify

# Flash (live PCIe -- see §4):
PYRO_FLASH_ALLOW_LIVE_PCIE=1 scripts/flash_u250.sh flash \
    hw/dfx/build/dcp/open_nic_shell.bit
```

## 3. Observations

**Diagnosis — hypothesis (b), and for an unanticipated reason.**

- `status.tcl`: `DONE=1`, `EOS=1`, `PLL_lock=1`, `CRC_error=0`, die 50.8 °C,
  VCCINT 0.847 V / VCCAUX 1.828 V / VCCBRAM 0.850 V. The FPGA configures
  **cleanly** and its rails are nominal — **hypothesis (a) (aux power) is
  dead**. PCIe link is Gen3 8.0 GT/s ×16, identical to zanetti's.
- **`BOOT_STATUS.SLR0 = 0x00000d07`** decodes to `STATUS_VALID_0` +
  **`IPROG_0`** + **`FALLBACK_0`** + **`WTO_ERROR_1`**, with `CRC_ERROR` and
  `ID_ERROR` **clear**. The golden multiboot jumped to `0x01002000`, the
  configuration **watchdog timed out**, and it fell back to golden. A clear
  CRC/ID with a watchdog timeout is the signature of an **empty or invalid
  user slot**, not a corrupted image.
- **This is not zanetti's card.** The FT4232H is on the Alveo itself, so its
  serial identifies the board. `ebpf-os/docs/fpga-bs.md` records zanetti's as
  **`2132049BF00YA`**; this one is **`2133061B901XA`**. A *different physical
  U250*, whose QSPI user slot was never written. That fully explains the
  golden fallback and why the 9 Jul image (`5603dd5c…`) is nowhere to be found.

**Build (from scratch, all sources now in git).**

- ID stub OOC synth: **0 errors, 0 critical warnings**, WNS **+2.885 ns** at
  the 4.000 ns (250 MHz) constraint; **32 LUTs / 336 FFs / 0 BRAM**.
- Static DFX assembly saw **exactly one black box** —
  `box_250mhz_inst/pyro_250mhz_inst/g_intf[0].pyro_rp_inst` — i.e. the plugin
  and the `-user_plugin` mechanism worked and only the RP was left empty.
- Floorplan **`CLOCKREGION_X0Y9:CLOCKREGION_X3Y10`** (SLR2), **not** the
  `CLOCKREGION_X5Y7:X5Y8` that `docs/device-bringup.md` quotes — see §4.
- Post-route timing, per clock domain:

  | Clock | WNS (ns) | Contents |
  |---|---|---|
  | `axis_aclk_0` | **+0.030** | QDMA H2C → `pyro_rp` → C2H (our datapath) |
  | `clk_out1_qdma_subsystem_clk_div` | +0.715 | QDMA / AXI-Lite |
  | `pipe_clk` | +0.351 | PCIe |
  | **`txoutclk_out[0]`** | **−0.427** | **OpenNIC `cmac_usplus` lbus2axis** |

  Worst path *inside* `pyro_rp`: setup **+0.482 ns**, hold **+0.021 ns** —
  all MET. The only failing domain is inside OpenNIC's own CMAC IP.
- **`PR_VERIFY_ALL_OK`** — the ID-stub partial verifies against config0.
- Artifacts: `open_nic_shell.bit` 43 MB (sha256 `ee5094ae…`),
  `open_nic_shell.mcs` 118 MB (sha256 `165c2480…`),
  `static_routed_locked.dcp` 76 MB, `partials/id_stub.bit` 3.9 MB.
  Build script computed and logged **BUILD16 = `0xB18A`** — but that is *not*
  what got baked into the shell; see the `BUILD16` defect below.

**Flash — and the zanetti hazard did not reproduce.**

- `program_hw_devices` loaded the SPI-bridge helper ("1 SPI core(s)", 12 s)
  **while the card was live on PCIe at `02:00.0`** — the exact operation that
  raised an uncorrectable root-port FATAL and reset zanetti four times.
- `Erase Operation successful.` → `Program/Verify Operation successful.` →
  `Flash programming completed successfully` — **FLASH_DONE, rc=0, 15m33s**.
- **The host did not crash.** Uptime unbroken across the whole operation.
- Card still enumerates `10ee:d004` (golden) post-flash — **expected**: the
  FPGA only reads QSPI at power-up.

**Cold power cycle — the shell boots, and `device_usable` flips true.**

- **`BOOT_STATUS.SLR0 = 0x00000005`** (was `0x00000d07`): `STATUS_VALID` +
  `IPROG`, with **`FALLBACK` and `WTO_ERROR` now CLEAR**. The multiboot jump to
  `0x01002000` completed. This one register is the whole before/after.
- `02:00.0` enumerates **`10ee:903f`**, PCI class **`0280`** (Network
  controller), subsystem **`10ee:0007`** — the same subsystem the 9 Jul entry
  recorded for the working shell. Gen3 8.0 GT/s ×16. BARs changed from golden's
  32M+64K to OpenNIC's **256K + 4M**.
- **One** physical function, not two (zanetti had `903f` + `913f`): we build
  `pf=cmac=1`, and the driver confirms — `onic: Number of CMAC instances = 1`.
- `onic` driver: **`make` alone produces a broken module.** Stale objects in
  `NetFPGA-PLUS/sw/driver/open-nic-driver` (from the 6.8.0-124 build) survive an
  incremental build; the link stamps the right vermagic but the objects carry
  old symbol CRCs, so `insmod` dies with `disagrees about version of symbol
  netdev_info` / `Unknown symbol ... (err -22)` and the misleading userspace
  message **"Invalid parameters"**. `make clean && make` fixes it (3.5 MB, the
  size ebpf-os recorded). The three kernel-6.8 API fixes were already applied.
- **The netdev is `ens2`**, not `enp2s0f0`: systemd used slot-based naming
  (`onic 0000:02:00.0 ens2: renamed from onic2s0f0`).
- Interface up (**NO-CARRIER, as expected** — CMAC is tied off), `CAP_NET_RAW`
  granted to a copied venv interpreter, then:

  ```
  device_usable = True
  reason = device_usable=true — static_shell_id=0x02023841, transport: CAP_NET_RAW present
  ```

- Protocol round-trip on real silicon:

  | request | reply | spec |
  |---|---|---|
  | `ID_REQUEST` | `ID_REPLY` (0x02), `SPEC16=0x0202`, **`rp_child_id = 0`** | R80/R81 |
  | `MATCH_REQUEST` | `STATUS/ERROR` (0x05), **code 7 = `PYRO_E_NOT_RESIDENT`** | R78.8 |

**Defect found and fixed: `BUILD16` is ASCII garbage in the flashed shell.**

- The shell reports `static_shell_id = 0x02023841`, i.e. **`BUILD16 = 0x3841`**,
  not the `0xB18A` the build script computed and logged.
- Cause: `synth_design -generic BUILD16=0xB18A`. **Vivado's `-generic` does not
  accept a `0x` literal** — it silently binds it as a *string*, and a string in
  a `[15:0]` parameter becomes its ASCII bytes. Vivado logs
  `Parameter BUILD16 bound to: 8A - type: string` and **does not warn**.
  `'8'=0x38`, `'A'=0x41` → `0x3841`. Exactly what the card reports.
- Fixed: pass BUILD16 as a **decimal** integer. Verified —
  `Parameter BUILD16 bound to: 16'b1011001100101110` (= `0xB32E`). `dfx_build.sh`
  now hard-fails if the parameter ever binds as a string again.

## 4. Data analysis

**The card is the story.** Every earlier hypothesis (aux power, corrupt image,
a flash that silently didn't take) was wrong in the same way: they all assumed
continuity of *the board*. The JTAG serial is the cheap invariant that settles
it, and it was never recorded in this notebook — only in `ebpf-os`'s. Worth
making a habit: **record the hw_target serial and FPGA DNA in every entry**,
because BDF, hostname, and netdev names are all properties of the *host*, and
only these identify the *card*.

**`BOOT_STATUS` is the right diagnostic, and it is free.** It answered
"is the user image there?" without a QSPI readback — which matters, because a
readback needs the same volatile `program_hw_devices` bridge the flash does,
i.e. it carries the identical host-crash exposure. Paying a host-reset risk to
*confirm* what a free register already told us would have been a bad trade.
`FALLBACK + IPROG + WTO_ERROR` with `CRC/ID` clear ⇒ nothing loadable at the
multiboot address.

**The live-PCIe crash appears genuinely R740-specific.** nf-server06 absorbed
a volatile JTAG reconfiguration of a live, enumerated endpoint with no fatal
and no reset. That is a single data point, not a proof, and the mechanism
(endpoint identity swapping under a running root port) is real — but the
zanetti workaround (BIOS slot disable via iDRAC) has no equivalent here and is
now, on this evidence, not needed. `flash_u250.sh` still refuses by default;
the risk decision remains an explicit `PYRO_FLASH_ALLOW_LIVE_PCIE=1`.

**The floorplan in the docs is wrong for this shell.** `CLOCKREGION_X5Y7:X5Y8`
overlaps OpenNIC's own packet-adapter pblocks on row Y8 (`X1Y8:X2Y8`,
`X5Y8:X6Y8` — recorded in `ebpf-os/integration/dfx/README.md`). Used the SLR2
region ebpf-os proved on this exact part instead. Pattern circuits measure
197–410 LUTs, so RP area is not the binding constraint; avoiding the collision
is. `hw/dfx/platform_manifest.json` is now the single source of truth.

**The timing failure is real but out of the datapath.** WNS −0.427 ns lives
entirely in OpenNIC's `cmac_usplus` lbus2axis FIFO on the 322 MHz transceiver
clock — the RISK-2 "OpenNIC-on-2025.2 closure" problem ebpf-os documented and
shipped partials on. In the PYRO shell the **CMAC path is tied off entirely**
(`adap_tx` idle, `adap_rx` sunk), which is precisely why bring-up needs no
100G transceiver or link partner: the R78 control protocol loops
H2C → `pyro_rp` → C2H *inside the card*, never reaching a MAC. Caveat for
future work: `axis_aclk_0` closes at only **+30 ps**. The 250 MHz box has
essentially no margin left, so a per-pattern child materially larger than the
ID stub may not close — watch this when the first real pattern partial is built.

**Two silent-corruption failures, same shape.** Both the `BUILD16` bug and the
`onic` build failure share a structure worth naming: **a tool accepted bad input
and produced a plausible artifact instead of an error.** Vivado took
`BUILD16=0xB18A`, decided it was a string, bound `"8A"`, logged it in a form
nobody reads, and emitted a bitstream that synthesizes, routes, passes
`pr_verify`, flashes, boots, and answers the probe — while carrying an identity
that is ASCII text. `make` took stale 6.8.0-124 objects, linked them with a
6.8.0-134 vermagic, and produced an `onic.ko` that is byte-for-byte a valid
module and fails only at `insmod`, where the kernel's honest complaint
("disagrees about version of symbol") is flattened by userspace into the
actively misleading **"Invalid parameters"** — which sends you hunting for a
module parameter that does not exist. Neither failure was caught by anything
except *checking the value on the far side*. The lesson is to assert on what the
tool actually bound, not on what you passed it: `dfx_build.sh` now greps the
synth log for `type: string` and hard-fails, and `make clean` is not optional
when the kernel has moved under a driver tree.

**`ens2` breaks the shape of F3, not just its value.** The spec's F3 fact names
a netdev (`enp175s0f0`) as though it were derivable from the card. It is not:
systemd chose *slot-based* naming here, so the interface is `ens2` — a name that
encodes the physical slot, not the BDF. No amount of re-deriving `enp<bus>s<slot>f<fn>`
from `02:00.0` would have produced it. A netdev name is a property of the host's
naming policy, and the spec should treat it as configuration, not as a fact.

**Root cause of the whole episode: the shell lived outside version control.**
`open-nic-shell` + the `pyro` plugin + the floorplan + the DFX scripts existed
only as a build tree under `/usr/local/cad/gn262/pyro/` on zanetti. Nothing in
`git log --all --diff-filter=A` for this repo has ever contained a `.xdc`, a
shell `.sv`, or a DFX `.tcl` (only `scripts/status.tcl`). The 6 Jul entry
claims a working PR flow and a first partial; none of the machinery that
produced them was committed. It is all now under `hw/` and
`third_party/open-nic-shell/`.

## 5. Ideas for future experiments

- **Spec debt, now urgent (R71/F2/F3).** The spec declares `af:00.0` (F2) and
  `enp175s0f0` (F3) as normative facts, and `pyro/_route.py` / `pyro/device.py`
  default `PYRO_DEVICE_IFACE` to `enp175s0f0`. On this host the card is
  `02:00.0` and the netdev is **`ens2`** — so *all three* are false and the
  library default cannot reach the device. Everything above only works with an
  explicit `PYRO_DEVICE_IFACE=ens2`. This needs a spec decision (re-declare F2/F3,
  or demote them from facts to per-host configuration), not a silent code edit.
  Note `ens2` also falsifies the *shape* of F3, not just its value: systemd's
  slot-based naming means the netdev name is not derivable from the BDF.
- **Reflash to get an honest `BUILD16`.** The running shell reports `0x3841`
  (ASCII `"8A"`). Harmless — R81 only constrains the SPEC16 half — but the
  discriminator fails at its one job: identifying which build is on the card.
  The fix is in `dfx_build.sh`; it costs a rebuild (~2.5 h) + reflash (~18 min).
  Worth folding into the next shell change rather than doing on its own.
- **AC-2b ledger:** AC-2b-2's hardware path (the real `(True, …)` flip against
  the physical board) is now **LIVE**, not SKIP. AC-2b-3 still needs a real
  per-pattern `pr_bitstream` loaded over JTAG.
- Load the ID-stub **partial** (`hw/dfx/build/partials/id_stub.bit`) over JTAG
  against the locked static — the cheapest possible exercise of `load_partial`
  (R86.5), and the first live PR reconfiguration. It should be a no-op
  observationally (same child), which is exactly what makes it a safe first test.
- Then a **real pattern child**: generate via `pyro.hdl.rp_wrapper`, build the
  partial against `static_routed_locked.dcp`, load it, and confirm `ID_REPLY`
  flips `rp_child_id` to the non-zero R78.5a value and `MATCH_REQUEST` returns
  actual matches instead of `PYRO_E_NOT_RESIDENT`.
  **Watch timing:** `axis_aclk_0` closed at only **+30 ps** with the 32-LUT ID
  stub in the RP. A pattern child is 197–410 LUTs. There is no guarantee the
  250 MHz box still closes — this is the most likely next failure.
- Re-run the AC-2-4 estimator calibration corpus under 2025.2 (R74a): the
  §6 table is 2023.1-derived and is not evidence for the current pin.

---

# EXPERIMENT  9 Jul 2026 10:59:06 U250 QSPI Flash — PYRO PR Shell User Image :complete:

## 1. Hypothesis

Can the Phase 2b PYRO PR shell be written persistently to the U250's QSPI
user-image slot using the proven-safe BIOS-Slot-4-disable procedure — without
crashing the host, as every live-slot JTAG reconfiguration has — so that the
card boots the PR-capable shell at the next cold power cycle?

## 2. How

- **Equipment:** Alveo U250 (xcu250-figd2104-2L-e) at PCI af:00.0 (Dell,
  iDRAC9 @ 10.66.3.9), QSPI mt25qu01g; host zanetti, Ubuntu 24.04,
  Linux 6.8.0-124-generic.
- **Software:** Vivado 2025.2 Hardware Manager over USB-FTDI JTAG;
  `scripts/flash_u250.sh` (ported from ebpf-os, commit f07ff28).
- **Image:** Phase 2b full flash `open_nic_shell.mcs` (124 MB, SPIx4,
  size 128, user image @ 0x01002000), sha256 `5603dd5c…0223e93f` — verified
  identical to the artifact recorded in `/usr/local/cad/gn262/pyro/STATUS.md`.
  Golden image at 0x0 untouched (unbrickable: bad user image falls back).

### Key commands

```bash
# Precondition (BIOS Slot 4 disabled via iDRAC, applied at reboot):
lspci -nn | grep -i xilinx        # -> no output; af:00.0 not enumerated
# Erase + program + verify QSPI over JTAG (script interlock re-checks af:00.0):
scripts/flash_u250.sh flash
# Boot verification (after Slot 4 re-enable + cold power cycle):
lspci -d 10ee: -nn                # -> af:00.0 [10ee:903f], af:00.1 [10ee:913f]
# Shell identity: OpenNIC BUILD_TIMESTAMP CSR, BAR2 offset 0x0
sudo python3 -c "import mmap,struct; f=open('/sys/bus/pci/devices/0000:af:00.0/resource2','r+b'); m=mmap.mmap(f.fileno(),4096); print(hex(struct.unpack('<I',m[0:4])[0]))"
```

## 3. Observations

- The script's PCIe interlock passed: `0000:af:00.0` absent from sysfs
  (Slot 4 disable in effect; slot still powered, JTAG reachable).
- Flash-helper bitstream loaded over JTAG (`program_hw_devices`, 12 s;
  "programmed with a design that has **1 SPI core(s)**").
- `Performing Erase Operation... Erase Operation successful.`
- `Performing Program and Verify Operations... Program/Verify Operation
  successful.`
- `INFO: [Labtoolstcl 44-377] Flash programming completed successfully` —
  **FLASH_DONE, rc=0, elapsed 18m10s** (10:39:29 → 10:57:52).
- **Host did not crash** — first successful on-host reconfiguration of this
  card since the 2026-07-06 JTAG crash.
- **10 Jul 00:45 — first post-flash power cycle, slot still disabled.** Host
  was down 00:02→00:45 and came back cleanly on 6.8.0-124, but no Xilinx
  device enumerated *and no root port `ae:00.0`* — only Sky Lake-E uncore
  functions on bus `ae`. The absent root port is the BIOS Slot 4 disable
  signature (a failed card would still show the root port with no link), so
  this power cycle did not include the Slot 4 re-enable; boot verification
  of the new user image has not happened yet.
- **10 Jul 00:56 — Slot 4 re-enabled + power cycle: card boots the new
  image.** Host up at 00:56:16; `af:00.0` [10ee:903f] / `af:00.1`
  [10ee:913f] (subsystem 10ee:0007) enumerated, **link Gen3 8.0 GT/s x16**,
  `onic` driver bound, netdevs `enp175s0f0/f1` present.
- **Shell identity CSR (BAR2 offset 0x0) reads `0x07060612`** — the OpenNIC
  `BUILD_TIMESTAMP`, confirming the running shell is the Phase 2b PYRO PR
  build (see §4).

## 4. Data analysis

The slot-disable procedure works as designed: with Slot 4 un-enumerated at
the BIOS level, the endpoint identity swap during JTAG activity never
reaches root port ae:00.0, so no platform-firmware FATAL. Timing matches
the ebpf-os reference run (~18 min for a full 128 Mb-addressed image with
erase+verify). The R45a CYCLES/BYTES CSR work (spec v2.3.0, harness 2.1.0)
does not stale this image: the counters live in per-pattern harness circuits
delivered later as PR partials over PCIe; the static shell is unchanged.

First boot resolved the remaining risk: the QSPI image configured before
BIOS bus scan and presented valid config space, so the endpoint enumerated
normally — no golden fallback.

Shell identity is positively the Phase 2b PYRO PR build, not the prior
stock 2022.2 OpenNIC image. OpenNIC's `build.tcl` sets `BUILD_TIMESTAMP`
to the build's launch wall-clock formatted `%m%d%H%M` and embedded as
literal hex digits, so `0x07060612` decodes to **Jul 6, 06:12** — exactly
when the PR-shell build launched (`pr_launcher.sh` mtime 2026-07-06
06:12:22 in `/usr/local/cad/gn262/pyro/`), and the flashed
`open_nic_shell.mcs` re-hashes to the same sha256 `5603dd5c…0223e93f`
recorded above. The stock image would report its own, older build time.

## 5. Ideas for future experiments

- **Next:** load the ID-stub partial over PCIe (ICAP) as the first live PR
  test on the now-verified PR shell.
- Rebuild the `ab+c` partial with the 2.1.0 harness (pre-2.1.0 artifacts are
  stale per R45a) and read back the CYCLES/BYTES counters after a real scan
  — first hardware data for R59/AC-3-3 win attribution.
- Automate the slot dance: `racadm set BIOS.IntegratedDevices.Slot4Disable`
  + `jobqueue create ... -r pwrcycle` (still untested).

---

# EXPERIMENT  6 Jul 2026 14:05:00 PYRO Phase 2b — PR Shell + First pr_bitstream Partial :complete:

## 1. Hypothesis

Can PYRO build a partial-reconfiguration-enabled OpenNIC shell for the Alveo
U250 and, against its locked static checkpoint, generate a genuine per-pattern
**partial bitstream** that closes timing and passes `pr_verify` — flipping the
R71/R83a `pr_flow_present` predicate true on honest, host-observable evidence?

## 2. How

- **Equipment:** Alveo U250 (xcu250-figd2104-2L-e), owner's board at PCI af:00.0;
  host 72-core, 376 GB RAM, Ubuntu 24.04 / glibc 2.39.
- **Software:** Vivado **2025.2** (`/usr/local/cad/2025.2/Vivado`, re-pinned from
  2023.1 which segfaults at batch exit on this glibc — spec v2.2.1); open-nic-shell
  @ ce85c8d + `pyro` plugin (reconfigurable partition `pyro_rp`, Pblock
  `CLOCKREGION_X5Y7:X5Y8`); CMAC license permanent through 2027.06.
- **Flow:** full DFX — baseline shell build, then static synth + `link` (opt/place/
  route → routed + **locked** static DCP, full flash `.bit`/`.mcs`, ID-stub partial),
  then the `VivadoToolchain` `pr_bitstream` mode against the locked substrate.

### Key commands

```bash
# PR shell (detached, ~2 h): static synth + DFX link
vivado -mode batch -source build_pr.tcl -tclargs -stage link -board au250 \
  -tag pyro_pr -jobs 32 -reference 1
# First real per-pattern partial via the toolchain PR mode
PYRO_TOOLCHAIN=vivado PYRO_VIVADO=/usr/local/cad/2025.2/Vivado \
PYRO_PR_STATIC_DCP=.../pr/pyro_static_locked.dcp \
PYRO_PR_REFERENCE_DCP=.../pr/pyro_static_id_stub_routed.dcp \
  python3 first_pr_job.py     # pattern 'ab+c'
# R83a availability probe with the evidence manifest
PYRO_PR_EVIDENCE_MANIFEST=.../patterns/pattern_a7cb950cc8624276_manifest.json \
  python3 -c "import phase2_support as p; print(p.pr_flow_present())"
```

## 3. Observations

PR shell (ID-stub reference config) and first pattern partial (`ab+c`):

| Artifact | Result |
|----------|--------|
| Baseline shell (2025.2 port) | 0 errors, `.bit`+routed DCP+`.mcs` |
| PR static, timing | **WNS +0.031 ns**, all constraints met |
| Locked static DCP | 101 MB, `lock_design -level routing` |
| Pattern `ab+c` partial | **1,752,132 B**, `pr_verify` = **compatible** |
| Pattern partial, timing | met, **fmax 251.95 MHz** (> 250 target) |
| RP-child resources | **3131 LUT / 1592 FF / 3 BRAM** (0.18 % device) |
| `pr_flow_present` probe | **True** on the real evidence manifest |

RP-child wrapper size, before vs after the RAM reworks:

| Version | LUT | FF | OOC synth | Routes? |
|---------|-----|-----|-----------|---------|
| Byte-array buffers | ~7204 | ~13709 | **50+ min (timeout)** | no |
| Beat-wide RAM buffers | 7204 | 13709 | 59 s | plateau ~34k overlaps |
| + match store → BRAM, hdr snapshot | **3131** | **1592** | **45 s** | **0 overlaps, closes** |

## 4. Data analysis

The end-to-end PR path works: a per-pattern circuit becomes a routed,
`pr_verify`-passing partial bitstream against the locked static, and the
resulting `pr_bitstream`/`pr_verified` manifest is the sole host-observable,
un-fabricable evidence (R47b-consistency rejects inconsistent manifests) that
flips `pr_flow_present` true (R83a).

Two synthesis/routing pathologies gated the result, both the same root cause —
storage expressed as flip-flops instead of memory. (1) A 1536-byte frame buffer
with 64 write ports could not infer as RAM (12 k flops + decode) → 50-min synth;
fixed with beat-wide single-write-port word arrays. (2) A 61×192-bit match
capture in parallel flops (~11.7 k FF) plus contained-routing pressure in a
2-clock-region DFX Pblock → routing plateaued at ~34 k overlaps; fixed by moving
the capture to block RAM, which then forced a beat-0 header-snapshot register to
keep the frame buffers RAM-inferable. Net **9× FF reduction** (13709 → 1592)
took utilization to ~0.2 % and routing closed immediately. Every wire byte
stayed identical across both reworks (xsim 5/5). The estimator predicts only the
engine (288 LUT); the wrapper's fixed parser/buffer/capture overhead dominates —
a calibration input for Phase 3.

Toolchain robustness also validated on real output: the `pr_verify` gate
correctly accepts "compatible / Number of differences : 0" while rejecting real
failures (W6-b), and the RM-scoped `report_utilization -cells` regex handles the
2025.2 "CLB LUTs*" footnote (W3-b). One flow bug fixed: the RP cell must be
located by `HD.RECONFIGURABLE` (its ref-name is gone once black-boxed in the
locked DCP) and re-queried by immutable NAME after each netlist mutation.

## 5. Ideas for future experiments

- Flash the PR shell via JTAG and perform the one root-assisted PCIe rescan to
  flip `device_usable` true; run the on-device ACs live (AC-2b-2/2b-3).
- Load the `ab+c` partial into `pyro_rp` over JTAG (R85) on the live board and
  drive a MATCH_REQUEST end-to-end over the onic netdev.
- Feed measured RP-child utilization back into the estimator (R74a): model the
  fixed wrapper overhead separately from the per-pattern engine.
- Multi-pattern residency: exercise R87 slot ≥ 2 once a multi-partition shell
  floorplan exists.

---

## 1. Hypothesis

Can the Phase-1 mock toolchain be replaced by a real Vivado synthesis flow —
regex-generated RTL through synth + P&R + timing for the physical board's part —
and does the resource estimator agree with real post-route utilization within
the pre-registered R74 margin? Hardware-gated ACs must record SKIP (never PASS)
since the board is unavailable (spec v2.1.x, R71).

## 2. How

- **Equipment:** Alveo U250 (xcu250-figd2104-2L-e) at PCI 0000:af:00.0/1 running
  a third-party OpenNIC shell image (onic driver v0.21) — observed only, never
  touched. Host: Ubuntu (kernel 6.8.0-124), no root, no /dev/qdma*, no OpenNIC
  PR-partition floorplan.
- **Software:** Vivado 2023.1 (/usr/local/cad/Vivado/2023.1; needs a
  libtinfo.so.5→.so.6 shim on this host — the adapter creates its own), CPython
  3.12.3, spec v2.1.0→v2.1.2 (R70–R77, R3c, R64a, R70b), PYRO ABI 2.0.0 frozen.
- **Benchmarks:** 6-pattern calibration corpus through OOC synth+P&R at 250 MHz;
  full acceptance suite AC-0-*..AC-2-* (1051 tests).

### Key commands

```bash
# probe: which Vivado supports the U250 part with a license
LD_LIBRARY_PATH=<shim> /usr/local/cad/Vivado/2023.1/bin/vivado -mode batch -source probe.tcl

# real flow, end to end
PYRO_TOOLCHAIN=vivado PYRO_VIVADO=/usr/local/cad/Vivado/2023.1 \
  python3 -m pytest tests/acceptance -q -rs
gmake lib abi-check
```

## 3. Observations

- Vivado 2019.2 (/tools/Xilinx) has **no UltraScale+ Alveo device support**;
  2023.1 and 2025.2 both synth+place+route xcu250 cleanly (license OK).
- The Phase-1 generated RTL (`pyro_circuit`) went through real synthesis
  **unmodified** — all patterns met 250 MHz timing OOC (WNS +1.7 to +2.4 ns).
- Real post-route utilization vs (recalibrated) estimator, Vivado 2023.1:

| pattern | states | real LUT | est LUT | real FF | est FF | WNS (ns) |
|---|---|---|---|---|---|---|
| `abc` | 4 | 197 | 278 | 427 | 452 | **+2.405** |
| `[a-z]+[0-9]{2,4}` | 9 | 197 | 304 | 430 | 457 | +1.726 |
| `(?:GET\|POST\|PUT) /[a-z/]* HTTP` | 24 | 221 | 388 | 441 | 472 | +2.200 |
| `^ERROR: .*$` | 12 | 226 | 320 | 431 | 460 | +1.884 |
| `[A-Za-z0-9]{60}` | 62 | 228 | 624 | 484 | 510 | — |
| `[A-Za-z0-9]{200}` | 202 | **410** | 1464 | **626** | 650 | — |

- The mock-era estimator (base 2000 LUTs/2000 FFs) violated the pre-registered
  R74 margin on 3 of 4 initial patterns (est/real up to **10.6×** > 10× ceiling);
  recalibrated constants: **256 LUTs + 4/state + 2/edge; 448 FFs + 1/state**.
- Full verification: **1045 passed, 6 skipped, 0 failed** (63:41). ABI 2.0.0
  intact. All 6 skips are R71-mandated, each naming its absent prerequisite.
- Loss-regime routing measured: median pyro 1225 ns vs stock re 257 ns =
  **4.77×** relative (R3a absolute ≤ 2 µs bound PASSES; R3b relative 1.15×
  bound SKIPs per new R3c — routing decision still Python-level).

## 4. Data analysis

The ~200-LUT/~430-FF floor (CSR mux, control FSM, 64-bit offset counters)
dominates small circuits, which is why the mock-era +2000 base overshot the
10× honesty ceiling — pre-registering the R74 margin before peeking at data
did its job by forcing an estimator fix rather than a margin fix. Per-state
scaling is mild (~1.1 LUT/state real vs 4 estimated conservatively); at
MAX_STATES=1024 the estimate (~6.4k LUTs) is ≪ the PR budget (216k), so
MAX_STATES remains the binding eligibility constraint (R12 design intent
preserved). Phase 2's deliverable ledger: real-toolchain clauses of
AC-2-1/2-3/2-4 and the model clauses of AC-2-5/2-6 PASS from real execution;
every PR-bitstream/on-device clause SKIPs honestly (no PR floorplan; board is
a third-party live NIC). The R3b 4.77× measurement confirms the relative
loss-regime bound needs the native routing path — now explicitly deferred to
AC-3-3 (R3c).

## 5. Ideas for future experiments

- Build an OpenNIC shell PR-partition floorplan (needs Vivado 2022.2-era shell
  sources + a pblock for the 250 MHz user box) to un-SKIP AC-2-1's bitstream
  clause — requires a board we are allowed to program.
- Move the §8 R51 routing decision into the L3 native runtime to attack the
  4.77× → ≤1.15× gap (hard requirement at AC-3-3, Phase 3).
- Calibrate BRAM once patterns large enough to infer block RAM appear; today's
  circuits use 0 BRAM (manifest carries the fixed 16 KiB reserve).
- Richer calibration corpus: bounded-repeat counters and case-folded classes to
  stress the FF intercept (only ~24 FF headroom above the observed floor).
- Wall-clock synthesis latency distribution (currently ~4–6 min/pattern OOC) to
  tune the R77 timeout and the R4a launch threshold for interactive workloads.

---

# EXPERIMENT  5 Jul 2026 12:05:02 PYRO Phase 1 — Per-Pattern Circuits, Synthesis Service, C ABI :complete:

## 1. Hypothesis

After the project owner inverted the architecture (spec v2.0.0: every
HW-eligible regex compiles to its own synthesized circuit for the OpenNIC
dynamic region, loaded by partial reconfiguration, with async background
synthesis), can Phase 1 deliver the full software stack — HDL generator,
circuit model, synthesis service, and native C ABI — with byte-identical
results and no hardware?

## 2. How

- **Equipment:** Intel C620 x86_64 server, Ubuntu (Linux 6.8.0-124-generic);
  Xilinx OpenNIC card present but unused (mock toolchain stands in for Vivado)
- **Software:** CPython 3.12.3, pytest 9.1.1, GCC 13.3 (C11), iverilog,
  valgrind; spec evolved v2.0.0 → v2.0.5 via §13 change control
- **Benchmarks:** 506-test spec-only acceptance suite; 519 unit tests;
  4000–6000-case lazy-quantifier fuzz; valgrind on the native runtime

### Key commands

```bash
python3 -m pytest tests/unit/          # 519 passed
python3 -m pytest tests/acceptance/    # 506 passed, 0 skipped
/usr/bin/make abi-check                # pyro_abi_version = 0x00020000
/usr/bin/make valgrind                 # 0 errors, 0 leaks
```

## 3. Observations

| Gate | Result |
|------|--------|
| Unit suite | **519 passed** |
| Acceptance suite | **506 passed, 0 skipped** |
| C ABI | **2.0.0** (0x00020000), valgrind clean |
| Generated RTL | deterministic; iverilog-lints; §7.4 harness CSR map exact |
| Final review verdict | Ready to merge: **Yes** |

Delivered: `pyro/hdl/` (automaton IR, resource estimator, Verilog
generator with R47a identity block and R19c over-approximation metadata);
`pyro/_circuit_model.py` (executes the generated automaton); `pyro/synth/`
(R47b manifests, persistent bitstream cache, mock toolchain, out-of-process
synthesis service, residency manager with LRU eviction); `pyro.prewarm` +
lifecycle stats; `include/pyro_rt.h` + `src/pyro_rt.c` (ABI 2.0.0 model
binding, hardened artifact parser); `pyro.testing` fault-injection seams.

Defects found and fixed by the review loop (regression-tested):
- Circuit-model finditer dropped zero-width matches, then (round 2) missed
  CPython's **must_advance** retry — `'a??'` on `"aa"` dropped real matches
  (R19 false negatives). Fixed by delegating span enumeration to stock
  `re` while the generated automaton remains an unconditional completeness
  oracle (`CompletenessError` on any missed start).
- Scoped `(?m:...)` multiline was not threaded into anchor lowering.
- Per-call `os.environ` reads and debug mutators in the frozen ABI header
  (now `#ifdef PYRO_TESTING`; production build exports zero test symbols).

## 4. Data analysis

The hybrid trust model carried the phase: the automaton only ever needs to
be **complete** (superset of match starts); stock `re` makes results exact.
Both finditer bugs lived in hand-reimplemented CPython iteration semantics
— the lesson (twice) is to delegate enumeration to the oracle and keep the
automaton as a cross-check, not to transcribe CPython's scanner by hand.
Spec §13 change control absorbed six amendments (R19a–c over-approximation
sanction, R47a hash inputs, R51b device-free ruling, R67–R69 public test
seams) without ever breaking Phase 0's ACs. **Important integration note:
production dispatch still runs the Phase-0 model** — the circuit model and
native binding are delivered and validated out-of-band but intentionally
not yet on the dispatch path.

## 5. Ideas for future experiments

- Phase 2 entry criteria (from final review): circuit finditer stays
  must_advance-correct AND RTL anchors (`at_eol`/`at_eob`, `res_start`)
  made real before any generated circuit serves user results
- Wire `_circuit_model`/`_native` into dispatch behind R51b; measure
  cross-tier equivalence on hardware
- Vivado + open-nic-shell PR flow bring-up; replace mock toolchain
- Result-ring LE enforcement on real DMA; JSON manifest parser hardening
- Benchmark suite (R59) on real corpora to validate R1/R2 win regime and
  R19b false-positive-rate bounds

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
