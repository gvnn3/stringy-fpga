#!/usr/bin/env bash
# setup_device_transport.sh — grant the PYRO device transport what it needs.
#
#   sudo scripts/setup_device_transport.sh [iface]
#
# Idempotent. Two things, both requiring root, neither touching the FPGA:
#
#   1. Bring the `onic` netdev UP. The R78 control protocol rides AF_PACKET on
#      it, and the kernel will not transmit on a DOWN interface -- so the probe
#      can never get an ID_REPLY while it is down. NOTE: no link partner and no
#      100G transceiver are needed. The PYRO shell loops
#      QDMA H2C -> pyro_rp -> C2H *inside the card*; frames never reach a MAC,
#      and the CMAC path is tied off (R80). The interface will stay NO-CARRIER
#      and that is fine.
#
#   2. Grant CAP_NET_RAW to a dedicated venv interpreter (R78, P2/P3, R83
#      condition (ii)). Scoped to .venv-pyro's *copied* python binary rather
#      than the system interpreter, so the blast radius of a mistake is one
#      venv and not every script on the host that runs python3
#      (docs/device-bringup.md §4).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run me with sudo:  sudo $0" >&2
  exit 1
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IFACE="${1:-${PYRO_DEVICE_IFACE:-ens2}}"
VENV_PY="${REPO}/.venv-pyro/bin/python3"

echo "=== 1. bring up the onic netdev: $IFACE ==="
if ! ip link show "$IFACE" >/dev/null 2>&1; then
  echo "ERROR: no such interface: $IFACE" >&2
  echo "  onic netdevs present:" >&2
  ip -br link show | awk '{print "    "$1}' >&2
  exit 2
fi
ip link set "$IFACE" up
sleep 1
ip -br link show "$IFACE"
echo "    (NO-CARRIER is expected -- the CMAC path is tied off; PYRO loops in-card)"

echo "=== 2. CAP_NET_RAW on the venv interpreter ==="
if [ ! -f "$VENV_PY" ]; then
  echo "ERROR: venv interpreter not found: $VENV_PY" >&2
  echo "  create it first (as your normal user):" >&2
  echo "    python3 -m venv --copies .venv-pyro" >&2
  exit 3
fi
# Must be a real copy, not a symlink to /usr/bin/python3 -- setcap on a symlink
# would either fail or (worse) mark the system interpreter.
if [ -L "$VENV_PY" ]; then
  echo "ERROR: $VENV_PY is a symlink. Recreate the venv with --copies." >&2
  exit 4
fi
setcap cap_net_raw+ep "$VENV_PY"
echo "    $(getcap "$VENV_PY")"

echo
echo "DEVICE_TRANSPORT_OK"
echo "Next (as your normal user):"
echo "    PYRO_DEVICE_IFACE=$IFACE .venv-pyro/bin/python3 -c \\"
echo "        'import pyro.device as d; print(d.probe_device(d.DeviceConfig()))'"
