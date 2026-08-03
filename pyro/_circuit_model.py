"""Circuit software model — software stand-in for one generated circuit (R7).

This is the behavioral twin of a **generated per-pattern circuit** (§2, R7).
Unlike the Phase-0 :mod:`pyro._model` (which located group-0 windows by simply
delegating to CPython ``re``), this model **executes the generator's automaton**
(:mod:`pyro.hdl.automaton`, consumed via :mod:`pyro.hdl.generator`).  It does
not
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
including the baked **circuit-identity** registers (R47a), honors the
result-ring
overflow / ``OVF`` semantics (R47), single-issue serialization (R48), and the
64-byte DMA-alignment contract (R49) — whose check is realized by the L3 binding
as a defined ``PYRO_E_INVALID`` error, not an abort (R49a, v2.0.4).  It is
interchangeable with a real circuit behind the L3 harness so later tasks can
bind it under the C ABI.
"""

from __future__ import annotations

import re._constants as _c
import threading
from bisect import bisect_left
from functools import lru_cache
from typing import List, NamedTuple, Optional, Tuple

# Requires CPython >= 3.11 (P7/R26): this model and the L2 front end depend on
# the ``re._parser`` / ``re._constants`` modules (present under those names only
# on 3.11+) via :mod:`pyro.hdl.automaton`.  The target host runs 3.12.

from . import hdl
from .hdl import automaton as _auto
from ._model import (
    MatchWindow, FLAG_VERIFIED, FLAG_ZERO_WIDTH, ENC_BYTES, ENC_UTF8,
    DeviceError, UnsupportedPattern, utf8_prefix,
    _stock_compile,
)

# The stock compiler comes from pyro._model, which captured it at ``import
# pyro`` time — always before install() can rebind re.compile.  THIS module is
# imported lazily (test / native-harness paths only), so capturing
# ``_re.compile`` here would grab the *patched* compiler when first imported
# while installed (double-wrap / recursion hazard on the verification paths;
# AC-3-1 counter-wedge review).

_WORD_BYTES = frozenset(
    b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")


class NotResident(Exception):
    """Dispatch attempted against a non-resident circuit
    (PYRO_E_NOT_RESIDENT)."""


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
    OVER-APPROX'd to always-satisfiable (the constraint is dropped — a
    superset —
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
    at re-verification).  Windows are UNVERIFIED (bit0 clear): the automaton
    is a
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
# R45a perf counters (v2.3.0): 64-bit RO pairs, cleared on START/RESET.
CSR_CYCLES_LO = 0x0058
CSR_CYCLES_HI = 0x005C
CSR_BYTES_LO = 0x0060
CSR_BYTES_HI = 0x0064

# STATUS bits (R45 0x0014).
ST_BUSY = 0x1
ST_DONE = 0x2
ST_ERR = 0x4
ST_OVF = 0x8


