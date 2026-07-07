#!/usr/bin/env bash
# Flash a full bitstream into the U250 QSPI (PERSISTENT, survives power cycle).
# Recipe matches an683's known-good corundum AU250 flow (mt25qu01g, SPIx4,
# user image @0x01002000). The factory GOLDEN image at 0x0 is NOT touched ->
# golden multiboot loads this user image at power-on, and a bad user image is
# still JTAG-recoverable. Nothing can be bricked.
#
#   flash_u250.sh mcs   [bit] [mcs]  -> generate .mcs/.prm only (fast, no hardware)
#   flash_u250.sh flash [bit] [mcs]  -> generate (if needed) AND write QSPI over JTAG (~18 min)
#
# Defaults are the PYRO PR shell artifacts.
#
# !!! SAFETY (see ~/Repos/Yale/ebpf-os/docs/fpga-bs.md and memory
# !!! u250-safe-flash-procedure) — ANY JTAG reconfiguration while PCIe Slot 4
# !!! is live hard-crashes this host (uncorrectable FATAL at root port
# !!! ae:00.0, platform-firmware level). OS-level driver unbind or
# !!! /sys/.../remove does NOT prevent it. The only proven-safe sequence:
# !!!   1. iDRAC9 (10.66.3.9): BIOS Settings -> Integrated Devices ->
# !!!      Slot Disablement -> Slot 4 = Disabled; apply at next reboot; reboot.
# !!!      (Slot stays powered; USB-FTDI JTAG still reaches the card.)
# !!!   2. Run this script in 'flash' mode.
# !!!   3. Re-enable Slot 4, then COLD power cycle via iDRAC (Power Cycle System).
# !!! 'flash' mode refuses to run while 0000:af:00.0 is still enumerated.
# !!! Never use volatile program_hw_devices on this host — no safe path exists.
set -o pipefail
MODE="${1:-mcs}"

PYRO_PR=/usr/local/cad/gn262/pyro/open-nic-shell/build/au250_pyro_pr/pr
BIT="${2:-$PYRO_PR/open_nic_shell.bit}"
MCS="${3:-$PYRO_PR/open_nic_shell.mcs}"
PRM="${MCS%.mcs}.prm"

[ -f "$BIT" ] || { echo "ERROR: bitstream not found: $BIT"; exit 1; }
source /usr/local/cad/2025.2/Vivado/settings64.sh
export TERM=xterm

if [ ! -f "$MCS" ] || [ "$BIT" -nt "$MCS" ]; then
  echo "=== STEP 1: generate .mcs/.prm (SPIx4, size 128, user image @0x01002000) ==="
  TCLF=$(mktemp --suffix=.tcl)
  cat > "$TCLF" <<TCL
write_cfgmem -force -format mcs -size 128 -interface SPIx4 \\
  -loadbit {up 0x01002000 $BIT} -checksum -file $MCS
TCL
  vivado -nojournal -nolog -mode batch -source "$TCLF" 2>&1 | \
    grep -iE 'cfgmem|ERROR|Writing|Bitstream|overflow|size' | head -20
  rm -f "$TCLF"
  if [ ! -f "$MCS" ]; then echo "MCS_FAILED (bitstream missing SPIx4 config? see gen_bit_spi.sh)"; exit 2; fi
else
  echo "=== STEP 1: existing .mcs is newer than .bit; reusing it ==="
fi
echo "MCS_OK: $(ls -lh "$MCS" | awk '{print $5}')  $MCS"

if [ "$MODE" != "flash" ]; then
  echo "=== mcs-only mode; NOT writing flash. Re-run with 'flash' to program QSPI. ==="
  exit 0
fi

# Interlock: if the card is still enumerated on PCIe, BIOS Slot 4 has not been
# disabled and flashing WILL crash the host. Refuse.
if [ -e /sys/bus/pci/devices/0000:af:00.0 ]; then
  echo "REFUSING TO FLASH: 0000:af:00.0 is still enumerated on PCIe."
  echo "Disable BIOS Slot 4 via iDRAC (10.66.3.9) and reboot first — see header."
  exit 3
fi
[ -f "$PRM" ] || { echo "ERROR: prm not found next to mcs: $PRM"; exit 1; }

echo "=== STEP 2: write QSPI over JTAG (erase+program+verify, user image @0x01002000) ==="
TCLF=$(mktemp --suffix=.tcl)
cat > "$TCLF" <<TCL
open_hw_manager
connect_hw_server -url localhost:3121
current_hw_target [lindex [get_hw_targets] 0]
open_hw_target
current_hw_device [lindex [get_hw_devices] 0]
refresh_hw_device -update_hw_probes false [current_hw_device]
create_hw_cfgmem -hw_device [current_hw_device] [lindex [get_cfgmem_parts {mt25qu01g-spi-x1_x2_x4}] 0]
current_hw_cfgmem -hw_device [current_hw_device] [get_property PROGRAM.HW_CFGMEM [current_hw_device]]
set_property PROGRAM.FILES [list "$MCS"] [current_hw_cfgmem]
set_property PROGRAM.PRM_FILES [list "$PRM"] [current_hw_cfgmem]
set_property PROGRAM.ERASE 1 [current_hw_cfgmem]
set_property PROGRAM.CFG_PROGRAM 1 [current_hw_cfgmem]
set_property PROGRAM.VERIFY 1 [current_hw_cfgmem]
set_property PROGRAM.CHECKSUM 0 [current_hw_cfgmem]
set_property PROGRAM.ADDRESS_RANGE {use_file} [current_hw_cfgmem]
set_property PROGRAM.UNUSED_PIN_TERMINATION {pull-none} [current_hw_cfgmem]
create_hw_bitstream -hw_device [current_hw_device] [get_property PROGRAM.HW_CFGMEM_BITFILE [current_hw_device]]
program_hw_devices [current_hw_device]
refresh_hw_device [current_hw_device]
program_hw_cfgmem -hw_cfgmem [current_hw_cfgmem]
puts "FLASH_DONE"
close_hw_target
disconnect_hw_server
TCL
vivado -nojournal -nolog -mode batch -source "$TCLF" 2>&1 | \
  grep -iE 'Program|Erasing|Erase|Writing|Verify|FLASH_DONE|ERROR|WARNING.*cfgmem|Flash' | tail -40
rc=$?
rm -f "$TCLF"
echo "=== flash step finished (rc=$rc) ==="
echo "NEXT: re-enable BIOS Slot 4 via iDRAC, then COLD power cycle (Power Cycle System)."
