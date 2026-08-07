"""Host side of the P1 MAC digest program — ``pyro.macwire``.

Payload codecs for the multi-program MAC amendment's R78 kinds
(``0x0E``-``0x14``), the packet-masking golden model that predicts
every hardware digest, and the request/reply + unsolicited-report
helpers that speak them over the R86.7 transports.

The frame *header* codec is unchanged — :func:`pyro.device.encode_frame`
/ :func:`pyro.device.decode_frame` carry these kinds like any other
(the kind constants live in :mod:`pyro.device` beside the existing
ones).  This module owns only the kind-specific payloads:

  * ``MAC_REPORT``   0x0E device->host, UNSOLICITED; ``seq`` is the
    autonomous ``wire_seq`` of the first record, so nothing here
    matches it against a request.
  * ``MAC_KEY_LOAD`` 0x0F / ``MAC_KEY_ACK`` 0x10 — key commit with the
    fail-closed keycheck: SipHash-2-4 under the just-committed key of
    the exact 16-byte ASCII string ``"PYROMACKEYCHECK1"``.
  * ``SCHED_SET``    0x11 / ``SCHED_ACK`` 0x12 — P0/P1 dispatch mode.
  * ``MAC_STAT_REQUEST`` 0x13 / ``MAC_STAT_REPLY`` 0x14 — the six
    counters with the zero-slack invariant
    ``seen == digested + skip_nonip + skip_nokey`` EXACTLY.

**Masking model.** :func:`mask_packet` / :func:`digest_packet`
implement the transit-invariant coverage (RFC 4302 mutable-field
model) byte-for-byte as the RTL does: L2 (MACs, stacked VLAN tags,
final EtherType) is parsed only to find L3 and is excluded; the digest
input is the frame from the start of the IP header through the end of
the frame *as received* (trailing Ethernet pad included — the hardware
digests per tuser size), with mutable fields zeroed IN PLACE so
offsets stay stable.  Software that feeds :func:`digest_packet` the
same frame bytes the card saw predicts the hardware digest exactly.
"""

from __future__ import annotations

import struct
import time
from typing import Iterable, Iterator, NamedTuple, Optional, Tuple

from .device import (
    ETHERTYPE,
    PYRO_HEADER_LEN,
    KIND_MAC_REPORT,
    KIND_MAC_KEY_LOAD,
    KIND_MAC_KEY_ACK,
    KIND_SCHED_SET,
    KIND_SCHED_ACK,
    KIND_MAC_STAT_REQUEST,
    KIND_MAC_STAT_REPLY,
    KIND_STATUS,
    DecodedFrame,
    DeviceConfig,
    PyroFrameError,
    decode_frame,
    encode_frame,
    _make_transport,
)
from .siphash import siphash24

__all__ = [
    # constants
    "MAC_KEYCHECK_STRING",
    "MAX_RECORDS_PER_REPORT",
    "REC_IPV4", "REC_IPV6", "REC_TCP", "REC_UDP", "REC_OTHER_L4",
    "REC_OPTS_ZEROED", "REC_TRUNCATED",
    "REPORT_LOSS", "REPORT_WIRE_ORIGIN",
    "SCHED_P0_ONLY", "SCHED_P1_ONLY", "SCHED_RR", "SCHED_BROADCAST",
    # typed views
    "MacRecord", "MacReport", "MacKeyAck", "SchedAck", "SchedState",
    "MacStats",
    # payload codecs
    "encode_mac_report", "decode_mac_report",
    "encode_mac_key_load", "decode_mac_key_ack",
    "encode_sched_set", "decode_sched_ack",
    "decode_mac_stat_reply",
    # masking / digest golden model
    "mask_packet", "digest_packet", "key_check",
    # transports
    "MacReportListener", "load_key", "set_sched", "read_sched",
    "read_mac_stats",
]

# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------

#: The exact 16-byte ASCII keycheck string of the MAC_KEY_ACK contract.
MAC_KEYCHECK_STRING = b"PYROMACKEYCHECK1"
assert len(MAC_KEYCHECK_STRING) == 16

#: MAC_REPORT batching bound: flush at 64 pending records.
MAX_RECORDS_PER_REPORT = 64

