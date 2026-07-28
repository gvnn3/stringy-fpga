"""SR6 rule grouping: pack triaged rules into pattern-set groups.

Implements spec ``snort-rule-offload`` §4.3 SR6 (stable grouping) plus the
identity/keying obligations SR7 (``rp_child_id`` = low-32 of the group hash)
and SR9 (bitstream-cache key = canonical group bytes ⊕ versions), reusing
:mod:`pyro.hdl.identity`'s hashing discipline verbatim.

Grouping (SR6)
--------------
Rules are keyed by **destination-port class** first (SF8: ``$HTTP_PORTS``,
``$ORACLE_PORTS``, literal-port families, ``any``), then packed in **stable
sid order** to ``GROUP_MAX = 256`` *rules* per group (SF6 ``MAX_PATTERNS``
caps *slots*, and slots ≤ rules because anchors dedup).  Only
``anchor-compilable`` rules (SR2) with a parseable ``sid`` are packed:
``header-only`` rules are decided host-side (SR13) and ``always-forward``
rules have no circuit at all (SR2), so neither belongs in a group.

Slots vs. rules — the dedup fact
--------------------------------
A **slot** is one automaton in the group circuit; ``pattern_id`` (PYRO R47's
4-byte field) is its index (SR7).  Anchors are deduplicated on
``(decoded literal after case-fold, nocase flag)`` — exactly
:attr:`pyro.snort.triage.Anchor.dedup_key` — so **one slot can serve several
rules** and the sidecar maps ``slot -> LIST of gid:sid``.  Measured on the
AC-S2-2 group: 256 rules → 253 distinct anchors (``/view-source``,
``/webspirs.cgi``, ``/fpcount.exe`` each appear twice).  The daemon MUST
nominate *every* rule on a hit slot; collapsing the list to one sid would be
an SR3 completeness defect.

A ``nocase`` slot stores the ASCII-lowercased literal, because that is the
literal the R15 byte-fold matcher actually recognizes; ``content:"ABC",nocase``
and ``content:"abc",nocase`` are therefore the *same* slot, while
``content:"abc"`` (case-sensitive) is a different one.

Tombstones (SR6)
----------------
Deleted sids leave **tombstones**: :func:`repack_with_tombstones` keeps every
surviving rule at its existing ``(group, slot)`` and empties — never reuses,
never renumbers — the slot of a deleted one, so a ruleset diff dirties the
minimum number of groups (SR9).  A tombstoned slot keeps its index and is
serialized as an empty slot, so ``pattern_id`` never shifts under the sidecar.

Identity and keys
-----------------
:meth:`RuleGroup.canonical_bytes` is the group's canonical byte serialization:
deterministic, length-prefixed, unambiguous, and a pure function of *rule
content*.  **Deployment variables never enter it (SR13).**  The distinction
this module draws, and it is load-bearing: the port-class **token**
(``$HTTP_PORTS``) is rule text and IS hashed; the token's **value** (the site's
port list) is configuration, lives in the daemon's variable table, and is
never seen here.  Two sites with different ``$HTTP_PORTS`` values therefore
share one group hash, one cache key, and one bitstream.

What else is hashed, and why:

* the port-class token — two groups with identical anchors but different port
  classes carry *different sidecars*; if they hashed equal, the SR14 identity
  check (``rp_child_id`` == expected) would pass for the wrong group and
  nominations would be misattributed;
* the ``gid:sid`` list of every slot — the sidecar is what turns a
  ``pattern_id`` into a nomination, so it must be pinned by the same identity
  the SR14 check verifies.  The cost is a rebuild of a byte-identical
  bitstream when only sids move; the alternative is silent misattribution.

Not hashed: tier/sub-tier/buffer (derived deterministically from the same rule
text), rule ``rev``/``msg``, line numbers, and anything site-specific.
"""

from __future__ import annotations

import hashlib
import re as _stdre
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from . import lowering as _lowering
from . import triage as _triage
from .rules import Rule
from .triage import TriageResult

# Stock compiler captured at ``import pyro`` time (pyro/__init__ imports
# pyro._model before install() can patch stdlib ``re``).  This module can be
# imported LAZILY while pyro.install() is active, so a module-level
# ``re.compile`` here would capture the *patched* compiler and route this
# module's own regexes through the dispatch layer — spurious R66 stats /
# reuse-counter ticks (the AC-3-1 counter-wedge review; same reasoning as
# pyro/hdl/identity.py).  ``re.escape`` is not affected and is used directly.
from .._model import _stock_compile

