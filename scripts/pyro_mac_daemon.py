#!/usr/bin/env python3
"""MAC digest daemon CLI: key load -> schedule -> stream digests.

Host side of the P1 SipHash-2-4 per-packet MAC program (multi-program
MAC design contract).  Operations, in the order they run when
combined:

* ``--load-key KEY`` (hex string, or a file of 16 raw bytes / hex)
  with ``--key-id N``: commits the key via MAC_KEY_LOAD and VERIFIES
  the MAC_KEY_ACK keycheck against the host's own
  SipHash-2-4(key, "PYROMACKEYCHECK1") — a mismatch is a hard failure
  because the device would be digesting under a different key.
* ``--sched MODE[/QUANTUM]`` (``p0``/``p1``/``rr``/``both`` or
  ``0``/``1``/``2``/``3``; quantum >= 1, default 1): sets the
  wire-frame dispatch via SCHED_SET and refuses to continue on a
  nonzero ack status.  ``both`` is mode 3 (broadcast): every wire
  frame is delivered to BOTH programs — full coverage instead of RR
  isolation, and each program counts every frame in its own seen.
* ``--stats``: one-shot MAC_STAT_REPLY dump with the zero-slack check
  (seen == digested + skip_nonip + skip_nokey EXACTLY).
* ``--watch``: streams unsolicited MAC_REPORT frames (kind-filtered
  only — reports carry an autonomous wire_seq, never an echoed seq),
  printing one line per record; every ``--stats-interval`` seconds it
  re-reads the counters, verifies zero-slack, and prints
  ``records_lost`` honestly (a loss bit or a climbing lost counter is
  reported, never suppressed).

Transport comes from DeviceConfig: PYRO_QDMA_CHARDEV wins when the
P2d data plane is swapped in, else the AF_PACKET control binding on
PYRO_DEVICE_IFACE (no default — F3/A4).

Exit status: 0 ok, 1 device/verification failure, 2 usage/config.
"""

import argparse
import os
import signal
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import pyro.device as pdev      # noqa: E402
import pyro.macwire as mw       # noqa: E402

_MODES = {"p0": mw.SCHED_P0_ONLY, "0": mw.SCHED_P0_ONLY,
          "p1": mw.SCHED_P1_ONLY, "1": mw.SCHED_P1_ONLY,
          "rr": mw.SCHED_RR, "2": mw.SCHED_RR,
          "both": mw.SCHED_BROADCAST, "3": mw.SCHED_BROADCAST}
_MODE_NAMES = {mw.SCHED_P0_ONLY: "p0-only", mw.SCHED_P1_ONLY: "p1-only",
               mw.SCHED_RR: "round-robin",
               mw.SCHED_BROADCAST: "broadcast"}


def parse_key(text):
    """A 16-byte key from a hex string or a file (raw 16 B or hex)."""
    if os.path.isfile(text):
        with open(text, "rb") as fh:
            data = fh.read()
        if len(data) == 16:
            return data
        text = data.decode("ascii", "replace")
    clean = text.strip().replace(":", "").replace(" ", "")
    if clean.lower().startswith("0x"):
        clean = clean[2:]
    try:
        key = bytes.fromhex(clean)
    except ValueError:
        raise SystemExit("bad key %r: not hex and not a key file" % text)
    if len(key) != 16:
        raise SystemExit(
            "key must be 16 bytes (32 hex digits), got %d" % len(key))
    return key


def parse_sched(text):
    """``MODE[/QUANTUM]`` -> (mode, quantum)."""
    part = text.strip().lower().split("/")
    if part[0] not in _MODES or len(part) > 2:
        raise SystemExit(
            "bad --sched %r: MODE[/QUANTUM], MODE in "
            "p0|p1|rr|both|0|1|2|3" % text)
    quantum = 1
    if len(part) == 2:
        try:
            quantum = int(part[1])
        except ValueError:
            raise SystemExit("bad --sched quantum %r" % part[1])
        if not 1 <= quantum <= 0xFFFF:
            raise SystemExit("--sched quantum must be 1..65535")
    return _MODES[part[0]], quantum


