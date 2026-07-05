"""Resource estimator (L2, spec §5.3, R11-R13).

A **pure function of the automaton** that decides whether a HW-eligible pattern's
generated circuit fits the PR-region resource budget and the advertised
complexity bounds (``MAX_STATES`` / ``MAX_PATTERNS`` / ``MAX_REPEAT``), *without*
a full synthesis (R11).  A pattern the estimator rejects is fallback-only with a
reason, integrating with :mod:`pyro._classify` (which already enforces the same
MAX_* bounds during eligibility, R8/R12).

Integration (Task-5 brief, item 2).  Eligibility and the authoritative
``states`` count come from :func:`pyro._classify.classify` — the single source of
truth used by the Phase-0 router — so the estimator never *accepts* a pattern
the classifier rejects and never diverges from it on the MAX_* boundaries.  On
top of that decision the estimator adds the derived PR-region resource numbers
(LUT/FF/BRAM/DSP) advertised via the caps block (R11/R42) and surfaced through
``pyro.re.explain()`` (R31 ``est_resources``).

The resource model is a deliberately simple linear function of the automaton
(one FF per NFA state for the one-hot state vector, LUTs per state and per byte-
comparator edge, plus a fixed harness overhead).  It is intentionally *not* a
synthesis; a pattern that passes the estimate but fails real P&R becomes
permanently fallback-only later (R12/R65), which is out of scope here.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

from .. import _classify
from . import automaton as _auto

# --- advertised PR-region resource budget (R11/R13, P5) --------------------
# Device/shell-specific in reality (advertised from the manifest, R47b); these
# are the software-model defaults.  They are sized so the derived complexity
# bound MAX_STATES (R13) is the *binding* constraint: any pattern the classifier
# accepts (states <= MAX_STATES) also fits this budget, so the estimator never
# rejects an otherwise-eligible pattern for resources on the model.  A real
# device re-advertises tighter numbers via the caps registers (R42/R45).
PR_LUTS = 216_000
PR_FFS = 432_000
PR_BRAM_KB = 4_320
PR_DSPS = 768
PR_PARTITIONS = 1  # single-tenant region (R64)

# Linear cost model (LUTs/FFs) — see module docstring.
_HARNESS_LUTS = 2_000       # CSR block, DMA sequencing, result-ring writer
_HARNESS_FFS = 2_000
_LUTS_PER_STATE = 8
_LUTS_PER_EDGE = 4
_FFS_PER_STATE = 1


class ResourceEstimate(NamedTuple):
    eligible: bool
    reason: str
    states: Optional[int]          # AST-expanded state count (matches _classify)
    resources: Optional[dict]      # None when fallback-only

    def as_explain(self) -> Optional[dict]:
        """The ``est_resources`` sub-dict for ``pyro.re.explain()`` (R31)."""
        return self.resources


def budget() -> dict:
    """The advertised PR-region resource budget (R11/R42)."""
    return {
        "pr_luts": PR_LUTS,
        "pr_ffs": PR_FFS,
        "pr_bram_kb": PR_BRAM_KB,
        "pr_dsps": PR_DSPS,
        "pr_partitions": PR_PARTITIONS,
        "max_states": _classify.MAX_STATES,
        "max_patterns": _classify.MAX_PATTERNS,
        "max_repeat": _classify.MAX_REPEAT,
    }


def estimate(pattern, flags: int = 0, enc: int = None) -> ResourceEstimate:
    """Estimate a pattern's circuit resources and decide fit (R11-R13).

    Pure and deterministic (R8).  Returns ``eligible=False`` with a reason for
    any pattern outside the supported subset (R10) or over any advertised bound
    (R12); otherwise returns the derived resource numbers.
    """
    classi = _classify.classify(pattern, flags)
    if not classi.eligible:
        return ResourceEstimate(False, classi.reason, None, None)

    # Single-pattern circuit: NUM_PAT == 1 (multi-pattern sets are a future
    # extension; MAX_PATTERNS is advertised but not exercised here).
    n_patterns = 1
    if n_patterns > _classify.MAX_PATTERNS:
        return ResourceEstimate(False, "exceeds MAX_PATTERNS", None, None)

    au = _auto.build(pattern, flags, enc)
    n_states = au.n_states
    n_byte_edges = sum(
        1 for s in range(au.n_states) for e in au.edges[s]
        if e.kind == _auto.E_BYTE
    )

    luts = _HARNESS_LUTS + _LUTS_PER_STATE * n_states + _LUTS_PER_EDGE * n_byte_edges
    ffs = _HARNESS_FFS + _FFS_PER_STATE * n_states
    # No BRAM/DSP in the one-hot NFA datapath model; harness reserves a small
    # fixed amount of BRAM for the result ring staging.
    bram_kb = 16
    dsps = 0

    resources = {
        "luts": luts,
        "ffs": ffs,
        "bram_kb": bram_kb,
        "dsps": dsps,
        "automaton_states": n_states,
        "byte_edges": n_byte_edges,
        "ast_states": classi.states,
        "num_patterns": n_patterns,
    }

    # Secondary PR-region budget check (R12).  With the model's default budget
    # this never binds for classifier-eligible patterns, but a real device may
    # advertise a tighter budget that does.
    if luts > PR_LUTS:
        return ResourceEstimate(False, "exceeds PR-region LUT budget", None, None)
    if ffs > PR_FFS:
        return ResourceEstimate(False, "exceeds PR-region FF budget", None, None)
    if bram_kb > PR_BRAM_KB:
        return ResourceEstimate(False, "exceeds PR-region BRAM budget", None, None)
    if dsps > PR_DSPS:
        return ResourceEstimate(False, "exceeds PR-region DSP budget", None, None)

    return ResourceEstimate(True, "eligible", classi.states, resources)
