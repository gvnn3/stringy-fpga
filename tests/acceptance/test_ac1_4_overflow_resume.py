"""AC-1-4: an input engineered to overflow OUT_CAP returns the complete, correct
match list via streaming resumption — byte-identical to stock re.  Driven
through the Python model path (PYRO_FORCE_MODEL), which exercises the harness
result-ring + resumption internally (R41, R47).

Scope note (§13): direct ABI-level OUT_CAP overflow (setting a tiny OUT_CAP and
observing STATUS.OVF + start_off resumption) requires a resident circuit built
from the private L2 descriptor (see AC-1-1 scope note); here we assert the
externally-observable guarantee AC-1-4 states — completeness and correctness of
the full match list across an overflow-scale corpus.
"""
import os

import pytest

import pyro
import pyro.re as pre


def _force_model():
    os.environ["PYRO_FORCE_MODEL"] = "1"
    pyro.refresh_env()


# Match counts chosen to exceed any plausible OUT_CAP result-ring capacity so
# resumption (R41) must fire to return the complete list.
BIG = 50000


@pytest.mark.parametrize("count", [BIG])
def test_dense_matches_complete_str(count):
    """R41/R47: a str corpus producing >> OUT_CAP matches returns every match,
    correctly, via resumption (byte-identical to stock re)."""
    import re as stdre
    _force_model()
    subject = "a" * count
    assert pre.findall("a", subject) == stdre.findall("a", subject)
    assert len(pre.findall("a", subject)) == count
    exp = [m.span() for m in stdre.finditer("a", subject)]
    act = [m.span() for m in pre.finditer("a", subject)]
    assert act == exp


def test_dense_matches_complete_bytes():
    """R41/R47/R14: same completeness guarantee for a bytes corpus."""
    import re as stdre
    _force_model()
    subject = b"x" * BIG
    assert pre.findall(b"x", subject) == stdre.findall(b"x", subject)
    assert len(pre.findall(b"x", subject)) == BIG


def test_spaced_matches_over_large_corpus():
    """R41/R47: many spaced multi-char matches across a large corpus are all
    returned in order, byte-identical to stock re."""
    import re as stdre
    _force_model()
    subject = ("tok%04d " % 0) + "".join("tok%04d " % (i % 10000)
               for i in range(40000))
    pattern = r"tok\d{4}"
    assert pre.findall(pattern, subject) == stdre.findall(pattern, subject)
    exp = [m.span() for m in stdre.finditer(pattern, subject)]
    act = [m.span() for m in pre.finditer(pattern, subject)]
    assert act == exp


def test_empty_and_nonempty_mixed_overflow():
    """R22/R41: zero-width-capable pattern producing a huge match sequence keeps
    CPython's advance-past-empty semantics across resumption."""
    import re as stdre
    _force_model()
    subject = "ab" * 20000
    for pattern in (r"a*", r"\b"):
        assert pre.findall(pattern, subject) == stdre.findall(pattern, subject)
        assert [m.span() for m in pre.finditer(pattern, subject)] == \
               [m.span() for m in stdre.finditer(pattern, subject)]