class CircuitModel:
    """Behavioral model of one resident-capable generated circuit (R7)."""

    __slots__ = ("circuit", "enc", "resident", "_status", "_out_count",
                 "_cycles", "_bytes", "_lock")

    def __init__(self, circuit: hdl.GeneratedCircuit):
        self.circuit = circuit
        self.enc = circuit.enc
        self.resident = False
        self._status = 0
        self._out_count = 0
        self._cycles = 0
        self._bytes = 0
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
            CSR_CYCLES_LO: self._cycles,
            CSR_CYCLES_HI: self._cycles >> 32,
            CSR_BYTES_LO: self._bytes,
            CSR_BYTES_HI: self._bytes >> 32,
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
        """Scan ``buf`` for candidate windows; returns ``(windows,
        overflowed)``.

        Requires the circuit resident (else :class:`NotResident`, R41).  On
        overflow (more windows than ``out_cap``) sets ``STATUS.OVF`` and returns
        ``overflowed=True`` with the ring truncated to ``out_cap`` (R47); the
        host resumes via ``start_off`` streaming (R41).
        """
        if not self.resident:
            raise NotResident("circuit not resident")
        # R49/R49a: the DMA contract requires 64-byte-aligned buffers; the
        # alignment "check" is normatively realized as a *defined error* (not an
        # abort/assert), i.e. the L3 ABI returns PYRO_E_INVALID for a misaligned
        # or malformed request (R49a, spec v2.0.4).  This Python model is driven
        # with host-encoded ``bytes`` (always suitably backed), so the binding
        # layer (src/pyro_rt.c) owns the misalignment rejection; here we
        # validate
        # the ring capacity argument as the analogous defined-error guard.
        if out_cap < 0:
            raise ValueError("out_cap must be non-negative")
        with self._lock:  # single-issue per circuit (R48)
            self._status = ST_BUSY
            data = bytes(buf)
            windows = _scan_windows(self.circuit.automaton, data, start_off)
            # R45a perf counters: the model reports the *idealized* datapath
            # numbers (BYTES = bytes consumed, CYCLES = ceil(BYTES/datapath));
            # only hardware-measured values are performance evidence (R59).
            self._bytes = max(0, len(data) - start_off)
            self._cycles = -(-self._bytes // self.circuit.datapath_bytes)
            overflowed = len(windows) > out_cap
            self._out_count = min(len(windows), out_cap)
            self._status = ST_DONE | (ST_OVF if overflowed else 0)
            return windows[:out_cap], overflowed


class GroupMatch(NamedTuple):
    """One entry of a group circuit's result ring (R47 ``pyro_match`` shape).

    ``pattern_id`` is the group-local **slot** index (SR7): the sidecar maps it
    to the LIST of ``gid:sid`` rules that share that slot's anchor.  ``start``
    and ``end`` follow :class:`CircuitModel`'s convention exactly — see
    :class:`GroupCircuitModel` for the (load-bearing) definition of ``start``.
    """

    pattern_id: int
    start: int          # inclusive byte offset — TRUE match start (see below)
    end: int            # exclusive byte offset
    flags: int = 0      # bit0 verified, bit1 zero_width (R47) — unverified

    @property
    def slot(self) -> int:
        """Alias: the group slot this entry came from."""
        return self.pattern_id

    def as_window(self) -> MatchWindow:
        """The same entry in :class:`pyro._model.MatchWindow` field order."""
        return MatchWindow(self.start, self.end, self.pattern_id, self.flags)


class GroupCircuitModel:
    """Behavioral model of one resident **group** circuit (SNORT-PF SR7).

    A group circuit is N automata sharing one harness; slot *i* is
    ``pattern_id`` *i* (SR7).  This is the model the SR16 differential oracle
    compares against.

    **Scope: fixed-length literal slots.**  Every SNORT-PF slot is one (spec §2
    "Anchor"; ``pyro.snort.groups.slot_pattern`` lowers it with ``re.escape``),
    and for those this model and the hardware agree exactly.  It is NOT the
    RTL's twin for an arbitrary automaton: ``_scan_windows`` is *per-start
    leftmost-longest* (one window per start offset, the longest), while the
    emitted engine is a continuously-seeded NFA that emits on every accepting
    cycle.  The two coincide for a literal and diverge otherwise — this model
    under-reports a ``+``/``*`` slot (one window per start where the RTL emits
    one per accepting byte) and reports separate windows where a bounded
    repeat lets one start accept at several lengths.  ``tests/hw/xsim_diff.py``
    therefore diffs the RTL against its own engine-semantics reference
    (``group_entries``), and ``test_acs2_3_oracle`` pins that the two agree on
    the real group's corpora — for literal slots, which is what S2 ships.

    The circuit argument
    --------------------
    Any object exposing the :class:`pyro.hdl.GeneratedCircuit` fields with
    ``automaton`` replaced by ``automata`` — an ordered sequence, index =
    slot = ``pattern_id``, entries may be ``None`` for a **tombstoned** slot
    (SR6: index retained, can never match).  Optional but honored:
    ``datapath_bytes`` (R45a perf counters), ``circ_id`` / ``circ_flags`` /
    ``harness_version`` / ``generator_version`` (R45/R47a CSR block).  A
    single-pattern :class:`pyro.hdl.GeneratedCircuit` is accepted as the
    degenerate 1-slot group (that is exactly the Phase-S1 child).

    Definition of ``start`` — read this before writing host code
    ------------------------------------------------------------
    This model reports the **true match start**: the leftmost byte offset from
    which the slot's automaton reaches accept, in absolute buffer offsets,
    identical to :class:`CircuitModel` / :func:`_scan_windows`.

    **Silicon does not.**  AC-S1-2 on the flashed shell reported ``start`` =
    the *chunk* start (the request's ``start_off``, i.e. 0 for a single-chunk
    request) where this model reports 5 — the harness emits an entry when an
    accept fires and never tracked where the run began (docs/notebook.md,
    2026-07-27 late, protocol note 2; ``tests/hw/xsim_diff.py``'s reference
    composer packs literal ``0`` into the entry's start field for the same
    reason).  Only ``end`` is exact on hardware.

    The model implements the true-start convention because (a) it is
    :class:`CircuitModel`'s, and forking it would give SNORT-PF two
    incompatible notions of a window, and (b) it is the *stronger* statement:
    a host that only trusts ``end`` is correct under both.  For a group, the
    two are reconcilable without new hardware — every slot's anchor is a fixed
    length literal, so ``start == end - len(anchor[pattern_id])`` and the
    daemon recovers the true start from ``end`` plus the sidecar's anchor
    length.  Attribution MUST therefore be driven from ``end``; treat
    ``[start, end]`` as the conservative candidate window (R78.7) and let the
    Snort re-verifier resolve it (SR5).

    Ring order
    ----------
    Entries are ordered by ``(end, pattern_id, start)`` ascending.  That is the
    hardware writer's order: the shared harness emits on accept (so ``end``
    ascending is emission time) and serializes slots that accept on the same
    byte by ascending slot index (priority encoder over the accept vector).
    This is the order the SR7 emitter MUST match.  The gate on the emitter is
    ``tests/hw/xsim_diff.py`` against *its own* reference (see the scope note
    above), run from ``tests/acceptance/test_acs2_3_oracle.py``; this class is
    what gate 1 checks the automata with.  Truncation on overflow keeps the
    earliest-*ending* entries, which is what the R41 resume needs.

    ``start_off`` is a MODEL-SIDE convenience.  :meth:`scan` honours it, but the
    emitted ``pyro_rp`` wrapper never reads the R78.6 ``start_off`` header
    field — it always feeds the corpus from byte 0.  A host resume must
    therefore re-send the TRIMMED corpus and re-label the offsets itself; see
    ``snortpf_s2_support.nominate_with_resume``.
    """

    __slots__ = ("circuit", "automata", "enc", "datapath_bytes", "resident",
                 "_status", "_out_count", "_cycles", "_bytes", "_lock")

    def __init__(self, circuit):
        self.circuit = circuit
        automata = getattr(circuit, "automata", None)
        if automata is None:  # degenerate 1-slot group (the S1 child)
            automata = (circuit.automaton,)
        self.automata = tuple(automata)
        self.enc = getattr(circuit, "enc", ENC_BYTES)
        self.datapath_bytes = int(getattr(circuit, "datapath_bytes", 1) or 1)
        self.resident = False
        self._status = 0
        self._out_count = 0
        self._cycles = 0
        self._bytes = 0
        self._lock = threading.Lock()

    @property
    def n_slots(self) -> int:
        """Number of slots == ``NUM_PAT`` in ``CIRC_FLAGS`` (R45 0x0028)."""
        return len(self.automata)

    # -- harness CSR block (R45/R47a) --------------------------------------
    def csr_read(self, offset: int) -> int:
        c = self.circuit
        table = {
            CSR_ID: hdl.ID_MAGIC,
            CSR_HARNESS_VER: getattr(c, "harness_version", 0),
            CSR_CAPS0: (self.datapath_bytes & 0xFFFF)
            | ((hdl.budget()["pr_partitions"] & 0xFFFF) << 16),
            CSR_CAPS1: getattr(c, "generator_version", 0),
            CSR_STATUS: self._status,
            CSR_CIRC_FLAGS: getattr(c, "circ_flags", 0),
            CSR_RESERVED: 0,
            CSR_OUT_COUNT: self._out_count,
            CSR_CYCLES_LO: self._cycles,
            CSR_CYCLES_HI: self._cycles >> 32,
            CSR_BYTES_LO: self._bytes,
            CSR_BYTES_HI: self._bytes >> 32,
        }
        circ_id = getattr(c, "circ_id", (0, 0, 0, 0))
        for i, reg in enumerate(
                (CSR_CIRC_ID0, CSR_CIRC_ID1, CSR_CIRC_ID2, CSR_CIRC_ID3)):
            table[reg] = circ_id[i]
        return int(table.get(offset, 0)) & 0xFFFFFFFF

    def identity_block(self) -> Tuple[int, int, int, int, int]:
        """(CIRC_ID0..3, CIRC_FLAGS) — the baked identity read for R47a/SR14."""
        c = self.circuit
        return (*getattr(c, "circ_id", (0, 0, 0, 0)),
                getattr(c, "circ_flags", 0))

    # -- R41 scan against a resident group circuit -------------------------
    def scan(self, buf: bytes, start_off: int = 0, out_cap: int = 1 << 62,
             ) -> Tuple[List[GroupMatch], bool]:
        """Scan ``buf`` across every slot; returns ``(entries, overflowed)``.

        Semantics per slot are :func:`_scan_windows` verbatim (the *generated*
        automaton is executed, so a lowering bug surfaces here rather than
        being masked, R7/R19), so a slot reports one entry per start position
        from which it can accept — several slots CAN and DO report at the same
        offset, and one slot can report overlapping windows.  Entries are
        unverified (bit0 clear); the host re-verifies (R19/R47a, SR5: every
        FPGA match is a nomination, never a verdict).

        Overflow is :class:`CircuitModel`'s exactly (R47): more than
        ``out_cap`` entries sets ``STATUS.OVF``, returns ``overflowed=True``,
        and truncates the ring to ``out_cap`` — in ring order, so the survivors
        are the earliest-ending entries and the host resumes by ``start_off``
        (R41/R78.7).
        """
        if not self.resident:
            raise NotResident("group circuit not resident")
        if out_cap < 0:
            raise ValueError("out_cap must be non-negative")
        with self._lock:  # single-issue per circuit (R48)
            self._status = ST_BUSY
            data = bytes(buf)
            entries: List[GroupMatch] = []
            for pid, au in enumerate(self.automata):
                if au is None:      # tombstoned slot (SR6): never matches
                    continue
                for w in _scan_windows(au, data, start_off):
                    entries.append(GroupMatch(pid, w.start, w.end, w.flags))
            entries.sort(key=lambda m: (m.end, m.pattern_id, m.start))
            # R45a perf counters: idealized datapath numbers, as CircuitModel
            # (only hardware-measured values are performance evidence, R59).
            self._bytes = max(0, len(data) - start_off)
            self._cycles = -(-self._bytes // self.datapath_bytes)
            overflowed = len(entries) > out_cap
            self._out_count = min(len(entries), out_cap)
            self._status = ST_DONE | (ST_OVF if overflowed else 0)
            return entries[:out_cap], overflowed


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


class CompletenessError(Exception):
    """The automaton missed a CPython match start — a generator lowering defect.

    R19 forbids false negatives for HW-eligible patterns; surfacing this as an
    error (rather than silently dropping a match) is the whole point of
    executing
    the generated automaton in the model.
    """


@lru_cache(maxsize=512)
def _cached_stock(ptype, pattern, flags):
    return _stock_compile(pattern, flags)


def _stock_for(circuit: hdl.GeneratedCircuit):
    """The stock-compiled pattern for a circuit, cached (M3 — no per-call
    recompile)."""
    return _cached_stock(type(circuit.pattern), circuit.pattern, circuit.flags)


def group0_finditer(circuit: hdl.GeneratedCircuit, subject
                    ) -> List[Tuple[int, int]]:
    """Byte-identical ``finditer`` group-0 spans via the R18/R19 hybrid.

    Span *enumeration* is delegated to CPython ``re`` — it is the oracle for
    R22's ``must_advance`` empty-match iteration (post-3.7 finditer retries at
    the
    same position demanding a non-empty match before advancing), which a
    hand-rolled scanner is error-prone to reproduce and MUST NOT get wrong (a
    dropped empty match is an R19 false negative).

    The generated **automaton is still executed** and its role is preserved as
    the R19 completeness oracle: every CPython match start MUST appear in the
    automaton's candidate-start set, else the automaton is missing a match a
    generator lowering bug would drop (:class:`CompletenessError`).  This keeps
    the honesty property — a generator completeness defect still surfaces here —
    without re-implementing CPython's iteration.
    """
    stock = _stock_for(circuit)
    spans = [m.span() for m in stock.finditer(subject)]
    # R19 cross-check: the automaton must cover every real match start.
    cand = set(candidate_starts(circuit, subject))
    for s, _e in spans:
        if s not in cand:
            raise CompletenessError(
                f"automaton missed match start {s} for {circuit.pattern!r} "
                f"(R19 completeness / generator lowering defect)")
    return spans
