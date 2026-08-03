"""Shared oracle helpers for the PYRO Phase 0 acceptance suite.

The behavioural oracle is CPython's stock ``re`` (spec R54). Nothing in this
module inspects the implementation; every expectation is computed independently
from stock ``re`` or is a literal spec constant. Test files import this module
(conftest.py puts this directory on sys.path).

Spec constants used across the suite:
  * S_min  = 64 KiB  (R2 / R51 step 4 threshold)
  * N_reuse = 32      (R2 / R51 step 4 threshold)
"""

import re as _re

# --------------------------------------------------------------------------
# Stock-callable capture (booby-trap fix, Phase 3).
#
# ``assert_equivalent`` computes its EXPECTED side from stock ``re``.  Looking
# the functions up on the ``re`` module AT CALL TIME (``getattr(_re, name)``)
# is a booby-trap under ``pyro.install()``: install() rebinds
# re.search/match/... to PYRO's implementations (R34), so any comparison made
# while installed would compare PYRO against PYRO and pass VACUOUSLY.  We
# therefore capture the stock callables ONCE, at oracle import, into _STOCK
# and compute every expectation from that table.  Existing pre-install callers
# are unaffected: the captured functions are exactly what the call-time
# getattr returned before install().
# --------------------------------------------------------------------------
_STOCK_NAMES = ("search", "match", "fullmatch", "findall", "finditer",
                "sub", "subn", "split", "compile")
_STOCK = {name: getattr(_re, name) for name in _STOCK_NAMES}

# Defend the capture itself: if this module were first imported while
# pyro.install() is active, _STOCK would capture PYRO's patched functions and
# the fix above would be moot.  PYRO's replacements are defined in the
# ``pyro.re`` module; stock re's are defined in ``re``.  Fail loudly at import
# rather than silently become a vacuous oracle.
for _n, _fn in _STOCK.items():
    if getattr(_fn, "__module__", "").startswith("pyro"):
        raise ImportError(
            f"oracle imported while pyro.install() is active: re.{_n} is "
            f"{_fn!r} (module {_fn.__module__}); the oracle must capture "
            "STOCK callables — import oracle (or run pyro.uninstall()) "
            "before installing")
del _n, _fn


def stock(name):
    """The STOCK ``re`` callable ``name``, captured at oracle import.

    Immune to ``pyro.install()``: use this (never a call-time attribute lookup
    on the ``re`` module) whenever an expectation must be computed while the
    interposition shim may be installed.
    """
    return _STOCK[name]


S_MIN = 64 * 1024          # R2: 64 KiB
N_REUSE = 32               # R2: N_reuse
# R37 / AC-0-8: ABI 1.0.0 packed MAJOR<<16|MINOR<<8|PATCH
ABI_EXPECTED = 0x00010000


# --------------------------------------------------------------------------
# Match-object canonicalisation.
#
# Per the task brief, match objects are compared by span + groups + lastindex
# + lastgroup (NOT identity, R36a).  We additionally compare the full set of
# per-group spans/starts/ends, groupdict, __getitem__, and the documented
# attributes string/pos/endpos (R28) because all are part of the drop-in
# contract and all must be byte-identical (R16).
# --------------------------------------------------------------------------
def canon_match(m):
    """Return a hashable/comparable canonical form of a Match, or None."""
    if m is None:
        return None
    groups = m.groups()
    ng = len(groups)
    return (
        ("span", m.span()),
        ("group0", m.group(0)),
        ("groups", groups),
        ("spans", tuple(m.span(i) for i in range(ng + 1))),
        ("starts", tuple(m.start(i) for i in range(ng + 1))),
        ("ends", tuple(m.end(i) for i in range(ng + 1))),
        ("lastindex", m.lastindex),
        ("lastgroup", m.lastgroup),
        ("groupdict", tuple(sorted(m.groupdict().items()))),
        ("getitem0", m[0]),
        ("string", m.string),
        ("pos", m.pos),
        ("endpos", m.endpos),
    )


