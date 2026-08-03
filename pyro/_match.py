"""Hybrid Match/Pattern wrappers (R17-R23, R27, R28).

``HybridMatch`` is a drop-in for ``re.Match`` whose group-0 span comes straight
from the FPGA/model candidate window (no CPython re-run — R20), and whose
group>0 / expand / groupdict / lastindex data is filled by *exactly one*
anchored CPython re-run over the window, computed lazily on first access
(R18/R20).

``PyroPattern`` is a drop-in for ``re.Pattern`` (R27) that routes each method
through the decision logic in :mod:`pyro._route`.
"""

from __future__ import annotations

import os
from typing import Tuple

# Module-level binding (not name import) so the _match <-> _route cycle
# resolves: at import time _route is only partially initialized, but methods
# below dereference ``_route.<fn>`` lazily at call time when it is complete.
# This keeps the hot fallback path free of per-call import machinery (R5).
from . import _route


class HybridMatch:
    """Lazy, capture-group-preserving match object (R28).

    Group 0 is served from ``span0`` without any re-run.  Any observation of a
    subgroup, ``groupdict``, ``expand``, ``lastindex`` or ``lastgroup``
    triggers a single anchored CPython re-run over the reported window.
    """

    __slots__ = (
        "_patt", "_stock", "_subject", "_span0", "_pos", "_endpos",
        "_op", "_ngroups", "_named", "_real",
    )

    def __init__(self, patt, subject, span0: Tuple[int, int],
                 pos: int, endpos: int, op: str):
        self._patt = patt              # PyroPattern (the .re attribute)
        self._stock = patt._stock      # compiled stdlib re.Pattern for re-run
        self._subject = subject
        self._span0 = span0
        self._pos = pos
        self._endpos = endpos
        # origin op: search/match/fullmatch/finditer
        self._op = op
        self._ngroups = patt._stock.groups
        self._named = bool(patt._stock.groupindex)
        self._real = None              # cached CPython re-run result

    # --- the one lazy re-run (R18/R20) ------------------------------------
    def _run(self):
        real = self._real
        if real is None:
            s, _e = self._span0
            # Primary reconstruction: anchor a plain match at the window start
            # over the FULL subject end (endpos == len on the model path).  This
            # preserves end-of-string / boundary context, so $, \Z, \b, \B are
            # evaluated exactly as in the original scan.  A window-TRUNCATED
            # fullmatch([s, e)) would move end-of-string to e and flip those
            # assertions at the window edge -- silently returning the same span
            # with the wrong group in an alternation (the regression this
            # fixes).
            real = self._stock.match(self._subject, s, self._endpos)
            if real is None or real.span(0) != self._span0:
                # The anchored match did not reproduce the reported window.
                # Legitimate for the empty-adjacency finditer case (R22: match()
                # returns the empty (s, s)) and for lazy-quantifier fullmatch.
                # Replay the origin op to adopt the exact (s, e) match.  R52:
                # MUST NOT raise.
                real = self._recover_via_fallback()
            self._real = real
        return real

    def _recover_via_fallback(self):
        """Recover this match's groups by replaying the origin op with stock re.

        The model produced the window by running stock ``re``'s own operation,
        so replaying that same op is guaranteed to contain a match whose
        group-0 span equals the reported window (R18/R19).  Never returns
        ``None`` -- the guarded chain always yields a real ``re.Match`` so
        subsequent accessors can never hit ``AttributeError`` (R52).
        """
        subj, (s, e) = self._subject, self._span0
        endpos, pos = self._endpos, self._pos
        if self._op == "finditer":
            for m in self._stock.finditer(subj, pos, endpos):
                if m.span(0) == (s, e):
                    return m
            raw = None
        else:
            raw = getattr(self._stock, self._op)(subj, pos, endpos)
            if raw is not None and raw.span(0) == (s, e):
                return raw
        # Defensive fallbacks (unreachable for a sound window), ordered so the
        # boundary-context-faithful primitives come first: the origin op's raw
        # result, then a full-context anchored match (real subject end), and
        # only as an absolute last resort the window-TRUNCATED fullmatch (which
        # moves end-of-string to e and can flip $/\b at the edge).  A match
        # always starts at s for any window the model reports, so the middle
        # candidate is non-None; never return None into _run.
        return (raw
                or self._stock.match(subj, s, endpos)
                or self._stock.fullmatch(subj, s, e))

    # --- group-0-only accessors: never re-run (R20) -----------------------
    @property
    def _slice0(self):
        s, e = self._span0
        return self._subject[s:e]

    def span(self, group=0):
        if group == 0:
            return self._span0
        return self._run().span(group)

    def start(self, group=0):
        if group == 0:
            return self._span0[0]
        return self._run().start(group)

    def end(self, group=0):
        if group == 0:
            return self._span0[1]
        return self._run().end(group)

    def group(self, *groups):
        if not groups:
            return self._slice0
        if len(groups) == 1:
            g = groups[0]
            if g == 0:
                return self._slice0
            return self._run().group(g)
        return tuple(self.group(g) for g in groups)

    def __getitem__(self, group):
        return self.group(group)

    def groups(self, default=None):
        if self._ngroups == 0:
            return ()
        return self._run().groups(default)

    def groupdict(self, default=None):
        if not self._named:
            return {}
        return self._run().groupdict(default)

    def expand(self, template):
        return self._run().expand(template)

    @property
    def lastindex(self):
        if self._ngroups == 0:
            return None
        return self._run().lastindex

    @property
    def lastgroup(self):
        if not self._named:
            return None
        return self._run().lastgroup

    @property
    def pos(self):
        return self._pos

    @property
    def endpos(self):
        return self._endpos

    @property
    def re(self):
        return self._patt

    @property
    def string(self):
        return self._subject

    @property
    def regs(self):
        return self._run().regs

    def __repr__(self):
        return "<pyro.Match span=%r match=%r>" % (self._span0, self._slice0)


