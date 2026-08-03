"""Routing / fallback decision logic (R51), device-error fallback (R52),
determinism (R53), and diagnostics counters (``stats``).

The decision order is exactly R51.  The fallback fast path (R3a/R5, AC-0-6) is
kept deliberately thin: resolve the cached classification, read the *cached*
env flags (never the process environment — R35a), apply the size/reuse gate,
then delegate straight to the stdlib pattern with no wrapper allocation.
"""

from __future__ import annotations

import os
import sys as _sys
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


# Offload thresholds (R2/R3): reuse count and minimum corpus size.  Re-exported
# from the single source of truth (pyro/_thresholds.py); tests read
# ``_route.S_MIN`` / ``_route.N_REUSE`` and the native routing extension bakes
# in
# a header generated from that SAME module, so the two can never diverge.
from ._thresholds import S_MIN, N_REUSE   # noqa: F401  (re-export)

# The native routing extension, or ``None`` on a pure-Python build.  Selected in
# _match (which owns the PYRO_NO_NATIVE / ROUTE_ABI guards); mirrored here so
# the
# dispatch/counter/env code can pick the native or pure-Python implementation
# with a single ``is not None`` check.  Populated at the BOTTOM of this module
# (after the run_* functions it configures exist); ``None`` until then, which is
# why ``sample_env()`` at import is a no-op push and is re-run once wired.
_fast = None

# --- diagnostics counters (stats(), R52/AC-3-4) ---------------------------
_STATS_LOCK = threading.Lock()
_STATS = {
    "hardware": 0,             # dispatches served by real device (none in Ph0)
    "model": 0,                # dispatches served by the software model
    "fallback": 0,             # dispatches routed to CPython re
    # device error / verify failure -> fallback (R52)
    "fallback_after_error": 0,
    "device_errors": 0,
    "total": 0,
}


# Index of each single-path counter in the native extension's per-thread block
# (CNT_HW..CNT_DEVERR); the order is identical to _STATS' insertion order, which
# the native module asserts via its CNT_* module constants.  Used only by the
# cold-path _record shim when the extension is present.
_CNT_IDX = {
    "hardware": 0,
    "model": 1,
    "fallback": 2,
    "fallback_after_error": 3,
    "device_errors": 4,
}


def stats() -> dict:
    """Dispatch counters (R52/R66).

    When the native extension is present the per-thread counters it owns are the
    ONE source of truth (the Python ``_STATS`` dict is not used at all); it
    returns a dict with exactly the six ``_STATS`` keys in exactly that order.
    Otherwise the Python lock+dict counters are returned verbatim.
    """
    if _fast is not None:
        return _fast.stats()
    with _STATS_LOCK:
        return dict(_STATS)


def reset_stats() -> None:
    if _fast is not None:
        _fast.reset_stats()
        return
    with _STATS_LOCK:
        for k in _STATS:
            _STATS[k] = 0


def _record(path: str) -> None:
    # When native, funnel the cold Python paths (model dispatch, sub/subn/split)
    # into the SAME per-thread counters the C hot path bumps — two counter
    # stores
    # would make the aggregate wrong; there is exactly one.  The ~50 ns
    # Python->C
    # hop is on paths that already cost microseconds, never the measured path.
    if _fast is not None:
        _fast.count(_CNT_IDX[path])
        return
    with _STATS_LOCK:
        _STATS[path] += 1
        _STATS["total"] += 1


def _record_error_fallback() -> None:
    if _fast is not None:
        _fast.count_error()
        return
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
# fallback path rather than silently ignoring an operator's non-canonical
# value.
_UNSET_VALUES = (None, "", "0")
_ENV = (False, False)                 # (disabled, force_model) cached snapshot
_ENV_LOCK = threading.Lock()          # serializes samplers (R35d thread-safety)

