"""Routing / fallback decision logic (R51), device-error fallback (R52),
determinism (R53), and diagnostics counters (``stats``).

The decision order is exactly R51.  The fallback fast path (R3a/R5, AC-0-6) is
kept deliberately thin: resolve the cached classification, read the *cached*
env flags (never the process environment — R35a), apply the size/reuse gate,
then delegate straight to the stdlib pattern with no wrapper allocation.
"""

from __future__ import annotations

import os
import threading
from bisect import bisect_left

from . import _model
from ._match import HybridMatch, PyroPattern
from ._model import (
    DeviceError, UnsupportedPattern, ENC_BYTES, ENC_UTF8, FLAG_VERIFIED,
    utf8_prefix,
)


class _VerifyError(Exception):
    """A candidate window failed CPython re-verification (R19) -> fallback.

    Routing control flow (raised by ``_verify_window``, caught by the dispatch
    entry points), so it lives here rather than in the match wrappers.
    """

    def __init__(self, span):
        super().__init__(f"window {span} failed verification")
        self.span = span


# Offload thresholds (R2/R3): reuse count and minimum corpus size.
S_MIN = 64 * 1024   # 64 KiB
N_REUSE = 32

# --- diagnostics counters (stats(), R52/AC-3-4) ---------------------------
_STATS_LOCK = threading.Lock()
_STATS = {
    "hardware": 0,             # dispatches served by real device (none in Ph0)
    "model": 0,                # dispatches served by the software model
    "fallback": 0,             # dispatches routed to CPython re
    "fallback_after_error": 0, # device error / verify failure -> fallback (R52)
    "device_errors": 0,
    "total": 0,
}


def stats() -> dict:
    with _STATS_LOCK:
        return dict(_STATS)


def reset_stats() -> None:
    with _STATS_LOCK:
        for k in _STATS:
            _STATS[k] = 0


def _record(path: str) -> None:
    with _STATS_LOCK:
        _STATS[path] += 1
        _STATS["total"] += 1


def _record_error_fallback() -> None:
    with _STATS_LOCK:
        _STATS["device_errors"] += 1
        _STATS["fallback_after_error"] += 1
        _STATS["total"] += 1


# --- environment controls (R35 / R35a-R35d) -------------------------------
# PYRO_DISABLE / PYRO_FORCE_MODEL are NOT read on the per-call hot path (that
# would blow the R3a/R5 <=2 us decision budget: os.environ.get costs ~0.85 us
# each).  Instead they are *sampled* into a single cached snapshot at the
# deterministic sampling points of R35a: package import, install()/uninstall(),
# and refresh_env().  The per-call decision reads only the snapshot.
#
# The snapshot is a single (disabled, force_model) tuple held behind one module
# global.  A per-call decision loads that reference once (atomic under the GIL),
# so a concurrent re-sample can never split a call across old/new values —
# satisfying R35d's "MUST NOT alter any in-flight call's routing decision".
#
# Truthiness rule (fail-safe): a variable counts as SET for any value other than
# unset/empty/"0".  In particular PYRO_DISABLE=false / no / off all count as SET
# (disabled) — for an opt-out/incident-mitigation switch we bias toward the safe
# fallback path rather than silently ignoring an operator's non-canonical value.
_UNSET_VALUES = (None, "", "0")
_ENV = (False, False)                 # (disabled, force_model) cached snapshot
_ENV_LOCK = threading.Lock()          # serializes samplers (R35d thread-safety)


def sample_env() -> None:
    """Re-sample PYRO_DISABLE / PYRO_FORCE_MODEL from ``os.environ`` (R35a).

    Called only at sampling points (import, install/uninstall, refresh_env).
    Thread-safe and idempotent; publishes a new snapshot with a single atomic
    rebind so in-flight decisions are unaffected (R35d).
    """
    global _ENV
    with _ENV_LOCK:
        disabled = os.environ.get("PYRO_DISABLE") not in _UNSET_VALUES
        force = os.environ.get("PYRO_FORCE_MODEL") not in _UNSET_VALUES
        _ENV = (disabled, force)


# Sample once at first import of this module (R35a.1: package initialization).
sample_env()


# --- R51 decision ---------------------------------------------------------
def _is_full_span(string, pos, endpos) -> bool:
    """True iff the call scans the whole subject.

    Non-default pos/endpos change anchor/boundary context (^ $ \\b), which the
    Phase-0 model does not encode, so such calls route to fallback for
    correctness (documented limitation).
    """
    if pos != 0:
        return False
    if endpos is not None and endpos < len(string):
        return False
    return True


