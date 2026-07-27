"""SR1/SR2 rule triage: tier classification + anchor selection.

Implements spec snort-rule-offload §4.1 (SR1 parse + triage, SR2 sub-tier)
and §2 Definitions ("Anchor: the declared ``fast_pattern`` content if any,
else the longest positive content"), grounded in the corpus facts SF8-SF14.

Tier decision (SR1, over the positive — non-negated — content set ``P``):

- ``P`` empty and payload-dependent (pcre / byte_* / isdataat / bufferlen /
  dsize / negated content) → ``always-forward`` (SF9: never
  literal-prefilterable);
- ``P`` empty and not payload-dependent → ``header-only`` (SF8: 95 rules;
  ``dsize`` IS payload-dependent — it constrains payload length — which is
  what puts the three zero-content dsize rules, sids 279/375/381, in
  always-forward and makes SF8's icmp 79 / udp 5 breakdown come out);
- otherwise → ``anchor-compilable`` (SF9: any positive content, any length
  — SF10's minimum anchor is 1 byte — any buffer).

Sub-tier (SR2 — change-controlled: the raw/normalized boundary is the
soundness boundary for suppression; do not alter without spec amendment):
the buffer in effect at the *anchor* content decides, and there are
exactly TWO sub-tier values (spec §2/SR2).  ``pkt_data`` / ``raw_data``
→ ``raw-anchor``; every other sticky buffer (http_*, file_data,
dce_stub_data, base64_data, dns_query, sip_*, ...) →
``normalized-buffer`` — including the ``http_raw_*`` variants, which
still require the HTTP inspector to delimit and therefore are NOT
raw-anchor sound (SF11 rationale).  The 5 ``dce_stub_data`` rules are
inside ``normalized-buffer`` per SR2; they stay visible in the report's
``buffer_histogram``/``buffer_detail``, not as a third sub-tier.

Sticky-buffer state machine: state resets to ``pkt_data`` at each rule
start, every buffer option rewrites it for all subsequent payload options,
and ``pkt_data`` mid-rule returns to raw (SF11; mis-tracking shifts the
1,759/2,137 split — spec amendment 1.0.1).  A buffer key selects its
buffer in both the bare form and the valued form — Snort 3's
``http_header:field user-agent`` and ``http_param:"id"`` narrow the
cursor *within* the selected buffer (sid 42886's content is evaluated in
the normalized User-Agent field, hence normalized-buffer) — EXCEPT the
two sip valued forms (``sip_method:options``, ``sip_stat_code:4``),
which are genuine match options that test a value without moving the
cursor; those six sip-valued rules anchor in pkt_data (raw 1,759).

Triage is a total function (PYRO classify/explain doctrine): a line that
fails to parse becomes an ``always-forward`` result with a ``parse error``
reason — default-deny, never an exception.

Also usable as the SR1 CLI::

    .venv-pyro/bin/python3 -m pyro.snort.triage <rules-file> -o report.json

(``scripts/pyro_snort.py`` wraps the same entry point per repo CLI
conventions.)
"""

from __future__ import annotations

import re as _stdre
from typing import List, NamedTuple, Optional, Tuple

from . import rules as _rules
from .rules import Content, Option, Pcre, Rule, RuleParseError

# --- Option vocabularies (spec §4.1; methodology §1) -----------------------

#: Sticky-buffer options: keys that set the buffer for all subsequent
#: content/pcre/byte_* until changed (SF11).  Both the bare and the valued
#: form select the buffer (a value narrows the cursor within it, e.g.
#: ``http_header:field user-agent``) — except the keys in
#: :data:`VALUED_MATCH_OPTIONS`, whose valued form is a match option.
STICKY_BUFFERS = frozenset({
    "pkt_data", "raw_data",
    "http_uri", "http_raw_uri", "http_header", "http_raw_header",
    "http_client_body", "http_cookie", "http_raw_cookie", "http_method",
    "http_stat_code", "http_stat_msg", "http_raw_body", "http_raw_request",
    "http_raw_status", "http_trailer", "http_raw_trailer", "http_true_ip",
    "http_version", "http_param",
    "file_data", "base64_data", "dce_stub_data", "dns_query",
    "sip_method", "sip_header", "sip_body", "sip_stat_code", "sip_stat_msg",
    "js_data", "vba_data",
})

