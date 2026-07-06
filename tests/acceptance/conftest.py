"""Pytest configuration and isolation fixtures for the PYRO Phase 0 suite.

Ensures every test starts from a clean, deterministic PYRO state:
  * the interposition shim is uninstalled,
  * PYRO_DISABLE / PYRO_FORCE_MODEL are cleared from os.environ and re-sampled
    via pyro.refresh_env() (R35a sampling point),
  * the compile/reuse caches are purged (so per-pattern reuse counters do not
    leak between tests, keeping R51-step-4 routing deterministic).

Tests that exercise env controls set the variables and call refresh_env()
themselves; this fixture restores the prior os.environ afterwards so the suite
is order-independent and leaves no global residue (brief hard rule).
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

# --------------------------------------------------------------------------
# Hygiene (Task 8 item 9): point the persistent bitstream cache (R4) at a
# per-session temp dir BEFORE `pyro` is first imported (the cache dir, like the
# env flags, is sampled at import — R35a), so synthesis-triggering tests
# (perf warmups crossing N_synth, prewarm) NEVER write to the developer's real
# ~/.cache.  Cleaned up (and asserted residue-free) at session end.
# Subprocess workers set their own PYRO_CACHE_DIR (temp dirs owned by the test).
# --------------------------------------------------------------------------
_OWN_CACHE_DIR = None
if not os.environ.get("PYRO_CACHE_DIR"):
    _OWN_CACHE_DIR = tempfile.mkdtemp(prefix="pyro_acc_cache_")
    os.environ["PYRO_CACHE_DIR"] = _OWN_CACHE_DIR

# Snapshot whether a plausible default cache dir exists, to prove our redirect
# kept the run from writing there.
_DEFAULT_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "pyro")
_DEFAULT_CACHE_PREEXISTED = os.path.exists(_DEFAULT_CACHE)

import pytest

# Env vars sampled at R35a points (import/install/uninstall/refresh_env). All are
# saved/cleared/restored per test so hook/knob tests are order-independent.
_ENV_KEYS = ("PYRO_DISABLE", "PYRO_FORCE_MODEL", "PYRO_ENABLE_TEST_HOOKS",
             "PYRO_N_SYNTH",
             # R68/R70 (v2.1.0) toolchain-selection knobs. Sampled at R35a points;
             # saved/cleared/restored per test so the Phase-2 vivado probe
             # (phase2_support.toolchain_present) can pin them for a single test
             # without leaking the vivado toolchain into Phase-0/1 mock-only tests.
             "PYRO_TOOLCHAIN", "PYRO_VIVADO",
             # R68 device knobs (v2.2.2): the onic netdev name and hw_server URL that
             # seed DeviceConfig defaults (R86.6), sampled at R35a points. Saved/
             # cleared/restored per test so knob-sampling tests leave no env residue.
             "PYRO_DEVICE_IFACE", "PYRO_HW_SERVER",
             # R68 PR-substrate knobs (v2.2.4): the locked-static and reference-routed
             # DCP paths that populate ToolchainConfig.static_dcp/reference_dcp (R82b/
             # R82c/R82d) and that the phase2_support.pr_flow_present probe consults
             # (R83). No default; fail-loud (R68/R88). Saved/cleared/restored per test
             # so the both-polarity pr_flow_present checks leave no env residue.
             "PYRO_PR_STATIC_DCP", "PYRO_PR_REFERENCE_DCP",
             # R68 PR evidence-manifest knob (v2.2.5): the host-observable proxy for a
             # passing pr_verify (R82c) that flips pr_flow_present true (R83a). No
             # default; fail-closed. Populates ToolchainConfig.pr_evidence_manifest.
             # Saved/cleared/restored per test so the full-true polarity leaves no residue.
             "PYRO_PR_EVIDENCE_MANIFEST")


# --------------------------------------------------------------------------
# Toolchain-env leak containment (test-isolation hygiene).
# phase2_support.toolchain_present() PINS PYRO_TOOLCHAIN=vivado / PYRO_VIVADO into
# os.environ (a raw persistent write, needed so out-of-process synth workers select the
# real OOC adapter). It is called from the SESSION-scoped `vivado_corpus` fixture, which
# is set up BEFORE the function-scoped `pyro_isolation` fixture for the first requesting
# test — so pyro_isolation captures the ALREADY-pinned 'vivado' as its per-test baseline
# and "restores" to 'vivado', letting the pin outlive the suite and leak into tests/unit
# in a combined `pytest tests/acceptance tests/unit` run (mock-expecting unit tests then
# launch real Vivado synth). We snapshot the PRISTINE toolchain selection at conftest
# import (before any fixture can pin it) and force these two keys back to it in every
# pyro_isolation teardown, so the pin can never survive past the test that set it.
_PRISTINE_TOOLCHAIN = {k: os.environ.get(k) for k in ("PYRO_TOOLCHAIN", "PYRO_VIVADO")}


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "perf: performance / routing-overhead tests (AC-0-6, R3a/R5)"
    )


def pytest_sessionfinish(session, exitstatus):
    # Remove our per-session cache dir (leave no tempdir residue).
    if _OWN_CACHE_DIR and os.path.isdir(_OWN_CACHE_DIR):
        shutil.rmtree(_OWN_CACHE_DIR, ignore_errors=True)
    # Prove the redirect held: the default cache dir must not have been created
    # by our run.
    if not _DEFAULT_CACHE_PREEXISTED and os.path.exists(_DEFAULT_CACHE):
        raise AssertionError(
            f"cache residue leaked to {_DEFAULT_CACHE}; PYRO_CACHE_DIR redirect "
            "did not take effect (Task 8 item 9)"
        )


def _safe(fn):
    try:
        fn()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def pyro_isolation():
    import pyro
    import pyro.re as pre

    saved = {k: os.environ.get(k) for k in _ENV_KEYS}

    _safe(pyro.uninstall)
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    _safe(pyro.refresh_env)
    _safe(pre.purge)
    _safe(lambda: pyro.testing.reset())  # clear any injected faults (R67)

    try:
        yield
    finally:
        _safe(lambda: pyro.testing.reset())  # never leak injected faults
        _safe(pyro.uninstall)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # Defeat the polluted-baseline ordering bug: force the toolchain-selection keys
        # back to the PRISTINE session snapshot (not the possibly-vivado-pinned per-test
        # baseline) so a session-fixture pin never outlives its test or leaks to tests/unit.
        for k, v in _PRISTINE_TOOLCHAIN.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _safe(pyro.refresh_env)
        _safe(pre.purge)
