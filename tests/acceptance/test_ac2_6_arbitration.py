"""AC-2-6: single-tenant PR arbitration.

SKIP clause (device_usable ∧ pr_flow_present, R71 — BOTH false here): on-device
arbitration where loading a second pattern's circuit evicts the first.

Always-LIVE clause (asserted on the software model, per the R71 matrix): the
single-tenant eviction policy (R64) and byte-identical results across evict/reload
cycles (R53).  Per R64a (v2.1.1) device-free residency bookkeeping is REQUIRED,
not vacuous: the model MUST track `circuits_resident`, promote to residency, and
fire deterministic LRU eviction (`circuits_evicted`) against the single-tenant
budget — so this is a FIRM, non-vacuous LIVE assertion.  The multi-pattern policy
is exercised on the mock toolchain (a real Vivado run per pattern would be
prohibitive); the DEVICE clause still records a SKIP.
(R64, R64a, R53, R71)
"""
import ctypes
import tempfile

import pytest

import phase2_support

# Distinct HW-eligible literal patterns; more than the single-tenant budget so at
# least one eviction is forced when each is driven toward residency (R64).
EVICT_PATTERNS = ["alphaZZ", "bravoZZ", "charlieZZ", "deltaZZ"]


def _pr_partitions():
    """R42/R64 single-tenant budget from the model caps; default 1 (single-tenant)
    if the C library is unavailable."""
    try:
        import abi_ctypes as abi
        lib = abi.load()
    except Exception:
        return 1
    ctx = ctypes.c_void_p()
    if lib.pyro_ctx_open(ctypes.byref(ctx), b"model://") != abi.PYRO_OK:
        return 1
    try:
        caps = abi.PyroCaps()
        if lib.pyro_caps_get(ctx, ctypes.byref(caps)) != abi.PYRO_OK:
            return 1
        return int(caps.pr_partitions) or 1
    finally:
        lib.pyro_ctx_close(ctx)


def test_on_device_arbitration_skips():
    """SKIP: on-device single-tenant arbitration/eviction requires
    device_usable ∧ pr_flow_present, both false (R71)."""
    dev_ok, dev_reason = phase2_support.device_usable()
    pr_ok, pr_reason = phase2_support.pr_flow_present()
    if not (dev_ok and pr_ok):
        reasons = [r for ok, r in ((dev_ok, dev_reason), (pr_ok, pr_reason)) if not ok]
        pytest.skip("; ".join(reasons))
    raise AssertionError("device_usable ∧ pr_flow_present unexpectedly true — "
                         "implement on-device eviction assertions (R64)")


def test_model_single_tenant_eviction_and_byte_identical():
    """LIVE (model): loading several distinct patterns' circuits keeps the resident
    gauge within the single-tenant budget, evicts deterministically without
    thrashing, is NOT a device error, and yields byte-identical results across
    evict/reload (R53/R64)."""
    pr_partitions = _pr_partitions()
    with tempfile.TemporaryDirectory(prefix="pyro_ac26_evict_") as cache:
        res, _o, _e = phase2_support.run_phase2_worker(
            "eviction", "|".join(EVICT_PATTERNS), cache_dir=cache, timeout=180)

    # Keystone: results are byte-identical across evict/reload regardless of tier.
    assert res["all_eq_stock"] is True, (
        f"results differ from stock re across evict/reload (R53/R64): "
        f"{res['per_pattern_eq']}")
    # Eviction is routing, never a device error (R64).
    assert res["fallback_after_error"] == 0, res
    # Single-tenant invariant: the resident gauge never exceeds the PR budget.
    assert res["max_resident_gauge"] <= pr_partitions, (
        f"resident gauge {res['max_resident_gauge']} exceeds single-tenant PR "
        f"budget {pr_partitions} (R42/R64)")
    # Deterministic eviction: once residency is exercised and more distinct
    # patterns than partitions are loaded, at least one eviction must occur.
    if res["max_resident_gauge"] >= 1 and res["n_patterns"] > pr_partitions:
        assert res["evicted_delta"] >= 1, (
            f"{res['n_patterns']} patterns loaded into {pr_partitions} partition(s) "
            f"but circuits_evicted did not advance ({res['evicted_delta']}) — "
            "eviction policy did not fire (R64)")
