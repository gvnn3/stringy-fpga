"""Serialized-automaton artifact format (spec §7.4 R47b; Task-7 extension).

Task 7 binds the software model behind the ABI-2.0.0 native runtime
(``src/pyro_rt.c``).  A *C* model cannot import Python, so it cannot execute the
live :class:`pyro.hdl.automaton.Automaton` object — it must be handed a
**self-describing binary table** it can parse and run.  This module produces
that
table and embeds it, together with the manifest-critical R47a/R47b header
fields,
into the **PR bitstream artifact** (``artifact.bin``) written by the mock
toolchain (:mod:`pyro.synth.toolchain`).

The native runtime coordinates with PYRO *only* through this file format (the
Task-7 brief's rule: "coordinate ONLY through the artifact file format, do not
import Python from C").  The artifact therefore carries, in a fixed
little-endian binary layout, everything the C model needs to:

  * verify the artifact is intact (a CRC-32 trailer over the whole payload — the
    R47b integrity check the C model performs without reading the JSON sidecar);
  * verify it is loadable into the live shell/harness (embedded
    ``shell_version`` / ``harness_version`` — the R47b compatibility check);
  * verify its baked identity matches the pattern the host wants to dispatch
    (embedded 128-bit ``pattern_hash`` + ``circ_flags`` — the R47a trust
    boundary); and
  * execute the recognizer (the automaton state/edge tables).

Binary layout (all multi-byte fields little-endian)::

    off  size  field
    0    8     magic  "PYROART1"
    8    4     format_version (== 1)
    12   16    pattern_hash        (CIRC_ID0..3, R47a)
    28   4     circ_flags          (packed CIRC_FLAGS word, R45 0x0028)
    32   4     encoding            (0 = PYRO_ENC_BYTES, 1 = PYRO_ENC_UTF8)
    36   4     effective_flags     (canonicalized re flags, R47a)
    40   4     generator_version
    44   4     harness_version
    48   4     shell_version       (target PR-region id, R47b)
    -- automaton body --
    52   4     n_states
    56   4     start_state
    60   4     accept_state
    64   4     n_byte_edges
    68   4     n_eps_edges
    72   4     n_assert_edges
    76   ...   byte-edge table   : [src u32][dst u32][bitmap 32 bytes] * n_byte
         ...   eps-edge table    : [src u32][dst u32]                 * n_eps
         ...   assert-edge table : [src u32][dst u32][code u32]        *
         n_assert
    -- trailer --
    end-4 4    crc32 over all preceding bytes (R47b integrity)

The layout is byte-identical for a fixed ``(pattern, flags, enc)`` because the
automaton is deterministic (canonical state numbering / edge ordering; see
:mod:`pyro.hdl.automaton`), so the mock toolchain stays deterministic (R63b).
"""

from __future__ import annotations

import binascii
import re._constants as _c
import struct

from ..hdl import automaton as _auto

ARTIFACT_MAGIC = b"PYROART1"
FORMAT_VERSION = 1

# Stable serialized zero-width-assertion codes.  These are format-internal (they
# do NOT depend on CPython's ``re._constants`` numeric values), so the C model's
# evaluator can mirror ``pyro._circuit_model._assert_ok`` without any coupling
# to
# a private Python enum.
AC_SOB = 0   # ^ (non-MULTILINE) / \A  -> pos == 0
AC_BOL = 1   # ^ (MULTILINE)           -> pos == 0 or buf[pos-1] == '\n'
AC_EOS = 2   # \Z                      -> pos == n
# $ (non-MULTILINE)       -> pos == n or (pos == n-1 and buf[pos]=='\n')
AC_EOB = 3
AC_EOL = 4   # $ (MULTILINE)           -> pos == n or buf[pos] == '\n'
AC_WB = 5    # \b (bytes mode)         -> ASCII word boundary
AC_NWB = 6   # \B (bytes mode)         -> not an ASCII word boundary
# over-approx / unknown   -> always satisfiable (never under-approx)
AC_TRUE = 7

