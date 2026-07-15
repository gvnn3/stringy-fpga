"""AC-2b-1: host-side control-frame codec — pyro.device.encode_frame / decode_frame.

LIVE (no hardware, pure host code): the R78 control-frame codec is exercised
strictly from the spec — R78 (frame format), R78.10 (normative hex test vectors),
and R86.2/R86.3 (public encode_frame/decode_frame contract + PyroFrameError
taxonomy).  Every assertion is byte-exact against the spec's normative vectors on
their PYRO-header-onward portion (frame offset 14+); the 14-byte Ethernet L2
header, FCS, and 60-byte zero-padding are the transport/NIC's concern and are
deliberately NOT this codec's output (R86.2, R78.9/R78.10).

Derived ONLY from the spec.  No implementation source is read.  These tests are
pending until pyro.device lands (coder is in parallel); a missing module marks the
device-codec tests xfail (pending-implementation), never a silent PASS/SKIP.

Coverage: AC-2b-1.  Requirements: R78.1–R78.10, R86.1, R86.2, R86.3, R47.
"""
import struct

import pytest

try:
    import pyro.device as pdev
    _IMPORT_ERR = None
except Exception as exc:  # noqa: BLE001 — module not yet landed is pending, not a spec bug
    pdev = None
    _IMPORT_ERR = exc


def _need_pdev():
    if pdev is None:
        pytest.xfail(f"pending pyro.device implementation (R86): {_IMPORT_ERR!r}")


def _field(res, name):
    """R86.3: decode_frame returns a structured result whose fields are named
    exactly for the R78 header.  Support attribute- or mapping-style access so the
    test binds to the NAMES (spec-normative) not a particular container type."""
    if hasattr(res, name):
        return getattr(res, name)
    try:
        return res[name]
    except (TypeError, KeyError):
        raise AssertionError(f"decode_frame result lacks R78 field {name!r}: {res!r}")


# --------------------------------------------------------------------------
# R78.4 message kinds.
# --------------------------------------------------------------------------
ID_REQUEST = 0x01
ID_REPLY = 0x02
MATCH_REQUEST = 0x03
MATCH_REPLY = 0x04
STATUS_ERROR = 0x05
PERF_REQUEST = 0x06  # R78.11 (v2.4.0)
PERF_REPLY = 0x07
VALID_KINDS = (ID_REQUEST, ID_REPLY, MATCH_REQUEST, MATCH_REPLY, STATUS_ERROR,
               PERF_REQUEST, PERF_REPLY)

MTU_PAYLOAD_MAX = 1486  # R78.9 / R86.2 / R86.3 payload bound


# --------------------------------------------------------------------------
# R78.10 normative test vectors, PYRO-header-onward (frame offset 14+), i.e. the
# exact bytes encode_frame MUST produce and decode_frame MUST parse (R86.2/R86.3).
# --------------------------------------------------------------------------
def _hx(s):
    return bytes.fromhex(s.replace(" ", ""))


# (a) ID_REQUEST: seq=1, slot=0, empty payload.
VEC_A_HDR = _hx("50 01 01 00  00 00  00 00 00 01  00 00  00 00")
VEC_A_PAYLOAD = b""

# (b) ID_REPLY: seq=1, slot=0, length=12, static_shell_id=0x02025A3C,
#     harness_version=0x00010000, rp_child_id=0.
VEC_B_PAYLOAD = _hx("02 02 5A 3C  00 01 00 00  00 00 00 00")
VEC_B = _hx("50 01 02 00  00 00  00 00 00 01  00 0C  00 00") + VEC_B_PAYLOAD

# (c) MATCH_REQUEST: slot=1, seq=2, start_off=0, out_cap=4, corpus "abcabc".
VEC_C_PAYLOAD = _hx("00 00 00 00 00 00 00 00  00 04  00 00") + b"abcabc"
VEC_C = _hx("50 01 03 00  00 01  00 00 00 02  00 12  00 00") + VEC_C_PAYLOAD

# (d) MATCH_REPLY: echoes slot=1, seq=2; count=2, status=0; two 24-byte LE
#     pyro_match entries [1,3) and [4,6), pattern_id=0, flags=0.
VEC_D_ENTRIES = (struct.pack("<QQII", 1, 3, 0, 0) +
                 struct.pack("<QQII", 4, 6, 0, 0))
