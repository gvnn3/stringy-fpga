"""Mock toolchain + R47b manifest contract, incl. R19c (R63b/R47b/R19c).

The mock toolchain (R63b) consumes the generator's RTL + metadata and emits a
stub PR artifact + manifest.  The manifest MUST carry the R47b fields including
the R19c over-approximation declaration; an exact circuit declares an empty set.
"""

import pytest

import pyro.hdl as hdl
from pyro.synth import MockToolchain, ToolchainConfig, SynthesisFailed
from pyro.synth.residency import _job_from_circuit
from pyro.synth.toolchain import TOOLCHAIN_VERSION, SHELL_VERSION


def _job(pattern, flags=0):
    return _job_from_circuit(hdl.generate(pattern, flags))


def test_stub_artifact_is_deterministic():
    tc = MockToolchain(ToolchainConfig())
    p1, m1 = tc.run(_job("abc[0-9]+"))
    p2, m2 = tc.run(_job("abc[0-9]+"))
    assert p1 == p2
    assert m1 == m2


def test_manifest_has_r47b_fields():
    _, m = MockToolchain().run(_job("abc[0-9]+"))
    # identity, versions, target region, utilization/timing, integrity.
    assert m.toolchain_version == TOOLCHAIN_VERSION
    assert m.shell_version == SHELL_VERSION
    assert m.harness_version == hdl.HARNESS_VERSION
    assert m.generator_version == hdl.GENERATOR_VERSION
    assert m.luts > 0 and m.ffs > 0
    assert m.met_timing is True and m.fmax_mhz > 0
    assert len(m.pattern_hash) == 32          # 128-bit hex
    assert m.payload_len > 0 and len(m.integrity_hash) == 8


def test_manifest_declares_over_approx_classes_r19c():
    # \w+ over-approximates the Unicode word category in str mode.
    _, m = MockToolchain().run(_job(r"\w+"))
    assert hdl.OA_UNICODE_CATEGORY in m.over_approx_classes
    assert m.estimated_fp_rate > 0.0


def test_exact_circuit_declares_empty_over_approx_r19c():
    # A pure-ASCII byte pattern is exactly recognized: empty OA set, 0.0 FP rate.
    _, m = MockToolchain().run(_job(rb"abc[0-9]+"))
    assert m.over_approx_classes == []
    assert m.estimated_fp_rate == 0.0


def test_word_boundary_over_approx_declared_r19c():
    _, m = MockToolchain().run(_job(r"\bword\b"))
    assert hdl.OA_WORD_BOUNDARY_UTF8 in m.over_approx_classes


def test_integrity_hash_matches_payload():
    payload, m = MockToolchain().run(_job("abc[0-9]+"))
    assert m.integrity_ok(payload) is True


def test_fail_mode_error_raises_synthesisfailed():
    tc = MockToolchain(ToolchainConfig(fail_mode="error"))
    with pytest.raises(SynthesisFailed):
        tc.run(_job("abc[0-9]+"))


def test_fail_mode_timeout_raises_synthesisfailed():
    tc = MockToolchain(ToolchainConfig(fail_mode="timeout"))
    with pytest.raises(SynthesisFailed):
        tc.run(_job("abc[0-9]+"))
