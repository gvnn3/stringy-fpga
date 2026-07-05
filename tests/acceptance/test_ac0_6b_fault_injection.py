"""R61 fault injection — Phase 0 scope.

R61 (simulate device errors / timeouts / false-positive windows and assert the
fallback-retry path R52) targets the C-ABI host runtime and software model that
are delivered in Phase 1 (AC-1-6).  Phase 0 exposes NO public hook on the
`pyro` / `pyro.re` surface to inject a PYRO_E_DEVICE / PYRO_E_TIMEOUT or a
false-positive window, and there is no physical/model device error to trigger in
the pure-Python shim.  Per the task brief, the missing Phase 0 hook is recorded
as a skip.  The routing controls that Phase 0 DOES expose (PYRO_DISABLE /
PYRO_FORCE_MODEL) are covered by test_ac0_5_install_env.py.

The public diagnostics surface that R52/R61 build on (pyro.re.stats()) is
sanity-checked below without hard-coding implementation-specific key names.
"""

import pytest

import pyro.re as pre


def test_r61_device_error_injection_not_a_phase0_public_hook():
    """R61/R52: device-error/false-positive injection is Phase 1 (AC-1-6);
    no public Phase 0 hook exists to drive it from the pyro public surface."""
    pytest.skip(
        "R61 device-error/timeout/false-positive injection is a Phase 1 model "
        "capability (R52, AC-1-6); Phase 0 exposes no public injection hook on "
        "pyro/pyro.re. Phase 0 routing controls (PYRO_DISABLE/PYRO_FORCE_MODEL) "
        "are covered in test_ac0_5_install_env.py."
    )


def test_stats_public_surface_is_counter_dict():
    """AC-3-4/R52 (public surface): pyro.re.stats() reports integer dispatch
    counters.  Exact key names are not pinned by the spec (only the categories
    hardware/model/fallback/fallback-after-error are named), so this asserts the
    shape without hard-coding key strings."""
    if not hasattr(pre, "stats") or not callable(pre.stats):
        pytest.skip("pyro.re.stats() not present on the public surface")
    s = pre.stats()
    assert isinstance(s, dict), f"stats() must return a dict, got {type(s)}"
    assert s, "stats() returned an empty dict"
    for k, v in s.items():
        assert isinstance(k, str), f"stats key {k!r} is not a str"
        assert isinstance(v, int), f"stats[{k!r}]={v!r} is not an int"
        assert v >= 0, f"stats[{k!r}]={v} is negative"
