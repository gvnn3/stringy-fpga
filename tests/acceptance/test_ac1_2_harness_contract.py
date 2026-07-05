"""AC-1-2: harness-contract conformance via the public ABI + frozen header.
(R58; R38, R47, R45, R37, §7.4)

Scope note (§13): the identity-block trust boundary (R47a), PR manifest
compatibility/integrity checks (R47b), result-ring OVF + streaming resumption,
single-issue, and the R49 alignment rejection are observable at the register/
DMA level only against a RESIDENT circuit, whose construction requires the
private L2 descriptor (see AC-1-1 scope note).  Those behaviors are exercised
through the Python surface: OVF/resumption completeness in AC-1-4, mandatory
window re-verification / byte-identical results (identity + re-verify) in
AC-1-3/1-7.  This module asserts the parts reachable from public info: the
result-ring entry layout (R38/R47), the frozen/additive enum values (R37/R38),
the absence of the obsolete PROG_*/blob path (§7.4), presence of the new
circuit-oriented symbols, and the defined no-resident CSR behavior.
"""
import ctypes
import re as stdre

import pytest

import abi_ctypes as abi

lib = None
_load_error = None
try:
    lib = abi.load()
except OSError as e:
    _load_error = str(e)

with open(abi.HEADER_PATH, "r", encoding="utf-8", errors="replace") as _f:
    HEADER = _f.read()


def test_result_ring_entry_layout_24_bytes_le():
    """R38/R47: a result-ring entry is exactly 24 bytes — start(8) end(8)
    pattern_id(4) flags(4) — little-endian, matching pyro_match."""
    assert ctypes.sizeof(abi.PyroMatch) == 24
    assert abi.PyroMatch.start.offset == 0
    assert abi.PyroMatch.end.offset == 8
    assert abi.PyroMatch.pattern_id.offset == 16
    assert abi.PyroMatch.flags.offset == 20


def test_obsolete_prog_blob_path_absent():
    """§7.4/R58: the pre-2.0.0 PROG_* registers and the R46 'PROG' blob header
    are removed — they MUST NOT appear in the ABI 2.0.0 header."""
    for gone in ("PROG_ADDR", "PROG_LEN", "PROG_ADDR_H", "pyro_prog",
                 "pyro_compile", "pyro_prog_load", "pyro_prog_free"):
        assert gone not in HEADER, f"obsolete symbol {gone!r} present in header"
    # the old 'PROG' blob magic 0x50524F47 must be gone
    assert "50524F47" not in HEADER.upper().replace("_", "")


def test_new_circuit_symbols_present():
    """R38/R40/R47a: the circuit-oriented ABI 2.0.0 symbols are declared."""
    for sym in ("pyro_circuit", "pyro_generate", "pyro_synth_request",
                "pyro_circuit_status", "pyro_circuit_load", "pyro_circ_status",
                "PYRO_CIRC_RESIDENT", "PYRO_CIRC_COLD", "PYRO_E_NOT_RESIDENT",
                "PYRO_E_SYNTH", "CIRC_ID", "CIRC_FLAGS"):
        assert sym in HEADER, f"expected ABI 2.0.0 symbol {sym!r} in header"


def test_status_enum_values_frozen():
    """R37/R38: the pyro_status enumerators have their normative, frozen values
    (additive-only; NOT_RESIDENT=7, SYNTH=8 must never be renumbered)."""
    expected = {
        "PYRO_OK": 0, "PYRO_E_UNSUPPORTED": 1, "PYRO_E_CAPACITY": 2,
        "PYRO_E_DEVICE": 3, "PYRO_E_INVALID": 4, "PYRO_E_NOMEM": 5,
        "PYRO_E_TIMEOUT": 6, "PYRO_E_NOT_RESIDENT": 7, "PYRO_E_SYNTH": 8,
    }
    for name, val in expected.items():
        m = stdre.search(rf"{name}\s*=\s*(\d+)", HEADER)
        assert m, f"enumerator {name} not found in header"
        assert int(m.group(1)) == val, f"{name} = {m.group(1)}, expected {val}"


def test_circ_status_enum_values_frozen():
    """R38: pyro_circ_status tier values (mirrors R4): COLD..FALLBACK 0..4."""
    expected = {
        "PYRO_CIRC_COLD": 0, "PYRO_CIRC_SYNTHESIZING": 1, "PYRO_CIRC_WARM": 2,
        "PYRO_CIRC_RESIDENT": 3, "PYRO_CIRC_FALLBACK": 4,
    }
    for name, val in expected.items():
        m = stdre.search(rf"{name}\s*=\s*(\d+)", HEADER)
        assert m and int(m.group(1)) == val, f"{name} expected {val}"


def test_encoding_enum_values_frozen():
    """R38/R47a: encoding tag values (part of the circuit identity)."""
    assert stdre.search(r"PYRO_ENC_BYTES\s*=\s*0", HEADER)
    assert stdre.search(r"PYRO_ENC_UTF8\s*=\s*1", HEADER)


@pytest.mark.skipif(lib is None, reason=f"libpyro_rt.so unavailable: {_load_error}")
def test_csr_read_no_resident_returns_zero():
    """§7.4/R45 (debug seam): with no resident circuit, a CSR read returns 0 —
    a defined, non-crashing behavior (the ID/identity block is only populated
    once a circuit is resident, R47a)."""
    ctx = ctypes.c_void_p()
    assert lib.pyro_ctx_open(ctypes.byref(ctx), b"model://") == abi.PYRO_OK
    try:
        for off in (0x0000, 0x0018, 0x001C, 0x0020, 0x0024, 0x0028, 0x004C):
            assert lib.pyro_ctx_debug_csr_read(ctx, off) == 0
    finally:
        lib.pyro_ctx_close(ctx)