#: Buffers whose bytes are on the wire verbatim — the SR2 ``raw-anchor``
#: side of the soundness boundary.  http_raw_* are NOT here (SF11).
RAW_BUFFERS = frozenset({"pkt_data", "raw_data"})

#: STICKY_BUFFERS keys whose *valued* form is a payload match option, NOT
#: a buffer selection: ``sip_method:INVITE`` tests the request method and
#: ``sip_stat_code:4`` the status class; neither moves the cursor
#: (Snort 3 grammar; SR2 amendment 1.0.1).  Every other valued buffer key
#: (``http_header:field x``, ``http_param:"id"``, ``http_uri:path``, ...)
#: still selects its buffer — the value only narrows the cursor within it.
VALUED_MATCH_OPTIONS = frozenset({"sip_method", "sip_stat_code"})

#: Payload-dependent options besides content/pcre: their presence (with no
#: positive content) makes a rule ``always-forward``, never ``header-only``.
#: ``dsize`` is here — it is a predicate on the payload (its length), and
#: SF8's header-only breakdown (icmp 79/tcp 10/udp 5/ip 1) requires it.
PAYLOAD_OPTIONS = frozenset({
    "byte_test", "byte_jump", "byte_extract", "byte_math",
    "isdataat", "bufferlen", "base64_decode", "ber_data", "ber_skip",
    "dsize",
})

#: Pure metadata: never payload-dependent, never a dropped conjunct.
METADATA_OPTIONS = frozenset({
    "msg", "metadata", "reference", "classtype", "sid", "rev", "gid",
    "service", "priority", "rem",
})

# Tier / sub-tier labels (SR1/SR2 vocabulary — spec §2 "Tier").
TIER_HEADER_ONLY = "header-only"
TIER_ANCHOR = "anchor-compilable"
TIER_ALWAYS_FORWARD = "always-forward"
SUBTIER_RAW = "raw-anchor"
SUBTIER_NORMALIZED = "normalized-buffer"
# SR2 defines exactly these two sub-tier values.  The 5 dce_stub_data
# rules are normalized-buffer (spec §4.1: "2,131 rules + 5 dce_stub_data",
# amended 1.0.1 to 2,137 total); they remain visible per-buffer in the
# report's buffer_histogram/buffer_detail, never as a third sub-tier.


# --- Result model ----------------------------------------------------------


class Anchor(NamedTuple):
    """The rule's best positive literal (spec §2 "Anchor", SF9)."""

    pattern: bytes
    buffer: str                  # sticky buffer in effect at this content
    declared_fast_pattern: bool  # bare fast_pattern modifier on the anchor
    option_index: int            # position in the rule's option chain
    nocase: bool                 # ``nocase`` modifier on the anchor content

    @property
    def dedup_key(self) -> bytes:
        """SF9 unique-literal identity: a ``nocase`` anchor's identity is
        its ASCII-casefolded bytes (that is the literal the matcher
        actually recognizes); case-sensitive anchors are exact bytes."""
        return self.pattern.lower() if self.nocase else self.pattern

    @property
    def length(self) -> int:
        return len(self.pattern)


class ContentInfo(NamedTuple):
    """A content occurrence annotated with its sticky-buffer context."""

    content: Content
    buffer: str
    option_index: int


class PcreInfo(NamedTuple):
    """A pcre occurrence with its SF13 classification."""

    pcre: Pcre
    buffer: str
    option_index: int
    pcre_class: str              # clean | backref | lookaround | reject
    max_bounded_repeat: Optional[int]  # largest {n[,m]} bound, None if none


class TriageResult(NamedTuple):
    """Per-rule triage verdict (SR1 machine-readable report row)."""

    tier: str
    subtier: Optional[str]       # only for anchor-compilable
    anchor: Optional[Anchor]
    reason: str
    contents: Tuple[ContentInfo, ...]
    pcres: Tuple[PcreInfo, ...]
    dropped_options: Tuple[str, ...]  # SR3 dropped-conjunct list
    warnings: Tuple[str, ...]


