"""Shared R3b measurement protocol (spec v2.5.0, R3b.1-R3b.4) + R3c gate.

One normative implementation, used by AC-2-5 (SKIP-or-PASS per R3c) and by
AC-3-3 (hard PASS), so the two ACs can never measure R3b two different ways:

  * R3b.1 — verdict = median of >= TRIALS (>= 5) trial ratios; each trial is
    median(PYRO per-call ns) / median(stock-re per-call ns) over
    >= PAIRS_PER_TRIAL (>= 1000) paired calls on distinct loss-regime patterns
    (per-pattern reuse < N_reuse, subject < S_min), classification/compile
    caches primed on both sides before timing.  A single trial never decides.
  * R3b.2 — the subject is pinned at exactly 132 bytes of ASCII
    (:data:`SUBJECT_132`); ratios at any other size must not be used to claim
    or deny R3b compliance.
  * R3b.3 — :func:`native_routing_active` evaluates the R3c precondition
    truthfully (the compiled extension must be *actively serving* the R51
    routing decision, not merely importable), so a pure-Python build can
    record its honest SKIP-with-measured-median and never a silent PASS.
  * R3b.4 — :func:`rotating_warmup` keeps every warmup pattern's reuse
    < N_reuse (rotating >= ceil(calls / (N_reuse - 1)) throwaway patterns)
    and every warmup subject < S_min, so warmup exercises the SAME
    loss-regime routing path that is subsequently measured.  The previous
    single-pattern warmup crossed N_reuse at call 33 and warmed the *model*
    verdict path instead (measured defect, 2026-07-14: 1.26-1.30x half-cold
    vs 1.05-1.09x properly warmed on the identical router).
"""

import re as stdre
import statistics
import time

import pyro.re as pre
import pyro._match as _pyro_match
from pyro._thresholds import N_REUSE, S_MIN

# R3b bound — 1.15x, retained unchanged at v2.5.0 (vindicated: native floor
# ~1.01x, native router median ~1.10x at the pinned subject).
R3B_RATIO = 1.15

# R3b.2: the pinned subject — exactly 132 bytes, pure ASCII (132 code points
# in str mode), and well under S_min so every call is loss-regime.
SUBJECT_132 = ("the quick brown fox jumps over the lazy dog " * 3)[:132]
if len(SUBJECT_132) != 132 or not SUBJECT_132.isascii():
    raise AssertionError("R3b.2 subject must be exactly 132 bytes of ASCII")
if len(SUBJECT_132) >= S_MIN:
    raise AssertionError("R3b subject must stay below S_min (loss regime)")

TRIALS = 5              # R3b.1: >= 5 trials, median decides
PAIRS_PER_TRIAL = 1000  # R3b.1: >= 1000 paired calls per trial
WARMUP_CALLS = 3000     # R3b.4 rationale measurement used 3000 warmup calls


def native_routing_active():
    """R3c/R3b.3 precondition, evaluated truthfully: ``(active, description)``.

    ``active`` is True only when the compiled routing extension
    (``pyro._fast``) is loaded AND is *actively serving* the §8 R51 routing
    decision — i.e. ``pyro.re.compile`` actually hands back a
    ``_fast.Pattern``.  Mere importability of the extension is not enough
    (R3c: "the mere existence of an L3 native runtime does not satisfy the
    precondition").  ``description`` names the build either way, so a SKIP
    reason can quote it verbatim (R3b.3: the reason must name the
    pure-Python build).
    """
    fast = getattr(_pyro_match, "_fast", None)
    if fast is None:
        return False, (
            "pure-Python build: the compiled routing extension (pyro._fast) "
            "is not loaded (absent .so, PYRO_NO_NATIVE set, or ROUTE_ABI "
            "mismatch), so the §8 R51 routing decision runs at Python level")
    probe = pre.compile("r3bnativeprobe_zz")
    if not isinstance(probe, fast.Pattern):
        return False, (
            "pure-Python routing in effect: pyro._fast is importable but "
            "pyro.re.compile() returns %s, so the §8 R51 routing decision is "
            "NOT served by the compiled extension" % type(probe).__name__)
    return True, ("native routing hot path active: pyro._fast.Pattern serves "
                  "the §8 R51 routing decision in compiled code")


