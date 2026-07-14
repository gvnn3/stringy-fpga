#!/usr/bin/env bash
# setup_jtag_udev.sh — make the U250's on-board JTAG reachable by hw_server.
#
#   sudo scripts/setup_jtag_udev.sh
#
# Idempotent and host-side only: this touches USB permissions and kernel driver
# binding. It NEVER programs, reconfigures, or otherwise talks to the FPGA.
#
# WHY THIS IS NEEDED (nf-server06, 2026-07-13):
#
#   1. `ftdi_sio` (the kernel USB-serial driver) grabs all four interfaces of the
#      board's FT4232H. Vivado's JTAG stack talks to the chip through libusb and
#      cannot claim an interface the kernel already owns -> hw_server reports
#      "No matching targets found".
#
#   2. Vivado's own shipped rule (52-xilinx-digilent-usb.rules) only grants
#      MODE:="666" when the FTDI reports  ATTRS{manufacturer}=="Digilent". The
#      U250's *on-board* JTAG reports:
#
#          idVendor=0403  idProduct=6011  manufacturer='Xilinx'
#          product='A-U250-P64G'
#
#      i.e. manufacturer 'Xilinx', NOT 'Digilent'. So the permission rule never
#      matches, the device node stays root:root, and a non-root hw_server cannot
#      open it. Xilinx's unbind rule fires, but the MODE rule does not. This
#      script adds the missing rule.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run me with sudo:  sudo $0" >&2
  exit 1
fi

VIVADO_DIR="${PYRO_VIVADO_DIR:-/usr/local/cad/2025.2/Vivado}"
DRIVERS="${VIVADO_DIR}/data/xicom/cable_drivers/lin64/install_script/install_drivers/install_drivers"
RULE=/etc/udev/rules.d/53-alveo-u250-jtag.rules

echo "=== 1. Vivado cable drivers (udev rules) ==="
if [ -f /etc/udev/rules.d/52-xilinx-ftdi-usb.rules ]; then
  echo "    already installed"
elif [ -x "$DRIVERS" ]; then
  "$DRIVERS"
else
  echo "    WARNING: cable-driver installer not found at $DRIVERS"
  echo "    (set PYRO_VIVADO_DIR if Vivado lives elsewhere)"
fi

echo "=== 2. permission rule for the Xilinx-branded on-board FTDI ==="
# Vivado's rule only covers manufacturer=="Digilent"; the U250's on-board JTAG
# reports manufacturer=="Xilinx", so it needs its own MODE rule.
cat > "$RULE" <<'EOF'
# Alveo U250 on-board JTAG (FT4232H). Vivado's 52-xilinx-digilent-usb.rules
# grants MODE 666 only to ATTRS{manufacturer}=="Digilent"; the U250's on-board
# cable reports manufacturer "Xilinx" (product A-U250-P64G) and so is not
# covered. Without this, /dev/bus/usb/... stays root:root and a non-root
# hw_server cannot open the cable.
ACTION=="add", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6011", ATTRS{manufacturer}=="Xilinx", MODE:="666"
EOF
echo "    wrote $RULE"

echo "=== 3. release the FT4232H interfaces from ftdi_sio ==="
released=0
for dev in /sys/bus/usb/devices/*; do
  [ -f "$dev/idVendor" ] || continue
  [ "$(cat "$dev/idVendor")" = "0403" ] || continue
  [ "$(cat "$dev/idProduct" 2>/dev/null)" = "6011" ] || continue
  base="$(basename "$dev")"
  for intf in "$dev":*; do
    [ -e "$intf" ] || continue
    i="$(basename "$intf")"
    if [ -e "/sys/bus/usb/drivers/ftdi_sio/$i" ]; then
      echo -n "$i" > /sys/bus/usb/drivers/ftdi_sio/unbind 2>/dev/null && {
        echo "    unbound $i from ftdi_sio"; released=$((released+1)); }
    fi
  done
  echo "    cable: $base  ($(cat "$dev/manufacturer" 2>/dev/null) $(cat "$dev/product" 2>/dev/null), serial $(cat "$dev/serial" 2>/dev/null))"
done
[ "$released" -eq 0 ] && echo "    (nothing bound to ftdi_sio — fine)"

echo "=== 4. reload + re-trigger udev ==="
udevadm control --reload-rules
udevadm trigger --action=add --subsystem-match=usb
sleep 2

echo "=== 5. result ==="
ok=0
for dev in /sys/bus/usb/devices/*; do
  [ -f "$dev/idVendor" ] || continue
  [ "$(cat "$dev/idVendor")" = "0403" ] || continue
  [ "$(cat "$dev/idProduct" 2>/dev/null)" = "6011" ] || continue
  bus=$(cat "$dev/busnum"); devnum=$(cat "$dev/devnum")
  node=$(printf "/dev/bus/usb/%03d/%03d" "$bus" "$devnum")
  ls -l "$node"
  if [ -w "$node" ] && [ -r "$node" ]; then ok=1; fi
  # world-writable is what the MODE:="666" rule is for
  perms=$(stat -c '%a' "$node")
  [ "$perms" = "666" ] && ok=1
done

if [ "$ok" -eq 1 ]; then
  echo
  echo "JTAG_UDEV_OK — the cable is now openable by a non-root hw_server."
  echo "Next (as your normal user):  pkill hw_server; hw_server -d"
else
  echo
  echo "JTAG_UDEV_INCOMPLETE — the node is still not world-accessible."
  echo "If the cable was plugged in before this ran, physically re-plug it"
  echo "(or reboot) so the ACTION==\"add\" rule fires against it."
  exit 2
fi
