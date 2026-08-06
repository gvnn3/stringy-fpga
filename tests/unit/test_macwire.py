"""Host side of the P1 MAC program: codecs, masking model, transports.

Device-free coverage of :mod:`pyro.macwire`:

  * the new R78 kinds 0x0E-0x14 are registered where the existing
    kinds live and round-trip through the R86.2/R86.3 frame codec;
  * MAC_REPORT records are byte-exact little-endian ``<IHHQ``;
  * :func:`mask_packet`/:func:`digest_packet` implement the RFC 4302
    mutable-field model — the same six scenarios the RTL testbench
    runs (TTL/DSCP/checksum invariance, payload sensitivity, VLAN
    invariance, ARP skip) plus the flag bits;
  * MacReportListener consumes UNSOLICITED reports (kind-filtered,
    never seq-filtered) and the request/reply helpers speak the R84
    timeout/retry shape — both over the R86.6 ``transport_factory``
    seam.
"""
import struct

import pytest

import pyro.device as pdev
import pyro.macwire as mw
from pyro.siphash import siphash24

KEY = bytes(range(16))
ETH = b"\xff" * 6 + b"\x02\x00\x00\x00\x00\x01" + b"\x88\xb5"


# ---------------------------------------------------------------------------
# frame builders
# ---------------------------------------------------------------------------


def l2(ethertype, vlan_tci=None):
    """dst + src (+ optional 802.1Q tag) + EtherType."""
    hdr = b"\x02\xaa\xbb\xcc\xdd\xee" + b"\x02\x11\x22\x33\x44\x55"
    if vlan_tci is not None:
        hdr += struct.pack(">HH", 0x8100, vlan_tci)
    return hdr + struct.pack(">H", ethertype)


def ipv4(payload, proto=6, ttl=64, tos=0, ipck=0x1234, ident=7,
         frag=0x4000, opts=b""):
    assert len(opts) % 4 == 0
    ihl = 5 + len(opts) // 4
    total = ihl * 4 + len(payload)
    return (struct.pack(">BBHHHBBH", (4 << 4) | ihl, tos, total, ident,
                        frag, ttl, proto, ipck)
            + b"\x0a\x00\x00\x01" + b"\x0a\x00\x00\x02" + opts
            + payload)


def tcp(payload, ck=0x5678):
    return struct.pack(">HHIIBBHHH", 1234, 80, 1, 2, 5 << 4, 0x18,
                       4096, ck, 0) + payload


def udp(payload, ck=0x9abc):
    return struct.pack(">HHHH", 53, 53, 8 + len(payload), ck) + payload


def ipv6(payload, nh=6, tc=0, flow=0, hlim=64):
    return (struct.pack(">IHBB", (6 << 28) | (tc << 20) | flow,
                        len(payload), nh, hlim)
            + bytes(range(0x20, 0x30)) + bytes(range(0x30, 0x40))
            + payload)


def dig(frame, key=KEY):
    return mw.digest_packet(key, frame)


# ---------------------------------------------------------------------------
# kind registration + payload codec round-trips
# ---------------------------------------------------------------------------


def test_kind_values_match_contract():
    assert pdev.KIND_MAC_REPORT == 0x0E
    assert pdev.KIND_MAC_KEY_LOAD == 0x0F
    assert pdev.KIND_MAC_KEY_ACK == 0x10
    assert pdev.KIND_SCHED_SET == 0x11
    assert pdev.KIND_SCHED_ACK == 0x12
    assert pdev.KIND_MAC_STAT_REQUEST == 0x13
    assert pdev.KIND_MAC_STAT_REPLY == 0x14
    for k in range(0x0E, 0x15):
        assert k in pdev.VALID_KINDS


def test_mac_report_roundtrip_and_record_endianness():
    recs = [mw.MacRecord(wire_seq=0x11223344, pkt_len=0x0102,
                         flags=mw.REC_IPV4 | mw.REC_TCP,
                         digest=0x0807060504030201),
            mw.MacRecord(wire_seq=5, pkt_len=60, flags=mw.REC_IPV6,
                         digest=0xFFFFFFFFFFFFFFFF)]
    status = mw.REPORT_WIRE_ORIGIN | mw.REPORT_LOSS
    payload = mw.encode_mac_report(status, 9, recs)
    frame = pdev.encode_frame(pdev.KIND_MAC_REPORT, 1, 0x11223344,
                              payload)
    rep = mw.decode_mac_report(pdev.decode_frame(frame))
    assert rep == mw.MacReport(seq=0x11223344, status=status, key_id=9,
                               records=tuple(recs))
    assert rep.loss and rep.wire_origin
    # Byte-exact: head is count/status BE + key_id BE; records are
    # LITTLE-endian <IHHQ per the contract.
    assert payload[0:8] == struct.pack(">HHI", 2, status, 9)
    assert payload[8:24] == bytes(
        [0x44, 0x33, 0x22, 0x11, 0x02, 0x01,
         (mw.REC_IPV4 | mw.REC_TCP), 0x00,
         0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08])