#: SR6 packing bound: rules per group.  Slots per group are ≤ this by
#: construction (dedup), which keeps the circuit inside ``MAX_PATTERNS = 256``
#: (SF6, ``pyro/_classify.py``).  Raising it requires measured post-route
#: utilization and a spec amendment (SR6/SR8).
GROUP_MAX = 256

#: Canonical-serialization format version.  Bump ⇒ every group hash and every
#: SR9 cache key rolls over, so it is versioned explicitly rather than
#: implicitly through code changes.
#: v2 (AC-S3-2): slots carry the SR3 lowered pattern (content chains,
#: ``offset/depth/distance/within`` windows, fused clean pcre), its flags
#: word, and its SR12 ``tail_span`` — all serialized, so two groups that
#: differ only in lowering can never hash equal.
GROUP_FORMAT_VERSION = 2

#: Domain-separation tag for the canonical bytes and the group hash — keeps a
#: group key from ever colliding with a PYRO single-pattern key (whose
#: pre-image is a bare regex source, R47a).
_GROUP_MAGIC = b"PYROGRP\x00"

# The group's subject encoding is always bytes: Snort ``content`` is bytes and
# the S1 silicon lesson (docs/notebook.md, 2026-07-27 evening) is that a
# str-mode ``nocase`` lowering silently over-approximates to "any code point".
ENC_BYTES = 0


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
class RuleRef(NamedTuple):
    """One rule attached to a slot — the sidecar row (spec §2).

    ``dropped``/``oa`` are the SR4 per-rule dropped-conjunct list and
    over-approximation classes; manifest data only, **never hashed**
    (canonical serialization takes ``gid``/``sid`` alone).
    """

    gid: int
    sid: int
    subtier: Optional[str]   # SR2: raw-anchor | normalized-buffer
    buffer: str              # sticky buffer the anchor lives in (SF11)
    line_no: int             # provenance only; never hashed
    dropped: Tuple[str, ...] = ()   # SR4 dropped-conjunct option keys
    oa: Tuple[str, ...] = ()        # SR4 over-approximation classes

    @property
    def key(self) -> str:
        """``gid:sid`` — the nomination identity (spec §2)."""
        return "%d:%d" % (self.gid, self.sid)


class GroupSlot(NamedTuple):
    """One automaton of the group circuit; ``index`` is R47's ``pattern_id``.

    v2 (AC-S3-2): a slot's matcher is its SR3 **lowered pattern** — an
    anchor-only literal for most rules, a content chain with bounded gap
    windows (± a fused clean pcre) where SR3 admits one.  The trailing
    fields default to "derive from the anchor" so an anchor-only slot can
    still be constructed positionally exactly as in v1; the effective
    values (:attr:`pattern_eff`/:attr:`flags_eff`/:attr:`tail_span_eff`)
    are what the emitter, the model, and the canonical serialization use.
    """

    index: int
    anchor: bytes            # decoded literal; ASCII-lowercased iff ``nocase``
    nocase: bool             # R15 ASCII byte-fold, lowered in BYTES mode
    rules: Tuple[RuleRef, ...]
    pattern: bytes = b""     # SR3 lowered regex source (b"" = derive)
    flags: int = -1          # slot re-flags word (-1 = derive from nocase)
    tail_span: int = -1      # SR12 tail contribution (-1 = derive: len(anchor))

    @property
    def tombstone(self) -> bool:
        """True once every rule that claimed this slot has been deleted (SR6)."""
        return not self.rules

    @property
    def length(self) -> int:
        return len(self.anchor)

    @property
    def pattern_eff(self) -> bytes:
        """The slot's matcher source: the lowered pattern, else the
        S1/S2-proven anchor escape."""
        return self.pattern if self.pattern else _stdre.escape(self.anchor)

    @property
    def flags_eff(self) -> int:
        if self.flags >= 0:
            return self.flags
        return _stdre.IGNORECASE if self.nocase else 0

    @property
    def tail_span_eff(self) -> int:
        """SR12 tail contribution: the max bytes a floating match spans
        (0 for a ``\\A``-anchored slot — it can only match at buffer
        start, which chunk 0 contains whole)."""
        return self.tail_span if self.tail_span >= 0 else len(self.anchor)


