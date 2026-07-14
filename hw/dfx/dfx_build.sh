#!/usr/bin/env bash
# dfx_build.sh [--fast] [--jobs N]
#
# Build the PYRO Phase 2b PR shell for the Alveo U250:
#   OpenNIC static shell (pf=cmac=1) + one DFX reconfigurable partition `pyro_rp`
#   carrying the ID-stub default child, a locked static DCP, and the ID-stub
#   partial bitstream, verified with pr_verify.
#
#   --fast   OOC-synthesize the ID stub only (minutes; checks the RTL elaborates
#            and meets 250 MHz). No static build, no card needed.
#   default  full DFX build: OpenNIC project -> static place/route/lock ->
#            ID-stub partial -> pr_verify   (HOURS)
#
# Outputs land in hw/dfx/build/:
#   dcp/static_full_config0.dcp    routed static + ID stub (pr_verify reference)
#   dcp/static_routed_locked.dcp   the locked substrate for every partial (R82b)
#   dcp/open_nic_shell.bit         FULL flash image, SPIx4 -- feed to scripts/flash_u250.sh
#   partials/id_stub.bit           ID stub as a PARTIAL (return to known-good baseline)
#
# The vendored shell under third_party/open-nic-shell is NEVER modified: the build
# runs on a throwaway copy under hw/dfx/build/.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DFX="${REPO}/hw/dfx"
HWSRC="${REPO}/hw/src"
PLUGIN="${REPO}/hw/pyro_plugin"
SHELL_SRC="${REPO}/third_party/open-nic-shell"

: "${PYRO_VIVADO_DIR:=/usr/local/cad/2025.2/Vivado}"
: "${XILINXD_LICENSE_FILE:=}"
PART="xcu250-figd2104-2L-e"

FAST=0
JOBS="$(nproc)"
while [ $# -gt 0 ]; do
  case "$1" in
    --fast) FAST=1 ;;
    --jobs) JOBS="$2"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

[ -f "$PYRO_VIVADO_DIR/settings64.sh" ] || {
  echo "ERROR: no Vivado at $PYRO_VIVADO_DIR (settings64.sh missing). Set PYRO_VIVADO_DIR." >&2
  exit 1
}
# settings64.sh reads $PYTHONPATH unguarded, which aborts under `set -u`.
export PYTHONPATH="${PYTHONPATH:-}"
# shellcheck disable=SC1090
source "$PYRO_VIVADO_DIR/settings64.sh"
export TERM=xterm
command -v vivado >/dev/null || { echo "ERROR: vivado not on PATH" >&2; exit 1; }

OUT="${DFX}/build"
DCP="${OUT}/dcp"
mkdir -p "$DCP"
log() { echo "=== [pyro-dfx] $* ==="; }

# R81 BUILD16: low 16 bits of the build's Unix-epoch MINUTE count. The host does
# not require a fixed value (only the SPEC16 half is checked), but it identifies
# which build a running shell is, via ID_REPLY's static_shell_id.
BUILD16="$(printf '0x%04X' $(( ($(date +%s) / 60) & 0xFFFF )))"
log "BUILD16 = ${BUILD16}"

# ---- 1. ID stub: OOC synthesis (always; this is the --fast gate) ------------
log "OOC synth: pyro_rp ID stub @250MHz"
cat > "${OUT}/_ooc_id_stub.tcl" <<TCL
read_verilog -sv [list ${HWSRC}/pyro_id_stub.sv]
synth_design -top pyro_rp -part ${PART} -mode out_of_context \\
  -generic BUILD16=${BUILD16}
create_clock -name clk -period 4.0 [get_ports clk]
set wns [get_property SLACK [get_timing_paths -delay_type max -nworst 1]]
puts "OOC_ID_STUB_WNS = \$wns"
report_utilization -file ${OUT}/id_stub_util.rpt
write_checkpoint -force ${DCP}/pyro_id_stub.dcp
if {\$wns >= 0} { puts "OOC_ID_STUB_TIMING_MET" } else { puts "OOC_ID_STUB_TIMING_FAILED" }
TCL
vivado -mode batch -nojournal -notrace -source "${OUT}/_ooc_id_stub.tcl" \
  -log "${OUT}/_ooc_id_stub.log" -journal /dev/null | tee "${OUT}/_ooc_id_stub.out"