VEC_D_PAYLOAD = _hx("00 02  00 00  00 00 00 00") + VEC_D_ENTRIES
VEC_D = _hx("50 01 04 00  00 01  00 00 00 02  00 38  00 00") + VEC_D_PAYLOAD

# (e) STATUS/ERROR (R78.10(e), v2.2.3): the ID stub's reply to MATCH_REQUEST (c) —
#     kind 0x05, echoes slot=1, seq=2, length=4, payload code=PYRO_E_NOT_RESIDENT (7).
VEC_E_PAYLOAD = _hx("00 00 00 07")  # code = 7 (BE)
VEC_E = _hx("50 01 05 00  00 01  00 00 00 02  00 04  00 00") + VEC_E_PAYLOAD
PYRO_E_NOT_RESIDENT = 7  # R38

# (f) PERF_REQUEST (R78.10(f), v2.4.0): slot=1, seq=3, empty payload.
VEC_F = _hx("50 01 06 00  00 01  00 00 00 03  00 00  00 00")
VEC_F_PAYLOAD = b""

# (g) PERF_REPLY (R78.10(g), v2.4.0): echoes slot=1, seq=3; length=16,
#     payload = cycles(8 BE)=6 | bytes(8 BE)=6 (values illustrative, layout
#     normative — R78.11).
VEC_G_PAYLOAD = _hx("00 00 00 00 00 00 00 06  00 00 00 00 00 00 00 06")
VEC_G = _hx("50 01 07 00  00 01  00 00 00 03  00 10  00 00") + VEC_G_PAYLOAD


# ==========================================================================
# encode_frame — byte-exact against every normative vector (R78.10 / R86.2).
# ==========================================================================
def test_encode_id_request_matches_vector_a():  # AC-2b-1 (R78.5/R78.10/R86.2)
    _need_pdev()
    out = pdev.encode_frame(ID_REQUEST, 0, 1, VEC_A_PAYLOAD)
    assert out == VEC_A_HDR, f"{out.hex()} != {VEC_A_HDR.hex()}"
    assert len(out) == 14 + len(VEC_A_PAYLOAD)


def test_encode_id_reply_matches_vector_b():  # AC-2b-1 (R78.5/R78.10/R86.2)
    _need_pdev()
    out = pdev.encode_frame(ID_REPLY, 0, 1, VEC_B_PAYLOAD)
    assert out == VEC_B, f"{out.hex()} != {VEC_B.hex()}"


def test_encode_match_request_matches_vector_c():  # AC-2b-1 (R78.6/R78.10/R86.2)
    _need_pdev()
    out = pdev.encode_frame(MATCH_REQUEST, 1, 2, VEC_C_PAYLOAD)
    assert out == VEC_C, f"{out.hex()} != {VEC_C.hex()}"
    # length field (BE @ offset 24-25 of the header-onward slice, i.e. bytes 10-11).
    assert out[10:12] == (18).to_bytes(2, "big"), "length must be 0x0012 = 18 (R78.10c)"


def test_encode_match_reply_matches_vector_d():  # AC-2b-1 (R78.7/R78.10/R86.2)
    _need_pdev()
    out = pdev.encode_frame(MATCH_REPLY, 1, 2, VEC_D_PAYLOAD)
    assert out == VEC_D, f"{out.hex()} != {VEC_D.hex()}"
    assert out[10:12] == (56).to_bytes(2, "big"), "length must be 0x0038 = 56 (R78.10d)"


def test_encode_status_error_matches_vector_e():  # AC-2b-1 (R78.8/R78.10(e)/R86.2)
    """R78.10(e) (v2.2.3): the ID stub's STATUS/ERROR PYRO_E_NOT_RESIDENT reply to
    MATCH_REQUEST (c) — kind 0x05, slot/seq echoed, length 4, code=7 (BE)."""
    _need_pdev()
    out = pdev.encode_frame(STATUS_ERROR, 1, 2, VEC_E_PAYLOAD)
    assert out == VEC_E, f"{out.hex()} != {VEC_E.hex()}"
    assert out[2] == 0x05, "kind must be STATUS/ERROR 0x05 (R78.10e)"
    assert out[10:12] == (4).to_bytes(2, "big"), "length must be 0x0004 = 4 (R78.10e)"


