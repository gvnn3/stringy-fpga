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

from . import _model
from ._match import HybridMatch, PyroPattern, byte_to_cp_map, _VerifyError
from ._model import DeviceError, UnsupportedPattern, ENC_BYTES, ENC_UTF8, FLAG_VERIFIED

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
_FALSEY_STR = (None, "", "0")
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
        disabled = os.environ.get("PYRO_DISABLE") not in _FALSEY_STR
        force = os.environ.get("PYRO_FORCE_MODEL") not in _FALSEY_STR
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
    """
    reuse = patt._calls
    patt._calls = reuse + 1

    disabled, force = _ENV                         # atomic snapshot (R35d)
    if disabled:                                   # R51.1 (cached flag)
        return "fallback"
    if not patt._classification.eligible:          # R51.2
        return "fallback"
    if not _is_full_span(string, pos, endpos):     # anchor-context safety
        return "fallback"
    if force:                                      # R51.4 override (cached)
        return "model"
    if len(string) < S_MIN and reuse < N_REUSE:    # R51.4 loss regime (R3a)
        return "fallback"
    return "model"                                 # R51.6 (no device in Ph0)


# --- encoding helpers (R14/R21) -------------------------------------------
def _encode(string):
    if isinstance(string, str):
        return string.encode("utf-8"), ENC_UTF8
    return bytes(string), ENC_BYTES


def _win_to_cp(string, w):
    """Convert a model byte window to caller-unit (start, end)."""
    if isinstance(string, str):
        b2c = byte_to_cp_map(string)
        return (b2c[w.start], b2c[w.end])
    return (w.start, w.end)


def _get_prog(patt: PyroPattern, string):
    if patt._prog is None:
        model = _model.get_model()
        enc = ENC_UTF8 if isinstance(string, str) else ENC_BYTES
        prog = model.compile(patt._stock.pattern, patt._stock.flags, enc)
        model.load(prog)  # resident (R4/R48); warm cache thereafter
        patt._prog = prog
    return patt._prog


def _verify_window(patt, string, w, span0, anchor_full):
    """Re-verify an unverified candidate window (R19).

    Verified windows (bit0 set) are trusted so that group-0 access never
    triggers a re-run (R20).  Unverified windows (fault injection) are checked
    against CPython *before returning*; a mismatch raises to force fallback.
    """
    if w.flags & FLAG_VERIFIED:
        return
    start = span0[0]
    if anchor_full:
        m = patt._stock.fullmatch(string, start)
    else:
        m = patt._stock.match(string, start)
    if m is None or m.span(0) != span0:
        raise _VerifyError(span0)


# --- stdlib fallback delegation (thin; no wrappers) -----------------------
def _stock_single(patt, op, string, pos, endpos):
    meth = getattr(patt._stock, op)
    if pos == 0 and endpos is None:
        return meth(string)
    return meth(string, pos, len(string) if endpos is None else endpos)


def _stock_finditer(patt, string, pos, endpos):
    if pos == 0 and endpos is None:
        return patt._stock.finditer(string)
    return patt._stock.finditer(string, pos, len(string) if endpos is None else endpos)


def _stock_findall(patt, string, pos, endpos):
    if pos == 0 and endpos is None:
        return patt._stock.findall(string)
    return patt._stock.findall(string, pos, len(string) if endpos is None else endpos)


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
    _verify_window(patt, string, w, span0, op == "fullmatch")
    return HybridMatch(patt, string, span0, pos, endpos_eff, op == "fullmatch")


def _model_finditer(patt, string, pos, endpos):
    model = _model.get_model()
    prog = _get_prog(patt, string)
    buf, _enc = _encode(string)
    windows, _ovf = model.scan(prog, buf, 0, mode="finditer")
    endpos_eff = len(string)
    # Build the byte->code-point map at most once for the whole result set
    # (str only); rebuilding it per window is O(matches * len) — a real
    # pathology on large corpora.
    b2c = byte_to_cp_map(string) if isinstance(string, str) else None
    out = []
    for w in windows:
        span0 = (b2c[w.start], b2c[w.end]) if b2c is not None else (w.start, w.end)
        _verify_window(patt, string, w, span0, False)
        out.append(HybridMatch(patt, string, span0, pos, endpos_eff, False))
    return out


# --- public dispatch entry points -----------------------------------------
def run_single(patt, op, string, pos=0, endpos=None):
    """search / match / fullmatch (R51/R52)."""
    path = _decide(patt, string, pos, endpos)
    if path == "fallback":
        _record("fallback")
        return _stock_single(patt, op, string, pos, endpos)
    try:
        result = _model_single(patt, op, string, pos, endpos)
    except (DeviceError, _VerifyError):
        _record_error_fallback()
        return _stock_single(patt, op, string, pos, endpos)
    except UnsupportedPattern:
        _record("fallback")
        return _stock_single(patt, op, string, pos, endpos)
    _record("model")
    return result


def run_finditer(patt, string, pos=0, endpos=None):
    path = _decide(patt, string, pos, endpos)
    if path == "fallback":
        _record("fallback")
        return _stock_finditer(patt, string, pos, endpos)
    try:
        matches = _model_finditer(patt, string, pos, endpos)
    except (DeviceError, _VerifyError):
        _record_error_fallback()
        return _stock_finditer(patt, string, pos, endpos)
    except UnsupportedPattern:
        _record("fallback")
        return _stock_finditer(patt, string, pos, endpos)
    _record("model")
    return iter(matches)


def run_findall(patt, string, pos=0, endpos=None):
    # Group extraction / result assembly is a host-side operation (§12); the
    # scan is conceptually offloaded but the assembled result is byte-identical
    # to CPython regardless of path (R53), so we compute it via the stdlib
    # pattern and record the dispatch label for diagnostics.
    path = _decide(patt, string, pos, endpos)
    _record(path)
    return _stock_findall(patt, string, pos, endpos)


def run_sub(patt, repl, string, count=0):
    path = _decide(patt, string, 0, None)
    _record(path)
    return patt._stock.sub(repl, string, count)


def run_subn(patt, repl, string, count=0):
    path = _decide(patt, string, 0, None)
    _record(path)
    return patt._stock.subn(repl, string, count)


def run_split(patt, string, maxsplit=0):
    path = _decide(patt, string, 0, None)
    _record(path)
    return patt._stock.split(string, maxsplit)
