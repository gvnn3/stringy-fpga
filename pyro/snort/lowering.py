"""SR3 content-chain lowering + clean-pcre fusion (AC-S3-2).

Turns a triaged rule into the **slot pattern** its group circuit compiles:
the anchor content chain with ``offset/depth/distance/within`` lowered to
bounded gap windows, ``nocase`` via the R15 ASCII byte-fold, and — when it
is clean (SF13), ``R``-flagged, and cursor-anchored — the rule's pcre fused
as a literal⋅regex concatenation.  Everything else stays a **dropped
conjunct** (SR3: dropping only ever enlarges the recognized language).

Completeness is absolute (SR3/PYRO R19): the lowered pattern must fire on
every input the rule's compiled conjuncts would fire on.  Three consequences
shape everything here:

1. **Windows are supersets.**  A ``distance D / within W`` link is lowered
   as a gap of ``[max(0,D), D+W]`` bytes — at least as wide as either
   reading of Snort's ``within`` (from the previous match end, or from the
   distance point).  When the bound cannot be represented
   (negative distance, ``within`` absent, non-integer argument,
   ``D+W > MAX_LOWER_GAP``) the chain STOPS there and the remaining
   conjuncts stay dropped — never a narrower window.

2. **Raw buffers only.**  ``distance``/``within``/``offset``/``depth``
   count bytes in the buffer Snort matches in.  For a normalized sticky
   buffer (http_uri, file_data, ...) those distances are measured in
   inspector-normalized bytes — %-decode, dechunk, gunzip all change byte
   offsets — so a window lowered against RAW bytes could *miss* (an SR3
   completeness defect, not an over-approximation).  Chains are therefore
   lowered only when the whole chain lives in a RAW buffer
   (``pkt_data``/``raw_data``); normalized-buffer rules keep their
   anchor-only circuit and the positional conjuncts stay dropped
   (declared ``anchor_strip``, exactly as S2 classified them).

3. **Spans are bounded.**  The daemon scans 1,474-byte chunks with an
   SR12 overlap tail; a match is only guaranteed to be seen if it fits
   entirely inside one chunk window, i.e. its span is ≤ tail+1.  Every
   floating (unanchored) lowered pattern therefore carries a computable
   ``tail_span`` bound, capped at :data:`SPAN_CAP`; a chain that would
   exceed it is trimmed back (dropping conjuncts, which is always sound).
   Unbounded constructs (``offset`` without ``depth``, unanchored ``R``
   pcre, unbounded pcre quantifiers) are never lowered.  ``\\A``-anchored
   patterns (lowered ``offset/depth``) contribute ``tail_span = 0``: they
   can only match at the buffer start, which chunk 0 always contains
   whole (the ``offset+depth <= CHUNK_BYTES`` admission condition), so
   the overlap tail owes them nothing.

Lowering is a **total, deterministic** function of the rule text (SR1
discipline): any internal surprise degrades to the S1/S2-proven
anchor-only lowering, never an exception.  Single-content rules with no
lowered positional prefix produce byte-identical patterns to the S2 path
(``re.escape(anchor)`` + slot-level ``IGNORECASE``), so pure-anchor slots
keep their identity across this change except for the GROUP_FORMAT_VERSION
bump that serializes the new fields.
"""

from __future__ import annotations

import re as _stdre
from typing import List, NamedTuple, Optional, Sequence, Tuple

from .rules import Rule
from . import triage as _triage
from .triage import Anchor, ContentInfo, PcreInfo, TriageResult

#: Largest lowered gap window, in bytes.  Matches SF6's ``MAX_REPEAT = 255``
#: (a ``.{lo,hi}`` gap costs ``hi`` automaton states, and 63 corpus patterns
#: already degrade on this bound — SF13).
MAX_LOWER_GAP = 255

#: Cap on a floating lowered pattern's span (``tail_span``).  The SR12
#: overlap tail is ``max tail_span − 1`` per group; at 384 the worst-case
#: tail is 383 bytes against 1,474-byte chunks (~26% re-scan overhead).
#: Chains that would exceed it are trimmed (sound: trimming drops
#: conjuncts).
SPAN_CAP = 384

