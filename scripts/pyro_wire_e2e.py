#!/usr/bin/env python3
"""OQ-2 wire path end-to-end on silicon (wiretap shell + loopback).

Runs AFTER the wiretap shell is flashed and cold-booted, with the
wire-scan overlay child resident (hw/dfx/build-wiretap/partials/
overlay_wire.bit via scripts/pyro_hw.py load).  The TX generator in
the wiretap shell emits one 64-byte frame every 1024 cycles
(~244k frames/s at 250 MHz); with CMAC near-end loopback those
frames return on RX and enter pyro_rp as wire frames (tuser src
0x0040, R78.13).  This script drives the three-phase experiment:

  A  loopback on, NO table: every wire frame must be DROPPED and
     counted (seen == drops climbing, scanned/noms flat) — the
     fail-closed gate on silicon.
  B  clean table (anchor that no wire frame contains): scanned
     climbs, noms flat, and NO wire MATCH_REPLY appears — the
     reply-only-on-nomination polarity on silicon.
  C  matching table (anchor = the TX generator's constant 14-byte
     frame prefix ff*6 | 02:00:00:00:00:01 | 88 B5): noms climb and
     wire MATCH_REPLYs (payload status bit2, R78.13) arrive with
     the child's slot and the active epoch — SR14' attribution on
     live wire traffic.

Every phase reads the R78.13 PERF wire counters before/after over a
sampling window; phases B/C also sniff ens2 for wire replies.  Wire
MATCH_REPLYs are addressed to the TX generator's source MAC, not
ours, so the sniffer counts on the fabric doing no dst filtering
(OpenNIC has none); if captures come up empty while noms climb, the
counters are still the primary evidence and the report says so.

Timing is reported alongside the pass/fail evidence: phases B and C
sample the R45a per-scan counters (reset at scan start, latched at
scan end, so under continuous wire load each PERF read observes one
completed 64-byte wire-frame scan) and report the per-packet scan
latency as min/median/max in cycles and nanoseconds (250 MHz fabric:
4 ns/cycle).  Each phase's wall-clock duration and the total run
time are printed at the end.

Usage:
    PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 \\
        scripts/pyro_wire_e2e.py [--window 3.0] [--capture 2.0] \\
        [--perf-n 200] [--perf-sleep 0.002]

Loopback is enabled via `sudo -n scripts/pyro_cmac_loopback.py
--keep` (NOPASSWD, /etc/sudoers.d/pyro-cmac) and left ON so the
counters keep moving for inspection; disable with `--off` later.
"""
import argparse
import os
import statistics
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyro.device as pdev                     # noqa: E402
from pyro.overlay import model as omodel       # noqa: E402
from pyro.overlay import table as otable       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_ID = 0x0A5E0001
NS_PER_CYCLE = 4.0                             # 250 MHz fabric clock
WIRE_FRAME_BYTES = 64                          # TX generator frame size

# The TX generator's constant frame prefix (pyro_wire_tx_gen.sv):
# dst ff:ff:ff:ff:ff:ff, src 02:00:00:00:00:01, ethertype 0x88B5.
TXGEN_PREFIX = (b"\xff" * 6 + b"\x02\x00\x00\x00\x00\x01" +
                b"\x88\xb5")
CLEAN_ANCHOR = b"never on any wire 5f0e"


def cfg():
    return pdev.DeviceConfig()


def perf(c):
    r = pdev.read_perf_counters(c, with_wire=True)
    if r is None:
        return None
    _cyc, _byt, wire = r
    return wire


def sample(c, seconds, label):
    a = perf(c)
    time.sleep(seconds)
    b = perf(c)
    if a is None or b is None:
        print("FAIL %s: PERF_REPLY unavailable" % label)
        return None, None
    d = pdev.WireCounters(*(y - x for x, y in zip(a, b)))
    print("%s: over %.1fs  d.seen=%d d.scanned=%d d.drops=%d "
          "d.noms=%d  (abs seen=%d)" %
          (label, seconds, d.seen, d.scanned, d.drops, d.noms,
           b.seen))
    return b, d


