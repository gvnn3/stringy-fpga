#!/usr/bin/env bash
# build_rom_rm.sh — build the S4 ROM-trie child partial against a locked
# static DCP (SNORT-PF AC-S4-1).
#
# Usage:
#   hw/dfx/build_rom_rm.sh [--static <locked.dcp>] [--out <dir>]
#                          [--tag <tag>] [--cap <bytes>]
#
# Defaults target the PRODUCTION tree; the wiretap link (the static the
# card runs while OQ-2's shell is flashed) is
#   hw/dfx/build_rom_rm.sh \
#       --static hw/dfx/build-wiretap/dcp/static_routed_locked.dcp \
#       --out hw/dfx/build-wiretap --tag rom_trie
#
# Differences from build_overlay_rm.sh, all of them the point:
#   * step 0 BUILDS THE TABLE — the full-corpus shared trie
#     (pyro.snort.shared_trie), its manifest, and the six $readmemh
#     files the engine bakes.  The SR8 fit gate closes here, before a
#     second of synthesis is spent.
#   * the engine is pyro_ac_rom_engine + a GENERATED pyro_circuit alias
#     (geometry and TABLE_ID are functions of the table build).
#   * the OOC step runs with CWD=$WORK so $readmemh resolves; the URAM
#     guard is retuned to the ROM sizing (24 at cap 16: bitmap only —
#     oidx is a BRAM ROM because UltraScale+ URAM cannot be
#     initialized, Synth 8-10226), and a second guard REFUSES the
#     build if synthesis warns that memory initial contents were
#     dropped — an uninitialized ROM would match nothing while meeting
#     timing beautifully, the exact false-pass shape
#     scripts/overlay_ooc_timing.tcl documents.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
STATIC="${REPO}/hw/dfx/build/dcp/static_routed_locked.dcp"
OUT="${REPO}/hw/dfx/build"
TAG="rom_trie"
CAP="16"

while [ $# -gt 0 ]; do
  case "$1" in
    --static) STATIC="$2"; shift 2 ;;
    --out)    OUT="$2";    shift 2 ;;
    --tag)    TAG="$2";    shift 2 ;;
    --cap)    CAP="$2";    shift 2 ;;
    *) echo "ERROR: unknown arg '$1'" >&2; exit 1 ;;
  esac
done

[ -f "$STATIC" ] || { echo "ERROR: no locked static: $STATIC"; exit 1; }

# Absolute paths, unconditionally: the OOC step runs Vivado with
# CWD=$WORK (so $readmemh resolves), which silently breaks every
# relative --static/--out the moment the cd happens.  Measured on the
# first run: Vivado started, found neither its tcl nor its log dir,
# and the wrapper pipeline reported success because tee ate the exit.
STATIC="$(readlink -f "$STATIC")"
mkdir -p "$OUT"
OUT="$(readlink -f "$OUT")"

: "${PYRO_VIVADO_DIR:=/usr/local/cad/2025.2/Vivado}"
[ -f "$PYRO_VIVADO_DIR/settings64.sh" ] || {
  echo "ERROR: no Vivado at $PYRO_VIVADO_DIR" >&2; exit 1; }
export PYTHONPATH="${PYTHONPATH:-}"
source "$PYRO_VIVADO_DIR/settings64.sh"
export TERM=xterm

WORK="${OUT}/_rom_rm"
mkdir -p "$WORK" "$OUT/partials"

echo "=== 0. build the S4 shared trie + ROM files + wrapper RTL ==="
"${REPO}/.venv-pyro/bin/python3" - "$WORK" "$CAP" "$OUT/partials/$TAG" \
    <<'PY'
import json
import sys
sys.path.insert(0, "/home/gnn/Repos/Yale/stringy-fpga")
from pyro.hdl import estimator as E
from pyro.hdl import rom_child as rc
import pyro.hdl.rp_wrapper as w
from pyro.overlay import table as otable
from pyro.snort import shared_trie as ST
from pyro.snort import triage as Tr

work, cap, manifest_base = sys.argv[1], int(sys.argv[2]), sys.argv[3]
tri = Tr.triage_file("/home/gnn/Repos/Yale/stringy-fpga/third_party/"
                     "snort3-community-rules/snort3-community.rules")
trie = ST.build_shared(tri, cap)
image = trie.image()
tr_n = sum(len(g) for g in trie.ac.goto)
outs = sum(len(o) for o in trie.ac.out)
est = E.estimate_table(trie.n_states, tr_n, outs)
print("S4 trie: %d states, %d patterns, %d rules, %d B image"
      % (trie.n_states, len(trie.slots), trie.n_rules, len(image)))