# R67/R68 cached snapshots — sampled at the SAME R35a points as _ENV, never on
# the per-call hot path (R35a/R5).  _TEST_HOOKS gates the pyro.testing seam;
# _N_SYNTH is the validated PYRO_N_SYNTH launch-threshold override
# (None=default).
_TEST_HOOKS = False
_N_SYNTH = None

# R70 cached toolchain selection — sampled at the SAME R35a points, never per
# call.  _TOOLCHAIN is "mock" | "vivado" (any unrecognized PYRO_TOOLCHAIN value
# is treated as "mock", fail safe); _VIVADO_DIR is the raw PYRO_VIVADO install
# dir (no PATH/XILINX_VIVADO scanning in library code — R70).  This snapshot is
# refreshed at every R35a sampling point; pyro.synth.residency reads it when it
# first BUILDS the residency manager and pins the resulting ToolchainConfig for
# that manager's lifetime (the out-of-process worker is spawned with it).  A
# mid-process PYRO_TOOLCHAIN switch therefore reaches a *freshly built* manager
# (e.g. after reset_manager), not an already-running one — see get_manager's
# docstring for why the toolchain is pinned at construction rather than
# live-swapped (orphaned-subprocess safety).
_TOOLCHAIN = "mock"
_VIVADO_DIR = None

# R68 device knobs (v2.2.2; iface no-default rule v2.5.0) — sampled at the SAME
# R35a points, never per call.  PYRO_DEVICE_IFACE is the onic netdev the R86
# device transport binds — **no default, fail-closed** (F3/R68 v2.5.0): the
# netdev name is host configuration, not derivable from any spec fact, so an
# unset knob yields None and probe_device fails closed with the R83 canonical
# `transport: PYRO_DEVICE_IFACE not configured` reason (never a guess, never a
# scan — R70).  PYRO_HW_SERVER is the Vivado hw_server URL the R85/R86.5 JTAG
# loader connects to (default TCP:localhost:3121).  These are the ONLY two
# env-sampled device inputs (R68/R86): pyro.device.DeviceConfig reads this
# cached
# snapshot for its iface/hw_server field defaults — the device functions
# themselves never touch os.environ (R5/R35a).  Every other device parameter
# (SPEC16, R84 timeouts, R78 frame constants) is a spec-fixed constant.
_HW_SERVER_DEFAULT = "TCP:localhost:3121"
_DEVICE_IFACE = None
_HW_SERVER = _HW_SERVER_DEFAULT
# R68 P2d data-plane knob (v2.7.0) — PYRO_QDMA_CHARDEV names the QDMA ST
# char-dev (e.g. /dev/qdma02000-ST-0) carrying the same eth-framed R78 AXIS
# payloads as the netdev transport.  **No default, fail-closed** (same F3/R68
# no-default rule as the iface): the char-dev only exists while the operator
# has swapped the PF to qdma-pf (scripts/pyro_dataplane_swap.sh), so an unset
# knob yields None and the transport stays on the netdev path.
_QDMA_CHARDEV = None

# R68 PR-substrate knobs (v2.2.4) — sampled at the SAME R35a points, never per
# call.  PYRO_PR_STATIC_DCP -> ToolchainConfig.static_dcp (R82b locked static),
# PYRO_PR_REFERENCE_DCP -> reference_dcp (R82c/R82d pr_verify reference).  **No
# default, fail-loud** (R88): if a pr_bitstream job is requested and either is
# unset/unresolvable, synthesis is SynthesisFailed (never a silent ooc
# downgrade).
# Unset here => None; the residency service passes them through to the worker.
_PR_STATIC_DCP = None
_PR_REFERENCE_DCP = None
# R68 PR-evidence knob (v2.2.5) — PYRO_PR_EVIDENCE_MANIFEST ->
# ToolchainConfig.pr_evidence_manifest: a Manifest JSON the operator asserts
# this host's PR flow produced against the configured substrate (R82d proxy for
# the passing-pr_verify conjunct).  No default; gates the R83a availability
# report only, never a job (absence => pr_flow_present stays false, not
# SynthesisFailed).
_PR_EVIDENCE_MANIFEST = None


