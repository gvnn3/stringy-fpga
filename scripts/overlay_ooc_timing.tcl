# Out-of-context timing check for the overlay engine.
#
# Why this exists as a checked-in script rather than an ad-hoc tcl:
#
# On 2026-07-30 a run reported WNS = +0.383 ns and it was meaningless.  The
# pipeline registers d_bitmap_q/d_oidx_q had picked up a second driver (a
# reset assignment) alongside the memory-read block that drives them.
# Synthesis resolved the conflict by keeping the constant 0 and discarding
# the real driver, which made the bitmap read path dead code, which let
# opt_design delete every URAM in the design.  What got timed was a stub:
# 768 LUTs, 0 URAM.  The slack was real -- it was just slack on the wrong
# circuit.  Simulation could not catch it, because the reset branch only
# fires during reset, so xsim sees exactly one driver and passes.
#
# The lesson is that a timing number is only meaningful together with
# evidence that the thing timed is the thing intended.  So this script
# refuses to report a verdict unless the routed netlist still contains the
# memories, and unless synthesis raised no critical warnings.
#
# Usage:
#   vivado -mode batch -source scripts/overlay_ooc_timing.tcl -tclargs <outdir>
#
# Greppable output: PYRO_WNS, PYRO_URAM, PYRO_BRAM, PYRO_VERDICT.

set outdir [expr {$argc > 0 ? [lindex $argv 0] : "."}]
file mkdir $outdir

set rtl [file join [file dirname [file dirname [file normalize [info script]]]] \
             hw rtl pyro_overlay_engine.v]

# Expected floor for the full-corpus configuration (MAX_STATES = 40960):
# bitmap is 256 b wide x 40960 deep = 40 URAM288, oidx is 64 b x 40960 = 10.
# Anything materially below this means the datapath was trimmed.
set MIN_URAM 40
set PERIOD   4.000

read_verilog $rtl
synth_design -top pyro_overlay_engine -part xcu250-figd2104-2L-e -mode out_of_context
create_clock -period $PERIOD -name clk [get_ports clk]

set critwarn [get_msg_config -severity {CRITICAL WARNING} -count]
puts "PYRO_CRITWARN: $critwarn"

opt_design
place_design
route_design

report_utilization -file [file join $outdir util.rpt]
report_timing -max_paths 1 -nworst 1 -setup -file [file join $outdir crit.rpt]

set n_uram [llength [get_cells -hier -filter {REF_NAME =~ URAM*}]]
set n_bram [llength [get_cells -hier -filter {REF_NAME =~ RAMB*}]]
puts "PYRO_URAM: $n_uram"
puts "PYRO_BRAM: $n_bram"

set p [get_timing_paths -max_paths 1 -nworst 1 -setup]
if {[llength $p]} {
    set wns [get_property SLACK [lindex $p 0]]
} else {
    set wns "NONE"
}
puts "PYRO_WNS: $wns"

set bad {}
if {$critwarn > 0}      { lappend bad "critical-warnings=$critwarn" }
if {$n_uram < $MIN_URAM} { lappend bad "uram=$n_uram (expected >= $MIN_URAM)" }

if {[llength $bad]} {
    puts "PYRO_VERDICT: INVALID -- [join $bad {, }]"
    puts "  The timing number above describes a netlist that is not the"
    puts "  intended design.  Do not record it.  Fix the cause and re-run."
} elseif {$wns eq "NONE"} {
    puts "PYRO_VERDICT: INVALID -- no timing paths found"
} elseif {$wns < 0} {
    puts "PYRO_VERDICT: FAIL -- negative slack $wns ns at [format %.0f [expr {1000.0/$PERIOD}]] MHz"
} else {
    puts "PYRO_VERDICT: PASS -- WNS $wns ns, $n_uram URAM, $n_bram BRAM"
}
puts "PYRO_OOC_DONE"