class RuleGroup(NamedTuple):
    """An ordered set of ≤ ``GROUP_MAX`` rules compiled into one circuit."""

    port_class: str          # normalized dst-port token (SF8) — the SR6 key
    index: int               # 0-based ordinal within the port class
    slots: Tuple[GroupSlot, ...]
    group_max: int = GROUP_MAX

    # -- shape -----------------------------------------------------------
    @property
    def name(self) -> str:
        return "%s/%d" % (self.port_class, self.index)

    @property
    def n_slots(self) -> int:
        return len(self.slots)

    @property
    def rule_count(self) -> int:
        return sum(len(s.rules) for s in self.slots)

    @property
    def max_anchor_len(self) -> int:
        """Longest live anchor literal (reporting; SF10 lineage)."""
        live = [s.length for s in self.slots if not s.tombstone]
        return max(live) if live else 0

    @property
    def max_tail_span(self) -> int:
        """Longest floating lowered-match span over live slots — the SR12
        quantity the overlap tail must cover.  Equal to ``max_anchor_len``
        for a purely anchor-lowered group; larger when chains are lowered
        (AC-S3-2), zero-contribution from ``\\A``-anchored slots."""
        live = [s.tail_span_eff for s in self.slots if not s.tombstone]
        return max(live) if live else 0

    @property
    def overlap_tail(self) -> int:
        """SR12 per-flow overlap tail: ``max floating span - 1`` bytes."""
        return max(0, self.max_tail_span - 1)

    def sidecar(self) -> Dict[int, Tuple[str, ...]]:
        """``pattern_id -> (gid:sid, ...)`` (spec §2 sidecar table).

        A LIST per slot, not a scalar: anchors dedup, so a hit on one slot
        nominates every rule that shares that anchor.
        """
        return {s.index: tuple(r.key for r in s.rules) for s in self.slots}

    # -- canonical serialization / identity (SR7, SR9, SR13) --------------
    def canonical_bytes(self) -> bytes:
        """Deterministic, unambiguous byte serialization of the group.

        Pure function of rule content (see the module docstring for exactly
        what is and is not included, and why).  Every variable-length field is
        length-prefixed, so no two distinct groups can serialize equal.
        """
        out = bytearray()
        out += _GROUP_MAGIC
        out += _u32(GROUP_FORMAT_VERSION)
        out += _u32(self.group_max)
        pc = self.port_class.encode("utf-8")
        out += _u32(len(pc)) + pc
        out += _u32(self.index)
        out += _u32(len(self.slots))
        for slot in self.slots:
            out += _u32(slot.index)
            flags = (0x1 if slot.nocase else 0) | (0x2 if slot.tombstone else 0)
            out += bytes([flags])
            out += _u32(len(slot.anchor)) + slot.anchor
            # v2 (AC-S3-2): the SR3 lowered matcher is identity-bearing.
            # EFFECTIVE values are serialized, so a legacy-constructed
            # anchor slot and its explicit equivalent hash identically.
            out += _u32(slot.flags_eff)
            pat = slot.pattern_eff
            out += _u32(len(pat)) + pat
            out += _u32(slot.tail_span_eff)
            # Sorted so slot identity does not depend on rule arrival order.
            refs = sorted((r.gid, r.sid) for r in slot.rules)
            out += _u32(len(refs))
            for gid, sid in refs:
                out += _u32(gid) + _u32(sid)
        return bytes(out)

    def group_hash(self, generator_version: int, harness_version: int,
                   datapath_bytes: int = 1) -> bytes:
        """The 16-byte group hash (SR7) — :func:`group_hash` on this group."""
        return group_hash(self, generator_version, harness_version,
                          datapath_bytes)

    def rp_child_id(self, generator_version: int, harness_version: int,
                    datapath_bytes: int = 1) -> int:
        """SR7/R78.5a child id: low-32 of the group hash, forced non-zero."""
        from ..hdl.rp_wrapper import rp_child_id_from_hash
        digest = self.group_hash(generator_version, harness_version,
                                 datapath_bytes)
        return rp_child_id_from_hash(digest.hex())

    def descriptor_key(self, generator_version: int, harness_version: int,
                       datapath_bytes: int = 1) -> tuple:
        """R4/R47a descriptor key with the canonical group bytes standing in
        for ``pattern_bytes`` (SR9).

        Shape is exactly :func:`pyro.hdl.identity.descriptor_key`'s —
        ``(bytes, encoding, flags, generator_version)`` — so
        :func:`pyro.synth.cache.make_key` consumes it unchanged.  There are no
        regex flags at group level (``nocase`` is per-slot, inside the
        canonical bytes), so the flags component is **repurposed** to carry the
        two other build inputs that change the artifact:

        * the datapath width in the low 32 bits — ``0`` at
          ``datapath_bytes == 1`` (byte-identical to a no-flags key, no
          rollover for the default build), the width itself above that, so a
          dpb=1 and a dpb=8 build of the same group stay on distinct keys; and
        * ``harness_version`` in the high 32 bits.  It belongs here because
          :meth:`group_hash` mixes it in, so it is baked into ``CIRC_ID0..3``
          and therefore into :meth:`rp_child_id` — the value the daemon
          verifies at load time (SR14).  Without it a harness-only version bump
          would be served a *cached* bitstream whose ``rp_child_id`` no longer
          matches what the host computes, and the identity check would fail on
          a build the cache believes is correct.  ``key_digest`` widens each
          component to 8 bytes, so the packed value is lossless.
        """
        flags = ((0 if int(datapath_bytes) == 1 else int(datapath_bytes))
                 | (int(harness_version) << 32))
        return (self.canonical_bytes(), ENC_BYTES, flags,
                int(generator_version))

    def bitstream_key(self, generator_version: int, harness_version: int,
                      toolchain_version: int, shell_version: int,
                      datapath_bytes: int = 1) -> tuple:
        """Full SR9 / R4/R47b bitstream-cache key for this group."""
        from ..synth.cache import make_key
        return make_key(
            self.descriptor_key(generator_version, harness_version,
                                datapath_bytes),
            toolchain_version, shell_version)

    # -- manifest --------------------------------------------------------
    def manifest(self, generator_version: int, harness_version: int,
                 datapath_bytes: int = 1) -> dict:
        """Host-side group manifest (SR4/SR7/SR12) — JSON-ready."""
        digest = self.group_hash(generator_version, harness_version,
                                 datapath_bytes)
        return {
            "port_class": self.port_class,
            "group_index": self.index,
            "name": self.name,
            "group_max": self.group_max,
            "format_version": GROUP_FORMAT_VERSION,
            "generator_version": int(generator_version),
            "harness_version": int(harness_version),
            "datapath_bytes": int(datapath_bytes),
            "n_slots": self.n_slots,
            "rule_count": self.rule_count,
            "group_hash": digest.hex(),
            "rp_child_id": self.rp_child_id(generator_version,
                                            harness_version, datapath_bytes),
            "max_anchor_len": self.max_anchor_len,
            "max_tail_span": self.max_tail_span,
            "overlap_tail": self.overlap_tail,   # SR12
            # SR4: group-level over-approximation classes = union of the
            # per-rule contributions; an exact circuit declares the empty
            # set (never true for a chunked prefilter — chunk_overlap).
            "over_approx_classes": sorted(
                {c for s in self.slots for r in s.rules for c in r.oa}),
            "slots": [
                {
                    "pattern_id": s.index,
                    "anchor_hex": s.anchor.hex(),
                    "anchor_len": s.length,
                    "nocase": s.nocase,
                    "tombstone": s.tombstone,
                    "pattern_hex": s.pattern_eff.hex() if not s.tombstone
                                   else "",
                    "slot_flags": s.flags_eff,
                    "tail_span": s.tail_span_eff,
                    "rules": [
                        {"gid": r.gid, "sid": r.sid, "key": r.key,
                         "subtier": r.subtier, "buffer": r.buffer,
                         # SR4 per-rule dropped-conjunct list + classes
                         "dropped": list(r.dropped),
                         "over_approx": list(r.oa)}
                        for r in s.rules
                    ],
                }
                for s in self.slots
            ],
        }