def _parse_n_synth(raw):
    """Validate a PYRO_N_SYNTH value: a positive int, else ``None``
    (default)."""
    if raw is None or raw == "":
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def sample_env() -> None:
    """Re-sample the PYRO_* env knobs from ``os.environ`` (R35a/R67/R68).

    Called only at sampling points (import, install/uninstall, refresh_env).
    Thread-safe and idempotent; publishes a new snapshot with a single atomic
    rebind so in-flight decisions are unaffected (R35d).
    """
    global _ENV, _TEST_HOOKS, _N_SYNTH, _TOOLCHAIN, _VIVADO_DIR
    global _DEVICE_IFACE, _HW_SERVER, _PR_STATIC_DCP, _PR_REFERENCE_DCP
    global _PR_EVIDENCE_MANIFEST, _QDMA_CHARDEV
    with _ENV_LOCK:
        disabled = os.environ.get("PYRO_DISABLE") not in _UNSET_VALUES
        force = os.environ.get("PYRO_FORCE_MODEL") not in _UNSET_VALUES
        _ENV = (disabled, force)
        _TEST_HOOKS = os.environ.get(
            "PYRO_ENABLE_TEST_HOOKS") not in _UNSET_VALUES
        _N_SYNTH = _parse_n_synth(os.environ.get("PYRO_N_SYNTH"))
        # R70: toolchain selection.  Unrecognized PYRO_TOOLCHAIN => "mock" (fail
        # safe: never silently attempt a real flow the operator did not name).
        _tc = os.environ.get("PYRO_TOOLCHAIN")
        _TOOLCHAIN = "vivado" if _tc == "vivado" else "mock"
        _vd = os.environ.get("PYRO_VIVADO")
        _VIVADO_DIR = _vd if _vd else None
        # R68 device knobs (v2.2.2): the only two env-sampled device inputs.  An
        # unset/empty PYRO_DEVICE_IFACE has NO default (None — fail-closed,
        # F3/R68 v2.5.0); an unset PYRO_HW_SERVER keeps the stock spec default.
        _di = os.environ.get("PYRO_DEVICE_IFACE")
        _DEVICE_IFACE = _di if _di else None
        # R68 P2d char-dev knob (v2.7.0): no default, fail-closed.
        _qc = os.environ.get("PYRO_QDMA_CHARDEV")
        _QDMA_CHARDEV = _qc if _qc else None
        _hs = os.environ.get("PYRO_HW_SERVER")
        _HW_SERVER = _hs if _hs else _HW_SERVER_DEFAULT
        # R68 PR-substrate knobs (v2.2.4): no default (fail-loud is enforced
        # by the
        # toolchain when a pr_bitstream job needs an absent path, R88).
        _psd = os.environ.get("PYRO_PR_STATIC_DCP")
        _PR_STATIC_DCP = _psd if _psd else None
        _prd = os.environ.get("PYRO_PR_REFERENCE_DCP")
        _PR_REFERENCE_DCP = _prd if _prd else None
        # R68 PR-evidence knob (v2.2.5): no default; availability-report only.
        _pem = os.environ.get("PYRO_PR_EVIDENCE_MANIFEST")
        _PR_EVIDENCE_MANIFEST = _pem if _pem else None
    # Push the freshly-sampled launch threshold onto the live residency manager
    # (R68).  Done outside the _ENV_LOCK and only if the synth subsystem is
    # already imported, so package init / the fallback hot path never pull it
    # in.
    _mod = _sys.modules.get("pyro.synth.residency")
    if _mod is not None:
        try:
            _mod.apply_n_synth()
        except Exception:
            pass
    # Push the freshly-sampled (disabled, force) pair onto the native router's
    # single atomic env word (R35d): one _Atomic uint32 store, so an in-flight
    # native call can never split across the old/new values — the same argument
    # as the one-tuple rebind of _ENV above.  No-op on a pure-Python build.
    if _fast is not None:
        _fast.set_env(disabled, force)


