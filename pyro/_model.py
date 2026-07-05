"""Software model of the L5 FPGA regex engine, wired through a mock L3/L4 (R7).

This object presents the *shape* of the §7.3 host-runtime interface
(``compile`` / ``load`` / ``scan`` plus a capability query), so that a real L3
binding can later drop in behind the same seam.  The C ABI itself is not
implemented in Python (see ``include/pyro_rt.h``); this is its pure-Python
behavioral twin for Phases 0-2.

For Phase 0 the engine locates group-0 candidate windows by delegating to
CPython ``re`` internally (permitted by the task brief).  It nonetheless honors
the candidate-window contract:

  * soundness/completeness for group 0 (R19): every window CPython would match
    is reported and none is invented;
  * windows are emitted in **transport units** (bytes) — UTF-8 byte offsets for
    str subjects (R14) — and marked *verified* (flags bit0) because the oracle
    is exact;
  * zero-width windows are flagged (bit1) per the result format (R38/R47).

The offsets are converted back to caller units by the L1 layer (R21).
"""

from __future__ import annotations

import threading
from typing import List, NamedTuple, Optional, Tuple

import re

from . import _classify

# Capture the original stdlib compiler at import time: ``pyro.install()`` may
# rebind ``re.compile`` on the module, and the model must not route through the
# patched shim (would recurse / double-wrap).
_stock_compile = re.compile

# pyro_match.flags bits (R38)
FLAG_VERIFIED = 0x1
FLAG_ZERO_WIDTH = 0x2

# pyro_encoding (R38)
ENC_BYTES = 0
ENC_UTF8 = 1


class DeviceError(Exception):
    """Models a transport/device failure (PYRO_E_DEVICE / PYRO_E_TIMEOUT).

    The router treats this as a signal to retry on the fallback path (R52).
    """


class UnsupportedPattern(Exception):
    """Model rejects a non-HW-eligible pattern (PYRO_E_UNSUPPORTED/CAPACITY)."""


class MatchWindow(NamedTuple):
    start: int          # inclusive byte offset (transport units)
    end: int            # exclusive byte offset
    pattern_id: int
    flags: int          # bit0 verified, bit1 zero_width


class ModelProgram:
    """Opaque compiled automaton program (mock of ``pyro_prog``)."""

    __slots__ = ("_re", "enc", "flags", "states", "resident")

    def __init__(self, compiled: "re.Pattern", enc: int, flags: int, states: int):
        self._re = compiled
        self.enc = enc
        self.flags = flags
        self.states = states
        self.resident = False


def utf8_prefix(s: str) -> List[int]:
    """UTF-8 prefix table: index i -> byte offset of ``s[:i]`` (len == len(s)+1).

    Strictly increasing (every code point is >= 1 byte), so it doubles as both
    the code-point->byte map (index it directly) and the byte->code-point map
    (``bisect_left``), astral-safe (R21): each char contributes its own UTF-8
    length.  Shared by the model (cp->byte, producing windows) and the router
    (byte->cp, translating them back).
    """
    out = [0] * (len(s) + 1)
    acc = 0
    for i, ch in enumerate(s):
        acc += len(ch.encode("utf-8"))
        out[i + 1] = acc
    return out


