"""AC-0-2 (amended v1.2.1): the differential corpus MUST include inputs that
trigger each Phase-0 safety gate (R51a) and assert they route to fallback with
byte-identical results AND return types vs stock re, never counted as
fallback_after_error.  (R14a, R16, R29, R51a, R54)

Gates covered:
  * R51a(a) subject-type gate — bytearray / memoryview subjects (bytes-mode).
  * R51a(b) pos/endpos gate — non-default pos/endpos on Pattern methods.
  * R51a(d) / R14a surrogate gate — str subjects/patterns with unpaired
    surrogates (not strictly UTF-8-encodable), short and >= S_min, incl. under
    PYRO_FORCE_MODEL: must not raise, must not increment fallback_after_error.

Type fidelity is checked EXPLICITLY because value equality hides type bugs in
Python (b'a' == bytearray(b'a') is True); stock re returns bytes (not bytearray/
memoryview) from group(0)/findall/sub/split and those results must be hashable
(usable as dict keys).  Routing to fallback is observed via the spec-guaranteed
R29 signal: on the fallback path the object IS a genuine re.Match.
"""

import os
import re as stdre

import pytest

import pyro
import pyro.re as pre
import oracle

LARGE_N = oracle.S_MIN + 64  # >= S_min so a normal call would reach the model


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _fallback_after_error(mod):
    if not (hasattr(mod, "stats") and callable(mod.stats)):
        return None
    s = mod.stats()
    return s.get("fallback_after_error") if isinstance(s, dict) else None


def _assert_bytes_result_fidelity(pattern, subject):
    """All 8 APIs byte-identical to stock re AND element/return types are
    exactly
    stock's (bytes), and results are hashable (dict-key usable)."""
    tag = f"pat={pattern!r} subj_type={type(subject).__name__}"

    for name in ("search", "match", "fullmatch"):
        exp = getattr(stdre, name)(pattern, subject)
        act = getattr(pre, name)(pattern, subject)
        assert (exp is None) == (act is None), f"{name} None-ness {tag}"
        if exp is None:
            continue
        # R51a(a)/R29: gate routes to a genuine fallback object.
        assert isinstance(
    act, stdre.Match), f"{name} not genuine re.Match {tag}"
        assert act.span() == exp.span(), f"{name} span {tag}"
        assert act.group(0) == exp.group(0), f"{name} group0 value {tag}"
        assert type(act.group(0)) is type(exp.group(0)), (
            f"{name} group0 type {type(act.group(0)).__name__} != "
            f"{type(exp.group(0)).__name__} {tag}"
        )
        assert act.groups() == exp.groups(), f"{name} groups value {tag}"
        for g_act, g_exp in zip(act.groups(), exp.groups()):
            assert type(g_act) is type(g_exp), f"{name} group type {tag}"
        assert act.lastindex == exp.lastindex and act.lastgroup == exp.lastgroup
        # group(0) usable as a dict key (bytes is hashable; bytearray is not).
        d = {act.group(0): "v"}
        assert d[exp.group(
            0)] == "v", f"{name} group0 not dict-key-usable {tag}"

    exp_fa = stdre.findall(pattern, subject)
    act_fa = pre.findall(pattern, subject)
    assert act_fa == exp_fa, f"findall value {tag}"
    for e_act, e_exp in zip(act_fa, exp_fa):
        assert type(e_act) is type(e_exp), f"findall elem type {tag}"
        hash(e_act)  # must be hashable

    assert oracle.canon_iter(pre.finditer(pattern, subject)) == \
        oracle.canon_iter(stdre.finditer(pattern, subject)), f"finditer {tag}"

    repl = b"Z"
    exp_sub = stdre.sub(pattern, repl, subject)
    act_sub = pre.sub(pattern, repl, subject)
    assert act_sub == exp_sub and type(
        act_sub) is type(exp_sub), f"sub type {tag}"
    exp_sn = stdre.subn(pattern, repl, subject)
    act_sn = pre.subn(pattern, repl, subject)
    assert act_sn == exp_sn and type(
    act_sn[0]) is type(
        exp_sn[0]), f"subn {tag}"

    exp_sp = stdre.split(pattern, subject)
    act_sp = pre.split(pattern, subject)
    assert act_sp == exp_sp, f"split value {tag}"
    for e_act, e_exp in zip(act_sp, exp_sp):
        if e_act is not None:
            assert type(e_act) is type(e_exp), f"split elem type {tag}"


