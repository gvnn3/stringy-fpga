#!/usr/bin/env python3
"""Pulse the OpenNIC user-box reset for pyro_rp over PCIe BAR2 (root required).

Why this exists: a JTAG partial load (`program_hw_devices`) reconfigures
`pyro_rp` but leaves the static-side AXIS interface wedged — no decoupler is
engaged and no coordinated interface reset happens during the ~17 s reconfig,
so the RP child comes up silent (docs/notebook.md, 15 Jul 2026 entry). Power-on
recovers only because it resets static + RP + interface together.

This tool reproduces that coordinated reset in-band, without a power cycle.
The chain (verified in RTL, not guessed):

  BAR2 0x014 bit 0 (system_config_register.v REG_USER_RST, WO, self-clearing)
    -> user_rstn[0]                      (open_nic_shell.sv:472)
    -> box_250mhz mod_rstn[0]            (user_plugin_250mhz_inst.vh:82)
    -> pyro_250mhz generic_reset         (100-cycle assert, pyro_250mhz.sv:103)
    -> axil_aresetn = rstn of BOTH pyro_rp_inst AND the box's static-side
       logic (pyro_250mhz.sv:163) — i.e. exactly the static+RP interface
       reset the JTAG path lacks.

BAR2 map (system_config_address_map.sv): 0x000 build timestamp (RO),
0x014 user reset (WO), 0x018 user status (RO, bit0 = rst_done[0]).

Usage:
    sudo python3 scripts/pyro_user_reset.py [BDF]     # default 0000:02:00.0
"""
import mmap
import struct
import sys
import time

REG_BUILD_TIMESTAMP = 0x000
REG_USER_RST = 0x014
REG_USER_STATUS = 0x018


def rd(m, off):
    return struct.unpack("<I", m[off:off + 4])[0]


def wr(m, off, val):
    m[off:off + 4] = struct.pack("<I", val)


def main():
    bdf = sys.argv[1] if len(sys.argv) > 1 else "0000:02:00.0"
    path = f"/sys/bus/pci/devices/{bdf}/resource2"
    with open(path, "r+b") as f:
        m = mmap.mmap(f.fileno(), 4096)
        ts = rd(m, REG_BUILD_TIMESTAMP)
        if ts in (0x0, 0xFFFFFFFF):
            print(f"ABORT: build timestamp reads {ts:#010x} — BAR looks dead, "
                  "not touching reset")
            return 1
        pre = rd(m, REG_USER_STATUS)
        print(f"build_timestamp={ts:#010x}  user_status(pre)={pre:#010x}")

        wr(m, REG_USER_RST, 0x0000_0001)   # pulse user_rstn[0] -> pyro box
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            status = rd(m, REG_USER_STATUS)
            if status & 0x1:
                print(f"USER_RESET_DONE  user_status(post)={status:#010x}")
                return 0
            time.sleep(0.001)
        print(f"TIMEOUT: user_status={rd(m, REG_USER_STATUS):#010x} "
              "(bit0 never returned — rst_done did not assert)")
        return 2


if __name__ == "__main__":
    sys.exit(main())
