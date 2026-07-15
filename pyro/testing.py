"""Public test/verification seam — ``pyro.testing`` (spec §9.1 R67; extended v2.5.0).

R67 requires a public, spec-only-author-drivable test-hook namespace so that the
AC-1-6 fault-injection behaviors (R52 device-error retry, R65 synthesis-failure
permanent fallback, R19 false-positive re-verification) — and, since v2.5.0, the
AC-3-2 tier-upgrade edge — can be exercised WITHOUT reading or patching
internals.  The seams here:

  * ``inject_device_error(kind="device"|"timeout", count=1)`` — the next ``count``
    model/hardware dispatches raise the corresponding device error, driving the
    R52 fallback-retry path and its ``fallback_after_error`` counter (R66).
  * ``inject_synth_failure(pattern, flags=0)`` — the named pattern's next
    synthesis fails (R65): permanent fallback, diagnostic, ``synth_failed`` (R66).
  * ``inject_false_positive(pattern, flags=0, count=1)`` — the model emits
    ``count`` spurious candidate windows for the pattern, re-verified away by the
    host (R19) so they never leak into results.
  * ``await_synthesis(pattern, flags=0, timeout=30.0)`` (v2.5.0) — block the
    CALLING test until the pattern's circuit reaches a terminal lifecycle tier
    (``"resident"`` / ``"fallback_only"``, R4/R31) or ``timeout`` elapses, and
    return the ``circuit_status`` actually reached (last observed on timeout).
    A test observation point, NOT a synchronization primitive of the system.
  * ``set_strict_residency(enabled=True)`` (v2.5.0) — suspend the R51b
    device-free precedence (R51b-strict): R51 step 5 binds exactly as on
    hardware (not-resident ⇒ genuine ``fallback`` dispatch; resident ⇒ ``model``
    dispatch), making the AC-3-2 fallback→resident upgrade edge observable via
    ``pyro.re.stats()`` on a device-free host.  Results stay byte-identical.
  * ``reset()`` — clear all injected faults and disable strict residency.

Guard (R67).  To avoid production foot-guns, EVERY seam here takes effect ONLY
when ``PYRO_ENABLE_TEST_HOOKS=1`` was sampled at an R35a sampling point (import,
``install()``/``uninstall()``, ``refresh_env()``).  The gate is read from the
cached env snapshot (``pyro._route.test_hooks_enabled``) — never the live
environment on any hot path.  When the gate is off the functions are importable
no-ops that raise nothing (``await_synthesis`` returns the current tier
immediately without waiting; ``set_strict_residency`` does nothing); injection
is deterministic and never alters caller-visible results relative to CPython
``re`` (R16 — the fallback nets catch every injected fault, and a
strict-residency fallback returns the same bytes the model would have).

This namespace is PYRO-specific and is NOT patched onto the standard ``re``
module by :func:`pyro.install` (§7.2), mirroring ``explain``/``stats``/``prewarm``.
"""

from __future__ import annotations

import time as _time

from . import _route
from . import _model

__all__ = [
    "inject_device_error",
    "inject_synth_failure",
    "inject_false_positive",
    "await_synthesis",
    "set_strict_residency",
    "reset",
]

# Terminal lifecycle tiers (R4/R31): synthesis has conclusively succeeded and the
# circuit is loaded (resident) or conclusively failed (permanent fallback, R65).
_TERMINAL_TIERS = ("resident", "fallback_only")

# await_synthesis polling cadence.  A modest fixed interval: the seam is a test
# observation point built on the SAME public observation ``pyro.re.explain()``
# reports (the residency manager's tier), not a sync primitive — it must never
# make synthesis synchronous nor perturb other callers, so it only looks.
_AWAIT_POLL_INTERVAL_S = 0.02


def inject_device_error(kind: str = "device", count: int = 1) -> None:
    """Arm the next ``count`` model dispatches to raise a device error (R52/R67)."""
    if not _route.test_hooks_enabled():
        return
    _model.get_model().inject_device_error(kind, count)


def inject_synth_failure(pattern, flags: int = 0) -> None:
    """Arm ``pattern``'s next synthesis to fail (R65 permanent fallback) (R67)."""
    if not _route.test_hooks_enabled():
        return
    from .synth import residency as _res
    _res.get_manager().inject_synth_failure(pattern, flags)


def inject_false_positive(pattern, flags: int = 0, count: int = 1) -> None:
    """Arm the model to emit ``count`` spurious windows for ``pattern`` (R19/R67).

    The spurious windows fail host re-verification and never leak into results.
    """
    if not _route.test_hooks_enabled():
        return
    _model.get_model().inject_false_positive(pattern, flags, count)


def _observe_tier(pattern, flags: int) -> str:
    """The pattern's current ``circuit_status`` (R31) — the same public
    observation ``pyro.re.explain()`` reports.  Local import: not a hot path,
    and it keeps package init free of the re/synth cascade."""
    from . import re as _pyro_re
    return _pyro_re.explain(pattern, flags)["circuit_status"]


def await_synthesis(pattern, flags: int = 0, timeout: float = 30.0) -> str:
    """Block the calling test until ``pattern``'s circuit reaches a terminal
    tier (``"resident"`` / ``"fallback_only"``, R4/R31) or ``timeout`` seconds
    elapse; return the ``circuit_status`` actually reached (R67, v2.5.0).

    On timeout the LAST OBSERVED tier is returned (never a raise) — a test
    asserting ``== "resident"`` therefore fails honestly on a hung service.
    Poll-only over the public tier observation: it MUST NOT (and does not) make
    synthesis synchronous, block/slow/reorder any other caller's dispatch
    (R63/R2b asynchrony unchanged), or alter routing or results (R16/R53); safe
    to call concurrently (R32 — each call polls independently).  When the R67
    gate is off it returns the current tier immediately, without waiting.
    """
    tier = _observe_tier(pattern, flags)
    if not _route.test_hooks_enabled():
        return tier
    deadline = _time.monotonic() + float(timeout)
    while tier not in _TERMINAL_TIERS:
        remaining = deadline - _time.monotonic()
        if remaining <= 0.0:
            break
        _time.sleep(min(_AWAIT_POLL_INTERVAL_S, remaining))
        tier = _observe_tier(pattern, flags)
    return tier


def set_strict_residency(enabled: bool = True) -> None:
    """Suspend (or restore) the R51b device-free precedence (R51b-strict, R67
    v2.5.0).

    While enabled — and only while the R67 gate is on — R51 step 5 binds
    strictly, exactly as on hardware: a HW-eligible, gate-crossed dispatch whose
    circuit is not resident (cold / synthesizing / warm) is served by genuine
    fallback and counted as a normal ``fallback`` dispatch (never
    ``fallback_after_error``); a resident circuit dispatches to the software
    model standing in for it (R7), counted as ``model``.  Results are
    byte-identical in both modes (R16/R36/R53); residency/eviction bookkeeping
    is unchanged (R64/R64a).  Cleared by :func:`reset`.
    """
    if not _route.test_hooks_enabled():
        return
    _route.set_strict_residency(bool(enabled))


def reset() -> None:
    """Clear all injected faults and disable strict residency (R67, v2.5.0).

    Always safe — even when the gate is off (nothing is armed then).  Idempotent.
    """
    _model.get_model().clear_injections()
    _route.set_strict_residency(False)
    import sys
    _res = sys.modules.get("pyro.synth.residency")
    if _res is not None and getattr(_res, "_GLOBAL", None) is not None:
        _res._GLOBAL.clear_injections()