#: R78 MATCH_REQUEST payload bound (corpus profile) — the admission
#: condition for lowering ``offset/depth``: the absolute window
#: ``[offset, offset+depth)`` must fit inside chunk 0, so a constrained
#: match is always fully visible to the one request that is
#: buffer-aligned.
CHUNK_BYTES = 1474

# SR4 over-approximation class vocabulary (spec §4.2 SR4, normative).
OA_DROPPED = "dropped_conjuncts"
OA_ANCHOR_STRIP = "anchor_strip"
OA_CASE_FOLD = "case_fold"
OA_CHUNK_OVERLAP = "chunk_overlap"
OA_CLASSES = (OA_DROPPED, OA_ANCHOR_STRIP, OA_CASE_FOLD, OA_CHUNK_OVERLAP)

#: Content modifiers a chain LINK may carry and still be lowered.  Anything
#: else on a candidate content stops the chain before it (conservative:
#: an unknown modifier might constrain position in a way we cannot see).
_LINK_MODS = frozenset({"nocase", "fast_pattern", "distance", "within"})
#: The chain HEAD may additionally carry the absolute-position pair.
_HEAD_MODS = _LINK_MODS | {"offset", "depth"}

_POSITIONAL = ("offset", "depth", "distance", "within")

# pcre flag letters we can honor in a fused fragment.  ``i``/``s``/``m``
# become a scoped ``(?ism:...)`` group; ``A`` anchors at the cursor (same
# as a leading ``^``); ``R`` is the relative flag fusion consumes; ``O``
# (Snort's match-limit override) and ``G`` (ungreedy default) do not change
# the recognized language.  Anything else — notably ``x`` — rejects fusion.
_FUSABLE_PCRE_FLAGS = frozenset("ismARGO")


class LoweredSlot(NamedTuple):
    """The circuit-side lowering of one rule (SR3)."""

    pattern: bytes                 # regex source, bytes mode (automaton input)
    flags: int                     # slot-level re flags (0 or IGNORECASE)
    tail_span: int                 # SR12 tail contribution (0 if \A-anchored)
    anchor: bytes                  # anchor literal, dedup form (reporting)
    nocase: bool                   # anchor nocase (reporting)
    lowered_indexes: Tuple[int, ...]  # option indexes realized in the circuit
    dropped: Tuple[str, ...]       # SR4 per-rule dropped-conjunct option keys
    oa_classes: Tuple[str, ...]    # SR4 classes this rule contributes
    fused_pcre: bool               # a pcre fragment is in the pattern
    chain_len: int                 # number of content fragments lowered


class _Frag(NamedTuple):
    """One lowered chain fragment: a literal with its gap to the previous."""

    literal: bytes                 # decoded content bytes (folded iff nocase)
    nocase: bool
    gap_lo: int                    # gap window to the PREVIOUS fragment
    gap_hi: int
    option_index: int


def _mod_map(ci: ContentInfo) -> dict:
    """Last-wins map of the content's raw modifiers."""
    return {name: arg for name, arg in ci.content.modifiers}


def _int_arg(arg: Optional[str]) -> Optional[int]:
    """Parse a positional-modifier argument; None for absent/variable/junk."""
    if arg is None:
        return None
    try:
        return int(arg.strip())
    except (ValueError, AttributeError):
        return None  # byte_extract variable or malformed — not lowerable


def _is_nocase(ci: ContentInfo) -> bool:
    return any(name == "nocase" for name, _ in ci.content.modifiers)


def positional(ci: ContentInfo) -> Tuple[Optional[int], Optional[int],
                                         Optional[int], Optional[int]]:
    """``(offset, depth, distance, within)`` as ints; None when absent or
    not an integer literal (a byte_extract variable is not lowerable)."""
    mods = _mod_map(ci)
    return tuple(_int_arg(mods.get(k)) for k in _POSITIONAL)  # type: ignore


def _has_positional(ci: ContentInfo) -> bool:
    mods = _mod_map(ci)
    return any(k in mods for k in _POSITIONAL)


def _cursor_neutral(key: str) -> bool:
    """True if an option key between two chain contents cannot move the
    detection cursor or change the buffer: metadata and header/flow
    predicates only.  Any payload option or buffer selector breaks
    relative adjacency (its cursor effect is what ``distance`` would be
    relative to)."""
    return (key not in _triage.STICKY_BUFFERS
            and key not in _triage.PAYLOAD_OPTIONS
            and key not in ("content", "pcre"))


