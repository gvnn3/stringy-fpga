#!/usr/bin/env python3
"""Pipelined MATCH throughput over the R78 raw-Ethernet transport.

Measures aggregate scan throughput with W MATCH_REQUEST frames in flight
(window/credit loop) against the resident child, for several window sizes.
The child harness serializes frames (tready only in ST_RX), so this measures
the CHILD's end-to-end frame-processing ceiling once transport latency is
hidden — the number that decides how much a P2 char-dev data plane can buy
without new child RTL.

Run:  PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3
scripts/pyro_pipeline_bench.py
"""
import os
import statistics
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pyro.device as pdev

MATCH_PREFIX = struct.Struct(">QHH")
# R78.9a: PYRO_BENCH_JUMBO=1 uses the jumbo payload bound — ONLY valid once
# the MAX_PKT_LEN=9600 shell is flashed (fail-closed default is 1518-shell).
MAX_PAYLOAD = (pdev.MAX_PAYLOAD_JUMBO if os.environ.get("PYRO_BENCH_JUMBO")
               else pdev.MAX_PAYLOAD)
CHUNK = MAX_PAYLOAD - MATCH_PREFIX.size               # corpus bytes per frame
TOTAL_BYTES = int(os.environ.get("PYRO_BENCH_TOTAL_MB")
                  or 4) << 20  # per window
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
                pdev.KIND_MATCH_REQUEST, SLOT, seq, payload,
                max_payload=MAX_PAYLOAD))
            inflight.add(seq)
            sent += 1
        raw = transport.recv(deadline_slack)
        if raw is None:
            # timed out — report loss
            break
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
    # P2d native loop (v2.7.0): on the char-dev transport the
    # interpreted credit loop costs more per frame than the whole hardware
    # budget, so the compiled loop measures (same R3b.3 compiled-build
    # discipline as the R3c router).  Requires exclusive queue access: the
    # cached Python transport is hard-closed first.
    if cfg.chardev is not None:
        try:
            from pyro import _fast
            native = _fast.dataplane_pipeline
        except (ImportError, AttributeError):
            native = None
        if native is not None:
            tr = pdev._CHARDEV_CACHE.pop(cfg.chardev, None)
            if tr is not None:
                tr._hard_close()
            time.sleep(0.2)   # let the zombie in-driver read drain
            # Multi-queue TX (5 GiB/s ladder): sibling ST nodes of the
            # configured queue, if the operator started them (accessible
            # check keeps this fail-closed to whatever the swap created).
            import glob as _glob
            prefix = cfg.chardev.rsplit("-", 1)[0]
            paths = tuple(p for p in sorted(_glob.glob(prefix + "-*"))
                          if os.access(p, os.R_OK | os.W_OK)) or (cfg.chardev,)
            # PYRO_BENCH_QUEUES=n caps the TX fan-out (loss isolation: 1 queue
            # vs many distinguishes host writer races from RTL arbitration).
            nq = int(os.environ.get("PYRO_BENCH_QUEUES") or len(paths))
            paths = paths[:max(1, nq)]
            print(f"native loop over {len(paths)} TX queue(s)")
            best = None
            for w in WINDOWS:
                (recvd, n, wall, t_active, n_status, n_dup, n_other,
                 n_retx) = native(paths, SLOT, CHUNK, TOTAL_BYTES, w)
                mib = recvd * CHUNK / wall / (1 << 20) if wall else 0.0
                usf = wall / recvd * 1e6 if recvd else 0.0
                lost = n - recvd
                print(f"W={w:>3}  {mib:8.1f} MiB/s  {usf:7.1f} us/frame  "
                      f"ok={recvd}/{n}"
                      + (f"  LOST={lost}" if lost else "")
                      + (f"  RETX={n_retx}" if n_retx else "")
                      + (f"  [status={n_status} dup={n_dup} other={n_other}]"
                         if (n_status or n_dup or n_other) else ""))
                if not lost and (best is None or mib > best[1]):
                    best = (w, mib, usf)
            if best:
                print(f"\nbest (native, zero-loss): W={best[0]} at "
                      f"{best[1]:.1f} MiB/s = {best[1]/1024:.3f} GiB/s "
                      f"({best[2]:.2f} us/frame, "
                      f"{CHUNK / (best[2] * 250.0):.3f} B/cyc-equivalent "
                      f"at 250 MHz)")
            return 0
    results = []
    for w in WINDOWS:
        transport = pdev._make_transport(cfg)
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
