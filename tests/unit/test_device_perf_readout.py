"""R78.11 (v2.4.0): host-side PERF counter read-out — pyro.device.read_perf_counters.

LIVE (no hardware): drives read_perf_counters through the R86.6 transport_factory
seam with scripted replies, asserting the R78.11 dispositions:

  * a PERF_REPLY yields ``(cycles, bytes)`` decoded as two 64-bit big-endian
    values (payload offsets 0 and 8);
  * no reply within the R84 budget yields ``None`` — the normative
    "counters unavailable" disposition against a pre-2.4.0 child (which drops
    the unknown kind per R78.4), never an exception;
  * a STATUS/ERROR reply (non-resident slot) yields ``None``;
  * a malformed PERF_REPLY to our seq (payload < 16) raises PyroFrameError;
  * stray frames (wrong ethertype / wrong seq / garbled) are ignored, not fatal;
  * the request on the wire is byte-exact R78.10(f) after the Ethernet header.
"""
import struct

import pyro.device as pdev


ETH = b"\x02\x00\x00\x00\x00\x01" + b"\x02\x00\x00\x00\x00\x02" + b"\x88\xb5"


def _hx(s):
    return bytes.fromhex(s.replace(" ", ""))


# R78.10(g): PERF_REPLY, slot=1, seq echoed, cycles=6, bytes=6.
def _perf_reply(seq, cycles=6, nbytes=6):
    payload = struct.pack(">QQ", cycles, nbytes)
    return ETH + pdev.encode_frame(pdev.KIND_PERF_REPLY, 1, seq, payload)


def _status_reply(seq):
    return ETH + pdev.encode_frame(pdev.KIND_STATUS, 1, seq, struct.pack(">I", 7))


class _FakeTransport:
    """send/recv/close per the R86.7 seam contract.  ``responder(seq, recv_i)``
    builds the reply for the recv-call at index ``recv_i`` since the last send."""

    def __init__(self, responder):
        self._responder = responder
        self.sends = []
        self._recv_i = 0
        self.closes = 0

    def send(self, frame):
        self.sends.append(bytes(frame))
        self._recv_i = 0

    def recv(self, timeout):
        seq = pdev.decode_frame(self.sends[-1][14:]).seq
        out = self._responder(seq, self._recv_i)
        self._recv_i += 1
        return out

    def close(self):  # R86.7: idempotent
        self.closes += 1


def _read(responder, *, slot=1, attempts=3, timeout=0.02):
    box = {}

    def factory(cfg):
        box["t"] = _FakeTransport(responder)
        return box["t"]

    cfg = pdev.DeviceConfig(iface="fake0", probe_attempts=attempts,
                            probe_timeout_s=timeout, transport_factory=factory)
    result = pdev.read_perf_counters(cfg, slot=slot)
    return result, box["t"]


def test_perf_reply_returns_cycles_and_bytes():  # R78.11
    result, t = _read(lambda seq, i: _perf_reply(seq, 123456, 654321) if i == 0 else None)
    assert result == (123456, 654321)
    assert t.closes >= 1, "transport must be closed (R86.7)"


def test_request_on_wire_is_vector_f_after_eth_header():  # R78.10(f)
    result, t = _read(lambda seq, i: _perf_reply(seq) if i == 0 else None)
    assert result == (6, 6)
    pyro_portion = t.sends[0][14:]
    assert pyro_portion == _hx("50 01 06 00  00 01  00 00 00 01  00 00  00 00"), (
        "first attempt must be the R78.10(f) PERF_REQUEST bytes (seq=1)")


def test_no_reply_yields_none_counters_unavailable():  # R78.11 (pre-2.4.0 child)
    result, t = _read(lambda seq, i: None)
    assert result is None, ("a silent child (drops unknown kind, R78.4) must "
                            "yield None — counters unavailable, never a fault")
    assert len(t.sends) == 3, "all R84 attempts must be made"
    assert t.closes >= 1


def test_status_error_reply_yields_none():  # R78.11 (non-resident slot)
    result, _ = _read(lambda seq, i: _status_reply(seq) if i == 0 else None)
    assert result is None


def test_short_perf_reply_raises_frame_error():  # R78.11 (malformed reply)
    def responder(seq, i):
        if i == 0:
            return ETH + pdev.encode_frame(pdev.KIND_PERF_REPLY, 1, seq, b"\x00" * 8)
        return None

    try:
        _read(responder)
    except pdev.PyroFrameError:
        pass
    else:
        raise AssertionError("payload < 16 must raise PyroFrameError (R78.11)")


def test_stray_frames_are_ignored_then_real_reply_wins():  # R78.11/N1
    def responder(seq, i):
        if i == 0:
            return b"\x00" * 40                      # not a PYRO frame (R78.1)
        if i == 1:
            return _perf_reply(seq + 7)              # wrong seq — not our reply
        if i == 2:
            return ETH + b"\x00\x01\x02"             # garbled PYRO portion
        if i == 3:
            return _perf_reply(seq, 42, 4242)
        return None

    result, _ = _read(responder)
    assert result == (42, 4242)


def test_wrong_slot_request_carries_slot():  # R78.11: slot selects the circuit
    result, t = _read(lambda seq, i: _perf_reply(seq) if i == 0 else None, slot=2)
    dec = pdev.decode_frame(t.sends[0][14:])
    assert dec.slot == 2
    assert dec.kind == pdev.KIND_PERF_REQUEST
