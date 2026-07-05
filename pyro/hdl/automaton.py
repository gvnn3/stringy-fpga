"""Regex AST -> byte-level automaton IR (L2, spec §4/§5.1, R9/R14).

This module lowers the CPython ``re._parser`` AST for a HW-eligible pattern into
a Thompson-style NFA over the **byte** alphabet (the transport/datapath unit,
R14).  The automaton is the shared intermediate representation consumed by both
the HDL generator (``pyro.hdl.generator``) and the circuit software model
(``pyro._circuit_model``): they operate on *the same* structure so that a
generator lowering bug surfaces in the model's behavior rather than being masked
by an independent re-derivation from the pattern (per the Task-5 brief).

Encoding modes (R14):

  * **bytes mode** (``enc == ENC_BYTES``): the alphabet is raw octets 0..255;
    classes, ``.`` and case folding follow CPython bytes semantics exactly.
  * **str mode** (``enc == ENC_UTF8``): the subject is UTF-8; the automaton
    matches over UTF-8 **code units** (bytes).  Code-point literals/classes are
    translated to equivalent UTF-8 byte fragments.

Soundness contract (R19).  The automaton is built to be **sound and complete for
group 0**: for every position where CPython ``re`` would begin a match, the NFA
can begin an accepting run.  Where an exact byte-level encoding of a construct is
disproportionately complex (full-Unicode categories, cross-length case folds,
Unicode word boundaries), the builder deliberately **over-approximates** — it
accepts a *superset* of the real language, never a subset — because the host/
model layer re-verifies every reported window against CPython (R19).  Over-
approximation preserves completeness (no CPython match start is ever missed);
false positives introduced by it are removed by re-verification.  Under-
approximation (missing a match) is never permitted and is avoided throughout.

Every over-approximation site is marked with an ``OVER-APPROX`` comment.

Determinism (R8/generator determinism).  State numbers are assigned in a fixed
DFS order over the AST; byte sets are canonical ``frozenset``s; edge lists are
emitted in a stable order.  The same ``(pattern, flags, enc)`` therefore yields a
byte-identical automaton and, in turn, byte-identical RTL.
"""

from __future__ import annotations

import re
import re._constants as _c
import re._parser as _sre
from typing import Dict, FrozenSet, List, NamedTuple, Tuple

# Encoding selector (mirrors pyro_encoding / _model.ENC_*).
ENC_BYTES = 0
ENC_UTF8 = 1

MAXREPEAT = _sre.MAXREPEAT

# Edge kinds.
E_BYTE = 0    # payload: frozenset[int] of matching byte values
E_EPS = 1     # payload: None
E_ASSERT = 2  # payload: int (an AT_* opcode, evaluated by position at run time)


class Edge(NamedTuple):
    kind: int
    payload: object  # frozenset[int] for E_BYTE, None for E_EPS, int for E_ASSERT
    target: int


class Automaton:
    """A byte-alphabet NFA with epsilon and zero-width-assertion edges.

    Attributes:
        n_states:  number of states, numbered ``0 .. n_states-1``.
        start:     the single start state.
        accept:    the single accept state.
        edges:     ``edges[s]`` is the list of outgoing :class:`Edge`s of ``s``.
        enc:       ENC_BYTES or ENC_UTF8.
        flags:     effective ``re`` flags baked into the automaton.
    """

    __slots__ = ("n_states", "start", "accept", "edges", "enc", "flags")

    def __init__(self, n_states, start, accept, edges, enc, flags):
        self.n_states = n_states
        self.start = start
        self.accept = accept
        self.edges = edges
        self.enc = enc
        self.flags = flags

    # -- deterministic serialization helpers (used by the RTL generator) ----
    def sorted_edges(self, s: int) -> List[Edge]:
        """Outgoing edges of ``s`` in a stable, content-defined order."""
        def key(e: Edge):
            if e.kind == E_BYTE:
                return (e.kind, tuple(sorted(e.payload)), e.target)
            if e.kind == E_ASSERT:
                return (e.kind, int(e.payload), e.target)
            return (e.kind, (), e.target)
        return sorted(self.edges[s], key=key)

    def byte_edges(self) -> List[Tuple[int, Tuple[Tuple[int, int], ...], int]]:
        """All byte-consuming edges as ``(src, ranges, dst)`` (ranges canonical)."""
        out = []
        for s in range(self.n_states):
            for e in self.sorted_edges(s):
                if e.kind == E_BYTE:
                    out.append((s, byteset_to_ranges(e.payload), e.target))
        return out