#: MAC_REPORT record layout — 16 bytes LITTLE-endian ``<IHHQ``:
#: {wire_seq u32, pkt_len u16, flags u16, digest u64}.
_RECORD_FMT = "<IHHQ"
_RECORD_LEN = struct.calcsize(_RECORD_FMT)
assert _RECORD_LEN == 16
#: MAC_REPORT fixed head — count u16 BE, status u16 BE, key_id u32 BE.
_REPORT_HEAD_FMT = ">HHI"
_REPORT_HEAD_LEN = struct.calcsize(_REPORT_HEAD_FMT)

# record.flags bits
REC_IPV4 = 1 << 0
REC_IPV6 = 1 << 1
REC_TCP = 1 << 2
REC_UDP = 1 << 3
REC_OTHER_L4 = 1 << 4
REC_OPTS_ZEROED = 1 << 5   # IPv4 options bytes zeroed (IHL > 5)
REC_TRUNCATED = 1 << 6     # short of the IP length claim, or ended
                           # before the IP/L4 header regions completed

# MAC_REPORT status bits
REPORT_LOSS = 1 << 0          # record loss occurred since last report
REPORT_WIRE_ORIGIN = 1 << 2   # wire-origin marker, ALWAYS set

# SCHED_SET modes
SCHED_P0_ONLY = 0
SCHED_P1_ONLY = 1
SCHED_RR = 2
SCHED_BROADCAST = 3     # every wire frame to BOTH programs

_VLAN_TPIDS = (0x8100, 0x88A8)
_ETH_IPV4 = 0x0800
_ETH_IPV6 = 0x86DD


# ---------------------------------------------------------------------------
# Typed payload views
# ---------------------------------------------------------------------------


class MacRecord(NamedTuple):
    """One 16-byte MAC_REPORT record (``<IHHQ``, little-endian)."""
    wire_seq: int
    pkt_len: int
    flags: int
    digest: int


class MacReport(NamedTuple):
    """A decoded ``MAC_REPORT`` (0x0E) frame.

    ``seq`` is the frame header's seq = the autonomous ``wire_seq`` of
    the first record — NOT an echo of any request.
    """
    seq: int
    status: int
    key_id: int
    records: Tuple[MacRecord, ...]

    @property
    def loss(self) -> bool:
        """Status bit0: record loss occurred since the last report."""
        return bool(self.status & REPORT_LOSS)

    @property
    def wire_origin(self) -> bool:
        """Status bit2: wire-origin marker (ALWAYS set by the device)."""
        return bool(self.status & REPORT_WIRE_ORIGIN)


class MacKeyAck(NamedTuple):
    """``MAC_KEY_ACK`` (0x10): key_id u32 BE + keycheck u64 BE."""
    key_id: int
    keycheck: int


class SchedAck(NamedTuple):
    """``SCHED_ACK`` (0x12): echoed settings + status (0 = ok)."""
    mode: int
    quantum: int
    status: int


class SchedState(NamedTuple):
    """The LIVE scheduler settings as read by :func:`read_sched`
    (mode/quantum echoed by a refused ``SCHED_SET`` probe)."""
    mode: int
    quantum: int


class MacStats(NamedTuple):
    """``MAC_STAT_REPLY`` (0x14): six u32 BE counters."""
    seen: int
    digested: int
    skip_nonip: int
    skip_nokey: int
    reports_sent: int
    records_lost: int

    @property
    def zero_slack(self) -> bool:
        """The contract invariant, EXACTLY:
        ``seen == digested + skip_nonip + skip_nokey``."""
        return self.seen == self.digested + self.skip_nonip + self.skip_nokey


# ---------------------------------------------------------------------------
# Payload codecs
# ---------------------------------------------------------------------------


def encode_mac_report(status: int, key_id: int,
                      records: Iterable[MacRecord]) -> bytes:
    """Build a ``MAC_REPORT`` payload (device->host direction; used by
    software models and tests to mirror the wrapper's composer)."""
    recs = [MacRecord(*r) for r in records]
    if not 1 <= len(recs) <= MAX_RECORDS_PER_REPORT:
        raise PyroFrameError(
            "MAC_REPORT carries 1..%d records, got %d"
            % (MAX_RECORDS_PER_REPORT, len(recs)))
    out = [struct.pack(_REPORT_HEAD_FMT, len(recs), status, key_id)]
    for r in recs:
        out.append(struct.pack(_RECORD_FMT, r.wire_seq, r.pkt_len,
                               r.flags, r.digest))
    return b"".join(out)