class ModelContext:
    """Mock host runtime / device context (shape of ``pyro_ctx``, §7.3).

    A single context is internally serialized (R32/R43/R48): one scan is in
    flight at a time.  Distinct contexts are independent.
    """

    def __init__(self):
        # Fault-injection hooks for exercising the R52 fallback-retry path.
        # These are private test seams; production callers never set them.
        self.fail_next_scan = False       # -> raise DeviceError on next scan
        self.unverify_windows = False     # -> emit windows with bit0 clear
        # Public-seam (R67) injection state, driven by pyro.testing behind the
        # PYRO_ENABLE_TEST_HOOKS gate.  All inert (0 / empty) until injected, so
        # the model path pays only a trivial int/dict check.
        self._inject_device = 0           # next N scans raise DeviceError (R52)
        self._inject_kind = "device"      # "device" | "timeout" (R67)
        self._inject_fp = {}              # pattern -> remaining spurious windows
        self._lock = threading.Lock()

    # -- R67 public fault-injection seams (via pyro.testing) ----------------
    def inject_device_error(self, kind: str = "device", count: int = 1) -> None:
        """Arm the next ``count`` model dispatches to raise a device error (R52)."""
        with self._lock:
            self._inject_kind = "timeout" if kind == "timeout" else "device"
            self._inject_device = max(0, int(count))

    def inject_false_positive(self, pattern, flags: int = 0, count: int = 1) -> None:
        """Arm the model to emit ``count`` spurious candidate windows for
        ``pattern`` (re-verified away by the host, R19); results stay identical."""
        with self._lock:
            self._inject_fp[pattern] = max(0, int(count))

    def clear_injections(self) -> None:
        """Clear all injected faults (R67 ``pyro.testing.reset``)."""
        with self._lock:
            self._inject_device = 0
            self._inject_kind = "device"
            self._inject_fp = {}
            self.fail_next_scan = False
            self.unverify_windows = False

    # --- R42 capability query ---------------------------------------------
    def caps(self) -> dict:
        return {
            "max_states": _classify.MAX_STATES,
            "max_patterns": _classify.MAX_PATTERNS,
            "max_repeat": _classify.MAX_REPEAT,
            "alphabet": _classify.ALPHABET,
            "engine_version": _classify.ENGINE_VERSION,
            "engine_kind": _classify.ENGINE_KIND_MODEL,
        }

    # --- R40 compile/load -------------------------------------------------
    def compile(self, pattern, flags: int, enc: int) -> ModelProgram:
        """Compile a pattern to a program, or raise ``UnsupportedPattern``.

        ``pattern`` is the original str/bytes needle; ``enc`` selects the
        subject encoding domain (R14).  Non-HW-eligible patterns raise
        (mirrors ``pyro_compile`` returning PYRO_E_UNSUPPORTED/CAPACITY, R40).
        """
        classi = _classify.classify(pattern, flags)
        if not classi.eligible:
            raise UnsupportedPattern(classi.reason)
        compiled = _stock_compile(pattern, flags)
        return ModelProgram(compiled, enc, flags, classi.states)

    def load(self, prog: ModelProgram) -> None:
        prog.resident = True

    # --- R41 scan ---------------------------------------------------------
    def scan(
        self,
        prog: ModelProgram,
        buf: bytes,
        start_off: int = 0,
        out_cap: int = 1 << 62,
        mode: str = "finditer",
    ) -> Tuple[List[MatchWindow], bool]:
        """Scan ``buf`` for group-0 windows; return ``(windows, overflowed)``.

        ``buf`` is the transported subject in encoding units (raw bytes for
        ENC_BYTES, UTF-8 for ENC_UTF8).  ``mode`` selects the engine's match
        discipline — a real engine encodes this in the program/CTRL word; here
        it maps to the corresponding CPython operation so group-0 windows are
        exact.  All offsets in the returned windows are byte offsets.
        """
        with self._lock:  # single-issue per engine instance (R48)
            if self.fail_next_scan:
                self.fail_next_scan = False
                raise DeviceError("injected device error")
            # R67 device/timeout injection: raise for the next N dispatches so the
            # router takes the R52 fallback-retry path (fallback_after_error++).
            if self._inject_device > 0:
                self._inject_device -= 1
                raise DeviceError(f"injected {self._inject_kind} error (R67)")

            vbit = 0 if self.unverify_windows else FLAG_VERIFIED

            if prog.enc == ENC_UTF8:
                subject = buf.decode("utf-8")
                cp_spans = _run(prog._re, subject, mode)
                cp2b = utf8_prefix(subject)
                windows = [
                    MatchWindow(cp2b[s], cp2b[e], 0,
                                vbit | (FLAG_ZERO_WIDTH if s == e else 0))
                    for (s, e) in cp_spans
                ]
            else:
                b_spans = _run(prog._re, buf, mode)
                windows = [
                    MatchWindow(s, e, 0,
                                vbit | (FLAG_ZERO_WIDTH if s == e else 0))
                    for (s, e) in b_spans
                ]

            # R67 false-positive injection: prepend a spurious, unverifiable
            # candidate window for this pattern.  Its end lies one byte past the
            # buffer, so the host's R19 re-verification (an anchored stock match)
            # can never confirm it -> the window is dropped (whole-op fallback),
            # never leaking into results (byte-identical to CPython, R16/R19).
            fp_left = self._inject_fp.get(prog._re.pattern, 0)
            if fp_left > 0:
                self._inject_fp[prog._re.pattern] = fp_left - 1
                windows = [MatchWindow(0, len(buf) + 1, 0, 0)] + windows

            windows = [w for w in windows if w.start >= start_off]
            overflowed = len(windows) > out_cap
            return windows[:out_cap], overflowed


def _run(compiled: "re.Pattern", subject, mode: str) -> List[Tuple[int, int]]:
    """Return group-0 spans for ``mode`` in the subject's own unit domain."""
    if mode == "search":
        m = compiled.search(subject)
        return [m.span(0)] if m is not None else []
    if mode == "match":
        m = compiled.match(subject)
        return [m.span(0)] if m is not None else []
    if mode == "fullmatch":
        m = compiled.fullmatch(subject)
        return [m.span(0)] if m is not None else []
    if mode == "finditer":
        return [m.span(0) for m in compiled.finditer(subject)]
    raise ValueError(f"unknown scan mode: {mode!r}")


# --- Process-wide model instance (Phase 0 has no physical device) ---------
_MODEL: Optional[ModelContext] = None


def get_model() -> ModelContext:
    global _MODEL
    if _MODEL is None:
        _MODEL = ModelContext()
    return _MODEL
