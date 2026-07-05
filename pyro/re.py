"""L1 public API — a drop-in superset of the stdlib ``re`` subset (R26-R31).

``import pyro.re as re`` yields transparent acceleration (R33): every symbol in
R26 is provided with stock-``re`` signatures, ``error is re.error``, and results
are byte-identical to CPython for HW-eligible patterns (R16) or delegated for
fallback-only ones (R29).
"""

from __future__ import annotations

import re as _re
import threading

from . import _classify, _route
from ._match import PyroPattern

# Bind the *original* stdlib callables at import time.  ``pyro.install()``
# rebinds ``re.compile`` etc. on the stdlib module; internal use of these
# captured references avoids infinite recursion when PYRO is installed.
_stock_compile = _re.compile
_stock_escape = _re.escape
_stock_purge = _re.purge

# --- R26: error is the stdlib exception type ------------------------------
error = _re.error

# --- R26: flag constants are aliases of re's values -----------------------
A = ASCII = _re.ASCII
I = IGNORECASE = _re.IGNORECASE
L = LOCALE = _re.LOCALE          # accepted; forces fallback via classifier
M = MULTILINE = _re.MULTILINE
S = DOTALL = _re.DOTALL
X = VERBOSE = _re.VERBOSE
U = UNICODE = _re.UNICODE        # no-op for str (R26)

__all__ = [
    "compile", "search", "match", "fullmatch", "findall", "finditer",
    "sub", "subn", "split", "escape", "purge", "error", "explain", "stats",
    "A", "ASCII", "I", "IGNORECASE", "L", "LOCALE", "M", "MULTILINE",
    "S", "DOTALL", "X", "VERBOSE", "U", "UNICODE",
]

# --- compiled-pattern cache (R4, thread-safe R32) -------------------------
_CACHE_LOCK = threading.Lock()
_CACHE: dict = {}
_MAX_CACHE = 512


def compile(pattern, flags=0):
    """Compile ``pattern`` to a ``PyroPattern`` (R26/R27).

    Validation goes through ``re.compile`` first, so this raises ``re.error``
    under exactly the same conditions as the stdlib (R30).
    """
    if isinstance(pattern, PyroPattern):
        if flags:
            raise ValueError(
                "cannot process flags argument with a compiled pattern")
        return pattern
    if isinstance(pattern, _re.Pattern):
        if flags:
            raise ValueError(
                "cannot process flags argument with a compiled pattern")
        classi = _classify.classify(pattern.pattern, pattern.flags)
        return PyroPattern(pattern, classi)

    # R4: cache keyed by (pattern_bytes/str type, pattern, flags, engine_version)
    # so a bumped engine version never serves a stale compiled program.
    key = (type(pattern), pattern, int(flags), _classify.ENGINE_VERSION)
    # Lock-free read: dict.get is atomic under the GIL, and a benign miss just
    # recompiles.  This keeps the warm-cache fast path thin (R4/R5).
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    stock = _stock_compile(pattern, flags)  # R30: raises re.error identically
    classi = _classify.classify(pattern, flags)
    patt = PyroPattern(stock, classi)
    with _CACHE_LOCK:
        if len(_CACHE) >= _MAX_CACHE:
            _CACHE.clear()
        _CACHE[key] = patt
    return patt


# --- module-level functions (R26) -----------------------------------------
def search(pattern, string, flags=0):
    return compile(pattern, flags).search(string)


def match(pattern, string, flags=0):
    return compile(pattern, flags).match(string)


def fullmatch(pattern, string, flags=0):
    return compile(pattern, flags).fullmatch(string)


def findall(pattern, string, flags=0):
    return compile(pattern, flags).findall(string)


def finditer(pattern, string, flags=0):
    return compile(pattern, flags).finditer(string)


def sub(pattern, repl, string, count=0, flags=0):
    return compile(pattern, flags).sub(repl, string, count)


def subn(pattern, repl, string, count=0, flags=0):
    return compile(pattern, flags).subn(repl, string, count)


def split(pattern, string, maxsplit=0, flags=0):
    return compile(pattern, flags).split(string, maxsplit)


def escape(pattern):
    return _stock_escape(pattern)


def purge():
    """Clear PYRO's and the stdlib's compiled-pattern caches (R26)."""
    with _CACHE_LOCK:
        _CACHE.clear()
    _stock_purge()


# --- PYRO-specific introspection (R31) — NOT patched onto stdlib re --------
def explain(pattern, flags=0) -> dict:
    """Return the HW-eligibility verdict for ``(pattern, flags)`` (R31)."""
    _stock_compile(pattern, flags)  # R30: reject what CPython rejects
    classi = _classify.classify(pattern, flags)
    engine = "model" if classi.eligible else "fallback"
    return {
        "eligible": classi.eligible,
        "reason": classi.reason,
        "engine": engine,
        "states": classi.states,
    }


def stats() -> dict:
    """Dispatch counters: hardware / model / fallback / fallback_after_error."""
    return _route.stats()
