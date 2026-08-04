# *************************************************************************
# OQ-2 wiretap spike - box_250mhz plugin build hook.
#
# Same contract as hw/pyro_plugin/build_box_250mhz.tcl (sourced by
# open-nic-shell/script/build.tcl with cwd == this directory).  Reuses
# the production RTL from ../pyro_plugin — the only local difference
# is user_plugin_250mhz_inst.vh setting WIRE_TAP=1 — plus the two
# spike modules (arbiter, TX generator).
#
# docs/studies/wire-rate-spike.md is the design record.
# *************************************************************************
if {$num_qdma > 1} {
    source box_250mhz/box_250mhz_axis_switch.tcl
}

read_verilog -quiet -sv [file normalize ../src/pyro_rp_stub.v]
read_verilog -quiet -sv [file normalize ../pyro_plugin/pyro_250mhz.sv]
read_verilog -quiet -sv \
    [file normalize ../pyro_plugin/pyro_axis_skid.sv]
read_verilog -quiet -sv \
    [file normalize ../pyro_plugin/pyro_axis_wire_arb.sv]
read_verilog -quiet -sv \
    [file normalize ../pyro_plugin/pyro_wire_tx_gen.sv]
