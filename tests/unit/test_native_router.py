"""Anti-drift + behavioural equivalence for the native routing hot path
(R3b/R3c).

Four nets keep the C router and the pure-Python router provably in sync:

  1. 288-point EXHAUSTIVE equivalence of ``_fast.decide_raw`` against a Python
     transcription of steps S1..S6 over the FULL input domain.
  2. Threshold round-trip: ``(_fast.S_MIN, _fast.N_REUSE) == (_route.S_MIN,
     _route.N_REUSE)`` — catches a stale generated header / stale in-tree .so.
  3. Behavioural differential: the native ``Pattern`` vs the pure-Python
     ``PyPattern`` across a wide corpus (incl. pos/endpos edge cases, zero-width
     matches, bytes vs str) — identical results, exception types, ``stats()``
     deltas, and ``_calls``.  The reference is PyPattern, NOT stock ``re``.
  4. GC hygiene: cached bound methods + _prog are traversed, so no leak under
     collection.

Every test skips cleanly on a pure-Python build (no extension) so the suite is
green in both CI configurations.
"""
import gc
import itertools
import re as stdre
import sys

import pytest

from pyro import _route
from pyro._match import PyPattern
import pyro.re as pre

_fast = _route._fast
native = pytest.mark.skipif(_fast is None, reason="native extension not built")


# --------------------------------------------------------------------------
# 1. Exhaustive decide_raw equivalence (288 points).
# --------------------------------------------------------------------------
def _ref_decide(
    subj_len,
    reuse,
    disabled,
    force,
    eligible,
    exact_type,
     full_span):
    S_MIN, N_REUSE = _route.S_MIN, _route.N_REUSE
    if disabled:
        return 0
    if not eligible:
        return 0
    if not exact_type:
        return 0
    if not full_span:
        return 0
    if (not force) and subj_len < S_MIN and reuse < N_REUSE:
        return 0
    return 1


@native
def test_decide_raw_exhaustive():
    S_MIN, N_REUSE = _route.S_MIN, _route.N_REUSE
    lens = [S_MIN - 1, S_MIN, S_MIN + 1]
    reuses = [N_REUSE - 1, N_REUSE, N_REUSE + 1]
    bools = [0, 1]
    n = 0
    for (
    disabled,
    force,
    eligible,
    exact_type,
    full_span,
    subj_len,
    reuse) in itertools.product(
        bools,
        bools,
        bools,
        bools,
        bools,
        lens,
         reuses):
        got = _fast.decide_raw(subj_len, reuse, disabled, force,
                               eligible, exact_type, full_span)
        exp = _ref_decide(subj_len, reuse, disabled, force,
                          eligible, exact_type, full_span)
        assert got == exp, (subj_len, reuse, disabled, force, eligible,
                            exact_type, full_span, got, exp)
        n += 1
    assert n == 2 ** 5 * 3 * 3 == 288


# --------------------------------------------------------------------------
# 2. Threshold round-trip.
# --------------------------------------------------------------------------
@native
def test_threshold_round_trip():
    assert (_fast.S_MIN, _fast.N_REUSE) == (_route.S_MIN, _route.N_REUSE)


@native
def test_route_abi_and_active():
    import pyro
    assert _fast.ROUTE_ABI == 1
    nr = pyro.native_router()
    assert nr["active"] is True
    assert nr["route_abi"] == 1


# --------------------------------------------------------------------------
# 3. Behavioural differential: native Pattern vs pure-Python PyPattern.
# --------------------------------------------------------------------------
_CASES = [
    (r"\d+", 0, "abc 123 def 456"),
    (r"[a-z]+", 0, "Hello World"),
    (r"a|bb|ccc", 0, "xcccbba"),
    (r"(foo|bar)+", 0, "foobarfoo"),
    (r"a*", 0, "baaab"),
    (r"a+?", 0, "aaaa"),
    (r"a{2,4}", 0, "aaaaaa"),
    (r"^\w+$", stdre.M, "one\ntwo\nthree"),
    (r"\bcat\b", 0, "a cat category cat."),
    (r"", 0, "abc"),
    (r"x?", 0, "yyy"),
    (r"(?P<y>\d+)h(?P<m>\d+)", 0, "12h30"),
    (r".", 0, "a\nb"),
    (r"\w+", 0, "ab\ud800cd ef"),     # lone surrogate subject
    (rb"\d+", 0, b"abc 123 def"),
    (rb"[A-Z]+", stdre.I, b"abcDEF"),
]


def _canon(m):
    if m is None:
        return None
    return (m.span(0), m.group(0), m.groups(), m.lastindex, m.lastgroup,
            m.pos, m.endpos, type(m.group(0)).__name__)


def _make_pair(pat, flags):
    """Fresh, parallel PyPattern + native Pattern (both reuse counter == 0).

    The native side is constructed DIRECTLY from _fast.Pattern rather than via
    pre.compile, so it does not pick up a cached pattern whose reuse counter has
    already crossed N_reuse (which would legitimately route it to the model path
    while the fresh PyPattern still falls back — a difference in _calls/stats,
    not in results)."""
    from pyro import _classify
    stock = stdre.compile(pat, flags)
    classi = _classify.classify(pat, flags)
    py = PyPattern(stock, classi)
    nat = _fast.Pattern(stdre.compile(pat, flags), classi)
    return py, nat


