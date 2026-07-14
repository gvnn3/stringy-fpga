"""THE R60 transparency-regression corpus (AC-3-1).

R60 requires the suite to pass "on a corpus of real ``re``-using programs" but
deliberately does NOT enumerate that corpus.  THIS MODULE IS THE CORPUS
DEFINITION: the programs registered in ``REGISTRY`` below are the normative
R60 corpus for this repository.  Adding/removing a program here changes what
AC-3-1 certifies.

What a corpus program is
------------------------
Each program is a REAL PROGRAM, not a bag of expressions: a deterministic
callable that takes the ``re`` MODULE as its only parameter (stock ``re`` or
the same module patched in place by ``pyro.install()`` — R34 rebinds the
module attributes, so one module object serves both modes) and yields the
program's work as ``(label, thunk)`` steps.  Driving a program with
:func:`run_program` therefore turns it into exactly what AC-3-1 needs: a
deterministic callable from the ``re`` module to the FULL per-call output
sequence, where a call's output is its return VALUE or the EXCEPTION it
raised (caught and canonicalised, never swallowed).  The out-of-process
harness (``phase3_workers.cmd_corpus``) drives the same generators step by
step so it can interleave ``explain()``/``stats()`` snapshots.

Programs assert NOTHING about tiers, engines, or stats — every assertion
(including the positive mid-run tier-transition assertion that keeps AC-3-1
non-vacuous) is made by the parent test over the recorded sequences.

The tier-transition vehicle
---------------------------
``r60_log_scan`` is the corpus's tier-transition vehicle (AC-3-1's "patterns
that transition tiers mid-run" clause): it compiles ONE pattern once and scans
a >= 64 KiB log with it many times (compile-once-use-many), so every
single-match dispatch crosses R51 step 4 by SIZE.  Run with ``PYRO_N_SYNTH=2``
(R4a env knob) it launches background synthesis on the second eligible
dispatch; its ``AWAIT_TIER`` step lets the parent-supplied poll (installed
mode only; a no-op under stock ``re``) wait for the mock toolchain to finish,
and the calls AFTER it run against the warm/resident circuit.  Identical
outputs are required on BOTH sides of that flip (R36/R53/R60).

Deliberate exclusions (trap: known, spec-accepted non-defects)
--------------------------------------------------------------
The following observable differences are KNOWN and DELIBERATE per the spec;
changing any of them would require a spec amendment, not a code fix, so no
corpus program may observe them (a program that did would fail the suite on a
non-defect):

  * the concrete match type name (``HybridMatch`` is not ``re.Match``;
    ``isinstance(m, re.Match)`` is carved out by R36a, but ``type(m).__name__``
    is not part of the drop-in contract);
  * ``m.re`` (the pattern object backing a match) and Pattern-object identity;
  * ``repr(m)`` / ``repr(pattern)``;
  * pickling of Match/Pattern objects.

Programs may freely observe span/group/groups/groupdict/lastindex/lastgroup/
``m[...]``/start/end, returned strings/lists/tuples, and exception type +
``str(e)`` + (for ``re.error``) ``.msg``/``.pos``/``.pattern`` — those ARE the
contract (R16, R28, R30, R36).

Determinism: no randomness, no clocks, no environment reads — every program
produces byte-identical step labels and values on every run.
"""

from re import MULTILINE  # flag CONSTANTS only: plain ints, identical in both
                          # stock re and pyro.re (R26); importing them creates
                          # no call-path dependence on either implementation.

S_MIN = 64 * 1024  # R2 / R51 step 4 size threshold (spec constant)

# Sentinel step: the harness (installed mode only) deadline-polls for the
# program's key pattern to reach warm/resident here; a no-op under stock re.
# Value-compatible with phase3_workers' AWAIT_TIER (checked via step[0]).
AWAIT_TIER = ("await_tier",)


def run_program(prog, re_mod, *, canon=None, canon_exc=None, on_await=None):
    """Drive corpus program ``prog`` against ``re_mod``; return the FULL
    per-call output sequence.

    Returns ``[{"label": L, "value": V} | {"label": L, "exc": E}, ...]`` — one
    record per call, in program order; an exception raised by a call IS that
    call's output (caught here, canonicalised via ``canon_exc``).  ``canon``
    canonicalises values (e.g. ``phase3_workers._json_safe`` for Match
    objects); both default to identity-ish forms.  ``on_await(n_calls_done)``
    is invoked at each AWAIT_TIER step (the out-of-process harness supplies
    the tier poll there; in-process/stock callers may omit it).
    """
    if canon is None:
        canon = lambda v: v
    if canon_exc is None:
        canon_exc = lambda e: {"type": type(e).__name__, "str": str(e)}
    out = []
    for step in prog(re_mod):
        if step == AWAIT_TIER:
            if on_await is not None:
                on_await(len(out))
            continue
        label, thunk = step
        try:
            out.append({"label": label, "value": canon(thunk())})
        except Exception as e:
            out.append({"label": label, "exc": canon_exc(e)})
    return out