def canon_iter(it):
    return [canon_match(m) for m in it]


def _repls(subject):
    """A pair of substitution templates typed to match the subject.

    'literal' exercises plain replacement; the second references group 0 via
    \\g<0> which is always valid regardless of capture-group count and
    exercises the expand/template path (R18 mentions \\g<n> triggers hybrid).
    """
    if isinstance(subject, (bytes, bytearray)):
        return [b"Z", rb"[\g<0>]"]
    return ["Z", r"[\g<0>]"]


def assert_equivalent(pyro_mod, pattern, subject, flags=0, label=""):
    """Assert every §7.1 API on pyro_mod matches stock re for one triple.

    Covers search/match/fullmatch/findall/finditer/sub/subn/split (R16, R29,
    R54).  ``pyro_mod`` is normally ``pyro.re`` (or the patched ``re`` under
    install()).  Expectations come from the STOCK ``re`` callables captured at
    oracle import (``_STOCK``) — never from a call-time attribute lookup on
    the ``re`` module — so the comparison stays honest while pyro.install()
    is active (see the booby-trap note at the top of this module).
    """
    tag = f"[{label}] pat={pattern!r} flags={int(flags)} subj={subject!r}"

    for name in ("search", "match", "fullmatch"):
        exp = canon_match(_STOCK[name](pattern, subject, flags))
        act = canon_match(getattr(pyro_mod, name)(pattern, subject, flags))
        assert act == exp, (
            f"{name} mismatch {tag}\n  expected={exp}\n  actual  ={act}")

    exp_fa = _STOCK["findall"](pattern, subject, flags)
    act_fa = pyro_mod.findall(pattern, subject, flags)
    assert act_fa == exp_fa, (
        f"findall mismatch {tag}\n  expected={exp_fa}\n  actual={act_fa}")

    exp_fi = canon_iter(_STOCK["finditer"](pattern, subject, flags))
    act_fi = canon_iter(pyro_mod.finditer(pattern, subject, flags))
    assert act_fi == exp_fi, (
        f"finditer mismatch {tag}\n  expected={exp_fi}\n  actual={act_fi}")

    for repl in _repls(subject):
        exp_s = _STOCK["sub"](pattern, repl, subject, 0, flags)
        act_s = pyro_mod.sub(pattern, repl, subject, 0, flags)
        assert act_s == exp_s, f"sub mismatch {tag} repl={
    repl!r}\n  expected={
        exp_s!r}\n  actual={
            act_s!r}"
        exp_sn = _STOCK["subn"](pattern, repl, subject, 0, flags)
        act_sn = pyro_mod.subn(pattern, repl, subject, 0, flags)
        assert act_sn == exp_sn, f"subn mismatch {tag} repl={
    repl!r}\n  expected={
        exp_sn!r}\n  actual={
            act_sn!r}"

    exp_sp = _STOCK["split"](pattern, subject, 0, flags)
    act_sp = pyro_mod.split(pattern, subject, 0, flags)
    assert act_sp == exp_sp, f"split mismatch {tag}\n  expected={
    exp_sp!r}\n  actual={
        act_sp!r}"


# --------------------------------------------------------------------------
# Corpora.  Each entry is (label, pattern, flags, subject).
# --------------------------------------------------------------------------

