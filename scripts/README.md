# U250 programming scripts

Adapted July 2026 from `~/Repos/Yale/ebpf-os/scripts/` (the June 2026
reference_nic saga; full writeup in `ebpf-os/docs/fpga-bs.md`).

## Which host

**The card moved.** These scripts were written for **zanetti** (Dell R740),
where the U250 sat at `0000:af:00.0` / `0000:af:00.1` behind root port
`ae:00.0`, in PCIe Slot 4, with Vivado 2025.2 at `/usr/local/cad/2025.2/Vivado`
and the PYRO shell build tree at `/usr/local/cad/gn262/pyro/open-nic-shell`.

As of **2026-07-13** the card is in **nf-server06** (Supermicro X99, Xeon E5 v4):

| | zanetti (R740) | nf-server06 (now) |
|---|---|---|
| Card BDF | `0000:af:00.0` / `.1` | **`0000:02:00.0`** (single fn — golden image) |
| Root port | `ae:00.0` | `0000:00:02.0` |
| Physical slot | 4 | 2 |
| Netdevs (post-flash) | `enp175s0f0` / `f1` | expect **`enp2s0f0` / `f1`** |
| BMC | iDRAC9 @ 10.66.3.9 | ASPEED (no iDRAC; **no slot-disablement path**) |
| Vivado | `/usr/local/cad/2025.2/Vivado` | **not installed** |
| PYRO shell build tree | `/usr/local/cad/gn262/pyro/…` | **not present** |

The card is currently running its **factory golden image** (`10ee:d004`), i.e.
there is no user image in QSPI at `0x01002000` — so there is no static shell,
and a *partial* bitstream has nothing to load into. Bring-up needs a **full**
shell image.

Note that `specs/python-regex-offload.md` still declares `af:00.0` (F2) and
`enp175s0f0` (F3) as normative facts, and `pyro/_route.py` / `pyro/device.py`
still default `PYRO_DEVICE_IFACE` to `enp175s0f0`. Those are now false for this
host and need a spec decision; until then, **override the iface explicitly**:

```bash
export PYRO_DEVICE_IFACE=enp2s0f0    # once the shell is flashed and onic binds
```

## The hazard

Any JTAG reconfiguration of the card while its PCIe slot was live crashed
**zanetti**: DONE asserts, the endpoint identity swaps under the running
system, and the root port raises an uncorrectable FATAL at the
platform-firmware level — the server resets itself. This happened three times
in June 2026 and again on 2026-07-06. **OS-level driver unbind or
`echo 1 > /sys/bus/pci/devices/<bdf>/remove` does NOT prevent it.**

**Whether this is specific to the R740's root port is UNVERIFIED.**
nf-server06 has no iDRAC and no known slot-disablement equivalent, so the
zanetti workaround does not transfer. The operator has elected (2026-07-13) to
treat the crash as R740-specific and flash with the card live. `flash_u250.sh`
still refuses by default; that decision is expressed as an explicit override:

```bash
PYRO_FLASH_ALLOW_LIVE_PCIE=1 ./flash_u250.sh flash <full_shell.bit>
```

If the crash was *not* R740-specific, this resets the host mid-erase. That is
recoverable — the factory golden image at `0x0` is never touched, so the card
falls back to golden and stays JTAG-reachable — but you will be re-flashing.

## The safe procedure

1. Bitstream must carry master-SPIx4 flash-boot config (`SPI_BUSWIDTH 4`,
   `SPI_32BIT_ADDR YES`, `CONFIGFALLBACK ENABLE`, ...). If `write_cfgmem`
   fails with `[Writecfgmem 68-20]`, re-emit from the routed checkpoint with
   `gen_bit_spi.sh <routed.dcp> <out.bit>`.
2. `flash_u250.sh mcs <full_shell.bit>` — generate `.mcs`/`.prm` (SPIx4, size
   128, user image at `0x01002000`). The factory golden image at `0x0` is never
   touched; a bad user image falls back to golden, so nothing can be bricked.
3. *(zanetti only — no equivalent on nf-server06)* Disable BIOS PCIe Slot 4 via
   iDRAC9: Configuration → BIOS Settings → Integrated Devices → Slot
   Disablement → Slot 4 = Disabled; apply at next reboot; reboot. The slot stays
   powered, so USB-FTDI JTAG still reaches the card.
4. `flash_u250.sh flash <full_shell.bit>` — erase + program + verify QSPI over
   JTAG (~18 min for a full 128 MB image). The script refuses while the card
   (`$PYRO_FLASH_BDF`, default `0000:02:00.0`) is still enumerated, unless
   `PYRO_FLASH_ALLOW_LIVE_PCIE=1`.
5. **Cold power cycle** (full AC-off, not a warm reboot). The QSPI image loads
   before BIOS enumeration, so the new endpoint is present at bus scan and no
   fatal is raised. Confirm with `lspci -d 10ee: -nn`.

## Files

- `flash_u250.sh` — steps 2 and 4. The bitstream must be named explicitly
  (argument or `PYRO_SHELL_BIT`); there is no longer a default, because the
  zanetti default path does not exist on this host.
- `gen_bit_spi.sh` — step 1 fallback: re-emit a routed `.dcp` as a bitstream
  with the SPIx4 flash-boot properties (no re-route). Still hardcodes the
  zanetti Vivado path.
- `status.tcl` — read-only JTAG health check (DONE pin, EOS, PLL lock, CRC,
  die temp/SYSMON, ILA/VIO counts). Never programs anything; safe to run
  anytime: `vivado -mode batch -source scripts/status.tcl`.
