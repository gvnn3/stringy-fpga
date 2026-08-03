"""R61 fault injection via the public v2.0.5 seam (R67).

R61 (simulate device errors / timeouts / false-positive windows and assert the
fallback-retry path, R52) is driven through the normative `pyro.testing` seam
(R67), gated behind PYRO_ENABLE_TEST_HOOKS=1 sampled at an R35a point.  The
comprehensive fault-injection coverage lives in test_ac1_6_fault_routing.py
(AC-1-6); this module keeps a minimal live R61 check plus the public stats
diagnostics-surface sanity check that R52/R61 build on (R66).
"""

import os

import pytest

import pyro
import pyro.re as pre


def test_r61_device_error_injection_fallback_retry(request):
    """R61/R52/R67: an injected device error routes the affected dispatch to
    fallback with a CPython-identical result and increments fallback_after_error
    (the seam makes R61 driveable from the public surface)."""
    import re as stdre
    os.environ["PYRO_ENABLE_TEST_HOOKS"] = "1"
    os.environ["PYRO_FORCE_MODEL"] = "1"
    pyro.refresh_env()
    request.addfinalizer(pyro.testing.reset)

    fae0 = pre.stats()["fallback_after_error"]
    pyro.testing.inject_device_error("device", 1)
    subj = "z r61pat z"
    m = pre.search("r61pat", subj)
    # result unchanged (R52)
    assert m.span() == stdre.search("r61pat", subj).span()
    assert pre.stats()["fallback_after_error"] - fae0 == 1  # counted (R66)


def test_stats_public_surface_is_counter_dict():
    """AC-3-4/R52/R66 (public surface): pyro.re.stats() reports integer dispatch
    counters (exact key names per R66)."""
    if not hasattr(pre, "stats") or not callable(pre.stats):
        pytest.skip("pyro.re.stats() not present on the public surface")
    s = pre.stats()
    assert isinstance(s, dict), f"stats() must return a dict, got {type(s)}"
    assert s, "stats() returned an empty dict"
    for k, v in s.items():
        assert isinstance(k, str), f"stats key {k!r} is not a str"
        assert isinstance(v, int), f"stats[{k!r}]={v!r} is not an int"
        assert v >= 0, f"stats[{k!r}]={v} is negative"
