"""Circuit identity (spec §7.4, R47a / R47b).

The generated circuit bakes a **cryptographic pattern hash** (>= 128-bit) of
``(pattern_bytes, flags, generator_version, harness_version)`` into its
``CIRC_ID0..3`` registers.  The host runtime reads this identity block before
dispatching a scan and refuses to dispatch to a circuit whose baked identity
does not match the pattern it intends to run (R47a, the trust boundary).

Determinism: the hash is SHA-256 (truncated to 128 bits) over a canonical
encoding, so the same inputs always yield the same identity.

Assumption (flagged for spec §13).  R47a lists the hashed tuple as
``(pattern_bytes, flags, generator_version, harness_version)`` but does not name
the subject **encoding mode** (bytes vs. UTF-8) as a component.  A ``str``
pattern and a ``bytes`` pattern can encode to identical ``pattern_bytes`` while
targeting different byte automata, so we additionally fold a one-byte encoding
tag into the canonical pre-image to keep identities distinct.  This is a
superset of the spec's key; see the Task-5 report.
"""

from __future__ import annotations

import hashlib
import re as _re
import re._parser as _sre
from typing import Tuple

# packed CIRC_FLAGS: low16 = baked re flags, hi16 = NUM_PAT (R45 0x0028).
_PUBLIC_FLAG_MASK = 0xFFFF

# A *global* inline-flag group at the very start of a pattern, e.g. ``(?i)``,
# ``(?ims)`` — flags only (no ``:`` scoped body, no ``-`` clearing form).  These
# are pure syntax for the effective flag set and MUST be canonicalized away so
# that ``(?i)abc`` and ``abc`` + ``re.I`` hash identically (R47a v2.0.1).  Scoped
# groups ``(?i:...)`` and clearing groups ``(?-i:...)`` are semantic and stay.
_GLOBAL_FLAGS_STR = _re.compile(r"^(?:\(\?[aiLmsux]+\))+")
_GLOBAL_FLAGS_BYTES = _re.compile(rb"^(?:\(\?[aiLmsux]+\))+")


def _strip_global_flags(pattern):
    """Strip leading global inline-flag groups from a str/bytes pattern."""
    if isinstance(pattern, str):
        return _GLOBAL_FLAGS_STR.sub("", pattern)
    pb = bytes(pattern)
    return _GLOBAL_FLAGS_BYTES.sub(b"", pb)


def canonical_pattern_bytes(pattern) -> Tuple[bytes, int]:
    """Return ``(pattern_bytes, enc_tag)`` for hashing / keying (R47a).

    ``pattern_bytes`` is the pattern source **after** leading global inline-flag
    extraction (those flags are folded into the effective flags instead), and the
    ``str`` pattern's UTF-8 encoding.  ``enc_tag`` is the R38 encoding
    discriminator (0=ENC_BYTES, 1=ENC_UTF8).
    """
    stripped = _strip_global_flags(pattern)
    if isinstance(stripped, str):
        return stripped.encode("utf-8"), 1  # ENC_UTF8
    return stripped, 0                       # ENC_BYTES


def effective_flags(pattern, flags: int) -> int:
    """The canonicalized, post-inline-extraction effective flags (R47a).

    Equals what CPython applies: inline global/scoped flags folded in, ``re.U``
    normalized to its (str) form, as a stable canonical integer.  Delegates to
    the CPython parser so it matches ``re`` exactly.
    """
    return int(_sre.parse(pattern, int(flags)).state.flags)


def pattern_hash(pattern, flags: int, generator_version: int,
                 harness_version: int) -> bytes:
    """The 16-byte (128-bit) pattern hash baked into ``CIRC_ID0..3`` (R47a).

    ``flags`` MUST be the effective (canonicalized) flags (see
    :func:`effective_flags`); callers on the generator path pass the automaton's
    resolved ``flags``.  Semantically identical patterns therefore hash equal.
    """
    pb, enc_tag = canonical_pattern_bytes(pattern)
    h = hashlib.sha256()
    h.update(b"PYRO\x00")                       # domain separation
    h.update(bytes([enc_tag]))
    h.update(int(flags).to_bytes(4, "little"))
    h.update(int(generator_version).to_bytes(4, "little"))
    h.update(int(harness_version).to_bytes(4, "little"))
    h.update(len(pb).to_bytes(8, "little"))     # length-prefix (unambiguous)
    h.update(pb)
    return h.digest()[:16]


def descriptor_key(pattern, flags: int, generator_version: int) -> tuple:
    """The canonical R4 host-classification / bitstream cache key prefix.

    ``(pattern_bytes, encoding, effective_flags, generator_version)`` per R4 /
    R47b, with ``pattern_bytes`` and ``effective_flags`` canonicalized so two
    semantically identical patterns (e.g. ``(?i)abc`` and ``abc`` + ``re.I``)
    share one key — and therefore one synthesized circuit (avoids double
    synthesis).  The bitstream cache appends toolchain/shell versions (R47b).
    """
    pb, enc_tag = canonical_pattern_bytes(pattern)
    return (pb, enc_tag, effective_flags(pattern, flags), int(generator_version))


def circ_id_words(digest16: bytes) -> Tuple[int, int, int, int]:
    """Split the 128-bit hash into the four little-endian ``CIRC_ID*`` words."""
    return tuple(
        int.from_bytes(digest16[i:i + 4], "little") for i in range(0, 16, 4)
    )  # type: ignore[return-value]


def circ_flags_word(flags: int, num_patterns: int) -> int:
    """Pack ``CIRC_FLAGS`` (R45 0x0028): low16 flags, hi16 NUM_PAT."""
    return (int(flags) & _PUBLIC_FLAG_MASK) | ((int(num_patterns) & 0xFFFF) << 16)