def test_hooks_enabled() -> bool:
    """Cached PYRO_ENABLE_TEST_HOOKS gate for the pyro.testing seam (R67)."""
    return _TEST_HOOKS


def n_synth_override():
    """Cached PYRO_N_SYNTH launch-threshold override, or ``None`` (R68)."""
    return _N_SYNTH


def toolchain_selection():
    """Cached (kind, vivado_dir) toolchain selection (R70).

    ``kind`` is ``"mock"`` or ``"vivado"`` (unrecognized => ``"mock"``);
    ``vivado_dir`` is the raw PYRO_VIVADO install dir or ``None``.  Sampled only
    at R35a points; read by :mod:`pyro.synth.residency` when it builds the
    residency manager's ToolchainConfig (never on the per-call hot path)."""
    return (_TOOLCHAIN, _VIVADO_DIR)


def device_iface():
    """Cached PYRO_DEVICE_IFACE (R68/v2.2.2; no-default rule v2.5.0), the onic
    netdev for the device transport — ``None`` when unset (**no spec default;
    fail-closed**, F3/R68: the name is host configuration, and probe_device then
    returns the R83 canonical ``transport: PYRO_DEVICE_IFACE not configured``
    reason instead of guessing or scanning).  Sampled only at R35a points;
    read by
    :mod:`pyro.device`'s ``DeviceConfig`` for its ``iface`` default — never on
    the
    per-call hot path, never via os.environ inside the device functions
    (R86)."""
    return _DEVICE_IFACE


def qdma_chardev():
    """Cached PYRO_QDMA_CHARDEV (R68, P2d v2.7.0), the QDMA ST char-dev
    path for the performance transport — ``None`` when unset (**no spec
    default; fail-closed**: the char-dev exists only while the operator has
    swapped the PF to qdma-pf, so nothing is guessed and nothing is scanned).
    Sampled only at R35a points; read by :mod:`pyro.device`'s ``DeviceConfig``
    for its ``chardev`` default."""
    return _QDMA_CHARDEV


def hw_server_url():
    """Cached PYRO_HW_SERVER (R68/v2.2.2), the Vivado hw_server URL for the R85
    JTAG loader (default ``TCP:localhost:3121``).  Sampled only at R35a points;
    read by :mod:`pyro.device`'s ``DeviceConfig`` for its ``hw_server``
    default."""
    return _HW_SERVER


def pr_substrate_dcps():
    """Cached (static_dcp, reference_dcp) PR substrate paths (R68/R88, v2.2.4).

    From PYRO_PR_STATIC_DCP / PYRO_PR_REFERENCE_DCP; ``None`` when unset (no
    default — fail-loud is enforced by the toolchain when a pr_bitstream job
    needs
    an absent path, R88).  Sampled only at R35a points; read by
    :mod:`pyro.synth.residency` when it builds the worker's ToolchainConfig."""
    return (_PR_STATIC_DCP, _PR_REFERENCE_DCP)


def pr_evidence_manifest():
    """Cached PYRO_PR_EVIDENCE_MANIFEST path (R68/R83a, v2.2.5); ``None`` when
    unset.  Evidence for the R83a pr_flow_present predicate only — never
    consumed by a synthesis job.  Sampled only at R35a points."""
    return _PR_EVIDENCE_MANIFEST


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


