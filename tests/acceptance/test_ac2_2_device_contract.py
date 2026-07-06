"""AC-2-2: on-device harness/identity/scan over a >=1 MiB corpus.

This AC is ENTIRELY device-gated: it requires device_usable AND pr_flow_present
(R71), BOTH established false on this host (third-party live NIC that must not be
perturbed; no OpenNIC PR floorplan / PR-bitstream flow).  Per the R71 SKIP
discipline it records a SKIP whose reason names the missing prerequisites — NEVER
a PASS.

The equivalent model-side byte-identical correctness over a >=1 MiB corpus
(sound/complete candidate windows re-verified to CPython, R16/R17/R19/R47a) is
covered on the software model by AC-1-3 (test_ac1_3_generator_differential.py) —
that is a comment here, not a PASS recorded by this module.
(R16, R17, R19, R47a, R71, F5)
"""
import pytest

import phase2_support


def test_on_device_harness_contract_over_1mib():
    """SKIP: on-device identity + sound/complete scan re-verified byte-identical
    over >=1 MiB requires device_usable ∧ pr_flow_present, both false (R71)."""
    dev_ok, dev_reason = phase2_support.device_usable()
    pr_ok, pr_reason = phase2_support.pr_flow_present()
    if not (dev_ok and pr_ok):
        reasons = [r for ok, r in ((dev_ok, dev_reason), (pr_ok, pr_reason)) if not ok]
        pytest.skip("; ".join(reasons))
    # Unreachable on this host.  Model-side correctness is AC-1-3 (not a PASS here).
    raise AssertionError(
        "device_usable ∧ pr_flow_present unexpectedly true — implement on-device "
        "harness/identity/scan assertions (R47a/R19)")