def test_mac_report_rejects_bad_counts():
    with pytest.raises(pdev.PyroFrameError):
        mw.encode_mac_report(mw.REPORT_WIRE_ORIGIN, 0, [])
    too_many = [mw.MacRecord(0, 0, 0, 0)] * 65
    with pytest.raises(pdev.PyroFrameError):
        mw.encode_mac_report(mw.REPORT_WIRE_ORIGIN, 0, too_many)
    # decode: count claims more records than the payload carries
    head = struct.pack(">HHI", 3, mw.REPORT_WIRE_ORIGIN, 0)
    frame = pdev.decode_frame(pdev.encode_frame(
        pdev.KIND_MAC_REPORT, 1, 1, head + b"\x00" * 16))
    with pytest.raises(pdev.PyroFrameError):
        mw.decode_mac_report(frame)


def test_key_load_and_ack_roundtrip():
    payload = mw.encode_mac_key_load(0xDEADBEEF, KEY)
    assert payload == struct.pack(">I", 0xDEADBEEF) + KEY
    ackp = struct.pack(">IQ", 0xDEADBEEF, mw.key_check(KEY))
    frame = pdev.decode_frame(pdev.encode_frame(
        pdev.KIND_MAC_KEY_ACK, 1, 3, ackp))
    ack = mw.decode_mac_key_ack(frame)
    assert ack.key_id == 0xDEADBEEF
    assert ack.keycheck == mw.key_check(KEY)
    with pytest.raises(pdev.PyroFrameError):
        mw.encode_mac_key_load(1, b"\x00" * 15)


def test_sched_roundtrip_and_local_rejects():
    payload = mw.encode_sched_set(mw.SCHED_RR, 4)
    assert payload == b"\x02\x00\x00\x04"
    frame = pdev.decode_frame(pdev.encode_frame(
        pdev.KIND_SCHED_ACK, 1, 2,
        struct.pack(">BBHI", mw.SCHED_RR, 0, 4, 0)))
    ack = mw.decode_sched_ack(frame)
    assert ack == mw.SchedAck(mode=mw.SCHED_RR, quantum=4, status=0)
    with pytest.raises(pdev.PyroFrameError):
        mw.encode_sched_set(3, 1)          # bad mode
    with pytest.raises(pdev.PyroFrameError):
        mw.encode_sched_set(mw.SCHED_RR, 0)  # quantum >= 1


def test_mac_stats_zero_slack():
    good = struct.pack(">6I", 100, 60, 30, 10, 4, 0)
    st = mw.decode_mac_stat_reply(pdev.decode_frame(pdev.encode_frame(
        pdev.KIND_MAC_STAT_REPLY, 1, 1, good)))
    assert st == mw.MacStats(100, 60, 30, 10, 4, 0)
    assert st.zero_slack
    bad = st._replace(digested=59)
    assert not bad.zero_slack


def test_keycheck_definition():
    assert mw.MAC_KEYCHECK_STRING == b"PYROMACKEYCHECK1"
    assert mw.key_check(KEY) == siphash24(KEY, b"PYROMACKEYCHECK1")


# ---------------------------------------------------------------------------
# masking golden model — the six RTL-TB scenarios
# ---------------------------------------------------------------------------


def test_mask_ttl_invariance():
    a = l2(0x0800) + ipv4(tcp(b"hello pyro"), ttl=64)
    b = l2(0x0800) + ipv4(tcp(b"hello pyro"), ttl=1)
    da, fa, ra = dig(a)
    db, fb, rb = dig(b)
    assert ra == rb == "digested"
    assert da == db
    assert fa == fb == (mw.REC_IPV4 | mw.REC_TCP)


def test_mask_dscp_ecn_invariance():
    a = l2(0x0800) + ipv4(tcp(b"hello pyro"), tos=0x00)
    b = l2(0x0800) + ipv4(tcp(b"hello pyro"), tos=0xB8)
    assert dig(a)[0] == dig(b)[0]