@native
@pytest.mark.parametrize("pat,flags,subj", _CASES,
                         ids=[repr(c[0]) for c in _CASES])
def test_differential_single_ops(pat, flags, subj):
    py, nat = _make_pair(pat, flags)
    for op in ("search", "match", "fullmatch"):
        rp = _canon(getattr(py, op)(subj))
        rn = _canon(getattr(nat, op)(subj))
        assert rp == rn, f"{op}: py={rp} nat={rn}"
    assert [_canon(m) for m in py.finditer(subj)] == \
           [_canon(m) for m in nat.finditer(subj)]
    assert py.findall(subj) == nat.findall(subj)


@native
@pytest.mark.parametrize("pos,endpos", [
    (0, None), (0, 3), (2, None), (2, 5), (3, 3), (0, 0),
    (0, 10 ** 9), (5, None), (0, -1), (-1, None),
])
def test_differential_pos_endpos(pos, endpos):
    subj = "foo bar baz qux 12 34"
    py, nat = _make_pair(r"(\w+)", 0)
    for op in ("search", "match", "fullmatch"):
        a = getattr(py, op)(subj, pos, endpos)
        b = getattr(nat, op)(subj, pos, endpos)
        assert _canon(a) == _canon(
            b), f"{op}({pos},{endpos}) py={_canon(a)} nat={_canon(b)}"
    assert [_canon(m) for m in py.finditer(subj, pos, endpos)] == \
           [_canon(m) for m in nat.finditer(subj, pos, endpos)]
    assert py.findall(subj, pos, endpos) == nat.findall(subj, pos, endpos)


@native
def test_differential_endpos_none_pos_nonzero_no_typeerror():
    """The spike bug: Pattern.search(subj, 2, None) must NOT raise; it defaults
    endpos to len(subj) exactly like PyPattern (the oracle)."""
    subj = "aa bb 11 cc"
    py, nat = _make_pair(r"\w+", 0)
    for op in ("search", "match", "fullmatch"):
        assert _canon(getattr(py, op)(subj, 2, None)) == \
               _canon(getattr(nat, op)(subj, 2, None))


@native
def test_differential_kwargs():
    subj = "abc 123"
    py, nat = _make_pair(r"\d+", 0)
    assert _canon(py.search(string=subj)) == _canon(nat.search(string=subj))
    assert _canon(py.search(subj, pos=0)) == _canon(nat.search(subj, pos=0))
    assert _canon(py.search(subj, pos=1, endpos=7)) == \
           _canon(nat.search(subj, pos=1, endpos=7))


@native
def test_differential_exotic_pos_type():
    """bool / __index__ pos types bail to the Python router; result must equal
    PyPattern's (both reproduce re's rich __index__ semantics)."""
    class Idx:
        def __init__(self, v):
            self.v = v
        def __index__(self):
            return self.v
    subj = "ab cd 12"
    py, nat = _make_pair(r"\w+", 0)
    for pos in (True, Idx(3)):
        assert _canon(py.search(subj, pos)) == _canon(nat.search(subj, pos))


@native
def test_differential_bytes_subject_types():
    """bytearray / memoryview subjects gate to fallback with bytes results."""
    py, nat = _make_pair(rb"\w+", 0)
    for subj in (bytearray(b"aa 12 bb"), memoryview(b"aa 12 bb")):
        rp, rn = py.search(subj), nat.search(subj)
        assert _canon(rp) == _canon(rn)
        assert type(rn.group(0)) is bytes


@native
def test_differential_calls_counter():
    py, nat = _make_pair(r"\d+", 0)
    subj = "x 1 y 2"
    for _ in range(37):
        py.search(subj)
        nat.search(subj)
    assert py._calls == nat._calls == 37
    # sub/split go through the cold trampolines; still one increment each.
    py.sub("Z", subj); nat.sub("Z", subj)
    py.split(subj); nat.split(subj)
    assert py._calls == nat._calls == 39


@native
def test_stats_deltas_match_between_builds():
    """Native stats() must present the six _STATS keys in order and count each
    dispatch exactly once."""
    _route.reset_stats()
    _py, p = _make_pair(r"\d+", 0)   # fresh native pattern, reuse 0
    for _ in range(5):
        p.search("a 1 b")
    p.findall("a 1 b")            # aggregate op -> fallback
    s = _route.stats()
    assert list(s.keys()) == ["hardware", "model", "fallback",
                              "fallback_after_error", "device_errors", "total"]
    assert s["fallback"] == 6
    assert s["total"] == 6
    assert s["model"] == 0


# --------------------------------------------------------------------------
# 4. GC hygiene.
# --------------------------------------------------------------------------
@native
def test_gc_no_leak_of_pattern_cycle():
    import weakref
    _py, p = _make_pair(r"(\w+)\1", 0)   # direct construction, NOT cached
    p.search("hi hi")
    ref = weakref.ref(p)
    # Manufacture a cycle through _prog (writable member).
    p._prog = p
    del p
    gc.collect()
    assert ref() is None, "native Pattern leaked under a reference cycle"


@native
def test_weakref_supported():
    import weakref
    _py, p = _make_pair(r"\d", 0)
    r = weakref.ref(p)
    assert r() is p
