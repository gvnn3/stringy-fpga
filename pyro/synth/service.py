"""Out-of-process synthesis service skeleton (spec §7.5 R63).

R63 requires synthesis to run in a **separate process** so a minutes-long Vivado
run never blocks or slows the Python application.  This module implements that
skeleton around the **mock toolchain** (R63b):

  * **R63a job queue + dedup.** Jobs are keyed by the R4 bitstream-cache key; a
    key that is already cached, already failed, or already in flight is not
    re-run.
  * **R63b toolchain.** Each job runs :class:`pyro.synth.toolchain.MockToolchain`
    (a real Vivado flow substitutes here in Phase 2).
  * **R63c concurrency.** The service MAY run several workers; loading into the
    single-tenant PR region is serialized elsewhere (R64).  Default is one worker.
  * **R63d cache population.** On success the worker writes the artifact +
    manifest to the persistent :class:`pyro.synth.cache.BitstreamCache`, so a
    later run (including after restart) finds the key **warm**.
  * **R63e isolation + timeout.** A job that raises, hangs, or overruns the
    per-job timeout is contained: the key is marked a permanent-fallback negative
    entry (R65) and the caller is never affected — callers keep running on
    fallback (R51/R52).

Nothing here blocks an API call: :meth:`submit` only enqueues.  Tests use
:meth:`drain` to await completion deterministically and :meth:`shutdown` to
guarantee no spawned-process / temp-dir residue.
"""

from __future__ import annotations

import multiprocessing as _mp
import queue as _queue
import time
from typing import Callable, Dict, Optional, Tuple

from .cache import BitstreamCache, BitstreamKey, key_digest
from .toolchain import (
    MockToolchain, SynthJob, SynthesisFailed, ToolchainConfig, VivadoToolchain,
)

# Result records pushed from the worker back to the client.
STATUS_OK = "ok"
STATUS_FAILED = "failed"


def _pick_context():
    """Prefer 'forkserver' (clean single-threaded fork parent), then 'fork',
    then the platform default multiprocessing context."""
    for method in ("forkserver", "fork"):
        try:
            return _mp.get_context(method)
        except ValueError:
            continue
    return _mp.get_context()


def _make_toolchain(config: ToolchainConfig):
    """Construct the toolchain the worker runs, selected by ``config.kind`` (R70).

    ``vivado`` selects the real OOC adapter (R70-R77); anything else selects the
    mock (R63b) — the Phase-0/1 default.  Note (R70/R71 fail-safe): when
    ``kind == "vivado"`` but PYRO_VIVADO does not resolve to a runnable Vivado,
    the mock is **NOT** silently substituted — :class:`VivadoToolchain` is still
    constructed and its ``run`` raises :class:`SynthesisFailed` (toolchain
    absent), so a vivado synth request fails safe to permanent fallback (R65)
    under its own R75a cache key rather than being served a mock stub.
    """
    if config.kind == "vivado":
        return VivadoToolchain(config)
    return MockToolchain(config)


def _worker_main(job_q, result_q, cache_root: str, config: ToolchainConfig) -> None:
    """Worker-process entry point (top-level so it is picklable under 'spawn').

    Consumes ``(key, job)`` items, runs the mock toolchain, populates the cache
    on success or records a negative entry on failure, and reports the outcome.
    Any exception is contained (R63e): it becomes a failed result, never a crash
    that could affect the caller.
    """
    cache = BitstreamCache(cache_root)
    toolchain = _make_toolchain(config)
    while True:
        item = job_q.get()
        if item is None:                      # shutdown sentinel
            break
        key, job = item
        try:
            payload, manifest = toolchain.run(job)
            cache.put(key, payload, manifest)
            result_q.put((key, STATUS_OK, ""))
        except SynthesisFailed as exc:        # R65 fit/timing/tool/timeout fail
            cache.put_failure(key, str(exc))
            result_q.put((key, STATUS_FAILED, str(exc)))
        except BaseException as exc:          # R63e: contain any other failure
            reason = f"synthesis worker error: {exc!r}"
            cache.put_failure(key, reason)
            result_q.put((key, STATUS_FAILED, reason))


