#!/usr/bin/env python3
"""SNORT-PF filter daemon CLI (AC-S3-1): tap → classify → nominate → stats.

Modes:

* ``--model`` (default): device-free — the resident group is served by its
  software twin (SR16's model side).  Everything else is the real daemon:
  variable table, histogram, tripwires, SR14 identity gate, SR19 stats.
* ``--wire``: R78 transport over the onic control binding; requires
  ``PYRO_DEVICE_IFACE`` (no default — F3/A4) and a loaded group child.

The SR10 scheduler runs on a tick loop; with ``--wire`` it swaps groups by
JTAG partial reconfiguration (SF20: ~14–45 s blind window, SR19-visible).
Bitstreams come from the SR9 cache / .superpowers/pr-builds artifacts of
the S3 batch build.

Stats (SR19) are appended as JSON lines to ``--stats-out`` every
``--stats-interval`` seconds and dumped on SIGUSR1.

Traffic input: ``--pcap FILE`` replays a capture through the daemon
(deterministic, device-free friendly); ``--tap`` binds AF_PACKET on the
operator-named interface (CAP_NET_RAW; control binding must be active —
see scripts/pyro_dataplane_swap.sh).
"""

import argparse
import json
import os
import signal
import struct
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

RULES = os.path.join(REPO, "third_party", "snort3-community-rules",
                     "snort3-community.rules")