# --------------------------------------------------------------------------
# R51a(a): subject-type gate — bytearray / memoryview
# --------------------------------------------------------------------------
_BYTES_PATTERNS = [rb"a(b)c", rb"\w+", rb"abc", rb"(?P<g>\d+)", rb"\s+"]


@pytest.mark.parametrize("pattern", _BYTES_PATTERNS)
def test_bytearray_subject_gate(pattern):
    """R51a(a)/R16/R29: bytearray subject -> fallback, byte-identical results
    with stock return types (bytes, not bytearray) and hashable results."""
    subject = bytearray(b"xx abc 12 abc 34 def")
    _assert_bytes_result_fidelity(pattern, subject)


@pytest.mark.parametrize("pattern", _BYTES_PATTERNS)
def test_memoryview_subject_gate(pattern):
    """R51a(a)/R16/R29: memoryview subject -> fallback, byte-identical results
    with stock return types (bytes, not memoryview) and hashable results."""
    subject = memoryview(b"xx abc 12 abc 34 def")
    _assert_bytes_result_fidelity(pattern, subject)


def test_bytearray_group0_is_bytes_dict_key():
    """R51a(a): the group(0) of a bytearray-subject match is bytes and usable as
    a dict key (explicit, since bytearray is unhashable)."""
    m = pre.search(rb"a\w+", bytearray(b"z abc123 z"))
    assert isinstance(m, stdre.Match)
    assert type(m.group(0)) is bytes
    table = {m.group(0): 42}
    assert table[b"abc123"] == 42


# --------------------------------------------------------------------------
# R51a(b): pos/endpos gate
# --------------------------------------------------------------------------
def test_nondefault_pos_routes_to_fallback():
    """R51a(b)/R29: Pattern.search with pos != 0 -> fallback (genuine re.Match)
    and byte-identical to stock."""
    p_std = stdre.compile(r"(\w)(\d)")
    p_pyro = pre.compile(r"(\w)(\d)")
    subject = "a1 b2 c3 d4"
    m_std = p_std.search(subject, 3)
    m_pyro = p_pyro.search(subject, 3)
    assert isinstance(m_pyro, stdre.Match)
    assert oracle.canon_match(m_pyro) == oracle.canon_match(m_std)


def test_nondefault_endpos_routes_to_fallback():
    """R51a(b)/R29: Pattern.search with endpos != len(subject) -> fallback and
    byte-identical to stock."""
    p_std = stdre.compile(r"\d+")
    p_pyro = pre.compile(r"\d+")
    subject = "ab 123 cd 456"
    m_std = p_std.search(subject, 0, 6)
    m_pyro = p_pyro.search(subject, 0, 6)
    assert isinstance(m_pyro, stdre.Match)
    assert oracle.canon_match(m_pyro) == oracle.canon_match(m_std)


@pytest.mark.parametrize("method",
    ["search",
    "match",
    "fullmatch",
    "findall",
     "finditer"])
def test_pos_endpos_methods_byte_identical(method):
    """R51a(b)/R16/R27: Pattern methods with non-default pos/endpos are
    byte-identical to stock across the drop-in surface."""
    p_std = stdre.compile(r"(\w+)")
    p_pyro = pre.compile(r"(\w+)")
    subject = "foo bar baz qux"
    args = (subject, 4, 11)
    exp = getattr(p_std, method)(*args)
    act = getattr(p_pyro, method)(*args)
    if method == "finditer":
        assert oracle.canon_iter(act) == oracle.canon_iter(exp)
    elif method == "findall":
        assert act == exp
    else:
        assert oracle.canon_match(act) == oracle.canon_match(exp)