def test_decode_status_error_vector_e_fields_and_code():  # AC-2b-1 (R78.8/R78.10(e)/R86.3)
    """Decoding (e) recovers the echoed slot/seq and the PYRO_E_NOT_RESIDENT code."""
    _need_pdev()
    res = pdev.decode_frame(VEC_E)
    assert _field(res, "kind") == STATUS_ERROR
    assert _field(res, "slot") == 1
    assert _field(res, "seq") == 2
    assert _field(res, "length") == 4
    payload = bytes(_field(res, "payload"))
    assert payload == VEC_E_PAYLOAD
    code = int.from_bytes(payload[0:4], "big")  # R78.8: code is 4 bytes BE
    assert code == PYRO_E_NOT_RESIDENT == 7, (
        "STATUS/ERROR code must be PYRO_E_NOT_RESIDENT (7) (R38/R78.10e)")


def test_encode_perf_request_matches_vector_f():  # AC-2b-1 (R78.11/R78.10(f)/R86.2)
    """R78.10(f) (v2.4.0): PERF_REQUEST — kind 0x06, slot=1, seq=3, empty payload."""
    _need_pdev()
    out = pdev.encode_frame(PERF_REQUEST, 1, 3, VEC_F_PAYLOAD)
    assert out == VEC_F, f"{out.hex()} != {VEC_F.hex()}"
    assert out[2] == 0x06, "kind must be PERF_REQUEST 0x06 (R78.10f)"
    assert out[10:12] == (0).to_bytes(2, "big"), "length must be 0 (R78.11)"


def test_encode_perf_reply_matches_vector_g():  # AC-2b-1 (R78.11/R78.10(g)/R86.2)
    """R78.10(g) (v2.4.0): PERF_REPLY — kind 0x07, echoes slot=1/seq=3, length=16,
    payload = cycles(8 BE) | bytes(8 BE) (R45a counters; layout normative)."""
    _need_pdev()
    out = pdev.encode_frame(PERF_REPLY, 1, 3, VEC_G_PAYLOAD)
    assert out == VEC_G, f"{out.hex()} != {VEC_G.hex()}"
    assert out[2] == 0x07, "kind must be PERF_REPLY 0x07 (R78.10g)"
    res = pdev.decode_frame(out)
    payload = bytes(_field(res, "payload"))
    assert int.from_bytes(payload[0:8], "big") == 6    # cycles (BE, R78.11)
    assert int.from_bytes(payload[8:16], "big") == 6   # bytes (BE, R78.11)


def test_encode_sets_frozen_header_fields():  # AC-2b-1 (R78.3/R86.2)
    """magic=0x50, version=0x01, flags=0, reserved=0 are frozen for version 1."""
    _need_pdev()
    out = pdev.encode_frame(ID_REPLY, 0, 1, VEC_B_PAYLOAD)
    assert out[0] == 0x50, "magic MUST be 0x50 ('P') (R78.3)"
    assert out[1] == 0x01, "version MUST be 0x01 (R78.3)"
    assert out[3] == 0x00, "flags MUST be 0x00 in version 1 (R78.3)"
    assert out[12:14] == b"\x00\x00", "reserved MUST be 0x0000 (R78.3)"


def test_encode_header_fields_are_big_endian():  # AC-2b-1 (R78.2/R78.3/R86.2)
    """slot, seq, length are big-endian in the header (R78.2)."""
    _need_pdev()
    out = pdev.encode_frame(MATCH_REQUEST, 0x0102, 0x03040506, b"xyz")
    assert out[4:6] == b"\x01\x02", "slot must be big-endian (R78.3)"
    assert out[6:10] == b"\x03\x04\x05\x06", "seq must be big-endian (R78.3)"
    assert out[10:12] == b"\x00\x03", "length must be big-endian len(payload) (R78.3)"