def read_pcap(path):
    """Yield (flow_key_tuple, payload) for TCP/UDP packets in a pcap."""
    with open(path, "rb") as fh:
        hdr = fh.read(24)
        if len(hdr) < 24:
            return
        magic = struct.unpack("<I", hdr[:4])[0]
        endian = "<" if magic in (0xA1B2C3D4, 0xA1B23C4D) else ">"
        while True:
            rec = fh.read(16)
            if len(rec) < 16:
                return
            _ts, _us, incl, _orig = struct.unpack(endian + "IIII", rec)
            frame = fh.read(incl)
            if len(frame) < 34 or frame[12:14] != b"\x08\x00":
                continue
            ihl = (frame[14] & 0x0F) * 4
            proto = frame[23]
            if proto not in (6, 17):
                continue
            src = ".".join(str(b) for b in frame[26:30])
            dst = ".".join(str(b) for b in frame[30:34])
            l4 = 14 + ihl
            if proto == 6:
                if len(frame) < l4 + 20:
                    continue
                sport, dport = struct.unpack("!HH", frame[l4:l4 + 4])
                doff = (frame[l4 + 12] >> 4) * 4
                payload = frame[l4 + doff:]
                key = ("tcp", src, sport, dst, dport)
            else:
                sport, dport = struct.unpack("!HH", frame[l4:l4 + 4])
                payload = frame[l4 + 8:]
                key = ("udp", src, sport, dst, dport)
            if payload:
                yield key, payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--pcap", help="replay this capture through the daemon")
    src.add_argument("--tap", action="store_true",
                     help="AF_PACKET tap on PYRO_DEVICE_IFACE")
    ap.add_argument("--wire", action="store_true",
                    help="R78 wire transport (default: model twin)")
    ap.add_argument("--rules", default=RULES)
    ap.add_argument("--stats-out", default="-",
                    help="JSONL stats sink (default stdout)")
    ap.add_argument("--stats-interval", type=float, default=10.0)
    ap.add_argument("--tick-interval", type=float, default=5.0,
                    help="SR10 scheduler tick period")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="stop after N seconds (0 = run until EOF/signal)")
    args = ap.parse_args()

    from pyro.snort import daemon as D
    from pyro.snort import groups as G
    from pyro.snort import scheduler as SCH
    from pyro.snort import triage as T

    triaged = T.triage_file(args.rules)
    all_groups = G.pack_groups(triaged)
    dm = D.FilterDaemon()

    if args.wire:
        iface = os.environ.get("PYRO_DEVICE_IFACE")
        if not iface:
            print("PYRO_DEVICE_IFACE not configured (no default; F3/A4)",
                  file=sys.stderr)
            return 2
        from pyro.synth import cache as C
        from pyro.hdl import generator as gen
        from pyro.synth.toolchain import (SHELL_VERSION,
                                          VIVADO_TOOLCHAIN_VERSION)
        cache = C.BitstreamCache()

        def key_of(g):
            return g.bitstream_key(gen.GENERATOR_VERSION,
                                   gen.HARNESS_VERSION,
                                   VIVADO_TOOLCHAIN_VERSION, SHELL_VERSION, 1)

        def available(g):
            e = cache.get(key_of(g))
            return e is not None and len(e.read_payload()) > 0

        def load(group):
            entry = cache.get(key_of(group))
            bit = os.path.join(os.path.dirname(str(entry.path))
                               if hasattr(entry, "path") else "",
                               "artifact.bin")
            return SCH.jtag_load_pipeline(group, bit, iface, dm.stats)
    else:
        from pyro._circuit_model import GroupCircuitModel

        def available(g):
            return True

        def load(group):
            circuit = G.group_circuit(group)
            model = GroupCircuitModel(circuit)
            model.resident = True
            return D.NominationPipeline(D.ModelTransport(group, model),
                                        dm.stats)

    sch = SCH.ResidencyScheduler(dm, all_groups, load, available=available)
    out = sys.stdout if args.stats_out == "-" else open(args.stats_out, "a")

    def dump(_sig=None, _frm=None):
        rec = {"t": time.time(), "stats": dm.stats.snapshot(),
               "mix_by_class": dm.histogram.snapshot_by_class(),
               "mix_by_port": {str(p): round(v, 1) for p, v
                               in dm.histogram.snapshot().items()}}
        out.write(json.dumps(rec, sort_keys=True) + "\n")
        out.flush()

    signal.signal(signal.SIGUSR1, dump)
    t0 = time.time()
    last_stats = last_tick = 0.0

    def housekeeping():
        nonlocal last_stats, last_tick
        now = time.monotonic()
        if now - last_tick >= args.tick_interval:
            swapped = sch.tick()
            if swapped:
                print("[daemon] resident -> %s" % swapped, file=sys.stderr)
            last_tick = now
        if now - last_stats >= args.stats_interval:
            dump()
            last_stats = now

    n_noms = 0
    if args.pcap:
        for key, payload in read_pcap(args.pcap):
            noms = dm.feed(D.FlowKey(*key), payload)
            n_noms += len(noms)
            for n in noms:
                if n.kind == "match":
                    print("[nominate] %d:%d flow=%s:%d->%s:%d end=%d"
                          % (n.gid, n.sid, n.flow.src_ip, n.flow.src_port,
                             n.flow.dst_ip, n.flow.dst_port, n.end_off),
                          file=sys.stderr)
            housekeeping()
            if args.duration and time.time() - t0 > args.duration:
                break
        dump()
        print("[daemon] pcap done: %d segments, %d nominations"
              % (dm.stats.segments, n_noms), file=sys.stderr)
    else:
        iface = os.environ.get("PYRO_DEVICE_IFACE")
        if not iface:
            print("PYRO_DEVICE_IFACE not configured (no default; F3/A4)",
                  file=sys.stderr)
            return 2
        stop = {"v": False}
        signal.signal(signal.SIGINT, lambda *_: stop.update(v=True))
        signal.signal(signal.SIGTERM, lambda *_: stop.update(v=True))

        import threading

        def ticker():
            while not stop["v"]:
                housekeeping()
                time.sleep(0.5)
                if args.duration and time.time() - t0 > args.duration:
                    stop["v"] = True

        th = threading.Thread(target=ticker, daemon=True)
        th.start()
        D.afpacket_tap(dm, iface, should_stop=lambda: stop["v"])
        dump()
    return 0


if __name__ == "__main__":
    sys.exit(main())
