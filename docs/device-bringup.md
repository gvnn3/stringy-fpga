# Device bring-up — Phase 2b operator's guide

Written against `specs/python-regex-offload.md` v2.2.3, branch `phase1-pyro`,
Phase 2b ("on-hardware bring-up: device probe + PR shell"). R-numbers below
cite the spec directly; read them there for the full normative text. This
document assumes you have already read `docs/vivado-toolchain.md` for the
pinned Vivado release (2025.2, R70a-pin) and the general OOC-synthesis
adapter — Phase 2b builds on top of that, adding the PR (partial
reconfiguration) shell, the in-band control protocol, and the physical
JTAG/flash procedure.

**Status as this is written: nothing in this guide has run against the new
shell yet.** Every on-device clause in the AC-2b suite records **SKIP**, never
PASS, until the live probe actually answers (R71/R83) — that discipline is not
a placeholder to be relaxed later; it is the spec's normative honesty rule, and
it is enforced by the `pyro.device.probe_device` return value itself, not by
test-author discretion.

> ## ⚠ HOST MOVE — 2026-07-13
>
> **The card is no longer in the host this guide was written for.** Everything
> below was written against **zanetti** (Dell R740): U250 at
`0000:af:00.0`/`.1`,
> root port `ae:00.0`, PCIe Slot 4, netdevs `enp175s0f0`/`f1`, iDRAC9 at
> 10.66.3.9, Vivado 2025.2 at `/usr/local/cad/2025.2/Vivado`, and the PYRO shell
> build tree at `/usr/local/cad/gn262/pyro/open-nic-shell`.
>
> The card is now in **nf-server06** (Supermicro X99, Xeon E5 v4), at
> **`0000:02:00.0`** (root port `0000:00:02.0`, physical slot 2). On this host:
>
> - The board carries its **factory golden image** (`10ee:d004`) — *not* the
>   stock 2022.2 OpenNIC shell it carried on zanetti. There is no user image in
>   QSPI, hence no static shell, hence nothing for a partial bitstream to load
>   into. Bring-up requires a **full** shell image, not a partial.
> - **Vivado is not installed** (`/usr/local/cad` does not exist, and is not an
>   unmounted NFS share), there is **no Xilinx license** present, and the PYRO
>   shell source/build tree did not come across with the card.
> - **No PYRO full-shell bitstream exists on this host**, and per §7 below that
>   build may never have completed on zanetti either. zanetti is on a different
>   network and is not reachable from nf-server06.
> - There is **no iDRAC**, so the slot-disablement step that made flashing safe
>   on zanetti (§3, `scripts/README.md`) has no equivalent here.
>
> Netdev names, BDFs, and the iDRAC procedure below are therefore **stale**.
> `scripts/flash_u250.sh` and `scripts/README.md` have been retargeted to
> `0000:02:00.0`; the spec (F2/F3), `pyro/_route.py`, `pyro/device.py`, and the
> AC-2b tests have **not** — they still declare `af:00.0` and `enp175s0f0` as
> normative facts. That is an open spec decision, not an oversight.

## 1. Architecture overview

### Static shell vs. the `pyro_rp` reconfigurable partition

The flashed image splits into two regions with very different change
cadences:

- The **static shell** is the OpenNIC `open-nic-shell` build (part
  `xcu250-figd2104-2L-e`) that owns PCIe, the QDMA engine, and the CMAC
  Ethernet MACs. It is bit-identical across every PR configuration (R82b)
  and changes only on a full reflash.
- **`pyro_rp`** is a Xilinx DFX reconfigurable partition (`HD.RECONFIGURABLE`
  + a `Pblock`) carved out of the static shell, floorplanned at
  `CLOCKREGION_X5Y7:X5Y8`. Per-pattern PYRO circuits become **partial**
  bitstreams targeting this one region (R72 `payload_kind == "pr_bitstream"`,
  once `pr_flow_present` is true).

