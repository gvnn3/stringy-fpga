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
# THE REAL `pyro_rp` BUDGET, from `hw/dfx/platform_manifest.json` `rp_budget`
# (SNORT-PF SF2, the binding envelope): 80,000 LUT / 160,000 FF / 160 BRAM36 /
# 64 URAM / 400 DSP on the flashed U250 shell.  These were 216,000/432,000/
# 4,320/768 through Phase 2 — placeholder device-scale numbers that predated
# the flashed shell and over-stated the region by 2.7x.  Nothing regressed when
# they were corrected: MAX_STATES (R13) is still the *binding* constraint (a
# MAX_STATES-sized single-pattern automaton estimates ~6.4k LUTs / ~1.5k FFs,
# well inside 80k/160k), so no classifier-eligible pattern is rejected for
# resources and no classification flips.  What changes is that the SNORT-PF
# SR8 "does this group fit" gate now closes against the real envelope instead
# of a fictional one — at 256 slots that is the difference between "68% of
# budget" and "25%".
#
# BRAM is expressed in KB of *usable* storage: 160 BRAM36 x 4.5 KB = 720 KB.
# A real device re-advertises from the manifest via the caps registers
# (R42/R45); ``tests/unit/test_estimator.py`` pins these constants to the
# manifest so the two can never drift again.
PR_LUTS = 80_000
PR_FFS = 160_000
PR_BRAM_KB = 720
PR_DSPS = 400
PR_PARTITIONS = 1  # single-tenant region (R64)

# Linear cost model (LUTs/FFs) — see module docstring.
#
# R74 recalibration (Phase 2, AC-2-4).  The pre-2.1.0 constants (base 2000
# LUTs/2000 FFs) predated any real synthesis and over-counted the fixed harness
# by ~10x, violating R74's `est <= 10 * real` for small patterns.  These
# constants are fit to genuine post-route (`report_utilization`) data from real
# Vivado 2023.1 OOC synth+P&R of `pyro_circuit` for the physical U250 part
# (`xcu250-figd2104-2L-e`), 250 MHz constraint, all met timing:
#
# R74a re-validation PENDING (v2.2.1, R70a-pin).  Post-route utilization is
# toolchain-bound, so this 2023.1-measured (0x17010000) calibration is NOT
# evidence for the re-pinned Vivado 2025.2 (0x19020000).  The constants are left
# unchanged deliberately — recalibration under 2025.2 happens later per R74a, and
# the R4/R75a cache key keeps 2023.1 and 2025.2 artifacts on distinct keys so no
# stale 2023.1 datum can claim an AC-2-4 PASS under the 2025.2 pin.  Data below is
# retained as the 2023.1 provenance record, awaiting re-measurement under 2025.2:
#
#   pattern                              n_states  byte_edges  real_LUTs  real_FFs
#   rb"abc"                                     4         3        197       427
#   rb"[a-z]+[0-9]{2,4}"                        9         6        197       430
#   rb"(?:GET|POST|PUT) /[a-z/]* HTTP"        24        18        221       441
#   rb"^ERROR: .*$"                           12         8        226       431
#   rb"[A-Za-z0-9]{60}"                        62        60        228       484
#   rb"[A-Za-z0-9]{200}"                      202       200        410       626
#
# Observations: (1) the ~200-LUT / ~430-FF floor (CSR mux, control FSM, 64-bit
# byte-index/offset counters, result-ring writer) dominates small patterns; (2)
# real FFs grow at almost exactly 1/state (the one-hot state vector is NSTATES
# bits by construction), with a strikingly stable intercept `real_ffs - n_states`
# of 417-424; (3) real LUTs grow only ~1 per (state≈edge).  The coefficients
# below deliberately over-count the *slope* (LUTs ~6/state combined, i.e. several
# times the observed ~1/state) so the estimate stays a conservative over-estimate
# for automata larger than the corpus (R74 clauses 1-2, `real <= est`), while the
# modest bases keep every corpus point within the 10x ceiling (R74 clauses 3-4):
# measured est/real ratios are 1.4-3.6x (LUTs) and ~1.05x (FFs).  MAX_STATES
# (=1024) remains the *binding* eligibility constraint (see the budget comment
# above): even a MAX_STATES-sized automaton estimates ~6.4k LUTs / ~1.5k FFs,
# far under PR_LUTS/PR_FFS, so no classifier-eligible pattern is rejected for
# resources and no classification flips.
# v2.3.0 (R45a): the perf counters add a 64-bit BUSY-cycle counter + four CSR
# read-mux sources to every circuit (~64 FFs / ~50 LUTs of real floor on top of
# the corpus data above, which predates them).  Intercepts bumped +64/+64 to
# keep `real <= est` conservative (R74 clauses 1-2); slopes unchanged.  The
# pending R74a re-measurement under 2025.2 recalibrates both together.
_HARNESS_LUTS = 320         # CSR block, DMA sequencing, result-ring writer
_HARNESS_FFS = 512
_LUTS_PER_STATE = 4
_LUTS_PER_EDGE = 2
_FFS_PER_STATE = 1

