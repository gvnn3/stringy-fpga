"""Unit tests for pyro.quantify.traffic (fpga-vs-snort generator).

Covers: byte-for-byte determinism, manifest consistency against
payloads.bin, pcap structural sanity (magic, per-record lengths,
timestamp index encoding, checksum validity), and density accounting.
No hardware, no network: everything is file-based.
"""
import json
import os
import struct

import pytest

from pyro.quantify import traffic

ANCHORS = [b"GET /index", b"\x00\x01evil\xff", b"needle",
           None, b"needle", b""]
SEED = 1234


def _gen(outdir, *, seed=SEED, density=0.1, count=200,
         group_name="test/0", anchors=ANCHORS):
    return traffic.generate(str(outdir), anchors, seed=seed,
                            density=density, count=count,
                            group_name=group_name)


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _files(outdir):
    return {name: _read(os.path.join(str(outdir), name))
            for name in ("corpus.pcap", "payloads.bin",
                         "manifest.json")}


def _parse_pcap(data):
    """Return (linktype, [(ts_sec, ts_usec, frame), ...])."""
    (magic, vmaj, vmin, _tz, _sig, _snap,
     linktype) = struct.unpack("<IHHiIII", data[:24])
    assert magic == 0xA1B2C3D4
    assert (vmaj, vmin) == (2, 4)
    recs = []
    pos = 24
    while pos < len(data):
        ts_sec, ts_usec, incl, orig = struct.unpack(
            "<IIII", data[pos:pos + 16])
        pos += 16
        assert incl == orig
        frame = data[pos:pos + incl]
        assert len(frame) == incl
        pos += incl
        recs.append((ts_sec, ts_usec, frame))
    assert pos == len(data)
    return linktype, recs


def test_determinism_same_seed(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _gen(a)
    _gen(b)
    fa, fb = _files(a), _files(b)
    for name in fa:
        assert fa[name] == fb[name], name


def test_different_seed_differs(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _gen(a, seed=SEED)
    _gen(b, seed=SEED + 1)
    assert _read(os.path.join(str(a), "payloads.bin")) != \
        _read(os.path.join(str(b), "payloads.bin"))


def test_manifest_contract_fields(tmp_path):
    man = _gen(tmp_path)
    on_disk = json.loads(
        _read(os.path.join(str(tmp_path), "manifest.json")))
    assert on_disk == man
    assert man["schema"] == "pyro-quantify/1"
    assert man["seed"] == SEED
    assert man["group"] == "test/0"
    assert man["density"] == 0.1
    assert man["count"] == 200
    assert man["payloads_bin"] == "payloads.bin"
    assert len(man["packets"]) == 200
    assert [p["i"] for p in man["packets"]] == list(range(200))


def test_manifest_slices_reconstruct_payloads(tmp_path):
    man = _gen(tmp_path)
    blob = _read(os.path.join(str(tmp_path), "payloads.bin"))
    assert man["payload_bytes"] == len(blob)
    pos = 0
    for p in man["packets"]:
        assert p["off"] == pos
        assert traffic.MIN_PAYLOAD <= p["len"] <= traffic.MAX_PAYLOAD
        pos += p["len"]
    assert pos == len(blob)


def test_embedded_patterns_present_at_offset(tmp_path):
    man = _gen(tmp_path)
    blob = _read(os.path.join(str(tmp_path), "payloads.bin"))
    pats = traffic.unique_patterns(ANCHORS)
    seen = set()
    for p in man["packets"]:
        for h in p["hits"]:
            pb = pats[h["pat"]]
            at = p["off"] + h["at"]
            assert 0 <= h["at"] <= p["len"] - len(pb)
            assert blob[at:at + len(pb)] == pb
            seen.add(h["pat"])
    # With 20 embeds over 3 patterns, every pattern should appear.
    assert seen == set(range(len(pats)))


def test_pcap_structure_and_payload_identity(tmp_path):
    man = _gen(tmp_path)
    blob = _read(os.path.join(str(tmp_path), "payloads.bin"))
    linktype, recs = _parse_pcap(
        _read(os.path.join(str(tmp_path), "corpus.pcap")))
    assert linktype == 1
    assert len(recs) == man["count"]
    for p, (ts_sec, ts_usec, frame) in zip(man["packets"], recs):
        i = p["i"]
        assert ts_sec == i // 1000000
        assert ts_usec == i % 1000000
        assert len(frame) == 54 + p["len"]
        assert frame[54:] == blob[p["off"]:p["off"] + p["len"]]


def test_frame_headers_and_checksums(tmp_path):
    man = _gen(tmp_path, count=50)
    _lt, recs = _parse_pcap(
        _read(os.path.join(str(tmp_path), "corpus.pcap")))
    for p, (_s, _u, frame) in zip(man["packets"], recs):
        assert frame[12:14] == b"\x08\x00"       # EtherType IPv4
        ip = frame[14:34]
        assert ip[0] == 0x45
        total_len = struct.unpack("!H", ip[2:4])[0]
        assert total_len == 40 + p["len"]
        assert ip[9] == 6                        # proto TCP
        assert traffic._csum(ip) == 0            # IP header csum ok
        tcp = frame[34:54]
        tcp_len = 20 + p["len"]
        ph = ip[12:16] + ip[16:20] + struct.pack("!BBH", 0, 6,
                                                 tcp_len)
        assert traffic._csum(ph + tcp + frame[54:]) == 0
        sport, dport = struct.unpack("!HH", tcp[0:4])
        assert (sport, dport) == (12345, 80)


@pytest.mark.parametrize("density,count,expect",
                         [(0.0, 100, 0), (0.01, 100, 1),
                          (0.1, 250, 25), (1.0, 30, 30)])
def test_density_accounting(tmp_path, density, count, expect):
    man = _gen(tmp_path / ("d%g" % density), density=density,
               count=count)
    hit_pkts = [p for p in man["packets"] if p["hits"]]
    assert len(hit_pkts) == expect
    for p in hit_pkts:
        assert len(p["hits"]) == 1               # exactly one embed


def test_pattern_index_space_sorted_unique():
    pats = traffic.unique_patterns(ANCHORS)
    assert pats == sorted(set(pats))
    assert None not in pats and b"" not in pats
    assert pats == [b"\x00\x01evil\xff", b"GET /index", b"needle"]


def test_zero_count_and_validation(tmp_path):
    man = _gen(tmp_path / "z", density=0.0, count=0)
    assert man["packets"] == [] and man["payload_bytes"] == 0
    with pytest.raises(ValueError):
        _gen(tmp_path / "v1", density=1.5)
    with pytest.raises(ValueError):
        _gen(tmp_path / "v2", density=0.5, anchors=[None, b""])
    with pytest.raises(ValueError):
        _gen(tmp_path / "v3", density=0.5,
             anchors=[b"x" * (traffic.MAX_PAYLOAD + 1)])


def test_long_anchor_still_embeds(tmp_path):
    # An anchor longer than the smallest size bucket must force the
    # payload to grow to fit it.
    long_anchor = bytes(range(200)) + b"tail" * 10
    man = _gen(tmp_path, anchors=[long_anchor], density=1.0,
               count=20)
    blob = _read(os.path.join(str(tmp_path), "payloads.bin"))
    for p in man["packets"]:
        assert p["len"] >= len(long_anchor)
        h = p["hits"][0]
        at = p["off"] + h["at"]
        assert blob[at:at + len(long_anchor)] == long_anchor
