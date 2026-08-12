#!/usr/bin/env python3
"""Per-packet MAC digest timing on silicon (WIRE-MAC v0.3.0, R45a).

Host-fed digests through MAC_DIGEST_REQUEST (0x15): each request
feeds one raw Ethernet frame through the SAME byte-serial engine
path a wire frame takes, and the MAC_DIGEST_REPLY echoes the
engine's per-frame DIG_CYCLES/DIG_BYTES latches (CSRs 0x00C4/0x00C8,
R45a semantics: reset at frame start, latched on the evt) beside the
digest — so every sample is a true per-frame hardware measurement,
not a host round-trip time.

Frame classes, --samples each (default 200):

    ipv4_tcp_64       64 B  — directly comparable to the SNORT
                              1292/1628 ns wire-scan numbers
    ipv4_tcp_1474   1474 B  — the 0x15 codec cap (MAX_DIGEST_PACKET
                              1474 B); a full 1514 B wire frame
                              cannot be host-fed, an honest
                              limitation this script prints
    ipv4_udp_256     256 B
    ipv6_tcp_86       86 B
    vlan_ipv4_68      68 B  — 802.1Q tagged IPv4/TCP
    arp_60            60 B  — the skip_nonip path
    ipv4_tcp_64_nokey 64 B  — run BEFORE the key load: skip_nokey

For every reply the status and flags are compared against
pyro.macwire.digest_packet; for every digested class the digest must
equal the model EXACTLY and dig_bytes must equal the frame length.
The nokey class needs a freshly reset child (no committed key);
re-running against a child whose key is already live: --skip-nokey.
Zero-slack stats are re-verified after the sweep — host-fed frames
really are seen+digested/skipped, they just never enter the
MAC_REPORT batcher and never consume wire_seq.

Requires the multi-program MAC child resident (probe + ID check
against 0x01005E0A; --skip-id trusts whatever answers instead of
aborting — use only when a known derivative child is loaded).  CMAC
loopback is NOT touched by this script: wire traffic may interleave
freely, host-fed digests are lockstep request/reply and wait their
turn at the single engine.

Usage:
    PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 \\
        scripts/pyro_mac_timing.py [--samples 200] [--skip-nokey] \\
            [--skip-id]
"""
import argparse
import os
import statistics
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))

import pyro.device as pdev                      # noqa: E402
import pyro.macwire as mw                       # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPECT_CHILD_ID = 0x01005E0A
SLOT = 1
NS_PER_CYCLE = 4.0                              # 250 MHz fabric clock
KEY = bytes(range(16))                          # the SipHash paper key
KEY_ID = 1


# ---- deterministic frame builders ------------------------------------

def pat(n, seed=0):
    """A deterministic non-zero payload pattern of n bytes."""
    return bytes(((seed + 7 * i + 3) & 0xFF) for i in range(n))


def l2(ethertype, vlan_tci=None):
    hdr = (b"\x02\xaa\xbb\xcc\xdd\xee"
           + b"\x02\x11\x22\x33\x44\x55")
    if vlan_tci is not None:
        hdr += struct.pack(">HH", 0x8100, vlan_tci)
    return hdr + struct.pack(">H", ethertype)


def ipv4(payload, proto=6):
    total = 20 + len(payload)
    return (struct.pack(">BBHHHBBH", 0x45, 0, total, 7, 0x4000, 64,
                        proto, 0x1234)
            + b"\x0a\x00\x00\x01" + b"\x0a\x00\x00\x02" + payload)


def tcp(payload):
    return struct.pack(">HHIIBBHHH", 1234, 80, 1, 2, 5 << 4, 0x18,
                       4096, 0x5678, 0) + payload


def udp(payload):
    return struct.pack(">HHHH", 53, 53, 8 + len(payload),
                       0x9ABC) + payload


def ipv6(payload, nh=6):
    return (struct.pack(">IHBB", (6 << 28), len(payload), nh, 64)
            + bytes(range(0x20, 0x30)) + bytes(range(0x30, 0x40))
            + payload)


def ipv4_tcp(total, vlan_tci=None):
    over = len(l2(0x0800, vlan_tci)) + 20 + 20
    return l2(0x0800, vlan_tci) + ipv4(tcp(pat(total - over)))


def ipv4_udp(total):
    over = 14 + 20 + 8
    return l2(0x0800) + ipv4(udp(pat(total - over)), proto=17)


def ipv6_tcp(total):
    over = 14 + 40 + 20
    return l2(0x86DD) + ipv6(tcp(pat(total - over)))


def arp(total):
    body = (struct.pack(">HHBBH", 1, 0x0800, 6, 4, 1)
            + b"\x02\x11\x22\x33\x44\x55" + b"\x0a\x00\x00\x01"
            + b"\x00" * 6 + b"\x0a\x00\x00\x02")
    fr = l2(0x0806) + body
    return fr + b"\x00" * (total - len(fr))