# §5.1 supported constructs (R9).  One or more entries per bullet.
SUPPORTED = [
    # literal characters / literal byte sequences
    ("literal.str", r"abc", 0, "xxabcxxabc"),
    ("literal.bytes", rb"abc", 0, b"xxabcxxabc"),
    # character classes
    ("class.pos", r"[abc]+", 0, "zzabcabz"),
    ("class.neg", r"[^abc]+", 0, "abxyzab"),
    ("class.range", r"[a-f]+", 0, "zzabcdefzz"),
    ("class.d", r"\d+", 0, "ab123cd45"),
    ("class.D", r"\D+", 0, "12ab34cd"),
    ("class.w", r"\w+", 0, " a_b9 c "),
    ("class.W", r"\W+", 0, "ab  cd--ef"),
    ("class.s", r"\s+", 0, "a  b\tc\nd"),
    ("class.S", r"\S+", 0, "  ab  cd  "),
    ("class.dot", r"a.c", 0, "a\nc a c abc"),
    ("class.dot.dotall", r"a.c", _re.DOTALL, "a\nc a-c"),
    # concatenation
    ("concat", r"foobar", 0, "xfoobarx"),
    # alternation
    ("alt", r"cat|dog|bird", 0, "I have a dog and a bird"),
    ("alt.literals", r"foo|bar|baz", 0, "quux bar foo baz"),
    # quantifiers greedy
    ("quant.star", r"ab*c", 0, "ac abc abbbc"),
    ("quant.plus", r"ab+c", 0, "ac abc abbc"),
    ("quant.opt", r"ab?c", 0, "ac abc abbc"),
    ("quant.m", r"a{3}", 0, "a aa aaa aaaa"),
    ("quant.mn", r"a{2,4}", 0, "a aa aaaaa"),
    ("quant.m_open", r"a{2,}", 0, "a aa aaaa"),
    # quantifiers lazy
    ("quant.star.lazy", r"a.*?b", 0, "axxbxxb"),
    ("quant.plus.lazy", r"a.+?b", 0, "axxbxxb"),
    ("quant.opt.lazy", r"ab??c", 0, "abc ac"),
    ("quant.mn.lazy", r"a{2,4}?", 0, "aaaaa"),
    # anchors
    ("anchor.caret", r"^abc", 0, "abc\nabc"),
    ("anchor.dollar", r"abc$", 0, "abc\nabc"),
    ("anchor.bigA", r"\Aabc", 0, "abc abc"),
    ("anchor.bigZ", r"abc\Z", 0, "abc abc"),
    ("anchor.wordb", r"\bword\b", 0, "a word and wordy words"),
    ("anchor.nwordb", r"\Bword", 0, "password word wordy"),
    # non-capturing group
    ("group.noncap", r"(?:ab)+", 0, "abab x ababab"),
    # capturing / named groups
    ("group.cap", r"(a)(b)(c)", 0, "zzabczz"),
    ("group.named", r"(?P<num>\d+)", 0, "abc12345"),
    # inline flags support
    ("flag.ignorecase.ascii", r"aBc", _re.IGNORECASE, "ABC abc AbC"),
    ("flag.multiline", r"^\w+", _re.MULTILINE, "foo\nbar\nbaz"),
    ("flag.dotall", r"a.b", _re.DOTALL, "a\nb"),
    ("flag.ascii", r"\w+", _re.ASCII, "abécd"),
    ("flag.verbose", r"a b c", _re.VERBOSE, "abc a b c"),
    ("flag.inline", r"(?i)abc", 0, "ABC abc"),
    ("bytes.class.ignorecase", rb"[a-z]+", _re.IGNORECASE, b"ABC xyz 123"),
]

# §5.2 unsupported constructs (R10) -> must classify fallback-only.
UNSUPPORTED = [
    ("backref.numeric", r"(a)\1", 0, "aa a aa"),
    ("backref.named", r"(?P<n>a)(?P=n)", 0, "aa a"),
    ("lookahead.pos", r"a(?=b)", 0, "ab ac ab"),
    ("lookahead.neg", r"a(?!b)", 0, "ab ac"),
    ("lookbehind.pos", r"(?<=a)b", 0, "ab cb ab"),
    ("lookbehind.neg", r"(?<!a)b", 0, "ab cb"),
    ("conditional", r"(a)?(?(1)b|c)", 0, "ab c ac"),
    ("atomic.group", r"(?>a+)b", 0, "aaab aa"),
    ("possessive", r"a++b", 0, "aaab"),
]