# --- SR7 group-harness additions (SNORT-PF) --------------------------------
# The group harness in `generator._emit_rtl_group` adds registers the
# single-pattern harness does not have, and `_HARNESS_FFS` has almost no slack
# to absorb them: the calibration corpus above measures `real_ffs - n_states` =
# 417-424, i.e. only ~90 bits of intercept headroom over the 512 base, and
# `_FFS_PER_STATE = 1` is exact by construction (the one-hot state vector IS
# NSTATES bits).  So every new group register has to be modelled explicitly or
# R74 clause 2 (`real <= est`) stops holding — for a 253-slot group at
# datapath_bytes=8 the omission was ~48%.
#
# The registers, verbatim from the emitter:
#   `reg [NPEND-1:0] pend`   NPEND = datapath_bytes * 2**ceil(log2(n_slots))
#   `reg [63:0] pend_base`
#   the 1-deep input skid (HAZARD (a)): skid_data (8*width), skid_keep (width,
#   only when width > 1), skid_valid, skid_last.
# Every `pend` bit with slot < n_slots is functionally live (it is the accept
# of one slot on one lane), so the vector is not trimmable.
#
# `pend` also costs LUTs the single-pattern model has no term for: the flat
# accept vector, the NPEND-wide priority encoder, and the per-bit set/clear.
# That is the one piece of group logic whose cost scales as
# n_slots x datapath_bytes rather than with the automata, so the SR8 "does this
# group fit" gate needs it; 3 LUTs/bit keeps it a conservative over-estimate
# (the encoder itself is a ~1-LUT/bit reduction tree).
_LUTS_PER_PEND_BIT = 3
_GROUP_PEND_BASE_FFS = 64   # `pend_base`


def _clog2(x: int) -> int:
    """``ceil(log2(x))`` for ``x >= 1`` (0 for ``x <= 1``).

    Duplicated from :mod:`pyro.hdl.generator` (three lines) rather than
    imported: the generator imports *this* module for the caps/budget block, so
    importing it back would close a cycle.
    """
    return max(0, (int(x) - 1).bit_length())


def _group_harness(n_slots: int, width: int) -> dict:
    """The SR7 group harness's own LUT/FF cost, above the shared base.

    Mirrors ``generator._emit_rtl_group``'s declarations exactly; kept next to
    the constants so the two can be diffed by eye when the emitter changes.
    """
    slot_bits = max(1, _clog2(n_slots))
    npend = width << slot_bits                      # == width * stride
    skid_ffs = 8 * width + 2 + (width if width > 1 else 0)
    return {
        "npend": npend,
        "ffs": npend + _GROUP_PEND_BASE_FFS + skid_ffs,
        "luts": _LUTS_PER_PEND_BIT * npend,
    }


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

    # Defensive: the classifier judged this pattern eligible, but if the HDL
    # lowering hits a construct it cannot yet represent, report fallback-only
    # (with a reason) rather than raising — keeps explain()/callers total (R8).
    try:
        au = _auto.build(pattern, flags, enc)
    except ValueError as exc:
        return ResourceEstimate(False, f"generator lowering gap: {exc}", None, None)
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


def estimate_group(automata, datapath_bytes: int = 1) -> dict:
    """Resources of an SR7 pattern-SET circuit: N automata, ONE harness.

    ``automata`` is the slot-ordered sequence handed to
    :func:`pyro.hdl.generator.generate_group` (``None`` = a tombstoned slot,
    SR6: it keeps its ``pattern_id`` and costs nothing but its index).

    The model is :func:`estimate`'s, applied per slot, with the fixed harness
    counted **once** — that sharing is the whole point of SR7 (measured:
    452 -> 130 LUT/slot when the harness stops being replicated).  Widening
    the datapath replicates the per-slot closure/move logic ``datapath_bytes``
    times (the cascade in ``_emit_rtl_group``) but not the state registers, so
    the *automaton* term scales with the width and the state flops do not.

    On top of that the group harness has hardware of its own that neither the
    single-pattern model nor the per-slot terms cover: the ``pend``
    pending-accept vector, ``pend_base``, the 1-deep input skid, and the
    priority encoder over ``pend``.  Its cost scales as
    ``n_slots x datapath_bytes`` (not with the automata at all), so it is
    modelled explicitly by :func:`_group_harness` — see the constants block.
    Widening the datapath therefore *does* add flops (4x ``NPEND`` from dpb=1
    to dpb=8 plus the wider skid); it is only the one-hot state vector that is
    width-independent.  Without that term ``real_ffs <= est_ffs`` (R74
    clause 2, cited normatively by SR8) fails outright at 253 slots.

    Returns the same dict shape as ``ResourceEstimate.resources`` with
    ``num_patterns`` = the SLOT count (tombstones included — ``pattern_id``
    is an index into slots, never into the live subset).  No fit *decision* is
    made here: an SR8 group build is gated by the caller against
    :func:`budget`, because a group that does not fit is a packing question
    (fewer slots per group), not an eligibility question about one pattern.
    """
    aus = list(automata)
    n_slots = len(aus)
    n_states = sum(a.n_states for a in aus if a is not None)
    n_byte_edges = sum(
        1 for a in aus if a is not None
        for s in range(a.n_states) for e in a.edges[s]
        if e.kind == _auto.E_BYTE
    )
    width = max(1, int(datapath_bytes))
    harness = _group_harness(n_slots, width)
    luts = (_HARNESS_LUTS + harness["luts"] + width * (
        _LUTS_PER_STATE * n_states + _LUTS_PER_EDGE * n_byte_edges))
    ffs = _HARNESS_FFS + harness["ffs"] + _FFS_PER_STATE * n_states
    return {
        "luts": luts,
        "ffs": ffs,
        "bram_kb": 16,
        "dsps": 0,
        "automaton_states": n_states,
        "byte_edges": n_byte_edges,
        "ast_states": n_states,
        "num_patterns": n_slots,
        "live_slots": sum(1 for a in aus if a is not None),
        # The group-harness terms, surfaced so an SR8 fit report can say which
        # half of the estimate is automata and which is arbitration.
        "pend_bits": harness["npend"],
        "group_harness_luts": harness["luts"],
        "group_harness_ffs": harness["ffs"],
    }