def _adjacent(rule: Rule, idx_a: int, idx_b: int) -> bool:
    """No cursor-affecting option strictly between option idx_a and idx_b."""
    return all(_cursor_neutral(rule.options[i].key)
               for i in range(idx_a + 1, idx_b))


def _link_window(ci: ContentInfo) -> Optional[Tuple[int, int]]:
    """Bounded relative window ``(gap_lo, gap_hi)`` for a chain link, or
    None when the link cannot be lowered soundly."""
    offset, depth, distance, within = positional(ci)
    if offset is not None or depth is not None:
        return None                      # absolutely positioned — not a link
    if within is None:
        return None                      # unbounded forward window
    mods = _mod_map(ci)
    if "distance" in mods and distance is None:
        return None                      # variable/junk distance argument
    if "within" in mods and within is None:
        return None
    d = distance if distance is not None else 0
    if d < 0:
        return None                      # overlap: a sequential NFA cannot
    if within < 0:
        return None
    hi = d + within                      # superset of either 'within' reading
    if hi > MAX_LOWER_GAP:
        return None                      # cannot represent without narrowing
    return (d, hi)


def _fold(literal: bytes, nocase: bool) -> bytes:
    """Dedup/storage form of a fragment literal (R15 identity)."""
    return literal.lower() if nocase else literal


def _frag_regex(frag: _Frag) -> bytes:
    lit = _stdre.escape(frag.literal)
    if frag.nocase:
        return b"(?i:" + lit + b")"
    return lit


# --------------------------------------------------------------------------
# pcre fusion (SF13 / SR3)
# --------------------------------------------------------------------------
def _pcre_max_span(body: bytes) -> Optional[int]:
    """Upper bound on the bytes a match of ``body`` can span; None when
    unbounded (or unparseable).  Computed on the stdlib parse tree so it is
    exact for the constructs the automaton accepts."""
    try:
        import re._parser as _sre  # py311+
    except ImportError:  # pragma: no cover - older interpreter
        import sre_parse as _sre  # type: ignore
    try:
        parsed = _sre.parse(body)
    except Exception:
        return None
    MAXREPEAT = _sre.MAXREPEAT

    def span(seq) -> Optional[int]:
        total = 0
        for op, av in seq:
            name = str(op)
            if name in ("LITERAL", "NOT_LITERAL", "IN", "ANY", "CATEGORY",
                        "RANGE"):
                total += 1
            elif name == "AT":
                continue
            elif name == "SUBPATTERN":
                sub = span(av[3])
                if sub is None:
                    return None
                total += sub
            elif name == "BRANCH":
                worst = 0
                for b in av[1]:
                    s = span(b)
                    if s is None:
                        return None
                    worst = max(worst, s)
                total += worst
            elif name in ("MAX_REPEAT", "MIN_REPEAT"):
                mn, mx, sub = av
                if mx is MAXREPEAT:
                    return None
                s = span(sub)
                if s is None:
                    return None
                total += int(mx) * s
            else:
                return None              # GROUPREF etc. — not fusable anyway
        return total

    return span(list(parsed))


def _fuse_pcre(pi: PcreInfo, buffer: str) -> Optional[Tuple[bytes, int]]:
    """The fused regex fragment ``(fragment_bytes, max_span)`` for a pcre,
    or None when it is not soundly fusable (SR3's conditions: clean per
    SF13, ``R``-flagged, cursor-anchored, raw buffer, bounded span,
    representable flags)."""
    p = pi.pcre
    if p.negated or not p.relative or pi.pcre_class != "clean":
        return None
    if buffer not in _triage.RAW_BUFFERS:
        return None
    if not set(p.flags) <= _FUSABLE_PCRE_FLAGS:
        return None
    body = p.pattern
    anchored = "A" in p.flags
    if body.startswith("^"):
        anchored = True
        body = body[1:]
    if not anchored:
        return None                      # unanchored R = unbounded gap
    if not body:
        return None
    try:
        body_b = body.encode("ascii")
    except UnicodeEncodeError:
        return None
    span = _pcre_max_span(body_b)
    if span is None or span > SPAN_CAP:
        return None
    scoped = "".join(f for f in "ism" if f in p.flags)
    if scoped:
        frag = b"(?" + scoped.encode("ascii") + b":" + body_b + b")"
    else:
        frag = b"(?:" + body_b + b")"
    return frag, span