# Over-capacity patterns (R11-R13, R45).  A bounded repeat of 70000 exceeds the
# maximum advertisable MAX_REPEAT (the CAPS1 register field is 16-bit, R45, so a
# conforming engine can advertise at most 65535; R13 sets only a >=255 minimum).
# Therefore any conforming classifier must route these to fallback (R12).
OVERCAP = [
    ("overcap.repeat", r"a{70000}", 0, "aaa"),
    ("overcap.states", r"(?:abcdefghij){70000}", 0, "abcdefghij"),
]

# §6.3 empty-match cases (R22).
EMPTY = [
    ("empty.star", r"a*", 0, "bbb"),
    ("empty.star.mixed", r"a*", 0, "baaab"),
    ("empty.opt", r"a?", 0, "bab"),
    ("empty.word", r"\b", 0, "ab cd"),
    ("empty.emptypat", r"", 0, "abc"),
    ("empty.caret_ml", r"^", _re.MULTILINE, "a\nb\nc"),
    ("empty.dollar_ml", r"$", _re.MULTILINE, "a\nb\nc"),
    ("empty.alt", r"a|", 0, "xax"),
]

# R22 must_advance: lazy / empty-preferring quantifiers that match empty at a
# position and then, on the finditer/findall/sub/split retry, must advance to a
# NON-empty match at the SAME start (CPython 3.7+ must_advance semantics).
# These
# stress the post-empty-match retry path that greedy `a*` does not, incl.
# empty-preferring quantifiers followed by a matchable atom and empty-branch
# alternations (§5.1-legal).  Oracle facts verified against stock re, e.g.
# `a??` on 'aa' -> spans [(0,0),(0,1),(1,1),(1,2),(2,2)].
MUST_ADVANCE = [
    ("lazy.opt", r"a??", 0, "aa"),
    ("lazy.opt.gap", r"a??", 0, "aXa"),
    ("lazy.star.dot", r".*?", 0, "ab"),
    ("lazy.opt.x", r"x??", 0, "xxx"),
    ("lazy.opt.d", r"\d??", 0, "12"),
    ("lazy.star", r"a*?", 0, "aaa"),
    ("lazy.star.d", r"\d*?", 0, "12a3"),
    ("lazy.opt.dotany", r".??", 0, ".x"),
    ("lazy.empty_alt", r"(a|)??", 0, "aa"),
    ("lazy.empty_alt.noncap", r"(?:a|)??", 0, "aba"),
    ("lazy.followed_atom", r"a*?b?", 0, "ab"),
    ("lazy.groups", r"(a??)(b?)", 0, "ab"),
    ("lazy.opt.then_atom", r"a??b", 0, "aab"),
    ("lazy.alt", r"a??|b", 0, "ab"),
]

# §6.5 multiline / anchor cases (R24).
ANCHORS = [
    ("ml.line", r"^\w+$", _re.MULTILINE, "foo\nbar\nbaz"),
    ("ml.caret", r"^x", _re.MULTILINE, "x\nyx\nx"),
    ("ml.dollar", r"x$", _re.MULTILINE, "ax\nxb\nx"),
    ("nonml.dollar.trailnl", r"abc$", 0, "abc\n"),
    ("nonml.dollar.notrail", r"abc$", 0, "abc"),
    ("bigZ.no_trailnl", r"abc\Z", 0, "abc\n"),
    ("bigZ.end", r"abc\Z", 0, "abc"),
    ("caret.nonml", r"^abc", 0, "xyz\nabc"),
]

# R15 IGNORECASE folding, incl. the full-fold (ß<->ss) fallback rule.
IGNORECASE = [
    ("ic.ascii", r"HELLO", _re.IGNORECASE, "hello HELLO HeLLo"),
    ("ic.ascii.class", r"[a-z]+", _re.IGNORECASE, "ABC def GHI"),
    ("ic.sharp_s.lower", "ß", _re.IGNORECASE, "straße STRASSE ss SS ß"),
    ("ic.sharp_s.cap", "ẞ", _re.IGNORECASE, "ß ẞ SS ss"),
    ("ic.ligature.fi", "ﬁ", _re.IGNORECASE, "fi ﬁ FI"),
    ("ic.kelvin", "k", _re.IGNORECASE, "k K K"),  # Kelvin sign simple fold
    ("ic.turkish_i", "i", _re.IGNORECASE, "i I İ ı"),
    ("ic.bytes", rb"ABC", _re.IGNORECASE, b"abc ABC"),
]

