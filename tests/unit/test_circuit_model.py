"""Unit tests for the circuit software model (pyro._circuit_model, R7/R19; AC-1-3).

The model *executes the generator's automaton* (not an independent re-derivation)
so a generator lowering bug shows up as a missing/extra candidate window here.
Tests assert: harness-contract CSR/identity semantics (R45/R47a), result-ring
overflow (R47), the not-resident guard (R41), and the correctness keystone —
soundness/completeness for group 0 and byte-identical reconstruction vs. stock
``re`` over the §5.1 corpus including a ≥ 1 MiB case and UTF-8/astral inputs.
"""
import re

import pytest

from pyro import hdl
from pyro import _circuit_model as cm


@pytest.fixture
def ctx():
    return cm.CircuitContext()


def _load(ctx, pat, flags=0):
    circ = ctx.generate(pat, flags)
    ctx.load(circ)
    return circ


# --- R45: harness CSR block ----------------------------------------------

def test_csr_id_magic_and_versions(ctx):
    circ = _load(ctx, "abc")
    assert circ.csr_read(cm.CSR_ID) == hdl.ID_MAGIC
    assert circ.csr_read(cm.CSR_HARNESS_VER) == hdl.HARNESS_VERSION
    assert circ.csr_read(cm.CSR_CAPS1) == hdl.GENERATOR_VERSION
    caps0 = circ.csr_read(cm.CSR_CAPS0)
    assert (caps0 & 0xFFFF) == hdl.DATAPATH_BYTES


def test_csr_identity_block_matches_circuit(ctx):
    circ = _load(ctx, "abc")
    id0, id1, id2, id3, flags = circ.identity_block()
    assert (id0, id1, id2, id3) == circ.circuit.circ_id
    assert circ.csr_read(cm.CSR_CIRC_ID0) == id0
    assert circ.csr_read(cm.CSR_CIRC_FLAGS) == flags


# --- R47a: identity trust boundary ---------------------------------------

def test_identity_matches_correct_target(ctx):
    circ = _load(ctx, "abc")
    good = hdl.identity.pattern_hash(
        "abc", circ.circuit.automaton.flags, hdl.GENERATOR_VERSION,
        hdl.HARNESS_VERSION)
    assert circ.identity_matches(good, circ.circuit.circ_flags) is True


def test_identity_rejects_wrong_pattern(ctx):
    circ = _load(ctx, "abc")
    wrong = hdl.identity.pattern_hash(
        "xyz", circ.circuit.automaton.flags, hdl.GENERATOR_VERSION,
        hdl.HARNESS_VERSION)
    assert circ.identity_matches(wrong, circ.circuit.circ_flags) is False


# --- R41: not-resident guard ---------------------------------------------

def test_scan_requires_resident(ctx):
    circ = ctx.generate("abc", 0)  # generated but not loaded
    with pytest.raises(cm.NotResident):
        circ.scan(b"abc")


def test_context_evicts_single_tenant(ctx):
    a = _load(ctx, "aaa")
    b = _load(ctx, "bbb")            # loading b evicts a (R64 single-tenant)
    assert b.resident is True
    assert a.resident is False


# --- R47: result-ring overflow (OVF) -------------------------------------

def test_overflow_sets_ovf_and_truncates(ctx):
    circ = _load(ctx, "a")
    subject = b"aaaaaa"             # 6 candidate windows
    windows, overflowed = circ.scan(subject, 0, out_cap=3)
    assert overflowed is True
    assert len(windows) == 3
    assert circ.csr_read(cm.CSR_STATUS) & cm.ST_OVF


def test_no_overflow_when_capacity_sufficient(ctx):
    circ = _load(ctx, "a")
    windows, overflowed = circ.scan(b"aaa", 0, out_cap=100)
    assert overflowed is False
    assert not (circ.csr_read(cm.CSR_STATUS) & cm.ST_OVF)


# --- R19: candidate windows are unverified (host re-verifies) -------------

def test_windows_are_unverified(ctx):
    circ = _load(ctx, r"[a-z]+")
    windows, _ = circ.scan(b"abc def")
    assert windows
    from pyro._model import FLAG_VERIFIED
    assert all((w.flags & FLAG_VERIFIED) == 0 for w in windows)


# --- AC-1-3: soundness/completeness + byte-identical over §5.1 corpus -----

_CORPUS = [
    (r"foo\d+bar", "x foo12bar foo9bar"),
    (r"[a-z]+", "Hello World abc"),
    (r"[^a]+", "banana pama"),
    (r"a|bc|def", "xdefabcx"),
    (r"(ab|cd)+", "ababcdz cd"),
    (r"colou?r", "color and colour"),
    (r"[0-9]{2,4}", "1 12 123 1234 12345"),
    (r"\w+\s\d+", "id 42 name 7"),
    (r"\bcat\b", "cat category the cat"),
    (r"a.c", "abc a\nc axc"),
    (r"a.c", "abc a\nc axc"),           # (re.DOTALL variant applied below)
    (r"^err", "ok\nerr1\nerr2"),
    (r"end$", "the end\nendx\nend"),
    ("ABC", "abcABCaBc"),               # (re.IGNORECASE applied below)
    ("é+", "caféé thé"),
    (r"\w+", "héllo wörld"),
    (r"\w+", "a\U0001D518b \U0001D518\U0001D518"),   # astral (R21)
    (r"\d+", "abc١٢٣ 45"),            # Arabic-Indic digits
    (rb"[a-z]+\d", b"abc9 xyz"),
]