def print_stats(st, prefix="[mac]"):
    """One honest line per counter read, zero-slack verified."""
    slack = st.seen - (st.digested + st.skip_nonip + st.skip_nokey)
    print("%s stats: seen=%d digested=%d skip_nonip=%d skip_nokey=%d "
          "reports_sent=%d records_lost=%d"
          % (prefix, st.seen, st.digested, st.skip_nonip,
             st.skip_nokey, st.reports_sent, st.records_lost))
    if not st.zero_slack:
        print("%s ZERO-SLACK VIOLATION: seen - accounted = %d "
              "(invariant: seen == digested + skip_nonip + skip_nokey)"
              % (prefix, slack))
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--load-key", metavar="KEY",
                    help="commit this 16-byte key (hex or file)")
    ap.add_argument("--key-id", type=int, default=1,
                    help="key_id for --load-key (default 1)")
    ap.add_argument("--sched", metavar="MODE[/QUANTUM]",
                    help="set dispatch: p0|p1|rr|both[/quantum] "
                         "(both = mode 3 broadcast)")
    ap.add_argument("--stats", action="store_true",
                    help="one-shot counter read + zero-slack check")
    ap.add_argument("--watch", action="store_true",
                    help="stream MAC_REPORT digests")
    ap.add_argument("--slot", type=int, default=1)
    ap.add_argument("--duration", type=float, default=0.0,
                    help="stop --watch after N seconds (0 = forever)")
    ap.add_argument("--stats-interval", type=float, default=10.0,
                    help="zero-slack verification period in --watch")
    args = ap.parse_args()
    if not (args.load_key or args.sched or args.stats or args.watch):
        ap.error("nothing to do: need --load-key/--sched/--stats/--watch")

    c = pdev.DeviceConfig()
    if c.iface is None and c.chardev is None:
        print("no transport configured: set PYRO_DEVICE_IFACE or "
              "PYRO_QDMA_CHARDEV (no default; F3/A4)", file=sys.stderr)
        return 2

    if args.load_key:
        key = parse_key(args.load_key)
        ack = mw.load_key(c, args.key_id, key, slot=args.slot)
        if ack is None:
            print("FAIL: no MAC_KEY_ACK — is the MAC child resident? "
                  "(unknown kinds are dropped, R78.4)")
            return 1
        want = mw.key_check(key)
        if ack.key_id != args.key_id or ack.keycheck != want:
            print("FAIL: keycheck mismatch — device key_id=%d "
                  "keycheck=0x%016x, host expects key_id=%d "
                  "0x%016x; the device is NOT using our key"
                  % (ack.key_id, ack.keycheck, args.key_id, want))
            return 1
        print("[mac] key %d committed; keycheck 0x%016x verified"
              % (ack.key_id, ack.keycheck))

    if args.sched:
        mode, quantum = parse_sched(args.sched)
        ack = mw.set_sched(c, mode, quantum, slot=args.slot)
        if ack is None:
            print("FAIL: no SCHED_ACK")
            return 1
        if ack.status != 0:
            print("FAIL: device refused schedule (status=%d); "
                  "settings unchanged" % ack.status)
            return 1
        print("[mac] schedule: %s quantum=%d"
              % (_MODE_NAMES.get(ack.mode, ack.mode), ack.quantum))

    if args.stats:
        st = mw.read_mac_stats(c, slot=args.slot)
        if st is None:
            print("FAIL: no MAC_STAT_REPLY")
            return 1
        if not print_stats(st):
            return 1

    if not args.watch:
        return 0

    stop = {"v": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(v=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(v=True))
    t0 = time.monotonic()
    last_stats = time.monotonic()
    n_records = 0
    n_loss_reports = 0
    slack_ok = True
    with mw.MacReportListener(c) as lis:
        while not stop["v"]:
            rep = lis.poll(0.5)
            if rep is not None:
                if rep.loss:
                    n_loss_reports += 1
                    print("[mac] RECORD LOSS flagged in report "
                          "seq=%u" % rep.seq)
                for r in rep.records:
                    n_records += 1
                    print("[mac] wire_seq=%u len=%u flags=0x%04x "
                          "digest=%016x key_id=%u"
                          % (r.wire_seq, r.pkt_len, r.flags,
                             r.digest, rep.key_id))
            now = time.monotonic()
            if now - last_stats >= args.stats_interval:
                st = mw.read_mac_stats(c, slot=args.slot)
                if st is None:
                    print("[mac] stats unavailable this period")
                else:
                    if not print_stats(st):
                        slack_ok = False
                    if st.records_lost:
                        print("[mac] cumulative records_lost=%d — "
                              "digests are being dropped, the stream "
                              "above is INCOMPLETE" % st.records_lost)
                last_stats = now
            if args.duration and now - t0 >= args.duration:
                break
    print("[mac] watch done: %d records, %d loss-flagged reports"
          % (n_records, n_loss_reports))
    return 0 if slack_ok else 1


if __name__ == "__main__":
    sys.exit(main())