# --------------------------------------------------------------------------
# R51a(d) / R14a: surrogate / non-UTF-8-encodable gate
# --------------------------------------------------------------------------
SURR_SUBJECTS = [
    ("lone_high", "\ud800abcXYZ"),
    ("lone_low", "abc\udfffXYZ"),
    ("surrogateescape",
     b"\xff\xfe log line abc".decode("utf-8", "surrogateescape")),
    ("mixed", "pre\ud834post abc match"),
]
SURR_PATTERNS = [r"abc", r".", r"\w+", r"[a-z]+"]


def _apply_mode(mode):
    if mode == "force_model":
        os.environ["PYRO_FORCE_MODEL"] = "1"
    pyro.refresh_env()


@pytest.mark.parametrize("mode", ["default", "force_model"])
@pytest.mark.parametrize("subj_label,subject",
    SURR_SUBJECTS,
     ids=[s[0] for s in SURR_SUBJECTS])
@pytest.mark.parametrize("pattern", SURR_PATTERNS)
def test_surrogate_subject_short(pattern, subj_label, subject, mode):
    """R14a/R51a(d): a str subject with unpaired surrogates -> fallback; results
    byte-identical to stock re and NO exception raised, on both routing
    modes."""
    _apply_mode(mode)
    fae0 = _fallback_after_error(pre)
    oracle.assert_equivalent(
    pre,
    pattern,
    subject,
    0,
     label=f"surr:{subj_label}/{mode}")
    fae1 = _fallback_after_error(pre)
    if fae0 is not None and fae1 is not None:
        assert fae1 == fae0, (
            "surrogate gate must not count as fallback_after_error (R14a)")


@pytest.mark.parametrize("mode", ["default", "force_model"])
def test_surrogate_subject_large_routes_to_fallback(mode):
    """R14a/R51a(d)/R29: a >= S_min surrogate subject (which would otherwise
    reach
    the model) falls back — genuine re.Match, no raise, byte-identical, and
    fallback_after_error unchanged."""
    _apply_mode(mode)
    subject = "x" * LARGE_N + "\ud800" + "needle_surr"
    pattern = r"needle_surr"
    fae0 = _fallback_after_error(pre)
    m_std = stdre.search(pattern, subject)
    m_pyro = pre.search(pattern, subject)
    assert m_pyro is not None
    assert isinstance(
    m_pyro, stdre.Match), "surrogate gate must route to genuine fallback"
    assert oracle.canon_match(m_pyro) == oracle.canon_match(m_std)
    fae1 = _fallback_after_error(pre)
    if fae0 is not None and fae1 is not None:
        assert fae1 == fae0, (
            "surrogate gate must not count as fallback_after_error (R14a)")


@pytest.mark.parametrize("mode", ["default", "force_model"])
def test_surrogate_pattern(mode):
    """R14a/R8: a pattern containing an unpaired surrogate is
    non-UTF-8-encodable
    -> fallback-only; must not raise and stays byte-identical to stock re."""
    _apply_mode(mode)
    pattern = "\ud800"  # lone surrogate literal
    subject = "z\ud800z\ud800"
    oracle.assert_equivalent(pre, pattern, subject, 0, label=f"surrpat/{mode}")


def test_surrogate_pattern_classified_fallback():
    """R14a/R8/R31: explain() reports a non-UTF-8-encodable pattern as
    fallback-only (not eligible)."""
    info = pre.explain("\ud800abc")
    assert info["eligible"] is False, (
        f"surrogate pattern must be fallback-only: {info}")
    assert info["engine"] == "fallback"
