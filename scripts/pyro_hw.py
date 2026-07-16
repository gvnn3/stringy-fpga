#!/usr/bin/env python3
"""PYRO Phase 2b hardware bring-up CLI — thin wrapper over ``pyro.device``.

Subcommands (all read-only on the host; ``load`` reconfigures ``pyro_rp`` over
JTAG per R85 — the spec-sanctioned partial-load path; it never touches the
static shell or the PCIe link):

    probe                     R83/R84 ID_REQUEST probe (needs CAP_NET_RAW -> sudo)
    load  <partial.bit>       R85/R86.5 JTAG partial load via hw_server, then the
                              R85a in-band recovery (sudo -n pyro_wedge_recover.sh
                              -- needs the NOPASSWD sudoers grant for that script)
    match <corpus> [--slot N] R78.6/R78.7 MATCH round-trip (needs CAP_NET_RAW)
    perf  [--slot N]          R78.11 CYCLES/BYTES read-out (needs CAP_NET_RAW)

Examples:
    sudo PYTHONPATH=. python3 scripts/pyro_hw.py probe
    PYTHONPATH=. python3 scripts/pyro_hw.py load .../pattern_<hash>_partial.bit
    sudo PYTHONPATH=. python3 scripts/pyro_hw.py match abcabc --slot 1
    sudo PYTHONPATH=. python3 scripts/pyro_hw.py perf --slot 1
"""
import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyro.device as pdev


def _cfg():
    return pdev.DeviceConfig()


def cmd_probe(_args):
    usable, reason = pdev.probe_device(_cfg())
    print(reason)
    return 0 if usable else 1


def cmd_load(args):
    t0 = time.time()
    pdev.load_partial(_cfg(), args.bitstream)
    print(f"load_partial OK in {time.time() - t0:.1f}s: {args.bitstream}")
    return 0


def _roundtrip(kind, slot, payload, what):
    """One request/reply over the raw-Ethernet transport (R86.7)."""
    cfg = _cfg()
    transport = pdev._EthTransport(cfg)  # bring-up tool: private transport OK
    try:
        eth = (bytes(cfg.dst_mac) + bytes(cfg.src_mac)
               + struct.pack(">H", pdev.ETHERTYPE))
        for attempt in range(cfg.probe_attempts):
            seq = attempt + 1
            transport.send(eth + pdev.encode_frame(kind, slot, seq, payload))
            deadline = time.monotonic() + cfg.probe_timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                reply = transport.recv(remaining)
                if reply is None:
                    break
                if (len(reply) < 14 + pdev.PYRO_HEADER_LEN or
                        struct.unpack(">H", reply[12:14])[0] != pdev.ETHERTYPE):
                    continue
                try:
                    dec = pdev.decode_frame(bytes(reply)[14:])
                except pdev.PyroFrameError:
                    continue
                if dec.seq != seq:
                    continue
                return dec
        print(f"{what}: no reply (R84 budget exhausted)")
        return None
    finally:
        transport.close()


def cmd_match(args):
    corpus = args.corpus.encode()
    payload = struct.pack(">QHH", 0, args.out_cap, 0) + corpus
    t0 = time.perf_counter()
    dec = _roundtrip(pdev.KIND_MATCH_REQUEST, args.slot, payload, "MATCH")
    dt = time.perf_counter() - t0
    if dec is None:
        return 1
    if dec.kind == pdev.KIND_STATUS:
        code = struct.unpack(">I", dec.payload[0:4])[0]
        print(f"STATUS/ERROR code={code} "
              f"({'PYRO_E_NOT_RESIDENT' if code == 7 else 'see R38'})")
        return 1
    if dec.kind != pdev.KIND_MATCH_REPLY:
        print(f"unexpected reply kind 0x{dec.kind:02x}")
        return 1
    count, status = struct.unpack(">HH", dec.payload[0:4])
    print(f"MATCH_REPLY count={count} status=0x{status:04x} "
          f"(OVF={status & 1}) rtt={dt*1e6:.0f}us")
    for i in range(count):
        s, e, pid, fl = struct.unpack_from("<QQII", dec.payload, 8 + 24 * i)
        print(f"  entry[{i}]: start={s} end={e} pattern_id={pid} flags=0x{fl:x}"
              f"  (candidate window; host re-verifies per R78.7)")
    return 0


def cmd_perf(args):
    got = pdev.read_perf_counters(_cfg(), slot=args.slot)
    if got is None:
        print("counters unavailable (no PERF_REPLY — pre-2.4.0 child or "
              "non-resident slot, R78.11)")
        return 1
    cycles, nbytes = got
    print(f"CYCLES={cycles} BYTES={nbytes}", end="")
    if cycles:
        t_clk_ns = 4.0  # 250 MHz core clock (F4)
        gbps = nbytes / (cycles * t_clk_ns)  # bytes/ns == GB/s
        print(f"  scan={cycles * t_clk_ns:.0f}ns "
              f"util={nbytes / cycles:.3f} B/cyc  {gbps:.3f} GB/s on-chip", end="")
    print()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    p = sub.add_parser("load")
    p.add_argument("bitstream")
    p = sub.add_parser("match")
    p.add_argument("corpus")
    p.add_argument("--slot", type=int, default=1)
    p.add_argument("--out-cap", type=int, default=61)
    p = sub.add_parser("perf")
    p.add_argument("--slot", type=int, default=1)
    args = ap.parse_args()
    return {"probe": cmd_probe, "load": cmd_load,
            "match": cmd_match, "perf": cmd_perf}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
