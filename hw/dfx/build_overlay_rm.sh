#!/usr/bin/env bash
# build_overlay_rm.sh — build the A5 overlay child partial against a
# locked static DCP.
#
# Usage:
#   hw/dfx/build_overlay_rm.sh [--static <locked.dcp>] [--out <dir>]
#                              [--tag <tag>] [--mac]
#
# --mac (PYRO MAC contract): build the MULTI-PROGRAM child — P0 = the
# overlay engine exactly as without the flag, P1 = the SipHash-2-4 MAC
# digest program (pyro_mac_engine_top + pyro_mac_engine + pyro_siphash
# from hw/rtl, concatenated into mac.v and read alongside).  Use a
# distinct --tag so the partial can never be mistaken for the classic
# overlay child.
#
# Defaults target the PRODUCTION build tree; the OQ-2 wiretap link is
#   hw/dfx/build_overlay_rm.sh \
#       --static hw/dfx/build-wiretap/dcp/static_routed_locked.dcp \
#       --out hw/dfx/build-wiretap --tag overlay_wire
#
# Why this exists: R82b — ANY static rebuild invalidates every cached
# partial, so re-linking the overlay child is a recurring operation,
# and the driver that produced the original overlay_engine_partial.bit
# (2026-07-30, "overlay-pr") was never checked in.  This is that
# driver, reconstructed: generate the wrapper RTL (wire-scan capable
# since PYRO v2.8.0/R78.13), synthesize pyro_rp out of context, link
# in context via build_rm.tcl, and pr_verify the result (R82c —
# MANDATORY before any partial claims pr_bitstream).
#
# Guards carried from scripts/overlay_ooc_timing.tcl: the synthesized
# netlist must still contain the engine's memories (>= 40 URAM), or
# the thing being linked is a trimmed stub and any timing number from
# it is meaningless.  OOC WNS is reported but NOT gated — for this
# design OOC is not a bound on the in-context number (see the
# 2026-07-30 notebook entry); build_rm's in-context WNS is the gate.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
STATIC="${REPO}/hw/dfx/build/dcp/static_routed_locked.dcp"
OUT="${REPO}/hw/dfx/build"
TAG="overlay_engine"
MAC=0

while [ $# -gt 0 ]; do
  case "$1" in
    --static) STATIC="$2"; shift 2 ;;
    --out)    OUT="$2";    shift 2 ;;
    --tag)    TAG="$2";    shift 2 ;;
    --mac)    MAC=1;       shift   ;;
    *) echo "ERROR: unknown arg '$1'" >&2; exit 1 ;;
  esac
done

[ -f "$STATIC" ] || { echo "ERROR: no locked static: $STATIC"; exit 1; }

: "${PYRO_VIVADO_DIR:=/usr/local/cad/2025.2/Vivado}"
[ -f "$PYRO_VIVADO_DIR/settings64.sh" ] || {
  echo "ERROR: no Vivado at $PYRO_VIVADO_DIR" >&2; exit 1; }
export PYTHONPATH="${PYTHONPATH:-}"
source "$PYRO_VIVADO_DIR/settings64.sh"
export TERM=xterm

WORK="${OUT}/_overlay_rm"
mkdir -p "$WORK"

echo "=== 1. generate the overlay child RTL (wire-scan wrapper) ==="
"${REPO}/.venv-pyro/bin/python3" - "$WORK" "$MAC" <<'PY'
import sys
sys.path.insert(0, "/home/gnn/Repos/Yale/stringy-fpga")
import pyro.hdl.rp_wrapper as w
work = sys.argv[1]
mac = sys.argv[2] == "1"
rtl = w.generate_rp_child("0a5e000100000000000000000000a5e1",
                          max_frame_bytes=9600,
                          engine_backpressure=True,
                          mac_program=mac)
open(work + "/rp.v", "w").write(rtl)
eng = ""
for name in ("pyro_overlay_engine.v", "pyro_circuit_overlay_top.v"):
    eng += open("/home/gnn/Repos/Yale/stringy-fpga/hw/rtl/" + name).read()
open(work + "/eng.v", "w").write(eng)
if mac:
    # PYRO MAC: the P1 engine sources ride alongside (fail loud when
    # a file is missing -- the engine is a separate deliverable).
    src = ""
    for p in w.mac_engine_sources():
        src += open(p).read() + "\n"
    open(work + "/mac.v", "w").write(src)
    print("mac.v %d chars (%s)" % (len(src),
                                   ", ".join(w.MAC_ENGINE_FILES)))
print("rp.v %d chars (wire_frame x%d), eng.v %d chars"
      % (len(rtl), rtl.count("wire_frame"), len(eng)))
PY

echo "=== 2. OOC synth of pyro_rp (engine + wrapper) ==="
MACREAD=""
[ "$MAC" = "1" ] && MACREAD="read_verilog $WORK/mac.v"
cat > "$WORK/_ooc.tcl" <<TCL
read_verilog $WORK/eng.v
${MACREAD}
read_verilog $WORK/rp.v
synth_design -top pyro_rp -part xcu250-figd2104-2L-e \
    -mode out_of_context
create_clock -period 4.000 -name clk [get_ports clk]
set ur [llength [get_cells -hier -filter {PRIMITIVE_TYPE =~ *URAM*}]]
set br [llength [get_cells -hier -filter \
    {PRIMITIVE_TYPE =~ BLOCKRAM.BRAM.*}]]
puts "OVERLAY_RM_URAM = \$ur"
puts "OVERLAY_RM_BRAM = \$br"
if {\$ur < 40} {
  puts "OVERLAY_RM_TRIMMED: URAM \$ur < 40 -- the datapath was deleted;"
  puts "refusing to link a stub (see scripts/overlay_ooc_timing.tcl)"
  exit 1
}
report_utilization -file $WORK/${TAG}_ooc_util.rpt
write_checkpoint -force $WORK/${TAG}_rm.dcp
puts "OVERLAY_RM_OOC_DONE"
TCL
vivado -mode batch -nojournal -notrace -source "$WORK/_ooc.tcl" \
  -log "$WORK/_ooc.log" -journal /dev/null | tee "$WORK/_ooc.out"
grep -q OVERLAY_RM_OOC_DONE "$WORK/_ooc.out" || {
  echo "OOC synth failed"; exit 1; }

echo "=== 3. in-context link vs locked static (build_rm.tcl) ==="
vivado -mode batch -nojournal -notrace \
  -source "${REPO}/hw/dfx/build_rm.tcl" \
  -log "$WORK/_link.log" -journal /dev/null \
  -tclargs "$STATIC" "$WORK/${TAG}_rm.dcp" "$TAG" "$OUT" \
  | tee "$WORK/_link.out"
grep -q "BUILD_RM_${TAG}_TIMING_MET" "$WORK/_link.out" || {
  echo "IN-CONTEXT TIMING FAILED — see $WORK/_link.out"; exit 1; }

echo "=== 4. pr_verify (R82c, mandatory) ==="
vivado -mode batch -nojournal -notrace \
  -source "${REPO}/hw/dfx/pr_verify.tcl" \
  -log "$WORK/_verify.log" -journal /dev/null \
  -tclargs "$OUT" | tee "$WORK/_verify.out"
grep -q "PR_VERIFY_FAIL" "$WORK/_verify.out" && {
  echo "PR_VERIFY FAILED"; exit 1; }
grep -q "PR_VERIFY_OK ${TAG}" "$WORK/_verify.out" || {
  echo "pr_verify did not cover ${TAG}"; exit 1; }

echo "OVERLAY_RM_COMPLETE: ${OUT}/partials/${TAG}.bit"
