"""AC-0-4: capture-group access triggers the hybrid re-run and returns
byte-identical groups/spans; accessing only group 0 does not re-run.
(R18, R20)

To exercise the hybrid model, HW-eligible group-bearing patterns are routed
onto the software-model path two ways: PYRO_FORCE_MODEL=1 (short subjects) and a
>= S_min subject (which bypasses the R51 step-4 loss-regime fallback).  On the
fallback path there is no hybrid, so forcing the model is required to test R18.

The "group-0 access does not re-run" clause (R20) has no public re-run counter;
this file verifies the *observable* half — group-0/span/start/end are correct
without touching groups>0 — and the report records that no-re-run is not
publicly falsifiable in Phase 0.
"""

import os
import re as stdre

import pytest

import pyro
import pyro.re as pre
import oracle

GROUP_PATTERNS = [
    ("two_num", r"(\d+)-(\d+)", "abc 12-345 xyz"),
    ("named", r"(?P<user>\w+)@(?P<host>\w+)", "mail alice@example rest"),
    ("nested", r"((a)(b))+", "xababy"),
    ("optional", r"(a)?(b)", "b ab"),
    ("greedy_group", r"(a.*b)c", "aXbXbc"),
    ("lazy_group", r"(a.*?b)c", "aXbXbc"),
    ("alt_group", r"(foo)|(bar)", "see bar and foo"),
]

LARGE_PREFIX = "x" * (oracle.S_MIN + 16)


def _force_model():
    os.environ["PYRO_FORCE_MODEL"] = "1"
    pyro.refresh_env()


@pytest.mark.parametrize("entry", GROUP_PATTERNS,
                         ids=[e[0] for e in GROUP_PATTERNS])
def test_hybrid_groups_byte_identical_forced_model(entry):
    """R18/R16: on the forced-model path, group extraction (groups/groupdict/
    span(n>0)/expand) is byte-identical to stock re."""
    label, pattern, subject = entry
    _force_model()
    oracle.assert_equivalent(pre, pattern, subject, 0, label=f"hybrid/{label}")


@pytest.mark.parametrize("entry", GROUP_PATTERNS,
                         ids=[e[0] for e in GROUP_PATTERNS])
def test_hybrid_groups_large_subject_model_path(entry):
    """R18/R21: with a >= S_min subject (model path), group spans/values remain
    byte-identical including code-point offset translation past the prefix."""
    label, pattern, subject = entry
    long_subject = LARGE_PREFIX + subject
    oracle.assert_equivalent(
    pre,
    pattern,
    long_subject,
    0,
     label=f"hybrid-large/{label}")


def test_expand_template_byte_identical():
    """R18/R28: expand()/\\g<n> templates (which observe groups) match stock re
    on the forced-model path."""
    _force_model()
    pattern = r"(?P<a>\w+):(?P<b>\w+)"
    subject = "key:value pair"
    m_pyro = pre.search(pattern, subject)
    m_std = stdre.search(pattern, subject)
    for tmpl in [r"\2-\1", r"\g<b>=\g<a>", r"[\g<0>]", r"\1\1"]:
        assert m_pyro.expand(tmpl) == m_std.expand(
            tmpl), f"expand({tmpl!r}) mismatch"


def test_group0_only_access_correct():
    """R20: accessing only group 0 / span(0) / start / end yields correct values
    (byte-identical to stock re) without observing groups>0.

    NOTE: R20 also states such access MUST NOT trigger a hybrid re-run; Phase 0
    exposes no public re-run counter, so the no-re-run half is not falsifiable
    from the public surface (recorded in the report)."""
    _force_model()
    pattern = r"(\d+)-(\d+)"
    subject = "id 12-345 end"
    m_pyro = pre.search(pattern, subject)
    m_std = stdre.search(pattern, subject)
    assert m_pyro.group(0) == m_std.group(0)
    assert m_pyro[0] == m_std[0]
    assert m_pyro.span(0) == m_std.span(0)
    assert m_pyro.span() == m_std.span()
    assert m_pyro.start() == m_std.start()
    assert m_pyro.end() == m_std.end()
    # Now group>0 access must still be correct after group-0-only access.
    assert m_pyro.group(1) == m_std.group(1)
    assert m_pyro.groups() == m_std.groups()