The boundary between the two is frozen for the life of a flashed static
image (R80) so that every partial bitstream — the ID stub, and later every
per-pattern circuit — links against the same locked static DCP (R82). It is
deliberately thin: `clk`/`rstn`, one 512-bit AXI-Stream slave carrying
host→device (QDMA H2C) frames in, one 512-bit AXI-Stream master carrying
device→host (QDMA C2H) frames out (each with `tkeep`/`tlast` and a 16-bit
`tuser` encoding `size`/`src`/`dst`), and nothing else — the AXI-Lite MMIO
path is tied off in Phase 2b (R80). Adding a signal across this boundary is
a static-shell change (new flash, new locked DCP); changing what happens
*inside* `pyro_rp` — including the control protocol itself — is a
partial-bitstream-only change (R79). That is the whole point of the
boundary: it lets the protocol and the per-pattern circuits evolve without
ever touching the static image once it is flashed and validated.

### In-band Ethernet control protocol (R78)

Because the AXI-Lite window is tied off, every piece of host-device
communication in Phase 2b — identity query, match request/reply, error
status — rides **in-band as raw Ethernet frames** on the `onic` netdev
(`enp175s0f0`/`f1`), using a dedicated EtherType **`0x88B5`**. A PYRO frame
is: the normal 14-byte Ethernet II header, then a fixed 14-byte PYRO control
header (`magic=0x50`, `version=0x01`, `kind`, `flags`, `slot`, `seq`,
`length`, `reserved`, all big-endian), then a kind-specific payload, then
the FCS. Five message kinds exist: `ID_REQUEST`/`ID_REPLY`,
`MATCH_REQUEST`/`MATCH_REPLY`, and `STATUS`/`ERROR`. The shell's
`max_pkt_len` is 1518 bytes, so the PYRO payload is capped at 1486 bytes,
frames under the 60-byte Ethernet minimum are zero-padded, and there is no
PYRO-level fragmentation — a corpus larger than one `MATCH_REQUEST` is
chunked by the host and reassembled via the `start_off` field, the same
mechanism used on the DMA path (R41). `MATCH_REPLY` entries embed the
existing 24-byte **little-endian** `pyro_match` record (R47) verbatim, so
that field is the one deliberate exception to the header's big-endian rule
— call this out to anyone hand-parsing a capture.

The full normative field layout, with byte-exact hex test vectors for
`ID_REQUEST`, a `MATCH` round-trip, and the `STATUS`/`ERROR` reply, is in
spec §10.1 (R78.1–R78.10); `pyro.device.encode_frame`/`decode_frame` are
required to reproduce those vectors exactly (AC-2b-1), and that
reproduction is testable today, on this host, without any device — it is
pure host-side codec logic (R86.2/R86.3).

Sending and receiving these frames uses `AF_PACKET` and therefore requires
`CAP_NET_RAW` — see §4 below.

### The ID stub — the default `pyro_rp` child (R80/R81)

Whatever is flashed into `pyro_rp`, the **default child** the static image
ships with is a minimal ID stub: it parses PYRO frames (R79), answers
`ID_REQUEST` with an `ID_REPLY` carrying `rp_child_id == 0` and the shell's
build identity (R81), and replies `STATUS`/`ERROR` with
`PYRO_E_NOT_RESIDENT` (code 7) to any `MATCH_REQUEST`, since no pattern
circuit is resident yet. This is deliberate: it is what lets
`device_usable` flip true from a **freshly flashed** board, before any
pattern has ever been synthesized or loaded (R83) — the probe only needs a
valid `ID_REPLY`, not a resident circuit.

`ID_REPLY` carries a 32-bit `static_shell_id = (SPEC16 << 16) | BUILD16`
(R81): `SPEC16` is `(spec_MAJOR << 8) | spec_MINOR` of the spec version the
shell was built against — `0x0202` for spec `2.2`, the value the host
runtime is compiled to expect as `PYRO_SHELL_SPEC16` — and `BUILD16` is a
build-identifying discriminator (the low 16 bits of the build's Unix-epoch
minute count). The probe accepts a device only if the high half matches;
the low half is recorded but not required to equal a fixed constant. Note
that `static_shell_id` (the wire probe identity) is a different value from
`SHELL_VERSION` (the R45/R75 manifest/cache-key shell identifier, still the
model-harness value `0x0A000001` until a real PR flow exists) — the two
serve different purposes and neither substitutes for the other.