# R21 astral (>= U+10000) offset cases.  Offsets must be code-point indices.
ASTRAL = [
    ("astral.after", r"(x)", 0, "\U0001F600x\U0001F601x"),
    ("astral.dot", r".", 0, "a\U0001F600b\U0001F602"),
    ("astral.literal", "\U0001F600+", 0, "z\U0001F600\U0001F600z"),
    ("astral.class", r"[\U0001F600-\U0001F610]+", 0, "a\U0001F600\U0001F605b"),
    ("astral.findall", r"\w", 0, "a\U0001F600b\U0001F601c"),
    ("astral.span", r"b", 0, "\U0001F600\U0001F601b"),
    ("astral.emoji_between", r"start(.*?)end", _re.DOTALL,
     "start\U0001F600\U0001F601end"),
]

# Group-differentiating alternations with anchors, where the anchored branch
# fails mid-string so a DIFFERENT capturing branch must win (R9 anchors + R16
# groups/lastindex/lastgroup + R17/R18 leftmost-greedy reconciliation).  This is
# the class that exposed an end-context group-reconstruction bug on the model
# path; each subject is chosen so the trailing anchor fails at the first
# branch.
ANCHOR_ALT = [
    ("altanchor.dollar.mid", r"(?P<a>foo)$|(?P<b>foo)", 0, "foobar"),
    ("altanchor.dollar.end", r"(?P<a>foo)$|(?P<b>foo)", 0, "foo"),
    ("altanchor.dollar.nl", r"(?P<a>foo)$|(?P<b>foo)", 0, "foo\nbar"),
    ("altanchor.dollar.finditer", r"(?P<a>foo)$|(?P<b>foo)", 0, "foobar foo"),
    ("altanchor.dollar.ml", r"(?P<a>foo)$|(?P<b>foo)", _re.MULTILINE,
     "foo\nfoobar"),
    ("altanchor.wordb.mid", r"(?P<a>foo)\b|(?P<b>foo)", 0, "foobar"),
    ("altanchor.wordb.sep", r"(?P<a>foo)\b|(?P<b>foo)", 0, "foo bar"),
    ("altanchor.wordb.finditer", r"(?P<a>foo)\b|(?P<b>foo)", 0, "foobar foo"),
    ("altanchor.nwordb.mid", r"(?P<a>foo)\B|(?P<b>foo)", 0, "foobar foo"),
    ("altanchor.bigZ.mid", r"(?P<a>foo)\Z|(?P<b>foo)", 0, "foobar"),
    ("altanchor.bigZ.end", r"(?P<a>foo)\Z|(?P<b>foo)", 0, "foo"),
    ("altanchor.bigZ.nl", r"(?P<a>foo)\Z|(?P<b>foo)", 0, "foo\n"),
    ("altanchor.numeric", r"(foo)$|(foo)", 0, "foobar foo"),
    ("altanchor.caret.lead", r"^(?P<a>foo)|(?P<b>foo)", 0, "xfoo foo"),
    ("altanchor.caret.lead.hit", r"^(?P<a>foo)|(?P<b>foo)", 0, "foo foo"),
    ("altanchor.fullmatch.ctx", r"(?P<a>foo)$|(?P<b>foobar)", 0, "foobar"),
    ("altanchor.three", r"(?P<a>x)$|(?P<b>x)\b|(?P<c>x)", 0, "xxy x"),
    ("altanchor.bytes", rb"(?P<a>foo)$|(?P<b>foo)", 0, b"foobar foo"),
]

# Greedy/lazy pairs where captured group boundaries differ (R23).
GREEDY_LAZY = [
    ("gl.greedy", r"(a.*b)", 0, "axbxb"),
    ("gl.lazy", r"(a.*?b)", 0, "axbxb"),
    ("gl.greedy.plus", r"(a.+b)", 0, "a1b2b3b"),
    ("gl.lazy.plus", r"(a.+?b)", 0, "a1b2b3b"),
    ("gl.alt.order", r"(a|ab)", 0, "abab"),
]


