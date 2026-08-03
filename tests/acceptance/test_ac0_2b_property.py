"""AC-0-2 / R55: property-based generation.

A deterministic, seeded generator (stdlib random only; no hypothesis) produces:
  * random patterns from the §5.1 supported grammar with random subjects, and
  * random arbitrary pattern strings,
and asserts equivalence to stock re (R16, R29, R30, R55): PYRO never returns a
wrong result — it either equals CPython, or (for anything it rejects/falls back
on) equals CPython trivially.  Runs under default routing and forced-model
routing.

The seed is fixed so failures are reproducible.  Expectations derive solely from
stock re (never from running the implementation).
"""

import os
import random
import re as stdre

import pytest

import pyro
import pyro.re as pre
import oracle

SEED = 0xC0FFEE
N_SUPPORTED = 400
N_ARBITRARY = 400


def _apply_env(mode):
    if mode == "force_model":
        os.environ["PYRO_FORCE_MODEL"] = "1"
    pyro.refresh_env()


def _equal_or_both_error(pattern, subject, flags, mode):
    # Determine stock behaviour first (the oracle).
    try:
        stdre.compile(pattern, flags)
    except stdre.error:
        # R30: pyro must reject exactly what stock rejects.
        with pytest.raises(stdre.error):
            pre.compile(pattern, flags)
        return
    oracle.assert_equivalent(
    pre,
    pattern,
    subject,
    flags,
     label=f"prop/{mode}")


@pytest.mark.parametrize("mode", ["default", "force_model"])
def test_supported_grammar_equivalence(mode):
    """R55: random supported-grammar patterns match stock re on all APIs."""
    _apply_env(mode)
    rng = random.Random(SEED)
    for _ in range(N_SUPPORTED):
        pattern = oracle.gen_supported_pattern(rng)
        subject = oracle.gen_subject(rng)
        _equal_or_both_error(pattern, subject, 0, mode)


@pytest.mark.filterwarnings("ignore::FutureWarning")
@pytest.mark.parametrize("mode", ["default", "force_model"])
def test_arbitrary_patterns_never_wrong(mode):
    """R55/R30: random arbitrary patterns never yield a wrong result — either
    equal to CPython, or a matching re.error rejection."""
    _apply_env(mode)
    rng = random.Random(SEED ^ 0x1234)
    for _ in range(N_ARBITRARY):
        pattern = oracle.gen_arbitrary_pattern(rng)
        subject = oracle.gen_subject(rng)
        _equal_or_both_error(pattern, subject, 0, mode)


@pytest.mark.parametrize("mode", ["default", "force_model"])
def test_bytes_subject_fuzz(mode):
    """R55/R14: random byte subjects against byte patterns match stock re."""
    _apply_env(mode)
    rng = random.Random(SEED ^ 0xBEEF)
    byte_pats = [rb"[a-c]+", rb"\d+", rb"\w+", rb"a.b", rb"x*y", rb"(ab)+"]
    for _ in range(200):
        pattern = rng.choice(byte_pats)
        subject = bytes(rng.randint(0, 255) for _ in range(rng.randint(0, 20)))
        oracle.assert_equivalent(
    pre, pattern, subject, 0, label=f"bytesfuzz/{mode}")
