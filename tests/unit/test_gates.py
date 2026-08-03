"""Phase-0 routing gates (spec v1.2.1): lone surrogates (C1) and non-exact
str/bytes subjects (C2). Both route to plain fallback, byte-identical to stock
re, without touching the (non-existent) device.
"""
import re

import pytest

import pyro
import pyro.re as pre
from pyro import _route

OPS = ("search", "match", "fullmatch")


def _canon(m):
    return None if m is None else (m.span(0), m.group(0), m.groups(),
                                   m.lastindex, m.lastgroup)


@pytest.fixture(params=["default", "force_model"])
def routing(request, monkeypatch):
    if request.param == "force_model":
        monkeypatch.setenv("PYRO_FORCE_MODEL", "1")
    else:
        monkeypatch.delenv("PYRO_FORCE_MODEL", raising=False)
    monkeypatch.delenv("PYRO_DISABLE", raising=False)
    pyro.refresh_env()
    pre.purge()
    _route.reset_stats()
    yield request.param
    pre.purge()


# --- C1: lone-surrogate str subject routes to fallback, matches stock -------

@pytest.mark.parametrize("pat", [r".", r"x?", r"(.)", r"\w*", r".$"])
def test_lone_surrogate_subject(routing, pat):
    # unpaired surrogates: not UTF-8 encodable
    subj = "ab\ud800cd\udfffx"
    p = pre.compile(pat)
    r = re.compile(pat)
    for op in OPS:
        assert _canon(getattr(p, op)(subj)) == _canon(getattr(r, op)(subj)), op
    assert [_canon(m) for m in p.finditer(subj)] == \
           [_canon(m) for m in r.finditer(subj)]
    assert p.findall(subj) == r.findall(subj)
    assert p.sub("Z", subj) == r.sub("Z", subj)
    assert p.subn("Z", subj) == r.subn("Z", subj)
    assert p.split(subj) == r.split(subj)
    # Gated to plain fallback -- no device error was involved.
    assert pre.stats()["fallback_after_error"] == 0
    assert pre.stats()["model"] == 0


def test_lone_surrogate_pattern_classified_fallback():
    info = pre.explain("\ud800")
    assert info["eligible"] is False
    assert info["engine"] == "fallback"


# --- C2: non-exact str/bytes subjects route to fallback ---------------------

def test_bytearray_subject_group0_is_bytes(routing):
    subj = bytearray(b"ab 12 cd 34")
    m = pre.search(rb"\d+", subj)
    ref = re.search(rb"\d+", bytes(subj))
    assert m.group(0) == ref.group(0)
    assert type(m.group(0)) is bytes            # not bytearray
    assert {m.group(0): 1} == {b"12": 1}        # hashable dict key works
    # forced-model must still fall back for a non-exact subject type.
    assert pre.stats()["model"] == 0


def test_memoryview_subject_falls_back(routing):
    subj = memoryview(b"xx 99 yy")
    m = pre.search(rb"\d+", subj)
    assert m.group(0) == b"99"
    assert type(m.group(0)) is bytes
    assert pre.stats()["model"] == 0


@pytest.mark.parametrize("op", ["search", "finditer", "findall", "sub"])
def test_str_subclass_subject_falls_back(routing, op):
    class MyStr(str):
        pass
    subj = MyStr("a1b2c3")
    p = pre.compile(r"\d")
    r = re.compile(r"\d")
    if op == "finditer":
        assert [m.span(0) for m in p.finditer(subj)] == \
               [m.span(0) for m in r.finditer(subj)]
    elif op == "findall":
        assert p.findall(subj) == r.findall(subj)
    elif op == "sub":
        assert p.sub("Z", subj) == r.sub("Z", subj)
    else:
        assert p.search(subj).span(0) == r.search(subj).span(0)
    assert pre.stats()["model"] == 0
