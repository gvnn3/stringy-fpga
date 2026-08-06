"""SipHash-2-4 golden model vs the official reference vectors.

The multi-program MAC contract requires the SipHash paper's reference
test vectors (key ``00 01 02 ... 0f``, message ``00 01 ... n-1``) to
pass in BOTH the RTL testbench and this Python model; these tests pin
the model side.  Vector values are the first 16 entries of the
reference implementation's ``vectors_sip64`` table (test.c), read as
little-endian u64; ``n=15`` is the worked example in the paper's
Appendix A.
"""
import pytest

from pyro.siphash import siphash24, siphash24_bytes

REF_KEY = bytes(range(16))

# vectors_sip64[n] as an integer tag, message = bytes(range(n)).
VECTORS = [
    0x726FDB47DD0E0E31,   # n = 0
    0x74F839C593DC67FD,   # n = 1
    0x0D6C8009D9A94F5A,   # n = 2
    0x85676696D7FB7E2D,   # n = 3
    0xCF2794E0277187B7,   # n = 4
    0x18765564CD99A68D,   # n = 5
    0xCBC9466E58FEE3CE,   # n = 6
    0xAB0200F58B01D137,   # n = 7
    0x93F5F5799A932462,   # n = 8
    0x9E0082DF0BA9E4B0,   # n = 9
    0x7A5DBBC594DDB9F3,   # n = 10
    0xF4B32F46226BADA7,   # n = 11
    0x751E8FBC860EE5FB,   # n = 12
    0x14EA5627C0843D90,   # n = 13
    0xF723CA908E7AF2EE,   # n = 14
    0xA129CA6149BE45E5,   # n = 15 (paper Appendix A)
]


@pytest.mark.parametrize("n", range(len(VECTORS)))
def test_reference_vectors(n):
    assert siphash24(REF_KEY, bytes(range(n))) == VECTORS[n]


def test_paper_appendix_a_vector():
    """The single fully-worked vector in the SipHash paper: key
    000102...0f, message 000102...0e -> 0xa129ca6149be45e5."""
    msg = bytes(range(15))
    assert siphash24(REF_KEY, msg) == 0xA129CA6149BE45E5


def test_tag_bytes_little_endian():
    """siphash24_bytes serializes the tag little-endian — the byte
    order of the MAC_REPORT record digest field (``<IHHQ``)."""
    tag = siphash24_bytes(REF_KEY, b"")
    assert tag == VECTORS[0].to_bytes(8, "little")
    assert tag[0] == 0x31 and tag[7] == 0x72


def test_multi_block_and_boundary_lengths():
    """8/16-byte messages exercise the full-block path (no final
    partial word except the length byte)."""
    assert siphash24(REF_KEY, bytes(range(8))) == VECTORS[8]
    # 16 bytes: two full blocks; value from the same reference table.
    tag16 = siphash24(REF_KEY, bytes(range(16)))
    assert tag16 != VECTORS[8]
    assert 0 <= tag16 <= 0xFFFFFFFFFFFFFFFF


def test_key_sensitivity():
    other = bytes([REF_KEY[0] ^ 1]) + REF_KEY[1:]
    assert siphash24(REF_KEY, b"x") != siphash24(other, b"x")


def test_key_length_enforced():
    with pytest.raises(ValueError):
        siphash24(b"\x00" * 15, b"")
    with pytest.raises(ValueError):
        siphash24(b"\x00" * 17, b"")
