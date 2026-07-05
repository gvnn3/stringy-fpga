"""AC-0-3: every §5.2 construct and every over-capacity pattern is classified
fallback-only by explain(), and still returns correct results via fallback.
(R8, R10, R12, R31, R56)
"""

import pytest

import pyro.re as pre
import oracle


def _assert_eligible(pattern, flags=0):
    info = pre.explain(pattern, flags)
    assert isinstance(info, dict), f"explain() must return dict, got {type(info)}"
    for key in ("eligible", "reason", "engine", "states"):
        assert key in info, f"explain() missing key {key!r}: {info}"
    assert info["eligible"] is True, f"expected eligible for {pattern!r}: {info}"
    assert info["engine"] in ("fpga", "model"), f"engine for eligible {pattern!r}: {info}"
    assert isinstance(info["reason"], str)
    assert info["states"] is None or isinstance(info["states"], int)


def _assert_fallback(pattern, flags=0):
    info = pre.explain(pattern, flags)
    assert isinstance(info, dict), f"explain() must return dict, got {type(info)}"
    for key in ("eligible", "reason", "engine", "states"):
        assert key in info, f"explain() missing key {key!r}: {info}"
    assert info["eligible"] is False, f"expected fallback-only for {pattern!r}: {info}"
    assert info["engine"] == "fallback", f"engine must be 'fallback' for {pattern!r}: {info}"
    assert isinstance(info["reason"], str) and info["reason"], \
        f"reason must be a non-empty string for {pattern!r}: {info}"


@pytest.mark.parametrize("entry", oracle.SUPPORTED, ids=[e[0] for e in oracle.SUPPORTED])
def test_supported_classified_eligible(entry):
    """R8/R9/R31/R56: §5.1 constructs classify HW-eligible via explain()."""
    label, pattern, flags, _subject = entry
    _assert_eligible(pattern, flags)


@pytest.mark.parametrize("entry", oracle.UNSUPPORTED, ids=[e[0] for e in oracle.UNSUPPORTED])
def test_unsupported_classified_fallback(entry):
    """R10/R31/R56: §5.2 constructs classify fallback-only via explain()."""
    label, pattern, flags, _subject = entry
    _assert_fallback(pattern, flags)


@pytest.mark.parametrize("entry", oracle.UNSUPPORTED, ids=[e[0] for e in oracle.UNSUPPORTED])
def test_unsupported_results_correct_via_fallback(entry):
    """R8/R29: fallback-only §5.2 patterns still return byte-identical results."""
    label, pattern, flags, subject = entry
    oracle.assert_equivalent(pre, pattern, subject, flags, label=f"fallback:{label}")


@pytest.mark.parametrize("entry", oracle.OVERCAP, ids=[e[0] for e in oracle.OVERCAP])
def test_overcapacity_classified_fallback(entry):
    """R11-R13/R12/R45/R56: patterns exceeding advertisable capacity classify
    fallback-only (the CAPS1 MAX_REPEAT field is 16-bit, so 70000 exceeds any
    conforming engine's advertisable maximum)."""
    label, pattern, flags, _subject = entry
    _assert_fallback(pattern, flags)


@pytest.mark.parametrize("entry", oracle.OVERCAP, ids=[e[0] for e in oracle.OVERCAP])
def test_overcapacity_results_correct_via_fallback(entry):
    """R12/R29: over-capacity patterns still return byte-identical results."""
    label, pattern, flags, subject = entry
    oracle.assert_equivalent(pre, pattern, subject, flags, label=f"overcap:{label}")


def test_explain_is_pure_function():
    """R8: classification is a deterministic, side-effect-free function of
    (pattern, flags) — repeated calls agree."""
    for pattern, flags in [(r"abc", 0), (r"(a)\1", 0), (r"a{70000}", 0),
                           (r"[a-z]+", pre.IGNORECASE)]:
        first = pre.explain(pattern, flags)
        for _ in range(3):
            assert pre.explain(pattern, flags) == first


def test_flags_change_classification_ignorecase_fullfold():
    """R15: str IGNORECASE full/multi-char folding (ß) is fallback-only, but the
    result via fallback stays byte-identical to stock re."""
    # Whatever stock re does for ß under IGNORECASE, pyro must match it.
    oracle.assert_equivalent(pre, "ß", "straße STRASSE ss", pre.IGNORECASE, label="ß")
