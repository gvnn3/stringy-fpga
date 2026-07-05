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
from typing import Tuple

# packed CIRC_FLAGS: low16 = baked re flags, hi16 = NUM_PAT (R45 0x0028).
_PUBLIC_FLAG_MASK = 0xFFFF


def canonical_pattern_bytes(pattern) -> Tuple[bytes, int]:
    """Return ``(pattern_bytes, enc_tag)`` for hashing (see module note)."""
    if isinstance(pattern, str):
        return pattern.encode("utf-8"), 1  # ENC_UTF8
    return bytes(pattern), 0               # ENC_BYTES


def pattern_hash(pattern, flags: int, generator_version: int,
                 harness_version: int) -> bytes:
    """The 16-byte (128-bit) pattern hash baked into ``CIRC_ID0..3``."""
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


def circ_id_words(digest16: bytes) -> Tuple[int, int, int, int]:
    """Split the 128-bit hash into the four little-endian ``CIRC_ID*`` words."""
    return tuple(
        int.from_bytes(digest16[i:i + 4], "little") for i in range(0, 16, 4)
    )  # type: ignore[return-value]


def circ_flags_word(flags: int, num_patterns: int) -> int:
    """Pack ``CIRC_FLAGS`` (R45 0x0028): low16 flags, hi16 NUM_PAT."""
    return (int(flags) & _PUBLIC_FLAG_MASK) | ((int(num_patterns) & 0xFFFF) << 16)