def test_mask_checksum_invariance():
    """Both the IPv4 header checksum and the TCP checksum are zeroed;
    frames differing only there digest identically."""
    a = l2(0x0800) + ipv4(tcp(b"hello pyro", ck=0x0000), ipck=0x0000)
    b = l2(0x0800) + ipv4(tcp(b"hello pyro", ck=0xFFFF), ipck=0xABCD)
    assert dig(a)[0] == dig(b)[0]


def test_mask_payload_sensitivity():
    a = l2(0x0800) + ipv4(tcp(b"hello pyro"))
    b = l2(0x0800) + ipv4(tcp(b"hello pyrO"))
    da, _, _ = dig(a)
    db, _, _ = dig(b)
    assert da is not None and db is not None and da != db


def test_mask_vlan_invariance():
    """L2 is excluded entirely: the same IP packet with and without an
    802.1Q tag digests identically (offsets are IP-header-relative)."""
    bare = l2(0x0800) + ipv4(tcp(b"hello pyro"))
    tagged = l2(0x0800, vlan_tci=0x0064) + ipv4(tcp(b"hello pyro"))
    assert dig(bare)[0] == dig(tagged)[0]
    # masked input is identical bytes, not merely an equal digest
    assert mw.mask_packet(bare) == mw.mask_packet(tagged)


def test_mask_arp_skip():
    arp = l2(0x0806) + b"\x00\x01\x08\x00\x06\x04\x00\x01" + bytes(20)
    d, flags, reason = dig(arp)
    assert d is None and flags == 0 and reason == "skip_nonip"
    assert mw.mask_packet(arp) is None


# -- beyond the six: flags, v6, options, truncation, no-key ---------------


def test_mask_udp_flags_and_checksum():
    a = l2(0x0800) + ipv4(udp(b"dns?", ck=0x0000), proto=17)
    b = l2(0x0800) + ipv4(udp(b"dns?", ck=0x1234), proto=17)
    da, fa, _ = dig(a)
    db, _, _ = dig(b)
    assert da == db
    assert fa == (mw.REC_IPV4 | mw.REC_UDP)


def test_mask_unknown_l4_covered_whole():
    """Unknown L4 proto (GRE=47): flag bit4, nothing zeroed past the
    IP header — the 'checksum' bytes stay covered."""
    a = l2(0x0800) + ipv4(b"\xaa" * 8, proto=47)
    b = l2(0x0800) + ipv4(b"\xaa\xaa\xbb" + b"\xaa" * 5, proto=47)
    da, fa, _ = dig(a)
    db, _, _ = dig(b)
    assert fa == (mw.REC_IPV4 | mw.REC_OTHER_L4)
    assert da != db


def test_mask_ipv4_options_zeroed():
    a = l2(0x0800) + ipv4(tcp(b"pp"), opts=b"\x01\x01\x01\x00")
    b = l2(0x0800) + ipv4(tcp(b"pp"), opts=b"\x44\x04\x05\x00")
    da, fa, _ = dig(a)
    db, fb, _ = dig(b)
    assert da == db
    assert fa == fb == (mw.REC_IPV4 | mw.REC_TCP | mw.REC_OPTS_ZEROED)


def test_mask_ipv6_mutable_fields():
    a = l2(0x86DD) + ipv6(tcp(b"six"), tc=0, flow=0, hlim=64)
    b = l2(0x86DD) + ipv6(tcp(b"six"), tc=0x2E, flow=0xBEEF, hlim=1)
    da, fa, _ = dig(a)
    db, fb, _ = dig(b)
    assert da == db
    assert fa == fb == (mw.REC_IPV6 | mw.REC_TCP)


def test_mask_ipv6_unknown_next_header():
    """v1 does not walk extension headers: nh=0 (hop-by-hop) is
    treated as an unknown L4 and covered whole (flag bit4)."""
    fr = l2(0x86DD) + ipv6(b"\x06\x00" + bytes(6), nh=0)
    d, flags, reason = dig(fr)
    assert reason == "digested" and d is not None
    assert flags == (mw.REC_IPV6 | mw.REC_OTHER_L4)


def test_mask_truncated_flag():
    full = l2(0x0800) + ipv4(tcp(b"hello pyro world"))
    cut = full[:-6]
    _, flags, reason = dig(cut)
    assert reason == "digested"
    assert flags & mw.REC_TRUNCATED