grep -q OOC_ID_STUB_TIMING_MET "${OUT}/_ooc_id_stub.out" || {
  echo "ID stub failed 250MHz OOC timing (or did not synthesize)"; exit 1; }

if [ "$FAST" = "1" ]; then
  log "--fast complete (ID stub synthesizes and meets 250MHz)"
  exit 0
fi

# ---- 2. OpenNIC project on a throwaway shell copy ---------------------------
log "OpenNIC static shell (pf=cmac=1, user_plugin=pyro)"
DST="${OUT}/_static/open-nic-shell"
rm -rf "${OUT}/_static"
mkdir -p "$(dirname "$DST")"
rsync -a --exclude '.git' --exclude 'build/' "${SHELL_SRC}/" "$DST/"

# 2025.2 synthesizes the top strictly and rejects OpenNIC's invalid $fatal("...")
# (needs $fatal(1, "...")). Rewrite them on the throwaway copy only.
"${DFX}/fix_opennic_2025p2.sh" "$DST"

( cd "$DST/script" && vivado -mode batch -notrace -source build.tcl -tclargs \
    -board au250 -jobs "$JOBS" -synth_ip 1 -use_phys_func 1 \
    -num_phys_func 1 -num_cmac_port 1 -impl 0 -rebuild 1 \
    -user_plugin "${PLUGIN}" ) > "${OUT}/_proj.log" 2>&1

XPR="$DST/build/au250/open_nic_shell/open_nic_shell.xpr"
[ -f "$XPR" ] || { echo "ERROR: OpenNIC project not created; see ${OUT}/_proj.log"; exit 1; }

# ---- 3. DFX static: assemble, floorplan, place/route, lock ------------------
log "build_static (DFX assembly, floorplan, place/route, lock, full .bit)"
vivado -mode batch -nojournal -notrace -source "${DFX}/build_static.tcl" \
  -tclargs "$XPR" "$DCP" "${DCP}/pyro_id_stub.dcp" "$BUILD16" \
  -log "${OUT}/_static.log" -journal /dev/null | tee "${OUT}/_static.out"
grep -q BUILD_STATIC_DONE "${OUT}/_static.out" || { echo "build_static did not finish"; exit 1; }

# NOTE (inherited, expect this): the full OpenNIC shell does not fully close
# timing on 2025.2 -- residual setup violations live inside OpenNIC's own CMAC IP
# and the unused box_322mhz. Those are outside pyro_rp and do not block partial
# generation or pr_verify. What matters is that the pyro_rp region and box_250mhz
# close; check BUILD_STATIC WNS and static_timing.rpt.

# ---- 4. ID stub as a PARTIAL + pr_verify ------------------------------------
log "ID-stub partial bitstream"
vivado -mode batch -nojournal -notrace -source "${DFX}/build_rm.tcl" \
  -tclargs "${DCP}/static_routed_locked.dcp" "${DCP}/pyro_id_stub.dcp" "id_stub" "${OUT}" \
  -log "${OUT}/_rm_id_stub.log" -journal /dev/null | tee "${OUT}/_partials.out"

log "pr_verify"
vivado -mode batch -nojournal -notrace -source "${DFX}/pr_verify.tcl" -tclargs "${OUT}" \
  -log "${OUT}/_prverify.log" -journal /dev/null | tee "${OUT}/_prverify.out"
grep -q PR_VERIFY_ALL_OK "${OUT}/_prverify.out" || { echo "pr_verify reported failures"; exit 1; }

log "FULL DFX BUILD COMPLETE"
echo "  full flash image : ${DCP}/open_nic_shell.bit"
echo "  locked static    : ${DCP}/static_routed_locked.dcp"
echo "  ID-stub partial  : ${OUT}/partials/id_stub.bit"
echo
echo "NEXT: flash the full image (see scripts/README.md -- note the live-PCIe hazard):"
echo "  PYRO_FLASH_ALLOW_LIVE_PCIE=1 scripts/flash_u250.sh flash ${DCP}/open_nic_shell.bit"
