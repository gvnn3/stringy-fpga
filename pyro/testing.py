"""Public fault-injection seam — ``pyro.testing`` (spec §9.1 R67).

R67 requires a public, spec-only-author-drivable test-hook namespace so that the
AC-1-6 fault-injection behaviors (R52 device-error retry, R65 synthesis-failure
permanent fallback, R19 false-positive re-verification) can be exercised WITHOUT
reading or patching internals.  The seams here:

  * ``inject_device_error(kind="device"|"timeout", count=1)`` — the next ``count``
    model/hardware dispatches raise the corresponding device error, driving the
    R52 fallback-retry path and its ``fallback_after_error`` counter (R66).
  * ``inject_synth_failure(pattern, flags=0)`` — the named pattern's next
    synthesis fails (R65): permanent fallback, diagnostic, ``synth_failed`` (R66).
  * ``inject_false_positive(pattern, flags=0, count=1)`` — the model emits
    ``count`` spurious candidate windows for the pattern, re-verified away by the
    host (R19) so they never leak into results.
  * ``reset()`` — clear all injected faults.

Guard (R67).  To avoid production foot-guns, injection takes effect ONLY when
``PYRO_ENABLE_TEST_HOOKS=1`` was sampled at an R35a sampling point (import,
``install()``/``uninstall()``, ``refresh_env()``).  The gate is read from the
cached env snapshot (``pyro._route.test_hooks_enabled``) — never the live
environment on any hot path.  When the gate is off the functions are importable
no-ops that raise nothing; injection is deterministic and never alters
caller-visible results relative to CPython ``re`` (R16 — the fallback nets catch
every injected fault).

This namespace is PYRO-specific and is NOT patched onto the standard ``re``
module by :func:`pyro.install` (§7.2), mirroring ``explain``/``stats``/``prewarm``.
"""

from __future__ import annotations

from . import _route
from . import _model

__all__ = [
    "inject_device_error",
    "inject_synth_failure",
    "inject_false_positive",
    "reset",
]


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


def reset() -> None:
    """Clear all injected faults (R67).

    Always safe — even when the gate is off (nothing is armed then).  Idempotent.
    """
    _model.get_model().clear_injections()
    import sys
    _res = sys.modules.get("pyro.synth.residency")
    if _res is not None and getattr(_res, "_GLOBAL", None) is not None:
        _res._GLOBAL.clear_injections()