def _u32(v: int) -> bytes:
    return int(v).to_bytes(4, "little")


# --------------------------------------------------------------------------
# Identity (mirrors pyro.hdl.identity.pattern_hash discipline)
# --------------------------------------------------------------------------
def group_hash(group: RuleGroup, generator_version: int,
               harness_version: int, datapath_bytes: int = 1) -> bytes:
    """The 16-byte (128-bit) group hash baked into ``CIRC_ID0..3`` (SR7).

    Same construction as :func:`pyro.hdl.identity.pattern_hash` — SHA-256 over
    a domain-separated, length-prefixed canonical pre-image, truncated to 128
    bits — with the canonical group bytes standing in for ``pattern_bytes``
    and a group-specific domain tag.  ``datapath_bytes`` is mixed in only when
    it is not 1, exactly as P2b does for single patterns, so the default build
    hashes stably.
    """
    h = hashlib.sha256()
    h.update(_GROUP_MAGIC)                       # domain separation
    h.update(bytes([ENC_BYTES]))
    h.update(int(generator_version).to_bytes(4, "little"))
    h.update(int(harness_version).to_bytes(4, "little"))
    if int(datapath_bytes) != 1:
        h.update(b"DPB\x00")                     # domain separation (P2b)
        h.update(int(datapath_bytes).to_bytes(4, "little"))
    cb = group.canonical_bytes()
    h.update(len(cb).to_bytes(8, "little"))      # length-prefix
    h.update(cb)
    return h.digest()[:16]


