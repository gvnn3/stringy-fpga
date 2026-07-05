"""L2 pattern classifier / compiler front end (spec §5, R8-R15).

Classifies a ``(pattern, flags)`` pair as **HW-eligible** or **fallback-only**.
The decision is a pure, deterministic, side-effect-free function of its inputs
(R8) and is cached by the routing layer (R4).

Design principle (spec §6 preamble): *when in doubt, fall back*.  Any construct
not explicitly enumerated as supported in §5.1 forces fallback.

The AST source is CPython's own regex parser (``re._parser`` / ``sre_parse``),
so eligibility is judged against exactly the structure CPython would compile.
"""

from __future__ import annotations

import re
import re._constants as _c
import re._parser as _sre
from typing import NamedTuple, Optional

# --- Capacity / capability limits (R11-R13, §7.4 caps) --------------------
# These are the advertised limits for the Phase-0 software model.  They meet
# the R13 minimums exactly (MAX_STATES >= 1024, MAX_PATTERNS >= 256,
# MAX_REPEAT >= 255).  A real device re-advertises these via the caps register
# block (R42/R45); software must not hard-code different values elsewhere.
MAX_STATES = 1024
MAX_PATTERNS = 256
MAX_REPEAT = 255
ALPHABET = 256
ENGINE_VERSION = 0x00010000  # packed 1.0.0, matches pyro_abi_version (R37)
ENGINE_KIND_MODEL = 0

# Flags whose semantics the compiler can faithfully encode (R9 inline/flags,
# R24/R25).  UNICODE is a no-op for str and is auto-added by the parser; any
# other flag bit (e.g. re.LOCALE, re.DEBUG) forces fallback (R25).
SUPPORTED_FLAGS = (
    re.ASCII | re.IGNORECASE | re.MULTILINE | re.DOTALL | re.VERBOSE | re.UNICODE
)

MAXREPEAT = _sre.MAXREPEAT

# Anchor opcodes that map to supported anchors (^ $ \A \Z \b \B and their
# unicode-boundary variants).  The LOCALE boundary variants are excluded and
# thus force fallback.
_SUPPORTED_AT = frozenset({
    _c.AT_BEGINNING, _c.AT_BEGINNING_LINE, _c.AT_BEGINNING_STRING,
    _c.AT_END, _c.AT_END_LINE, _c.AT_END_STRING,
    _c.AT_BOUNDARY, _c.AT_NON_BOUNDARY,
    _c.AT_UNI_BOUNDARY, _c.AT_UNI_NON_BOUNDARY,
})

# Shorthand-class categories that are supported (\d \D \w \W \s \S and their
# unicode variants).  LOCALE categories are excluded -> fallback.
_SUPPORTED_CATEGORY = frozenset({
    _c.CATEGORY_DIGIT, _c.CATEGORY_NOT_DIGIT,
    _c.CATEGORY_SPACE, _c.CATEGORY_NOT_SPACE,
    _c.CATEGORY_WORD, _c.CATEGORY_NOT_WORD,
    _c.CATEGORY_UNI_DIGIT, _c.CATEGORY_UNI_NOT_DIGIT,
    _c.CATEGORY_UNI_SPACE, _c.CATEGORY_UNI_NOT_SPACE,
    _c.CATEGORY_UNI_WORD, _c.CATEGORY_UNI_NOT_WORD,
    _c.CATEGORY_LINEBREAK, _c.CATEGORY_NOT_LINEBREAK,
    _c.CATEGORY_UNI_LINEBREAK, _c.CATEGORY_UNI_NOT_LINEBREAK,
})


class Classification(NamedTuple):
    eligible: bool
    reason: str
    states: Optional[int]  # estimated automaton state count, None if fallback