# ==========================================================================
# decode_frame — recovers exactly the R78-named fields (R78.3 / R86.3).
# ==========================================================================
@pytest.mark.parametrize(
    "vec,kind,slot,seq,length,payload",
    [
        (VEC_A_HDR, ID_REQUEST, 0, 1, 0, VEC_A_PAYLOAD),
        (VEC_B, ID_REPLY, 0, 1, 12, VEC_B_PAYLOAD),
        (VEC_C, MATCH_REQUEST, 1, 2, 18, VEC_C_PAYLOAD),
        (VEC_D, MATCH_REPLY, 1, 2, 56, VEC_D_PAYLOAD),
        (VEC_E, STATUS_ERROR, 1, 2, 4, VEC_E_PAYLOAD),
        (VEC_F, PERF_REQUEST, 1, 3, 0, VEC_F_PAYLOAD),
        (VEC_G, PERF_REPLY, 1, 3, 16, VEC_G_PAYLOAD),
    ],
    ids=["ID_REQUEST", "ID_REPLY", "MATCH_REQUEST", "MATCH_REPLY", "STATUS_ERROR",
         "PERF_REQUEST", "PERF_REPLY"],
)
def test_decode_recovers_named_fields(vec, kind, slot, seq, length, payload):
    # AC-2b-1 (R78.3/R78.10/R86.3)
    _need_pdev()
    res = pdev.decode_frame(vec)
    assert _field(res, "magic") == 0x50
    assert _field(res, "version") == 0x01
    assert _field(res, "kind") == kind
    assert _field(res, "flags") == 0
    assert _field(res, "slot") == slot
    assert _field(res, "seq") == seq
    assert _field(res, "length") == length
    assert bytes(_field(res, "payload")) == payload


@pytest.mark.parametrize(
    "kind,slot,seq,payload",
    [
        (ID_REQUEST, 0, 1, VEC_A_PAYLOAD),
        (ID_REPLY, 0, 1, VEC_B_PAYLOAD),
        (MATCH_REQUEST, 1, 2, VEC_C_PAYLOAD),
        (MATCH_REPLY, 1, 2, VEC_D_PAYLOAD),
        (STATUS_ERROR, 0, 9, struct.pack(">I", 7) + b"not resident"),
        (PERF_REQUEST, 1, 3, VEC_F_PAYLOAD),
        (PERF_REPLY, 1, 3, VEC_G_PAYLOAD),
    ],
    ids=["ID_REQUEST", "ID_REPLY", "MATCH_REQUEST", "MATCH_REPLY", "STATUS_ERROR",
         "PERF_REQUEST", "PERF_REPLY"],
)
def test_encode_decode_roundtrip(kind, slot, seq, payload):  # AC-2b-1 (R86.2/R86.3)
    _need_pdev()
    enc = pdev.encode_frame(kind, slot, seq, payload)
    res = pdev.decode_frame(enc)
    assert _field(res, "kind") == kind
    assert _field(res, "slot") == slot
    assert _field(res, "seq") == seq
    assert _field(res, "length") == len(payload)
    assert bytes(_field(res, "payload")) == payload


# ==========================================================================
# MATCH_REPLY embedded 24-byte little-endian pyro_match round-trip (R47/R78.7).
# ==========================================================================
def test_match_reply_embeds_le_pyro_match_entries():  # AC-2b-1 (R78.2/R78.7/R47)
    """The header is big-endian but each MATCH_REPLY entry is the 24-byte LE
    pyro_match (start,end,pattern_id,flags) verbatim (R78.7) — mixed endianness."""
    _need_pdev()
    enc = pdev.encode_frame(MATCH_REPLY, 1, 2, VEC_D_PAYLOAD)
    assert enc == VEC_D
    res = pdev.decode_frame(enc)
    payload = bytes(_field(res, "payload"))
    count = int.from_bytes(payload[0:2], "big")  # count is BE (R78.7)
    status = int.from_bytes(payload[2:4], "big")  # status is BE (R78.7)
    assert count == 2
    assert status == 0
    got = []
    for i in range(count):
        start, end, pid, flags = struct.unpack_from("<QQII", payload, 8 + 24 * i)
        got.append((start, end, pid, flags))
    assert got == [(1, 3, 0, 0), (4, 6, 0, 0)], (
        "entries must decode as little-endian pyro_match (R78.7/R47)")
    # flags bit0 (verified) == 0 => unverified; host must re-verify per R19 (R78.7).
    assert all((flags & 0x1) == 0 for *_, flags in got)


