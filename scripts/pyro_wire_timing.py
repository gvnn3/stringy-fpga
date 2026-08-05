#!/usr/bin/env python3
"""OQ-2 wire-match demo + timing measurement on silicon (R45a/R78.13).

Runs on the wiretap shell with the wire-scan overlay child resident
and CMAC near-end loopback ON: the in-fabric TX generator emits one
64-byte frame every 1024 cycles (~245k frames/s) which loop back into
pyro_rp as wire frames (tuser src 0x0040).  Three measurements:

  1  DEMO — a three-anchor table where EVERY generator frame
     nominates all three patterns at different positions:
       pid 0: the full 14-byte constant prefix (ends at 14)
       pid 1: ff*6, the broadcast dst (ends at 6)
       pid 2: src MAC + ethertype 88 B5 (ends at 14)
     The host model (OverlayEngineModel) is consulted FIRST for the
     exact expected (pid, end) set on a synthetic generator frame;
     ~50 wire MATCH_REPLYs are then captured off the netdev and each
     one is validated against that expectation exactly.
  2  TIMING, per-scan — the R45a counters are reset at each scan's
     START and hold the LAST COMPLETED scan's values, so under
     continuous wire load every PERF read samples one wire-frame
     scan (bytes == 64).  N reads with a small sleep between them
     give a distribution of per-frame scan latencies, taken twice:
     with the CLEAN table (the pure-scan floor) and with the
     matching table (3+ nominations per frame).  250 MHz clock:
     1 cycle = 4 ns.
  3  TIMING, host round-trip — ~50 MATCH_REQUESTs carrying the same
     64-byte subject over the netdev, wall-clock RTT each, for
     contrast with the in-fabric numbers.  Run with the CLEAN table
     resident so no wire MATCH_REPLYs (also kind 0x04) interleave;
     replies with payload status bit2 are filtered out regardless.

End state: the CLEAN table is left resident (no reply flood) and
loopback is left ON.

Usage:
    PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 \\
        scripts/pyro_wire_timing.py [--replies 50] [--perf-n 200] \\
        [--rtt-n 50] [--model-only]

``--model-only`` prints the host-model expectation and exits without
touching the device (no netdev needed).
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

# The TX generator's constant 14-byte frame prefix
# (hw/pyro_plugin/pyro_wire_tx_gen.sv): dst ff:ff:ff:ff:ff:ff,
# src 02:00:00:00:00:01, ethertype 0x88B5.  Bytes 14-17 carry a
# 32-bit LE incrementing seq; bytes 18-63 are zeros.
TXGEN_PREFIX = (b"\xff" * 6 + b"\x02\x00\x00\x00\x00\x01" +
                b"\x88\xb5")
MATCH_ANCHORS = [
    TXGEN_PREFIX,                              # pid 0: full prefix
    b"\xff" * 6,                               # pid 1: broadcast dst
    b"\x02\x00\x00\x00\x00\x01\x88\xb5",       # pid 2: src + ethertype
]
CLEAN_ANCHOR = b"never on any wire 5f0e"


def txgen_frame(seq=0):
    """A synthetic 64-byte TX-generator frame with the given seq."""
    return TXGEN_PREFIX + struct.pack("<I", seq) + b"\x00" * 46


def cfg():
    return pdev.DeviceConfig()


def build_image(anchors):
    ac = otable.build(anchors)
    return ac, otable.serialize(ac, engine_id=ENGINE_ID)


def model_expectation():
    """The host model's (pid, end) set for a TX-generator frame.

    All three anchors lie inside the constant first 14 bytes, so the
    expectation must not depend on the frame's seq field; that
    invariance is checked across several seq values here rather than
    assumed.
    """
    ac, image = build_image(MATCH_ANCHORS)
    want_id = otable.table_id(image)
    ref = omodel.OverlayEngineModel(engine_id=ENGINE_ID)
    ref.table_begin(len(image), want_id, ENGINE_ID,
                    otable.TABLE_FORMAT_VERSION, ac.n_states,
                    len(MATCH_ANCHORS))
    ref.table_data(0, image)
    ref.table_commit(want_id)

    want = None
    for seq in (0, 1, 0xFF, 0x01020304, 0xFFFFFFFF):
        got, ovf = ref.scan(txgen_frame(seq), out_cap=61)
        got_set = {(m.pattern_id, m.end) for m in got}
        if ovf:
            raise RuntimeError("model overflow on a 64-byte frame")
        if want is None:
            want = got_set
        elif got_set != want:
            raise RuntimeError("model expectation varies with seq: "
                               "%s vs %s" % (sorted(want),
                                             sorted(got_set)))
    print("model expectation (any seq): %d matches  %s"
          % (len(want), sorted(want)))
    return want


def load(c, anchors, what):
    ac, image = build_image(anchors)
    st = pdev.load_table(c, image)
    print("loaded %s table: id=0x%08x epoch=%d (%d states)"
          % (what, st.active_table_id, st.epoch, ac.n_states))
    return st


def loopback_on():
    r = subprocess.run(["sudo", "-n",
                        os.path.join(REPO,
                                     "scripts/pyro_cmac_loopback.py"),
                        "--keep"], capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        print("ABORT: loopback bring-up failed\n%s" % r.stderr)
        return False
    return True


# ---- 1. demo: capture + validate wire MATCH_REPLYs -------------------

def sniff_wire_replies(c, limit, seconds):
    """Collect wire MATCH_REPLYs (payload status bit2) off the netdev."""
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


def reply_entries(dec):
    """(count, status, epoch, [(pid, start, end), ...]) or None."""
    if len(dec.payload) < 8:
        return None
    count, mstat = struct.unpack(">HH", dec.payload[0:4])
    epoch = struct.unpack(">I", dec.payload[4:8])[0]
    if len(dec.payload) < 8 + 24 * count:
        return None
    ent = []
    for j in range(count):
        s, e, pid, _fl = struct.unpack_from("<QQII", dec.payload,
                                            8 + 24 * j)
        ent.append((pid, s, e))
    return count, mstat, epoch, ent


def print_sample_reply(dec):
    parsed = reply_entries(dec)
    if parsed is None:
        print("  sample reply: malformed payload (%d bytes)"
              % len(dec.payload))
        return
    count, mstat, epoch, ent = parsed
    print("  sample reply: seq=%d slot=%d epoch=%d status=0x%04x "
          "count=%d" % (dec.seq, dec.slot, epoch, mstat, count))
    for i, (pid, s, e) in enumerate(ent):
        print("    entry[%d]: pid=%d start=%d end=%d" % (i, pid, s, e))


def demo_phase(c, want_set, st, limit, seconds, failures):
    replies, total = sniff_wire_replies(c, limit, seconds)
    print("capture: %d wire MATCH_REPLYs in %d PYRO frames"
          % (len(replies), total))
    if not replies:
        failures.append("demo: no wire MATCH_REPLYs captured")
        return
    good = 0
    for dec in replies:
        parsed = reply_entries(dec)
        if parsed is None:
            continue
        count, mstat, epoch, ent = parsed
        got_set = {(pid, e) for pid, _s, e in ent}
        if (dec.flags == 0 and dec.slot == 1 and epoch == st.epoch
                and not (mstat & 0x2) and got_set == want_set):
            good += 1
    print("validated: %d/%d replies match the model exactly "
          "(slot=1 epoch=%d flags=0, entries == %s)"
          % (good, len(replies), st.epoch, sorted(want_set)))
    print_sample_reply(replies[0])
    if good != len(replies):
        failures.append("demo: %d/%d replies deviated from the model"
                        % (len(replies) - good, len(replies)))
    return good, len(replies)


# ---- 2. timing: R45a per-scan sampling -------------------------------

def sample_perf(c, n, sleep_s):
    """N PERF reads; keep cycles where bytes == 64 (a wire scan)."""
    cycles, discarded = [], 0
    for _ in range(n):
        r = pdev.read_perf_counters(c, slot=1, with_wire=True)
        if r is None:
            discarded += 1
        else:
            cyc, byt, _wire = r
            if byt == 64:
                cycles.append(cyc)
            else:
                discarded += 1
        time.sleep(sleep_s)
    return cycles, discarded


def pctile(xs, p):
    s = sorted(xs)
    k = (len(s) - 1) * p
    f = int(k)
    nxt = min(f + 1, len(s) - 1)
    return s[f] + (s[nxt] - s[f]) * (k - f)


def report_dist(label, cycles, discarded, n):
    if not cycles:
        print("%s: NO 64-byte samples (%d/%d discarded)"
              % (label, discarded, n))
        return None
    med = statistics.median(cycles)
    p90 = pctile(cycles, 0.90)
    print("%s: %d samples kept, %d discarded (bytes != 64 or no "
          "reply)" % (label, len(cycles), discarded))
    print("  cycles      min=%d median=%.1f p90=%.1f max=%d"
          % (min(cycles), med, p90, max(cycles)))
    print("  cycles/byte min=%.2f median=%.2f p90=%.2f max=%.2f"
          % (min(cycles) / 64.0, med / 64.0, p90 / 64.0,
             max(cycles) / 64.0))
    print("  nanoseconds min=%.0f median=%.0f p90=%.0f max=%.0f "
          "(4 ns/cycle)"
          % (min(cycles) * NS_PER_CYCLE, med * NS_PER_CYCLE,
             p90 * NS_PER_CYCLE, max(cycles) * NS_PER_CYCLE))
    return med


# ---- 3. timing: host MATCH round-trip --------------------------------

def host_rtt(c, subject, n):
    """Wall-clock RTTs (seconds) for n MATCH_REQUESTs, plus timeouts."""
    t = pdev._make_transport(c)
    rtts, timeouts = [], 0
    try:
        eth = (bytes(c.dst_mac) + bytes(c.src_mac)
               + struct.pack(">H", pdev.ETHERTYPE))
        payload = struct.pack(">QHH", 0, 16, 0) + subject
        for i in range(n):
            seq = 0x7000 + i
            req = eth + pdev.encode_frame(pdev.KIND_MATCH_REQUEST, 1,
                                          seq, payload)
            t0 = time.perf_counter()
            t.send(req)
            deadline = time.monotonic() + c.probe_timeout_s
            got = None
            while time.monotonic() < deadline:
                reply = t.recv(max(0.0, deadline - time.monotonic()))
                if reply is None:
                    break
                reply = bytes(reply)
                if (len(reply) < 14 + pdev.PYRO_HEADER_LEN or
                        struct.unpack(">H", reply[12:14])[0]
                        != pdev.ETHERTYPE):
                    continue
                try:
                    dec = pdev.decode_frame(reply[14:])
                except pdev.PyroFrameError:
                    continue
                if (dec.kind != pdev.KIND_MATCH_REPLY
                        or dec.seq != seq):
                    continue
                if (len(dec.payload) >= 4 and
                        struct.unpack(">HH", dec.payload[0:4])[1]
                        & 0x4):
                    continue                   # wire reply: not ours
                got = time.perf_counter() - t0
                break
            if got is None:
                timeouts += 1
            else:
                rtts.append(got)
    finally:
        t.close()
    return rtts, timeouts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replies", type=int, default=50,
                    help="wire MATCH_REPLYs to capture (demo)")
    ap.add_argument("--capture", type=float, default=5.0,
                    help="max seconds to wait for the capture")
    ap.add_argument("--perf-n", type=int, default=200,
                    help="PERF samples per distribution")
    ap.add_argument("--perf-sleep", type=float, default=0.002,
                    help="sleep between PERF reads (s)")
    ap.add_argument("--rtt-n", type=int, default=50,
                    help="host MATCH round-trips to time")
    ap.add_argument("--model-only", action="store_true",
                    help="print the model expectation and exit "
                         "(no device access)")
    args = ap.parse_args()

    want_set = model_expectation()
    if args.model_only:
        return 0

    c = cfg()
    failures = []

    usable, reason = pdev.probe_device(c)
    print("probe: %s" % reason)
    if not usable:
        print("ABORT: device not usable — load the wire-scan child "
              "first:\n  PYRO_DEVICE_IFACE=%s .venv-pyro/bin/python3 "
              "scripts/pyro_hw.py load "
              "hw/dfx/build-wiretap/partials/overlay_wire.bit"
              % (c.iface or "ens2"))
        return 1
    if not loopback_on():
        return 1

    # ---- clean table first: the pure-scan floor ----------------------
    print()
    load(c, [CLEAN_ANCHOR], "clean")
    time.sleep(0.2)                  # let post-commit scans complete
    cyc_clean, disc = sample_perf(c, args.perf_n, args.perf_sleep)
    med_clean = report_dist("TIMING clean (scan floor, 0 noms)",
                            cyc_clean, disc, args.perf_n)
    if med_clean is None:
        failures.append("clean: no per-scan samples")

    # ---- matching table: demo capture, then timing under load --------
    print()
    st = load(c, MATCH_ANCHORS, "matching")
    demo_phase(c, want_set, st, args.replies, args.capture, failures)
    cyc_match, disc = sample_perf(c, args.perf_n, args.perf_sleep)
    med_match = report_dist("TIMING matching (3+ noms/frame)",
                            cyc_match, disc, args.perf_n)
    if med_match is None:
        failures.append("matching: no per-scan samples")

    # ---- back to clean, then host round-trips for contrast -----------
    print()
    load(c, [CLEAN_ANCHOR], "clean (final: stop the reply flood)")
    time.sleep(0.2)
    rtts, timeouts = host_rtt(c, txgen_frame(0), args.rtt_n)
    if rtts:
        us = sorted(r * 1e6 for r in rtts)
        print("HOST RTT: %d/%d replies (%d timeouts)  "
              "min=%.0f us median=%.0f us max=%.0f us"
              % (len(rtts), args.rtt_n, timeouts, us[0],
                 statistics.median(us), us[-1]))
    else:
        failures.append("host RTT: no MATCH_REPLYs")
    if timeouts:
        failures.append("host RTT: %d/%d timed out"
                        % (timeouts, args.rtt_n))

    print()
    if failures:
        print("WIRE_TIMING: FAIL")
        for f in failures:
            print("  - %s" % f)
        return 1
    print("WIRE_TIMING: PASS — wire scans median %.0f ns clean / "
          "%.0f ns matching (4 ns/cycle) vs host RTT median "
          "%.0f us; clean table resident, loopback ON"
          % (med_clean * NS_PER_CYCLE, med_match * NS_PER_CYCLE,
             statistics.median(us)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
