"""Shared fixtures for the PYRO unit suite.

Env flags are now a cached snapshot sampled only at import / install /
uninstall / refresh_env (R35a).  To keep tests isolated, re-sample the (clean)
process environment around every test so a cached flag set by one test cannot
leak into the next.  A test that mutates os.environ mid-body must itself call
``pyro.refresh_env()`` after the mutation for it to take effect (R35b/R35d).
"""
import pytest

import pyro


@pytest.fixture(autouse=True)
def _reset_env_cache():
    pyro.refresh_env()
    yield
    pyro.refresh_env()
    # Reset the process-wide residency manager (shuts down any spawned synthesis
    # service, clears launch counters / resident state) so no process or counter
    # residue leaks between tests (Task-6 brief hard rule).
    try:
        from pyro.synth import residency as _res
        _res.reset_manager()
    except Exception:
        pass
