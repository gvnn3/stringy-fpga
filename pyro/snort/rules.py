"""Snort 3 rule tokenizer/parser (spec snort-rule-offload §3 Stage 1, SR1).

Parses one Snort 3 rule per the grammar observed in the community corpus
(spec SF8-SF16 profile): ``action proto src_net src_port dir dst_net
dst_port ( option; option; ... )``.  The parser is deliberately *lossless*
and *deterministic* (SR1): it produces the header predicate plus the
**ordered** option chain with raw values preserved — option order is
semantic in Snort 3 (sticky buffers, relative modifiers), so the order is
part of the parse result, and triage (:mod:`pyro.snort.triage`) replays it.

Payload-bearing options get dedicated decoders here because their decoded
form is what triage measures (spec §2 "Anchor", SF9/SF10):

- ``content`` — Snort 3 form: ``content:[!]"pattern",mod,mod...`` with the
  modifiers comma-suffixed *inside* the option (not separate ``;`` options
  as in Snort 2).  ``|xx xx|`` hex runs are decoded to bytes, ``\\x``
  escapes are honored, and the bare ``fast_pattern`` modifier is
  distinguished from ``fast_pattern_offset``/``fast_pattern_length``
  (the substring trap that overcounts declared anchors).
- ``pcre`` — ``pcre:[!]"/re/flags"``; trailing flags are parsed after the
  final delimiter.  Snort 2 buffer letters (``UHPIDMCKSYB``) do not exist
  in Snort 3; if seen they are surfaced as a parse *warning*, never an
  error (buffer identity comes from the sticky-buffer state in triage).

Errors raise :class:`RuleParseError` from the single-rule entry points
(:func:`parse_rule`, :func:`parse_content`, :func:`parse_pcre`); the
file-level entry point :func:`parse_rules_file` is **total** — a line that
fails to parse becomes a :class:`Rule` with ``parse_error`` set, so one
malformed line in an SF15 ruleset drop can never abort the whole file.
The triage layer converts both shapes to an ``always-forward`` result
value at the boundary (PYRO "total function" doctrine, R31 shape).
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional, Tuple

# --- Data model ------------------------------------------------------------


class RuleParseError(ValueError):
    """A line is not a parseable Snort 3 rule.

    Carries a ``.reason`` attribute (repo idiom, cf. ``pyro._classify._Reject``);
    callers at the boundary convert it to a result value, never leak it.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Option(NamedTuple):
    """One option from the ordered chain: ``key[:value]`` (raw value)."""

    key: str
    value: Optional[str]  # raw text after ':', stripped; None for bare keys


class Rule(NamedTuple):
    """One parsed rule: header predicate + ordered option chain (SR1)."""

    action: str
    proto: str
    src_net: str
    src_port: str
    direction: str  # "->" or "<>"
    dst_net: str    # syntactic RHS (counts as dst even for "<>", SF8)
    dst_port: str
    options: Tuple[Option, ...]
    line_no: int    # 1-based line in the source file
    raw: str        # the full source line, stripped
    headerless: bool = False  # Snort 3 service rule "alert http ( ... )"
    parse_error: Optional[str] = None  # set by parse_rules_file on a bad line


class Content(NamedTuple):
    """A decoded ``content`` option (Snort 3 comma-modifier form)."""

    pattern: bytes          # fully decoded: printables + one byte per hex pair
    negated: bool           # content:!"..."
    modifiers: Tuple[Tuple[str, Optional[str]], ...]  # ordered (name, arg)
    fast_pattern: bool      # bare ``fast_pattern`` modifier only


class Pcre(NamedTuple):
    """A parsed ``pcre`` option (pattern body kept verbatim)."""

    pattern: str            # regex body between the ``/`` delimiters, raw
    flags: str              # trailing flag letters after the final delimiter
    negated: bool           # pcre:!"..."
    relative: bool          # Snort ``R`` flag present (SF13)
    warnings: Tuple[str, ...]  # e.g. legacy Snort 2 buffer letters seen


# Snort 2 buffer-selection flag letters; Snort 3 dropped them (buffer comes
# from the sticky buffer in effect).  Presence => warning, not error.
_LEGACY_PCRE_BUFFER_LETTERS = frozenset("UHPIDMCKSYB")
# PCRE letters + Snort 3 extras that are legal on a pcre option.
_KNOWN_PCRE_LETTERS = frozenset("ismxAEGRO")


# --- Low-level scanners ----------------------------------------------------


def _split_header_body(line: str) -> Tuple[str, str]:
    """Split ``header ( body )`` — parens delimit the option chain."""
    try:
        i = line.index("(")
        j = line.rindex(")")
    except ValueError:
        raise RuleParseError("missing option parentheses")
    if j < i:
        raise RuleParseError("mismatched option parentheses")
    return line[:i], line[i + 1:j]


