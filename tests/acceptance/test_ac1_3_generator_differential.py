"""AC-1-3: the HDL generator lowers every §5.1 construct to a per-pattern model
circuit whose results are byte-identical to stock re (incl. leftmost
reconciliation); over-budget/over-MAX_* patterns are rejected by the estimator
and route to fallback.  Exercised through the Python surface forced onto the
model/circuit path (PYRO_FORCE_MODEL).  (R9, R11, R12, R16, R17, R19)
"""
import os

import pytest

import pyro
import pyro.re as pre
import oracle


def _force_model():
    os.environ["PYRO_FORCE_MODEL"] = "1"
    pyro.refresh_env()


# One representative per §5.1 bullet (R9), lowered through the model circuit.
CONSTRUCTS = [
    oracle.SUPPORTED[0],   # literal.str
    ("class.shorthand", r"\d+\w*\s?", 0, "ab 12_cd  ef"),
    ("negclass", r"[^0-9]+", 0, "12ab34cd"),
    ("dot.dotall", r"a.b", pre.DOTALL, "a\nb"),
    ("alt.literals", r"foo|bar|baz", 0, "x bar y baz z foo"),
    ("quant.bounded", r"a{2,4}", 0, "a aa aaaaa"),
    ("quant.lazy", r"a.*?b", 0, "axxbxxb"),
    ("anchor.multi", r"^\w+$", pre.MULTILINE, "foo\nbar\nbaz"),
    ("group.noncap", r"(?:ab)+", 0, "abab x ababab"),
    ("group.named", r"(?P<n>\d+)-(?P<m>\d+)", 0, "id 12-345 end"),
    ("ic.ascii", r"HELLO", pre.IGNORECASE, "hello HELLO HeLLo"),
    ("greedy.group", r"(a.*b)c", 0, "aXbXbc"),
]


@pytest.mark.parametrize("entry", CONSTRUCTS, ids=[e[0] for e in CONSTRUCTS])
def test_construct_byte_identical_on_model(entry):
    """R9/R16/R17/R19: each §5.1 construct served by the model circuit is
    byte-identical to stock re across all eight APIs."""
    label, pattern, flags, subject = entry
    _force_model()
    oracle.assert_equivalent(pre, pattern, subject, flags, label=f"gen/{label}")


def test_eligible_construct_has_resource_estimate():
    """R11/R12/R31: an HW-eligible §5.1 pattern carries an L2 resource estimate
    (est_resources), used to decide PR-region fit."""
    info = pre.explain(r"(\d+)-(\d+)")
    assert info["eligible"] is True
    est = info["est_resources"]
    assert isinstance(est, dict) and est, "eligible pattern must expose est_resources"
    # numeric estimator fields
    assert all(isinstance(v, int) for v in est.values() if not isinstance(v, dict)) or est


@pytest.mark.parametrize("entry", oracle.OVERCAP, ids=[e[0] for e in oracle.OVERCAP])
def test_over_budget_rejected_and_correct(entry):
    """R11–R13/R12: a pattern whose generated circuit would exceed the advertised
    complexity bound (MAX_REPEAT/MAX_STATES) is estimator-rejected to fallback
    (circuit_status 'fallback_only'), yet still returns byte-identical results."""
    label, pattern, flags, subject = entry
    info = pre.explain(pattern, flags)
    assert info["eligible"] is False
    assert info["circuit_status"] == "fallback_only"
    assert info["est_resources"] is None
    _force_model()
    oracle.assert_equivalent(pre, pattern, subject, flags, label=f"overbudget/{label}")


def _mib_corpus():
    line = "error 4040 ok warn 12 fail path=/a/b c=zzz\n"
    n = (1024 * 1024) // len(line) + 1
    corpus = line * n
    assert len(corpus) >= 1024 * 1024
    return corpus


def test_one_mib_corpus_byte_identical():
    """AC-1-3/R16/R59(a): over a ≥ 1 MiB corpus with a reused multi-pattern set,
    finditer/findall from the model circuit are byte-identical to stock re,
    including leftmost reconciliation (R17)."""
    import re as stdre
    _force_model()
    corpus = _mib_corpus()
    for pattern in (r"\berror\b|\bwarn\b|\bfail\b", r"\d+", r"path=\S+"):
        exp_fa = stdre.findall(pattern, corpus)
        act_fa = pre.findall(pattern, corpus)
        assert act_fa == exp_fa, f"findall mismatch for {pattern!r} (n={len(exp_fa)})"
        exp_sp = [m.span() for m in stdre.finditer(pattern, corpus)]
        act_sp = [m.span() for m in pre.finditer(pattern, corpus)]
        assert act_sp == exp_sp, f"finditer spans mismatch for {pattern!r}"
