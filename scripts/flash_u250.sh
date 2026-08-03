#!/usr/bin/env bash
# Flash a full bitstream into the U250 QSPI (PERSISTENT, survives power cycle).
# Recipe matches an683's known-good corundum AU250 flow (mt25qu01g, SPIx4,
# user image @0x01002000). The factory GOLDEN image at 0x0 is NOT touched ->
# golden multiboot loads this user image at power-on, and a bad user image is
# still JTAG-recoverable. Nothing can be bricked.
#
# flash_u250.sh mcs   [bit] [mcs]  -> generate .mcs/.prm only (fast, no
# hardware)
# flash_u250.sh flash [bit] [mcs]  -> generate (if needed) AND write QSPI over
# JTAG (~18 min)
#
# ---------------------------------------------------------------------------
# HOST RETARGET, 2026-07-13: this script was written for `zanetti` (Dell R740),
# where the U250 sat at 0000:af:00.0 behind root port ae:00.0 in PCIe Slot 4.
# The card now lives in `nf-server06` (Supermicro X99), at 0000:02:00.0 behind
# root port 0000:00:02.0, physical slot 2. Every BDF below follows the card;
# override with PYRO_FLASH_BDF if it moves again.
#
# !!! SAFETY (see ~/Repos/Yale/ebpf-os/docs/fpga-bs.md and scripts/README.md)
# !!! On zanetti, ANY JTAG reconfiguration while the card's PCIe slot was live
# !!! hard-crashed the host: uncorrectable FATAL at the root port, at
# !!! platform-firmware level. OS-level driver unbind or /sys/.../remove did
# !!! NOT prevent it. It happened four times (3x June 2026, again 2026-07-06).
# !!! The fix there was to disable the slot in BIOS via iDRAC9 before flashing.
# !!!
# !!! nf-server06 HAS NO iDRAC and no known equivalent slot-disablement path.
# !!! Whether the crash was specific to the R740's root port is UNVERIFIED.
# !!! The operator has elected to treat it as R740-specific and flash with the
# !!! card live. This script therefore still refuses by default, but the
# !!! refusal can be overridden explicitly:
# !!!
# !!!     PYRO_FLASH_ALLOW_LIVE_PCIE=1 flash_u250.sh flash ...
# !!!
# !!! That override is the whole risk decision. Do not add it to a wrapper or
# !!! a shell rc where it stops being a conscious act. If the crash was NOT
# !!! R740-specific, this reboots the host mid-erase.
# ---------------------------------------------------------------------------
set -o pipefail
MODE="${1:-mcs}"

# The card's PCIe address. Was 0000:af:00.0 on zanetti; 0000:02:00.0 here.
BDF="${PYRO_FLASH_BDF:-0000:02:00.0}"

# Vivado install. On zanetti this was /usr/local/cad/2025.2/Vivado (the R70a
# pin, == PINNED_VIVADO_DIR in pyro/synth/toolchain.py). Not yet installed on
# nf-server06 as of 2026-07-13 -- checked below rather than assumed.
VIVADO_DIR="${PYRO_VIVADO_DIR:-/usr/local/cad/2025.2/Vivado}"

# Full-shell artifacts. The zanetti default
# (/usr/local/cad/gn262/pyro/open-nic-shell/build/au250_pyro_pr/pr) does not
# exist on nf-server06 -- the PYRO shell build tree never came across with the
# card. There is no defensible default here, so BIT must be named explicitly
# (argument, or PYRO_SHELL_BIT) rather than silently resolving to nothing.
BIT="${2:-${PYRO_SHELL_BIT:-}}"
if [ -z "$BIT" ]; then
  echo "ERROR: no bitstream given."
  echo "  usage: flash_u250.sh {mcs|flash} <full_shell.bit> [out.mcs]"
  echo "  or:    PYRO_SHELL_BIT=/path/to/open_nic_shell.bit flash_u250.sh ..."
  echo
  echo "  NOTE: this must be a FULL static-shell image, not a partial. The card"
  echo "  is currently on its golden fallback image (10ee:d004), so there is no"
  echo "  static shell for a partial bitstream to load into."
  exit 1
fi
MCS="${3:-${BIT%.bit}.mcs}"
PRM="${MCS%.mcs}.prm"

[ -f "$BIT" ] || { echo "ERROR: bitstream not found: $BIT"; exit 1; }

# Interlock, checked BEFORE any slow work so a refusal is immediate. On zanetti
# this checked a hardcoded 0000:af:00.0 -- which on nf-server06 never exists, so
# the check passed vacuously and would have flashed a live card. It now follows
# $BDF, so it actually fires.
if [ "$MODE" = "flash" ] && [ -e "/sys/bus/pci/devices/$BDF" ]; then
  if [ "${PYRO_FLASH_ALLOW_LIVE_PCIE:-0}" != "1" ]; then
    echo "REFUSING TO FLASH: $BDF is still enumerated on PCIe."
    echo
    echo "  On zanetti (R740), JTAG-reconfiguring a live card hard-crashed the"
    echo "  host at platform-firmware level -- four times. nf-server06 has no"
    echo "  iDRAC slot-disablement equivalent, and whether that crash was"
    echo "  specific to the R740's root port is UNVERIFIED."
    echo
    echo "  To flash anyway, accepting that risk explicitly:"
    echo "      PYRO_FLASH_ALLOW_LIVE_PCIE=1 $0 flash \"$BIT\""
    exit 3
  fi
  echo "!!! WARNING: $BDF is LIVE on PCIe and PYRO_FLASH_ALLOW_LIVE_PCIE=1."
  echo "!!! Proceeding. If the zanetti crash was not R740-specific, this host"
  echo "!!! will take an uncorrectable PCIe fatal and reset mid-erase."
  echo "!!! A reset during QSPI erase leaves an invalid user image"
  echo "!!! -- recoverable"
  echo "!!! (golden at 0x0 is untouched), but you will be re-flashing"
  echo "!!! from JTAG."
  sleep 5