# --------------------------------------------------------------------------
# Slot lowering (the S1 case-fold lesson, pinned once)
# --------------------------------------------------------------------------
def slot_pattern(slot: GroupSlot) -> Tuple[bytes, int]:
    """``(pattern, flags)`` for one slot — **always a bytes pattern**.

    Snort ``content`` is bytes.  Lowering a ``nocase`` anchor as a *str*
    pattern with ``re.IGNORECASE`` takes ``automaton.py``'s
    ``OA_CROSS_LENGTH_CASEFOLD`` path (Unicode simple folds can cross UTF-8
    lengths) and produces a **match-everything** circuit that still passes any
    completeness-only test — the exact defect that failed AC-S1-2's negative
    corpus on silicon (docs/notebook.md, 2026-07-27 evening).  Bytes mode
    gives the exact 2-byte ASCII fold set (R15).  Every group lowering path —
    emitter, model, oracle — MUST go through this function.

    v2 (AC-S3-2): the slot's matcher is its SR3 lowered pattern when one
    was stored (content chain / ``\\A`` prefix / fused pcre — built by
    :mod:`pyro.snort.lowering`, whose case folds are scoped ``(?i:...)``
    groups in bytes mode, the same R15 fold path); an anchor-only slot
    derives the S1/S2-proven escape exactly as before.
    """
    return slot.pattern_eff, slot.flags_eff


def slot_automaton(slot: GroupSlot):
    """Build the byte automaton for one slot (None for a tombstone)."""
    if slot.tombstone:
        return None
    from ..hdl import automaton as _auto
    pattern, flags = slot_pattern(slot)
    return _auto.build(pattern, flags, _auto.ENC_BYTES)


def group_circuit(group: RuleGroup, datapath_bytes: int = 1):
    """Compile a :class:`RuleGroup` to its SR7 pattern-set circuit.

    The ONE supported way to get RTL for a group.  It exists so no caller has
    to re-derive the three things that are easy to get wrong and impossible to
    notice afterwards:

    1. **the identity.**  ``pyro.hdl.generator.generate_group`` falls back to
       hashing the pattern list when no ``group_hash`` is passed — that hash
       cannot see the sidecar or the port class, so two groups with identical
       anchors and different ``gid:sid`` lists would bake the SAME
       ``CIRC_ID``/``rp_child_id`` and the SR14 check would pass for the wrong
       group (nominations misattributed, silently).  This function always
       passes :meth:`RuleGroup.group_hash`.
    2. **the lowering.**  Every slot goes through :func:`slot_pattern`, i.e.
       bytes mode with the R15 ASCII fold — the S1 silicon defect was a
       str-mode ``nocase`` lowering that over-approximated to "any code point"
       and still passed every completeness-only test.
    3. **the slot order.**  Slot index IS ``pattern_id``, tombstones included,
       so the emitted circuit and the sidecar agree by construction.

    Returns a :class:`pyro.hdl.generator.GeneratedGroup`, which is also the
    duck type :class:`pyro._circuit_model.GroupCircuitModel` consumes — the
    RTL and the software model are then two views of one artifact (SR16).
    """
    from ..hdl import generator as _gen
    pats: List[Optional[bytes]] = []
    flags: List[int] = []
    for slot in group.slots:
        if slot.tombstone:
            pats.append(None)
            flags.append(0)
            continue
        pat, fl = slot_pattern(slot)
        pats.append(pat)
        flags.append(fl)
    digest = group.group_hash(_gen.GENERATOR_VERSION, _gen.HARNESS_VERSION,
                              datapath_bytes)
    return _gen.generate_group(pats, flags, datapath_bytes=datapath_bytes,
                               group_hash=digest)


def group_automata(group: RuleGroup) -> Tuple:
    """Per-slot automata, index-aligned with ``group.slots`` (SR7 N automata).

    Tombstoned slots yield ``None`` — they keep their ``pattern_id`` but can
    never match.
    """
    return tuple(slot_automaton(s) for s in group.slots)


