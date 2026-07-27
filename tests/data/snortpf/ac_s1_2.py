#!/usr/bin/env python3
"""AC-S1-2: drive the S1 pcap corpora through the resident child and map
MATCH_REPLY windows to (flow, gid:sid, offset) nominations (SR5/SR11/SR14).

Reads the client->server TCP payload out of each pcap (the daemon's job in
S3 is the same, minus reassembly), sends it as a MATCH_REQUEST, and maps
pattern_id -> gid:sid via the build's sidecar. Positive corpus MUST nominate
sid 1927; negative MUST nominate nothing.

Usage: PYRO_DEVICE_IFACE=ens2 python3 ac_s1_2.py <sidecar.json> <pos.pcap> <neg.pcap>
"""
import json
import struct
import sys
import time

sys.path.insert(0, "/home/gnn/Repos/Yale/stringy-fpga")
from pyro import device as pdev  # noqa: E402

SERVER_PORT = 21
OUT_CAP = 61          # max pyro_match entries per MATCH_REPLY (R78.7)


def read_pcap_c2s_payloads(path):
    """Yield (flow, payload) for client->server TCP segments carrying data."""
    with open(path, "rb") as f:
        blob = f.read()
    magic, = struct.unpack("<I", blob[:4])
    if magic != 0xA1B2C3D4:
        raise SystemExit(f"{path}: unsupported pcap magic 0x{magic:08x}")
    off = 24
    out = []
    while off + 16 <= len(blob):
        _ts, _us, caplen, _orig = struct.unpack("<IIII", blob[off:off + 16])
        off += 16
        pkt = blob[off:off + caplen]
        off += caplen
        if len(pkt) < 14 + 20 or struct.unpack(">H", pkt[12:14])[0] != 0x0800:
            continue
        ip = pkt[14:]
        ihl = (ip[0] & 0x0F) * 4
        if ip[9] != 6:
            continue
        src, dst = ip[12:16], ip[16:20]
        tcp = ip[ihl:]
        sport, dport = struct.unpack(">HH", tcp[0:4])
        doff = (tcp[12] >> 4) * 4
        payload = tcp[doff:]
        if dport != SERVER_PORT or not payload:
            continue
        flow = (".".join(map(str, src)), sport, ".".join(map(str, dst)), dport)
        out.append((flow, payload))
    return out


def match_once(cfg, corpus):
    """One MATCH_REQUEST/REPLY round trip; returns list of (start, end, pid)."""
    tr = pdev._make_transport(cfg)
    eth = (bytes(cfg.dst_mac) + bytes(cfg.src_mac)
           + struct.pack(">H", pdev.ETHERTYPE))
    seq = 23
    body = struct.pack(">QHH", 0, OUT_CAP, 0) + corpus  # start_off, out_cap, rsvd
    tr.send(eth + pdev.encode_frame(pdev.KIND_MATCH_REQUEST, 1, seq, body))
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        r = tr.recv(0.5)
        if r is None:
            continue
        r = bytes(r)
        if len(r) < 14 or struct.unpack(">H", r[12:14])[0] != pdev.ETHERTYPE:
            continue
        d = pdev.decode_frame(r[14:])
        if d.kind != pdev.KIND_MATCH_REPLY or d.seq != seq:
            continue
        count, status = struct.unpack(">HH", d.payload[0:4])
        if status & 1:
            print("  !! OVF set — result ring overflowed (R78.7)")
        entries = []
        for i in range(count):
            start, end, pid, _fl = struct.unpack_from("<QQII", d.payload,
                                                      8 + 24 * i)
            entries.append((start, end, pid))
        return entries
    raise SystemExit("no MATCH_REPLY within 2 s")


def main():
    sidecar_path, pos_pcap, neg_pcap = sys.argv[1:4]
    sidecar = json.load(open(sidecar_path))
    pid_map = {r["pattern_id"]: r for r in sidecar["rules"]}

    cfg = pdev.DeviceConfig()
    usable, reason = pdev.probe_device(cfg)
    print(reason)
    if not usable:                                  # SR18 SKIP discipline
        print("SKIP: device_usable=false")
        return 2

    rc = 0
    for label, path, expect in (("positive", pos_pcap, True),
                                ("negative", neg_pcap, False)):
        noms = []
        for flow, payload in read_pcap_c2s_payloads(path):
            for start, end, pid in match_once(cfg, payload):
                rule = pid_map.get(pid)
                if rule is None:
                    print(f"  !! pattern_id {pid} not in sidecar")
                    rc = 1
                    continue
                noms.append((flow, rule["gid"], rule["sid"], start, end,
                             payload[start:end]))
        print(f"\n{label} corpus: {len(noms)} nomination(s)")
        for flow, gid, sid, start, end, window in noms:
            print(f"  {flow[0]}:{flow[1]} -> {flow[2]}:{flow[3]}  "
                  f"{gid}:{sid} offset={start}..{end} window={window!r}")
        if expect and not noms:
            print("  FAIL: expected a nomination"); rc = 1
        if not expect and noms:
            print("  FAIL: expected zero nominations"); rc = 1

    print("\nAC-S1-2:", "PASS" if rc == 0 else "FAIL")
    return rc


if __name__ == "__main__":
    sys.exit(main())