def decode_mac_report(frame: DecodedFrame) -> MacReport:
    """Parse a decoded ``MAC_REPORT`` frame into a :class:`MacReport`.

    Raises :class:`PyroFrameError` on the wrong kind, a count of 0 or
    > 64, or a payload shorter than the count claims.
    """
    if frame.kind != KIND_MAC_REPORT:
        raise PyroFrameError(
            "not a MAC_REPORT: kind 0x%02x" % frame.kind)
    p = frame.payload
    if len(p) < _REPORT_HEAD_LEN:
        raise PyroFrameError(
            "malformed MAC_REPORT: payload length %d < %d"
            % (len(p), _REPORT_HEAD_LEN))
    count, status, key_id = struct.unpack_from(_REPORT_HEAD_FMT, p, 0)
    if not 1 <= count <= MAX_RECORDS_PER_REPORT:
        raise PyroFrameError(
            "malformed MAC_REPORT: count %d outside 1..%d"
            % (count, MAX_RECORDS_PER_REPORT))
    need = _REPORT_HEAD_LEN + count * _RECORD_LEN
    if len(p) < need:
        raise PyroFrameError(
            "malformed MAC_REPORT: %d records need %d payload bytes, "
            "got %d" % (count, need, len(p)))
    records = tuple(
        MacRecord(*struct.unpack_from(
            _RECORD_FMT, p, _REPORT_HEAD_LEN + i * _RECORD_LEN))
        for i in range(count))
    return MacReport(seq=frame.seq, status=status, key_id=key_id,
                     records=records)


def encode_mac_key_load(key_id: int, key: bytes) -> bytes:
    """``MAC_KEY_LOAD`` (0x0F) payload: key_id u32 BE + 16-byte key
    (bytes 0-7 = SipHash k0 LE, 8-15 = k1 LE — reference convention)."""
    key = bytes(key)
    if len(key) != 16:
        raise PyroFrameError(
            "MAC key must be exactly 16 bytes, got %d" % len(key))
    if not 0 <= int(key_id) <= 0xFFFFFFFF:
        raise PyroFrameError("key_id %r out of 32-bit range" % (key_id,))
    return struct.pack(">I", key_id) + key


def decode_mac_key_ack(frame: DecodedFrame) -> MacKeyAck:
    """Parse a decoded ``MAC_KEY_ACK`` frame."""
    if frame.kind != KIND_MAC_KEY_ACK:
        raise PyroFrameError(
            "not a MAC_KEY_ACK: kind 0x%02x" % frame.kind)
    if len(frame.payload) < 12:
        raise PyroFrameError(
            "malformed MAC_KEY_ACK: payload length %d < 12"
            % len(frame.payload))
    key_id, keycheck = struct.unpack_from(">IQ", frame.payload, 0)
    return MacKeyAck(key_id=key_id, keycheck=keycheck)


def encode_sched_set(mode: int, quantum: int) -> bytes:
    """``SCHED_SET`` (0x11) payload: mode u8, resv u8, quantum u16 BE.

    Rejects locally what the device would refuse (status 1): a mode
    outside 0..3 or a quantum outside 1..65535.  Mode 3 (broadcast)
    delivers every wire frame to BOTH programs; the INVALID 0xFF the
    refusal-probe read depends on stays rejected here by design.
    """
    if mode not in (SCHED_P0_ONLY, SCHED_P1_ONLY, SCHED_RR,
                    SCHED_BROADCAST):
        raise PyroFrameError(
            "bad scheduler mode %r (0|1|2|3)" % (mode,))
    if not 1 <= int(quantum) <= 0xFFFF:
        raise PyroFrameError(
            "bad scheduler quantum %r (1..65535)" % (quantum,))
    return struct.pack(">BBH", mode, 0, quantum)