def _split_header_fields(header: str) -> List[str]:
    """Whitespace-split the header, but never inside ``[...]`` lists."""
    fields: List[str] = []
    cur: List[str] = []
    depth = 0
    for ch in header:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        if ch.isspace() and depth == 0:
            if cur:
                fields.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        fields.append("".join(cur))
    return fields


def split_options(body: str) -> List[str]:
    """Split the option chain on ``;`` — quote-aware, escape-aware.

    ``;`` is legal inside the double-quoted strings of ``msg``/``content``/
    ``pcre`` (edge-case #2 of the triage methodology), so a naive
    ``split(';')`` corrupts the chain.  Backslash escapes (``\\"`` ``\\\\``)
    are honored inside quotes.
    """
    out: List[str] = []
    cur: List[str] = []
    in_quote = False
    escaped = False
    for ch in body:
        if escaped:
            cur.append(ch)
            escaped = False
            continue
        if ch == "\\":
            cur.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_quote = not in_quote
            cur.append(ch)
            continue
        if ch == ";" and not in_quote:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    if in_quote:
        raise RuleParseError("unterminated quoted string in option chain")
    tail = "".join(cur)
    if tail.strip():
        out.append(tail)
    return [o.strip() for o in out if o.strip()]


def _scan_quoted(s: str, start: int) -> Tuple[str, int]:
    """Scan a double-quoted string starting at ``s[start] == '\"'``.

    Returns ``(inner_raw, index_after_closing_quote)``.  The inner text is
    returned *verbatim* (escapes preserved) — ``content`` decoding and
    ``pcre`` bodies each interpret escapes their own way.
    """
    if start >= len(s) or s[start] != '"':
        raise RuleParseError("expected opening quote")
    i = start + 1
    buf: List[str] = []
    while i < len(s):
        ch = s[i]
        if ch == "\\":
            if i + 1 >= len(s):
                raise RuleParseError("dangling backslash in quoted string")
            buf.append(ch)
            buf.append(s[i + 1])
            i += 2
            continue
        if ch == '"':
            return "".join(buf), i + 1
        buf.append(ch)
        i += 1
    raise RuleParseError("unterminated quoted string")


# --- content decoding ------------------------------------------------------

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def decode_content_pattern(raw: str) -> bytes:
    """Decode a raw (in-quotes) content pattern to bytes.

    ``|xx xx|`` hex runs contribute one byte per pair (whitespace inside the
    pipes ignored, case-insensitive); ``\\c`` escapes yield the literal
    character; everything else is the character's byte value.  Decoded
    length — printable bytes + one byte per hex pair — is the SF10 anchor
    length metric.
    """
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\":
            if i + 1 >= n:
                raise RuleParseError("dangling backslash in content")
            code = ord(raw[i + 1])
            if code > 0xFF:
                raise RuleParseError(
                    "non-byte character %r in content" % raw[i + 1])
            out.append(code)
            i += 2
        elif ch == "|":
            j = raw.find("|", i + 1)
            if j < 0:
                raise RuleParseError("unterminated hex run in content")
            hexrun = "".join(raw[i + 1:j].split())
            if len(hexrun) % 2 or not set(hexrun) <= _HEX_DIGITS:
                raise RuleParseError("malformed hex run %r" % raw[i + 1:j])
            out.extend(bytes.fromhex(hexrun))
            i = j + 1
        else:
            # A rules file is bytes on disk; anything that does not fit in
            # one byte (non-Latin-1 unicode, or a \udcXX surrogate from the
            # surrogateescape file read of raw non-UTF-8 bytes) is a parse
            # error — RuleParseError, so triage degrades the rule to
            # always-forward instead of crashing on a plain ValueError.
            code = ord(ch)
            if code > 0xFF:
                raise RuleParseError("non-byte character %r in content" % ch)
            out.append(code)
            i += 1
    return bytes(out)


def parse_content(value: str) -> Content:
    """Parse a ``content`` option value (Snort 3 comma-modifier form)."""
    s = value.strip()
    negated = s.startswith("!")
    if negated:
        s = s[1:].lstrip()
    inner, end = _scan_quoted(s, 0)
    mods: List[Tuple[str, Optional[str]]] = []
    fast_pattern = False
    rest = s[end:].strip()
    if rest:
        if not rest.startswith(","):
            raise RuleParseError("junk after content pattern: %r" % rest)
        for piece in rest[1:].split(","):
            piece = piece.strip()
            if not piece:
                continue
            parts = piece.split(None, 1)
            name = parts[0]
            arg = parts[1].strip() if len(parts) > 1 else None
            mods.append((name, arg))
            # Exact-token compare: ``fast_pattern_offset``/``_length`` must
            # NOT count as a declared fast_pattern (substring trap; the
            # corpus has 2,178 raw token hits but only 2,168 bare mods).
            if name == "fast_pattern" and arg is None:
                fast_pattern = True
    return Content(decode_content_pattern(inner), negated, tuple(mods),
                   fast_pattern)


