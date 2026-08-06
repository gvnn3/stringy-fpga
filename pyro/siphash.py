"""Pure-Python SipHash-2-4 — the golden model for the P1 MAC engine.

This is the host-side reference the RTL digest engine is verified
against (multi-program MAC design contract): SipHash-2-4, 128-bit key,
64-bit tag, reference convention from the SipHash paper (Aumasson &
Bernstein, "SipHash: a fast short-input PRF"): ``k0`` = key bytes 0-7
little-endian, ``k1`` = key bytes 8-15 little-endian; the tag is the
64-bit value ``v0 ^ v1 ^ v2 ^ v3`` after finalization, transmitted
little-endian when serialized to bytes.

Official test vectors (paper appendix / reference ``test.c``
``vectors_sip64``), key ``00 01 02 ... 0f``, message ``00 01 ... n-1``:

    n=0  -> 0x726fdb47dd0e0e31      n=8  -> 0x93f5f5799a932462
    n=1  -> 0x74f839c593dc67fd      n=15 -> 0xa129ca6149be45e5

The full 16-entry prefix of the table is pinned in
``tests/unit/test_siphash.py``; the ``n=15`` value is the single
worked example in the paper's Appendix A.  Both the RTL testbench and
this model MUST pass them (contract: "MAC algorithm").

No dependencies; byte-serial absorption in hardware corresponds to the
8-byte little-endian word grouping below.
"""

from __future__ import annotations

__all__ = ["siphash24", "siphash24_bytes"]

_MASK64 = 0xFFFFFFFFFFFFFFFF


def _rotl(x: int, b: int) -> int:
    """Rotate the 64-bit value ``x`` left by ``b`` bits."""
    return ((x << b) | (x >> (64 - b))) & _MASK64


def _sipround(v0: int, v1: int, v2: int, v3: int):
    """One SipRound (paper Fig. 2.1)."""
    v0 = (v0 + v1) & _MASK64
    v1 = _rotl(v1, 13)
    v1 ^= v0
    v0 = _rotl(v0, 32)
    v2 = (v2 + v3) & _MASK64
    v3 = _rotl(v3, 16)
    v3 ^= v2
    v0 = (v0 + v3) & _MASK64
    v3 = _rotl(v3, 21)
    v3 ^= v0
    v2 = (v2 + v1) & _MASK64
    v1 = _rotl(v1, 17)
    v1 ^= v2
    v2 = _rotl(v2, 32)
    return v0, v1, v2, v3


def siphash24(key: bytes, data: bytes) -> int:
    """SipHash-2-4 of ``data`` under the 16-byte ``key`` -> 64-bit int.

    ``key`` bytes 0-7 are ``k0`` little-endian, bytes 8-15 ``k1``
    little-endian (reference convention, and the exact byte order of
    the R78 ``MAC_KEY_LOAD`` 16-byte key field).  Raises
    :class:`ValueError` on a key that is not exactly 16 bytes.
    """
    key = bytes(key)
    if len(key) != 16:
        raise ValueError(
            "SipHash key must be exactly 16 bytes, got %d" % len(key))
    data = bytes(data)
    k0 = int.from_bytes(key[0:8], "little")
    k1 = int.from_bytes(key[8:16], "little")
    v0 = k0 ^ 0x736F6D6570736575
    v1 = k1 ^ 0x646F72616E646F6D
    v2 = k0 ^ 0x6C7967656E657261
    v3 = k1 ^ 0x7465646279746573

    n = len(data)
    end = n - (n % 8)
    for off in range(0, end, 8):
        m = int.from_bytes(data[off:off + 8], "little")
        v3 ^= m
        v0, v1, v2, v3 = _sipround(v0, v1, v2, v3)
        v0, v1, v2, v3 = _sipround(v0, v1, v2, v3)
        v0 ^= m
    # Final block: remaining bytes little-endian, length mod 256 in the
    # top byte (paper section 2.2).
    b = ((n & 0xFF) << 56) | int.from_bytes(data[end:], "little")
    v3 ^= b
    v0, v1, v2, v3 = _sipround(v0, v1, v2, v3)
    v0, v1, v2, v3 = _sipround(v0, v1, v2, v3)
    v0 ^= b
    v2 ^= 0xFF
    for _ in range(4):
        v0, v1, v2, v3 = _sipround(v0, v1, v2, v3)
    return (v0 ^ v1 ^ v2 ^ v3) & _MASK64


def siphash24_bytes(key: bytes, data: bytes) -> bytes:
    """SipHash-2-4 tag as 8 bytes little-endian — the byte order of the
    ``digest`` field in a ``MAC_REPORT`` record (``<IHHQ``)."""
    return siphash24(key, data).to_bytes(8, "little")