# --- pcre classification (SF13) --------------------------------------------

# Constructs the SF6 engine can never take (spec SF13 "zero
# possessive/atomic/recursion/conditional" — assert, don't assume).
_RECURSION_RE = _stdre.compile(r"^(?:R|\d+|&\w+|P>\w+)\)")


def _scan_pcre_body(body: str) -> Tuple[bool, bool, bool, Optional[int]]:
    """Single escape/class-aware scan of a pcre body.

    Returns ``(has_backref, has_lookaround, has_reject_construct,
    max_bounded_repeat)``.  Classes are detected disjointly by the caller
    with precedence backref → lookaround → reject → clean (edge-case #12).
    """
    has_backref = False
    has_lookaround = False
    has_reject = False
    max_repeat: Optional[int] = None
    i = 0
    n = len(body)
    in_class = False
    prev_quantifiable_quant = False  # previous token was * + ? or {n,m}
    while i < n:
        ch = body[i]
        if ch == "\\":
            nxt = body[i + 1] if i + 1 < n else ""
            if not in_class and nxt.isdigit() and nxt != "0":
                has_backref = True  # \1..\9 (inside a class it's octal)
            elif not in_class and nxt == "g":
                has_backref = True  # \g{n} / \g<n>
            elif not in_class and nxt == "k":
                has_backref = True  # named backref \k<name>/\k{name}/\k'name'
            i += 2
            prev_quantifiable_quant = False
            continue
        if in_class:
            if ch == "]":
                in_class = False
            i += 1
            continue
        if ch == "[":
            in_class = True
            i += 1
            prev_quantifiable_quant = False
            continue
        if ch == "(" and body.startswith("(?", i):
            rest = body[i + 2:i + 6]
            if rest[:1] in ("=", "!") or rest[:2] in ("<=", "<!"):
                has_lookaround = True
            elif rest[:1] == ">":
                has_reject = True  # atomic group
            elif rest[:1] == "(":
                has_reject = True  # conditional (?(
            elif rest[:2] == "P=":
                has_backref = True  # (?P=name)
            elif _RECURSION_RE.match(body[i + 2:i + 8] + ")"):
                # (?R) (?1) (?&name) (?P>name) — recursion/subroutine
                if _RECURSION_RE.match(body[i + 2:]):
                    has_reject = True
            i += 2
            prev_quantifiable_quant = False
            continue
        if ch == "{":
            m = _stdre.match(r"\{(\d+)(?:,(\d*))?\}", body[i:])
            if m:
                lo = int(m.group(1))
                hi = m.group(2)
                bound = lo if hi in (None, "") else int(hi)
                if hi == "":
                    bound = lo  # {n,} — unbounded; count the floor
                if max_repeat is None or bound > max_repeat:
                    max_repeat = bound
                i += m.end()
                # a bounded repeat is itself a quantifier
                if i < n and body[i] == "+" and body[i - 1] == "}":
                    has_reject = True  # possessive {..}+
                prev_quantifiable_quant = True
                continue
            i += 1
            prev_quantifiable_quant = False
            continue
        if ch in "*+?":
            if prev_quantifiable_quant and ch == "+":
                has_reject = True  # possessive *+ ++ ?+
                prev_quantifiable_quant = False
            else:
                prev_quantifiable_quant = ch in "*+?"
            i += 1
            continue
        prev_quantifiable_quant = False
        i += 1
    return has_backref, has_lookaround, has_reject, max_repeat


def classify_pcre(body: str) -> Tuple[str, Optional[int]]:
    """Classify one pcre occurrence per SF13.

    Precedence backref → lookaround → reject → clean makes the classes
    partition (799 + 239 + 42 + 0 = 1,080 on the profiled corpus).
    Returns ``(class, max_bounded_repeat)``.
    """
    has_backref, has_lookaround, has_reject, max_rep = _scan_pcre_body(body)
    if has_backref:
        return "backref", max_rep
    if has_lookaround:
        return "lookaround", max_rep
    if has_reject:
        return "reject", max_rep
    return "clean", max_rep


