# pr_verify.tcl <out_dir>
#
# Verify every full config against config0 (the static + ID-stub reference).
#
# R82c/R82d: pr_verify is MANDATORY. A manifest may claim
# `payload_kind == "pr_bitstream"` (and `pr_verified == true`) ONLY if this
# actually ran and passed for that artifact. A failure here is not a warning --
# it means the partial is not interchangeable with the locked static, and the
# pattern must stay `ooc_metrics` / fall back.

set out   [lindex $argv 0]
set ref   $out/dcp/static_full_config0.dcp
set fails 0
set n     0

foreach f [glob -nocomplain $out/full/*.dcp] {
  incr n
  if {[catch {pr_verify -initial $ref -additional $f} e]} {
    puts "PR_VERIFY_FAIL [file tail $f]: $e"
    incr fails
  } else {
    puts "PR_VERIFY_OK [file tail $f]"
  }
}

puts "PR_VERIFY checked $n config(s)"
if {$n > 0 && $fails == 0} {
  puts "PR_VERIFY_ALL_OK"
} else {
  puts "PR_VERIFY_FAILS=$fails (of $n)"
}
