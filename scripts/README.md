# U250 programming scripts

Adapted July 2026 from `~/Repos/Yale/ebpf-os/scripts/` (the June 2026
reference_nic saga; full writeup in `ebpf-os/docs/fpga-bs.md`). These encode
the **only proven-safe way to reprogram the U250 on zanetti**.

## The hazard

Any JTAG reconfiguration of the card while PCIe Slot 4 is live crashes the
host: DONE asserts, the endpoint identity swaps under the running system, and
root port `ae:00.0` raises an uncorrectable FATAL at the platform-firmware
level — the server resets itself. This happened three times in June 2026 and
again on 2026-07-06. **OS-level driver unbind or
`echo 1 > /sys/bus/pci/devices/0000:af:00.0/remove` does NOT prevent it.**
There is no known safe way to run volatile `program_hw_devices` (config-RAM
load) on this machine.

## The safe procedure

1. Bitstream must carry master-SPIx4 flash-boot config (`SPI_BUSWIDTH 4`,
   `SPI_32BIT_ADDR YES`, `CONFIGFALLBACK ENABLE`, ...). If `write_cfgmem`
   fails with `[Writecfgmem 68-20]`, re-emit from the routed checkpoint with
   `gen_bit_spi.sh <routed.dcp> <out.bit>`.
2. `flash_u250.sh mcs` — generate `.mcs`/`.prm` (SPIx4, size 128, user image
   at `0x01002000`). The factory golden image at `0x0` is never touched; a
   bad user image falls back to golden, so nothing can be bricked.
3. Disable BIOS PCIe Slot 4 via iDRAC9 (10.66.3.9): Configuration → BIOS
   Settings → Integrated Devices → Slot Disablement → Slot 4 = Disabled;
   apply at next reboot; reboot. The slot stays powered, so USB-FTDI JTAG
   still reaches the card.
4. `flash_u250.sh flash` — erase + program + verify QSPI over JTAG (~18 min
   for a full 128 MB image). The script refuses to run this step while
   `0000:af:00.0` is still enumerated.
5. Re-enable Slot 4, then **cold power cycle** via iDRAC (Power → Power Cycle
   System). The QSPI image loads before BIOS enumeration, so the new endpoint
   is present at bus scan and no fatal is raised.

## Files

- `flash_u250.sh` — steps 2 and 4. Defaults to the PYRO PR shell artifacts
  under `/usr/local/cad/gn262/pyro/open-nic-shell/build/au250_pyro_pr/pr/`;
  pass `<bit> [mcs]` to override.
- `gen_bit_spi.sh` — step 1 fallback: re-emit a routed `.dcp` as a bitstream
  with the SPIx4 flash-boot properties (no re-route).
- `status.tcl` — read-only JTAG health check (DONE pin, EOS, PLL lock, CRC,
  die temp/SYSMON, ILA/VIO counts). Never programs anything; safe to run
  anytime: `vivado -mode batch -source scripts/status.tcl`.
