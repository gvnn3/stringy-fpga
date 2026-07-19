#!/usr/bin/env bash
# pyro_dataplane_swap.sh — swap the single PF between the two host bindings (root).
#
#   sudo scripts/pyro_dataplane_swap.sh data      onic -> qdma-pf char-devs (P2d)
#   sudo scripts/pyro_dataplane_swap.sh control   qdma-pf -> onic netdev (R78 control)
#   sudo scripts/pyro_dataplane_swap.sh status    show which binding is active
#
# WHY A SWAP: the shell exposes ONE physical function (0000:02:00.0), and the
# onic netdev driver and the generic qdma-pf char-dev driver cannot bind it
# simultaneously. In data mode the R78 control frames ride the ST queues too —
# they are AXIS payloads either way; the card cannot tell the drivers apart.
#
# The qdma-pf ST queue pair is left added+started so /dev/qdma02000-ST-* are
# usable immediately. Defaults are overridable via environment:
set -euo pipefail

BDF="${PYRO_BDF:-0000:02:00.0}"
QDEV="qdma$(echo "$BDF" | sed 's/^0000://; s/[:.]//g')"        # qdma02000
DMA_IP="${DMA_IP_DRIVERS:-/home/gnn/Repos/Yale/dma_ip_drivers/QDMA/linux-kernel}"
QDMA_KO="${QDMA_KO:-$DMA_IP/bin/qdma-pf.ko}"
DMACTL="${DMACTL:-$DMA_IP/bin/dma-ctl}"
ONIC_KO="${ONIC_KO:-/home/gnn/Repos/Yale/NetFPGA-PLUS/sw/driver/open-nic-driver/onic.ko}"
IFACE="${PYRO_DEVICE_IFACE:-ens2}"
MTU="${PYRO_DEVICE_MTU:-9586}"
QIDX="${PYRO_QDMA_QIDX:-0}"

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run me with sudo:  sudo $0 {data|control|status}" >&2
  exit 1
fi

mode="${1:-status}"

status() {
  drv="$(basename "$(readlink -f /sys/bus/pci/devices/$BDF/driver 2>/dev/null)" 2>/dev/null || echo none)"
  echo "binding: $drv"
  ls /dev/${QDEV}* 2>/dev/null || true
  ip -br link show "$IFACE" 2>/dev/null || true
}

case "$mode" in
status) status ;;

data)
  echo "=== onic -> qdma-pf ==="
  rmmod onic 2>/dev/null && echo "    onic removed" || echo "    (onic not loaded)"
  if ! lsmod | grep -q '^qdma_pf'; then
    insmod "$QDMA_KO"
    echo "    qdma-pf inserted"
  fi
  sleep 1
  QMAX_SYS="/sys/bus/pci/devices/$BDF/qdma/qmax"
  [ -f "$QMAX_SYS" ] || { echo "ERROR: $QMAX_SYS missing — driver did not bind"; exit 2; }
  echo 32 > "$QMAX_SYS"
  # ST queue pair on index $QIDX. C2H needs a completion ring; defaults plus
  # explicit trigger mode so replies surface without batching latency.
  "$DMACTL" "$QDEV" q add idx "$QIDX" mode st dir bi >/dev/null
  "$DMACTL" "$QDEV" q start idx "$QIDX" dir h2c >/dev/null
  # cmptsz 0 = 8 B completion entries; trigmode every = surface each reply
  # immediately (latency over batching — replies are ~64 B).
  "$DMACTL" "$QDEV" q start idx "$QIDX" dir c2h cmptsz 0 trigmode every >/dev/null
  echo "    ST queue $QIDX started (bi)"
  # SHELL-side function qid map (NOT a QDMA-IP register): the open-nic
  # qdma_subsystem gates H2C tready on qid ∈ [q_base, q_base+num_q)
  # (qdma_subsystem_function.sv:185-194) and QCONF resets to num_q=0 — all
  # H2C is blocked until this BAR2 write. onic programs it in
  # onic_init_hardware (onic_hardware.c:263-266); the generic qdma-pf driver
  # has no notion of it. PF0: q_base=0 (matches the IP fmap qbase), num_q=32.
  # The all-zero RSS indirection table then steers every C2H reply to qid 0.
  BDF="$BDF" python3 - <<'PYEOF'
import mmap, os, struct
QCONF = 0x1000            # QDMA_FUNC_OFFSET_QCONF(0) in the shell BAR2 map
qbase, num_q = 0, 32
path = f"/sys/bus/pci/devices/{os.environ['BDF']}/resource2"
with open(path, "r+b") as f:
    m = mmap.mmap(f.fileno(), 0x2000)
    m[QCONF:QCONF+4] = struct.pack("<I", (qbase << 16) | num_q)
    rb = struct.unpack("<I", m[QCONF:QCONF+4])[0]
    print(f"    shell QCONF(0) = {rb:#010x} (qbase={rb>>16}, num_q={rb & 0xFFFF})")
PYEOF
  ls -la /dev/${QDEV}-ST-${QIDX} 2>/dev/null || {
    echo "ERROR: char-dev /dev/${QDEV}-ST-${QIDX} did not appear"; exit 3; }
  echo "DATAPLANE_UP — /dev/${QDEV}-ST-${QIDX}"
  ;;

control)
  echo "=== qdma-pf -> onic ==="
  if lsmod | grep -q '^qdma_pf'; then
    "$DMACTL" "$QDEV" q stop idx "$QIDX" dir bi >/dev/null 2>&1 || true
    "$DMACTL" "$QDEV" q del  idx "$QIDX" dir bi >/dev/null 2>&1 || true
    rmmod qdma_pf
    echo "    qdma-pf removed"
  else
    echo "    (qdma-pf not loaded)"
  fi
  insmod "$ONIC_KO"
  sleep 1
  ip link set "$IFACE" up
  ip link set "$IFACE" mtu "$MTU" 2>/dev/null || true
  ip -br link show "$IFACE"
  echo "CONTROL_UP — $IFACE"
  ;;

*) echo "usage: $0 {data|control|status}" >&2; exit 1 ;;
esac