# --------------------------------------------------------------------------
# The lowering
# --------------------------------------------------------------------------
def _legacy(anchor: Anchor, dropped: Tuple[str, ...],
            oa: Tuple[str, ...]) -> LoweredSlot:
    """The S1/S2-proven anchor-only lowering — byte-identical to
    ``re.escape(dedup_key)`` + slot-level IGNORECASE."""
    lit = anchor.dedup_key
    return LoweredSlot(
        pattern=_stdre.escape(lit),
        flags=(_stdre.IGNORECASE if anchor.nocase else 0),
        tail_span=len(lit),
        anchor=lit,
        nocase=anchor.nocase,
        lowered_indexes=(anchor.option_index,),
        dropped=dropped,
        oa_classes=oa,
        fused_pcre=False,
        chain_len=1,
    )


def _rule_dropped(rule: Rule, lowered_indexes: Sequence[int]) -> Tuple[str, ...]:
    """SR4 per-rule dropped-conjunct list: every detection option not
    realized in the circuit (metadata and buffer selectors excluded)."""
    lowered = set(lowered_indexes)
    out: List[str] = []
    for idx, opt in enumerate(rule.options):
        if opt.key in _triage.METADATA_OPTIONS:
            continue
        if opt.key in _triage.STICKY_BUFFERS and not (
                opt.value is not None
                and opt.key in _triage.VALUED_MATCH_OPTIONS):
            continue
        if idx in lowered:
            continue
        out.append(opt.key)
    return tuple(out)


def _oa_classes(res: TriageResult, *, any_nocase: bool, dropped: bool,
                positional_undropped: bool) -> Tuple[str, ...]:
    out: List[str] = []
    if dropped:
        out.append(OA_DROPPED)
    if res.subtier == _triage.SUBTIER_NORMALIZED or positional_undropped:
        out.append(OA_ANCHOR_STRIP)
    if any_nocase:
        out.append(OA_CASE_FOLD)
    out.append(OA_CHUNK_OVERLAP)        # chunked scanning + SR12 tail re-scan
    return tuple(out)


def lower_rule(rule: Rule, res: TriageResult) -> LoweredSlot:
    """SR3 lowering of one anchor-compilable rule.  Total and deterministic:
    any condition that cannot be lowered soundly degrades toward the
    anchor-only circuit, never an exception, never a narrower window."""
    anchor = res.anchor
    if anchor is None:
        raise ValueError("lower_rule requires an anchor-compilable rule")
    try:
        return _lower(rule, res, anchor)
    except Exception:
        # Total-function discipline: an unexpected corpus shape degrades to
        # the proven lowering (completeness preserved; precision lost).
        dropped = _rule_dropped(rule, (anchor.option_index,))
        oa = _oa_classes(res, any_nocase=anchor.nocase, dropped=bool(dropped),
                         positional_undropped=_any_positional(res))
        return _legacy(anchor, dropped, oa)


def _any_positional(res: TriageResult) -> bool:
    return any(_has_positional(ci) for ci in res.contents
               if not ci.content.negated)