def decode_sched_ack(frame: DecodedFrame) -> SchedAck:
    """Parse a decoded ``SCHED_ACK`` frame."""
    if frame.kind != KIND_SCHED_ACK:
        raise PyroFrameError(
            "not a SCHED_ACK: kind 0x%02x" % frame.kind)
    if len(frame.payload) < 8:
        raise PyroFrameError(
            "malformed SCHED_ACK: payload length %d < 8"
            % len(frame.payload))
    mode, _resv, quantum, status = struct.unpack_from(
        ">BBHI", frame.payload, 0)
    return SchedAck(mode=mode, quantum=quantum, status=status)


def decode_mac_stat_reply(frame: DecodedFrame) -> MacStats:
    """Parse a decoded ``MAC_STAT_REPLY`` frame."""
    if frame.kind != KIND_MAC_STAT_REPLY:
        raise PyroFrameError(
            "not a MAC_STAT_REPLY: kind 0x%02x" % frame.kind)
    if len(frame.payload) < 24:
        raise PyroFrameError(
            "malformed MAC_STAT_REPLY: payload length %d < 24"
            % len(frame.payload))
    return MacStats(*struct.unpack_from(">6I", frame.payload, 0))


# ---------------------------------------------------------------------------
# Masking golden model (RFC 4302 mutable-field coverage)
# ---------------------------------------------------------------------------


def _l3_offset(frame: bytes) -> Tuple[Optional[int], Optional[int]]:
    """Skip L2 (MACs + stacked VLAN tags) -> (l3_offset, ethertype).

    Returns ``(None, None)`` when no complete EtherType is present.
    L2 is parsed only to find L3; none of it is digested.
    """
    off = 12
    while True:
        if off + 2 > len(frame):
            return None, None
        et = struct.unpack_from(">H", frame, off)[0]
        if et in _VLAN_TPIDS:
            off += 4       # TPID + TCI
            continue
        return off + 2, et


def _mask(frame: bytes):
    """Core masking: -> (masked bytearray | None, record flags, reason).

    ``masked`` is the digest input: frame bytes from the start of the
    IP header through the end of the frame, mutable fields zeroed in
    place (masking, not elision).  ``None`` => not digested; reason is
    ``"skip_nonip"``.
    """
    frame = bytes(frame)
    l3, et = _l3_offset(frame)
    if l3 is None or et not in (_ETH_IPV4, _ETH_IPV6):
        return None, 0, "skip_nonip"
    m = bytearray(frame[l3:])
    if not m:
        # EtherType known, zero L3 bytes: nothing to digest — the
        # engine counts this skip_nonip, so must we.
        return None, 0, "skip_nonip"
    flags = 0
    if et == _ETH_IPV4:
        flags |= REC_IPV4
        if len(m) < 20:
            # Runt: shorter than a minimal IPv4 header.  Mask whatever
            # mutable bytes exist, cover the rest, flag truncated.  No
            # L4 flag: with the header incomplete there is no protocol
            # byte to trust (the engine sets none either).
            for i in (1, 6, 7, 8, 10, 11):
                if i < len(m):
                    m[i] = 0
            return m, flags | REC_TRUNCATED, "digested"
        ihl = (m[0] & 0x0F) * 4
        total = struct.unpack_from(">H", m, 2)[0]
        # Mutable per RFC 4302: DSCP/ECN, flags+frag offset, TTL,
        # header checksum.
        m[1] = 0
        m[6] = 0
        m[7] = 0
        m[8] = 0
        m[10] = 0
        m[11] = 0
        if ihl > 20 and len(m) > 20:
            # bit5 only when an options byte actually arrived — an
            # IHL > 5 claim cut at exactly 20 bytes zeroes nothing.
            for i in range(20, min(ihl, len(m))):
                m[i] = 0
            flags |= REC_OPTS_ZEROED
        if total > len(m):
            flags |= REC_TRUNCATED
        proto = m[9]
        l4 = max(ihl, 20)    # a bogus IHL < 5 is treated as 20
        if len(m) < l4:
            # Ends inside the options: header incomplete, so no L4
            # classification (matches the engine byte-for-byte).
            return m, flags | REC_TRUNCATED, "digested"
    else:
        flags |= REC_IPV6
        if len(m) < 40:
            # Runt IPv6: same policy as the v4 runt above.
            if len(m) >= 1:
                m[0] &= 0xF0
            for i in (1, 2, 3, 7):
                if i < len(m):
                    m[i] = 0
            return m, flags | REC_TRUNCATED, "digested"
        proto = m[6]         # v1 does NOT walk extension headers
        # Keep the version nibble, zero TC + flow label + hop limit.
        m[0] &= 0xF0
        m[1] = 0
        m[2] = 0
        m[3] = 0
        m[7] = 0
        plen = struct.unpack_from(">H", m, 4)[0]
        if 40 + plen > len(m):
            flags |= REC_TRUNCATED
        l4 = 40
    # L4 checksum masking: TCP 16-17, UDP 6-7, ICMP/ICMPv6 2-3
    # (offsets relative to the L4 header start).  ICMP/ICMPv6 get no
    # flag bit (MR11 reserves bit4 for protocols that are covered
    # whole); anything else is covered whole and flagged bit4.
    if proto == 6:
        flags |= REC_TCP
        ck = 16
    elif proto == 17:
        flags |= REC_UDP
        ck = 6
    elif (et == _ETH_IPV4 and proto == 1) or \
            (et == _ETH_IPV6 and proto == 58):
        ck = 2
    else:
        flags |= REC_OTHER_L4
        ck = None
    if ck is not None:
        if len(m) < l4 + ck + 2:
            # Ends before the L4 checksum window completes: the L4
            # header region is truncated (the engine flags this too).
            flags |= REC_TRUNCATED
        for i in (l4 + ck, l4 + ck + 1):
            if i < len(m):
                m[i] = 0
    return m, flags, "digested"