# --------------------------------------------------------------------------
# Packing (SR6)
# --------------------------------------------------------------------------
_WS = _stock_compile(r"\s+")
_PORT_VAR = _stock_compile(r"\$[A-Za-z_][A-Za-z0-9_]*\Z")

#: Class for every non-variable destination port (single literal port, list,
#: range, negation).  See :func:`port_class` for why they coalesce.
PORT_CLASS_LITERAL = "literal"
PORT_CLASS_ANY = "any"


def port_class(dst_port: str) -> str:
    """Map a rule's destination-port token to its SR6 group key.

    The **token** only: ``$HTTP_PORTS`` stays ``$HTTP_PORTS``.  Its site-local
    *value* is never resolved here (SR13/SF16) — that is the daemon's variable
    table.  Whitespace in list forms is collapsed and ``any`` is
    case-normalized so two spellings of one class cannot split a group.

    Every **non-variable** port token — a literal port, a list, a range, a
    negation — coalesces into one ``literal`` class.  Measured on the corpus
    (3,896 anchor-compilable rules): keying on the raw token gives 180 classes
    and **190 groups**, almost all of them tiny (one 4 MB partial and a ~45 s
    JTAG swap, SF20, to carry three rules).  Coalescing gives 8 classes and
    **21 groups**, which is what SR6's "→ ~16 groups" describes.  Correctness
    is unaffected either way: the daemon still evaluates each rule's own
    header predicate host-side before nominating (SR13), so a coarse class
    only means a flow is offered to a group holding some rules its port does
    not satisfy — extra work, never a missed or misattributed rule.

    Splitting the ``literal`` class back out by port family (SF8: 25→121,
    21→86, 111→63, …) would sharpen SR10's residency scheduling; that needs
    measured traffic-mix evidence, so it is deliberately *not* a knob here.
    ``pack_groups(..., class_of=...)`` exists for experiments.
    """
    tok = _WS.sub("", (dst_port or "").strip())
    if not tok or tok.lower() == "any":
        return PORT_CLASS_ANY
    if _PORT_VAR.match(tok):
        return tok
    return PORT_CLASS_LITERAL


def _rule_ident(rule: Rule) -> Tuple[Optional[int], int]:
    """``(sid, gid)`` from the option chain; ``gid`` defaults to 1."""
    sid = gid = None
    for opt in rule.options:
        if opt.key == "sid":
            sid = _safe_int(opt.value)
        elif opt.key == "gid":
            gid = _safe_int(opt.value)
    return sid, (1 if gid is None else gid)