_FLAGS = {
    10: re.S,          # a.c DOTALL variant
    11: re.M,          # ^err multiline
    12: re.M,          # end$ multiline
    13: re.I,          # ABC ignorecase
}


@pytest.mark.parametrize("i", range(len(_CORPUS)))
def test_group0_complete_and_byte_identical(ctx, i):
    pat, subj = _CORPUS[i]
    flags = _FLAGS.get(i, 0)
    circ = _load(ctx, pat, flags)

    # completeness (R19): the automaton covers every CPython match start.
    model_starts = set(cm.candidate_starts(circ.circuit, subj))
    stock_starts = {m.start() for m in re.finditer(pat, subj, flags)}
    assert stock_starts <= model_starts

    # byte-identical group-0 finditer via the hybrid re-verification (R18/R19).
    got = cm.group0_finditer(circ.circuit, subj)
    ref = [m.span() for m in re.finditer(pat, subj, flags)]
    assert got == ref


# --- R22/AC-1-3: empty-capable patterns yield EVERY empty match -----------

_EMPTY_CORPUS = [
    ("a*", "bbb"),
    ("a*", "baab"),
    (r"\d*", "x12y3"),
    (r"\d*", "a1b"),
    ("x?", "xyxx"),
    ("", "ab"),
    ("", ""),
    (r"\b", "a b cd"),
    ("(ab)?", "abxab"),
    ("a|", "cab"),
    ("(?:)", "xy"),
    (r"^", "a\nb"),
]


@pytest.mark.parametrize("i", range(len(_EMPTY_CORPUS)))
def test_empty_matches_are_byte_identical(ctx, i):
    pat, subj = _EMPTY_CORPUS[i]
    circ = _load(ctx, pat)
    got = cm.group0_finditer(circ.circuit, subj)
    ref = [m.span() for m in re.finditer(pat, subj)]
    assert got == ref            # includes every zero-width match (no suppression)


# --- R24/§6.5: scoped inline multiline threaded per-anchor ----------------

_SCOPED_ML = [
    ("(?m:^)abc", "x\nabc"),
    ("(?m:^abc)", "x\nabc"),
    ("(?m:abc$)", "abc\ny"),
    ("(?-m:^)a", "x\na"),
    ("pre(?m:^)x", "pre\nx"),
]


@pytest.mark.parametrize("i", range(len(_SCOPED_ML)))
def test_scoped_multiline_complete_and_identical(ctx, i):
    pat, subj = _SCOPED_ML[i]
    circ = _load(ctx, pat)
    model_starts = set(cm.candidate_starts(circ.circuit, subj))
    stock_starts = {m.start() for m in re.finditer(pat, subj)}
    assert stock_starts <= model_starts              # completeness (R19)
    got = cm.group0_finditer(circ.circuit, subj)
    ref = [m.span() for m in re.finditer(pat, subj)]
    assert got == ref


# --- R23/R17: greedy vs lazy still byte-identical after reconciliation ----

@pytest.mark.parametrize("pat", [r"a+", r"a+?", r"<.*>", r"<.*?>"])
def test_greedy_lazy_reconciled(ctx, pat):
    subj = "<a> <b> aaa"
    circ = _load(ctx, pat)
    got = cm.group0_finditer(circ.circuit, subj)
    ref = [m.span() for m in re.finditer(pat, subj)]
    assert got == ref


# --- AC-1-3: ≥ 1 MiB corpus, byte-identical to stock re -------------------

def test_one_mib_corpus_byte_identical(ctx):
    # A fast-failing needle over ~1.1 MiB of log-like text; the model scans the
    # whole corpus via the generated automaton and must agree with stock re.
    block = ("2026-07-05 INFO nothing to see here; move along ......... \n"
             "2026-07-05 DEBUG still nothing of interest on this line   \n")
    filler = (block * 9000)                      # ~1.06 MiB
    corpus = filler + "2026-07-05 ERROR-4242 boom\n" + filler + \
        "2026-07-05 ERROR-0001 again\n"
    assert len(corpus.encode("utf-8")) >= 1 << 20
    pat = r"ERROR-\d{4}"
    circ = _load(ctx, pat)
    got = cm.group0_finditer(circ.circuit, corpus)
    ref = [m.span() for m in re.finditer(pat, corpus)]
    assert got == ref
    assert len(ref) == 2
