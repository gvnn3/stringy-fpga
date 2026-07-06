"""Circuit residency, launch policy, and PR-region arbitration (spec §7.5).

This is the L3-side coordinator that ties the bitstream cache (R4), the
out-of-process synthesis service (R63), the synthesis-launch policy (R4a), and
the single-tenant PR-region eviction policy (R64) together, and exposes the
circuit-lifecycle counters (R66).

Responsibilities:

  * **Launch policy (R4a/R62).** A key's synthesis is launched only when it has
    been dispatched HW-eligible at least ``N_synth`` times (default **1000**) or
    was explicitly ``prewarm``ed.  Launching is deduplicated (R63a) and never
    blocks the triggering call (R63/R51).
  * **Tier tracking (R4/R31).** cold -> synthesizing -> warm -> resident, plus
    permanently-fallback (R65).  ``tier()`` reports it; ``explain()`` surfaces it.
  * **PR-region arbitration (R64).** One resident circuit at a time
    (single-tenant).  A warm artifact is promoted to resident with a mock PR
    load; loading a different circuit evicts the least-recently-dispatched
    resident (deterministic, progress-guaranteeing, no thrash).
  * **Stats (R66).** ``synth_launched/succeeded/failed`` (monotonic),
    ``circuits_synthesizing``/``circuits_resident`` (gauges),
    ``circuits_evicted`` and ``pr_loads`` (monotonic).

Design note (R7 vs R51 step 5, for the spec-writer).  On this host there is no
device (F5), so per R7 the **software model stands in for the resident tier**:
the router serves HW-eligible, gate-crossed work via the model regardless of this
manager's tier.  This manager therefore models the *device* lifecycle — it drives
the launch policy, tier bookkeeping, eviction, and R66 counters, and it forces
fallback only for a **permanently-fallback** key (R65).  The genuine
NOT_RESIDENT-as-routing and tier-equivalence behavior (AC-1-6/AC-1-7) is exact in
this manager and is exercised directly by the lifecycle tests; it is not observed
through the model-as-resident router path.  See the Task-6 report §13 note.
"""

from __future__ import annotations

import atexit
import threading
from collections import OrderedDict
from typing import Dict, Optional, Set, Tuple

from .. import hdl
from ..hdl import identity as _identity
from . import artifact as _artifact
from .cache import BitstreamCache, BitstreamKey, make_key, key_digest
from .service import SynthesisService, STATUS_OK, STATUS_FAILED
from .toolchain import (
    TOOLCHAIN_VERSION, VIVADO_TOOLCHAIN_VERSION, SHELL_VERSION, SynthJob,
    ToolchainConfig,
)

# Launch-policy threshold (R4a): dispatch count at/above which a hot pattern's
# synthesis is launched.  Tunable; the spec default is 1000.
N_SYNTH_DEFAULT = 1000

# Lifecycle tiers (mirror pyro_circ_status / R31 circuit_status strings).
TIER_COLD = "cold"
TIER_SYNTHESIZING = "synthesizing"
TIER_WARM = "warm"
TIER_RESIDENT = "resident"
TIER_FALLBACK_ONLY = "fallback_only"

# Router decision outcomes (R51 step 5 amended).  NOT_RESIDENT/SYNTH are ordinary
# routing (AC-1-6): a non-permanent, non-resident key routes to fallback WITHOUT
# being a device error.  PERMANENT_FALLBACK is R65.
ROUTE_RESIDENT = "resident"
ROUTE_NOT_RESIDENT = "not_resident"
ROUTE_SYNTH = "synthesizing"
ROUTE_PERMANENT_FALLBACK = "permanent_fallback"


class _ResidentInfo:
    __slots__ = ("key", "manifest")

    def __init__(self, key, manifest):
        self.key = key
        self.manifest = manifest