# ==========================================================================
# 1. r60_log_scan — log-scanning loop; compile-once-use-many over >= 64 KiB.
#    THE tier-transition vehicle.
# ==========================================================================
LOG_PATTERN = r"(?P<verb>GET|POST|PUT) (?P<path>/[\w./-]+) (?P<status>\d{3})"


def _build_log():
    """Deterministic ~70 KiB synthetic access log (>= S_min, R51 step 4)."""
    verbs = ("GET", "POST", "PUT")
    paths = ("/index.html", "/api/v1/items", "/static/app.js", "/login",
             "/health/ready")
    statuses = (200, 304, 404, 500)
    lines = []
    for i in range(1300):
        lines.append(
            f"{verbs[i % 3]} {paths[i % 5]} {statuses[i % 4]} "
            f"{(i * 7) % 900}ms req={i:05d} host=web{i % 7:02d} "
            f"user=u{i % 13:03d}\n")
    return "".join(lines)


BIG_LOG = _build_log()
assert len(BIG_LOG) >= S_MIN, "BIG_LOG must be >= 64 KiB (R51 step 4 by size)"


def _verb_tally(scanner, text):
    """Real log-analysis logic: count requests per verb via finditer."""
    tally = {}
    for m in scanner.finditer(text):
        v = m.group("verb")
        tally[v] = tally.get(v, 0) + 1
    return tally


def prog_r60_log_scan(re):
    """Access-log scanner.  One compiled pattern, many big-subject uses.

    The pre-AWAIT_TIER section makes >= 2 single-match dispatches over the
    >= 64 KiB subject (search/match/finditer are the ops that reach the R4a
    launch consultation), so PYRO_N_SYNTH=2 launches synthesis before the
    await; the post section re-runs the same analyses warm/resident.
    """
    scanner = re.compile(LOG_PATTERN)  # compile once (R4 reuse)
    yield "search.first.pre", lambda: scanner.search(BIG_LOG)
    yield "match.head.pre", lambda: scanner.match(BIG_LOG)
    yield "tally.verbs.pre", lambda: _verb_tally(scanner, BIG_LOG)
    yield "findall.groups.pre", lambda: scanner.findall(BIG_LOG)
    yield "fullmatch.line.pre", lambda: scanner.fullmatch(
        "POST /api/v1/items 500")
    yield AWAIT_TIER
    yield "search.first.post", lambda: scanner.search(BIG_LOG)
    yield "match.head.post", lambda: scanner.match(BIG_LOG)
    yield "tally.verbs.post", lambda: _verb_tally(scanner, BIG_LOG)
    yield "findall.groups.post", lambda: scanner.findall(BIG_LOG)
    yield "sub.redact.post", lambda: scanner.sub(r"\g<verb> <path> \g<status>",
                                                 BIG_LOG, 50)
    yield "subn.count.post", lambda: scanner.subn("HIT", BIG_LOG)[1]
    yield "split.stanza.post", lambda: scanner.split(BIG_LOG[:200])
    yield "search.miss.post", lambda: scanner.search("no request lines here")


# ==========================================================================
# 2. r60_tokenizer — a real finditer-driven tokenizer with named alternation.
# ==========================================================================
TOKEN_PATTERN = (r"(?P<num>\d+(?:\.\d+)?)|(?P<name>[A-Za-z_]\w*)"
                 r"|(?P<op>[-+*/=<>()])|(?P<ws>[ \t]+)|(?P<junk>.)")

_SOURCES = (
    "rate = 12 * (base + 7)",
    "x2 = x1 / 3.5 - offset",
    "total=price*qty",
    "if depth > 3 then stop",
    "bad ~ line $ with junk",
)


def _tokenize(re, line):
    toks = []
    for m in re.finditer(TOKEN_PATTERN, line):
        kind = m.lastgroup
        if kind == "ws":
            continue
        toks.append([kind, m.group()])
    return toks


