"""AC-1-7: asynchrony correctness keystone — for the same pattern/subject,
results are byte-identical whether served cold-fallback, via the model, or via a
"resident" model circuit, proving R36's asynchrony clause and R53 without
hardware.  (R36, R53, R58a)

Two layers:
  * in-process: disabled(fallback) vs forced-model vs stock re must all agree;
  * out-of-process (subprocess worker): a pattern is driven cold→hot→warm/
    resident and served via the disabled / model / resident configurations,
    each compared to stock re computed here in the parent (the oracle).
"""
import os
import re as stdre
import tempfile

import pytest

import pyro
import pyro.re as pre
import oracle
import phase1_support

CASES = [
    (r"(\w+)@(\w+)", "a@b and cd@ef gh@ij"),
    (r"(?P<a>foo)$|(?P<b>foo)", "foobar foo"),
    (r"\d{2,4}", "1 22 333 4444 55555"),
    (r"(a.*b)c", "aXbXbc"),
    (r"^\w+", "alpha\nbeta"),
    (r"[A-Z]+", "abcDEFghiJKL"),
    # must_advance: lazy/empty-preferring quantifiers (R22) across tiers.
    (r"a??", "aa"),
    (r".*?", "ab"),
    (r"a*?b?", "ab"),
    (r"(a??)(b?)", "ab"),
    (r"(a|)??", "aa"),
]


def _canon_stock(pattern, subject):
    return {
        "search": _c(stdre.search(pattern, subject)),
        "findall": stdre.findall(pattern, subject),
        "finditer": [_c(m) for m in stdre.finditer(pattern, subject)],
        "sub": stdre.sub(pattern, "X", subject),
        "split": stdre.split(pattern, subject),
    }


def _c(m):
    if m is None:
        return None
    return {
        "span": list(m.span()),
        "group0": m.group(0),
        "groups": list(m.groups()),
        "spans": [list(m.span(i)) for i in range(len(m.groups()) + 1)],
        "lastindex": m.lastindex,
        "lastgroup": m.lastgroup,
        "groupdict": dict(sorted(m.groupdict().items())),
    }


@pytest.mark.parametrize("pattern,subject", CASES)
def test_inprocess_tier_equivalence(pattern, subject):
    """R36/R53: disabled(fallback) and forced-model serving paths both equal
    stock re for the same input (tier-independent observable result)."""
    expected = _canon_stock(pattern, subject)

    os.environ["PYRO_DISABLE"] = "1"
    os.environ.pop("PYRO_FORCE_MODEL", None)
    pyro.refresh_env()
    disabled = {
        "search": _c(pre.search(pattern, subject)),
        "findall": pre.findall(pattern, subject),
        "finditer": [_c(m) for m in pre.finditer(pattern, subject)],
        "sub": pre.sub(pattern, "X", subject),
        "split": pre.split(pattern, subject),
    }

    os.environ.pop("PYRO_DISABLE", None)
    os.environ["PYRO_FORCE_MODEL"] = "1"
    pyro.refresh_env()
    model = {
        "search": _c(pre.search(pattern, subject)),
        "findall": pre.findall(pattern, subject),
        "finditer": [_c(m) for m in pre.finditer(pattern, subject)],
        "sub": pre.sub(pattern, "X", subject),
        "split": pre.split(pattern, subject),
    }

    assert disabled == expected, "cold-fallback path differs from stock re"
    assert model == expected, "model path differs from stock re"


@pytest.mark.parametrize("pattern,subject", [
    (r"(\w+)@(\w+)", "a@b and cd@ef"),
    (r"(?P<a>foo)$|(?P<b>foo)", "foobar foo"),
    (r"\d{2,4}", "1 22 333 4444"),
    (r"a??", "aa"),                 # must_advance across cold/model/resident
    (r"(a??)(b?)", "ab"),           # must_advance with capture groups
])
def test_subprocess_cold_model_resident_equivalence(pattern, subject):
    """R58a/R36/R53 keystone: a pattern driven cold→hot→warm/resident is served
    byte-identically across the disabled / model / resident configurations, each
    equal to stock re — proving results never depend on the circuit tier."""
    # Canonicalize BOTH sides through the JSON shape (tuples->lists) so the
    # in-process oracle (stock re, which returns tuples from findall) compares
    # symmetrically against the worker result that crossed a JSON boundary.
    expected = phase1_support.jsonify(_canon_stock(pattern, subject))
    with tempfile.TemporaryDirectory(prefix="pyro_tier_") as cache:
        result, _out, _err = phase1_support.run_worker(
            "tier_equiv", pattern, subject, cache_dir=cache)
    for tier in ("disabled", "model", "resident"):
        assert phase1_support.jsonify(result[tier]) == expected, (
            f"tier {tier!r} (circuit_status={result.get('circuit_status')}) "
            f"differs from stock re"
        )