class ResidencyManager:
    """Coordinates the bitstream cache, synthesis service, launch + eviction."""

    def __init__(self, cache: Optional[BitstreamCache] = None,
                 toolchain_config: ToolchainConfig = ToolchainConfig(),
                 n_synth: int = N_SYNTH_DEFAULT, pr_partitions: int = 1,
                 timeout: float = 30.0):
        self._cache = cache if cache is not None else BitstreamCache()
        self._toolchain_config = toolchain_config
        self._n_synth = int(n_synth)
        self._pr_partitions = max(1, int(pr_partitions))
        self._timeout = float(timeout)

        self._lock = threading.RLock()
        self._service: Optional[SynthesisService] = None
        self._counts: Dict[str, int] = {}          # digest -> eligible-dispatch count
        self._prewarmed: Set[str] = set()          # digest set
        self._synthesizing: Set[str] = set()        # digest set (in flight)
        # digest -> _ResidentInfo, ordered least->most recently dispatched (LRU).
        self._resident: "OrderedDict[str, _ResidentInfo]" = OrderedDict()
        # R67: digests whose next synthesis is armed to fail (via pyro.testing).
        self._inject_synth_fail: Set[str] = set()
        # In-memory memo of the *persistent* per-key verdict ("cold"/"warm"/
        # "failed"), so the hot reused-pattern dispatch path (R51 step 5) does not
        # re-stat the filesystem every call.  Populated on the first fs read for a
        # key and invalidated on the state transitions this manager controls under
        # the RLock (synthesis completion, generator-gap failure, timeout reap).
        self._verdict: Dict[str, str] = {}

        self._stats = {
            "synth_launched": 0,
            "synth_succeeded": 0,
            "synth_failed": 0,
            "circuits_synthesizing": 0,   # gauge
            "circuits_resident": 0,        # gauge
            "circuits_evicted": 0,
            "pr_loads": 0,
        }

    # -- key construction (canonical, R4b) ---------------------------------
    def _toolchain_version(self) -> int:
        """The R4/R47b toolchain_version for the configured kind (R75/R75a).

        mock => 0x00000100, vivado => 0x17010000 (pinned 2023.1).  Because this
        is a component of the R4 bitstream-cache key, a mock artifact and a
        vivado artifact for the same pattern occupy **distinct keys** and never
        collide (R75a) — switching PYRO_TOOLCHAIN never serves a mock stub where
        real metrics are expected, or vice versa."""
        if getattr(self._toolchain_config, "kind", "mock") == "vivado":
            return VIVADO_TOOLCHAIN_VERSION
        return TOOLCHAIN_VERSION

    def bitstream_key(self, pattern, flags: int, enc: Optional[int] = None
                      ) -> BitstreamKey:
        dk = _identity.descriptor_key(pattern, flags, hdl.GENERATOR_VERSION)
        return make_key(dk, self._toolchain_version(), SHELL_VERSION)

    # -- service lifecycle -------------------------------------------------
    def _ensure_service(self) -> SynthesisService:
        if self._service is None:
            self._service = SynthesisService(
                self._cache, self._toolchain_config, workers=1,
                timeout=self._timeout)
            self._service.set_done_callback(self._on_synth_done)
        return self._service

    def _on_synth_done(self, key: BitstreamKey, status: str, reason: str) -> None:
        dig = key_digest(key)
        with self._lock:
            self._synthesizing.discard(dig)
            if status == STATUS_OK:
                self._stats["synth_succeeded"] += 1
                self._verdict[dig] = "warm"    # worker wrote the artifact (R63d)
            elif status == STATUS_FAILED:
                self._stats["synth_failed"] += 1
                self._verdict[dig] = "failed"  # negative cache entry (R65)
            self._stats["circuits_synthesizing"] = len(self._synthesizing)

    def poll(self) -> None:
        """Drain synthesis completions (non-blocking)."""
        if self._service is not None:
            self._service.poll()

    def drain(self, timeout: float = 10.0) -> bool:
        """Await all in-flight synthesis (test helper; API never blocks)."""
        if self._service is None:
            return True
        ok = self._service.drain(timeout)
        return ok

    # -- launch policy (R4a/R62/R63a) --------------------------------------
    def _launch(self, pattern, flags: int, enc: Optional[int],
                key: BitstreamKey, dig: str) -> None:
        """Generate + enqueue synthesis for a key (deduplicated).  Never raises
        to the caller (R63e/R65): a generator/service error is contained."""
        if self._cache.has(key) or self._cache.is_failed(key):
            return
        if dig in self._synthesizing:
            return
        # R67 injected synthesis failure: this launch fails deterministically —
        # the pattern becomes permanent fallback-only (R65), recorded in the
        # negative cache + synth_failed counter, with no worker/exception.
        if dig in self._inject_synth_fail:
            self._inject_synth_fail.discard(dig)
            self._cache.put_failure(key, "injected synthesis failure (R67)")
            self._stats["synth_launched"] += 1
            self._stats["synth_failed"] += 1
            self._verdict[dig] = "failed"
            return
        try:
            circuit = hdl.generate(pattern, flags, enc)
            job = _job_from_circuit(circuit)
        except Exception:
            # A lowering/estimation gap: treat as permanent fallback (R65) rather
            # than raising into the caller.
            self._cache.put_failure(key, "generator could not lower pattern")
            self._stats["synth_failed"] += 1
            self._verdict[dig] = "failed"
            return
        service = self._ensure_service()
        if service.submit(key, job):
            self._synthesizing.add(dig)
            self._stats["synth_launched"] += 1
            self._stats["circuits_synthesizing"] = len(self._synthesizing)

    def prewarm(self, pattern, flags: int = 0, enc: Optional[int] = None) -> bool:
        """R62: request synthesis of one HW-eligible pattern regardless of count.

        Returns ``True`` if a launch was (or already is) in progress for the key;
        ``False`` if the pattern is fallback-only or already cached/failed.
        Never blocks and never raises (R62/R65).
        """
        try:
            est = hdl.estimate(pattern, flags, enc)
        except Exception:
            return False
        if not est.eligible:
            return False
        with self._lock:
            key = self.bitstream_key(pattern, flags, enc)
            dig = key_digest(key)
            self._prewarmed.add(dig)
            if self._cache.is_failed(key):
                return False
            if self._cache.has(key) or dig in self._synthesizing:
                return True
            self._launch(pattern, flags, enc, key, dig)
            return dig in self._synthesizing or self._cache.has(key)

    # -- R67 injected synthesis failure (via pyro.testing) -----------------
    def inject_synth_failure(self, pattern, flags: int = 0,
                             enc: Optional[int] = None) -> None:
        """Arm the pattern's next synthesis to fail (R65 permanent fallback)."""
        try:
            key = self.bitstream_key(pattern, flags, enc)
        except Exception:
            return
        with self._lock:
            self._inject_synth_fail.add(key_digest(key))

    def clear_injections(self) -> None:
        """Clear armed synthesis-failure injections (R67 ``pyro.testing.reset``)."""
        with self._lock:
            self._inject_synth_fail.clear()

    # -- PR-region arbitration (R64) ---------------------------------------
    def _promote_to_resident(self, key: BitstreamKey, dig: str) -> bool:
        """Mock PR load of a warm artifact -> resident, verifying the manifest
        (R47b) and evicting the LRU resident if the single-tenant region is full
        (R64).  Returns ``True`` on success."""
        entry = self._cache.get(key)
        if entry is None:
            return False
        manifest = entry.manifest
        # R72b (loader honesty): a genuine on-*device* PR load requires
        # manifest.is_device_loadable() (payload_kind == "pr_bitstream").  On
        # this device-free host no such artifact exists ("mock_stub"/"ooc_metrics"
        # are model-exec containers, not device bitstreams), so what follows is
        # the *model-resident standin* for the device lifecycle (see the module
        # design note, R7/R51b): the software model executes the PYROART1
        # container regardless of payload_kind, and tier/residency bookkeeping is
        # unchanged.  The on-device residency clauses SKIP until pr_flow_present.
        # R47b: refuse a load whose shell/PR-region + harness are incompatible, or
        # whose bitstream integrity hash does not match (treated as fallback, not
        # a device error).
        if not manifest.compatible_with(SHELL_VERSION, hdl.HARNESS_VERSION):
            return False
        try:
            if not manifest.integrity_ok(entry.read_payload()):
                return False
        except OSError:
            return False
        # Evict LRU resident(s) to make room (single-tenant: pr_partitions == 1).
        while len(self._resident) >= self._pr_partitions and dig not in self._resident:
            _ev_dig, _ev = self._resident.popitem(last=False)
            self._stats["circuits_evicted"] += 1
        self._resident[dig] = _ResidentInfo(key, manifest)
        self._resident.move_to_end(dig)
        self._stats["pr_loads"] += 1
        self._stats["circuits_resident"] = len(self._resident)
        return True

    # -- tier query (R4/R31) -----------------------------------------------
    def tier(self, pattern, flags: int = 0, enc: Optional[int] = None) -> str:
        with self._lock:
            key = self.bitstream_key(pattern, flags, enc)
            dig = key_digest(key)
            self.poll()
            if self._cache.is_failed(key):
                return TIER_FALLBACK_ONLY
            if dig in self._resident:
                return TIER_RESIDENT
            if self._cache.has(key):
                return TIER_WARM
            if dig in self._synthesizing:
                return TIER_SYNTHESIZING
            return TIER_COLD

    def _fs_verdict(self, key: BitstreamKey, dig: str) -> str:
        """Memoized persistent verdict for a key: ``"cold"``/``"warm"``/``"failed"``.

        Hits the filesystem (``is_failed`` + ``has``) only on the first lookup of
        a key; thereafter the in-memory memo is authoritative, kept current by the
        state transitions this manager owns under the RLock (synthesis completion
        -> warm/failed, generator-gap/timeout -> failed).  Keeps the hot
        reused-pattern dispatch path off the filesystem while staying behaviorally
        identical to reading the cache each call.
        """
        v = self._verdict.get(dig)
        if v is not None:
            return v
        if self._cache.is_failed(key):
            v = "failed"
        elif self._cache.has(key):
            v = "warm"
        else:
            v = "cold"
        self._verdict[dig] = v
        return v

    # -- the router-facing step (R51 step 5, amended) ----------------------
    def note_eligible_dispatch(self, pattern, flags: int = 0,
                               enc: Optional[int] = None) -> str:
        """Register one HW-eligible dispatch and return the routing outcome.

        Ticks the launch counter, evaluates the launch policy (R4a), advances the
        lifecycle (drains completions, promotes a warm artifact to resident), and
        returns one of the ``ROUTE_*`` outcomes.  Never raises (R65): the caller
        continues on fallback/model regardless.
        """
        with self._lock:
            try:
                key = self.bitstream_key(pattern, flags, enc)
            except Exception:
                return ROUTE_NOT_RESIDENT
            dig = key_digest(key)
            self.poll()

            verdict = self._fs_verdict(key, dig)   # memoized (no per-call stat)
            if verdict == "failed":
                return ROUTE_PERMANENT_FALLBACK

            self._counts[dig] = self._counts.get(dig, 0) + 1
            count = self._counts[dig]

            # Already resident: update LRU recency and dispatch (R64).
            if dig in self._resident:
                self._resident.move_to_end(dig)
                return ROUTE_RESIDENT

            # Warm artifact present: promote to resident (mock PR load, R4/R64).
            if verdict == "warm":
                if self._promote_to_resident(key, dig):
                    return ROUTE_RESIDENT
                return ROUTE_NOT_RESIDENT

            # In flight: synthesizing (R63) -> ordinary routing (AC-1-6).
            if dig in self._synthesizing:
                return ROUTE_SYNTH

            # Cold: evaluate the launch policy (R4a).
            if count >= self._n_synth or dig in self._prewarmed:
                self._launch(pattern, flags, enc, key, dig)
                return ROUTE_SYNTH if dig in self._synthesizing else ROUTE_NOT_RESIDENT
            return ROUTE_NOT_RESIDENT

    # -- stats (R66) -------------------------------------------------------
    def stats(self) -> dict:
        with self._lock:
            self.poll()
            snap = dict(self._stats)
            snap["circuits_synthesizing"] = len(self._synthesizing)
            snap["circuits_resident"] = len(self._resident)
            return snap

    # -- teardown ----------------------------------------------------------
    def shutdown(self) -> None:
        with self._lock:
            if self._service is not None:
                self._service.shutdown()
                self._service = None
            self._synthesizing.clear()
            self._resident.clear()

    def reset(self) -> None:
        """Test helper: shut down the service and clear all state (hermetic)."""
        self.shutdown()
        with self._lock:
            self._counts.clear()
            self._prewarmed.clear()
            self._verdict.clear()
            self._inject_synth_fail.clear()
            for k in self._stats:
                self._stats[k] = 0