# ==========================================================================
# Padding tolerance (R78.9 / R86.3): trailing bytes beyond `length` are ignored.
# ==========================================================================
def test_decode_ignores_trailing_zero_padding():  # AC-2b-1 (R78.9/R86.3)
    _need_pdev()
    padded = VEC_B + b"\x00" * 20  # simulate 60-byte-minimum zero-padding on the wire
    res = pdev.decode_frame(padded)
    assert _field(res, "length") == 12
    assert bytes(_field(res, "payload")) == VEC_B_PAYLOAD, (
        "payload MUST be exactly `length` bytes; trailing padding ignored (R78.9)")


def test_decode_ignores_nonzero_trailing_bytes():  # AC-2b-1 (R78.9/R86.3)
    """R78.9: the receiver ignores ALL trailing bytes beyond `length`, delimited by
    the length field alone (not only zero padding)."""
    _need_pdev()
    padded = VEC_A_HDR + b"\xde\xad\xbe\xef"
    res = pdev.decode_frame(padded)
    assert _field(res, "length") == 0
    assert bytes(_field(res, "payload")) == b""


# ==========================================================================
# Error taxonomy — encode_frame (R86.1 / R86.2): only PyroFrameError, no leakage.
# ==========================================================================
def test_exception_taxonomy_shape():  # AC-2b-1 (R86.1)
    _need_pdev()
    assert issubclass(pdev.PyroFrameError, pdev.PyroDeviceError)
    assert issubclass(pdev.PyroDeviceError, Exception)
    # Must NOT be an OSError/PermissionError family (no transport-internal leakage).
    assert not issubclass(pdev.PyroFrameError, OSError)


def test_encode_oversize_payload_raises_frame_error():  # AC-2b-1 (R78.9/R86.2)
    _need_pdev()
    with pytest.raises(pdev.PyroFrameError):
        pdev.encode_frame(MATCH_REQUEST, 1, 2, b"\x00" * (MTU_PAYLOAD_MAX + 1))


def test_encode_max_payload_is_accepted_boundary():  # AC-2b-1 (R78.9/R86.2)
    """1486 is the inclusive max; exactly 1486 MUST encode without error."""
    _need_pdev()
    out = pdev.encode_frame(MATCH_REQUEST, 1, 2, b"\x00" * MTU_PAYLOAD_MAX)
    assert len(out) == 14 + MTU_PAYLOAD_MAX
    assert out[10:12] == MTU_PAYLOAD_MAX.to_bytes(2, "big")


# R86.2 (v2.4.0): sendable kinds are EXACTLY 0x01-0x07 (0x06/0x07 added by
# R78.11).  0x00 is `reserved` and is NOT a message kind — encode_frame MUST
# reject it, as must any value > 0x07.
@pytest.mark.parametrize("bad_kind", [0x00, 0x08, 0x09, 0x10, 0x42, 0xFF])
def test_encode_invalid_kind_raises_frame_error(bad_kind):  # AC-2b-1 (R78.4/R86.2)
    _need_pdev()
    with pytest.raises(pdev.PyroFrameError):
        pdev.encode_frame(bad_kind, 0, 1, b"")


def test_encode_reserved_kind_0x00_rejected():  # AC-2b-1 (R86.2 v2.2.2)
    """R86.2 (v2.2.2, explicit): kind 0x00 is `reserved`, not a sendable message
    kind, and encode_frame MUST raise PyroFrameError for it."""
    _need_pdev()
    with pytest.raises(pdev.PyroFrameError):
        pdev.encode_frame(0x00, 0, 1, b"")


def test_encode_all_sendable_kinds_accepted():  # AC-2b-1 (R86.2 v2.4.0)
    """The sendable kinds are exactly 0x01-0x07 (R78.4/R86.2); each MUST encode."""
    _need_pdev()
    for kind in (0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07):
        out = pdev.encode_frame(kind, 0, 1, b"")
        assert out[2] == kind


