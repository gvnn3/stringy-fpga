# *************************************************************************
# PYRO Phase 2b - box_250mhz plugin build hook.
#
# Sourced by open-nic-shell/script/build.tcl with cwd == this plugin directory
# (the -user_plugin path). Reads the PYRO box and the pyro_rp BLACK BOX.
#
# The RP's real contents are NOT read here. `pyro_rp_stub.v` is an empty module
# with the frozen R80 port list, which Vivado elaborates as a black box; the
# actual child (the ID stub, and later each per-pattern partial) is linked in by
# hw/dfx/build_static.tcl via `read_checkpoint -cell`. That is what makes the
# cell reconfigurable rather than baked in.
# *************************************************************************
if {$num_qdma > 1} {
    source box_250mhz/box_250mhz_axis_switch.tcl
}

read_verilog -quiet -sv [file normalize ../src/pyro_rp_stub.v]
read_verilog -quiet -sv pyro_250mhz.sv
# OQ-2 spike modules; instantiated only under WIRE_TAP=1 (dead code
# in the production WIRE_TAP=0 build, read so elaboration never sees
# an unresolved module in the inactive generate branch).
read_verilog -quiet -sv pyro_axis_wire_arb.sv
read_verilog -quiet -sv pyro_wire_tx_gen.sv
