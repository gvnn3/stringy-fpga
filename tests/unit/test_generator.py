"""Unit tests for the HDL generator (pyro.hdl.generator, R45-R50; AC-1-3).

Covers: per-construct lowering to valid RTL, byte-identical determinism,
structural harness-contract invariants (normative CSR offsets, baked identity,
absence of the obsolete PROG_* path), over-budget rejection, and — when
``iverilog`` is on PATH — a Verilog syntax smoke lint of one generated circuit.
"""
import shutil
import subprocess

import pytest

from pyro import hdl
from pyro.hdl import generator as G

_SUBSET = [
    "abc", b"abc", r"foo\d+bar", "[a-z]+", "[^a]x", r"\w\s\d",
    "a|bc|def", "(?:ab)+", "a{2,4}", "colou?r", "^line$", r"\bword\b",
    "é+", "\U0001D518", r"a.c",
]


# --- AC-1-3: every §5.1 construct lowers to RTL ---------------------------

@pytest.mark.parametrize("pat", _SUBSET)
def test_every_construct_lowers_to_rtl(pat):
    c = hdl.generate(pat, 0)
    assert "module pyro_circuit" in c.rtl
    assert c.rtl.endswith("endmodule\n")
    assert c.automaton.n_states >= 1


# --- determinism (byte-identical RTL) ------------------------------------

@pytest.mark.parametrize("pat", _SUBSET)
def test_rtl_is_byte_identical_across_runs(pat):
    assert hdl.generate(pat, 0).rtl == hdl.generate(pat, 0).rtl


def test_different_patterns_differ():
    assert hdl.generate("abc", 0).rtl != hdl.generate("abd", 0).rtl


# --- R45: normative CSR register block ------------------------------------

def test_rtl_has_pyro_magic_and_versions():
    c = hdl.generate("abc", 0)
    assert "32'h5059524F" in c.rtl                 # ID magic "PYRO"
    assert f"32'h{G.HARNESS_VERSION:08X}" in c.rtl
    assert f"32'h{G.GENERATOR_VERSION:08X}" in c.rtl


@pytest.mark.parametrize("off", [
    "16'h0000", "16'h0004", "16'h0008", "16'h000C", "16'h0010", "16'h0014",
    "16'h0018", "16'h001C", "16'h0020", "16'h0024", "16'h0028",
    "16'h0030", "16'h0038", "16'h0048", "16'h004C", "16'h0050", "16'h0054",
])
def test_rtl_decodes_all_normative_offsets(off):
    assert off in hdl.generate("abc", 0).rtl


# --- R47a: baked circuit-identity block -----------------------------------

def test_identity_block_baked_and_hashed():
    c = hdl.generate("abc", 0)
    assert len(c.circ_id) == 4
    assert len(c.pattern_hash16) == 16
    for w in c.circ_id:
        assert f"32'h{w:08X}" in c.rtl
    assert f"32'h{c.circ_flags:08X}" in c.rtl


def test_identity_depends_on_pattern_flags_and_versions():
    a = hdl.generate("abc", 0)
    b = hdl.generate("abd", 0)
    import re as _re
    c = hdl.generate("abc", _re.I)
    assert a.circ_id != b.circ_id      # different pattern
    assert a.circ_id != c.circ_id      # different flags


@pytest.mark.parametrize("a,fa,b,fb", [
    ("(?i)abc", 0, "abc", __import__("re").I),
    ("(?ms)x.y", 0, "x.y", __import__("re").M | __import__("re").S),
    (b"(?i)ab", 0, b"ab", __import__("re").I),
])
def test_global_inline_flags_canonicalize_to_same_identity(a, fa, b, fb):
    # (?i)abc and abc+re.I are semantically identical after inline-flag
    # extraction -> identical identity hash and cache key (R47a v2.0.1 / R4).
    ca, cb = hdl.generate(a, fa), hdl.generate(b, fb)
    assert ca.circ_id == cb.circ_id
    assert ca.circ_flags == cb.circ_flags
    assert (hdl.identity.descriptor_key(a, fa, hdl.GENERATOR_VERSION)
            == hdl.identity.descriptor_key(b, fb, hdl.GENERATOR_VERSION))


def test_scoped_inline_flags_are_not_canonicalized_away():
    # Scoped (?i:...) is semantic and must NOT collapse to the flagless form.
    assert hdl.generate("(?i:abc)", 0).circ_id != hdl.generate("abc", 0).circ_id


def test_circ_flags_packs_num_patterns_high16():
    import re as _re
    c = hdl.generate("abc", _re.I)
    # low16 holds the *effective* baked flags (str auto-adds re.UNICODE).
    assert (c.circ_flags & 0xFFFF) == (c.automaton.flags & 0xFFFF)
    assert (c.circ_flags & 0xFFFF) & _re.I           # IGNORECASE present
    assert (c.circ_flags >> 16) == c.num_patterns


# --- R58: obsolete PROG_* / "PROG" blob path must be absent ---------------

def test_no_obsolete_prog_blob_path():
    rtl = hdl.generate("abc", 0).rtl
    assert "PROG" not in rtl
    assert "5050524F" not in rtl  # "PROG" magic must not appear


# --- R12: over-budget patterns cannot be generated ------------------------

def test_generate_rejects_ineligible_pattern():
    with pytest.raises(ValueError):
        hdl.generate(r"(a)\1", 0)          # backreference (R10)
    with pytest.raises(ValueError):
        hdl.generate("a{5000}", 0)         # over MAX_REPEAT (R12)


# --- RTL syntax smoke lint (iverilog if present) --------------------------

def test_iverilog_lints_generated_rtl(tmp_path):
    iverilog = shutil.which("iverilog")
    if iverilog is None:
        pytest.skip("iverilog not on PATH; structural invariants asserted above")
    c = hdl.generate(r"^foo\d+(bar|baz)*$", 0)
    v = tmp_path / "circ.v"
    v.write_text(c.rtl)
    proc = subprocess.run(
        [iverilog, "-t", "null", "-g2001", str(v)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.strip() == ""