# --- Triage ----------------------------------------------------------------


def analyze_options(rule: Rule):
    """Replay the option chain through the sticky-buffer state machine.

    Returns ``(contents, pcres, payload_dep, warnings)``.
    """
    buffer = "pkt_data"  # initial state at rule start (SF11)
    contents: List[ContentInfo] = []
    pcres: List[PcreInfo] = []
    payload_dep = False
    warnings: List[str] = []
    for idx, opt in enumerate(rule.options):
        key = opt.key
        if key in STICKY_BUFFERS:
            if opt.value is not None and key in VALUED_MATCH_OPTIONS:
                payload_dep = True  # sip_method:/sip_stat_code: match option
            else:
                # Bare form, or a valued form that narrows the cursor
                # within its buffer (http_header:field ..., http_param:"x",
                # ...): both SELECT the buffer (SR2 amendment 1.0.1).
                buffer = key
        elif key == "content":
            c = _rules.parse_content(opt.value or "")
            contents.append(ContentInfo(c, buffer, idx))
            payload_dep = True
        elif key == "pcre":
            p = _rules.parse_pcre(opt.value or "")
            cls, max_rep = classify_pcre(p.pattern)
            pcres.append(PcreInfo(p, buffer, idx, cls, max_rep))
            payload_dep = True
            warnings.extend(p.warnings)
        elif key in PAYLOAD_OPTIONS:
            payload_dep = True
        # Fallthrough: flow/flowbits/itype/icode/icmp_id/icmp_seq/flags/
        # ttl/fragbits/ip_proto/seq/ack/window/ssl_*/dce_iface/... and all
        # metadata leave payload_dep unchanged.  NOTE: ``dsize`` is NOT
        # here — it is in PAYLOAD_OPTIONS above (a predicate on payload
        # length, hence payload-dependent; SF8's 95 header-only count and
        # the 26 always-forward depend on that classification).
    return tuple(contents), tuple(pcres), payload_dep, tuple(warnings)


def select_anchor(contents: Tuple[ContentInfo, ...],
                  warnings: List[str]) -> Optional[Anchor]:
    """Pick the anchor per spec §2: declared fast_pattern if any, else the
    longest positive content in ANY buffer; ties break to first in rule
    order (SR1 determinism, edge-case #8)."""
    positives = [ci for ci in contents if not ci.content.negated]
    if not positives:
        return None
    declared = [ci for ci in positives if ci.content.fast_pattern]
    negated_fp = [ci for ci in contents
                  if ci.content.negated and ci.content.fast_pattern]
    if negated_fp:
        warnings.append("fast_pattern on negated content ignored "
                        "(rule error in Snort; anchor must be positive)")
    def _nocase(ci: ContentInfo) -> bool:
        return any(name == "nocase" for name, _ in ci.content.modifiers)

    if declared:
        if len(declared) > 1:
            warnings.append("multiple declared fast_pattern contents; "
                            "taking the first (Snort permits at most one)")
        chosen = declared[0]
        return Anchor(chosen.content.pattern, chosen.buffer, True,
                      chosen.option_index, _nocase(chosen))
    # Deterministic tie-break (SR1, edge-case #8): scan in rule order with a
    # strict-greater compare, keeping the FIRST of the longest.
    best = positives[0]
    for ci in positives[1:]:
        if len(ci.content.pattern) > len(best.content.pattern):
            best = ci
    return Anchor(best.content.pattern, best.buffer, False,
                  best.option_index, _nocase(best))


def _dropped_conjuncts(rule: Rule, anchor: Optional[Anchor]) -> Tuple[str, ...]:
    """SR3 dropped-conjunct list: every detection option the prefilter
    circuit does not implement — i.e. everything that is not metadata, not
    a sticky-buffer selector, and not the anchor content itself."""
    dropped: List[str] = []
    for idx, opt in enumerate(rule.options):
        if opt.key in METADATA_OPTIONS or opt.key in STICKY_BUFFERS:
            continue
        if anchor is not None and idx == anchor.option_index:
            continue
        dropped.append(opt.key)
    return tuple(dropped)


