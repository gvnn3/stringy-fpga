"""AC-0-7: empty-match, multiline/anchor, IGNORECASE-folding, and astral-
codepoint offset cases are byte-identical to stock re.  (R21-R24)

Every case is run under default routing and under PYRO_FORCE_MODEL=1 so the
edge semantics are proven on both the fallback and the accelerated-model paths.
"""

import os

import pytest

import pyro
import pyro.re as pre
import oracle


def _apply(mode):
    if mode == "force_model":
        os.environ["PYRO_FORCE_MODEL"] = "1"
    pyro.refresh_env()


MODES = ["default", "force_model"]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("entry", oracle.EMPTY, ids=[e[0] for e in oracle.EMPTY])
def test_empty_matches(entry, mode):
    """R22: zero-width matches replicate CPython advance-past-empty semantics for
    findall/finditer/sub across all APIs."""
    label, pattern, flags, subject = entry
    _apply(mode)
    oracle.assert_equivalent(pre, pattern, subject, flags, label=f"empty:{label}/{mode}")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("entry", oracle.MUST_ADVANCE, ids=[e[0] for e in oracle.MUST_ADVANCE])
def test_must_advance_lazy_empty(entry, mode):
    """R22: lazy/empty-preferring quantifiers (a??, .*?, a*?, empty-branch
    alternations, empty-preferring-then-atom) reproduce CPython's must_advance
    retry — an empty match at a position followed by a NON-empty match at the
    same start — byte-identically across finditer/findall/sub/split."""
    label, pattern, flags, subject = entry
    _apply(mode)
    oracle.assert_equivalent(pre, pattern, subject, flags, label=f"mustadv:{label}/{mode}")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("entry", oracle.ANCHORS, ids=[e[0] for e in oracle.ANCHORS])
def test_multiline_and_anchors(entry, mode):
    """R24: MULTILINE ^/$, non-MULTILINE $ before trailing \\n, and \\Z are
    byte-identical to stock re."""
    label, pattern, flags, subject = entry
    _apply(mode)
    oracle.assert_equivalent(pre, pattern, subject, flags, label=f"anchor:{label}/{mode}")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("entry", oracle.IGNORECASE, ids=[e[0] for e in oracle.IGNORECASE])
def test_ignorecase_folding(entry, mode):
    """R15/R16: IGNORECASE folding — ASCII/simple folds accelerate, full folds
    (ß<->ss) fall back, but every result is byte-identical to stock re."""
    label, pattern, flags, subject = entry
    _apply(mode)
    oracle.assert_equivalent(pre, pattern, subject, flags, label=f"ic:{label}/{mode}")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("entry", oracle.ASTRAL, ids=[e[0] for e in oracle.ASTRAL])
def test_astral_codepoint_offsets(entry, mode):
    """R21: offsets for astral (>= U+10000) subjects are code-point indices,
    exactly translated back from internal UTF-8, byte-identical to stock re."""
    label, pattern, flags, subject = entry
    _apply(mode)
    oracle.assert_equivalent(pre, pattern, subject, flags, label=f"astral:{label}/{mode}")


@pytest.mark.parametrize("mode", MODES)
def test_astral_offset_after_multiple_astrals(mode):
    """R21: explicit spot-check that a capture after several astral code points
    reports code-point (not UTF-8 byte) offsets."""
    import re as stdre
    _apply(mode)
    subject = "\U0001F600\U0001F601\U0001F602(tail)"
    pattern = r"\((\w+)\)"
    assert oracle.canon_match(pre.search(pattern, subject)) == \
        oracle.canon_match(stdre.search(pattern, subject))
