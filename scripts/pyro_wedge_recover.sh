#!/usr/bin/env bash
# pyro_wedge_recover.sh — in-band recovery of a JTAG-wedged pyro_rp (root).
#
# Reproduces the power-on reset ordering without a power cycle:
#
#   1. rmmod onic            — quiesce host side; queue contexts become stale
#                              the moment we reset the QDMA engines anyway
#   2. USER reset  bit 0     — BAR2 0x014: pyro box + pyro_rp + static-side
#                              interface (generic_reset, 100 cycles)
#   3. SHELL reset bit 0     — BAR2 0x00C: QDMA subsystem soft reset. Drives
#                              the QDMA IP's soft_reset_n ONLY; sys_rst_n is
#                              pcie_rstn (edge connector), so the PCIe link
#                              stays up (qdma_subsystem_qdma_wrapper.v:299,458)
#   4. insmod onic + link up — fresh queue/completion contexts
#
# Usage:  sudo scripts/pyro_wedge_recover.sh
set -euo pipefail

BDF="${PYRO_BDF:-0000:02:00.0}"
KO="${ONIC_KO:-/home/gnn/Repos/Yale/NetFPGA-PLUS/sw/driver/open-nic-driver/onic.ko}"
IFACE="${PYRO_DEVICE_IFACE:-ens2}"

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run me with sudo:  sudo $0" >&2
  exit 1
fi

echo "=== 1. rmmod onic ==="
rmmod onic 2>/dev/null && echo "    removed" || echo "    (not loaded)"

echo "=== 2+3. user-box reset, then QDMA soft reset (BAR2 of $BDF) ==="
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

echo "=== 4. reload onic, bring $IFACE up ==="
insmod "$KO"
sleep 1
ip link set "$IFACE" up
# R78.9a jumbo bound: 14 eth + 14 pyro hdr + 9568 payload = 9596 on the wire,
# so the netdev needs mtu >= 9582. 9586 == shell MAX_PKT_LEN (9600) - ETH_HLEN,
# the driver's max_mtu. Falls back gracefully under a pre-jumbo onic.ko.
MTU="${PYRO_DEVICE_MTU:-9586}"
if ! ip link set "$IFACE" mtu "$MTU" 2>/dev/null; then
  echo "    WARN: mtu $MTU refused (pre-jumbo onic.ko?) — staying at $(cat /sys/class/net/$IFACE/mtu)"
fi
sleep 1
ip -br link show "$IFACE"

echo
echo "WEDGE_RECOVER_DONE — now probe as your normal user:"
echo "    PYRO_DEVICE_IFACE=$IFACE .venv-pyro/bin/python3 scripts/pyro_hw.py probe"
