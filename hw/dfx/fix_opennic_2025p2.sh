#!/usr/bin/env bash
# fix_opennic_2025p2.sh <shell_dir>
# RISK-2 / Vivado-2025.2 top-synthesis fixups applied to a throwaway OpenNIC
# shell copy (never the pristine submodule). 2025.2 enforces strict
# SystemVerilog
# $fatal syntax: the first argument MUST be a finish-number (0|1|2), then the
# message. OpenNIC's sources call $fatal("msg", ...) with a string first arg,
# which fails top synth ([Synth 8-11587]). This rewrites $fatal(" -> $fatal(1,
# ".
# (Only matters at full-shell top synthesis, which the DFX flow performs; prior
# IP-only synth never hit it.)
set -euo pipefail
DIR="${1:?usage: fix_opennic_2025p2.sh <shell_dir>}"
[ -d "$DIR" ] || { echo "no such dir: $DIR" >&2; exit 1; }
n=0
while IFS= read -r f; do
  if grep -q '\$fatal("' "$f"; then
    sed -i 's/\$fatal("/\$fatal(1, "/g' "$f"
    n=$((n+1))
  fi
done < <(find "$DIR" \( -name '*.sv' -o -name '*.v' -o -name '*.vh' \) -type f)
echo "[fix_opennic_2025p2] patched \$fatal syntax in $n file(s) under $DIR"
