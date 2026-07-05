"""Differential tests against stock ``re`` (R16/R29/R54, AC-0-2/AC-0-7).

Every PYRO API result must be byte-identical to CPython ``re``.  Each case is
run twice: once through the normal router (short inputs -> fallback) and once
with PYRO_FORCE_MODEL=1 (model path + hybrid), so both code paths are proven.
"""
import re

import pytest

import pyro.re as pre
from pyro import _route

CASES = [
    # (pattern, flags, subject)
    (r"\d+", 0, "abc 123 def 456"),
    (r"[a-z]+", 0, "Hello World"),
    (r"[^0-9]+", 0, "a1b2c3"),
    (r"\w+@\w+\.\w+", 0, "mail me@host.com now"),
    (r"a|bb|ccc", 0, "xcccbba"),
    (r"(foo|bar)+", 0, "foobarfoo"),
    (r"a*", 0, "baaab"),
    (r"a+?", 0, "aaaa"),
    (r"a{2,4}", 0, "aaaaaa"),
    (r"a{3}", 0, "aaaa"),
    (r"^\w+$", re.M, "one\ntwo\nthree"),
    (r"\bcat\b", 0, "a cat category cat."),
    (r".", re.S, "a\nb"),
    (r".", 0, "a\nb"),
    (r"colou?r", 0, "color colour"),
    (r"(\d{4})-(\d{2})-(\d{2})", 0, "date 2026-07-05 end"),
    (r"(?P<y>\d+)h(?P<m>\d+)", 0, "12h30"),
    (r"HELLO", re.I, "hello HeLLo"),
    (r"\s+", 0, "a  b\tc\nd"),
    (r"<(.*?)>", 0, "<a><bb><ccc>"),
    (r"", 0, "abc"),
    (rb"\d+", 0, b"abc 123 def"),
    (rb"[A-Z]+", re.I, b"abcDEF"),
]


def _run_ops(mod, pattern, flags, subject):
    p = mod.compile(pattern, flags)
    out = {
        "search": p.search(subject),
        "match": p.match(subject),
        "fullmatch": p.fullmatch(subject),
        "findall": p.findall(subject),
        "finditer": [m.span(0) for m in p.finditer(subject)],
        "split": p.split(subject),
    }
    # normalize match objects to comparable tuples
    for k in ("search", "match", "fullmatch"):
        m = out[k]
        out[k] = None if m is None else (m.span(0), m.group(0))
    repl = "#" if isinstance(subject, str) else b"#"
    out["sub"] = p.sub(repl, subject)
    out["subn"] = p.subn(repl, subject)
    return out


@pytest.mark.parametrize("pattern,flags,subject", CASES)
def test_differential_fallback_path(pattern, flags, subject, monkeypatch):
    monkeypatch.delenv("PYRO_FORCE_MODEL", raising=False)
    monkeypatch.delenv("PYRO_DISABLE", raising=False)
    pre.purge()
    assert _run_ops(pre, pattern, flags, subject) == _run_ops(
        re, pattern, flags, subject)


@pytest.mark.parametrize("pattern,flags,subject", CASES)
def test_differential_model_path(pattern, flags, subject, monkeypatch):
    monkeypatch.setenv("PYRO_FORCE_MODEL", "1")
    monkeypatch.delenv("PYRO_DISABLE", raising=False)
    pre.purge()
    _route.reset_stats()
    assert _run_ops(pre, pattern, flags, subject) == _run_ops(
        re, pattern, flags, subject)
    # search/match/fullmatch/finditer for eligible patterns took the model path
    assert pre.stats()["model"] > 0


# --- R30: exceptions identical to stock re --------------------------------

@pytest.mark.parametrize("bad", [r"(", r"[a-", r"*abc", r"a{2,1}", r"(?P<>x)"])
def test_invalid_patterns_raise_re_error(bad):
    with pytest.raises(re.error):
        pre.compile(bad)
    with pytest.raises(re.error):
        pre.explain(bad)


# --- AC-0-3: unsupported / over-capacity still return correct via fallback -

@pytest.mark.parametrize("pattern,subject", [
    (r"(a+)\1", "aa aaaa"),
    (r"(?=abc)abc", "abc"),
    (r"(?<=x)y", "xy zy"),
    (r"a{500}", "b"),
])
def test_fallback_only_still_correct(pattern, subject):
    assert not pre.explain(pattern)["eligible"]
    m = pre.search(pattern, subject)
    ref = re.search(pattern, subject)
    assert (m is None) == (ref is None)
    if ref is not None:
        assert m.span(0) == ref.span(0)