fi

[ -f "$VIVADO_DIR/settings64.sh" ] || {
  echo "ERROR: no Vivado at $VIVADO_DIR (settings64.sh missing)."
  echo "  Set PYRO_VIVADO_DIR, or install Vivado 2025.2 (the R70a pin)."
  exit 1
}
# settings64.sh references $PYTHONPATH unguarded; harmless under set -u callers.
export PYTHONPATH="${PYTHONPATH:-}"
source "$VIVADO_DIR/settings64.sh"
export TERM=xterm

if [ ! -f "$MCS" ] || [ "$BIT" -nt "$MCS" ]; then
  echo "=== STEP 1: generate .mcs/.prm (SPIx4, size 128, user image"
  echo "  @0x01002000) ==="
  TCLF=$(mktemp --suffix=.tcl)
  cat > "$TCLF" <<TCL
write_cfgmem -force -format mcs -size 128 -interface SPIx4 \\
  -loadbit {up 0x01002000 $BIT} -checksum -file $MCS
TCL
  vivado -nojournal -nolog -mode batch -source "$TCLF" 2>&1 | \
    grep -iE 'cfgmem|ERROR|Writing|Bitstream|overflow|size' | head -20
  rm -f "$TCLF"
  if [ ! -f "$MCS" ]; then
    echo "MCS_FAILED (bitstream missing SPIx4 config? see gen_bit_spi.sh)"
    exit 2
  fi
else
  echo "=== STEP 1: existing .mcs is newer than .bit; reusing it ==="
fi
echo "MCS_OK: $(ls -lh "$MCS" | awk '{print $5}')  $MCS"

if [ "$MODE" != "flash" ]; then
  echo "=== mcs-only mode; NOT writing flash. Re-run with 'flash' to program"
  echo "  QSPI. ==="
  exit 0
fi

[ -f "$PRM" ] || { echo "ERROR: prm not found next to mcs: $PRM"; exit 1; }

echo "=== STEP 2: write QSPI over JTAG (erase+program+verify, user image"
echo "  @0x01002000) ==="
TCLF=$(mktemp --suffix=.tcl)
cat > "$TCLF" <<TCL
open_hw_manager
connect_hw_server -url localhost:3121
current_hw_target [lindex [get_hw_targets] 0]
open_hw_target
current_hw_device [lindex [get_hw_devices] 0]
refresh_hw_device -update_hw_probes false [current_hw_device]
create_hw_cfgmem -hw_device [current_hw_device] [lindex [get_cfgmem_parts \
  {mt25qu01g-spi-x1_x2_x4}] 0]
current_hw_cfgmem -hw_device [current_hw_device] [get_property \
  PROGRAM.HW_CFGMEM [current_hw_device]]
set_property PROGRAM.FILES [list "$MCS"] [current_hw_cfgmem]
set_property PROGRAM.PRM_FILES [list "$PRM"] [current_hw_cfgmem]
set_property PROGRAM.ERASE 1 [current_hw_cfgmem]
set_property PROGRAM.CFG_PROGRAM 1 [current_hw_cfgmem]
set_property PROGRAM.VERIFY 1 [current_hw_cfgmem]
set_property PROGRAM.CHECKSUM 0 [current_hw_cfgmem]
set_property PROGRAM.ADDRESS_RANGE {use_file} [current_hw_cfgmem]
set_property PROGRAM.UNUSED_PIN_TERMINATION {pull-none} [current_hw_cfgmem]
create_hw_bitstream -hw_device [current_hw_device] [get_property \
  PROGRAM.HW_CFGMEM_BITFILE [current_hw_device]]
program_hw_devices [current_hw_device]
refresh_hw_device [current_hw_device]
program_hw_cfgmem -hw_cfgmem [current_hw_cfgmem]
puts "FLASH_DONE"
close_hw_target
disconnect_hw_server
TCL
vivado -nojournal -nolog -mode batch -source "$TCLF" 2>&1 | \
  grep -iE \
    -e 'Program|Erasing|Erase|Writing|Verify|FLASH_DONE|ERROR' \
    -e 'WARNING.*cfgmem|Flash' | tail -40
rc=$?
rm -f "$TCLF"
echo "=== flash step finished (rc=$rc) ==="
echo "NEXT: COLD power cycle the host (full AC-off power cycle, not a warm"
echo "  reboot)."
echo "      The QSPI image must load before BIOS enumeration for the new"
echo "  endpoint"
echo "      to be present at bus scan. Then confirm:  lspci -d 10ee: -nn"
