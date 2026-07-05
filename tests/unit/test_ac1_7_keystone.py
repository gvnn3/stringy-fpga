"""AC-1-7 asynchrony correctness keystone (R36 asynchrony clause / R53 / R58a).

For the same pattern/subject, results MUST be byte-identical whether served
cold-fallback (stock re), via the software model, or via a "resident" model
circuit (the generated automaton executed by pyro._circuit_model, then
re-verified per R19).  This proves the transparency invariant across tiers
without hardware.  The corpus deliberately includes over-approximation patterns
(\\w+, \\b, IGNORECASE) so the resident-circuit's superset recognizer + R19
re-verification is exercised.
"""

import re

import pytest

import pyro.hdl as hdl
from pyro import _model, _circuit_model
from pyro._model import ENC_UTF8, ENC_BYTES, utf8_prefix
from bisect import bisect_left

CORPUS = [
    (r"abc", "xx abc yy abc"),
    (r"[0-9]+", "a1 b22 c333 d4444"),
    (r"\d+", "id=12, id=345, none"),
    (r"\w+", "café touché naïve x123"),          # unicode_category over-approx
    (r"\bword\b", "a word wordy word. word"),     # word_boundary over-approx
    (r"(?i)hello", "HELLO hello HeLLo world"),    # cross-length casefold OA
    (r"a*", "baaab aa"),                          # empty + greedy
    (r"^\w+", "first line only"),
    (r"(\w+)@(\w+)", "u@h and a@b end"),          # capture groups (hybrid R18)
    (r"colou?r", "color colour colouur"),
]


def _stock(pattern, subject):
    return [(m.span(), m.groups()) for m in re.finditer(pattern, subject)]


def _model_path(pattern, subject):
    """Serve via the Phase-0 software model (the resident stand-in, R7)."""
    ctx = _model.ModelContext()
    enc = ENC_UTF8 if isinstance(subject, str) else ENC_BYTES
    prog = ctx.compile(pattern, 0, enc)
    ctx.load(prog)
    buf = subject.encode("utf-8") if isinstance(subject, str) else bytes(subject)
    windows, _ovf = ctx.scan(prog, buf, mode="finditer")
    prefix = utf8_prefix(subject) if isinstance(subject, str) else None
    out = []
    stock = re.compile(pattern)
    for w in windows:
        if prefix is not None:
            s, e = bisect_left(prefix, w.start), bisect_left(prefix, w.end)
        else:
            s, e = w.start, w.end
        m = stock.match(subject, s)          # hybrid re-run for groups (R18)
        out.append(((s, e), m.groups() if m else ()))
    return out


def _resident_circuit_path(pattern, subject):
    """Serve via a 'resident' generated circuit: the automaton finds candidate
    windows, the host re-verifies + reconstructs groups (R19/R18)."""
    circuit = hdl.generate(pattern, 0)
    spans = _circuit_model.group0_finditer(circuit, subject)
    stock = re.compile(pattern)
    return [(sp, stock.match(subject, sp[0]).groups()) for sp in spans]


@pytest.mark.parametrize("pattern,subject", CORPUS)
def test_byte_identical_across_tiers(pattern, subject):
    cold = _stock(pattern, subject)
    model = _model_path(pattern, subject)
    resident = _resident_circuit_path(pattern, subject)
    assert model == cold, f"model tier diverged for {pattern!r}"
    assert resident == cold, f"resident-circuit tier diverged for {pattern!r}"


def test_over_approx_windows_reverified_to_identical():
    # An over-approximating circuit (\\w+ admits any non-ASCII cp) MUST re-verify
    # every candidate window; the returned result stays byte-identical (R19a).
    circuit = hdl.generate(r"\w+", 0)
    assert circuit.over_approx  # this circuit really does over-approximate
    subject = "αβγ abc ☃ x9 —"
    resident = [sp for sp in _circuit_model.group0_finditer(circuit, subject)]
    cold = [m.span() for m in re.finditer(r"\w+", subject)]
    assert resident == cold
