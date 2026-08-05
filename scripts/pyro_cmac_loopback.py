#!/usr/bin/env python3
"""CMAC-0 near-end (PMA) loopback bring-up over PCIe BAR2 (root).

OQ-2 (docs/studies/wire-rate-spike.md §2): no transceiver link exists
on nf-server06, so functional wire tests need CMAC near-end loopback,
"to be configured via the CMAC's AXI-Lite window; verified as part of
the spike, not assumed".  This tool is that verification.  It runs on
the PRODUCTION shell: the CMAC core and its AXI-Lite window are in
the static shell (BAR2 0x8000, cmac_subsystem_address_map.v); only
the box-plugin datapath is tied off.  What can be proven here is
that loopback programming works and the PCS aligns against its own
TX (stat_rx_aligned) — frames THROUGH the loop into pyro_rp need
the wiretap shell (G4, an owner decision).

Register truth is the onic driver (open-nic-driver/onic_register.h,
onic_hardware.c:onic_enable_cmac), not guesswork:

  BAR2 0x00C  SHELL_RESET   bit4 = CMAC-0 subsystem reset
  BAR2 0x010  SHELL_STATUS  bit4 = CMAC-0 reset done
  0x8000+0x090  GT_LOOPBACK   bit0 = near-end PMA loopback (PG203)
  0x8000+0x00C  CONF_TX_1     bit0 enable, bit4 send RFI
  0x8000+0x014  CONF_RX_1     bit0 enable
  0x8000+0x204  STAT_RX_STATUS  bit0 status, bit1 aligned (latched:
                read twice, believe the second — onic_ethtool idiom)
  0x8000+0x1000/0x107C  RS-FEC indication/enable (mirrored from the
                onic module parameter RS_FEC_ENABLED, as at probe)

Sequence (mirrors onic_enable_cmac, with GT_LOOPBACK written first
so alignment starts under loopback): loopback on -> CMAC-0 shell
reset -> RS-FEC (if the driver had it on) -> RX enable -> TX RFI ->
TX enable -> flow control -> poll aligned.  Default RESTORES the
card as found (loopback off + same sequence, expect not-aligned);
--keep leaves loopback up; --status is read-only; --off restores.

The QDMA path (pyro control frames, ens2) is untouched: SHELL_RESET
bit4 resets only the CMAC subsystem, not QDMA (bit0) or the user
box (USER_RESET 0x014, pyro_user_reset.py).  ens2 carrier may flap.

Usage:
    sudo python3 scripts/pyro_cmac_loopback.py [--status|--off|--keep]
                                               [--bdf 0000:02:00.0]
"""
import argparse
import ctypes
import mmap
import sys
import time

SYS_BUILD = 0x000
SYS_SHELL_RESET = 0x00C
SYS_SHELL_STATUS = 0x010
CMAC0_RESET_BIT = 0x10          # bit4 = CMAC-0 (onic_enable_cmac)

CMAC = 0x8000                    # CMAC-0 IP block (subsystem + 0x0)
GT_LOOPBACK = CMAC + 0x090
CONF_TX_1 = CMAC + 0x00C
CONF_RX_1 = CMAC + 0x014
CORE_VERSION = CMAC + 0x024
CONF_RX_FC_CTRL_1 = CMAC + 0x084
CONF_RX_FC_CTRL_2 = CMAC + 0x088
CONF_TX_FC_QNTA = [CMAC + o for o in
                   (0x048, 0x04C, 0x050, 0x054, 0x058)]
CONF_TX_FC_RFRH = [CMAC + o for o in
                   (0x034, 0x038, 0x03C, 0x040, 0x044)]
CONF_TX_FC_CTRL_1 = CMAC + 0x030
RSFEC_IND_CORRECTION = CMAC + 0x1000
RSFEC_ENABLE = CMAC + 0x107C
STAT_TX_STATUS = CMAC + 0x200
STAT_RX_STATUS = CMAC + 0x204
STAT_RX_BLOCK_LOCK = CMAC + 0x20C

EXPECT_CORE_VERSION = 0x00000301   # ONIC_CMAC_CORE_VERSION

RS_FEC_PARAM = "/sys/module/onic/parameters/RS_FEC_ENABLED"


# Single aligned 32-bit loads/stores, NOT mmap slice copies: a slice
# assignment may issue byte stores, and an AXI-Lite slave is free to
# ignore partial-strobe writes.  ctypes.c_uint32 access compiles to
# one 4-byte MOV against the BAR mapping.
def _reg(m, off):
    return ctypes.c_uint32.from_buffer(m, off)


def rd(m, off):
    return _reg(m, off).value


def wr(m, off, val):
    _reg(m, off).value = val


def rx_status(m):
    """Latched status: read twice, the second read is current."""
    rd(m, STAT_RX_STATUS)
    return rd(m, STAT_RX_STATUS)


def show(m, label):
    rx = rx_status(m)
    rd(m, STAT_TX_STATUS)
    tx = rd(m, STAT_TX_STATUS)
    lock = rd(m, STAT_RX_BLOCK_LOCK)
    lb = rd(m, GT_LOOPBACK)
    # Config readbacks diagnose whether CMAC writes stick at all:
    # the onic probe wrote RSFEC_ENABLE=0x3, CONF_RX_1=1, CONF_TX_1=1
    # (onic_enable_cmac) — zeros here mean writes are not landing.
    print("%s: GT_LOOPBACK=%#x  RX_STATUS=%#x (status=%d aligned=%d "
          "misaligned=%d)  TX_STATUS=%#x  BLOCK_LOCK=%#07x"
          % (label, lb, rx, rx & 1, (rx >> 1) & 1, (rx >> 2) & 1,
             tx, lock))
    print("%s: CONF_TX_1=%#x  CONF_RX_1=%#x  RSFEC_ENABLE=%#x"
          % (label, rd(m, CONF_TX_1), rd(m, CONF_RX_1),
             rd(m, RSFEC_ENABLE)))
    return rx