## 2. Artifact set (R82d)

`pr_flow_present` (R71/R83) is the conjunction of the locked static DCP
being present and validated on the host, the `pyro_rp` floorplan existing
(`Pblock` + `HD.RECONFIGURABLE`, R80), and a PR-generation flow that
produces a genuine loadable partial bitstream with a **passing** `pr_verify`
(R82c/R82d). In terms of the concrete files that flow produces (standard
Vivado DFX artifact-flow vocabulary, applied to this project's build):

- **Routed static DCP**
  - What it is: The static shell (OpenNIC `open-nic-shell` @ `ce85c8d` + the
    `pyro` plugin) after synthesis, place, and route — the precursor to
    locking, before any `pyro_rp` child is fixed in place.
  - Spec grounding: Standard Vivado DFX flow step preceding R82b.
- **Locked static DCP**
  - What it is: The routed static checkpoint with `pyro_rp` locked
    (`HD.RECONFIGURABLE`) — the **substrate every partial bitstream is
    implemented in-context against**, so the static region is bit-identical
    across every configuration.
  - Spec grounding: R82b; part of the R82d conjunction
- **Full flash `.bit`/`.mcs`**
  - What it is: The complete initial image (static shell + whatever default
    `pyro_rp` child is baked in — the ID stub, R80) used for the one-time
    JTAG/flash bring-up described in §3.
  - Spec grounding: R80 (default ID-stub child); standard Vivado
    bitstream/configuration-memory outputs
- **ID-stub partial `.bit`**
  - What it is: The R80 default child (ID stub) compiled as a **partial**
    bitstream for `pyro_rp` — used to (re)load just the stub without touching
    the static region, e.g. to return to a known-good baseline.
  - Spec grounding: R80, R82 (partial implemented against the locked static
    DCP)
- **Per-pattern partials + manifests**
  - What it is: One partial bitstream per synthesized regex pattern, each with
    a manifest recording `payload_kind == "pr_bitstream"` (R72), the real
    shell/PR-region `SHELL_VERSION`, and the independent boolean field
    **`pr_verified`** (R47b/R82c) — `true` iff Vivado `pr_verify` actually ran
    and passed for that artifact. `pr_verified` MUST be consistent with
    `payload_kind` (`true` exactly when `payload_kind == "pr_bitstream"`); the
    `pyro.synth.Manifest` constructor and `from_json` both reject a manifest
    that violates this (R47b-consistency, v2.2.3), so a hand-edited or corrupt
    manifest cannot silently claim a bitstream is PR-verified when it isn't.
  - Spec grounding: R47b, R72, R82c

Two rules govern all of the above and are worth internalizing before
touching any of these files by hand: (1) `pr_verify` is **mandatory** — a
manifest may claim `payload_kind == "pr_bitstream"` only if `pr_verify`
passed; otherwise it stays `ooc_metrics` (honest metrics, no device claim)
or the synthesis job simply fails (R82c/R65). (2) every partial — the ID
stub and every per-pattern circuit alike — MUST be built by the **same
Vivado release** that built the static shell (the pinned 2025.2,
`toolchain_version == 0x19020000`, R82a); a partial recorded under a
different `toolchain_version` is refused at load and the pattern falls back
(R47c), it does not raise a device error.

## 3. Flash procedure

This is a **two-stage** procedure: JTAG-program the new full-shell image,
then get the host to notice the board changed. The second stage needs root;
the first does not.

### Stage 1 — JTAG full-shell flash via `hw_server` (no root needed)

JTAG programming is verified working as this user: an FT4232H USB-JTAG
bridge is attached, Vivado `hw_server` connects, and the chain enumerates
`xcu250_0`. The standard Vivado Hardware Manager flow applies — either
interactively or via a Tcl script driving `hw_server`, e.g.:

```
open_hw_manager
connect_hw_server -url localhost:3121
open_hw_target
current_hw_device [lindex [get_hw_devices xcu250_0] 0]
set_property PROGRAM.FILE {/path/to/pyro_full_shell.bit} [current_hw_device]
program_hw_devices [current_hw_device]
```

This is the **one-time initial bring-up** flash of the full image (static
shell + default ID-stub `pyro_rp` child, §2); it is a distinct step from the
runtime partial-load path (`pyro.device.load_partial`, R86.5, §6 below),
which only ever reloads `pyro_rp` contents against an already-flashed
static image.

**The artifact does not exist on nf-server06.** The `.bit`/`.mcs` was to come
from the `/usr/local/cad/gn262/pyro/open-nic-shell` build on zanetti, which was
still in progress when this guide was written and whose completion is unknown
(§7). Nothing in §3 is executable here until that image is located on zanetti
and copied over, or rebuilt from source on this host. Note also that JTAG
programming on nf-server06 is untested and there is no Vivado installed to run
`hw_server` — see §7.

Two cautions on the flow above, both of which bit on zanetti:

- The `program_hw_devices` call in that snippet is a **volatile** (config-RAM)
  load. `scripts/README.md` records that there is no known safe way to run a
  volatile load on zanetti — it is what crashed the host. Bring-up on this
  project goes through **QSPI** (`scripts/flash_u250.sh`), which is persistent
  and survives the cold power cycle; prefer it.
- The bitstream must carry master-SPIx4 flash-boot config or `write_cfgmem`
  rejects it (`[Writecfgmem 68-20]`); `scripts/gen_bit_spi.sh` re-emits a routed
  `.dcp` with those properties.

### Stage 2 — driver unbind + PCIe rescan (THIS NEEDS ROOT)

JTAG programming replaces the FPGA's configuration but does **not**
re-enumerate the PCIe device the host already attached to at boot; the
kernel's view of the device (BARs, driver binding) is stale until you force
a rescan, or reboot. This is a straightforward consequence of how PCIe
hot-configuration works, not something the spec adjudicates, but it is the
step R71/§11 P1(c) names as needing "root PCIe-rescan cooperation."

**THIS NEEDS ROOT.** Unbind the current driver from both physical functions
of the card, remove the PCI devices, then rescan the bus so the kernel
re-reads config space and re-binds (BDFs are **nf-server06**'s — `02:00.x`;
on zanetti these were `af:00.x`):

```
# THIS NEEDS ROOT
sudo bash -c '
  for pf in 0000:02:00.0 0000:02:00.1; do
    echo "$pf" > /sys/bus/pci/devices/$pf/driver/unbind 2>/dev/null || true
    echo 1 > /sys/bus/pci/devices/$pf/remove
  done
  echo 1 > /sys/bus/pci/rescan
'
```

Confirm the driver actually bound to the new PFs with `lspci -k -s 02:00`
before proceeding — the driver name to unbind/rebind from is whatever
`lspci -k` reports as currently attached (this is an operator-verification
step, since the exact sysfs driver path depends on the driver actually
loaded at the time). If the unbind/remove/rescan sequence does not bring
the card back cleanly (missing BARs, netdevs not appearing), a full reboot
is the fallback and is unconditionally safe — it always re-enumerates from
scratch.

**On nf-server06, prefer the cold power cycle outright.** A rescan is the
*less* proven path here: the zanetti crash (uncorrectable FATAL at the root
port when the endpoint identity swaps under a live system) was never shown to
be R740-specific, and `scripts/README.md` records that OS-level unbind/remove
did not prevent it there. A full AC-off power cycle loads QSPI before BIOS
enumeration and sidesteps the question entirely.

### Stage 3 — `onic` driver rebind (verify, not necessarily an action)

Once the PCIe rescan (or reboot) completes, the `onic` driver (v0.21) should
rebind automatically if it is already loaded and its ID table matches the
freshly flashed shell's PCI IDs; if it does not rebind automatically, load
or reload the module and confirm the expected netdevs appear:

```
lsmod | grep onic
ip link show enp2s0f0      # zanetti: enp175s0f0
ip link show enp2s0f1      # zanetti: enp175s0f1
```

Netdev names follow the PCI bus, so on nf-server06 (bus `02`) expect
**`enp2s0f0`/`enp2s0f1`**, not the `enp175s0f0`/`f1` (bus `af` = 175) this guide
was originally written against. Seeing both as netdevs (even if down) is the
practical sign that the shell reflash + rescan succeeded at the PCIe/driver
level. This is necessary but not sufficient for `device_usable` — it only
confirms the netdev exists; the R83 probe (§5) is what actually validates
that the flashed image answers PYRO's protocol correctly.

## 4. Transport privileges

Sending/receiving PYRO frames uses `AF_PACKET` on the `onic` netdev, which
the kernel gates behind `CAP_NET_RAW` (R78, P2/P3, R83 condition (ii)). Two
ways to grant it to the process actually running the test suite / runtime,
in order of what this document recommends:

- **`setcap` on a dedicated venv python copy (recommended).** Grant the
  capability directly to a Python interpreter binary, scoped to a venv you
  use only for PYRO device work, rather than to the system interpreter:

  ```
  # THIS NEEDS ROOT (setcap itself requires CAP_SETFCAP / root)
  sudo setcap cap_net_raw+ep /path/to/pyro-venv/bin/python3.12
  ```

  After this, any process launched from that exact binary carries
  `CAP_NET_RAW` without needing `sudo` at invocation time, and — because
  it's scoped to one venv copy, not the system Python — it does not grant
  the capability to every script on the host that happens to invoke
  `python3`. This is the safer default: the blast radius of a mistake is
  one venv, not the whole machine's Python install.
- **`sudo` with ambient capabilities, or just running under `sudo`.** The
  alternative is to run the test runner under `sudo` (or use
  `capsh --caps=... -- -c '...'` / `pam_cap` ambient-capability setup) so
  the process inherits `CAP_NET_RAW` for that invocation only. This avoids
  permanently marking a binary but means every invocation needs root
  (or a pre-configured ambient-capability rule), which is easy to forget
  and easy to over-grant (a `sudo`'d process has a lot more than
  `CAP_NET_RAW`).

Either way, `probe_device` (R86.4) is required to **never** need elevated
privilege just to tell you it can't proceed: if `CAP_NET_RAW` is absent, it
detects that before attempting any privileged `AF_PACKET` call and returns
`(False, reason)` rather than raising `PermissionError` or crashing. So
running the suite without either of the above is safe — it will report
`device_usable=false` with the capability named as one of the unmet
conditions (§5), not blow up.

Two device-specific environment knobs round out the configuration surface
(R68, sampled at the R35a points, carried on `DeviceConfig` — R86.6, never
read ad hoc):

- **`PYRO_DEVICE_IFACE`**
  - Default: `enp175s0f0`
  - Effect: The `onic` netdev name the device transport binds for the
    `AF_PACKET` path. **On nf-server06 this default is wrong and MUST be
    overridden** — the card is on PCI bus `02`, so the netdev is `enp2s0f0`
    (or `enp2s0f1` for the second port), not the bus-`af` name the spec's F3
    fact declares. Set `PYRO_DEVICE_IFACE=enp2s0f0`. The library default still
    follows spec F3 (`pyro/device.py`), which has not been re-declared for the
    new host.
- **`PYRO_HW_SERVER`**
  - Default: `TCP:localhost:3121`
  - Effect: The Vivado `hw_server` URL the JTAG loader connects to
    (R85/R86.5). Override if `hw_server` is not running on the default port,
    or is reached via a different host.

Everything else device-related — the expected `PYRO_SHELL_SPEC16`
(`0x0202`), the probe/JTAG timeouts, and every R78 frame constant — is
spec-fixed, not an environment knob; a `DeviceConfig` may override them for
tests, but there is no env var for them (R68).

## 5. Verifying: the probe and the canonical SKIP reasons

`pyro.device.probe_device(config) -> (usable: bool, reason: str)` (R86.4)
is the single source of truth for `device_usable`. It sends `ID_REQUEST` up
to 3 times (`PYRO_PROBE_TIMEOUT = 500 ms` each, R84), validates the
`ID_REPLY`'s `static_shell_id` `SPEC16` half against `PYRO_SHELL_SPEC16`
(R81), and checks for `CAP_NET_RAW`. It returns `(True, reason)` only when
**both** conditions hold; otherwise `(False, reason)`, where `reason` is
the R83 canonical enumeration — a fixed-order, comma-separated list of
exactly the unmet conditions, drawn from:

1. `probe: no valid ID_REPLY (no reply within PYRO_PROBE_TIMEOUT, or
   static_shell_id SPEC16 mismatch)`
2. `transport: CAP_NET_RAW absent`

On this host, **before** the PR shell is flashed and before `CAP_NET_RAW`
is granted, both conditions are unmet, so the reason is the exact,
non-elided two-condition literal:

```
device_usable=false — probe: no valid ID_REPLY (no reply within PYRO_PROBE_TIMEOUT, or static_shell_id SPEC16 mismatch), transport: CAP_NET_RAW absent
```

If the PR shell were flashed and the probe got a valid `ID_REPLY` but
`CAP_NET_RAW` were still absent, only condition 2 would appear; if
`CAP_NET_RAW` were granted but the flashed shell answered with a
mismatched `SPEC16` (or didn't answer at all — timeout), only condition 1
would appear. A clause additionally requiring `pr_flow_present` appends
`; pr_flow_present=false — <first missing R82d artifact>` when that
predicate is also unmet (R83). This is a genuine enumeration, not a fixed
string: **what changes as bring-up progresses is which of these conditions
drops out of the list**, and the AC-2b suite flips the corresponding clause
from SKIP to LIVE exactly when its required predicate(s) actually flip true
— never before, and never by asserting it manually (R71/R83). Concretely:

- **AC-2b-1** (host frame codec vs. the R78 test vectors) requires nothing
  from the device — it is **LIVE today**, on this host, with no hardware
  involved at all.
- **AC-2b-2** (the live probe / `device_usable` flip) is **LIVE** for its
  no-privilege, timeout, `SPEC16`-mismatch, and valid-`ID_REPLY` paths
  *as driven through the R86.6 `DeviceConfig` seams* (`cap_check`,
  `transport_factory`) without a real NIC — but the real `(True, …)` flip
  against the physical board is **SKIP** until the probe actually gets a
  valid reply from real hardware.
- **AC-2b-3** (`pr_bitstream` via the locked static + `pr_verify`, JTAG
  load) is **SKIP** until both `pr_flow_present` and `device_usable` flip
  true; the `pr_verified`/manifest-consistency checking itself is
  host-testable without a device.

A SKIP is never recorded as PASS, and a PASS is never asserted from
anything other than real execution with its predicate satisfied — that
discipline is what lets this document say, honestly, that hardware ACs
SKIP until the probe answers, full stop, regardless of how far along the
PR shell build or the flash procedure is.

## 6. Runtime partial loads via JTAG (R85)

Once `pr_flow_present` and `device_usable` are both true and a per-pattern
`pr_bitstream` artifact exists, loading it onto the card is
`pyro.device.load_partial(config, partial_bitstream_path)` (R86.5). In
Phase 2b this is implemented **out of band, via JTAG** through
`hw_server` — the same mechanism used for the one-time full-shell flash in
§3, just targeting a partial bitstream instead of the full image. This is
safe with respect to the live PCIe link: the static shell owns PCIe and is
bit-identical across every `pyro_rp` configuration (R82b), so reconfiguring
`pyro_rp` over JTAG does **not** disturb the PCIe link or the `onic`
netdev — no driver unbind, no rescan, no reboot needed for a partial load,
unlike the full-shell flash in §3.

`load_partial` bounds the JTAG programming step
(`program_hw_devices`, in Vivado terms) by `PYRO_JTAG_LOAD_TIMEOUT = 600 s`
(10 minutes, R84); on expiry it kills the programming process tree and
raises `PyroLoadError`, the same exception it raises on any other JTAG/
`hw_server` failure (unreachable server, chain mismatch, corrupt or
incompatible bitstream). A JTAG-load timeout is treated as **transient** —
it maps to `PYRO_E_NOT_RESIDENT`/fallback (R47c) — not as the R65 permanent
synthesis-failure semantics; a failed load simply means the pattern isn't
resident yet, not that it never will be. Artifact admissibility itself
(`payload_kind == "pr_bitstream"`, same Vivado release as the static shell,
integrity hash — R72b/R82a) is checked by `pyro_circuit_load` (R40)
upstream of `load_partial`; `load_partial`'s own job is purely the JTAG
mechanism.

**ICAP/MCAP (in-band, self-hosted) partial reconfiguration is explicitly
deferred** (R85) — the `ce85c8d` PR shell does not yet expose the
additional shell plumbing (an ICAP/MCAP controller reachable from the host
path) that self-reconfiguration would need. Until a future spec version
lifts that deferral, **JTAG via `hw_server` is the only sanctioned
on-device PR-load mechanism**, and no AC may assume otherwise. Practically:
every partial load in this phase is an out-of-band operation through the
same `hw_server` connection used for bring-up, not something the running
QDMA/PCIe path can trigger on its own.

## 7. Honest caveats, restated

- Every on-device AC-2b clause **SKIPs, never PASSes**, until
  `pyro.device.probe_device` actually returns `(True, …)` against real
  hardware (R71/R83). This document describes the procedure; it does not
  claim the procedure has been executed successfully yet.
- The PR shell build (`open-nic-shell` @ `ce85c8d` + the `pyro` plugin,
  Vivado 2025.2, at `/usr/local/cad/gn262/pyro/open-nic-shell`) was still
  in progress as this guide was written, **on zanetti**. The artifact set in §2
  describes what the build is expected to produce. **Whether that build ever
  completed is unknown**, and zanetti is not reachable from nf-server06 — so as
  of 2026-07-13 it is not established that a PYRO full-shell bitstream exists
  anywhere. Establishing that is the pacing item for all of §3.
- On nf-server06 the board carries its **factory golden image** (`10ee:d004`),
  not the stock 2022.2 OpenNIC shell it carried on zanetti. Flashing the PYRO
  PR shell (§3) has not happened, `CAP_NET_RAW` has not been granted to a
  test-runner process (§4), and the probe (§5) has never been run against real
  hardware.
- **The toolchain did not move with the card.** Vivado is not installed on
  nf-server06, no Xilinx license is present, and the shell source tree is
  absent. §3 cannot be executed here until at minimum a full-shell `.bit` and a
  Vivado (or Lab Edition / hardware-server) install exist on this host.
- JTAG programmability is a confirmed **non-blocker in principle** (verified
  working on zanetti 2026-07-06; the FT4232H cable is attached to nf-server06
  and the board is the owner's own, so reprogramming is permitted) — but
  "non-blocker" describes permission and mechanism availability, not that the
  sequence has been run to completion, and JTAG has **not** been exercised on
  nf-server06 yet.
- The zanetti PCIe-fatal-on-live-reconfiguration hazard (`scripts/README.md`)
  is **unresolved, not disproven**, for this host. It has been *elected* to be
  treated as R740-specific; that election is recorded as the explicit
  `PYRO_FLASH_ALLOW_LIVE_PCIE=1` override in `flash_u250.sh`, not as a finding.