def mask_packet(frame: bytes) -> Optional[bytes]:
    """The masked digest input for ``frame``, or ``None`` if the frame
    is not digested (non-IP EtherType -> ``skip_nonip``).

    ``frame`` is the full Ethernet frame as received on the wire,
    trailing pad included — the hardware digests per tuser size, so
    the pad is covered and a host prediction must include it too.
    """
    masked, _flags, _reason = _mask(frame)
    return None if masked is None else bytes(masked)


def digest_packet(key: Optional[bytes],
                  frame: bytes) -> Tuple[Optional[int], int, str]:
    """Predict the hardware digest -> ``(digest | None, flags, reason)``.

    ``reason`` is the counter the frame lands in: ``"digested"``,
    ``"skip_nonip"``, or ``"skip_nokey"`` (``key is None`` — the
    fail-closed no-committed-key state).  The device decides keyless
    at frame byte 0, so with no committed key EVERY wire frame counts
    ``skip_nokey`` — non-IP included (MR15); ``skip_nonip`` exists
    only once a key is live.  ``flags`` is the record flags word
    (bit0 ipv4 ... bit6 truncated) even when skipped for a key
    reason, and 0 for non-IP.
    """
    masked, flags, reason = _mask(frame)
    if key is None:
        return None, flags, "skip_nokey"
    if masked is None:
        return None, flags, reason
    return siphash24(key, bytes(masked)), flags, "digested"


def key_check(key: bytes) -> int:
    """The ``MAC_KEY_ACK`` keycheck: SipHash-2-4 under ``key`` of the
    exact 16-byte ASCII string ``"PYROMACKEYCHECK1"``."""
    return siphash24(key, MAC_KEYCHECK_STRING)


# ---------------------------------------------------------------------------
# Transport helpers
# ---------------------------------------------------------------------------


def _eth_hdr(config: DeviceConfig) -> bytes:
    return (bytes(config.dst_mac) + bytes(config.src_mac)
            + struct.pack(">H", ETHERTYPE))


def _decode_pyro(reply: bytes,
                 max_payload: int) -> Optional[DecodedFrame]:
    """Decode a received Ethernet frame's R78 portion, or ``None`` for
    anything that is not a well-formed PYRO frame (ignored, N1)."""
    reply = bytes(reply)
    if (len(reply) < 14 + PYRO_HEADER_LEN
            or struct.unpack(">H", reply[12:14])[0] != ETHERTYPE):
        return None
    try:
        return decode_frame(reply[14:], max_payload=max_payload)
    except PyroFrameError:
        return None


