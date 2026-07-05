"""AC-0-2: differential oracle over §5.1 constructs — every
search/match/fullmatch/findall/finditer/sub/subn/split result is byte-identical
to stock re.  (R16, R29, R54)

Each supported triple is run in two routing configurations:
  * "default"      — clean env; short subjects route to fallback (R51 step 4),
                     proving the delegate/fallback path is byte-identical (R29).
  * "force_model"  — PYRO_FORCE_MODEL=1 sampled via refresh_env(), forcing
                     HW-eligible work onto the software model path (R51 step 4
                     carve-out), proving R16 on the accelerated path.

Comparison is by span+groups+lastindex+lastgroup (+full spans/attrs), not
identity (R36a); isinstance against re.Pattern/re.Match is deliberately NOT
asserted here (R36a).
"""

import os

import pytest

import pyro
import pyro.re as pre
import oracle


def _apply_env(mode):
    if mode == "force_model":
        os.environ["PYRO_FORCE_MODEL"] = "1"
    pyro.refresh_env()


@pytest.mark.parametrize("mode", ["default", "force_model"])
@pytest.mark.parametrize("entry", oracle.SUPPORTED, ids=[e[0] for e in oracle.SUPPORTED])
def test_supported_construct_byte_identical(entry, mode):
    """R16/R29/R54: each §5.1 construct is byte-identical to stock re on all
    eight APIs, on both the fallback and the forced-model routing paths."""
    label, pattern, flags, subject = entry
    _apply_env(mode)
    oracle.assert_equivalent(pre, pattern, subject, flags, label=f"{label}/{mode}")


@pytest.mark.parametrize("mode", ["default", "force_model"])
@pytest.mark.parametrize("entry", oracle.ANCHOR_ALT, ids=[e[0] for e in oracle.ANCHOR_ALT])
def test_anchored_alternation_group_selection(entry, mode):
    """R9/R16/R17/R18/R54: group-differentiating alternations with a trailing
    anchor that fails mid-string select the correct capturing branch — spans,
    groups, lastindex and lastgroup byte-identical to stock re across
    search/match/fullmatch/finditer (+findall/sub/subn/split), on both routing
    paths.  Covers the anchored end-context reconstruction case."""
    label, pattern, flags, subject = entry
    _apply_env(mode)
    oracle.assert_equivalent(pre, pattern, subject, flags, label=f"{label}/{mode}")


@pytest.mark.parametrize("mode", ["default", "force_model"])
def test_greedy_lazy_group_boundaries(mode):
    """R23/R17/R18: greedy vs lazy quantifiers that move captured-group
    boundaries return CPython leftmost-greedy spans byte-identically."""
    _apply_env(mode)
    for label, pattern, flags, subject in oracle.GREEDY_LAZY:
        oracle.assert_equivalent(pre, pattern, subject, flags, label=f"{label}/{mode}")


def test_compiled_pattern_methods_match_stock():
    """R27: compiled Pattern exposes search/match/fullmatch/findall/finditer/
    sub/subn/split with pos/endpos semantics identical to re.Pattern."""
    import re as stdre

    p_std = stdre.compile(r"(\w)(\d)")
    p_pyro = pre.compile(r"(\w)(\d)")
    subject = "a1 b2 c3 d4"

    # pos / endpos parameters
    assert oracle.canon_match(p_pyro.search(subject, 3, 8)) == \
        oracle.canon_match(p_std.search(subject, 3, 8))
    assert oracle.canon_match(p_pyro.match(subject, 3)) == \
        oracle.canon_match(p_std.match(subject, 3))
    assert p_pyro.findall(subject, 0, 5) == p_std.findall(subject, 0, 5)
    assert oracle.canon_iter(p_pyro.finditer(subject)) == \
        oracle.canon_iter(p_std.finditer(subject))

    # Pattern attributes (R27)
    assert p_pyro.pattern == p_std.pattern
    assert p_pyro.flags == p_std.flags
    assert p_pyro.groups == p_std.groups
    assert dict(p_pyro.groupindex) == dict(p_std.groupindex)