@pytest.mark.parametrize("bad_flags", [1, 2, 0x80, 0xFF])
def test_encode_nonzero_flags_raises_frame_error(bad_flags):  # AC-2b-1 (R78.3/R86.2)
    _need_pdev()
    with pytest.raises(pdev.PyroFrameError):
        pdev.encode_frame(ID_REQUEST, 0, 1, b"", flags=bad_flags)


def test_encode_does_not_leak_os_permission_error():  # AC-2b-1 (R86.1)
    """A malformed encode raises PyroFrameError specifically — never OSError/
    PermissionError or a transport internal (R86.1 no-leakage)."""
    _need_pdev()
    try:
        pdev.encode_frame(0xFF, 0, 1, b"")
    except pdev.PyroFrameError:
        pass
    except (OSError, PermissionError) as exc:  # pragma: no cover - spec violation
        pytest.fail(f"encode_frame leaked {type(exc).__name__} (R86.1)")


# ==========================================================================
# Error taxonomy — decode_frame (R86.1 / R86.3): only PyroFrameError, no leakage.
# ==========================================================================
def test_decode_bad_magic_raises_frame_error():  # AC-2b-1 (R78.3/R86.3)
    _need_pdev()
    bad = bytearray(VEC_A_HDR)
    bad[0] = 0x51
    with pytest.raises(pdev.PyroFrameError):
        pdev.decode_frame(bytes(bad))


def test_decode_bad_version_raises_frame_error():  # AC-2b-1 (R78.3/R86.3)
    _need_pdev()
    bad = bytearray(VEC_A_HDR)
    bad[1] = 0x02
    with pytest.raises(pdev.PyroFrameError):
        pdev.decode_frame(bytes(bad))


def test_decode_nonzero_flags_raises_frame_error():  # AC-2b-1 (R78.3/R86.3)
    _need_pdev()
    bad = bytearray(VEC_A_HDR)
    bad[3] = 0x01
    with pytest.raises(pdev.PyroFrameError):
        pdev.decode_frame(bytes(bad))


def test_decode_oversize_length_raises_frame_error():  # AC-2b-1 (R78.9/R86.3)
    """A `length` field exceeding the 1486 bound is malformed (R86.3)."""
    _need_pdev()
    bad = bytearray(VEC_A_HDR)
    bad[10:12] = (MTU_PAYLOAD_MAX + 1).to_bytes(2, "big")
    with pytest.raises(pdev.PyroFrameError):
        pdev.decode_frame(bytes(bad))


def test_decode_truncated_payload_raises_frame_error():  # AC-2b-1 (R86.3)
    """length inconsistent with available bytes (fewer payload bytes than
    `length`) is malformed (R86.3)."""
    _need_pdev()
    # header claims length=12 but only 5 payload bytes present.
    truncated = _hx("50 01 02 00  00 00  00 00 00 01  00 0C  00 00") + b"\x00" * 5
    with pytest.raises(pdev.PyroFrameError):
        pdev.decode_frame(truncated)


def test_decode_unknown_kind_does_not_raise():  # AC-2b-1 (R78.4/R86.3)
    """decode_frame's raise conditions (R86.3) do NOT include `kind`; an unknown
    kind is parsed and returned (the HOST, per R78.4/R52, routes to fallback — a
    layer above the codec).  0x08 is the first kind undefined as of v2.4.0
    (0x06/0x07 became PERF_REQUEST/PERF_REPLY, R78.11)."""
    _need_pdev()
    frame = bytearray(_hx("50 01 08 00  00 00  00 00 00 01  00 00  00 00"))
    res = pdev.decode_frame(bytes(frame))
    assert _field(res, "kind") == 0x08
    assert bytes(_field(res, "payload")) == b""


def test_decode_does_not_leak_os_permission_error():  # AC-2b-1 (R86.1)
    _need_pdev()
    bad = bytearray(VEC_A_HDR)
    bad[0] = 0x00  # bad magic
    try:
        pdev.decode_frame(bytes(bad))
    except pdev.PyroFrameError:
        pass
    except (OSError, PermissionError) as exc:  # pragma: no cover - spec violation
        pytest.fail(f"decode_frame leaked {type(exc).__name__} (R86.1)")