def _decide(patt: PyroPattern, string, pos, endpos) -> str:
    """Return 'model' or 'fallback' per R51; updates the reuse counter.

    Consults only the cached env snapshot (R35a); no os.environ access here.
    Applies the Phase-0 routing gates of spec v1.2.1: only exact ``str``/``bytes``
    subjects are HW-eligible, and ``str`` subjects must be strict-UTF-8
    transportable.  Both gate to *plain* fallback (no device touched, so counted
    as ``fallback``, not ``fallback_after_error``).
    """
    reuse = patt._calls
    patt._calls = reuse + 1

    disabled, force = _ENV                         # atomic snapshot (R35d)
    if disabled:                                   # R51.1 (cached flag)
        return "fallback"
    if not patt._classification.eligible:          # R51.2
        return "fallback"
    # C2: subclasses of str/bytes and bytearray/memoryview would make group(0)
    # (subject[s:e]) a different / unhashable / mutable type than stock re's
    # bytes result -- route them to the genuine re objects (Phase 0).
    tstr = type(string)
    if tstr is not str and tstr is not bytes:      # gate (v1.2.1)
        return "fallback"
    if not _is_full_span(string, pos, endpos):     # anchor-context safety
        return "fallback"
    if not force and len(string) < S_MIN and reuse < N_REUSE:
        return "fallback"                          # R51.4 loss regime (R3a)
    # C1: only strict-UTF-8-encodable str subjects can be transported to the
    # model; a lone surrogate (which stock re still matches) must fall back.
    # Checked here -- on the model-bound path only -- to keep the short fallback
    # fast path free of the O(n) encode probe.
    if tstr is str and not _utf8_transportable(string):
        return "fallback"
    return "model"                                 # R51.6 (no device in Ph0)


def _utf8_transportable(s: str) -> bool:
    try:
        s.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


# --- residency-aware routing (R51 step 5 amended, R4a/R65) -----------------
# The residency subsystem (pyro.synth) is imported lazily and cached so the
# per-call *fallback* fast path (AC-0-6) never touches it — only the model path
# (large/reused/forced, not the perf-measured path) consults it.  Because on this
# host there is no device (F5, R7), the software model stands in for the resident
# tier, so this consultation drives the launch policy (R4a) and lifecycle stats
# (R66) as a side effect and forces fallback only for a *permanently-fallback*
# pattern (R65).  It never raises into a caller (R52/R65).
_residency = None


def _consult_residency(patt, string) -> bool:
    """Register a HW-eligible dispatch and return ``True`` to force fallback.

    Returns ``True`` only when the pattern's circuit is **permanently
    fallback-only** (synthesis failed, R65) — ordinary routing, counted as a
    normal ``fallback`` (not ``fallback_after_error``, AC-1-6).  Otherwise ticks
    the launch policy / lifecycle and returns ``False`` (proceed via the model).
    """
    global _residency
    try:
        res = _residency
        if res is None:
            from .synth import residency as res  # cached in sys.modules
            _residency = res
        enc = ENC_UTF8 if isinstance(string, str) else ENC_BYTES
        outcome = res.get_manager().note_eligible_dispatch(
            patt._stock.pattern, patt._stock.flags, enc)
        return outcome == res.ROUTE_PERMANENT_FALLBACK
    except Exception:
        return False


# --- encoding helpers (R14/R21) -------------------------------------------
def _encode(string):
    if isinstance(string, str):
        return string.encode("utf-8"), ENC_UTF8
    return bytes(string), ENC_BYTES


def _win_to_cp(string, w, prefix=None):
    """Convert a model byte window to caller-unit (start, end).

    For str, byte offsets map to code-point indices via ``bisect_left`` on the
    UTF-8 prefix table (astral-safe, R21).  ``prefix`` may be supplied to reuse
    one table across many windows.
    """
    if isinstance(string, str):
        if prefix is None:
            prefix = utf8_prefix(string)
        return (bisect_left(prefix, w.start), bisect_left(prefix, w.end))
    return (w.start, w.end)


def _get_prog(patt: PyroPattern, string):
    if patt._prog is None:
        model = _model.get_model()
        enc = ENC_UTF8 if isinstance(string, str) else ENC_BYTES
        prog = model.compile(patt._stock.pattern, patt._stock.flags, enc)
        model.load(prog)  # resident (R4/R48); warm cache thereafter
        patt._prog = prog
    return patt._prog


def _verify_window(patt, string, w, span0):
    """Re-verify an unverified candidate window (R19).

    Verified windows (bit0 set) are trusted so that group-0 access never
    triggers a re-run (R20).  Unverified windows (fault injection) are checked
    against CPython *before returning*; a mismatch raises to force fallback.

    Uses a full-context anchored ``match`` (over the real subject end), NOT a
    window-truncated fullmatch: truncating to [s, e) would move end-of-string
    to e and flip $/\\Z/\\b/\\B at the window edge, defeating the check.  A
    window that a plain anchored match cannot confirm (e.g. an empty-adjacency
    window, R22) conservatively forces whole-op fallback.
    """
    if w.flags & FLAG_VERIFIED:
        return
    s, _e = span0
    m = patt._stock.match(string, s, len(string))
    if m is None or m.span(0) != span0:
        raise _VerifyError(span0)


