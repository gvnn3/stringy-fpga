"""AC-2b-2: live device probe pyro.device.probe_device — privilege-free path
LIVE.

R86.4/R83/R84: probe_device(config) -> (usable: bool, reason: str) implements
the
R83/R84 device-usability probe.  The privilege-free (False, reason) path is LIVE
on THIS host today: with no CAP_NET_RAW it MUST detect that BEFORE any
privileged
AF_PACKET operation and return (False, <R83 canonical reason>) WITHOUT raising
PermissionError/OSError (R86.4).

The usable==true flip requires a flashed PYRO PR shell answering ID_REQUEST
with a
SPEC16-valid ID_REPLY AND CAP_NET_RAW (R83 (i)+(ii)); on this host that path
records a SKIP naming the missing prerequisite (R71 SKIP discipline, never
PASS).

Derived ONLY from the spec.  Device-probe assertions are pending until
pyro.device
lands (xfail); the phase2_support-predicate SKIP path runs today.

Coverage: AC-2b-2.  Requirements: R78, R81, R83, R84, R86.1, R86.4, R86.6.

v2.2.2 (R86.4): the current-host reason is now the EXACT, non-elided
two-condition
canonical literal (converged with R83's current-host string).  CANON_HOST_REASON
asserts it exactly — no interpretation slack remains.
"""
import pytest

import phase2_support

try:
    import pyro.device as pdev
    _IMPORT_ERR = None
except Exception as exc:  # noqa: BLE001 — pending pyro.device is not a spec bug
    pdev = None
    _IMPORT_ERR = exc


def _need_pdev():
    if pdev is None:
        pytest.xfail(
    f"pending pyro.device implementation (R86): {
        _IMPORT_ERR!r}")


# R83 canonical unmet-condition phrases (exact spec text, fixed order).
CANON_PROBE = ("probe: no valid ID_REPLY (no reply within PYRO_PROBE_TIMEOUT, "
               "or static_shell_id SPEC16 mismatch)")
CANON_TRANSPORT = "transport: CAP_NET_RAW absent"
CANON_PREFIX = "device_usable=false — "
# On THIS host (no flashed PR shell, no CAP_NET_RAW) both conditions are unmet:
CANON_HOST_REASON = CANON_PREFIX + CANON_PROBE + ", " + CANON_TRANSPORT


def _make_config():
    """R86.6 probe config built directly from the normative DeviceConfig field
    names (iface = F3 onic netdev, expected_spec16 = R81 0x0202). 
    DeviceConfig is a
    frozen dataclass, so an unknown kwarg raises TypeError — no silent drop."""
    return pdev.DeviceConfig(
        iface="enp175s0f0",                            # F3 onic netdev (R86.6)
        expected_spec16=phase2_support.PYRO_SHELL_SPEC16,  # R81 expected 0x0202
    )


# ==========================================================================
# LIVE (no hardware): privilege-free (False, reason) path (R86.4).
# ==========================================================================
# AC-2b-2 (R86.4 exact literal)
def test_probe_privilege_free_exact_canonical_reason():
    """LIVE on this host (no CAP_NET_RAW): probe_device returns (False, reason)
    WITHOUT raising PermissionError/OSError, and `reason` is the EXACT,
    non-elided
    two-condition canonical literal R86.4 fixes for this condition set (both
    probe
    and transport conditions unmet, fixed order)."""
    _need_pdev()
    # Environmental precondition, not a product property: on a host that
    # HAS CAP_NET_RAW (any hardware-attached bench box) the privilege-free
    # path cannot be observed live, so the honest disposition is skip —
    # a hard assert here fails the suite for having hardware access.
    if phase2_support.has_cap_net_raw():
        pytest.skip("host has CAP_NET_RAW; the privilege-free LIVE "
                    "assertion needs a host without it (R86.4)")
    try:
        usable, reason = pdev.probe_device(_make_config())
    # pragma: no cover - spec violation
    except (PermissionError, OSError) as exc:
        pytest.fail(f"probe_device leaked {type(exc).__name__}"
                    " on the privilege-"
                    f"free path (R86.4 forbids requiring privilege for False)")
    assert usable is False, (
        "no CAP_NET_RAW and no flashed shell => not usable (R83)")
    assert reason == CANON_HOST_REASON, (
        f"reason != R86.4 exact non-elided canonical literal.\n got: "
        f"{reason!r}\n"
        f"want: {CANON_HOST_REASON!r}")


def test_probe_never_raises_permission_error():  # AC-2b-2 (R86.4)
    """R86.4: probe_device MUST detect missing CAP_NET_RAW BEFORE any privileged
    AF_PACKET operation; it returns, never raises, for any not-usable condition
    (only a genuinely malformed reply may raise PyroFrameError, which needs a
    live
    peer and cannot occur on this privilege-free host)."""
    _need_pdev()
    result = pdev.probe_device(_make_config())  # must not raise
    assert isinstance(result, tuple) and len(result) == 2
    usable, reason = result
    assert isinstance(usable, bool) and isinstance(reason, str)


# ==========================================================================
# usable==true flip path: SKIP until the probe confirms (R71/R83 discipline).
# ==========================================================================
def test_device_usable_flip_is_skip_until_probe_confirms():  # AC-2b-2 (R71/R83)
    """The (True, reason) flip requires a flashed SPEC16-valid PR shell +
    CAP_NET_RAW
    (R83 (i)+(ii)).  Neither holds here, so this records a SKIP whose reason
    names
    the missing prerequisites (R71 SKIP discipline; a SKIP is NEVER a PASS)."""
    usable, reason = phase2_support.device_usable()
    if not usable:
        pytest.skip(reason)
    # Unreachable on this host; a PASS here would require a real probe-confirmed
    # device_usable == true (R83 honesty).
    raise AssertionError(
        "device_usable unexpectedly true — implement the on-device "
        "probe-confirmed "
        "assertions (valid ID_REPLY SPEC16 + CAP_NET_RAW, R81/R83/R86.4)")


# ==========================================================================
# Harness sweep check: phase2_support.device_usable() emits the R83 canonical
# reason (supersedes the stale v2.1.3 literal).  Validates Task-4 sweep.
# ==========================================================================
# AC-2b-2 (R83 supersedes v2.1.3)
def test_phase2_support_reason_is_r83_canonical():
    usable, reason = phase2_support.device_usable()
    assert usable is False
    assert reason.startswith(CANON_PREFIX), reason
    assert CANON_PROBE in reason, (
        "R83 probe-unmet condition must be enumerated (no flashed PR shell)")
    # The stale v2.1.3 literal MUST NOT survive the sweep.
    assert "no loadable PR artifact" not in reason
    assert "full reprogram needs root" not in reason
    # On this host (no CAP_NET_RAW) the exact canonical enumeration is both
    # unmet.
    if not phase2_support.has_cap_net_raw():
        assert reason == CANON_HOST_REASON, reason