def _decide_hot(patt: PyroPattern, string, pos, endpos) -> str:
    """The native-equivalent routing decision: steps S0..S6, no UTF-8 probe.

    This is the EXACT contract the C ``pyro_route_decide`` reproduces
    bit-for-bit
    (the 288-point equivalence test enumerates the full domain against a Python
    transcription of it).  The O(n) UTF-8 transportability probe
    (R51a(d)/R14a) is
    deliberately NOT here — it is model-bound-only and lives in
    :func:`_serve_model_single`/:func:`_serve_model_finditer`, so the short
    fallback fast path never pays for it.

    Consults only the cached env snapshot (R35a); no os.environ access here.
    Updates the per-pattern reuse counter as its FIRST action (S0), before every
    gate including PYRO_DISABLE — the side effect is observable even for a
    disabled or fallback-only pattern (AC-1-6/R51).
    """
    reuse = patt._calls
    patt._calls = reuse + 1                         # S0 (side effect first)

    disabled, force = _ENV                          # atomic snapshot (R35d)
    if disabled:                                    # S1  R51.1 (cached flag)
        return "fallback"
    if not patt._classification.eligible:           # S2  R51.2
        return "fallback"
    # S3  C2: subclasses of str/bytes and bytearray/memoryview would make
    # group(0) (subject[s:e]) a different / unhashable / mutable type than stock
    # re's bytes result -- route them to the genuine re objects (Phase 0).
    tstr = type(string)
    if tstr is not str and tstr is not bytes:       # gate (v1.2.1)
        return "fallback"
    if not _is_full_span(string, pos, endpos):      # S4  anchor-context safety
        return "fallback"
    if not force and len(string) < S_MIN and reuse < N_REUSE:
        # S5  R51.4 loss regime (R3a)
        return "fallback"
    # S6  R51.6 (no device in Ph0)
    return "model"


def _decide(patt: PyroPattern, string, pos, endpos) -> str:
    """Return 'model' or 'fallback' per the full R51 decision (S0..S6 + the
    UTF-8 gate).  Kept for the aggregate ops (findall/sub/subn/split), which run
    it purely for the S0 reuse side effect and discard the verdict.

    Semantics are identical to the pre-refactor ``_decide``: a 'model' verdict
    that fails the UTF-8 probe becomes 'fallback' exactly as before.
    """
    verdict = _decide_hot(patt, string, pos, endpos)
    # C1: only strict-UTF-8-encodable str subjects can be transported to the
    # model; a lone surrogate (which stock re still matches) must fall back.
    if verdict == "model" and type(
        string) is str and not _utf8_transportable(string):
        return "fallback"
    return verdict


def _utf8_transportable(s: str) -> bool:
    try:
        s.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


# --- residency-aware routing (R51 step 5 amended, R4a/R65) -----------------
# The residency subsystem (pyro.synth) is imported lazily and cached so the
# per-call *fallback* fast path (AC-0-6) never touches it — only the model path
# (large/reused/forced, not the perf-measured path) consults it.  Because on
# this
# host there is no device (F5, R7), the software model stands in for the
# resident
# tier, so this consultation drives the launch policy (R4a) and lifecycle stats
# (R66) as a side effect and forces fallback only for a *permanently-fallback*
# pattern (R65).  It never raises into a caller (R52/R65).
_residency = None

# --- R51b-strict (R67 set_strict_residency seam, v2.5.0) -------------------
# While True AND the R67 test-hook gate is on, the R51b device-free precedence
# of R7 is SUSPENDED and R51 step 5 binds strictly, exactly as on hardware: a
# HW-eligible, gate-crossed dispatch whose circuit is NOT resident (cold /
# synthesizing / warm) is served by genuine fallback for that call (counted as a
# normal ``fallback`` dispatch, never ``fallback_after_error``), while a
# resident circuit dispatches to the software model standing in for it (R7,
# counted as ``model``).  Results are byte-identical in both modes (R16/R36/
# R53) — only the R66 dispatch counters and the latency differ — and the
# residency/eviction bookkeeping (R64/R64a) is unchanged: the consultation
# below still ticks the launch policy and lifecycle either way.  Set only via
# ``pyro.testing.set_strict_residency`` (gated), cleared by
# ``pyro.testing.reset``.  A single module-global bool read once per consult
# (atomic under the GIL) — concurrency-safe the same way _ENV is (R35d).
_STRICT_RESIDENCY = False