# --- stdlib fallback delegation (thin; no wrappers) -----------------------
def _stock_op(patt, op, string, pos, endpos):
    """Delegate a pos/endpos-taking op (search/match/fullmatch/finditer/findall)
    to the stdlib pattern, defaulting endpos to len(string) in one place."""
    meth = getattr(patt._stock, op)
    if pos == 0 and endpos is None:
        return meth(string)
    return meth(string, pos, len(string) if endpos is None else endpos)


# --- model execution ------------------------------------------------------
def _model_single(patt, op, string, pos, endpos):
    model = _model.get_model()
    prog = _get_prog(patt, string)
    buf, _enc = _encode(string)
    windows, _ovf = model.scan(prog, buf, 0, mode=op)
    if not windows:
        return None
    w = windows[0]
    span0 = _win_to_cp(string, w)
    endpos_eff = len(string) if endpos is None else min(endpos, len(string))
    _verify_window(patt, string, w, span0)
    return HybridMatch(patt, string, span0, pos, endpos_eff, op)


def _model_finditer(patt, string, pos, endpos):
    model = _model.get_model()
    prog = _get_prog(patt, string)
    buf, _enc = _encode(string)
    windows, _ovf = model.scan(prog, buf, 0, mode="finditer")
    endpos_eff = len(string)
    # Build the UTF-8 prefix table at most once for the whole result set (str
    # only); translating each window per-call would be O(matches * len).
    # N3 (Phase-1 line item): this eagerly materialises every match into a list,
    # and the empty-adjacency recovery path (HybridMatch) re-scans via stock
    # finditer -- both fine for a software model but worth streaming/caching once
    # a real device engine lands.
    prefix = utf8_prefix(string) if isinstance(string, str) else None
    out = []
    for w in windows:
        span0 = _win_to_cp(string, w, prefix)
        _verify_window(patt, string, w, span0)
        out.append(HybridMatch(patt, string, span0, pos, endpos_eff, "finditer"))
    return out


# --- public dispatch entry points -----------------------------------------
def run_single(patt, op, string, pos=0, endpos=None):
    """search / match / fullmatch (R51/R52)."""
    path = _decide(patt, string, pos, endpos)
    if path == "fallback":
        _record("fallback")
        return _stock_op(patt, op, string, pos, endpos)
    if _consult_residency(patt, string):       # R65 permanent fallback -> routing
        _record("fallback")
        return _stock_op(patt, op, string, pos, endpos)
    try:
        result = _model_single(patt, op, string, pos, endpos)
    except (DeviceError, _VerifyError):
        _record_error_fallback()
        return _stock_op(patt, op, string, pos, endpos)
    except UnsupportedPattern:
        _record("fallback")
        return _stock_op(patt, op, string, pos, endpos)
    _record("model")
    return result


def run_finditer(patt, string, pos=0, endpos=None):
    path = _decide(patt, string, pos, endpos)
    if path == "fallback":
        _record("fallback")
        return _stock_op(patt, "finditer", string, pos, endpos)
    if _consult_residency(patt, string):       # R65 permanent fallback -> routing
        _record("fallback")
        return _stock_op(patt, "finditer", string, pos, endpos)
    try:
        matches = _model_finditer(patt, string, pos, endpos)
    except (DeviceError, _VerifyError):
        _record_error_fallback()
        return _stock_op(patt, "finditer", string, pos, endpos)
    except UnsupportedPattern:
        _record("fallback")
        return _stock_op(patt, "finditer", string, pos, endpos)
    _record("model")
    return iter(matches)


# Aggregate ops (findall/sub/subn/split): only the match SCAN is offloadable
# (§12 -- replacement/assembly is host-side); Phase 0 delegates the whole op to
# stock re, so they are honestly recorded as ``fallback`` (scan offload for
# aggregate ops is a Phase-1+ item).  We still run _decide for its reuse-counter
# side effect and env determinism.
def run_findall(patt, string, pos=0, endpos=None):
    _decide(patt, string, pos, endpos)
    _record("fallback")
    return _stock_op(patt, "findall", string, pos, endpos)


def run_sub(patt, repl, string, count=0):
    _decide(patt, string, 0, None)
    _record("fallback")
    return patt._stock.sub(repl, string, count)


def run_subn(patt, repl, string, count=0):
    _decide(patt, string, 0, None)
    _record("fallback")
    return patt._stock.subn(repl, string, count)


def run_split(patt, string, maxsplit=0):
    _decide(patt, string, 0, None)
    _record("fallback")
    return patt._stock.split(string, maxsplit)