class _Reject(Exception):
    """Internal control-flow: a subtree is not HW-eligible."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def classify(pattern, flags: int = 0) -> Classification:
    """Classify ``(pattern, flags)``.  Pure function (R8).

    ``pattern`` may be ``str`` or ``bytes``/``bytearray``.  ``flags`` is an int
    or ``re.RegexFlag``.  The pattern is assumed already accepted by CPython;
    callers that need R30 semantics validate via ``re.compile`` first.
    """
    is_bytes = not isinstance(pattern, str)
    if not is_bytes:
        # A str pattern must be transportable as UTF-8 to the byte engine; a
        # lone surrogate (which stock re still compiles/matches) cannot be
        # encoded, so such a pattern is fallback-only (spec v1.2.1 / R14).
        try:
            pattern.encode("utf-8")
        except UnicodeEncodeError:
            return Classification(False, "pattern not UTF-8 encodable", None)
    try:
        parsed = _sre.parse(
            bytes(pattern) if is_bytes and not isinstance(pattern, bytes) else pattern,
            flags,
        )
    except re.error as exc:  # invalid pattern -> not our decision to make
        return Classification(False, f"parse error: {exc}", None)

    eff_flags = int(parsed.state.flags)
    if eff_flags & ~int(SUPPORTED_FLAGS):
        return Classification(False, "unsupported flag", None)

    ignorecase = bool(eff_flags & re.IGNORECASE)
    ascii_mode = bool(eff_flags & re.ASCII)

    try:
        states = _walk(list(parsed), is_bytes, ignorecase, ascii_mode)
    except _Reject as rej:
        return Classification(False, rej.reason, None)

    if states > MAX_STATES:
        return Classification(False, "exceeds MAX_STATES", None)
    return Classification(True, "eligible", states)


def _walk(seq, is_bytes: bool, ignorecase: bool, ascii_mode: bool) -> int:
    """Recursively validate a subpattern sequence and count automaton states.

    Raises ``_Reject`` on any unsupported construct or capacity violation.
    Returns the estimated number of states (bounded repeats expanded, R12).
    """
    total = 0
    for op, av in seq:
        total += _node_states(op, av, is_bytes, ignorecase, ascii_mode)
    return total


def _node_states(op, av, is_bytes, ignorecase, ascii_mode) -> int:
    if op in (_c.LITERAL, _c.NOT_LITERAL):
        _check_literal(av, is_bytes, ignorecase, ascii_mode)
        return 1
    if op is _c.ANY:
        return 1
    if op is _c.IN:
        _check_class(av, is_bytes, ignorecase, ascii_mode)
        return 1
    if op is _c.AT:
        if av not in _SUPPORTED_AT:
            raise _Reject("unsupported anchor")
        return 0
    if op is _c.BRANCH:
        _none, branches = av
        return sum(_walk(b, is_bytes, ignorecase, ascii_mode) for b in branches)
    if op is _c.SUBPATTERN:
        group, add_flags, del_flags, sub = av
        if (add_flags | del_flags) & ~int(SUPPORTED_FLAGS):
            raise _Reject("unsupported inline flag")
        sub_ic = (ignorecase or bool(add_flags & re.IGNORECASE)) and not (
            del_flags & re.IGNORECASE
        )
        sub_ascii = (ascii_mode or bool(add_flags & re.ASCII)) and not (
            del_flags & re.ASCII
        )
        return _walk(sub, is_bytes, sub_ic, sub_ascii)
    if op in (_c.MAX_REPEAT, _c.MIN_REPEAT):
        mn, mx, sub = av
        sub_states = _walk(sub, is_bytes, ignorecase, ascii_mode)
        if mx is MAXREPEAT:
            # Unbounded above (* + or {m,}): the limit applies to m (R9, R12).
            if mn is not MAXREPEAT and mn > MAX_REPEAT:
                raise _Reject("bounded repeat exceeds MAX_REPEAT")
            # A loop reuses its body states; no expansion.
            return sub_states
        if mx > MAX_REPEAT:
            raise _Reject("bounded repeat exceeds MAX_REPEAT")
        return sub_states * mx
    # Everything else is unsupported (R10): GROUPREF, GROUPREF_EXISTS,
    # ASSERT/ASSERT_NOT (lookaround), ATOMIC_GROUP, POSSESSIVE_REPEAT, etc.
    raise _Reject(f"unsupported construct: {op}")


def _check_literal(cp, is_bytes, ignorecase, ascii_mode):
    if is_bytes:
        return  # bytes IGNORECASE is ASCII-only, always encodable (R15)
    if ignorecase and not ascii_mode:
        # str + Unicode IGNORECASE: only simple (1:1) folding is HW-eligible.
        # A codepoint whose case fold is multi-character (e.g. 'ß' -> 'ss',
        # 'İ' -> 'i̇') needs full folding and is fallback-only (R15).
        if len(chr(cp).casefold()) != 1:
            raise _Reject("full case folding")


def _check_class(members, is_bytes, ignorecase, ascii_mode):
    for item in members:
        iop, iav = item
        if iop is _c.NEGATE:
            continue
        if iop is _c.LITERAL:
            _check_literal(iav, is_bytes, ignorecase, ascii_mode)
        elif iop is _c.RANGE:
            lo, hi = iav
            if not is_bytes and ignorecase and not ascii_mode:
                # Conservatively reject a range whose endpoints need full
                # folding; interior full-fold chars in wide ranges are not
                # deeply scanned (documented limitation).
                if len(chr(lo).casefold()) != 1 or len(chr(hi).casefold()) != 1:
                    raise _Reject("full case folding")
        elif iop is _c.CATEGORY:
            if iav not in _SUPPORTED_CATEGORY:
                raise _Reject("unsupported category")
        else:
            raise _Reject(f"unsupported class member: {iop}")