_HEADER = struct.Struct("<8sI16sIIIIII")   # up to shell_version (offset 0..51)
_DIMS = struct.Struct("<IIIIII")            # n_states..n_assert_edges


def _assert_code(at: int, is_bytes: bool) -> int:
    """Map a resolved ``re`` AT_* opcode + encoding to a stable serialized code.

    Mirrors :func:`pyro._circuit_model._assert_ok` exactly: ``^``/``$`` have
    already been resolved to their LINE variants under MULTILINE at lowering
    time
    (``automaton._resolve_at``); Unicode word boundaries in str mode are
    over-approximated to always-true (the C model re-verifies, R19).
    """
    if at in (_c.AT_BEGINNING, _c.AT_BEGINNING_STRING):
        return AC_SOB
    if at == _c.AT_BEGINNING_LINE:
        return AC_BOL
    if at == _c.AT_END_STRING:
        return AC_EOS
    if at == _c.AT_END:
        return AC_EOB
    if at == _c.AT_END_LINE:
        return AC_EOL
    if at in (_c.AT_BOUNDARY, _c.AT_UNI_BOUNDARY):
        return AC_WB if is_bytes else AC_TRUE
    if at in (_c.AT_NON_BOUNDARY, _c.AT_UNI_NON_BOUNDARY):
        return AC_NWB if is_bytes else AC_TRUE
    return AC_TRUE  # unknown assertion: over-approx true (never under-approx)


def _bitmap(byteset) -> bytes:
    """256-bit little-endian membership bitmap of a byte set (bit b => b in
    set)."""
    bm = bytearray(32)
    for b in byteset:
        bm[b >> 3] |= 1 << (b & 7)
    return bytes(bm)


def serialize_automaton_body(au: _auto.Automaton) -> bytes:
    """Serialize the automaton state/edge tables (the artifact "body").

    Deterministic: edges are emitted in each state's canonical
    :meth:`~pyro.hdl.automaton.Automaton.sorted_edges` order.
    """
    is_bytes = au.enc == _auto.ENC_BYTES
    byte_edges = bytearray()
    eps_edges = bytearray()
    assert_edges = bytearray()
    n_byte = n_eps = n_assert = 0
    for s in range(au.n_states):
        for e in au.sorted_edges(s):
            if e.kind == _auto.E_BYTE:
                byte_edges += struct.pack("<II", s, e.target)
                byte_edges += _bitmap(e.payload)
                n_byte += 1
            elif e.kind == _auto.E_EPS:
                eps_edges += struct.pack("<II", s, e.target)
                n_eps += 1
            elif e.kind == _auto.E_ASSERT:
                code = _assert_code(int(e.payload), is_bytes)
                assert_edges += struct.pack("<III", s, e.target, code)
                n_assert += 1
    body = _DIMS.pack(
    au.n_states,
    au.start,
    au.accept,
    n_byte,
    n_eps,
     n_assert)
    return body + bytes(byte_edges) + bytes(eps_edges) + bytes(assert_edges)


def build_artifact(pattern_hash16: bytes, circ_flags: int, encoding: int,
                   effective_flags: int, generator_version: int,
                   harness_version: int, shell_version: int,
                   body: bytes) -> bytes:
    """Assemble the full ``artifact.bin`` payload from header parts + body.

    ``body`` is the output of :func:`serialize_automaton_body`.  Appends a
    CRC-32
    trailer over all preceding bytes (the R47b integrity check the C model
    performs) and returns the complete payload.
    """
    if len(pattern_hash16) != 16:
        raise ValueError("pattern_hash16 must be exactly 16 bytes")
    head = _HEADER.pack(
        ARTIFACT_MAGIC, FORMAT_VERSION, bytes(pattern_hash16),
        int(circ_flags) & 0xFFFFFFFF, int(encoding) & 0xFFFFFFFF,
        int(effective_flags) & 0xFFFFFFFF, int(generator_version) & 0xFFFFFFFF,
        int(harness_version) & 0xFFFFFFFF, int(shell_version) & 0xFFFFFFFF,
    )
    payload = head + body
    crc = binascii.crc32(payload) & 0xFFFFFFFF
    return payload + struct.pack("<I", crc)
