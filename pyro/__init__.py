"""PYRO — transparent Python regex offload (Phase 0, software-only).

Public entry points:

  * ``import pyro.re as re``          — import-alias acceleration (R33)
  * ``pyro.install()`` / ``uninstall()`` — patch/restore the stdlib ``re`` (R34)

Everything hardware-related is served by a pure-Python software model of the
FPGA engine (R7); results are byte-identical to CPython ``re`` for the supported
subset (R16) and delegated otherwise (R29).
"""

from __future__ import annotations

import re as _stdlib_re
import threading

# Import the router at package initialization so the env snapshot is sampled at
# "first import of pyro" (R35a.1).  This is the earliest deterministic sampling
# point; a variable set before process start is therefore honored as if read
# per-call (R35b).
from . import _route as _route

__version__ = "0.0.1"
# Packed C-ABI version this build targets (R37); mirrors pyro_abi_version().
PYRO_ABI_VERSION = 0x00010000

# Names on the stdlib ``re`` module that install() overrides with PYRO's.
# NOTE: ``explain``/``stats`` are intentionally excluded — they MUST NOT appear
# on the patched standard ``re`` namespace (R31).
_PATCHED = (
    "compile", "search", "match", "fullmatch", "findall", "finditer",
    "sub", "subn", "split", "purge",
)

_ORIGINALS: dict = {}
# Serializes install()/uninstall() so a racing pair cannot (a) both capture
# originals -- the loser capturing PYRO's own patched functions as "originals"
# and corrupting restore -- nor (b) interleave patch/restore (W2, TOCTOU).
_INSTALL_LOCK = threading.Lock()


def install() -> None:
    """Patch the already-imported stdlib ``re`` so calls route through PYRO (R34).

    Idempotent and thread-safe.  ``PYRO_DISABLE=1`` still forces fallback (R35);
    fallback-only patterns are indistinguishable from stock ``re`` (R3/R29).
    """
    global _ORIGINALS
    _route.sample_env()  # sampling point R35a.2 (always, even if already on)
    with _INSTALL_LOCK:
        if _ORIGINALS:
            return  # already installed (double-checked under the lock)
        from . import re as _pyro_re

        saved = {}
        for name in _PATCHED:
            saved[name] = getattr(_stdlib_re, name)
            setattr(_stdlib_re, name, getattr(_pyro_re, name))
        _ORIGINALS = saved


def uninstall() -> None:
    """Fully restore the stdlib ``re`` behavior patched by :func:`install` (R34)."""
    global _ORIGINALS
    _route.sample_env()  # sampling point R35a.2 (always, even if not installed)
    with _INSTALL_LOCK:
        if not _ORIGINALS:
            return
        for name, obj in _ORIGINALS.items():
            setattr(_stdlib_re, name, obj)
        _ORIGINALS = {}


def refresh_env() -> None:
    """Re-sample PYRO_DISABLE / PYRO_FORCE_MODEL from ``os.environ`` (R35d).

    Applies the new values to all subsequent top-level calls.  Thread-safe and
    idempotent; does not alter any in-flight call's routing decision.
    """
    _route.sample_env()


def is_installed() -> bool:
    return bool(_ORIGINALS)


def prewarm(patterns, flags=0) -> None:
    """Request background synthesis of one or more HW-eligible patterns (R62).

    Accepts a single pattern (``str``/``bytes``/compiled ``Pattern``) or an
    iterable of them.  For each **HW-eligible** pattern it enqueues synthesis
    (R4a/R63) regardless of call count; fallback-only patterns are silently
    ignored.  Returns promptly (non-blocking) and MUST NOT raise for a
    fallback-only pattern or one whose synthesis later fails (R62/R65).

    ``pyro.prewarm`` is PYRO-specific and is NOT patched onto the standard ``re``
    namespace by :func:`install` (R62/§7.2).
    """
    from .synth import residency as _res

    if isinstance(patterns, (str, bytes, bytearray)) or hasattr(patterns, "pattern"):
        items = [patterns]
    else:
        try:
            items = list(patterns)
        except TypeError:
            items = [patterns]

    mgr = _res.get_manager()
    for item in items:
        try:
            if hasattr(item, "pattern") and not isinstance(
                    item, (str, bytes, bytearray)):
                pat, f = item.pattern, getattr(item, "flags", flags)
            else:
                pat, f = item, flags
            mgr.prewarm(pat, f)
        except Exception:
            # R62: never raise for a fallback-only / un-synthesizable pattern.
            continue


# Expose the R67 public fault-injection seam as ``pyro.testing`` (importable
# always; inert unless PYRO_ENABLE_TEST_HOOKS=1 was sampled).  Lightweight: it
# pulls only _route/_model, no synthesis machinery, until a seam is invoked.
from . import testing  # noqa: E402