def set_strict_residency(enabled: bool = True) -> None:
    """Enable/disable R51b-strict routing (R67 seam plumbing, v2.5.0).

    Callers go through ``pyro.testing.set_strict_residency``, which owns the
    PYRO_ENABLE_TEST_HOOKS gate; this setter is the ungated state holder (the
    dispatch-path check below ALSO requires the gate, so a stale True can never
    bind once hooks are off).
    """
    global _STRICT_RESIDENCY
    _STRICT_RESIDENCY = bool(enabled)


def strict_residency_active() -> bool:
    """True iff R51b-strict binds: seam enabled AND the R67 gate is on."""
    return _STRICT_RESIDENCY and _TEST_HOOKS

# --- residency-consultation diagnostics (AC-3-1 counter-wedge review) ------
# _consult_residency below MUST NOT raise into a dispatch (R52/R65), so it
# swallows every exception.  But a swallowed failure here can mask a dead R4a
# launch policy for the whole process (the "counter wedge": ``_residency``
# pinned to a partially-initialized pyro.synth.residency module whose import
# attempt died, so every consult raises AttributeError).  Narrowing the except
# is NOT safe — the wedge's own failure classes (ImportError, AttributeError)
# are exactly the ones that must not escape into user regex calls — so the
# swallowed failure is recorded observably instead.  Diagnostics only: not part
# of the R52/R66 stats() shape, never patched onto the stdlib ``re`` namespace,
# written only on the (exceptional) failure path.
_RESIDENCY_CONSULT_FAILURES = 0
_LAST_RESIDENCY_CONSULT_ERROR = None


def _note_residency_failure(exc: BaseException) -> None:
    global _RESIDENCY_CONSULT_FAILURES, _LAST_RESIDENCY_CONSULT_ERROR
    _RESIDENCY_CONSULT_FAILURES += 1
    _LAST_RESIDENCY_CONSULT_ERROR = "%s: %s" % (type(exc).__name__, exc)


def _consult_residency(patt, string) -> bool:
    """Register a HW-eligible dispatch and return ``True`` to force fallback.

    Returns ``True`` when the pattern's circuit is **permanently fallback-only**
    (synthesis failed, R65) — ordinary routing, counted as a normal ``fallback``
    (not ``fallback_after_error``, AC-1-6) — and, while **R51b-strict** is
    active (R67 ``set_strict_residency`` seam, v2.5.0), for ANY not-resident
    tier (cold / synthesizing / warm): R51 step 5 then binds exactly as on
    hardware.  Otherwise ticks the launch policy / lifecycle and returns
    ``False`` (proceed via the model).  The bookkeeping side effects
    (``note_eligible_dispatch``: launch counter, R4a policy, warm→resident
    promotion, R66 counters) are identical in both modes.
    """
    global _residency
    try:
        res = _residency
        if res is None:
            from .synth import residency as res  # cached in sys.modules
            # Re-entrancy guard (AC-3-1 counter wedge): when the FIRST import
            # of pyro.synth is initiated elsewhere (explain()/stats()/prewarm)
            # while pyro.install() is active, stdlib modules first imported by
            # that lazy cascade (dataclasses, pickle) run module-level code
            # through the *patched* ``re`` and re-enter this consult
            # MID-IMPORT.  The nested import statement above can then yield a
            # partially initialized ``residency`` module whose own import
            # attempt subsequently fails, so importlib evicts it from
            # sys.modules — caching that object would pin a permanent corpse
            # (no ``get_manager``) and silently kill the R4a launch policy for
            # the whole process.  ``get_manager`` is defined at the very end
            # of residency.py, so its presence proves the module body
            # completed; cache only then.  An incomplete module still serves
            # this one consult below (raising AttributeError into the
            # swallow/record path) but is never cached, and the next consult
            # after the outer import completes picks up the healthy module.
            if hasattr(res, "get_manager"):
                _residency = res
        enc = ENC_UTF8 if isinstance(string, str) else ENC_BYTES
        outcome = res.get_manager().note_eligible_dispatch(
            patt._stock.pattern, patt._stock.flags, enc)
        if outcome == res.ROUTE_PERMANENT_FALLBACK:
            return True
        # R51b-strict (v2.5.0): while the R67 seam is active (and hooks gated
        # on), a not-resident circuit routes to genuine fallback for this call —
        # the production R51 step 5 rule — instead of the R51b model standin.
        if _STRICT_RESIDENCY and _TEST_HOOKS and outcome != res.ROUTE_RESIDENT:
            return True
        return False
    except Exception as exc:
        _note_residency_failure(exc)
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
    # finditer -- both fine for a software model but worth streaming/caching
    # once
    # a real device engine lands.
    prefix = utf8_prefix(string) if isinstance(string, str) else None
    out = []
    for w in windows:
        span0 = _win_to_cp(string, w, prefix)
        _verify_window(patt, string, w, span0)
        out.append(
    HybridMatch(
        patt,
        string,
        span0,
        pos,
        endpos_eff,
         "finditer"))
    return out