def test_mask_icmp_checksum_masked_no_flag():
    """ICMP/ICMPv6: checksum bytes 2-3 zeroed, but NO L4 flag bit —
    MR11 reserves bit4 for protocols covered whole (the engine sets
    no bit for ICMP either)."""
    a = l2(0x0800) + ipv4(b"\x08\x00\xf0\x0d\x12\x34\x00\x01ping",
                          proto=1)
    b = l2(0x0800) + ipv4(b"\x08\x00\x00\x00\x12\x34\x00\x01ping",
                          proto=1)
    da, fa, ra = dig(a)
    db, fb, _ = dig(b)
    assert ra == "digested" and da == db          # checksum masked
    assert fa == fb == mw.REC_IPV4                # no OTHER_L4, no TCP
    a6 = l2(0x86DD) + ipv6(b"\x80\x00\xbe\xef\x00\x01\x00\x01x", nh=58)
    _, f6, _ = dig(a6)
    assert f6 == mw.REC_IPV6


def test_mask_l4_window_truncation_flag():
    """A frame that ends before the L4 checksum window completes is
    flagged truncated even when the total-length field agrees (the
    engine flags this from its S_L4 path)."""
    fr = l2(0x0800) + ipv4(tcp(b"")[:10])         # 10 of 20 TCP bytes
    _, flags, reason = dig(fr)
    assert reason == "digested"
    assert flags == (mw.REC_IPV4 | mw.REC_TCP | mw.REC_TRUNCATED)


def test_mask_runt_ipv4_no_other_l4():
    """A runt (frame shorter than a minimal IP header) is truncated
    but carries NO L4 classification bit — there is no protocol byte
    to trust, and the engine sets none."""
    fr = l2(0x0800) + bytes(10)
    d, flags, reason = dig(fr)
    assert reason == "digested" and d is not None
    assert flags == (mw.REC_IPV4 | mw.REC_TRUNCATED)


def test_mask_empty_l3_is_skip_nonip():
    """A frame ending exactly at the EtherType has zero L3 bytes:
    nothing to digest — the engine counts it skip_nonip, so must the
    model (not a digest of the empty message)."""
    d, flags, reason = dig(l2(0x0800))
    assert d is None and flags == 0 and reason == "skip_nonip"
    assert mw.mask_packet(l2(0x0800)) is None


def test_digest_skip_nokey():
    fr = l2(0x0800) + ipv4(tcp(b"x"))
    d, flags, reason = mw.digest_packet(None, fr)
    assert d is None and reason == "skip_nokey"
    assert flags == (mw.REC_IPV4 | mw.REC_TCP)


def test_digest_skip_nokey_wins_over_nonip():
    """The device decides keyless at frame byte 0: with no committed
    key EVERY wire frame counts skip_nokey, non-IP included (MR15) —
    skip_nonip exists only once a key is live."""
    arp = l2(0x0806) + b"\x00\x01\x08\x00\x06\x04\x00\x01" + bytes(20)
    d, flags, reason = mw.digest_packet(None, arp)
    assert d is None and flags == 0 and reason == "skip_nokey"
    assert dig(arp)[2] == "skip_nonip"            # keyed: non-IP skip


def test_mask_pad_is_covered():
    """The digest input runs to the end of the frame (per tuser size):
    trailing Ethernet pad participates, as in hardware."""
    fr = l2(0x0800) + ipv4(tcp(b"x"))
    padded = fr + b"\x00" * (60 - len(fr))
    assert dig(fr)[0] != dig(padded)[0]


# ---------------------------------------------------------------------------
# transports: listener + request/reply helpers over the R86.6 seam
# ---------------------------------------------------------------------------


class FakeTransport:
    """R86.7-shaped in-memory transport for the transport_factory
    seam: recv pops queued frames (None when drained), send records
    the frame and lets an optional responder queue replies."""

    def __init__(self, frames=None, responder=None):
        self.queue = list(frames or [])
        self.responder = responder
        self.sent = []
        self.closed = 0

    def send(self, frame):
        self.sent.append(bytes(frame))
        if self.responder is not None:
            self.queue.extend(self.responder(bytes(frame)))

    def recv(self, timeout):
        return self.queue.pop(0) if self.queue else None

    def close(self):
        self.closed += 1


def _config(tr):
    return pdev.DeviceConfig(iface=None, chardev=None,
                             transport_factory=lambda _cfg: tr)


def report_frame(seq, records, status=mw.REPORT_WIRE_ORIGIN, key_id=1):
    return ETH + pdev.encode_frame(
        pdev.KIND_MAC_REPORT, 1, seq,
        mw.encode_mac_report(status, key_id, records))