class MacReportListener:
    """Consumer of UNSOLICITED ``MAC_REPORT`` (0x0E) frames.

    Unlike every request/reply helper, this filters on **kind only**:
    a report's ``seq`` is the autonomous ``wire_seq`` of its first
    record, and wire-origin frames may be addressed to another MAC
    entirely (see scripts/pyro_wire_e2e.py), so neither seq nor dst
    MAC is matched.  Works over either R86.7 transport via
    ``pyro.device._make_transport`` (an injected ``transport_factory``
    seam included).

    Usage::

        with MacReportListener(config) as lis:
            for report in lis.reports(duration=5.0):
                ...             # a MacReport per 0x0E frame
    """

    def __init__(self, config: DeviceConfig):
        self._config = config
        self._transport = None

    def open(self) -> "MacReportListener":
        if self._transport is None:
            self._transport = _make_transport(self._config)
        return self

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def __enter__(self) -> "MacReportListener":
        return self.open()

    def __exit__(self, *_exc) -> None:
        self.close()

    def poll(self, timeout: float) -> Optional[MacReport]:
        """The next ``MAC_REPORT`` within ``timeout`` seconds, else
        ``None``.  Non-PYRO traffic, garbled frames, and other PYRO
        kinds (request replies sharing the socket) are skipped without
        ending the wait; a malformed 0x0E body raises
        :class:`PyroFrameError` (it *is* addressed to us, R86.4)."""
        if self._transport is None:
            self.open()
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return None
            reply = self._transport.recv(remaining)
            if reply is None:
                return None
            dec = _decode_pyro(reply, self._config.max_payload)
            if dec is None or dec.kind != KIND_MAC_REPORT:
                continue
            return decode_mac_report(dec)

    def reports(self, duration: Optional[float] = None,
                poll_s: float = 0.5) -> Iterator[MacReport]:
        """Yield reports for ``duration`` seconds (``None`` = forever)."""
        end = None if duration is None else time.monotonic() + duration
        while end is None or time.monotonic() < end:
            budget = poll_s if end is None \
                else min(poll_s, max(0.0, end - time.monotonic()))
            rep = self.poll(budget)
            if rep is not None:
                yield rep

    def records(self, duration: Optional[float] = None
                ) -> Iterator[MacRecord]:
        """Flattened per-packet records from :meth:`reports`."""
        for rep in self.reports(duration):
            for rec in rep.records:
                yield rec


def _mac_request(config: DeviceConfig, kind: int, payload: bytes,
                 reply_kind: int, slot: int) -> Optional[DecodedFrame]:
    """One MAC request/reply exchange, R84-style (same timeout/retry
    shape as ``probe_device``/``read_perf_counters``): up to
    ``probe_attempts`` attempts with a fresh ``seq``, each with a
    ``probe_timeout_s`` receive window.

    Returns the decoded reply, or ``None`` for no-reply (the expected
    outcome against a child that predates the amendment and drops the
    unknown kind, R78.4) and for a ``STATUS``/``ERROR`` refusal.
    Unsolicited ``MAC_REPORT`` frames sharing the socket are skipped
    by kind BEFORE the seq match — their autonomous seq could collide
    with ours.  Fail-closed with no configured transport; ``OSError``
    is contained to ``None`` (R86.1).
    """
    if (config.transport_factory is None and config.iface is None
            and config.chardev is None):
        return None     # R68 fail-closed: no transport configured
    try:
        transport = _make_transport(config)
    except OSError:
        return None
    try:
        for attempt in range(max(1, int(config.probe_attempts))):
            seq = attempt + 1
            frame = _eth_hdr(config) + encode_frame(
                kind, slot, seq, payload, max_payload=config.max_payload)
            try:
                transport.send(frame)
            except OSError:
                return None
            deadline = time.monotonic() + float(config.probe_timeout_s)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break   # this attempt timed out; try the next seq
                try:
                    reply = transport.recv(remaining)
                except OSError:
                    return None
                if reply is None:
                    break
                dec = _decode_pyro(reply, config.max_payload)
                if dec is None or dec.kind == KIND_MAC_REPORT:
                    continue    # not ours / unsolicited report
                if dec.seq != seq:
                    continue
                if dec.kind == KIND_STATUS:
                    return None     # device refused the kind
                if dec.kind != reply_kind:
                    continue
                return dec
        return None
    finally:
        transport.close()