def _lower(rule: Rule, res: TriageResult, anchor: Anchor) -> LoweredSlot:
    contents = [ci for ci in res.contents]
    by_index = {ci.option_index: ci for ci in contents}
    anchor_ci = by_index.get(anchor.option_index)

    raw_chain = (anchor.buffer in _triage.RAW_BUFFERS
                 and anchor_ci is not None)

    # ---- assemble the chain (head..anchor..tail), raw buffers only -------
    chain: List[ContentInfo] = [anchor_ci] if raw_chain else []
    if raw_chain:
        order = [ci for ci in contents if not ci.content.negated
                 and ci.buffer == anchor.buffer]
        pos = order.index(anchor_ci)
        # Backward: while the current head is relatively linked to its
        # predecessor with a bounded window and clean modifiers.
        i = pos
        while i > 0:
            head, prev = order[i], order[i - 1]
            if not set(_mod_map(head)) <= _LINK_MODS | {"distance", "within"}:
                break
            if _link_window(head) is None:
                break
            if not _adjacent(rule, prev.option_index, head.option_index):
                break
            if not set(_mod_map(prev)) <= _HEAD_MODS:
                break
            chain.insert(0, prev)
            i -= 1
        # Forward: extend past the anchor while links stay bounded.
        j = pos
        while j + 1 < len(order):
            nxt = order[j + 1]
            if not set(_mod_map(nxt)) <= _LINK_MODS:
                break
            if _link_window(nxt) is None:
                break
            if not _adjacent(rule, order[j].option_index, nxt.option_index):
                break
            chain.append(nxt)
            j += 1

    if not chain:
        # Normalized-buffer anchor: nothing beyond the S2 path is sound
        # (chains, prefixes, and fusion all need raw-byte distances).
        dropped = _rule_dropped(rule, (anchor.option_index,))
        oa = _oa_classes(res, any_nocase=anchor.nocase, dropped=bool(dropped),
                         positional_undropped=_any_positional(res))
        return _legacy(anchor, dropped, oa)

    # ---- head absolute prefix (offset/depth), admission-gated ------------
    head = chain[0]
    prefix = _lowerable_prefix(rule, head)

    # ---- build fragments, then trim to the span cap ----------------------
    frags: List[_Frag] = []
    for k, ci in enumerate(chain):
        if k == 0:
            gap = (0, 0)
        else:
            gap = _link_window(ci)
            assert gap is not None       # enforced during assembly
        frags.append(_Frag(_fold(ci.content.pattern, _is_nocase(ci)),
                           _is_nocase(ci), gap[0], gap[1], ci.option_index))

    def _span(fr: Sequence[_Frag]) -> int:
        return sum(len(f.literal) + f.gap_hi for f in fr)

    # Trim trailing links until the floating span fits the cap; never trim
    # past the anchor.
    anchor_pos = next(k for k, f in enumerate(frags)
                      if f.option_index == anchor.option_index)
    while _span(frags) > SPAN_CAP and len(frags) - 1 > anchor_pos:
        frags.pop()
    while _span(frags) > SPAN_CAP and anchor_pos > 0:
        frags.pop(0)
        anchor_pos -= 1
    if _span(frags) > SPAN_CAP:
        dropped = _rule_dropped(rule, (anchor.option_index,))
        oa = _oa_classes(res, any_nocase=anchor.nocase, dropped=bool(dropped),
                         positional_undropped=_any_positional(res))
        return _legacy(anchor, dropped, oa)
    if frags[0].option_index != head.option_index:
        prefix = None                    # head was trimmed off

    # ---- pcre fusion (only directly after the LAST lowered content) ------
    fused: Optional[Tuple[bytes, int]] = None
    fused_index: Optional[int] = None
    last_idx = frags[-1].option_index
    nxt_pcre = [pi for pi in res.pcres
                if pi.option_index > last_idx
                and _adjacent(rule, last_idx, pi.option_index)]
    if nxt_pcre:
        pi = min(nxt_pcre, key=lambda p: p.option_index)
        if pi.buffer == anchor.buffer:
            cand = _fuse_pcre(pi, pi.buffer)
            if cand is not None and _span(frags) + cand[1] <= SPAN_CAP:
                fused, fused_index = cand, pi.option_index

    # ---- emit the pattern ------------------------------------------------
    if len(frags) == 1 and prefix is None and fused is None:
        dropped = _rule_dropped(rule, (anchor.option_index,))
        oa = _oa_classes(res, any_nocase=anchor.nocase, dropped=bool(dropped),
                         positional_undropped=_any_positional(res))
        return _legacy(anchor, dropped, oa)

    parts: List[bytes] = []
    if prefix is not None:
        lo, hi = prefix
        parts.append(b"\\A" if (lo, hi) == (0, 0)
                     else b"\\A.{%d,%d}" % (lo, hi))
    for k, f in enumerate(frags):
        if k > 0:
            parts.append(b".{%d,%d}" % (f.gap_lo, f.gap_hi)
                         if (f.gap_lo, f.gap_hi) != (0, 0) else b"")
        parts.append(_frag_regex(f))
    if fused is not None:
        parts.append(fused[0])
    pattern = b"(?s:" + b"".join(parts) + b")"

    # ---- validate against the real automaton (fail toward legacy) --------
    from ..hdl import automaton as _auto
    try:
        _auto.build(pattern, 0, _auto.ENC_BYTES)
    except Exception:
        if fused is not None:
            return _lower_without_pcre(rule, res, anchor)
        dropped = _rule_dropped(rule, (anchor.option_index,))
        oa = _oa_classes(res, any_nocase=anchor.nocase, dropped=bool(dropped),
                         positional_undropped=_any_positional(res))
        return _legacy(anchor, dropped, oa)

    lowered_indexes = tuple(f.option_index for f in frags) + (
        (fused_index,) if fused_index is not None else ())
    dropped = _rule_dropped(rule, lowered_indexes)
    any_nocase = any(f.nocase for f in frags)
    tail_span = 0 if prefix is not None else _span(frags) + (
        fused[1] if fused is not None else 0)
    oa = _oa_classes(res, any_nocase=any_nocase, dropped=bool(dropped),
                     positional_undropped=_positional_undropped(
                         res, lowered_indexes))
    return LoweredSlot(
        pattern=pattern,
        flags=0,
        tail_span=tail_span,
        anchor=anchor.dedup_key,
        nocase=anchor.nocase,
        lowered_indexes=lowered_indexes,
        dropped=dropped,
        oa_classes=oa,
        fused_pcre=fused is not None,
        chain_len=len(frags),
    )


