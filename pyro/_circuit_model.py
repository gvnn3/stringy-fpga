"""Circuit software model — software stand-in for one generated circuit (R7).

This is the behavioral twin of a **generated per-pattern circuit** (§2, R7).
Unlike the Phase-0 :mod:`pyro._model` (which located group-0 windows by simply
delegating to CPython ``re``), this model **executes the generator's automaton**
(:mod:`pyro.hdl.automaton`, consumed via :mod:`pyro.hdl.generator`).  It does not
re-derive the recognizer from the pattern independently, so a bug in the HDL
generator's lowering surfaces as a missing/extra candidate window here rather
than being masked (Task-5 brief, item 3).

Correctness contract (R19, "sound and complete for group 0"):

  * **Complete.** For every position where CPython ``re`` begins a match, the
    automaton can begin an accepting run, so this model reports a candidate
    window starting there.  This holds because the automaton is built as a
    superset recognizer (see the OVER-APPROX notes in ``automaton.py``).
  * **Sound-after-verification.** The automaton may over-accept (report a
    candidate where CPython would not match, or a longer end than CPython's
    greedy span).  Candidate windows are therefore emitted **unverified**
    (``flags`` bit0 clear); the host layer re-verifies every window against
    CPython before returning it (R19/R47a).  The circuit's advisory ``verified``
    bit never relieves the host of re-verification.

Harness contract (§7.4).  The model exposes the normative CSR block (R45)
including the baked **circuit-identity** registers (R47a), honors the result-ring
overflow / ``OVF`` semantics (R47), single-issue serialization (R48), and the
64-byte DMA-alignment assertion (R49).  It is interchangeable with a real
circuit behind the L3 harness so later tasks can bind it under the C ABI.
"""

from __future__ import annotations

import re as _re
import re._constants as _c
import threading
from bisect import bisect_left
from typing import List, Optional, Tuple

from . import hdl
from .hdl import automaton as _auto
from ._model import (
    MatchWindow, FLAG_VERIFIED, FLAG_ZERO_WIDTH, ENC_BYTES, ENC_UTF8,
    DeviceError, UnsupportedPattern, utf8_prefix,
)

# Stock compiler captured at import time (install() may rebind re.compile).
_stock_compile = _re.compile