def _job_from_circuit(circuit: hdl.GeneratedCircuit) -> SynthJob:
    """Build a picklable :class:`SynthJob` from a generated circuit's metadata."""
    r = circuit.resources or {}
    return SynthJob(
        pattern_hash=circuit.pattern_hash16.hex(),
        encoding=circuit.enc,
        effective_flags=int(circuit.automaton.flags),
        circ_flags=circuit.circ_flags,
        generator_version=circuit.generator_version,
        harness_version=circuit.harness_version,
        rtl=circuit.rtl,
        luts=int(r.get("luts", 0)),
        ffs=int(r.get("ffs", 0)),
        bram_kb=int(r.get("bram_kb", 0)),
        dsps=int(r.get("dsps", 0)),
        over_approx_classes=tuple(circuit.over_approx),
        estimated_fp_rate=float(circuit.estimated_fp_rate),
        # Serialized automaton the native C model executes (Task-7 artifact
        # extension); coordinates with src/pyro_rt.c only through this file.
        automaton_table=_artifact.serialize_automaton_body(circuit.automaton),
    )


# --- process-wide manager used by the router / pyro.prewarm ----------------
_GLOBAL: Optional[ResidencyManager] = None
_GLOBAL_LOCK = threading.Lock()


def _effective_n_synth() -> int:
    """The launch threshold in force: the sampled ``PYRO_N_SYNTH`` override (R68)
    if valid, else the spec default.  Read only at sampling points (R35a) — never
    on the per-call hot path — via :mod:`pyro._route`'s cached snapshot."""
    try:
        from .. import _route
        override = _route.n_synth_override()
    except Exception:
        override = None
    return int(override) if override else N_SYNTH_DEFAULT