# --- post-decision service (the single shared model/cold path) ------------
# Both the pure-Python router (run_single/run_finditer below) and the native
# router (pyro._fast.Pattern) reach the model/cold path through THESE functions
# and no other, so the model/error/UTF-8 semantics exist exactly once.  Neither
# touches ``patt._calls`` — the S0 side effect already happened in the caller's
# _decide_hot (Python) or in C (native).
def _serve_model_single(patt, op, string, pos, endpos):
    """Serve a 'model' verdict for search/match/fullmatch (R51.6/R52).

    Order matters (contract): the model-bound UTF-8 gate first (R51a(d)/R14a),
    then the residency consultation (R65), then the model with device-error
    (R52) and unsupported-pattern fallbacks.
    """
    if type(string) is str and not _utf8_transportable(string):
        _record("fallback")
        return _stock_op(patt, op, string, pos, endpos)
    if _consult_residency(
    patt, string):       # R65 permanent fallback -> routing
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


def _serve_model_finditer(patt, string, pos, endpos):
    """Serve a 'model' verdict for finditer (mirrors _serve_model_single)."""
    if type(string) is str and not _utf8_transportable(string):
        _record("fallback")
        return _stock_op(patt, "finditer", string, pos, endpos)
    if _consult_residency(
    patt, string):       # R65 permanent fallback -> routing
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


# --- public dispatch entry points -----------------------------------------
# The native router calls _decide_hot in C and _serve_model_* directly; these
# Python entry points are the pure-Python router AND the bail-out target the
# native router hands exotic argument shapes to (weird pos/endpos types, kwargs)
# so their rich-compare semantics are reproduced by construction, not re-coded.
def run_single(patt, op, string, pos=0, endpos=None):
    """search / match / fullmatch (R51/R52)."""
    if _decide_hot(patt, string, pos, endpos) == "fallback":
        _record("fallback")
        return _stock_op(patt, op, string, pos, endpos)
    return _serve_model_single(patt, op, string, pos, endpos)


def run_finditer(patt, string, pos=0, endpos=None):
    if _decide_hot(patt, string, pos, endpos) == "fallback":
        _record("fallback")
        return _stock_op(patt, "finditer", string, pos, endpos)
    return _serve_model_finditer(patt, string, pos, endpos)


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


# --- native routing wiring (R3c) ------------------------------------------
# The module DAG is _fast (leaf C module) <- _match <- _route.  _match owns the
# PYRO_NO_NATIVE / ROUTE_ABI selection; we mirror its choice and, when the
# extension is active, hand it strong references to the SAME post-decision and
# bail-out functions the pure-Python router uses.  After configuring, re-run
# sample_env() so the env word is pushed now that set_env is wired (the import
# sample above ran with _fast still None).
# noqa: E402  (bottom import breaks the cycle)
from . import _match as _match_mod

_fast = _match_mod._fast
if _fast is not None:
    _fast.configure(
        _serve_model_single,
        _serve_model_finditer,
        run_single,
        run_finditer,
        run_findall,
        run_sub,
        run_subn,
        run_split,
    )
    sample_env()