def rotating_warmup(tag, calls=WARMUP_CALLS, subject=SUBJECT_132):
    """R3b.4-compliant CPU warmup: stays in the loss regime for its duration.

    Rotates ``ceil(calls / (N_reuse - 1))`` throwaway patterns so no warmup
    pattern's reuse ever reaches ``N_reuse`` (each is used at most
    ``N_reuse - 1`` times), and the warmup subject stays below ``S_min`` —
    every warmup call is therefore a genuine below-threshold routing decision
    on the exact path the caller is about to time.  ``tag`` namespaces the
    throwaway patterns away from the measured ones.
    """
    if len(subject) >= S_MIN:
        raise AssertionError("R3b.4: warmup subject must stay below S_min")
    n_pats = -(-calls // (N_REUSE - 1))          # ceil
    re_c = [stdre.compile("%swarm%dzz" % (tag, i)) for i in range(n_pats)]
    py_c = [pre.compile("%swarm%dzz" % (tag, i)) for i in range(n_pats)]
    per_pat = -(-calls // n_pats)                # ceil; <= N_reuse - 1
    done = 0
    for _ in range(per_pat):
        for i in range(n_pats):
            re_c[i].search(subject)
            py_c[i].search(subject)
            done += 1
            if done >= calls:
                return


def one_trial(tag, pairs=PAIRS_PER_TRIAL, subject=SUBJECT_132):
    """One R3b.1 trial: ``(trial_ratio, med_stock_ns, med_pyro_ns)``.

    ``pairs`` distinct never-before-seen patterns (namespaced by ``tag``),
    compiled on both sides, classification/compile caches primed with one
    search each (reuse -> 1, still loss regime), R3b.4 rotating warmup, then
    one timed paired call per pattern (reuse -> 2 < N_reuse: every timed call
    is a genuine §8 below-threshold routing decision ending in fallback).
    """
    pats = ["%stok%dzz" % (tag, i) for i in range(pairs)]
    re_c = [stdre.compile(p) for p in pats]
    py_c = [pre.compile(p) for p in pats]
    for i in range(pairs):        # prime both sides (R3b.1; reuse < N_reuse)
        re_c[i].search(subject)
        py_c[i].search(subject)
    rotating_warmup(tag + "w", subject=subject)
    stock_ns, pyro_ns = [], []
    for i in range(pairs):
        t0 = time.perf_counter_ns()
        re_c[i].search(subject)
        t1 = time.perf_counter_ns()
        py_c[i].search(subject)
        t2 = time.perf_counter_ns()
        stock_ns.append(t1 - t0)
        pyro_ns.append(t2 - t1)
    med_stock = statistics.median(stock_ns)
    med_pyro = statistics.median(pyro_ns)
    ratio = (med_pyro / med_stock) if med_stock else float("inf")
    return ratio, med_stock, med_pyro


def measure_r3b(trials=TRIALS, pairs=PAIRS_PER_TRIAL, subject=SUBJECT_132,
                tag="r3b"):
    """Run the full R3b.1/R3b.2 protocol: ``(verdict, trial_ratios, detail)``.

    ``verdict`` is the median of ``trials`` (>= 5) trial ratios at the pinned
    132-byte subject; ``detail`` is the per-trial
    ``(ratio, med_stock_ns, med_pyro_ns)`` list for honest reporting.
    """
    if trials < 5:
        raise AssertionError("R3b.1: the verdict requires >= 5 trials")
    if pairs < 1000:
        raise AssertionError("R3b.1: each trial requires >= 1000 paired calls")
    detail = [one_trial("%st%d" % (tag, t), pairs=pairs, subject=subject)
              for t in range(trials)]
    trial_ratios = [d[0] for d in detail]
    return statistics.median(trial_ratios), trial_ratios, detail


def format_detail(verdict, trial_ratios, detail):
    """One-line human summary of a measure_r3b result (for skip/fail text)."""
    trials = ", ".join("%.3f" % r for r in trial_ratios)
    meds = "; ".join("%.0f/%.0f ns" % (d[2], d[1]) for d in detail)
    return ("median of %d trials = %.3fx at the pinned 132-byte subject "
            "(R3b.2); trial ratios [%s]; per-trial pyro/stock medians [%s]"
            % (len(trial_ratios), verdict, trials, meds))
