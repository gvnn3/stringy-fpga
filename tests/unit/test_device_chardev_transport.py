"""P2d (v2.7.0-draft): the QDMA ST char-dev transport in pyro.device.

LIVE (no hardware): a named FIFO stands in for /dev/qdmaNNNNN-ST-N (same
byte-stream, no-datagram-boundary semantics), driving _CharDevTransport's
R78.3 length-field re-framer through the cases AF_PACKET never presents:

  * one frame per read, two frames in one read, one frame split across reads;
  * leading zero padding (the R78.9 60-byte sender pad) skipped between frames;
  * garbage prefix resynced to the next plausible header (ethertype+MAGIC);
  * recv timeout None sentinel and idempotent close (R86.7);
  * fail-closed construction: chardev=None => OSError (R68 no-default rule);
  * _make_transport precedence: factory seam > chardev > iface.
"""
import os
import struct

import pytest

import pyro.device as pdev

ETH = (b"\x02\x00\x00\x00\x00\x01" + b"\x02\x00\x00\x00\x00\x02"
       + struct.pack(">H", pdev.ETHERTYPE))


def _frame(payload=b"", seq=1, kind=pdev.KIND_STATUS):
    return ETH + pdev.encode_frame(kind, 0, seq, payload)


@pytest.fixture
def fifo(tmp_path):
    path = str(tmp_path / "qdma-ST-0")
    os.mkfifo(path)
    return path


@pytest.fixture
def transport(fifo):
    t = pdev._CharDevTransport(pdev.DeviceConfig(iface=None, chardev=fifo))
    yield t
    t.close()


def _feed(fifo, data):
    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def test_single_frame_roundtrip(fifo, transport):
    f = _frame(b"hello", seq=7)
    _feed(fifo, f)
    assert transport.recv(0.5) == f


def test_two_frames_in_one_read_are_split(fifo, transport):
    f1, f2 = _frame(b"a", seq=1), _frame(b"bb", seq=2)
    _feed(fifo, f1 + f2)
    assert transport.recv(0.5) == f1
    assert transport.recv(0.5) == f2


def test_frame_split_across_reads_is_assembled(fifo, transport):
    f = _frame(b"x" * 100, seq=3)
    _feed(fifo, f[:20])
    assert transport.recv(0.05) is None      # incomplete: None, not a partial
    _feed(fifo, f[20:])
    assert transport.recv(0.5) == f


def test_leading_zero_padding_skipped(fifo, transport):
    # R78.9: a sender pads short frames to 60 B with zeros; on a byte stream
    # that pad precedes the next frame.
    f = _frame(b"pad", seq=4)
    _feed(fifo, b"\x00" * 17 + f)
    assert transport.recv(0.5) == f


def test_garbage_prefix_resyncs_to_next_frame(fifo, transport):
    f = _frame(b"ok", seq=5)
    _feed(fifo, b"\x7f\x13\x37" * 7 + f)
    assert transport.recv(0.5) == f


def test_recv_timeout_returns_none(transport):
    assert transport.recv(0.05) is None


def test_send_pads_to_l2_minimum(fifo, transport):
    short = _frame(b"")                      # 28 B on the wire
    transport.send(short)
    fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        raw = os.read(fd, 4096)
    finally:
        os.close(fd)
    assert raw == short + b"\x00" * (60 - len(short))


def test_close_is_idempotent(fifo):
    t = pdev._CharDevTransport(pdev.DeviceConfig(iface=None, chardev=fifo))
    t.close()
    t.close()                                # second close must not raise


def test_unconfigured_chardev_fails_closed():
    with pytest.raises(OSError):
        pdev._CharDevTransport(pdev.DeviceConfig(iface=None, chardev=None))


def test_make_transport_precedence(fifo):
    sentinel = object()
    cfg = pdev.DeviceConfig(iface=None, chardev=fifo,
                            transport_factory=lambda c: sentinel)
    assert pdev._make_transport(cfg) is sentinel          # seam wins (R86.6)
    t = pdev._make_transport(pdev.DeviceConfig(iface=None, chardev=fifo))
    try:
        assert isinstance(t, pdev._CharDevTransport)      # chardev > iface
    finally:
        t.close()


def test_probe_reason_names_chardev_gate(tmp_path):
    # Unreadable char-dev => the P2d canonical transport clause, not the
    # CAP_NET_RAW clause (probe gates on file access in chardev mode).
    missing = str(tmp_path / "absent-ST-0")
    usable, reason = pdev.probe_device(
        pdev.DeviceConfig(iface=None, chardev=missing))
    assert usable is False
    assert "transport: QDMA char-dev not accessible" in reason
    assert "CAP_NET_RAW" not in reason