def triage_rule(rule: Rule) -> TriageResult:
    """Classify one rule into exactly one tier (SR1). Total function."""
    if rule.parse_error is not None:
        # File-level default-deny: parse_rules_file could not parse the
        # line (bad header, unterminated quote, ...) — always-forward.
        return TriageResult(TIER_ALWAYS_FORWARD, None, None,
                            "parse error: %s" % rule.parse_error,
                            (), (), (), ())
    try:
        contents, pcres, payload_dep, warns = analyze_options(rule)
    except RuleParseError as exc:  # default-deny at the boundary
        return TriageResult(TIER_ALWAYS_FORWARD, None, None,
                            "parse error: %s" % exc.reason,
                            (), (), (), ())
    warnings = list(warns)
    anchor = select_anchor(contents, warnings)
    if anchor is None:
        if payload_dep:
            kinds = []
            if pcres:
                kinds.append("pcre")
            if any(ci.content.negated for ci in contents):
                kinds.append("negated content")
            if any(o.key in PAYLOAD_OPTIONS for o in rule.options):
                kinds.append("byte/payload test")
            reason = ("payload-dependent without a positive content anchor"
                      " (%s)" % ", ".join(kinds or ["payload option"]))
            return TriageResult(TIER_ALWAYS_FORWARD, None, None, reason,
                                contents, pcres,
                                _dropped_conjuncts(rule, None),
                                tuple(warnings))
        return TriageResult(TIER_HEADER_ONLY, None, None,
                            "decidable from L3/L4 header predicate alone",
                            contents, pcres, (), tuple(warnings))
    # SR2: exactly two sub-tiers; dce_stub_data is normalized-buffer.
    subtier = (SUBTIER_RAW if anchor.buffer in RAW_BUFFERS
               else SUBTIER_NORMALIZED)
    reason = ("anchor %d B in %s (%s)"
              % (anchor.length, anchor.buffer,
                 "declared fast_pattern" if anchor.declared_fast_pattern
                 else "longest positive content"))
    return TriageResult(TIER_ANCHOR, subtier, anchor, reason,
                        contents, pcres, _dropped_conjuncts(rule, anchor),
                        tuple(warnings))


def triage_file(path: str) -> List[Tuple[Rule, TriageResult]]:
    """Parse + triage a whole rules file (SR1). Deterministic in input."""
    out: List[Tuple[Rule, TriageResult]] = []
    for rule in _rules.parse_rules_file(path):
        out.append((rule, triage_rule(rule)))
    return out


# --- CLI (python -m pyro.snort.triage; scripts/pyro_snort.py wraps this) ---


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import json
    import sys

    from . import report as _report

    ap = argparse.ArgumentParser(
        prog="python -m pyro.snort.triage",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("rules_file", help="Snort 3 rules file")
    ap.add_argument("-o", "--output", metavar="REPORT.json", default=None,
                    help="write the machine-readable SR1 triage report here")
    ap.add_argument("--no-rules", action="store_true",
                    help="omit the per-rule section (aggregates only)")
    args = ap.parse_args(argv)

    rep = _report.build_report(args.rules_file,
                               include_rules=not args.no_rules)
    text = json.dumps(rep, indent=2, sort_keys=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        sys.stdout.write(text + "\n")

    agg = rep["aggregates"]
    sys.stderr.write(
        "triage: %d rules -> header-only %d / anchor-compilable %d / "
        "always-forward %d\n" % (
            rep["corpus"]["rule_count"],
            agg["tiers"][TIER_HEADER_ONLY],
            agg["tiers"][TIER_ANCHOR],
            agg["tiers"][TIER_ALWAYS_FORWARD]))
    failures = rep.get("invariant_failures", [])
    if failures:
        for f in failures:
            sys.stderr.write("INVARIANT FAILED: %s\n" % f)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