def prog_r60_tokenizer(re):
    """Tokenizer: named-alternation pattern, lastgroup-driven token stream."""
    for i, src in enumerate(_SOURCES):
        yield f"tokens.{i}", (lambda src=src: _tokenize(re, src))
    yield "kinds.histogram", lambda: {
        kind: sum(1 for src in _SOURCES for k, _ in _tokenize(re, src)
                  if k == kind)
        for kind in ("num", "name", "op", "junk")}
    yield "junk.positions", lambda: [
        [i, m.start(), m.group()]
        for i, src in enumerate(_SOURCES)
        for m in re.finditer(TOKEN_PATTERN, src) if m.lastgroup == "junk"]


# ==========================================================================
# 3. r60_config_parser — INI-style parser: anchors, named groups, MULTILINE.
# ==========================================================================
CONF_TEXT = """\
# deployment config
[server]
host = 10.0.0.5
port = 8080
; alt comment style
timeout=30

[auth]
user = admin
retries = 3

[paths]
log_dir = /var/log/app
"""

_SECTION_RE = r"\[(?P<name>[^\]]+)\]\s*"
_KV_RE = r"(?P<key>\w+)\s*=\s*(?P<val>\S+)\s*"
_COMMENT_RE = r"\s*[#;]"


def _parse_conf(re):
    """Real parse pass: build {section: {key: val}} from CONF_TEXT."""
    conf, section = {}, None
    for line in CONF_TEXT.splitlines():
        if not line.strip() or re.match(_COMMENT_RE, line):
            continue
        if m := re.fullmatch(_SECTION_RE, line):
            section = m.group("name")
            conf[section] = {}
        elif (m := re.fullmatch(_KV_RE, line)) and section is not None:
            conf[section][m.group("key")] = m.group("val")
    return conf


def prog_r60_config_parser(re):
    """Config parser: per-line classification then a full parse pass."""
    for i, line in enumerate(CONF_TEXT.splitlines()):
        def classify(line=line):
            if not line.strip():
                return ["blank"]
            if re.match(_COMMENT_RE, line):
                return ["comment"]
            if m := re.fullmatch(_SECTION_RE, line):
                return ["section", m.group("name")]
            if m := re.fullmatch(_KV_RE, line):
                return ["kv", m.group("key"), m.group("val")]
            return ["unknown", line]
        yield f"line.{i:02d}", classify
    yield "parse.full", lambda: _parse_conf(re)
    yield "sections.multiline", lambda: re.findall(
        r"^\[(\w+)\]$", CONF_TEXT, MULTILINE)
    yield "keys.multiline", lambda: re.findall(
        r"^(\w+)\s*=", CONF_TEXT, MULTILINE)


# ==========================================================================
# 4. r60_sub_callable — re.sub/subn with callable replacements.
# ==========================================================================
_TEMPLATE = "Dear {{name}}, your {{item}} ships {{when}} ({{missing}})."
_CONTEXT = {"name": "Ada", "item": "board", "when": "today"}


def _refuse(m):
    """Replacement callable that RAISES — the exception must propagate
    identically through sub() in both modes (type AND str, trap 10)."""
    raise ValueError(f"replacement refused for {m.group(0)!r}")


def prog_r60_sub_callable(re):
    """Template renderer + numeric rewriters, all callable-replacement sub."""
    yield "template.render", lambda: re.sub(
        r"\{\{(\w+)\}\}", lambda m: _CONTEXT.get(m.group(1), "<unset>"),
        _TEMPLATE)
    yield "numbers.double", lambda: re.sub(
        r"\d+", lambda m: str(int(m.group(0)) * 2), "a1 b22 c333 d")
    yield "named.swap", lambda: re.sub(
        r"(?P<first>\w+)/(?P<second>\w+)",
        lambda m: m.group("second") + "/" + m.group("first"),
        "a/b c/d ee/ff")
    yield "subn.reverse.users", lambda: list(re.subn(
        r"(?P<user>u\d+)", lambda m: "<" + m.group("user")[::-1] + ">",
        "u1 u22 x u333"))
    yield "sub.count.limited", lambda: re.sub(
        r"\s+", lambda m: "_", "a b  c   d", 2)
    yield "sub.callable.raises", lambda: re.sub(r"\d+", _refuse, "keep 42")
    yield "sub.callable.spans", lambda: re.sub(
        r"[aeiou]", lambda m: str(m.start()), "banana")


# ==========================================================================
# 5. r60_walrus_scan — "if m := ..." and "while m := ..." idioms.
# ==========================================================================
_LOGIN_LINES = (
    "login user=alice ok",
    "logout",
    "login user=bob fail",
    "ping seq=9",
    "login user=carol ok",
)