def sample_scan_timing(c, n, sleep_s):
    """N R45a PERF reads; keep cycles where bytes == one wire frame.

    The per-scan counters reset at scan start and hold the last
    completed scan, so under continuous wire load each read samples
    one 64-byte wire-frame scan.
    """
    cycles, discarded = [], 0
    for _ in range(n):
        r = pdev.read_perf_counters(c, slot=1, with_wire=True)
        if r is None:
            discarded += 1
        else:
            cyc, byt, _wire = r
            if byt == WIRE_FRAME_BYTES:
                cycles.append(cyc)
            else:
                discarded += 1
        time.sleep(sleep_s)
    return cycles, discarded


def report_scan_timing(label, cycles, discarded, n):
    """Per-packet scan latency: min/median/max in cycles and ns."""
    if not cycles:
        print("TIMING %s: NO %d-byte samples (%d/%d discarded)"
              % (label, WIRE_FRAME_BYTES, discarded, n))
        return None
    med = statistics.median(cycles)
    print("TIMING %s: %d per-packet samples, %d discarded "
          "(bytes != %d or no reply)"
          % (label, len(cycles), discarded, WIRE_FRAME_BYTES))
    print("  per-packet cycles  min=%d median=%.1f max=%d"
          % (min(cycles), med, max(cycles)))
    print("  per-packet ns      min=%.0f median=%.0f max=%.0f "
          "(4 ns/cycle)"
          % (min(cycles) * NS_PER_CYCLE, med * NS_PER_CYCLE,
             max(cycles) * NS_PER_CYCLE))
    return med


def sniff_wire_replies(c, seconds, limit=50):
    """Collect wire MATCH_REPLYs (status bit2) off the netdev."""
    t = pdev._make_transport(c)
    out, total = [], 0
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline and len(out) < limit:
            fr = t.recv(max(0.05, deadline - time.monotonic()))
            if fr is None:
                continue
            fr = bytes(fr)
            if (len(fr) < 14 + pdev.PYRO_HEADER_LEN or
                    struct.unpack(">H", fr[12:14])[0] != pdev.ETHERTYPE):
                continue
            try:
                dec = pdev.decode_frame(fr[14:])
            except pdev.PyroFrameError:
                continue
            total += 1
            if (dec.kind == pdev.KIND_MATCH_REPLY
                    and len(dec.payload) >= 8
                    and struct.unpack(">HH", dec.payload[0:4])[1] & 0x4):
                out.append(dec)
    finally:
        t.close()
    return out, total


