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

mode="${1:-status}"

# sudo strips the environment, so overrides ride as trailing PYRO_*=value
# args:  sudo .../pyro_dataplane_swap.sh data PYRO_QDMA_MODE=02:0:0
shift 2>/dev/null || true
for kv in "$@"; do
  case "$kv" in
    PYRO_[A-Z_]*=*) export "$kv" ;;
    *) echo "ERROR: unrecognized arg '$kv' (want PYRO_*=value)" >&2; exit 1 ;;
  esac
done

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
  # Idempotent re-entry: tear down any prior qdma-pf state so a rebuilt .ko
  # and fresh queue config always take effect.
  if lsmod | grep '^qdma_pf' >/dev/null; then
    for q in $(seq "$QIDX" $((QIDX + ${PYRO_QDMA_QCOUNT:-4} - 1))); do "$DMACTL" "$QDEV" q stop idx "$q" dir bi >/dev/null 2>&1 || true; done
    for q in $(seq "$QIDX" $((QIDX + ${PYRO_QDMA_QCOUNT:-4} - 1))); do "$DMACTL" "$QDEV" q del idx "$q" dir bi >/dev/null 2>&1 || true; done
    rmmod qdma_pf
    echo "    stale qdma-pf removed"
  fi
  # mode <bus>:<pf>:<drv_mode>; drv_mode 2 = DIRECT_INTR (MSI-X per queue).
  # The default AUTO mode POLLS for writeback status — a ~4.6 ms/frame
  # latency floor that caps the char-dev at ~2 MiB/s.
  insmod "$QDMA_KO" mode="${PYRO_QDMA_MODE:-02:0:2}"
  echo "    qdma-pf inserted (mode=${PYRO_QDMA_MODE:-02:0:2})"
  sleep 1
  QMAX_SYS="/sys/bus/pci/devices/$BDF/qdma/qmax"
  [ -f "$QMAX_SYS" ] || { echo "ERROR: $QMAX_SYS missing — driver did not bind"; exit 2; }
  echo 32 > "$QMAX_SYS"
  # ST queue pairs on indexes QIDX..QIDX+QCOUNT-1 (default 4: parallel TX
  # queues for the 5 GiB/s target — the per-write syscall cost serializes a
  # single queue at ~3 GB/s; separate queues have separate descq locks).
  # The all-zero RSS indirection table steers EVERY C2H reply to qid 0, so
  # queue 0 is the only reader; the extras are TX-only in practice.  C2H is
  # still started on each (harmless; cmptsz 0 = 8 B entries, trigmode every
  # = surface each reply immediately, latency over batching).
  QCOUNT="${PYRO_QDMA_QCOUNT:-4}"
  for q in $(seq "$QIDX" $((QIDX + QCOUNT - 1))); do
    "$DMACTL" "$QDEV" q add idx "$q" mode st dir bi >/dev/null
    "$DMACTL" "$QDEV" q start idx "$q" dir h2c >/dev/null
    "$DMACTL" "$QDEV" q start idx "$q" dir c2h cmptsz 0 trigmode every >/dev/null
  done
  echo "    ST queues $QIDX..$((QIDX + QCOUNT - 1)) started (bi)"
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
  # Grant the invoking user the nodes (mirrors the CAP_NET_RAW scoping choice:
  # one user, not world). SUDO_USER is who ran `sudo <this script>`.
  if [ -n "${SUDO_USER:-}" ]; then
    for q in $(seq "$QIDX" $((QIDX + QCOUNT - 1))); do
      chown "$SUDO_USER" /dev/${QDEV}-ST-${q}
    done
    echo "    owner -> $SUDO_USER (${QCOUNT} nodes)"
  fi
  echo "DATAPLANE_UP — /dev/${QDEV}-ST-${QIDX}..$((QIDX + QCOUNT - 1))"
  ;;

control)
  echo "=== qdma-pf -> onic ==="
  if lsmod | grep '^qdma_pf' >/dev/null; then
    for q in $(seq "$QIDX" $((QIDX + ${PYRO_QDMA_QCOUNT:-4} - 1))); do "$DMACTL" "$QDEV" q stop idx "$q" dir bi >/dev/null 2>&1 || true; done
    for q in $(seq "$QIDX" $((QIDX + ${PYRO_QDMA_QCOUNT:-4} - 1))); do "$DMACTL" "$QDEV" q del idx "$q" dir bi >/dev/null 2>&1 || true; done
    rmmod qdma_pf
    echo "    qdma-pf removed"
  else
    echo "    (qdma-pf not loaded)"
  fi
  # Reset user box + QDMA subsystem before handing the PF to onic. A wedged
  # RP holds a queue MSI-X vector pending across the swap and fires it the
  # instant onic's request_irq completes — before ndo_open has allocated
  # rx_queue[] (the 2026-07-25 panic). Same pulse ordering as
  # pyro_wedge_recover.sh; PCIe link stays up (soft_reset_n only).
  # PYRO_SWAP_NO_RESET=1 skips it (e.g. to preserve pyro box state on a
  # known-healthy card).
  if [ -z "${PYRO_SWAP_NO_RESET:-}" ]; then
    BDF="$BDF" python3 - <<'PYEOF'
import mmap, os, struct, sys, time

REGS = {"build_ts": 0x000, "shell_rst": 0x00C, "shell_status": 0x010,
        "user_rst": 0x014, "user_status": 0x018}

def rd(m, off): return struct.unpack("<I", m[off:off+4])[0]
def wr(m, off, v): m[off:off+4] = struct.pack("<I", v)

def pulse(m, rst, status, name):
    wr(m, rst, 0x1)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if rd(m, status) & 0x1:
            print(f"    {name}: RESET_DONE (status={rd(m, status):#010x})")
            return
        time.sleep(0.001)
    sys.exit(f"    {name}: TIMEOUT — rst_done never asserted")

path = f"/sys/bus/pci/devices/{os.environ['BDF']}/resource2"
with open(path, "r+b") as f:
    m = mmap.mmap(f.fileno(), 4096)
    ts = rd(m, REGS["build_ts"])
    if ts in (0x0, 0xFFFFFFFF):
        sys.exit(f"    ABORT: build timestamp {ts:#010x} — BAR dead, not poking")
    print(f"    build_timestamp={ts:#010x}")
    pulse(m, REGS["user_rst"], REGS["user_status"], "user[0]  (pyro box+RP)")
    pulse(m, REGS["shell_rst"], REGS["shell_status"], "shell[0] (QDMA soft)")
PYEOF
    echo "    pre-insmod reset done"
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