def frame_classes():
    """[(name, frame)] — nokey is handled separately (before keying)."""
    out = [
        ("ipv4_tcp_64", ipv4_tcp(64)),
        ("ipv4_tcp_1474", ipv4_tcp(mw.MAX_DIGEST_PACKET)),
        ("ipv4_udp_256", ipv4_udp(256)),
        ("ipv6_tcp_86", ipv6_tcp(86)),
        ("vlan_ipv4_68", ipv4_tcp(68, vlan_tci=0x0064)),
        ("arp_60", arp(60)),
    ]
    for name, fr in out:
        want = int(name.rsplit("_", 1)[1])
        assert len(fr) == want, (name, len(fr))
    return out


# ---- transport conventions (pyro_mac_silicon.py) ---------------------

def roundtrip(cfg, kind, payload, timeout_s=2.0, seq=99, slot=SLOT):
    r = pdev._make_transport(cfg)
    try:
        eth = (bytes(cfg.dst_mac) + bytes(cfg.src_mac)
               + struct.pack(">H", pdev.ETHERTYPE))
        r.send(eth + pdev.encode_frame(kind, slot, seq, payload))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            raw = r.recv(0.5)
            if raw is None:
                continue
            raw = bytes(raw)
            if len(raw) < 14 or struct.unpack(
                    ">H", raw[12:14])[0] != pdev.ETHERTYPE:
                continue
            try:
                dec = pdev.decode_frame(raw[14:])
            except pdev.PyroFrameError:
                continue
            # Skip unsolicited frames (autonomous seq counters) by
            # kind/origin BEFORE the seq match, as macwire does:
            # MAC_REPORT and wire-origin MATCH_REPLYs (status bit2).
            if dec.kind == pdev.KIND_MAC_REPORT:
                continue
            if (dec.kind == pdev.KIND_MATCH_REPLY
                    and len(dec.payload) >= 4
                    and struct.unpack(">HH", dec.payload[0:4])[1] & 0x4):
                continue
            if dec.seq != seq:
                continue
            return dec
        return None
    finally:
        r.close()


def id_probe(cfg, seq=41):
    """rp_child_id from the ID_REPLY (payload[8:12] u32 BE), or None."""
    dec = roundtrip(cfg, pdev.KIND_ID_REQUEST, b"", seq=seq, slot=0)
    if dec is None or dec.kind != pdev.KIND_ID_REPLY \
            or len(dec.payload) < 12:
        return None
    return struct.unpack(">I", dec.payload[8:12])[0]


# ---- per-class measurement -------------------------------------------

def run_class(cfg, name, frame, key, samples, failures):
    """--samples digest requests for one frame class -> a table row.

    Every reply is checked against the golden model: reason and flags
    always; digest (EXACT) and dig_bytes == len(frame) whenever the
    model says digested.  Returns (row, ok_cycles) or (None, []).
    """
    want_dig, want_flags, want_reason = mw.digest_packet(key, frame)
    cycles, errs, noreply = [], [], 0
    for i in range(samples):
        rep = mw.digest_request(cfg, frame, slot=SLOT)
        if rep is None:
            noreply += 1
            continue
        bad = []
        if rep.reason != want_reason:
            bad.append("reason %s != %s" % (rep.reason, want_reason))
        if rep.flags != want_flags:
            bad.append("flags 0x%04x != 0x%04x"
                       % (rep.flags, want_flags))
        if want_reason == "digested":
            if rep.digest != want_dig:
                bad.append("digest 0x%016x != 0x%016x"
                           % (rep.digest, want_dig))
            if rep.dig_bytes != len(frame):
                bad.append("dig_bytes %d != %d"
                           % (rep.dig_bytes, len(frame)))
        if bad:
            if len(errs) < 3:
                errs.append("sample %d: %s" % (i, "; ".join(bad)))
            continue
        cycles.append(rep.dig_cycles)
    print("%s: %d/%d samples ok, %d no-reply, %d mismatched (%s)"
          % (name, len(cycles), samples, noreply,
             samples - noreply - len(cycles), want_reason))
    for e in errs:
        print("  - %s" % e)
    if noreply:
        failures.append("%s: %d/%d requests got no MAC_DIGEST_REPLY"
                        % (name, noreply, samples))
    if samples - noreply - len(cycles):
        failures.append("%s: %d replies deviated from the model"
                        % (name, samples - noreply - len(cycles)))
    if not cycles:
        failures.append("%s: no clean samples" % name)
        return None, []
    med = statistics.median(cycles)
    row = (name, len(frame), min(cycles), med, max(cycles),
           med / len(frame), med * NS_PER_CYCLE,
           max(cycles) * NS_PER_CYCLE)
    return row, cycles


