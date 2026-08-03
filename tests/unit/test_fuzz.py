"""Lightweight differential fuzz (R16/R55 flavor) over supported patterns.

Random subjects are matched with PYRO and stock ``re`` and required to agree,
on both the fallback path and the forced-model path.  Also exercises the
natural size-gate crossing into the model path with a large corpus.
"""
import random
import re

import pytest

import pyro
import pyro.re as pre
from pyro import _route

PATTERNS = [
    r"\d+", r"[a-z]+", r"\w+", r"a|bb|ccc", r"(ab)+", r"x*y",
    r"\bfoo\b", r"[^ ]+", r"a.c", r"(\d)(\d)?", r"colou?r", r"a{1,3}",
]

ALPHABET = "abcxyz012 \tfooc"


def _norm(m):
    return None if m is None else (m.span(0), m.group(0))


def _iter_spans(it):
    return [m.span(0) for m in it]


@pytest.mark.parametrize("seed", range(40))
def test_fuzz_agrees_with_re(seed, monkeypatch):
    rng = random.Random(seed)
    pat = rng.choice(PATTERNS)
    subj = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 40)))

    force = bool(seed % 2)
    if force:
        monkeypatch.setenv("PYRO_FORCE_MODEL", "1")
    else:
        monkeypatch.delenv("PYRO_FORCE_MODEL", raising=False)
    monkeypatch.delenv("PYRO_DISABLE", raising=False)
    pyro.refresh_env()
    pre.purge()

    p = pre.compile(pat)
    r = re.compile(pat)
    assert _norm(p.search(subj)) == _norm(r.search(subj)), (pat, subj)
    assert _norm(p.match(subj)) == _norm(r.match(subj)), (pat, subj)
    assert _norm(p.fullmatch(subj)) == _norm(r.fullmatch(subj)), (pat, subj)
    assert _iter_spans(
    p.finditer(subj)) == _iter_spans(
        r.finditer(subj)), (pat, subj)
    assert p.findall(subj) == r.findall(subj), (pat, subj)
    assert p.sub("#", subj) == r.sub("#", subj), (pat, subj)
    assert p.split(subj) == r.split(subj), (pat, subj)


def test_large_corpus_crosses_into_model(monkeypatch):
    # Natural routing: a corpus >= S_min must be served by the model without
    # forcing, and must remain byte-identical (R51.4/R16).
    monkeypatch.delenv("PYRO_FORCE_MODEL", raising=False)
    monkeypatch.delenv("PYRO_DISABLE", raising=False)
    pyro.refresh_env()
    pre.purge()
    _route.reset_stats()
    rng = random.Random(1)
    subj = "".join(rng.choice("abc 123\n") for _ in range(_route.S_MIN + 500))
    got = [m.span(0) for m in pre.finditer(r"\d+", subj)]
    ref = [m.span(0) for m in re.finditer(r"\d+", subj)]
    assert got == ref
    assert pre.stats()["model"] == 1