class SynthesisService:
    """Client handle to the out-of-process synthesis worker(s) (R63)."""

    def __init__(self, cache: BitstreamCache,
                 config: ToolchainConfig = ToolchainConfig(),
                 workers: int = 1, timeout: float = 30.0):
        self._cache = cache
        self._config = config
        self._timeout = float(timeout)
        # 'forkserver': workers are forked from a clean, single-threaded server
        # process.  This avoids (a) the 'spawn' re-import of __main__ (which
        # breaks under pytest / -c / stdin entry points) and (b) the
        # fork-in-a-multithreaded-process hazard flagged on 3.12 (the parent runs
        # a Queue feeder thread).  ``_worker_main`` is a top-level function, so it
        # is picklable for forkserver.  Falls back to fork, then the default.
        self._ctx = _pick_context()
        self._job_q = self._ctx.Queue()
        self._result_q = self._ctx.Queue()
        self._procs = []
        # digest -> (key, submit monotonic timestamp) for R63e timeout + dedup.
        # The full key is kept so a timeout reap can write a negative cache entry.
        self._inflight: Dict[str, Tuple[BitstreamKey, float]] = {}
        self._done_cb: Optional[Callable[[BitstreamKey, str, str], None]] = None
        for _ in range(max(1, int(workers))):
            p = self._ctx.Process(
                target=_worker_main,
                args=(self._job_q, self._result_q, str(cache.root), config),
                daemon=True,
            )
            p.start()
            self._procs.append(p)

    # -- completion callback (residency manager wires this) ----------------
    def set_done_callback(self, cb: Callable[[BitstreamKey, str, str], None]) -> None:
        self._done_cb = cb

    # -- R63a submit + dedup ------------------------------------------------
    def submit(self, key: BitstreamKey, job: SynthJob) -> bool:
        """Enqueue a synthesis job.  Returns ``True`` if newly enqueued.

        Deduplicates (R63a): a key already cached (warm), already failed (R65),
        or already in flight is NOT re-run.  Never blocks beyond a queue put.
        """
        self._reap_timeouts()
        dig = key_digest(key)
        if dig in self._inflight:
            return False
        if self._cache.has(key) or self._cache.is_failed(key):
            return False
        self._inflight[dig] = (key, time.monotonic())
        self._job_q.put((key, job))
        return True

    def in_flight(self) -> int:
        self._poll_results()
        self._reap_timeouts()
        return len(self._inflight)

    def is_in_flight(self, key: BitstreamKey) -> bool:
        return key_digest(key) in self._inflight

    # -- result draining ----------------------------------------------------
    def _poll_results(self) -> None:
        while True:
            try:
                key, status, reason = self._result_q.get_nowait()
            except _queue.Empty:
                break
            self._inflight.pop(key_digest(key), None)
            if self._done_cb is not None:
                self._done_cb(key, status, reason)

    def _reap_timeouts(self) -> None:
        """R63e: mark any job that overran the per-job timeout as **failed**.

        The (mock) worker finishes quickly; this guards the real-flow case where
        a job hangs.  A reaped key is treated exactly like a worker-mediated
        failure: it becomes a permanent-fallback negative cache entry (R65) AND
        the done-callback is fired with the failure, so the residency manager's
        ``_synthesizing`` set/gauge is unstuck and ``synth_failed`` is counted.
        A late worker result for a reaped key is ignored (no longer in flight).
        """
        if not self._inflight:
            return
        now = time.monotonic()
        expired = [(dig, key) for dig, (key, t0) in self._inflight.items()
                   if now - t0 > self._timeout]
        for dig, key in expired:
            self._inflight.pop(dig, None)
            reason = f"synthesis per-job timeout ({self._timeout:g}s) exceeded (R63e)"
            try:
                self._cache.put_failure(key, reason)     # R65 permanent fallback
            except OSError:
                pass
            if self._done_cb is not None:
                self._done_cb(key, STATUS_FAILED, reason)

    def poll(self) -> None:
        """Drain any available results and reap timeouts (non-blocking)."""
        self._poll_results()
        self._reap_timeouts()

    def drain(self, timeout: float = 10.0) -> bool:
        """Block until all in-flight jobs complete or ``timeout`` elapses.

        Test helper only — API calls never block on synthesis (R63).  Returns
        ``True`` if the queue drained, ``False`` on timeout.
        """
        deadline = time.monotonic() + timeout
        while True:
            self._poll_results()
            self._reap_timeouts()
            if not self._inflight:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.002)

    # -- lifecycle ----------------------------------------------------------
    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the worker(s) and release IPC resources (no residue)."""
        for _ in self._procs:
            try:
                self._job_q.put(None)          # one sentinel per worker
            except (ValueError, OSError):
                pass
        for p in self._procs:
            p.join(timeout)
            if p.is_alive():
                p.terminate()
                p.join(timeout)
        self._procs = []
        for q in (self._job_q, self._result_q):
            try:
                q.close()
                q.join_thread()
            except (ValueError, OSError, AssertionError):
                pass
        self._inflight.clear()

    def __del__(self):
        # Best-effort safety net; explicit shutdown() is preferred.
        try:
            if self._procs:
                self.shutdown(timeout=1.0)
        except BaseException:
            pass
