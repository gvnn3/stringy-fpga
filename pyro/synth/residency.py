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
from .cache import BitstreamCache, BitstreamKey, make_key, key_digest
from .service import SynthesisService, STATUS_OK, STATUS_FAILED
from .toolchain import (
    TOOLCHAIN_VERSION, SHELL_VERSION, SynthJob, ToolchainConfig,
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
    def bitstream_key(self, pattern, flags: int, enc: Optional[int] = None
                      ) -> BitstreamKey:
        dk = _identity.descriptor_key(pattern, flags, hdl.GENERATOR_VERSION)
        return make_key(dk, TOOLCHAIN_VERSION, SHELL_VERSION)

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

    # -- PR-region arbitration (R64) ---------------------------------------
    def _promote_to_resident(self, key: BitstreamKey, dig: str) -> bool:
        """Mock PR load of a warm artifact -> resident, verifying the manifest
        (R47b) and evicting the LRU resident if the single-tenant region is full
        (R64).  Returns ``True`` on success."""
        entry = self._cache.get(key)
        if entry is None:
            return False
        manifest = entry.manifest
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
    )


# --- process-wide manager used by the router / pyro.prewarm ----------------
_GLOBAL: Optional[ResidencyManager] = None
_GLOBAL_LOCK = threading.Lock()


def get_manager() -> ResidencyManager:
    """The process-wide residency manager (lazily created).

    Its persistent cache defaults to the user cache area (``default_root``); a
    test may point it at a temp directory via ``PYRO_CACHE_DIR`` + ``reset_manager``.
    """
    global _GLOBAL
    mgr = _GLOBAL
    if mgr is not None:
        return mgr
    with _GLOBAL_LOCK:
        if _GLOBAL is None:
            _GLOBAL = ResidencyManager()
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