def parse_pcre(value: str) -> Pcre:
    """Parse a ``pcre`` option value: ``[!]"/re/flags"``."""
    s = value.strip()
    negated = s.startswith("!")
    if negated:
        s = s[1:].lstrip()
    inner, end = _scan_quoted(s, 0)
    if s[end:].strip():
        raise RuleParseError("junk after pcre string: %r" % s[end:])
    if not inner.startswith("/"):
        raise RuleParseError("pcre must start with '/' delimiter")
    last = inner.rfind("/")
    if last == 0:
        raise RuleParseError("pcre missing closing delimiter")
    body = inner[1:last]
    flags = inner[last + 1:]
    warnings: List[str] = []
    for letter in flags:
        if letter in _LEGACY_PCRE_BUFFER_LETTERS and letter not in _KNOWN_PCRE_LETTERS:
            warnings.append("legacy Snort 2 buffer flag %r ignored "
                            "(buffer comes from sticky-buffer state)" % letter)
        elif letter not in _KNOWN_PCRE_LETTERS:
            warnings.append("unknown pcre flag %r" % letter)
    return Pcre(body, flags, negated, "R" in flags, tuple(warnings))


# --- Rule-level parse ------------------------------------------------------

_DIRECTIONS = frozenset({"->", "<>"})


def parse_rule(line: str, line_no: int = 0) -> Rule:
    """Parse one rule line into a :class:`Rule` (SR1: deterministic)."""
    raw = line.strip()
    header, body = _split_header_body(raw)
    fields = _split_header_fields(header)
    headerless = False
    if len(fields) == 2:
        # Snort 3 headerless service rule: ``alert http ( ... )`` — nets
        # and ports default to ``any``, direction to ``->``.
        action, proto = fields
        src_net = src_port = dst_net = dst_port = "any"
        direction = "->"
        headerless = True
    elif len(fields) == 7:
        (action, proto, src_net, src_port,
         direction, dst_net, dst_port) = fields
    else:
        raise RuleParseError(
            "expected 2 or 7 header fields, got %d: %r"
            % (len(fields), fields))
    if direction not in _DIRECTIONS:
        raise RuleParseError("bad direction token %r" % direction)
    options: List[Option] = []
    for opt in split_options(body):
        if ":" in opt:
            key, val = opt.split(":", 1)
            options.append(Option(key.strip(), val.strip()))
        else:
            options.append(Option(opt.strip(), None))
    return Rule(action, proto, src_net, src_port, direction, dst_net,
                dst_port, tuple(options), line_no, raw, headerless)


def _parse_rule_total(line: str, line_no: int) -> Rule:
    """Total per-line parse: a :class:`RuleParseError` becomes a Rule with
    ``parse_error`` set (empty header/options, raw preserved) so the file
    parse never aborts — triage classifies it ``always-forward`` (SR1
    default-deny)."""
    try:
        return parse_rule(line, line_no)
    except RuleParseError as exc:
        return Rule("", "", "", "", "->", "", "", (), line_no,
                    line.strip(), False, exc.reason)


def parse_rules_file(path: str) -> List[Rule]:
    """Parse a rules file: one rule per line, ``#`` comments and blank
    lines skipped, ``\\`` line-continuations joined (SF15 future-proofing —
    the current community corpus needs none of these, but wholesale ruleset
    replacement must not break the parser).

    **Total at the file level**: a line that fails header/option-chain
    parse yields a Rule with ``parse_error`` set instead of raising, so
    one odd line in a future ruleset drop never zeroes the whole file
    (SF15; the triage layer turns such rules into ``always-forward``)."""
    rules: List[Rule] = []
    pending = ""
    pending_start = 0
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as fh:
        for line_no, line in enumerate(fh, 1):
            text = line.rstrip("\n")
            if not pending:
                stripped = text.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                pending_start = line_no
            if text.rstrip().endswith("\\"):
                pending += text.rstrip()[:-1]
                continue
            pending += text
            rules.append(_parse_rule_total(pending, pending_start))
            pending = ""
    if pending.strip():
        rules.append(_parse_rule_total(pending, pending_start))
    return rules
