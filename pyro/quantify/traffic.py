"""Seeded pcap + manifest traffic generator (fpga-vs-snort study).

Generates the synthetic corpus for one (group, density) cell of the
experiment in docs/studies/fpga-vs-snort.md section 2:

  corpus.pcap    TCP/IPv4/Ethernet frames with correct IP and TCP
                 checksums.  The pcap timestamp encodes the packet
                 index (ts_sec = i // 1000000, ts_usec = i % 1000000)
                 so Snort alert timestamps identify packets exactly.
  payloads.bin   The concatenated TCP payloads, byte-identical to the
                 wire bytes, so the FPGA driver scans the same data in
                 the same order.
  manifest.json  Locates every payload inside payloads.bin ("off",
                 "len") and records the generator's embedded pattern
                 placements in "hits".  "hits" is generator knowledge,
                 NOT the oracle verdict: a packet may also match by
                 accident; parity uses the oracle.

Pattern identity: within a group, patterns are indexed by position in
the sorted unique anchor byte-string list (index 0..P-1).  The Snort
side derives sid = 1000000 + pattern_index from the same list.

Pure stdlib.  All randomness flows through one random.Random(seed), so
a given (seed, anchors, density, count) input is byte-for-byte
reproducible.
"""

import json
import os
import random
import struct

SCHEMA = "pyro-quantify/1"

# Fixed payload-size mix (sizes in [64, 1460]).  The classic 7:4:1
# IMIX shape adapted to TCP payload sizes: mostly small packets, a
# band of mediums, some full-size segments.
MIN_PAYLOAD = 64
MAX_PAYLOAD = 1460
SIZE_MIX = ((64, 7), (576, 4), (1460, 1))
_SIZES = tuple(s for s, _w in SIZE_MIX)
_CUM = []
for _s, _w in SIZE_MIX:
    _CUM.append(_w + (_CUM[-1] if _CUM else 0))
_CUM = tuple(_CUM)

# Fixed frame skeleton (same approach as the validated mkpcap.py
# builder): locally-administered MACs, 10.0.0.1 -> 10.0.0.2,
# TCP 12345 -> 80, PSH|ACK.
_ETH = (b"\x02\x00\x00\x00\x00\x01"      # dst MAC
        b"\x02\x00\x00\x00\x00\x02"      # src MAC
        b"\x08\x00")                     # EtherType IPv4
_SRC_IP = b"\x0a\x00\x00\x01"
_DST_IP = b"\x0a\x00\x00\x02"
_SPORT = 12345
_DPORT = 80

_PCAP_MAGIC = 0xA1B2C3D4
_PCAP_GLOBAL = struct.pack("<IHHiIII", _PCAP_MAGIC, 2, 4, 0, 0,
                           65535, 1)     # linktype 1 = Ethernet


def _csum(b):
    """RFC 1071 ones-complement checksum of b."""
    if len(b) % 2:
        b += b"\0"
    s = sum(struct.unpack("!%dH" % (len(b) // 2), b))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def build_frame(payload, index):
    """One Ethernet/IPv4/TCP frame carrying payload.

    index varies the IP id and TCP sequence number so frames are not
    all identical on the wire; IP and TCP checksums are correct.
    """
    tcp_len = 20 + len(payload)
    ip = struct.pack("!BBHHHBBH", 0x45, 0, 20 + tcp_len,
                     index & 0xFFFF, 0x4000, 64, 6, 0)
    ip += _SRC_IP + _DST_IP
    ip = ip[:10] + struct.pack("!H", _csum(ip)) + ip[12:]
    tcp = struct.pack("!HHIIBBHHH", _SPORT, _DPORT,
                      index & 0xFFFFFFFF, 0, 0x50, 0x18, 8192, 0, 0)
    ph = _SRC_IP + _DST_IP + struct.pack("!BBH", 0, 6, tcp_len)
    ck = _csum(ph + tcp + payload)
    tcp = tcp[:16] + struct.pack("!H", ck) + tcp[18:]
    return _ETH + ip + tcp + payload


def unique_patterns(anchors):
    """Sorted unique anchor byte-strings; the pattern index space.

    None and empty entries (tombstones) contribute nothing.  Position
    in the returned list IS the pattern index used in manifest "hits"
    and in the Snort sid mapping (sid = 1000000 + index).
    """
    return sorted({bytes(a) for a in anchors if a})


def _draw_size(rng):
    return rng.choices(_SIZES, cum_weights=_CUM)[0]


def generate(outdir, anchors, *, seed, density, count, group_name):
    """Write corpus.pcap, payloads.bin, manifest.json under outdir.

    anchors: the group's anchor byte-strings (tombstones as None are
    tolerated and skipped).  density: fraction of packets carrying
    exactly one embedded pattern; the embedded packet count is
    round(density * count), packets chosen without replacement, the
    pattern chosen uniformly from the group's unique anchors and
    placed at a random valid offset.  Returns the manifest dict.
    """
    if not 0.0 <= density <= 1.0:
        raise ValueError("density must be in [0, 1]: %r" % (density,))
    if count < 0:
        raise ValueError("count must be >= 0: %r" % (count,))
    patterns = unique_patterns(anchors)
    n_embed = int(round(density * count))
    if n_embed > 0:
        if not patterns:
            raise ValueError("density > 0 but no live anchors")
        too_long = [p for p in patterns if len(p) > MAX_PAYLOAD]
        if too_long:
            raise ValueError("anchor longer than %d bytes: %r"
                             % (MAX_PAYLOAD, too_long[0]))

    rng = random.Random(seed)
    embed = frozenset(rng.sample(range(count), n_embed))

    os.makedirs(outdir, exist_ok=True)
    packets = []
    off = 0
    pcap_path = os.path.join(outdir, "corpus.pcap")
    bin_path = os.path.join(outdir, "payloads.bin")
    with open(pcap_path, "wb") as fpcap, open(bin_path, "wb") as fbin:
        fpcap.write(_PCAP_GLOBAL)
        for i in range(count):
            size = _draw_size(rng)
            hits = []
            if i in embed:
                pat = rng.randrange(len(patterns))
                pbytes = patterns[pat]
                size = max(size, len(pbytes))
                at = rng.randrange(size - len(pbytes) + 1)
                buf = bytearray(rng.randbytes(size))
                buf[at:at + len(pbytes)] = pbytes
                payload = bytes(buf)
                hits.append({"pat": pat, "at": at})
            else:
                payload = rng.randbytes(size)
            frame = build_frame(payload, i)
            fpcap.write(struct.pack("<IIII", i // 1000000,
                                    i % 1000000, len(frame),
                                    len(frame)))
            fpcap.write(frame)
            fbin.write(payload)
            packets.append({"i": i, "off": off, "len": len(payload),
                            "hits": hits})
            off += len(payload)

    manifest = {
        "schema": SCHEMA,
        "seed": seed,
        "group": group_name,
        "density": density,
        "count": count,
        "payload_bytes": off,
        "payloads_bin": "payloads.bin",
        "packets": packets,
    }
    with open(os.path.join(outdir, "manifest.json"), "w") as f:
        json.dump(manifest, f, sort_keys=True, separators=(",", ":"))
        f.write("\n")
    return manifest
