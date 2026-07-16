#!/usr/bin/env python3
"""Pipelined MATCH throughput over the R78 raw-Ethernet transport.

Measures aggregate scan throughput with W MATCH_REQUEST frames in flight
(window/credit loop) against the resident child, for several window sizes.
The child harness serializes frames (tready only in ST_RX), so this measures
the CHILD's end-to-end frame-processing ceiling once transport latency is
hidden — the number that decides how much a P2 char-dev data plane can buy
without new child RTL.

Run:  PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 scripts/pyro_pipeline_bench.py
"""
import os
import statistics
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pyro.device as pdev

MATCH_PREFIX = struct.Struct(">QHH")
CHUNK = pdev.MAX_PAYLOAD - MATCH_PREFIX.size          # 1474 B corpus per frame
TOTAL_BYTES = 4 << 20                                  # 4 MiB per window size
SLOT = 1
WINDOWS = (1, 2, 4, 8, 16, 32, 64)
TIMEOUT_S = 2.0


def run_window(cfg, transport, window):
    eth = (bytes(cfg.dst_mac) + bytes(cfg.src_mac)
           + struct.pack(">H", pdev.ETHERTYPE))
    corpus = b"\x78" * CHUNK
    payload = MATCH_PREFIX.pack(0, 8, 0) + corpus
    nframes = TOTAL_BYTES // CHUNK

    sent = recvd = 0
    inflight = set()
    t0 = time.perf_counter()
    deadline_slack = TIMEOUT_S
    while recvd < nframes:
        while sent < nframes and len(inflight) < window:
            seq = 1 + sent
            transport.send(eth + pdev.encode_frame(
                pdev.KIND_MATCH_REQUEST, SLOT, seq, payload))
            inflight.add(seq)
            sent += 1
        raw = transport.recv(deadline_slack)
        if raw is None:
            break                                       # timed out — report loss
        if len(raw) < 14 + pdev.PYRO_HEADER_LEN:
            continue
        if raw[12:14] != struct.pack(">H", pdev.ETHERTYPE):
            continue
        try:
            dec = pdev.decode_frame(raw[14:])
        except pdev.PyroFrameError:
            continue
        if dec.kind != pdev.KIND_MATCH_REPLY or dec.seq not in inflight:
            continue
        inflight.discard(dec.seq)
        recvd += 1
    wall = time.perf_counter() - t0
    bytes_scanned = recvd * CHUNK
    return {
        "window": window,
        "frames_sent": sent,
        "frames_ok": recvd,
        "lost": sent - recvd,
        "bytes": bytes_scanned,
        "wall_s": wall,
        "MiB_per_s": (bytes_scanned / wall) / (1 << 20) if wall else 0.0,
        "us_per_frame": (wall / recvd) * 1e6 if recvd else None,
    }


def main():
    cfg = pdev.DeviceConfig()
    usable, reason = pdev.probe_device(cfg)
    print(reason)
    if not usable:
        return 1
    results = []
    for w in WINDOWS:
        transport = pdev._EthTransport(cfg)
        try:
            r = run_window(cfg, transport, w)
        finally:
            transport.close()
        results.append(r)
        print(f"W={r['window']:>3}  {r['MiB_per_s']:8.1f} MiB/s  "
              f"{r['us_per_frame'] or 0:7.1f} us/frame  "
              f"ok={r['frames_ok']}/{r['frames_sent']}"
              + (f"  LOST={r['lost']}" if r["lost"] else ""))
    best = max(results, key=lambda r: r["MiB_per_s"])
    print(f"\nbest: W={best['window']} at {best['MiB_per_s']:.1f} MiB/s "
          f"({best['us_per_frame']:.1f} us/frame, "
          f"{CHUNK / (best['us_per_frame'] * 250.0):.3f} B/cyc-equivalent "
          f"at 250 MHz)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
