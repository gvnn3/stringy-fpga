# build_static.tcl <project_xpr> <out_dir> <id_stub_dcp> <build16>
#
# Drive the DFX static implementation on a freshly-synthesized OpenNIC project:
#   open_run synth_1  -> mark pyro_rp HD.RECONFIGURABLE -> link the ID stub as the
#   default child -> floorplan -> opt/place/route -> config0 -> black-box + lock.
#
# Outputs (the R82d artifact set):
#   <out>/static_full_config0.dcp    routed static + ID-stub child (pr_verify reference)
#   <out>/static_routed_locked.dcp   locked static; the substrate every partial is
#                                    implemented against, bit-identical across configs (R82b)
#   <out>/open_nic_shell.bit         FULL flash image (static + ID stub), SPIx4 flash-boot
#
# Lessons inherited from the ebpf-os U250 DFX flow (integration/dfx) -- do not
# "simplify" these away:
#   * `open_run synth_1` FROM THE PROJECT is required. It links the ~30 OOC IPs so
#     that only pyro_rp is left a black box. A bare `open_checkpoint` on the synth
#     .dcp leaves the IPs unlinked and SEGFAULTS Vivado.
#   * Re-query `get_cells` FRESH after every netlist edit. Cached cell handles go
#     stale and crash the tool.
#   * Black-box a cell before `read_checkpoint -cell` if it already has content.

set xpr     [lindex $argv 0]
set out     [lindex $argv 1]
set stub    [lindex $argv 2]
set build16 [lindex $argv 3]
set part    xcu250-figd2104-2L-e
file mkdir $out

# The RP cell name and pblock range come from platform_manifest.json.
source [file join [file dirname [file normalize [info script]]] floorplan.tcl]
array set MF [_read_manifest]
set RPCELL $MF(PYRO_RP,cell)
set RPRANGE $MF(PYRO_RP,range)

# Always re-query; never cache the handle.
proc rpcell {} {
  global RPCELL
  return [get_cells -hier -filter "NAME =~ *$RPCELL"]
}

# 1. Open the synthesized static (IPs linked via the project run).
open_project $xpr
if {[get_property NEEDS_REFRESH [get_runs synth_1]] || [get_property PROGRESS [get_runs synth_1]] ne "100%"} {
  reset_run synth_1
  launch_runs synth_1 -jobs 64
  wait_on_run synth_1
}
open_run synth_1

set bb [get_cells -hier -filter {IS_BLACKBOX==1}]
puts "BUILD_STATIC: initial blackboxes ([llength $bb]) = $bb"
if {[llength [rpcell]] != 1} {
  error "BUILD_STATIC: expected exactly 1 pyro_rp cell, found [llength [rpcell]] -- pf=cmac=1?"
}

# 2. Mark the RP reconfigurable and link the ID stub as the default child (R80).
if {![get_property IS_BLACKBOX [get_cells [rpcell]]]} {
  update_design -cell [get_cells [rpcell]] -black_box
}
set_property HD.RECONFIGURABLE 1 [get_cells [rpcell]]
read_checkpoint -cell [get_cells [rpcell]] $stub
puts "BUILD_STATIC: linked ID stub into [rpcell]"

# 3. Floorplan. HD.RECONFIGURABLE lives on the CELL (step 2, per UG909); the
#    pblock carries the region placement + RESET_AFTER_RECONFIG/SNAPPING_MODE.
create_pblock pblock_pyro_rp
resize_pblock pblock_pyro_rp -add $RPRANGE
add_cells_to_pblock [get_pblocks pblock_pyro_rp] [rpcell]
set_property RESET_AFTER_RECONFIG 1 [get_pblocks pblock_pyro_rp]
set_property SNAPPING_MODE        ON [get_pblocks pblock_pyro_rp]
puts "BUILD_STATIC: floorplan pyro_rp = $RPRANGE"

# 4. CDC: the QDMA-derived clock and the AXIS user clock are asynchronous (bridged
#    by async FIFOs). Group them so the manual DFX impl flow does not time those
#    crossings as synchronous -- this is what the QDMA IP's own scoped constraints
#    do, and they are not in force under a hand-driven flow.
set _qd [get_clocks -quiet clk_out1_qdma_subsystem_clk_div]
set _ax [get_clocks -quiet axis_aclk*]
if {[llength $_qd] && [llength $_ax]} {
  set_clock_groups -asynchronous -group $_qd -group $_ax
}

# 5. Implement static + initial config.
opt_design
place_design
route_design
report_timing_summary -file $out/static_timing.rpt
set wns [get_property SLACK [get_timing_paths -delay_type max -nworst 1]]
puts "BUILD_STATIC: static WNS = $wns ns"
write_checkpoint -force $out/static_full_config0.dcp

# 6. FULL flash image, with the master-SPIx4 flash-boot config the U250's QSPI
#    boot needs. Without these, write_cfgmem rejects the bitstream ([Writecfgmem
#    68-20]) and scripts/flash_u250.sh cannot build the .mcs.
set_property BITSTREAM.GENERAL.COMPRESS        TRUE     [current_design]
set_property BITSTREAM.CONFIG.CONFIGFALLBACK   ENABLE   [current_design]
set_property BITSTREAM.CONFIG.EXTMASTERCCLK_EN DISABLE  [current_design]
set_property BITSTREAM.CONFIG.CONFIGRATE       63.8     [current_design]
set_property BITSTREAM.CONFIG.SPI_32BIT_ADDR   YES      [current_design]
set_property BITSTREAM.CONFIG.SPI_BUSWIDTH     4        [current_design]
set_property BITSTREAM.CONFIG.SPI_FALL_EDGE    YES      [current_design]
set_property BITSTREAM.CONFIG.UNUSEDPIN        PULLUP   [current_design]
write_bitstream -force $out/open_nic_shell.bit
puts "BUILD_STATIC: full flash bitstream -> $out/open_nic_shell.bit (BUILD16=$build16)"

# 7. Black-box the RM + lock the static, so every partial implements in-context
#    against a bit-identical static region (R82b).
update_design -cell [rpcell] -black_box
lock_design -level routing
write_checkpoint -force $out/static_routed_locked.dcp

if {$wns >= 0} { puts "BUILD_STATIC_TIMING_MET" } else { puts "BUILD_STATIC_TIMING_FAILED" }
puts "BUILD_STATIC_DONE"