def _lower_without_pcre(rule: Rule, res: TriageResult,
                        anchor: Anchor) -> LoweredSlot:
    """Retry path when the fused pattern fails automaton validation."""
    trimmed = TriageResult(res.tier, res.subtier, res.anchor, res.reason,
                           res.contents, (), res.dropped_options,
                           res.warnings)
    return _lower(rule, trimmed, anchor)


def _positional_undropped(res: TriageResult,
                          lowered_indexes: Sequence[int]) -> bool:
    """True when some positive content still carries positional modifiers
    the circuit did not realize (its constraint was widened or dropped)."""
    lowered = set(lowered_indexes)
    for ci in res.contents:
        if ci.content.negated:
            continue
        if ci.option_index not in lowered and _has_positional(ci):
            return True
    return False


def _pdu_aligned(rule: Rule) -> bool:
    """True when a request buffer's byte 0 provably IS the PDU's byte 0 —
    the admission precondition for ``\\A`` offset/depth lowering.

    Measured defect this guards (AC-S2-3 differential, 2026-07-28): Snort 3
    binds a raw-cursor ``depth`` to the **current PDU section** of an
    inspected flow, not to raw stream offset 0 — sid 509
    (``depth 36``, service:http) alerts on an anchor sitting in a POST
    *body*, far past the first 36 raw stream bytes.  A ``\\A``-lowered
    window evaluated against chunk 0 of the raw stream therefore MISSES it
    (an SR3 completeness defect, demonstrated, not theoretical).  Datagram
    protocols with no service inspector are the case where PDU == datagram
    == the daemon's request buffer, so ``\\A`` is sound there and only
    there.  TCP offset/depth stays a dropped conjunct (``anchor_strip``).
    """
    if rule.proto.lower() not in ("udp", "icmp", "ip"):
        return False
    return not any(o.key == "service" for o in rule.options)


def _lowerable_prefix(rule: Rule, head: ContentInfo
                      ) -> Optional[Tuple[int, int]]:
    """The ``\\A.{lo,hi}`` window for the chain head's ``offset/depth``, or
    None when it must stay a dropped conjunct.

    Admission (all required): :func:`_pdu_aligned` (datagram proto, no
    service inspector — see the measured sid-509 defect there), the head
    in a raw buffer (caller checked), ``depth`` present as an integer,
    ``offset`` integer ≥ 0 (default 0), the absolute window end
    ``offset+depth`` inside one request buffer (``<= CHUNK_BYTES``), and
    the optional-gap width inside the automaton's repeat bound.
    ``offset`` alone (no depth) is an unbounded window — never lowered.
    """
    if not _pdu_aligned(rule):
        return None
    offset, depth, _d, _w = positional(head)
    mods = _mod_map(head)
    if "offset" in mods and offset is None:
        return None
    if "depth" in mods and depth is None:
        return None
    if depth is None:
        return None
    o = offset if offset is not None else 0
    if o < 0 or depth < 1:
        return None
    if o + depth > CHUNK_BYTES:
        return None
    hi = o + depth - 1                   # conservative: >= o+depth-len(head)
    if hi > MAX_LOWER_GAP:
        return None
    return (o, hi)