def load_key(config: DeviceConfig, key_id: int, key: bytes,
             slot: int = 1) -> Optional[MacKeyAck]:
    """Commit a MAC key via ``MAC_KEY_LOAD`` -> the ``MAC_KEY_ACK``.

    ``None`` = no ack (no MAC program resident, R78.4 drop).  The
    caller verifies the returned ``keycheck`` against
    :func:`key_check` — a mismatch means the device is NOT digesting
    under the key the host thinks it loaded.
    """
    dec = _mac_request(config, KIND_MAC_KEY_LOAD,
                       encode_mac_key_load(key_id, key),
                       KIND_MAC_KEY_ACK, slot)
    return None if dec is None else decode_mac_key_ack(dec)


def set_sched(config: DeviceConfig, mode: int, quantum: int = 1,
              slot: int = 1) -> Optional[SchedAck]:
    """Set the P0/P1 wire dispatch schedule via ``SCHED_SET``.

    ``None`` = no ack.  A returned ``status != 0`` means the device
    refused the settings and left them unchanged.
    """
    dec = _mac_request(config, KIND_SCHED_SET,
                       encode_sched_set(mode, quantum),
                       KIND_SCHED_ACK, slot)
    return None if dec is None else decode_sched_ack(dec)


#: The refusal-probe payload :func:`read_sched` sends: an INVALID mode
#: (0xFF, quantum 0 — invalid too, belt and braces).  Built directly
#: because :func:`encode_sched_set` locally rejects what the device
#: refuses, by design.
_SCHED_PROBE = struct.pack(">BBH", 0xFF, 0, 0)


def read_sched(config: DeviceConfig,
               slot: int = 1) -> Optional[SchedState]:
    """Read the LIVE scheduler mode+quantum via the REFUSAL PROBE.

    There is no SCHED_GET kind; the read idiom is a ``SCHED_SET``
    whose mode is INVALID (0xFF).  The generated wrapper refuses it —
    ``sc_ok`` is false, so no register is written, and the rotation
    state (cur_prog/turn_cnt) is untouched because the RTL
    change-detect only fires on a value CHANGE and nothing changed —
    while the ``SCHED_ACK`` still echoes the live post-op
    mode+quantum with a NONZERO status.  Proven on silicon 2026-08-06
    (AC-M3 step 5).

    NEVER "write back what you think is current" as a read: a VALID
    same-value ``SCHED_SET`` happens to be rotation-safe too, but a
    valid DIFFERENT value resets the rotation — only an invalid mode
    is safe against every possible live state.  That also makes the
    R84 retry loop below harmless: a refused probe changes nothing no
    matter how often it is resent.

    ``None`` = no ack (child predates the amendment and drops the
    kind, R78.4; or no transport configured — fail-closed), the same
    shape as :func:`read_mac_stats`.  An ack with ``status == 0``
    means the device APPLIED the invalid mode: that is a device-side
    contract violation, and this raises :class:`RuntimeError` rather
    than ever returning it as truth.
    """
    dec = _mac_request(config, KIND_SCHED_SET, _SCHED_PROBE,
                       KIND_SCHED_ACK, slot)
    if dec is None:
        return None
    ack = decode_sched_ack(dec)
    if ack.status == 0:
        raise RuntimeError(
            "SCHED_ACK status 0 for the invalid-mode refusal probe: "
            "the device APPLIED mode 0x%02x quantum %d — refusing to "
            "report that as scheduler state" % (ack.mode, ack.quantum))
    return SchedState(mode=ack.mode, quantum=ack.quantum)


def read_mac_stats(config: DeviceConfig,
                   slot: int = 1) -> Optional[MacStats]:
    """Read the six MAC counters via ``MAC_STAT_REQUEST``.

    ``None`` = no reply (expected against a pre-amendment child) —
    never a device fault by itself.  Callers check
    :attr:`MacStats.zero_slack`, which the device guarantees EXACTLY.
    """
    dec = _mac_request(config, KIND_MAC_STAT_REQUEST, b"",
                       KIND_MAC_STAT_REPLY, slot)
    return None if dec is None else decode_mac_stat_reply(dec)