def load(c, anchors, what):
    ac = otable.build(anchors)
    image = otable.serialize(ac, engine_id=ENGINE_ID)
    st = pdev.load_table(c, image)
    print("loaded %s table: id=0x%08x epoch=%d (%d states)"
          % (what, st.active_table_id, st.epoch, ac.n_states))
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=float, default=3.0)
    ap.add_argument("--capture", type=float, default=2.0)
    ap.add_argument("--perf-n", type=int, default=200,
                    help="per-packet R45a timing samples per phase")
    ap.add_argument("--perf-sleep", type=float, default=0.002,
                    help="sleep between PERF reads (s)")
    args = ap.parse_args()
    c = cfg()
    failures = []
    t_run0 = time.perf_counter()
    phase_times = []

    usable, reason = pdev.probe_device(c)
    print("probe: %s" % reason)
    if not usable:
        print("ABORT: device not usable — load the wire-scan child "
              "first:\n  PYRO_DEVICE_IFACE=%s .venv-pyro/bin/python3 "
              "scripts/pyro_hw.py load "
              "hw/dfx/build-wiretap/partials/overlay_wire.bit"
              % (c.iface or "ens2"))
        return 1

    w0 = perf(c)
    if w0 is None:
        print("ABORT: no PERF_REPLY — is the overlay_wire child "
              "resident?")
        return 1
    print("wire counters at start: %s" % (w0,))

    # Loopback ON (idempotent; --keep leaves it up).
    r = subprocess.run(["sudo", "-n",
                        os.path.join(REPO,
                                     "scripts/pyro_cmac_loopback.py"),
                        "--keep"], capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        print("ABORT: loopback bring-up failed\n%s" % r.stderr)
        return 1

    # ---- A: no table -> drop, counted --------------------------------
    t0 = time.perf_counter()
    _, d = sample(c, args.window, "A(no-table)")
    if d is None:
        return 1
    if not (d.seen > 0 and d.drops == d.seen and d.scanned == 0
            and d.noms == 0):
        failures.append("A: expected pure counted drops, got %s"
                        % (d,))
    phase_times.append(("A(no-table)", time.perf_counter() - t0))

    # ---- B: clean table -> scanned, silent ---------------------------
    t0 = time.perf_counter()
    load(c, [CLEAN_ANCHOR], "clean")
    _, d = sample(c, args.window, "B(clean)")
    if d is None:
        return 1
    if not (d.seen > 0 and d.scanned > 0 and d.noms == 0):
        failures.append("B: expected silent scans, got %s" % (d,))
    cyc, disc = sample_scan_timing(c, args.perf_n, args.perf_sleep)
    med_clean = report_scan_timing("B(clean scan floor)", cyc, disc,
                                   args.perf_n)
    replies, seen_frames = sniff_wire_replies(c, args.capture)
    print("B capture: %d wire MATCH_REPLYs in %d PYRO frames"
          % (len(replies), seen_frames))
    if replies:
        failures.append("B: %d wire replies from a clean table"
                        % len(replies))
    phase_times.append(("B(clean)", time.perf_counter() - t0))

    # ---- C: matching table -> nominations + wire replies -------------
    t0 = time.perf_counter()
    st = load(c, [TXGEN_PREFIX], "matching")
    ref = omodel.OverlayEngineModel(engine_id=ENGINE_ID)
    _, d = sample(c, args.window, "C(matching)")
    if d is None:
        return 1
    if not (d.scanned > 0 and d.noms >= d.scanned):
        failures.append("C: expected >=1 nom per scanned frame, "
                        "got %s" % (d,))
    cyc, disc = sample_scan_timing(c, args.perf_n, args.perf_sleep)
    med_match = report_scan_timing("C(matching)", cyc, disc,
                                   args.perf_n)
    replies, seen_frames = sniff_wire_replies(c, args.capture)
    print("C capture: %d wire MATCH_REPLYs in %d PYRO frames"
          % (len(replies), seen_frames))
    if not replies:
        print("  (no wire replies captured — counters above are the "
              "primary evidence; dst-MAC delivery noted in header)")
    bad = 0
    for dec in replies:
        count, mstat = struct.unpack(">HH", dec.payload[0:4])
        epoch = struct.unpack(">I", dec.payload[4:8])[0]
        ok = (dec.flags == 0 and dec.slot == 1
              and epoch == st.epoch and count >= 1
              and not (mstat & 0x2))
        for j in range(count):
            s, e, pid, _fl = struct.unpack_from("<QQII", dec.payload,
                                                8 + 24 * j)
            if not (pid == 0 and e == 14):
                ok = False
        if not ok:
            bad += 1
    if replies:
        print("C validated: %d/%d replies carry slot=1 epoch=%d "
              "pid=0 end=14 flags=0" % (len(replies) - bad,
                                        len(replies), st.epoch))
    if bad:
        failures.append("C: %d malformed wire replies" % bad)
    phase_times.append(("C(matching)", time.perf_counter() - t0))

    # ---- timing summary ----------------------------------------------
    print()
    print("TIMING summary:")
    for name, secs in phase_times:
        print("  phase %-12s %.2f s" % (name, secs))
    print("  total run         %.2f s"
          % (time.perf_counter() - t_run0))
    if med_clean is not None:
        print("  per-packet scan   median %.0f ns clean"
              % (med_clean * NS_PER_CYCLE)
              + (" / %.0f ns matching" % (med_match * NS_PER_CYCLE)
                 if med_match is not None else "")
              + "  (4 ns/cycle)")

    print()
    if failures:
        print("WIRE_E2E: FAIL")
        for f in failures:
            print("  - %s" % f)
        return 1
    print("WIRE_E2E: PASS — wire frames dropped/scanned/nominated per "
          "R78.13 on silicon; loopback left ON")
    return 0


if __name__ == "__main__":
    sys.exit(main())