def apply_n_synth() -> None:
    """Push the freshly-sampled ``PYRO_N_SYNTH`` (R68) onto the live global
    manager, if one exists.  Called from an R35a sampling point (``sample_env``).
    """
    with _GLOBAL_LOCK:
        if _GLOBAL is not None:
            _GLOBAL._n_synth = _effective_n_synth()


def _effective_toolchain_config() -> ToolchainConfig:
    """Build the worker's :class:`ToolchainConfig` from the sampled R70 toolchain
    selection (``PYRO_TOOLCHAIN`` / ``PYRO_VIVADO``).

    The selection is read from :mod:`pyro._route`'s cached snapshot, which is
    (re)sampled from ``os.environ`` at the R35a sampling points (import,
    install/uninstall, refresh_env) — never on the per-call hot path.  The
    residency manager reads this snapshot **once, when it is first created**, and
    pins the toolchain for its lifetime (see :func:`get_manager`).  Defaults
    reproduce the mock behavior, so with no knob set this returns a byte-identical
    ``ToolchainConfig()`` (kind ``"mock"``) and Phase-0/1 behavior is unchanged
    (R70a)."""
    from .. import _route
    kind, vivado_dir = _route.toolchain_selection()
    if kind == "vivado":
        return ToolchainConfig(kind="vivado", vivado_dir=vivado_dir)
    return ToolchainConfig()