def _scan_all(re, pattern, text):
    """Manual while-walrus scan loop using Pattern.search(string, pos)."""
    out, pos = [], 0
    pat = re.compile(pattern)
    while m := pat.search(text, pos):
        out.append([list(m.span()), m.group(0)])
        pos = m.end() if m.end() > pos else pos + 1
    return out


def prog_r60_walrus_scan(re):
    """Walrus-operator scanning: per-line `if m :=` plus a `while m :=` loop."""
    for i, line in enumerate(_LOGIN_LINES):
        yield f"walrus.if.{i}", (lambda line=line: (
            m["user"] if (m := re.search(r"user=(?P<user>\w+)", line))
            else "<anon>"))
    yield "walrus.while.words", lambda: _scan_all(
        re, r"[A-Za-z]+", "ab 12 cd--ef 34 gh")
    yield "walrus.while.pairs", lambda: _scan_all(
        re, r"\w+=\w+", " ".join(_LOGIN_LINES))
    yield "walrus.if.else_branch", lambda: (
        m.span() if (m := re.fullmatch(r"\d+", "12x")) else "no-fullmatch")


# ==========================================================================
# 6. r60_finditer_dispatch — finditer driving groupdict-keyed dispatch.
# ==========================================================================
_SCRIPT = "x:=1; incr(x); y:=add2(y); noop(); z:=42; emit(z);"
_DISPATCH_PATTERN = (r"(?P<assign>(?P<lhs>\w+):=(?P<rhs>\w+))"
                     r"|(?P<call>(?P<fn>\w+)\((?P<arg>\w*)\))"
                     r"|(?P<semi>;)")

_HANDLERS = {
    "assign": lambda gd: ["assign", gd["lhs"], gd["rhs"]],
    "call": lambda gd: ["call", gd["fn"], gd["arg"] or None],
    "semi": lambda gd: ["end-stmt"],
}


def _interpret(re):
    """Dispatch each finditer hit on its groupdict (which outer alternative
    participated) — the groupdict None/non-None pattern is exactly the
    group-reconstruction surface AC-1-7 stressed."""
    records = []
    for m in re.finditer(_DISPATCH_PATTERN, _SCRIPT):
        gd = m.groupdict()
        for op in ("assign", "call", "semi"):
            if gd[op] is not None:
                records.append(_HANDLERS[op](gd))
                break
    return records


def prog_r60_finditer_dispatch(re):
    yield "interpret.script", lambda: _interpret(re)
    yield "groupdicts.raw", lambda: [
        m.groupdict() for m in re.finditer(_DISPATCH_PATTERN, _SCRIPT)]
    yield "last.markers", lambda: [
        [m.lastindex, m.lastgroup]
        for m in re.finditer(_DISPATCH_PATTERN, _SCRIPT)]
    yield "spans.outer", lambda: [
        [list(m.span("assign")), list(m.span("call")), list(m.span("semi"))]
        for m in re.finditer(_DISPATCH_PATTERN, _SCRIPT)]
    yield "assign.count", lambda: sum(
        1 for r in _interpret(re) if r[0] == "assign")


# ==========================================================================
# 7. r60_error_flow — an "except re.error" program flow.
# ==========================================================================
_CANDIDATE_PATTERNS = (
    r"(ok)+",             # valid
    r"(unclosed",         # error: missing )
    r"[z-a]",             # error: bad character range
    r"*nothing",          # error: nothing to repeat
    r"(?P<good>\w+)!",    # valid
    r"(?P<dup>x)(?P<dup>y)",  # error: duplicate group name
    rb"(bad",             # error on a BYTES pattern
)
_ERROR_SUBJECT = "ok okok stop! go!"


def prog_r60_error_flow(re):
    """A validator that tries user-supplied patterns, CATCHES re.error itself
    (recording str/msg/pos/pattern — the R30 fidelity surface), and falls back
    to a default pattern when compilation fails.  Two trailing calls let their
    exceptions PROPAGATE so the harness-side exception comparison is also
    exercised."""
    for i, cand in enumerate(_CANDIDATE_PATTERNS):
        def attempt(cand=cand):
            try:
                pat = re.compile(cand)
            except re.error as e:
                return {"ok": False, "str": str(e), "msg": e.msg,
                        "pos": e.pos,
                        "pattern": (e.pattern if isinstance(e.pattern, str)
                                    else repr(e.pattern))}
            if isinstance(cand, bytes):
                return {"ok": True, "found": None}
            return {"ok": True, "found": pat.findall(_ERROR_SUBJECT)}
        yield f"candidate.{i}", attempt
    yield "fallback.after_error", lambda: (
        lambda default: default.findall(_ERROR_SUBJECT))(re.compile(r"\w+!"))
    yield "uncaught.badgroupref", lambda: re.sub(r"(a)", r"\9", "aa")
    yield "uncaught.type_mix", lambda: re.search(rb"a", "aaa")
    yield "uncaught.compile", lambda: re.compile(r"(?P<1bad>x)") and None


