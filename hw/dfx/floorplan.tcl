# floorplan.tcl - read the pyro_rp pblock definition from
# platform_manifest.json.
#
# The manifest is the single source of truth for the pblock range and the RP
# cell
# name; keep both out of the build scripts so they cannot drift apart.
#
# Capture the directory at source time: [info script] is correct here, but NOT
# inside a proc body.
set _floorplan_dir [file normalize [file dirname [info script]]]

proc _read_manifest {} {
  global _floorplan_dir
  set fh [open ${_floorplan_dir}/platform_manifest.json r]
  set txt [read $fh]
  close $fh
  array set m {}
  foreach rp {PYRO_RP} {
    set re_cell "\"$rp\"\\s*:\\s*\\{\[^\\}\]*?\"cell\"\\s*:\\s*"
    append re_cell "\"(\[^\"\]+)\""
    regexp $re_cell $txt -> m($rp,cell)
    set re_rng "\"$rp\"\\s*:\\s*\\{\[^\\}\]*?\"pblock_range\""
    append re_rng "\\s*:\\s*\"(\[^\"\]+)\""
    regexp $re_rng $txt -> m($rp,range)
  }
  return [array get m]
}