class PyPattern:
    """Pure-Python drop-in for ``re.Pattern`` (R27).

    Wraps a compiled stdlib pattern (used for validation, fallback, and the
    hybrid re-run) plus the cached HW-eligibility classification (R4/R8).

    This class is kept in the tree **permanently** and is always constructible,
    even when the native ``pyro._fast.Pattern`` is active: it is the
    byte-for-byte
    behavioural reference the differential tests drive the native type against,
    and the fallback the whole package uses when the extension cannot be built
    (no C toolchain).  ``PyroPattern`` below is an alias for whichever class is
    selected at import.
    """

    __slots__ = ("_stock", "_classification", "_calls", "_prog", "__weakref__")

    def __init__(self, stock, classification):
        self._stock = stock
        self._classification = classification
        self._calls = 0  # per-pattern reuse counter (R51 step 4)
        self._prog = None  # lazily-compiled model program (R4 warm cache)

    # --- delegated attributes (R27) ---------------------------------------
    @property
    def pattern(self):
        return self._stock.pattern

    @property
    def flags(self):
        return self._stock.flags

    @property
    def groups(self):
        return self._stock.groups

    @property
    def groupindex(self):
        return self._stock.groupindex

    # --- routed match-producing methods -----------------------------------
    def search(self, string, pos=0, endpos=None):
        return _route.run_single(self, "search", string, pos, endpos)

    def match(self, string, pos=0, endpos=None):
        return _route.run_single(self, "match", string, pos, endpos)

    def fullmatch(self, string, pos=0, endpos=None):
        return _route.run_single(self, "fullmatch", string, pos, endpos)

    def finditer(self, string, pos=0, endpos=None):
        return _route.run_finditer(self, string, pos, endpos)

    def findall(self, string, pos=0, endpos=None):
        return _route.run_findall(self, string, pos, endpos)

    def sub(self, repl, string, count=0):
        return _route.run_sub(self, repl, string, count)

    def subn(self, repl, string, count=0):
        return _route.run_subn(self, repl, string, count)

    def split(self, string, maxsplit=0):
        return _route.run_split(self, string, maxsplit)

    def __repr__(self):
        return "<pyro.Pattern %r>" % (self._stock.pattern,)


# --- native-router selection (R3b/R3c) ------------------------------------
# ``pyro._fast.Pattern`` is a C extension type that serves the §8 R51 routing
# decision in compiled code (direct C call, no ctypes hop), deleting the four
# costs that keep the pure-Python router at ~4.5x stock.  It is an accelerator,
# never a fork: when it is absent (no C toolchain — a supported configuration)
# or explicitly disabled, ``PyroPattern`` is ``PyPattern`` and everything still
# works, just slow (R3a governs; R3b honestly SKIPs).
#
# PYRO_NO_NATIVE is read ONCE here, at import — it selects a class object, so it
# is explicitly outside the R35a per-call sampling contract and outside the
# R5/R3a per-call budget (R68).  CI runs the full suite with and without it so
# the pure-Python semantics can never rot behind the accelerator.
_fast = None
if not os.environ.get("PYRO_NO_NATIVE"):
    try:
        from . import _fast as _f
        # ROUTE_ABI guards a stale .so from an older build sitting in-tree.
        if getattr(_f, "ROUTE_ABI", 0) == 1:
            _fast = _f
    except ImportError:
        pass

PyroPattern = _fast.Pattern if _fast is not None else PyPattern