# --------------------------------------------------------------------------
# Deterministic property-based generator (R55) — stdlib random only.
# --------------------------------------------------------------------------
import random as _random

_ATOMS = [
    "a", "b", "c", "d", "x", "0", "1", "_", " ",
    r"\d", r"\D", r"\w", r"\W", r"\s", r"\S", ".",
    "[a-c]", "[^a-c]", "[abc0-9]",
]
# Atom quantifiers may be unbounded; GROUP quantifiers are restricted to bounded
# forms so a group whose body already contains an unbounded quantifier is never
# itself unbounded-quantified.  This prevents nested unbounded repetition
# (e.g. (a*)* / (a+)+) that causes catastrophic backtracking in the stock-re
# oracle — the equivalence property is unaffected, only ReDoS is avoided.
_ATOM_QUANTS = [
    "",
    "*",
    "+",
    "?",
    "*?",
    "+?",
    "??",
    "{2}",
    "{1,3}",
    "{2,}",
     "{0,2}?"]
_GROUP_QUANTS = ["", "?", "{2}", "{1,3}", "{0,2}"]

# §5.1 anchors (R9).  Emitted as zero-width leaf atoms and NEVER given a
# quantifier (quantifying a bare anchor is a stock-re "nothing to repeat"
# error), so anchored alternation branches such as (?P<a>foo)$|(?P<b>foo) are
# reachable by the fuzzer (R54/R55).  Anchors are zero-width and add no
# backtracking blow-up, so ReDoS safety is preserved.
_ANCHOR_ATOMS = ["^", "$", r"\b", r"\B", r"\A", r"\Z"]


def _gen_term(rng, depth, name_ctr):
    if depth > 0 and rng.random() < 0.30:
        n = rng.randint(1, 3)
        inner = "".join(_gen_term(rng, depth - 1, name_ctr) for _ in range(n))
        kind = rng.random()
        if kind < 0.45:
            atom = "(?:" + inner + ")"
        elif kind < 0.8:
            atom = "(" + inner + ")"
        else:
            name_ctr[0] += 1
            atom = f"(?P<g{name_ctr[0]}>" + inner + ")"
        return atom + rng.choice(_GROUP_QUANTS)
    if rng.random() < 0.18:
        # bare anchor, unquantified
        return rng.choice(_ANCHOR_ATOMS)
    atom = rng.choice(_ATOMS)
    return atom + rng.choice(_ATOM_QUANTS)


def gen_supported_pattern(rng, depth=2):
    """Generate a random pattern from the §5.1 grammar (may occasionally be an
    invalid regex; callers cross-check against stock re.compile).

    ~30% of generated patterns are biased to end a branch with a trailing anchor
    so that group-differentiating anchored alternations (the class that exposed
    an end-context group-reconstruction bug) are exercised."""
    branches = []
    for _ in range(rng.randint(1, 3)):
        nterms = rng.randint(1, 4)
        name_ctr = [rng.randint(0, 1000)]
        terms = [_gen_term(rng, depth, name_ctr) for _ in range(nterms)]
        if rng.random() < 0.30:
            terms.append(rng.choice(["$", r"\b", r"\B", r"\Z"]))
        branches.append("".join(terms))
    return "|".join(branches)


_SUBJ_ALPHABET = "abcd012_ .\n\txyz"


def gen_subject(rng, maxlen=12):
    return "".join(rng.choice(_SUBJ_ALPHABET)
                   for _ in range(rng.randint(0, maxlen)))


_ARBITRARY_ALPHABET = "abc012()[]{}|*+?.\\^$-,: "


def gen_arbitrary_pattern(rng, maxlen=12):
    return "".join(rng.choice(_ARBITRARY_ALPHABET)
                   for _ in range(rng.randint(1, maxlen)))