def print_table(rows):
    """The notebook table format (docs/notebook.md wire-scan style)."""
    print()
    print("| Class             |    B | cyc min | cyc med | cyc max "
          "| cyc/B med | ns med | ns max |")
    print("|-------------------|------|---------|---------|---------"
          "|-----------|--------|--------|")
    for (name, nbytes, cmin, cmed, cmax, cpb, nsmed, nsmax) in rows:
        print("| %-17s | %4d | %7d | %7.1f | %7d | %9.2f | %6.0f "
              "| %6.0f |"
              % (name, nbytes, cmin, cmed, cmax, cpb, nsmed, nsmax))
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=200,
                    help="digest requests per frame class")
    ap.add_argument("--skip-nokey", action="store_true",
                    help="skip the pre-key skip_nokey class (a key "
                         "is already live on the child)")
    ap.add_argument("--skip-id", action="store_true",
                    help="skip the rp_child_id check (derivative "
                         "child loaded deliberately)")
    args = ap.parse_args()

    if not os.environ.get("PYRO_DEVICE_IFACE"):
        print("SKIP: PYRO_DEVICE_IFACE not set (R68 fail-closed)")
        return 2
    cfg = pdev.DeviceConfig()
    failures = []
    rows = []

    # ---- pre-flight: probe + identity ---------------------------------
    ok, reason = pdev.probe_device(cfg)
    print("probe: %s" % reason)
    if not ok:
        print("ABORT: device not usable — load the multi-MAC child "
              "first (scripts/pyro_mac_silicon.py)")
        return 1
    child = id_probe(cfg)
    if child is None:
        print("ABORT: no ID_REPLY from the child")
        return 1
    print("rp_child_id=0x%08x" % child)
    if child != EXPECT_CHILD_ID and not args.skip_id:
        print("ABORT: rp_child_id 0x%08x != expected 0x%08x "
              "(--skip-id to override)" % (child, EXPECT_CHILD_ID))
        return 1

    # ---- baseline stats ----------------------------------------------
    s0 = mw.read_mac_stats(cfg, slot=SLOT)
    if s0 is None:
        print("ABORT: no MAC_STAT_REPLY — MAC program not resident?")
        return 1
    print("baseline stats: %s" % (s0,))
    if not s0.zero_slack:
        failures.append("baseline zero-slack VIOLATION: %s" % (s0,))

    # ---- nokey class BEFORE any key load -----------------------------
    print()
    if args.skip_nokey:
        print("ipv4_tcp_64_nokey: skipped (--skip-nokey)")
    else:
        row, _ = run_class(cfg, "ipv4_tcp_64_nokey", ipv4_tcp(64),
                           None, args.samples, failures)
        if row is not None:
            rows.append(row)

    # ---- key load, keycheck-verified (fail closed) -------------------
    print()
    want = mw.key_check(KEY)
    ack = mw.load_key(cfg, KEY_ID, KEY, slot=SLOT)
    if ack is None:
        print("ABORT: no MAC_KEY_ACK — cannot key the engine")
        return 1
    print("key load: key_id=%d keycheck=0x%016x (want 0x%016x)"
          % (ack.key_id, ack.keycheck, want))
    if ack.key_id != KEY_ID or ack.keycheck != want:
        print("ABORT: keycheck mismatch — the device is NOT "
              "digesting under the host's key")
        return 1

    # ---- the keyed sweep ---------------------------------------------
    print()
    print("NOTE: the 0x15 codec caps host-fed frames at %d B "
          "(MAX_DIGEST_PACKET) — a full 1514 B wire frame cannot be "
          "host-fed, so the large class is the cap itself."
          % mw.MAX_DIGEST_PACKET)
    for name, frame in frame_classes():
        row, _ = run_class(cfg, name, frame, KEY, args.samples,
                           failures)
        if row is not None:
            rows.append(row)

    # ---- zero-slack re-verified after the sweep ----------------------
    print()
    time.sleep(0.2)
    s1 = mw.read_mac_stats(cfg, slot=SLOT)
    if s1 is None:
        failures.append("post-sweep MAC_STAT_REPLY missing")
    else:
        print("post-sweep stats: %s" % (s1,))
        ds = mw.MacStats(*(b - a for a, b in zip(s0, s1)))
        print("deltas: seen=+%d digested=+%d skip_nonip=+%d "
              "skip_nokey=+%d (host-fed frames DO count; wire "
              "traffic, if any, interleaves — loopback untouched)"
              % (ds.seen, ds.digested, ds.skip_nonip, ds.skip_nokey))
        if not s1.zero_slack:
            failures.append(
                "post-sweep zero-slack VIOLATION: seen %d != "
                "%d + %d + %d" % (s1.seen, s1.digested,
                                  s1.skip_nonip, s1.skip_nokey))

    if rows:
        print_table(rows)
    if failures:
        print("MAC_TIMING: FAIL")
        for f in failures:
            print("  - %s" % f)
        return 1
    print("MAC_TIMING: PASS — every reply digest matched "
          "digest_packet exactly; %d samples/class at 4 ns/cycle"
          % args.samples)
    return 0


if __name__ == "__main__":
    sys.exit(main())