def _effective_timeout(config: ToolchainConfig) -> float:
    """The client-side service-reaper (R63e) timeout for a given toolchain.

    R77: the reaper is *bookkeeping only* and a **backstop** to the vivado
    adapter's own authoritative process-tree kill (job_timeout_s).  A backstop
    that fires *before* the authoritative timeout would spuriously mark every
    minutes-long real Vivado job as failed, so for the vivado kind the reaper
    window must sit strictly beyond the adapter's own deadline.  For the mock
    kind the Phase-0/1 default (30 s) is preserved byte-for-byte."""
    if getattr(config, "kind", "mock") == "vivado":
        # adapter kills at job_timeout_s; give the backstop a generous margin for
        # Vivado startup + report writing + result plumbing.
        return float(config.job_timeout_s) + 300.0
    return 30.0


def get_manager() -> ResidencyManager:
    """The process-wide residency manager (lazily created).

    Its persistent cache defaults to the user cache area (``default_root``); a
    test may point it at a temp directory via ``PYRO_CACHE_DIR`` + ``reset_manager``.
    Its launch threshold honors the sampled ``PYRO_N_SYNTH`` override (R68).

    Toolchain selection (R70).  The ``PYRO_TOOLCHAIN`` / ``PYRO_VIVADO`` knobs are
    re-sampled into :mod:`pyro._route`'s cached snapshot at every R35a sampling
    point (import, install/uninstall, refresh_env).  This manager reads that
    snapshot **once, at first creation**, and pins the resulting
    :class:`ToolchainConfig` (and its R77 reaper timeout) for its lifetime — the
    out-of-process worker is spawned with that config.  A mid-process switch of
    ``PYRO_TOOLCHAIN`` therefore takes effect for a **freshly created** manager
    (e.g. after :func:`reset_manager`), NOT for an already-running one; this is
    intentional — live-swapping the toolchain of a manager with an in-flight
    real-Vivado job would orphan that job's subprocess tree (its R77 self-kill
    cannot run once the worker is torn down).  Unlike the scalar ``PYRO_N_SYNTH``
    (consulted per dispatch and pushed live by :func:`apply_n_synth`), the
    toolchain governs a spawned subprocess and so is pinned at construction.
    """
    global _GLOBAL
    mgr = _GLOBAL
    if mgr is not None:
        return mgr
    with _GLOBAL_LOCK:
        if _GLOBAL is None:
            # R70a: the sampled toolchain selection crosses into the worker via
            # the ToolchainConfig built here (mock by default; vivado when the
            # operator opted in at the last R35a sampling point).  The service
            # reaper timeout tracks the toolchain (R77 backstop vs. mock default).
            _cfg = _effective_toolchain_config()
            _GLOBAL = ResidencyManager(
                toolchain_config=_cfg,
                n_synth=_effective_n_synth(),
                timeout=_effective_timeout(_cfg))
        return _GLOBAL


def reset_manager() -> None:
    """Test helper: tear down the global manager (shut down its service, clear
    state) and drop it so the next :func:`get_manager` rebuilds a fresh one —
    no spawned-process / counter / cache-dir residue leaks between tests."""
    global _GLOBAL
    with _GLOBAL_LOCK:
        if _GLOBAL is not None:
            _GLOBAL.shutdown()
            _GLOBAL = None


@atexit.register
def _atexit_shutdown() -> None:
    """Shut down whatever global manager exists at interpreter exit (no zombie
    worker), registered exactly once regardless of how many managers were built."""
    with _GLOBAL_LOCK:
        if _GLOBAL is not None:
            try:
                _GLOBAL.shutdown()
            except BaseException:
                pass
