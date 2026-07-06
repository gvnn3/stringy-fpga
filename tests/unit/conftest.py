"""Shared fixtures for the PYRO unit suite.

Env flags are now a cached snapshot sampled only at import / install /
uninstall / refresh_env (R35a).  To keep tests isolated, re-sample the (clean)
process environment around every test so a cached flag set by one test cannot
leak into the next.  A test that mutates os.environ mid-body must itself call
``pyro.refresh_env()`` after the mutation for it to take effect (R35b/R35d).
"""
import os

import pytest

import pyro

# Toolchain-selection knobs (R68/R70). The unit suite asserts MOCK synthesis
# semantics (tier transitions 'synthesizing'->'warm', warm reuse, permanent
# fallback); NO unit test exercises the vivado toolchain. A combined
# `pytest tests/acceptance tests/unit` run can leak PYRO_TOOLCHAIN=vivado /
# PYRO_VIVADO into os.environ from the real-Vivado acceptance path (the session
# vivado_corpus pins it and the acceptance _ENV_KEYS hygiene is directory-scoped),
# which would make a mock-expecting prewarm test build a VIVADO residency manager and
# launch a real (minutes-long) synth, timing out drain() so the tier stays
# 'synthesizing'. Force the mock baseline hermetically for every unit test.
_TOOLCHAIN_KEYS = ("PYRO_TOOLCHAIN", "PYRO_VIVADO")


@pytest.fixture(autouse=True)
def _reset_env_cache():
    # Force MOCK toolchain selection, ignoring any leaked vivado env, BEFORE
    # refresh_env re-samples (R35a) and BEFORE any get_manager()/reset_manager()
    # builds a residency manager. Saved/restored so we leave no env residue.
    _saved_tc = {k: os.environ.get(k) for k in _TOOLCHAIN_KEYS}
    os.environ["PYRO_TOOLCHAIN"] = "mock"
    os.environ.pop("PYRO_VIVADO", None)
    pyro.refresh_env()
    yield
    for k, v in _saved_tc.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    pyro.refresh_env()
    # Reset the process-wide residency manager (shuts down any spawned synthesis
    # service, clears launch counters / resident state) so no process or counter
    # residue leaks between tests (Task-6 brief hard rule).
    try:
        from pyro.synth import residency as _res
        _res.reset_manager()
    except Exception:
        pass
