# build_rm.tcl <locked_static_dcp> <rm_dcp> <tag> <out_dir>
#
# Implement ONE child into pyro_rp against the locked static, and write its
# PARTIAL bitstream. This is the R82 "implemented in-context against the locked
# static DCP" step -- the reason every partial is interchangeable at runtime.
#
# Unlike the ebpf-os two-RP flow, PYRO has a single RP, so there is no "filler"
# RM to keep the other partition populated: the one cell under test is the only
# reconfigurable cell in the design.
#
# Outputs:
#   <out>/full/<tag>.dcp        routed full config (the pr_verify subject)
# <out>/partials/<tag>.bit    the partial bitstream (payload_kind ==
# pr_bitstream)
# <out>/partials/<tag>.bin    raw partial, for a future ICAP/MCAP path (R85
# defers this)

set static [lindex $argv 0]
set rmdcp  [lindex $argv 1]
set tag    [lindex $argv 2]
set out    [lindex $argv 3]

file mkdir $out/partials $out/full

source [file join [file dirname [file normalize [info script]]] floorplan.tcl]
array set MF [_read_manifest]
set RPCELL $MF(PYRO_RP,cell)

proc rpcell {} {
  global RPCELL
  return [get_cells -hier -filter "NAME =~ *$RPCELL"]
}

open_checkpoint $static

if {![get_property IS_BLACKBOX [get_cells [rpcell]]]} {
  update_design -cell [get_cells [rpcell]] -black_box
}
read_checkpoint -cell [get_cells [rpcell]] $rmdcp

opt_design
place_design
route_design

report_timing_summary -file $out/full/${tag}_timing.rpt
set wns [get_property SLACK [get_timing_paths -delay_type max -nworst 1]]
puts "BUILD_RM ${tag}: WNS = $wns ns"
write_checkpoint -force $out/full/${tag}.dcp

# Partial bitstream for the RP cell only -- NOT a full device image.
write_bitstream -force -cell [get_cells [rpcell]] $out/partials/${tag}.bit
catch {
  write_cfgmem -force -format bin -interface SMAPx32 -disablebitswap \
    -loadbit "up 0x0 $out/partials/${tag}.bit" $out/partials/${tag}.bin
}

if {$wns >= 0} { puts "BUILD_RM_${tag}_TIMING_MET" } \
else { puts "BUILD_RM_${tag}_TIMING_FAILED" }
puts "BUILD_RM_${tag}_DONE"