def rs_fec_enabled():
    try:
        return open(RS_FEC_PARAM).read().strip() == "1"
    except OSError:
        return False


def cmac_reset(m):
    wr(m, SYS_SHELL_RESET, CMAC0_RESET_BIT)
    deadline = time.monotonic() + 1.0
    while (rd(m, SYS_SHELL_STATUS) & CMAC0_RESET_BIT) != CMAC0_RESET_BIT:
        if time.monotonic() > deadline:
            print("ABORT: CMAC-0 reset did not complete "
                  "(SHELL_STATUS=%#x)" % rd(m, SYS_SHELL_STATUS))
            return False
        time.sleep(0.001)
    return True


def cmac_enable(m, fec):
    """onic_enable_cmac's post-reset half, verbatim order."""
    if fec:
        wr(m, RSFEC_ENABLE, 0x3)
        wr(m, RSFEC_IND_CORRECTION, 0x7)
    wr(m, CONF_RX_1, 0x1)
    wr(m, CONF_TX_1, 0x10)          # send remote-fault indication
    wr(m, CONF_TX_1, 0x1)           # enable, clear RFI
    wr(m, CONF_RX_FC_CTRL_1, 0x00003DFF)
    wr(m, CONF_RX_FC_CTRL_2, 0x0001C631)
    for off in CONF_TX_FC_QNTA[:4]:
        wr(m, off, 0xFFFFFFFF)
    wr(m, CONF_TX_FC_QNTA[4], 0x0000FFFF)
    for off in CONF_TX_FC_RFRH[:4]:
        wr(m, off, 0xFFFFFFFF)
    wr(m, CONF_TX_FC_RFRH[4], 0x0000FFFF)
    wr(m, CONF_TX_FC_CTRL_1, 0x000001FF)


def poll_aligned(m, want, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rx = rx_status(m)
        if bool(rx & 0x2) == want:
            return rx
        time.sleep(0.05)
    return rx_status(m)


GT_RESET_REG = CMAC + 0x000
RESET_REG = CMAC + 0x004
RX_SERDES_RESET_MASK = 0x000003FF


def set_loopback(m, on, fec):
    # Order matters: the CMAC-0 subsystem reset clears the AXI
    # register bank to defaults (GT_LOOPBACK included, default 0), so
    # loopback must be written AFTER the reset — the first draft
    # wrote it before and the reset silently erased it.
    if not cmac_reset(m):
        return None
    wr(m, GT_LOOPBACK, 0x1 if on else 0x0)
    got = rd(m, GT_LOOPBACK)
    print("GT_LOOPBACK wrote %#x, reads %#x" % (1 if on else 0, got))
    cmac_enable(m, fec)
    rx = poll_aligned(m, want=on, timeout_s=1.5)
    if bool(rx & 0x2) != on:
        # GT guidance: a loopback-mode change wants an RX-serdes
        # reset to reliably re-lock the CDR.  Pulse it via the AXI
        # RESET_REG (self-restoring: write lanes high, then low).
        print("no %s after enable; pulsing RX serdes reset"
              % ("alignment" if on else "de-alignment"))
        wr(m, RESET_REG, RX_SERDES_RESET_MASK)
        time.sleep(0.01)
        wr(m, RESET_REG, 0x0)
        rx = poll_aligned(m, want=on, timeout_s=3.0)
    return rx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bdf", default="0000:02:00.0")
    ap.add_argument("--status", action="store_true",
                    help="read-only: print CMAC state and exit")
    ap.add_argument("--off", action="store_true",
                    help="clear loopback, re-init, expect not-aligned")
    ap.add_argument("--keep", action="store_true",
                    help="leave loopback enabled (default restores)")
    args = ap.parse_args()

    path = "/sys/bus/pci/devices/%s/resource2" % args.bdf
    with open(path, "r+b") as f:
        m = mmap.mmap(f.fileno(), 0x10000)

        ts = rd(m, SYS_BUILD)
        if ts in (0x0, 0xFFFFFFFF):
            print("ABORT: build timestamp %#010x — BAR looks dead" % ts)
            return 1
        ver = rd(m, CORE_VERSION)
        print("build_timestamp=%#010x  cmac_core_version=%#010x  "
              "rs_fec=%s" % (ts, ver, rs_fec_enabled()))
        if ver != EXPECT_CORE_VERSION:
            print("ABORT: CMAC core version != %#x — wrong window, "
                  "not writing" % EXPECT_CORE_VERSION)
            return 1

        show(m, "before")
        if args.status:
            return 0

        fec = rs_fec_enabled()
        if args.off:
            rx = set_loopback(m, False, fec)
            show(m, "after restore")
            return 0 if rx is not None and not (rx & 0x2) else 1

        rx = set_loopback(m, True, fec)
        if rx is None:
            return 1
        ok = bool(rx & 0x2)
        show(m, "loopback")
        print("RESULT: near-end PMA loopback %s"
              % ("ALIGNED — PCS locks against own TX" if ok
                 else "did NOT align"))

        if args.keep:
            print("leaving loopback enabled (--keep); restore with "
                  "--off")
            return 0 if ok else 1

        rxr = set_loopback(m, False, fec)
        show(m, "after restore")
        restored = rxr is not None and not (rxr & 0x2)
        if not restored:
            print("WARNING: card did not return to not-aligned after "
                  "restore")
        return 0 if ok and restored else 1


if __name__ == "__main__":
    sys.exit(main())