# --------------------------------------------------------------------------
# Byte-set construction helpers
# --------------------------------------------------------------------------
_ALL_BYTES = frozenset(range(256))
_ASCII = frozenset(range(0x80))
_NEWLINE = 0x0A


def byteset_to_ranges(bs: FrozenSet[int]) -> Tuple[Tuple[int, int], ...]:
    """Collapse a byte set into canonical sorted ``(lo, hi)`` inclusive ranges."""
    out: List[Tuple[int, int]] = []
    lo = None
    prev = None
    for b in sorted(bs):
        if lo is None:
            lo = prev = b
        elif b == prev + 1:
            prev = b
        else:
            out.append((lo, prev))
            lo = prev = b
    if lo is not None:
        out.append((lo, prev))
    return tuple(out)


def _ascii_fold(cp: int) -> FrozenSet[int]:
    """ASCII case-fold set of a byte value (bytes mode / str+ASCII flag, R15)."""
    if 0x41 <= cp <= 0x5A or 0x61 <= cp <= 0x7A:
        return frozenset({cp, cp ^ 0x20})
    return frozenset({cp})


# UTF-8 well-formed lead/continuation ranges per code-point length.  The
# continuation byte range is the standard 0x80..0xBF.  Being restricted to the
# canonical (non-overlong) lead ranges keeps the fragment tight; every *real*
# UTF-8-encoded code point of the given length is still accepted (completeness).
_CONT = frozenset(range(0x80, 0xC0))
_LEAD = {
    1: _ASCII,
    2: frozenset(range(0xC2, 0xE0)),
    3: frozenset(range(0xE0, 0xF0)),
    4: frozenset(range(0xF0, 0xF5)),
}


def _utf8_len(cp: int) -> int:
    if cp < 0x80:
        return 1
    if cp < 0x800:
        return 2
    if cp < 0x10000:
        return 3
    return 4


