#!/usr/bin/env bash
# Re-emit a bitstream from a routed checkpoint WITH master-SPIx4 flash-boot config
# (an683's known-good corundum AU250 recipe). No re-route; IP must already be
# license-generated. write_cfgmem refuses bitstreams without SPI_BUSWIDTH 4, so
# run this if .mcs generation errors with [Writecfgmem 68-20].
#
# Usage: gen_bit_spi.sh <routed.dcp> <out.bit>
#
# Adapted from ~/Repos/Yale/ebpf-os/scripts/gen_bit_spi.sh (June 2026 saga;
# background in ebpf-os/docs/fpga-bs.md).
set -o pipefail
DCP="$1"
OUT="$2"
[ -n "$DCP" ] && [ -n "$OUT" ] || { echo "usage: $0 <routed.dcp> <out.bit>"; exit 1; }
[ -f "$DCP" ] || { echo "ERROR: routed dcp not found: $DCP"; exit 1; }

source /usr/local/cad/2025.2/Vivado/settings64.sh
export TERM=xterm
export XILINXD_LICENSE_FILE=/home/gn262/.Xilinx/Xilinx.lic

TCLF=$(mktemp --suffix=.tcl)
cat > "$TCLF" <<TCL
open_checkpoint $DCP
set_property BITSTREAM.GENERAL.COMPRESS         true     [current_design]
set_property BITSTREAM.CONFIG.CONFIGFALLBACK    ENABLE   [current_design]
set_property BITSTREAM.CONFIG.EXTMASTERCCLK_EN  DISABLE  [current_design]
set_property BITSTREAM.CONFIG.CONFIGRATE        63.8     [current_design]
set_property BITSTREAM.CONFIG.SPI_32BIT_ADDR    YES      [current_design]
set_property BITSTREAM.CONFIG.SPI_BUSWIDTH      4        [current_design]
set_property BITSTREAM.CONFIG.SPI_FALL_EDGE     YES      [current_design]
set_property BITSTREAM.CONFIG.UNUSEDPIN         PULLUP   [current_design]
write_bitstream -force $OUT
puts "WBIT_DONE"
TCL
echo "Writing SPIx4 bitstream from routed dcp -> $OUT"
vivado -nojournal -nolog -mode batch -source "$TCLF" 2>&1 | \
  grep -iE 'write_bitstream|WBIT_DONE|ERROR|Critical|license|Bitstream gen' | tail -30
rm -f "$TCLF"
if [ -f "$OUT" ]; then ls -lh "$OUT"; echo "SPIX4_BIT_PRESENT"; else echo "NO_BIT"; exit 2; fi