print("SR8 fit: %d/%d URAM, %d/%d BRAM36"
      % (est["uram"], est["uram_budget"],
         est["bram36"], est["bram36_budget"]))
if not (est["fits_uram"] and est["fits_bram"]):
    sys.exit("SR8 REFUSAL: the table does not fit the RP budget")

geom = rc.emit_memh(image, work)
open(work + "/alias.v", "w").write(
    rc.alias_verilog(geom, epoch=ST.ROM_EPOCH))
rtl = w.generate_rp_child(otable.strong_id(image).hex(),
                          max_frame_bytes=9600,
                          engine_backpressure=True)
open(work + "/rp.v", "w").write(rtl)
eng = open("/home/gnn/Repos/Yale/stringy-fpga/hw/rtl/"
           "pyro_ac_rom_engine.v").read()
open(work + "/eng.v", "w").write(eng)
man = trie.manifest()
with open(manifest_base + "_manifest.json", "w") as f:
    json.dump(man, f, indent=1, sort_keys=True)
print("table_id %s strong_id %s -> %s_manifest.json"
      % (man["table_id"], man["strong_id"], manifest_base))
print("S4_EXPECT_URAM=%d" % est["uram"])
PY

# The URAM floor derives from the emitted files, not from stdout
# parsing: only the bitmap lives in URAM (boot-expanded — UltraScale+
# URAM has no init, Synth 8-10226 measured here on the first run), so
# the model is uram(256, n_states) with a 3/4 margin.
EXPECT_URAM=$("${REPO}/.venv-pyro/bin/python3" - "$WORK" <<'PY'
import sys
n = sum(1 for _ in open(sys.argv[1] + "/s4_base.memh"))
print(((256 + 71) // 72) * ((n + 4095) // 4096))
PY
)
URAM_FLOOR=$(( EXPECT_URAM * 3 / 4 ))

echo "=== 2. OOC synth of pyro_rp (ROM engine + wrapper) ==="
cat > "$WORK/_ooc.tcl" <<TCL
read_verilog $WORK/eng.v
read_verilog $WORK/alias.v
read_verilog $WORK/rp.v
synth_design -top pyro_rp -part xcu250-figd2104-2L-e \
    -mode out_of_context
create_clock -period 4.000 -name clk [get_ports clk]
set ur [llength [get_cells -hier -filter {PRIMITIVE_TYPE =~ *URAM*}]]
set br [llength [get_cells -hier -filter \
    {PRIMITIVE_TYPE =~ BLOCKRAM.BRAM.*}]]
puts "ROM_RM_URAM = \$ur"
puts "ROM_RM_BRAM = \$br"
if {\$ur < $URAM_FLOOR} {
  puts "ROM_RM_TRIMMED: URAM \$ur < $URAM_FLOOR -- either the datapath"
  puts "was deleted or the initialized arrays fell back to BRAM;"
  puts "refusing to link (see scripts/overlay_ooc_timing.tcl)"
  exit 1
}
report_utilization -file $WORK/${TAG}_ooc_util.rpt
write_checkpoint -force $WORK/${TAG}_rm.dcp
puts "ROM_RM_OOC_DONE"
TCL
( cd "$WORK" && vivado -mode batch -nojournal -notrace \
    -source "$WORK/_ooc.tcl" \
    -log "$WORK/_ooc.log" -journal /dev/null | tee "$WORK/_ooc.out" )
grep -q ROM_RM_OOC_DONE "$WORK/_ooc.out" || {
  echo "OOC synth failed"; exit 1; }

# The false pass this file must not allow: timing met, memories
# present, CONTENT silently zeroed.  Synth 8-10226 is the exact
# message the first run of this script produced (ram_style=ultra on
# an initialized ROM: "URAM primitives on this device do not support
# initializations") — the design now keeps every initialized array in
# BRAM, so ANY such warning means an init was silently dropped and
# the ROM would match nothing.  Fatal, not cosmetic.
if grep -qE "Synth 8-10226" "$WORK/_ooc.log" || \
   grep -iE "initial (value|content)s?.*(ignor|not support|discard)" \
       "$WORK/_ooc.log" | grep -qiE "_mem"; then
  echo "ROM_RM_UNINITIALIZED: synthesis dropped memory initial"
  echo "contents; an empty ROM meets timing and matches nothing —"
  echo "refusing."
  exit 1
fi

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

echo "ROM_RM_COMPLETE: ${OUT}/partials/${TAG}.bit"