def _safe_int(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class GroupEntry(NamedTuple):
    """A groupable rule: its sort key, its lowered matcher, its sidecar row."""

    sid: int
    gid: int
    line_no: int
    anchor: bytes            # anchor dedup form (case-folded iff nocase)
    nocase: bool
    ref: RuleRef
    port_class: str
    dst_port: str            # raw token, for reporting; never hashed
    pattern: bytes = b""     # SR3 lowered matcher source (b"" = derive)
    flags: int = -1          # matcher flags (-1 = derive from nocase)
    tail_span: int = -1      # SR12 tail contribution (-1 = len(anchor))

    @property
    def slot_key(self) -> Tuple[bytes, int]:
        """Slot-dedup identity: two rules share a slot iff their lowered
        matchers are identical (v1 deduped on the anchor literal — same
        thing for anchor-only rules, since the lowering is deterministic)."""
        pat = self.pattern if self.pattern else _stdre.escape(self.anchor)
        fl = self.flags if self.flags >= 0 else (
            _stdre.IGNORECASE if self.nocase else 0)
        return (pat, fl)


def groupable_entries(triaged: Iterable[Tuple[Rule, TriageResult]],
                      class_of=port_class) -> List[GroupEntry]:
    """The SR6-packable subset of a triage run, in stable sid order.

    Keeps only ``anchor-compilable`` rules (SR2) that carry a parseable
    ``sid`` — attribution is by ``gid:sid`` (spec §2), so a rule without one
    could never be nominated.  Sort key is ``(sid, gid, line_no)``: total,
    deterministic, and independent of the input file's line order, so a
    reordered ruleset packs identically (SR6 stability).

    Each entry carries its SR3 lowering (AC-S3-2): the content chain /
    fused-pcre matcher from :func:`pyro.snort.lowering.lower_rule`, plus
    the SR4 dropped-conjunct list and over-approximation classes on the
    sidecar row.
    """
    out: List[GroupEntry] = []
    for rule, res in triaged:
        if res.tier != _triage.TIER_ANCHOR or res.anchor is None:
            continue
        sid, gid = _rule_ident(rule)
        if sid is None:
            continue
        anchor = res.anchor
        low = _lowering.lower_rule(rule, res)
        out.append(GroupEntry(
            sid=sid, gid=gid, line_no=rule.line_no,
            anchor=anchor.dedup_key, nocase=anchor.nocase,
            ref=RuleRef(gid, sid, res.subtier, anchor.buffer, rule.line_no,
                        dropped=low.dropped, oa=low.oa_classes),
            port_class=class_of(rule.dst_port),
            dst_port=_WS.sub("", (rule.dst_port or "").strip()) or "any",
            pattern=low.pattern, flags=low.flags, tail_span=low.tail_span,
        ))
    out.sort(key=lambda e: (e.sid, e.gid, e.line_no))
    return out


def _build_group(port_cls: str, index: int, entries: Sequence[GroupEntry],
                 group_max: int) -> RuleGroup:
    """Dedup ``entries`` onto slots, first-claim order (SR6 determinism)."""
    order: List[Tuple[bytes, int]] = []
    by_key: Dict[Tuple[bytes, int], List[GroupEntry]] = {}
    for e in entries:
        key = e.slot_key
        if key not in by_key:
            by_key[key] = []
            order.append(key)
        by_key[key].append(e)
    slots = []
    for i, key in enumerate(order):
        claim = by_key[key][0]           # first-claim: deterministic
        slots.append(GroupSlot(
            i, claim.anchor, claim.nocase,
            tuple(e.ref for e in by_key[key]),
            pattern=claim.pattern, flags=claim.flags,
            tail_span=claim.tail_span))
    return RuleGroup(port_cls, index, tuple(slots), group_max)


def pack_groups(triaged: Iterable[Tuple[Rule, TriageResult]],
                group_max: int = GROUP_MAX,
                class_of=port_class) -> List[RuleGroup]:
    """Pack a triage run into SR6 groups: port class first, then sid order.

    Returns groups ordered by ``(port_class, index)`` with the port classes in
    lexicographic order — deliberately *not* by rule count, which would churn
    group identities every time the corpus shifts (SR6/SR9 stability).
    ``group_max`` bounds **rules** per group; slots are ≤ that after dedup.
    On the current corpus this yields 21 groups over 8 classes, the largest
    being ``$HTTP_PORTS/0`` (256 rules → 253 slots — the AC-S2-2 group).

    ``class_of`` overrides the SR6 class key for experiments only; changing
    the default repartitions every group and dirties every cache key (SR9).
    """
    if group_max < 1:
        raise ValueError("group_max must be >= 1")
    buckets: Dict[str, List[GroupEntry]] = {}
    for e in groupable_entries(triaged, class_of):
        buckets.setdefault(e.port_class, []).append(e)
    groups: List[RuleGroup] = []
    for cls in sorted(buckets):
        entries = buckets[cls]
        for i in range(0, len(entries), group_max):
            groups.append(_build_group(cls, i // group_max,
                                       entries[i:i + group_max], group_max))
    return groups


# --------------------------------------------------------------------------
# Incremental repack with tombstones (SR6/SR9)
# --------------------------------------------------------------------------
def repack_with_tombstones(previous: Sequence[RuleGroup],
                           triaged: Iterable[Tuple[Rule, TriageResult]],
                           group_max: int = GROUP_MAX,
                           class_of=port_class) -> List[RuleGroup]:
    """Re-pack a new ruleset against a previous layout, leaving tombstones.

    SR6: a deleted sid must **not** trigger a repack, because a repack shifts
    every downstream ``pattern_id`` and dirties every group (SR9).  So:

    * a rule that survives with an unchanged anchor keeps its ``(group, slot)``
      — matched by ``gid:sid``, not by anchor, so a rule is never migrated
      across groups just because some other group happens to hold the same
      literal (that would dirty two groups and could push one past
      ``group_max``);
    * a deleted rule is dropped from its slot; a slot that loses all its rules
      becomes a **tombstone** — index retained, never reused, never renumbered;
    * a rule whose anchor changed is treated as a deletion plus an insertion;
    * a new rule joins an existing slot with the same anchor **in a group of
      its class that still has rule capacity**, else takes the next free slot
      index in the first group of its class with spare rule/slot capacity
      (tombstoned groups refill without renumbering), else starts a new group.

    Groups absent from the new ruleset survive as all-tombstone groups (their
    ``pattern_id`` space stays reserved); a *new* port class simply appears.
    """
    new_by_class: Dict[str, List[GroupEntry]] = {}
    for e in groupable_entries(triaged, class_of):
        new_by_class.setdefault(e.port_class, []).append(e)

    # Where each rule sat, and on which matcher: a surviving rule keeps its
    # placement only if its LOWERED matcher is unchanged (SR6 stability; v1
    # compared the anchor — same predicate for anchor-only rules, and a
    # chain change genuinely is a different circuit, so it must move).
    prev_rule: Dict[Tuple[str, int, int],
                    Tuple[int, int, Tuple[bytes, int]]] = {}
    for g in previous:
        for s in g.slots:
            for r in s.rules:
                prev_rule[(g.port_class, r.gid, r.sid)] = (
                    g.index, s.index, (s.pattern_eff, s.flags_eff))

    # Start from the previous shape, emptied: every slot keeps its index.
    # ``slot_desc`` holds each slot's full matcher descriptor
    # (pattern, flags, tail_span, anchor, nocase) for reconstruction;
    # matching is on the effective (pattern, flags) key.
    shape: Dict[str, Dict[int, Dict[int, List[RuleRef]]]] = {}
    slot_desc: Dict[Tuple[str, int, int],
                    Tuple[bytes, int, int, bytes, bool]] = {}
    caps: Dict[str, int] = {}
    for g in previous:
        cls_slots = shape.setdefault(g.port_class, {}).setdefault(g.index, {})
        caps[g.port_class] = max(caps.get(g.port_class, 0), g.index + 1)
        for s in g.slots:
            cls_slots[s.index] = []
            slot_desc[(g.port_class, g.index, s.index)] = (
                s.pattern, s.flags, s.tail_span, s.anchor, s.nocase)

    for cls in sorted(new_by_class):
        for e in new_by_class[cls]:
            prior = prev_rule.get((cls, e.ref.gid, e.ref.sid))
            if prior is not None and prior[2] == e.slot_key:
                gi, si = prior[0], prior[1]     # unchanged: stay put
            else:
                gi, si = _placement(shape, slot_desc, caps, cls, e, group_max)
            shape.setdefault(cls, {}).setdefault(gi, {}).setdefault(si, [])
            shape[cls][gi][si].append(e.ref)
            slot_desc[(cls, gi, si)] = (e.pattern, e.flags, e.tail_span,
                                        e.anchor, e.nocase)

    out: List[RuleGroup] = []
    for cls in sorted(shape):
        for gi in sorted(shape[cls]):
            slots = []
            for si in sorted(shape[cls][gi]):
                pattern, flags, tail_span, anchor, nocase = \
                    slot_desc[(cls, gi, si)]
                slots.append(GroupSlot(si, anchor, nocase,
                                       tuple(shape[cls][gi][si]),
                                       pattern=pattern, flags=flags,
                                       tail_span=tail_span))
            out.append(RuleGroup(cls, gi, tuple(slots), group_max))
    return out


def _slot_desc_key(desc: Tuple[bytes, int, int, bytes, bool]
                   ) -> Tuple[bytes, int]:
    """Effective (pattern, flags) of a stored slot descriptor."""
    pattern, flags, _tail, anchor, nocase = desc
    pat = pattern if pattern else _stdre.escape(anchor)
    fl = flags if flags >= 0 else (_stdre.IGNORECASE if nocase else 0)
    return (pat, fl)


def _placement(shape, slot_desc, caps, cls: str, entry: GroupEntry,
               group_max: int) -> Tuple[int, int]:
    """Where a (re)placed rule goes: an existing matching slot with room, else
    a free slot index in the first group with room, else a new group."""
    groups = shape.setdefault(cls, {})
    want = entry.slot_key
    for gi in sorted(groups):
        rules = sum(len(v) for v in groups[gi].values())
        if rules >= group_max:
            continue
        for si in sorted(groups[gi]):
            desc = slot_desc.get((cls, gi, si))
            if desc is not None and _slot_desc_key(desc) == want:
                return gi, si            # share the slot (dedup)
        if len(groups[gi]) < group_max:
            return gi, (max(groups[gi]) + 1) if groups[gi] else 0
    gi = caps.get(cls, 0)
    caps[cls] = gi + 1
    groups.setdefault(gi, {})
    return gi, 0