class _Builder:
    """Allocates states and edges; produces an :class:`Automaton`."""

    def __init__(self, enc: int, flags: int):
        self.enc = enc
        self.flags = flags
        self.is_bytes = enc == ENC_BYTES
        self.edges: List[List[Edge]] = []

    # -- state / edge primitives -------------------------------------------
    def new_state(self) -> int:
        self.edges.append([])
        return len(self.edges) - 1

    def eps(self, a: int, b: int) -> None:
        self.edges[a].append(Edge(E_EPS, None, b))

    def byte(self, a: int, b: int, bs: FrozenSet[int]) -> None:
        self.edges[a].append(Edge(E_BYTE, bs, b))

    def assertion(self, a: int, b: int, at: int) -> None:
        self.edges[a].append(Edge(E_ASSERT, int(at), b))

    # -- byte-sequence fragment (a chain of consuming edges) ---------------
    def _chain(self, entry: int, bytesets: List[FrozenSet[int]]) -> int:
        cur = entry
        for bs in bytesets:
            nxt = self.new_state()
            self.byte(cur, nxt, bs)
            cur = nxt
        return cur

    def _anycp_fragment(self, entry: int, exit_: int, lengths, first_len1) -> None:
        """Add fragments matching any UTF-8 code point whose length is in
        ``lengths``.  ``first_len1`` is the byte set used for the (sole) byte of
        a length-1 code point; multi-byte lengths use canonical UTF-8 ranges.
        OVER-APPROX: accepts any code point of the given length(s)."""
        for L in sorted(lengths):
            if L == 1:
                self.byte(entry, exit_, first_len1)
                continue
            bytesets = [_LEAD[L]] + [_CONT] * (L - 1)
            end = self._chain(entry, bytesets)
            self.eps(end, exit_)

    # -- element lowering ---------------------------------------------------
    def _lit_bytesets(self, cp: int, ignorecase: bool, ascii_flag: bool):
        """Return either a list of byte sets (a byte chain) for a literal code
        point, or the sentinel ``None`` meaning 'over-approximate to any code
        point' (used for Unicode-simple IGNORECASE, see R15)."""
        if self.is_bytes:
            bs = _ascii_fold(cp) if ignorecase else frozenset({cp & 0xFF})
            return [bs]
        # str mode (UTF-8)
        if ignorecase and not ascii_flag:
            # OVER-APPROX: Unicode simple case folding can map across UTF-8
            # lengths (e.g. 'K' U+004B <-> U+212A KELVIN SIGN, 's' <-> U+017F).
            # A fixed-length byte encoding would miss a cross-length fold and
            # break completeness, so we accept any code point here and let R19
            # re-verification remove the false positives.
            return None
        if ignorecase and ascii_flag and cp < 0x80:
            # ASCII-flag IGNORECASE folds within ASCII only (single byte, exact).
            fold = _ascii_fold(cp)
            if all(x < 0x80 for x in fold):
                return [fold]  # all single-byte
            return None
        # Exact UTF-8 byte chain for the literal code point.
        return [frozenset({b}) for b in chr(cp).encode("utf-8")]

    def _lower_literal(self, entry, cp, ignorecase, ascii_flag) -> int:
        chain = self._lit_bytesets(cp, ignorecase, ascii_flag)
        if chain is None:
            exit_ = self.new_state()
            self._anycp_fragment(entry, exit_, (1, 2, 3, 4), _ASCII)
            return exit_
        return self._chain(entry, chain)

    def _lower_not_literal(self, entry, cp, ignorecase, ascii_flag) -> int:
        """``[^x]`` style single-character negation."""
        exit_ = self.new_state()
        if self.is_bytes:
            excl = _ascii_fold(cp) if ignorecase else frozenset({cp & 0xFF})
            self.byte(entry, exit_, _ALL_BYTES - excl)
            return exit_
        # str mode: any code point except cp (and, under IGNORECASE, its folds).
        excl_ascii = set()
        if cp < 0x80:
            excl_ascii = set(_ascii_fold(cp)) if (ignorecase and ascii_flag) else {cp}
            if ignorecase and not ascii_flag:
                excl_ascii = {cp}  # OVER-APPROX: ignore cross-length folds here
        # length-1 code points except the excluded ASCII ones (exact for the
        # common ASCII case); all multi-byte code points (OVER-APPROX: at most a
        # single multibyte code point 'cp' is wrongly admitted, R19 removes it).
        len1 = _ASCII - excl_ascii
        self._anycp_fragment(entry, exit_, (1, 2, 3, 4), len1)
        return exit_

    def _lower_any(self, entry, dotall) -> int:
        exit_ = self.new_state()
        if self.is_bytes:
            bs = _ALL_BYTES if dotall else (_ALL_BYTES - {_NEWLINE})
            self.byte(entry, exit_, bs)
            return exit_
        len1 = _ASCII if dotall else (_ASCII - {_NEWLINE})
        self._anycp_fragment(entry, exit_, (1, 2, 3, 4), len1)
        return exit_

    def _category_ascii_set(self, cat: int) -> FrozenSet[int]:
        """ASCII members of a shorthand category (\\d \\w \\s and negations)."""
        digit = frozenset(b"0123456789")
        word = digit | frozenset(range(0x41, 0x5B)) | frozenset(
            range(0x61, 0x7B)) | frozenset({0x5F})
        space = frozenset(b" \t\n\r\f\v")
        pos = {
            _c.CATEGORY_DIGIT: digit, _c.CATEGORY_UNI_DIGIT: digit,
            _c.CATEGORY_WORD: word, _c.CATEGORY_UNI_WORD: word,
            _c.CATEGORY_SPACE: space, _c.CATEGORY_UNI_SPACE: space,
        }
        neg = {
            _c.CATEGORY_NOT_DIGIT: digit, _c.CATEGORY_UNI_NOT_DIGIT: digit,
            _c.CATEGORY_NOT_WORD: word, _c.CATEGORY_UNI_NOT_WORD: word,
            _c.CATEGORY_NOT_SPACE: space, _c.CATEGORY_UNI_NOT_SPACE: space,
        }
        if cat in pos:
            return pos[cat]
        if cat in neg:
            return _ASCII - neg[cat]
        # Unknown category (linebreak etc.): the classifier only admits the
        # ones above, but be safe -> match nothing in ASCII, everything else via
        # the multibyte OVER-APPROX below.
        return frozenset()

    def _lower_in(self, entry, members, ignorecase, ascii_flag) -> int:
        """Lower an ``IN`` character-class node ``[...]`` / ``[^...]``."""
        negate = any(op is _c.NEGATE for op, _ in members)
        exit_ = self.new_state()

        if self.is_bytes:
            acc = set()
            has_cat_multibyte = False  # unused in bytes mode
            for op, av in members:
                if op is _c.NEGATE:
                    continue
                if op is _c.LITERAL:
                    acc |= set(_ascii_fold(av) if ignorecase else {av & 0xFF})
                elif op is _c.RANGE:
                    lo, hi = av
                    rng = set(range(lo, hi + 1))
                    if ignorecase:
                        for cp in list(rng):
                            rng |= set(_ascii_fold(cp))
                    acc |= {b & 0xFF for b in rng}
                elif op is _c.CATEGORY:
                    acc |= set(self._category_bytes(av))
            bs = frozenset(acc)
            if negate:
                bs = _ALL_BYTES - bs
            self.byte(entry, exit_, bs)
            return exit_

        # ---- str mode ----
        ascii_acc = set()      # exact ASCII (len-1) members
        multibyte_lengths = set()
        anycp = False          # OVER-APPROX: some member forces any-code-point
        for op, av in members:
            if op is _c.NEGATE:
                continue
            if op is _c.LITERAL:
                cp = av
                if ignorecase and not ascii_flag and cp < 0x80:
                    anycp = True  # cross-length fold risk (OVER-APPROX)
                elif cp < 0x80:
                    if ignorecase and ascii_flag:
                        ascii_acc |= set(_ascii_fold(cp))
                    else:
                        ascii_acc.add(cp)
                else:
                    if ignorecase:
                        anycp = True  # OVER-APPROX
                    else:
                        multibyte_lengths.add(_utf8_len(cp))
            elif op is _c.RANGE:
                lo, hi = av
                if ignorecase and not ascii_flag:
                    anycp = True  # OVER-APPROX (folds across the range)
                    continue
                if lo < 0x80:
                    hi_a = min(hi, 0x7F)
                    for cp in range(lo, hi_a + 1):
                        if ignorecase and ascii_flag:
                            ascii_acc |= set(_ascii_fold(cp))
                        else:
                            ascii_acc.add(cp)
                if hi >= 0x80:
                    lo_m = max(lo, 0x80)
                    multibyte_lengths |= {_utf8_len(lo_m), _utf8_len(hi)}
            elif op is _c.CATEGORY:
                ascii_acc |= set(self._category_ascii_set(av))
                if not ascii_flag:
                    # In str mode without the ASCII flag, a shorthand category
                    # matches Unicode code points (e.g. \\d admits Arabic-Indic
                    # digits, \\w admits letters in any script, \\S/\\D/\\W admit
                    # essentially all non-ASCII code points).  OVER-APPROX: admit
                    # any non-ASCII code point of length 2..4; R19 re-verifies.
                    multibyte_lengths |= {2, 3, 4}

        if negate:
            # Negated class in str mode.  ASCII portion is exact; all non-ASCII
            # code points are admitted (OVER-APPROX only when the class itself
            # names a multibyte member, which is then wrongly re-admitted and
            # removed by R19 re-verification).
            len1 = _ASCII - ascii_acc
            self._anycp_fragment(entry, exit_, (1, 2, 3, 4), len1)
            return exit_

        if anycp:
            self._anycp_fragment(entry, exit_, (1, 2, 3, 4), _ASCII)
            return exit_

        # Positive class: exact ASCII bytes + any-code-point of each multibyte
        # length actually present.
        if ascii_acc:
            self.byte(entry, exit_, frozenset(ascii_acc))
        if multibyte_lengths:
            self._anycp_fragment(entry, exit_, multibyte_lengths, frozenset())
        return exit_

    def _category_bytes(self, cat: int) -> FrozenSet[int]:
        """Byte set for a shorthand category in bytes mode (exact)."""
        base = self._category_ascii_set(cat)
        # For *NOT* categories in bytes mode, the complement is over all 256
        # octets, not just ASCII.
        not_cats = {
            _c.CATEGORY_NOT_DIGIT: frozenset(b"0123456789"),
            _c.CATEGORY_UNI_NOT_DIGIT: frozenset(b"0123456789"),
            _c.CATEGORY_NOT_WORD: None,
            _c.CATEGORY_UNI_NOT_WORD: None,
            _c.CATEGORY_NOT_SPACE: frozenset(b" \t\n\r\f\v"),
            _c.CATEGORY_UNI_NOT_SPACE: frozenset(b" \t\n\r\f\v"),
        }
        if cat in not_cats:
            if not_cats[cat] is None:
                word = frozenset(b"0123456789") | frozenset(range(0x41, 0x5B)) \
                    | frozenset(range(0x61, 0x7B)) | frozenset({0x5F})
                return _ALL_BYTES - word
            return _ALL_BYTES - not_cats[cat]
        return base

    # -- recursive sequence / node lowering --------------------------------
    def lower_seq(self, seq, entry, ignorecase, ascii_flag, dotall) -> int:
        cur = entry
        for op, av in seq:
            cur = self._lower_node(op, av, cur, ignorecase, ascii_flag, dotall)
        return cur

    def _lower_node(self, op, av, entry, ignorecase, ascii_flag, dotall) -> int:
        if op is _c.LITERAL:
            return self._lower_literal(entry, av, ignorecase, ascii_flag)
        if op is _c.NOT_LITERAL:
            return self._lower_not_literal(entry, av, ignorecase, ascii_flag)
        if op is _c.ANY:
            return self._lower_any(entry, dotall)
        if op is getattr(_c, "ANY_ALL", None):
            return self._lower_any(entry, True)
        if op is _c.IN:
            return self._lower_in(entry, av, ignorecase, ascii_flag)
        if op is _c.AT:
            exit_ = self.new_state()
            self.assertion(entry, exit_, av)
            return exit_
        if op is _c.BRANCH:
            _none, branches = av
            exit_ = self.new_state()
            for b in branches:
                bstart = self.new_state()
                self.eps(entry, bstart)
                bend = self.lower_seq(b, bstart, ignorecase, ascii_flag, dotall)
                self.eps(bend, exit_)
            return exit_
        if op is _c.SUBPATTERN:
            _group, add_flags, del_flags, sub = av
            sub_ic = (ignorecase or bool(add_flags & re.IGNORECASE)) and not (
                del_flags & re.IGNORECASE)
            sub_ascii = (ascii_flag or bool(add_flags & re.ASCII)) and not (
                del_flags & re.ASCII)
            sub_dotall = (dotall or bool(add_flags & re.DOTALL)) and not (
                del_flags & re.DOTALL)
            return self.lower_seq(sub, entry, sub_ic, sub_ascii, sub_dotall)
        if op in (_c.MAX_REPEAT, _c.MIN_REPEAT):
            return self._lower_repeat(av, entry, ignorecase, ascii_flag, dotall)
        raise ValueError(f"cannot lower unsupported construct: {op}")

    def _lower_repeat(self, av, entry, ignorecase, ascii_flag, dotall) -> int:
        mn, mx, sub = av
        # Greedy vs lazy (MAX_REPEAT vs MIN_REPEAT) do not change the *language*,
        # only span selection, which R17/R18 reconcile via CPython.  We lower
        # both identically as a language recognizer.
        cur = entry
        # mandatory copies (m of them)
        m = 0 if mn is MAXREPEAT else int(mn)
        for _ in range(m):
            cur = self.lower_seq(sub, cur, ignorecase, ascii_flag, dotall)
        if mx is MAXREPEAT:
            # unbounded tail: a Kleene star over the sub-pattern.
            loop = self.new_state()
            self.eps(cur, loop)
            body = self.lower_seq(sub, loop, ignorecase, ascii_flag, dotall)
            self.eps(body, loop)
            return loop
        n = int(mx)
        # optional copies (n - m of them), each skippable to the shared exit.
        exit_ = self.new_state()
        self.eps(cur, exit_)
        for _ in range(n - m):
            cur = self.lower_seq(sub, cur, ignorecase, ascii_flag, dotall)
            self.eps(cur, exit_)
        return exit_


def build(pattern, flags: int = 0, enc: int = None) -> Automaton:
    """Build the byte-level automaton for ``(pattern, flags)`` (R9/R14).

    ``enc`` defaults from the pattern type: ``str`` -> ENC_UTF8, otherwise
    ENC_BYTES.  The pattern MUST already be HW-eligible (see
    :func:`pyro.hdl.estimator.estimate`); this function raises ``ValueError`` on
    an unsupported construct rather than silently approximating structure.
    """
    is_str = isinstance(pattern, str)
    if enc is None:
        enc = ENC_UTF8 if is_str else ENC_BYTES

    parsed = _sre.parse(
        pattern if (is_str or isinstance(pattern, bytes)) else bytes(pattern),
        flags,
    )
    eff = int(parsed.state.flags)
    ignorecase = bool(eff & re.IGNORECASE)
    ascii_flag = bool(eff & re.ASCII)
    dotall = bool(eff & re.DOTALL)

    b = _Builder(enc, eff)
    start = b.new_state()
    accept = b.lower_seq(list(parsed), start, ignorecase, ascii_flag, dotall)
    return Automaton(len(b.edges), start, accept, b.edges, enc, eff)