# ==========================================================================
# 8. r60_ac17_shapes — the AC-1-7 bug-derived shapes as a regression scanner.
# ==========================================================================
_AC17_SHAPES = (
    # anchored alternation with capture groups (the end-context
    # group-reconstruction bug class)
    ("anchalt.mid", r"(?P<a>foo)$|(?P<b>foo)", "foobar", 0),
    ("anchalt.end", r"(?P<a>foo)$|(?P<b>foo)", "foo", 0),
    ("anchalt.both", r"(?P<a>foo)$|(?P<b>foo)", "foobar foo", 0),
    ("anchalt.ml", r"(?P<a>foo)$|(?P<b>foo)", "foo\nfoobar", MULTILINE),
    ("anchalt.numeric", r"(foo)$|(foo)", "foobar foo", 0),
    ("anchalt.wordb", r"(?P<a>foo)\b|(?P<b>foo)", "foobar foo", 0),
    # must_advance / empty-preferring lazy quantifiers
    ("lazyopt", r"a??", "aa", 0),
    ("lazyopt.gap", r"a??", "aXa", 0),
    ("lazystar.dot", r".*?", "ab", 0),
    ("emptyalt.lazy", r"(a|)??", "aa", 0),
)


def _match_facts(m):
    if m is None:
        return None
    return [list(m.span()), m.group(0), list(m.groups()),
            m.lastindex, m.lastgroup,
            {k: v for k, v in m.groupdict().items()}]


def prog_r60_ac17_shapes(re):
    """Regression scanner over the pattern shapes that exposed AC-1-7 bugs:
    for each shape run the full single/multi-match API and record group-level
    facts (never type/repr/identity)."""
    for name, pattern, subject, flags in _AC17_SHAPES:
        yield f"{name}.search", (lambda p=pattern, s=subject, f=flags:
                                 _match_facts(re.search(p, s, f)))
        yield f"{name}.findall", (lambda p=pattern, s=subject, f=flags:
                                  re.findall(p, s, f))
        yield f"{name}.finditer", (lambda p=pattern, s=subject, f=flags:
                                   [_match_facts(m)
                                    for m in re.finditer(p, s, f)])
        yield f"{name}.sub", (lambda p=pattern, s=subject, f=flags:
                              re.sub(p, "<>", s, 0, f))
        yield f"{name}.split", (lambda p=pattern, s=subject, f=flags:
                                re.split(p, s, 0, f))


# ==========================================================================
# Registry — THE corpus.  key_pattern/key_flags name the pattern whose
# explain() tier the harness snapshots; awaits_tier marks the transition
# vehicle(s) that must be run with a small PYRO_N_SYNTH.
# ==========================================================================
REGISTRY = {
    "r60_log_scan": {"fn": prog_r60_log_scan,
                     "key_pattern": LOG_PATTERN, "key_flags": 0,
                     "awaits_tier": True},
    "r60_tokenizer": {"fn": prog_r60_tokenizer,
                      "key_pattern": None, "key_flags": 0,
                      "awaits_tier": False},
    "r60_config_parser": {"fn": prog_r60_config_parser,
                          "key_pattern": None, "key_flags": 0,
                          "awaits_tier": False},
    "r60_sub_callable": {"fn": prog_r60_sub_callable,
                         "key_pattern": None, "key_flags": 0,
                         "awaits_tier": False},
    "r60_walrus_scan": {"fn": prog_r60_walrus_scan,
                        "key_pattern": None, "key_flags": 0,
                        "awaits_tier": False},
    "r60_finditer_dispatch": {"fn": prog_r60_finditer_dispatch,
                              "key_pattern": None, "key_flags": 0,
                              "awaits_tier": False},
    "r60_error_flow": {"fn": prog_r60_error_flow,
                       "key_pattern": None, "key_flags": 0,
                       "awaits_tier": False},
    "r60_ac17_shapes": {"fn": prog_r60_ac17_shapes,
                        "key_pattern": None, "key_flags": 0,
                        "awaits_tier": False},
}

TRANSITION_PROGRAMS = tuple(n for n, m in REGISTRY.items() if m["awaits_tier"])