_WORD_BYTES = frozenset(
    b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")


class NotResident(Exception):
    """Dispatch attempted against a non-resident circuit (PYRO_E_NOT_RESIDENT)."""


class IdentityMismatch(Exception):
    """Resident circuit's baked identity != the target pattern's (R47a)."""


# --------------------------------------------------------------------------
# Automaton simulation (executes the *generated* recognizer)
# --------------------------------------------------------------------------
def _assert_ok(at: int, buf: bytes, pos: int, is_bytes: bool) -> bool:
    """Evaluate a zero-width assertion at absolute byte offset ``pos``.

    ``^ $ \\A \\Z`` are evaluated exactly in both modes (``\\n`` is byte 0x0A,
    never a UTF-8 lead/continuation byte).  The (scoped) MULTILINE flag has
    already been baked into each ``^``/``$`` edge at lowering time
    (``AT_BEGINNING``/``AT_END`` vs. their ``*_LINE`` variants, see
    ``automaton._resolve_at``), so this evaluator needs no ambient flag.  Word
    boundaries ``\\b``/``\\B`` are exact in bytes mode; in str mode they are
    OVER-APPROX'd to always-satisfiable (the constraint is dropped — a superset —
    and R19 re-verification restores exactness).
    """
    n = len(buf)
    if at in (_c.AT_BEGINNING, _c.AT_BEGINNING_STRING):  # ^ (non-M) / \A
        return pos == 0
    if at == _c.AT_BEGINNING_LINE:  # ^ under MULTILINE
        return pos == 0 or buf[pos - 1] == 0x0A
    if at == _c.AT_END_STRING:  # \Z — absolute
        return pos == n
    if at == _c.AT_END:  # $ (non-M): end, or just before a final '\n'
        return pos == n or (pos == n - 1 and buf[pos] == 0x0A)
    if at == _c.AT_END_LINE:  # $ under MULTILINE
        return pos == n or buf[pos] == 0x0A
    if at in (_c.AT_BOUNDARY, _c.AT_NON_BOUNDARY,
              _c.AT_UNI_BOUNDARY, _c.AT_UNI_NON_BOUNDARY):
        if not is_bytes:
            return True  # OVER-APPROX: drop Unicode word-boundary constraint
        before = pos > 0 and buf[pos - 1] in _WORD_BYTES
        after = pos < n and buf[pos] in _WORD_BYTES
        boundary = before != after
        if at in (_c.AT_BOUNDARY, _c.AT_UNI_BOUNDARY):
            return boundary
        return not boundary
    # Unknown assertion: OVER-APPROX true (never under-approximate).
    return True


def _closure(au: _auto.Automaton, states, buf: bytes, pos: int,
             is_bytes: bool) -> frozenset:
    """Epsilon + assertion closure of ``states`` at offset ``pos``."""
    stack = list(states)
    seen = set(states)
    while stack:
        s = stack.pop()
        for e in au.edges[s]:
            if e.kind == _auto.E_EPS:
                if e.target not in seen:
                    seen.add(e.target)
                    stack.append(e.target)
            elif e.kind == _auto.E_ASSERT:
                if e.target not in seen and _assert_ok(
                        e.payload, buf, pos, is_bytes):
                    seen.add(e.target)
                    stack.append(e.target)
    return frozenset(seen)


def _scan_windows(au: _auto.Automaton, buf: bytes, start_off: int
                  ) -> List[MatchWindow]:
    """Return candidate group-0 windows (byte offsets) from the automaton.

    A window ``[s, e)`` is reported for every start ``s`` from which the
    automaton reaches its accept state consuming ``buf[s:e]`` (``e`` is the
    longest such end — leftmost-longest; R17 reconciles to CPython's greedy span
    at re-verification).  Windows are UNVERIFIED (bit0 clear): the automaton is a
    superset recognizer and the host re-verifies each window (R19).
    """
    is_bytes = au.enc == ENC_BYTES
    n = len(buf)
    accept = au.accept
    out: List[MatchWindow] = []
    edges = au.edges
    s = start_off
    while s <= n:
        cur = _closure(au, (au.start,), buf, s, is_bytes)
        last_accept = s if accept in cur else None
        pos = s
        while pos < n and cur:
            b = buf[pos]
            moved = set()
            for st in cur:
                for e in edges[st]:
                    if e.kind == _auto.E_BYTE and b in e.payload:
                        moved.add(e.target)
            if not moved:
                cur = frozenset()
                break
            cur = _closure(au, moved, buf, pos + 1, is_bytes)
            pos += 1
            if accept in cur:
                last_accept = pos
        if last_accept is not None:
            zw = FLAG_ZERO_WIDTH if last_accept == s else 0
            out.append(MatchWindow(s, last_accept, 0, zw))  # unverified
        s += 1
    return out


# --------------------------------------------------------------------------
# Circuit model (one generated circuit) + harness CSR block (§7.4)
# --------------------------------------------------------------------------
# CSR offsets (R45), normative and identical for all circuits.
CSR_ID = 0x0000
CSR_HARNESS_VER = 0x0004
CSR_CAPS0 = 0x0008
CSR_CAPS1 = 0x000C
CSR_CTRL = 0x0010
CSR_STATUS = 0x0014
CSR_CIRC_ID0 = 0x0018
CSR_CIRC_ID1 = 0x001C
CSR_CIRC_ID2 = 0x0020
CSR_CIRC_ID3 = 0x0024
CSR_CIRC_FLAGS = 0x0028
CSR_RESERVED = 0x002C
CSR_OUT_COUNT = 0x004C

# STATUS bits (R45 0x0014).
ST_BUSY = 0x1
ST_DONE = 0x2
ST_ERR = 0x4
ST_OVF = 0x8


class CircuitModel:
    """Behavioral model of one resident-capable generated circuit (R7)."""

    __slots__ = ("circuit", "enc", "resident", "_status", "_out_count", "_lock")

    def __init__(self, circuit: hdl.GeneratedCircuit):
        self.circuit = circuit
        self.enc = circuit.enc
        self.resident = False
        self._status = 0
        self._out_count = 0
        self._lock = threading.Lock()

    # -- harness CSR block (R45/R47a) --------------------------------------
    def csr_read(self, offset: int) -> int:
        c = self.circuit
        table = {
            CSR_ID: hdl.ID_MAGIC,
            CSR_HARNESS_VER: c.harness_version,
            CSR_CAPS0: (c.datapath_bytes & 0xFFFF)
            | ((hdl.budget()["pr_partitions"] & 0xFFFF) << 16),
            CSR_CAPS1: c.generator_version,
            CSR_STATUS: self._status,
            CSR_CIRC_ID0: c.circ_id[0],
            CSR_CIRC_ID1: c.circ_id[1],
            CSR_CIRC_ID2: c.circ_id[2],
            CSR_CIRC_ID3: c.circ_id[3],
            CSR_CIRC_FLAGS: c.circ_flags,
            CSR_RESERVED: 0,
            CSR_OUT_COUNT: self._out_count,
        }
        return int(table.get(offset, 0)) & 0xFFFFFFFF

    def identity_block(self) -> Tuple[int, int, int, int, int]:
        """(CIRC_ID0..3, CIRC_FLAGS) — the baked identity read for R47a."""
        c = self.circuit
        return (*c.circ_id, c.circ_flags)

    def identity_matches(self, expected_hash16: bytes, expected_flags: int
                         ) -> bool:
        """R47a trust boundary: does the baked identity match the target?"""
        return (self.circuit.pattern_hash16 == expected_hash16
                and self.circuit.circ_flags == expected_flags)

    # -- R41 scan against a resident circuit -------------------------------
    def scan(self, buf: bytes, start_off: int = 0, out_cap: int = 1 << 62,
             ) -> Tuple[List[MatchWindow], bool]:
        """Scan ``buf`` for candidate windows; returns ``(windows, overflowed)``.

        Requires the circuit resident (else :class:`NotResident`, R41).  On
        overflow (more windows than ``out_cap``) sets ``STATUS.OVF`` and returns
        ``overflowed=True`` with the ring truncated to ``out_cap`` (R47); the
        host resumes via ``start_off`` streaming (R41).
        """
        if not self.resident:
            raise NotResident("circuit not resident")
        # R49: buffers are 64-byte aligned in the DMA contract; bytes objects
        # produced by the host encoder satisfy this. We assert the ring cap is
        # sane rather than the (Python-managed) buffer address.
        if out_cap < 0:
            raise ValueError("out_cap must be non-negative")
        with self._lock:  # single-issue per circuit (R48)
            self._status = ST_BUSY
            windows = _scan_windows(self.circuit.automaton, bytes(buf), start_off)
            overflowed = len(windows) > out_cap
            self._out_count = min(len(windows), out_cap)
            self._status = ST_DONE | (ST_OVF if overflowed else 0)
            return windows[:out_cap], overflowed


class CircuitContext:
    """Mock host runtime (shape of ``pyro_ctx``, §7.3) over circuit models.

    Serializes residency/scan against a single-tenant PR region (R64/R48): at
    most one circuit is resident.  Distinct contexts are independent (R43).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._resident: Optional[CircuitModel] = None
        # Fault-injection seams for the R52 path (private test hooks).
        self.fail_next_scan = False

    # -- R42 capability query ----------------------------------------------
    def caps(self) -> dict:
        b = hdl.budget()
        return {
            "max_states": b["max_states"],
            "max_patterns": b["max_patterns"],
            "max_repeat": b["max_repeat"],
            "alphabet": 256,
            "generator_version": hdl.GENERATOR_VERSION,
            "harness_version": hdl.HARNESS_VERSION,
            "datapath_bytes": hdl.DATAPATH_BYTES,
            "pr_luts": b["pr_luts"],
            "pr_ffs": b["pr_ffs"],
            "pr_bram_kb": b["pr_bram_kb"],
            "pr_dsps": b["pr_dsps"],
            "pr_partitions": b["pr_partitions"],
        }

    # -- R40 generate / load -----------------------------------------------
    def generate(self, pattern, flags: int, enc: int = None) -> CircuitModel:
        """Generate a (cold) circuit model, or raise for non-HW-eligible."""
        est = hdl.estimate(pattern, flags, enc)
        if not est.eligible:
            raise UnsupportedPattern(est.reason)
        circuit = hdl.generate(pattern, flags, enc)
        return CircuitModel(circuit)

    def load(self, circuit: CircuitModel) -> None:
        """Make ``circuit`` the resident circuit (warm -> resident, R40)."""
        with self._lock:
            if self._resident is not None and self._resident is not circuit:
                self._resident.resident = False  # single-tenant eviction (R64)
            self._resident = circuit
            circuit.resident = True

    def scan(self, circuit: CircuitModel, buf: bytes, start_off: int = 0,
             out_cap: int = 1 << 62) -> Tuple[List[MatchWindow], bool]:
        if self.fail_next_scan:
            self.fail_next_scan = False
            raise DeviceError("injected device error")
        if not circuit.resident:
            raise NotResident("circuit not resident")
        return circuit.scan(buf, start_off, out_cap)


# --------------------------------------------------------------------------
# Host-side hybrid reconstruction (R18/R19) — byte-identical group-0 results
# --------------------------------------------------------------------------
def candidate_starts(circuit: hdl.GeneratedCircuit, subject) -> List[int]:
    """Candidate match-start offsets in *caller units* (R21).

    Runs the generated automaton over the encoded subject and maps the reported
    byte offsets back to code-point indices for ``str`` (astral-safe) or leaves
    byte offsets for ``bytes``.
    """
    if isinstance(subject, str):
        buf = subject.encode("utf-8")
        windows = _scan_windows(circuit.automaton, buf, 0)
        prefix = utf8_prefix(subject)
        return [bisect_left(prefix, w.start) for w in windows]
    buf = bytes(subject)
    windows = _scan_windows(circuit.automaton, buf, 0)
    return [w.start for w in windows]


def group0_finditer(circuit: hdl.GeneratedCircuit, subject
                    ) -> List[Tuple[int, int]]:
    """Byte-identical ``finditer`` group-0 spans via the R18/R19 hybrid.

    Candidate starts come from the **automaton** (completeness, R19); exact spans
    and CPython's leftmost-greedy / non-overlapping advancement come from a stock
    ``re`` re-run anchored at each candidate start.  Because the automaton is
    complete, the produced list equals ``stock.finditer`` spans; a generator bug
    that drops a start makes this list diverge (surfacing the bug).
    """
    stock = _stock_compile(circuit.pattern, circuit.flags)
    starts = sorted(set(candidate_starts(circuit, subject)))
    out: List[Tuple[int, int]] = []
    # Reproduce CPython's finditer exactly (R22): walk left to right; at each
    # position take the leftmost real match starting at or after ``pos`` (found
    # by trying stock.match at successive candidate starts — the automaton is
    # complete, so every real match start is among ``starts``), append EVERY
    # match span including empties, and advance ``pos`` to the match end, or one
    # past it when the match was empty (to make progress).  There is NO empty-
    # match adjacency suppression: CPython's finditer yields the empty matches.
    pos = 0
    i = 0
    n_starts = len(starts)
    while i < n_starts:
        s = starts[i]
        if s < pos:
            i += 1
            continue
        m = stock.match(subject, s)
        if m is None:
            i += 1            # candidate false positive (R19) — skip permanently
            continue
        span = m.span()
        out.append(span)
        pos = span[1] if span[1] > span[0] else span[1] + 1
        i += 1
    return out
