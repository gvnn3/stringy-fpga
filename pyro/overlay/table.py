"""Overlay table: Aho-Corasick construction, serialization, and TABLE_ID.

Amendment A5 (approved 2026-07-30) opened the loadable-table slot.  This is
the host side of it: rules → an Aho-Corasick automaton → a hardware-shaped
table image → the identity that attests it.

Why Aho-Corasick and not the group NFA
--------------------------------------
SF14's sizing — 21,841 states ≈ 1.33 MB for all 3,896 anchors — is an
AC-trie number, and AC is what fits the memory budget.  AC matches **literal
strings**, so the overlay recognises rule *anchors*, not the AC-S3-2
lowered chains (``(?s:AAAA.{2,12}BBBB)``) that the bitstream circuits can.

**That is a real, deliberate precision trade and it must not be glossed.**
Dropping a chain conjunct only ever *enlarges* the recognised language
(SR3), so completeness is preserved and Snort still re-verifies every
nomination — but the false-nomination rate rises, and the host pays for it.
The overlay buys capacity (all anchors resident) and switch speed (0.54 ms
vs 13.6 s) at the cost of on-chip chain refinement.
:func:`precision_delta` measures exactly what is given up rather than
leaving it as an assertion.

Table layout, and why it is shaped this way
-------------------------------------------
Per state, a 256-bit transition bitmap plus a base index into a dense
next-state array::

    next_state = dense[base + popcount(bitmap & ((1 << b) - 1))]

That is 32 B + 4 B per state plus 4 B per real transition — the
"~64 B/state bitmap-compressed" figure SF14 costed — and bitmap+popcount is
a standard hardware idiom, so the format is one the fabric can walk in a
fixed number of cycles.

Outputs are stored **pre-unioned along the failure chain**.  Walking the
chain at match time would be smaller but variable-latency, which a
1 B/cycle pipeline cannot absorb; paying table bytes to keep the match path
fixed-latency is the right trade for hardware.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

#: Table format version.  Bump ⇒ every TABLE_ID rolls and the device
#: rejects older images at TABLE_BEGIN (A5 §3).
TABLE_FORMAT_VERSION = 1

#: Magic, domain-separating a table image from any other artifact.
TABLE_MAGIC = b"PYROTBL\x00"

#: Header is fixed size so the device can parse it before allocating.
_HDR_FMT = "<8sIIIIIIIIIIII"
HEADER_LEN = struct.calcsize(_HDR_FMT)

#: A state's transition bitmap is 256 bits = 32 bytes.
BITMAP_BYTES = 32


class TableStats(NamedTuple):
    n_states: int
    n_patterns: int
    n_transitions: int
    n_outputs: int
    image_bytes: int


class AhoCorasick:
    """Literal multi-pattern automaton over bytes.

    Patterns are indexed by position in the input sequence; that index is
    the ``pattern_id`` the device reports and the sidecar resolves, exactly
    as slot index is ``pattern_id`` for the bitstream groups (SR7).
    """

    __slots__ = ("patterns", "goto", "fail", "out")

    def __init__(self, patterns: Sequence[Optional[bytes]]):
        self.patterns: List[Optional[bytes]] = list(patterns)
        # goto[state] = {byte: next_state}; state 0 is the root.
        self.goto: List[Dict[int, int]] = [{}]
        self.fail: List[int] = [0]
        self.out: List[set] = [set()]
        self._build_goto()
        self._build_fail()

    def _new_state(self) -> int:
        self.goto.append({})
        self.fail.append(0)
        self.out.append(set())
        return len(self.goto) - 1

    def _build_goto(self) -> None:
        for pid, pat in enumerate(self.patterns):
            if not pat:          # None or b"" — a tombstone keeps its id
                continue
            s = 0
            for b in pat:
                nxt = self.goto[s].get(b)
                if nxt is None:
                    nxt = self._new_state()
                    self.goto[s][b] = nxt
                s = nxt
            self.out[s].add(pid)

    def _build_fail(self) -> None:
        """BFS failure links, then pre-union outputs along the chain."""
        from collections import deque
        q = deque()
        for b, s in self.goto[0].items():
            self.fail[s] = 0
            q.append(s)
        while q:
            r = q.popleft()
            for b, s in self.goto[r].items():
                q.append(s)
                f = self.fail[r]
                while f and b not in self.goto[f]:
                    f = self.fail[f]
                self.fail[s] = self.goto[f].get(
    b, 0) if f or b in self.goto[0] else 0
                if self.fail[s] == s:
                    self.fail[s] = 0
                # Pre-union: the match path must be fixed-latency.
                self.out[s] |= self.out[self.fail[s]]

    @property
    def n_states(self) -> int:
        return len(self.goto)

    def next_state(self, state: int, b: int) -> int:
        """Transition with failure fallback — the reference semantics the
        RTL must reproduce byte for byte."""
        s = state
        while True:
            nxt = self.goto[s].get(b)
            if nxt is not None:
                return nxt
            if s == 0:
                return 0
            s = self.fail[s]


def build(patterns: Sequence[Optional[bytes]]) -> AhoCorasick:
    return AhoCorasick(patterns)


# --------------------------------------------------------------------------
# Case handling: two automata, not a branching trie
# --------------------------------------------------------------------------
def ascii_fold(data: bytes) -> bytes:
    """R15 ASCII fold — A-Z to a-z, nothing else.

    ONLY ASCII.  A Unicode-aware fold is what shipped a match-everything
    circuit in S1 (docs/notebook.md, 2026-07-27); the fold set here is the
    exact 2-byte one the fabric implements.
    """
    return bytes(b + 0x20 if 0x41 <= b <= 0x5A else b for b in data)


class CaseSplitTable:
    """Two automata: nocase patterns over folded input, exact ones over raw.

    Three designs were possible and two are wrong.  Branching the trie on
    both cases is **exponential** — a 20-letter nocase anchor becomes 2^20
    states (measured: the build did not terminate).  Merging both cases onto
    shared states is linear but **over-approximates**, letting a
    case-sensitive pattern match mixed-case input.  Splitting into two
    automata is linear *and* exact, at the cost of scanning twice — which
    in hardware is two engines or two passes, and in either case is the
    honest price of getting case right.
    """

    __slots__ = ("ci", "cs", "ci_ids", "cs_ids", "n_patterns")

    def __init__(self, patterns: Sequence[Optional[bytes]],
                 nocase: Sequence[bool]):
        if len(patterns) != len(nocase):
            raise ValueError("nocase flags must match pattern count")
        self.n_patterns = len(patterns)
        ci_pats, cs_pats = [], []
        self.ci_ids, self.cs_ids = [], []
        for pid, (p, ci) in enumerate(zip(patterns, nocase)):
            if not p:
                continue
            if ci:
                ci_pats.append(ascii_fold(p))
                self.ci_ids.append(pid)
            else:
                cs_pats.append(p)
                self.cs_ids.append(pid)
        self.ci = AhoCorasick(ci_pats)
        self.cs = AhoCorasick(cs_pats)

    @property
    def n_states(self) -> int:
        return self.ci.n_states + self.cs.n_states

    def scan(self, data: bytes):
        """(pattern_id, end) for every match, exactly."""
        out = []
        for ac, ids, subject in ((self.cs, self.cs_ids, data),
                                 (self.ci, self.ci_ids, ascii_fold(data))):
            st = 0
            for i, b in enumerate(subject):
                st = ac.next_state(st, b)
                for local in ac.out[st]:
                    out.append((ids[local], i + 1))
        return out


def build_for_group(group) -> CaseSplitTable:
    """Case-correct table for an SR6 group's slots.

    A slot stores its anchor case-FOLDED when nocase, so building without
    the flag matches only the lowercase form and silently misses mixed-case
    traffic — a completeness defect the AC-S2-3 corpus catches immediately
    (it did: 4 violations on first run).
    """
    return CaseSplitTable(
        [None if s.tombstone else s.anchor for s in group.slots],
        [bool(s.nocase) for s in group.slots])


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------
def serialize(ac: AhoCorasick, engine_id: int = 0) -> bytes:
    """The wire/table image the device receives.

    Deterministic: the same automaton always serializes to the same bytes,
    because TABLE_ID is a hash of this image and a nondeterministic layout
    would make identity meaningless.
    """
    n = ac.n_states
    bitmaps = bytearray()
    bases = bytearray()
    dense: List[int] = []
    for s in range(n):
        trans = ac.goto[s]
        bits = 0
        for b in sorted(trans):
            bits |= 1 << b
        bitmaps += bits.to_bytes(BITMAP_BYTES, "little")
        bases += struct.pack("<I", len(dense))
        for b in sorted(trans):
            dense.append(trans[b])
    dense_b = b"".join(struct.pack("<I", v) for v in dense)
    fail_b = b"".join(struct.pack("<I", f) for f in ac.fail)

    # Outputs: per-state (offset, count) into a flat pattern-id array.
    out_index = bytearray()
    out_flat: List[int] = []
    for s in range(n):
        ids = sorted(ac.out[s])
        out_index += struct.pack("<II", len(out_flat), len(ids))
        out_flat.extend(ids)
    out_flat_b = b"".join(struct.pack("<I", v) for v in out_flat)

    sections = [bytes(bitmaps), bytes(bases), dense_b, fail_b,
                bytes(out_index), out_flat_b]
    offsets, cur = [], HEADER_LEN
    for sec in sections:
        offsets.append((cur, len(sec)))
        cur += len(sec)

    hdr = struct.pack(
        _HDR_FMT, TABLE_MAGIC, TABLE_FORMAT_VERSION, int(engine_id),
        n, len(ac.patterns),
        offsets[0][0], offsets[1][0], offsets[2][0],
        offsets[3][0], offsets[4][0], offsets[5][0],
        len(dense), len(out_flat))
    return hdr + b"".join(sections)


def stats(ac: AhoCorasick, image: bytes) -> TableStats:
    return TableStats(
        n_states=ac.n_states,
        n_patterns=len(ac.patterns),
        n_transitions=sum(len(g) for g in ac.goto),
        n_outputs=sum(len(o) for o in ac.out),
        image_bytes=len(image))


# --------------------------------------------------------------------------
# Identity (A5 §2)
# --------------------------------------------------------------------------
_CRC32C_POLY = 0x82F63B78
_CRC32C_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ (_CRC32C_POLY if _c & 1 else 0)
    _CRC32C_TABLE.append(_c)


def crc32c(data: bytes, crc: int = 0xFFFFFFFF) -> int:
    """CRC-32C (Castagnoli), byte-reflected — bit-identical to the fabric's
    per-byte loop in ``hw/rtl/pyro_overlay_engine.v``.

    Python's ``zlib.crc32`` is the IEEE polynomial, NOT Castagnoli; using it
    here silently disagreed with the RTL and the xsim differential caught
    it on the first run (device 0xf2edc7f8 vs host 0xe4924a78).
    """
    for b in data:
        crc = (crc >> 8) ^ _CRC32C_TABLE[(crc ^ b) & 0xFF]
    return crc & 0xFFFFFFFF


def table_id(image: bytes) -> int:
    """Device-computable TABLE_ID: CRC-32C over the image.

    Integrity-grade, matching what the fabric can compute cheaply and what
    SR14′ needs (catching torn, wrong or truncated writes).  Forced
    non-zero, because zero is reserved for "no valid table" — the same
    convention ``rp_child_id`` already uses.
    """
    v = (~crc32c(image)) & 0xFFFFFFFF     # final inversion, as the RTL does
    return v if v else 1


def strong_id(image: bytes) -> bytes:
    """Host-side identity of record: 128-bit domain-separated hash.

    The device cannot compute this; it is what ties an image to a rule set
    in the manifest, in the same discipline R47a uses for bitstreams.
    """
    h = hashlib.sha256()
    h.update(b"PYRO-TABLE\x00")
    h.update(struct.pack("<I", TABLE_FORMAT_VERSION))
    h.update(struct.pack("<Q", len(image)))
    h.update(image)
    return h.digest()[:16]


def manifest(image: bytes, ac: AhoCorasick, engine_id: int,
             rule_keys: Optional[Sequence[Sequence[str]]] = None) -> dict:
    """The auditable chain: rules → image → identity (A5 §2.1)."""
    st = stats(ac, image)
    return {
        "table_format_version": TABLE_FORMAT_VERSION,
        "engine_id": int(engine_id),
        "table_id": "0x%08x" % table_id(image),
        "strong_id": strong_id(image).hex(),
        "n_states": st.n_states,
        "n_patterns": st.n_patterns,
        "n_transitions": st.n_transitions,
        "n_outputs": st.n_outputs,
        "image_bytes": st.image_bytes,
        "bytes_per_state": round(st.image_bytes / max(1, st.n_states), 1),
        # pattern_id -> the gid:sid list it nominates, the SR7 sidecar role
        "sidecar": {str(i): list(k) for i, k in enumerate(rule_keys or [])},
    }


# --------------------------------------------------------------------------
# What the overlay gives up, measured rather than asserted
# --------------------------------------------------------------------------
def precision_delta(group, subject: bytes) -> dict:
    """Nominations from anchor-only AC vs the group's lowered circuits.

    The overlay matches literal anchors; the bitstream circuits match
    AC-S3-2 lowered chains, which are strictly more selective.  Both are
    sound (SR3: dropping conjuncts only enlarges the language), so this
    measures **extra host re-verification**, never missed detections — and
    the assertion that ``ac ⊇ lowered`` is checked here, not assumed.
    """
    import re as _re
    tbl = build_for_group(group)
    ac_hits = {pid for pid, _end in tbl.scan(subject)}

    low_hits = set()
    for s in group.slots:
        if s.tombstone:
            continue
        if _re.compile(s.pattern_eff, s.flags_eff).search(subject):
            low_hits.add(s.index)

    return {
        "ac_nominations": len(ac_hits),
        "lowered_nominations": len(low_hits),
        "extra": len(ac_hits - low_hits),
        "missed": len(low_hits - ac_hits),      # MUST be 0 (soundness)
        "sound": not (low_hits - ac_hits),
    }
