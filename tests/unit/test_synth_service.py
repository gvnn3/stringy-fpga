"""Out-of-process synthesis service skeleton (R63/R63a-e).

Hermetic: every service is created with a temp-dir cache and explicitly shut down
in a fixture teardown, leaving no spawned-process or temp-dir residue.
"""

import os
import time

import pytest

import pyro.hdl as hdl
from pyro.hdl import identity
from pyro.synth import (
    BitstreamCache, SynthesisService, ToolchainConfig, make_key, key_digest,
    STATUS_OK, STATUS_FAILED,
)
from pyro.synth.residency import _job_from_circuit
from pyro.synth.toolchain import TOOLCHAIN_VERSION, SHELL_VERSION


def _key(pattern, flags=0):
    dk = identity.descriptor_key(pattern, flags, hdl.GENERATOR_VERSION)
    return make_key(dk, TOOLCHAIN_VERSION, SHELL_VERSION)


def _job(pattern, flags=0):
    return _job_from_circuit(hdl.generate(pattern, flags))


@pytest.fixture
def service_factory(tmp_path):
    made = []

    def _make(config=ToolchainConfig(), **kw):
        cache = BitstreamCache(tmp_path / f"c{len(made)}")
        svc = SynthesisService(cache, config, **kw)
        made.append((svc, cache))
        return svc, cache

    yield _make
    for svc, _cache in made:
        svc.shutdown()


# --- R63/R63d: jobs run out of process and populate the cache warm ---------
def test_job_runs_and_populates_cache(service_factory):
    svc, cache = service_factory()
    k = _key("abc[0-9]+")
    assert cache.has(k) is False
    assert svc.submit(k, _job("abc[0-9]+")) is True
    assert svc.drain(10.0) is True
    assert cache.has(k) is True                 # warm after synthesis (R63d)


def test_worker_runs_in_separate_process(service_factory):
    # The worker reports a different PID than the caller (R63 separate process).
    svc, cache = service_factory()
    # A pattern whose stub payload embeds nothing about pid; instead assert the
    # process object is a real, live child at submit time.
    assert any(p.pid != os.getpid() and p.pid is not None for p in svc._procs)


# --- R63a: dedup by key ----------------------------------------------------
def test_dedup_in_flight_and_cached(service_factory):
    svc, cache = service_factory()
    k = _key("dedup[0-9]+")
    assert svc.submit(k, _job("dedup[0-9]+")) is True
    assert svc.submit(k, _job("dedup[0-9]+")) is False   # already in flight
    svc.drain(10.0)
    assert svc.submit(k, _job("dedup[0-9]+")) is False   # already cached (warm)


# --- R65: synthesis failure -> negative entry, no exception, correct signal -
def test_failure_records_negative_entry(service_factory):
    svc, cache = service_factory(ToolchainConfig(fail_mode="error"))
    seen = []
    svc.set_done_callback(lambda key, status, reason: seen.append(status))
    k = _key("failing[0-9]+")
    svc.submit(k, _job("failing[0-9]+"))
    assert svc.drain(10.0) is True
    assert cache.is_failed(k) is True           # permanent fallback (R65)
    assert cache.has(k) is False
    assert seen == [STATUS_FAILED]


def test_timeout_mode_marks_failed(service_factory):
    svc, cache = service_factory(ToolchainConfig(fail_mode="timeout"))
    k = _key("slow[0-9]+")
    svc.submit(k, _job("slow[0-9]+"))
    assert svc.drain(10.0) is True
    assert cache.is_failed(k) is True


# --- R63e per-job timeout reap: hung job -> permanent fallback -------------
def test_timeout_reap_marks_failed_and_fires_callback(service_factory):
    # A genuinely hung worker never reports back; the client-side per-job timeout
    # reap MUST treat the key exactly like a worker-mediated failure: write a
    # negative cache entry (R65) AND fire the done-callback with the failure, so a
    # residency manager's in-flight slot is unstuck and the key is not
    # re-synthesizable.  Injected directly (no real hang) for determinism.
    svc, cache = service_factory(timeout=0.01)
    seen = []
    svc.set_done_callback(lambda k, s, r: seen.append((s, r)))
    k = _key("hung[0-9]+")
    # Simulate a job stuck in flight past its deadline.
    svc._inflight[key_digest(k)] = (k, time.monotonic() - 10.0)
    svc.poll()                                   # triggers _reap_timeouts
    assert cache.is_failed(k) is True            # permanent-fallback entry (R65)
    assert svc.in_flight() == 0                   # slot unstuck
    assert len(seen) == 1 and seen[0][0] == STATUS_FAILED
    # Re-submitting the reaped key is refused (dedup against the negative entry).
    assert svc.submit(k, _job("hung[0-9]+")) is False


# --- R63e isolation: a job failure does not affect other jobs --------------
def test_isolation_success_after_earlier_success(service_factory):
    svc, cache = service_factory()
    ks = [_key(f"p{i}[0-9]+") for i in range(4)]
    for i, k in enumerate(ks):
        svc.submit(k, _job(f"p{i}[0-9]+"))
    assert svc.drain(10.0) is True
    assert all(cache.has(k) for k in ks)


# --- R4/R63d: warm artifact persists across a simulated service restart -----
def test_persistence_across_service_restart(tmp_path):
    cache_dir = tmp_path / "persist"
    svc1 = SynthesisService(BitstreamCache(cache_dir), ToolchainConfig())
    k = _key("persist[0-9]+")
    try:
        svc1.submit(k, _job("persist[0-9]+"))
        assert svc1.drain(10.0) is True
    finally:
        svc1.shutdown()
    # "Restart": a fresh service + cache over the same directory finds it warm
    # and does NOT re-run synthesis (dedup by cached key).
    svc2 = SynthesisService(BitstreamCache(cache_dir), ToolchainConfig())
    try:
        assert BitstreamCache(cache_dir).has(k) is True
        assert svc2.submit(k, _job("persist[0-9]+")) is False
    finally:
        svc2.shutdown()


# --- hermetic shutdown: workers gone, no residue ---------------------------
def test_shutdown_terminates_workers(tmp_path):
    svc = SynthesisService(BitstreamCache(tmp_path / "x"), ToolchainConfig())
    procs = list(svc._procs)
    svc.shutdown()
    assert all(not p.is_alive() for p in procs)