def test_listener_yields_unsolicited_reports_kind_filtered():
    r1 = [mw.MacRecord(100, 60, mw.REC_IPV4 | mw.REC_TCP, 0x1111),
          mw.MacRecord(101, 64, mw.REC_IPV4 | mw.REC_UDP, 0x2222)]
    r2 = [mw.MacRecord(102, 60, mw.REC_IPV6, 0x3333)]
    noise = [
        b"\x00" * 10,                                   # runt
        l2(0x0806) + b"arp-noise",                      # wrong ethertype
        ETH + b"\x51\x01garbage",                       # bad magic
        ETH + pdev.encode_frame(pdev.KIND_PERF_REPLY, 1, 100,
                                struct.pack(">QQ", 0, 0)),
    ]
    tr = FakeTransport(frames=noise + [report_frame(100, r1),
                                       report_frame(102, r2)])
    with mw.MacReportListener(_config(tr)) as lis:
        a = lis.poll(1.0)
        b = lis.poll(1.0)
        c = lis.poll(1.0)
    # seq is the autonomous wire_seq of the first record — reports are
    # accepted regardless of it (kind-only filtering).
    assert a is not None and a.seq == 100 and a.records == tuple(r1)
    assert b is not None and b.seq == 102 and b.records == tuple(r2)
    assert a.wire_origin and not a.loss
    assert c is None
    assert tr.closed == 1


def test_load_key_verifies_against_keycheck():
    def responder(frame):
        dec = pdev.decode_frame(frame[14:])
        assert dec.kind == pdev.KIND_MAC_KEY_LOAD
        key_id = struct.unpack(">I", dec.payload[0:4])[0]
        key = dec.payload[4:20]
        # a colliding-seq unsolicited report must not be mistaken for
        # the ack (kind is filtered before seq)
        rep = report_frame(dec.seq, [mw.MacRecord(dec.seq, 60, 1, 5)])
        ack = ETH + pdev.encode_frame(
            pdev.KIND_MAC_KEY_ACK, dec.slot, dec.seq,
            struct.pack(">IQ", key_id, mw.key_check(key)))
        return [rep, ack]

    tr = FakeTransport(responder=responder)
    ack = mw.load_key(_config(tr), 7, KEY)
    assert ack == mw.MacKeyAck(key_id=7, keycheck=mw.key_check(KEY))
    assert len(tr.sent) == 1


def test_set_sched_ack_roundtrip():
    def responder(frame):
        dec = pdev.decode_frame(frame[14:])
        mode, _resv, quantum = struct.unpack(">BBH", dec.payload)
        return [ETH + pdev.encode_frame(
            pdev.KIND_SCHED_ACK, dec.slot, dec.seq,
            struct.pack(">BBHI", mode, 0, quantum, 0))]

    ack = mw.set_sched(_config(FakeTransport(responder=responder)),
                       mw.SCHED_RR, 4)
    assert ack == mw.SchedAck(mode=mw.SCHED_RR, quantum=4, status=0)


def test_read_mac_stats_and_no_reply_paths():
    def responder(frame):
        dec = pdev.decode_frame(frame[14:])
        assert dec.kind == pdev.KIND_MAC_STAT_REQUEST
        assert dec.payload == b""
        return [ETH + pdev.encode_frame(
            pdev.KIND_MAC_STAT_REPLY, dec.slot, dec.seq,
            struct.pack(">6I", 10, 5, 3, 2, 1, 0))]

    st = mw.read_mac_stats(_config(FakeTransport(responder=responder)))
    assert st == mw.MacStats(10, 5, 3, 2, 1, 0) and st.zero_slack
    # a pre-amendment child drops the unknown kind: no reply -> None
    silent = pdev.DeviceConfig(
        iface=None, chardev=None, probe_attempts=1, probe_timeout_s=0.01,
        transport_factory=lambda _cfg: FakeTransport())
    assert mw.read_mac_stats(silent) is None
    # fail-closed: no transport configured at all -> None, no raise
    bare = pdev.DeviceConfig(iface=None, chardev=None)
    assert mw.read_mac_stats(bare) is None


def test_status_refusal_yields_none():
    def responder(frame):
        dec = pdev.decode_frame(frame[14:])
        return [ETH + pdev.encode_frame(
            pdev.KIND_STATUS, dec.slot, dec.seq,
            struct.pack(">I", 2))]

    cfg = pdev.DeviceConfig(
        iface=None, chardev=None, probe_attempts=1,
        transport_factory=lambda _c: FakeTransport(responder=responder))
    assert mw.set_sched(cfg, mw.SCHED_P1_ONLY, 1) is None
