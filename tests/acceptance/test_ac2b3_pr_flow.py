"""AC-2b-3: PR-bitstream flow gating + manifest-layer honesty + JTAG load surface.

AC-2b-3 requires a per-pattern PYRO circuit implemented in-context against the
locked static DCP (R82), a passing `pr_verify`, a manifest carrying
`payload_kind == "pr_bitstream"` (R72/R82), and a JTAG partial-bitstream load
(R85/R86.5) after which the loaded circuit answers a MATCH_REQUEST byte-identically.
That whole path is gated on `pr_flow_present ∧ device_usable` (R83/R82d), BOTH
false on this host, so the on-device PASS records a SKIP naming the missing
prerequisites (R71 discipline, never a PASS).

Testable WITHOUT hardware (this file):
  * R72c/R82c honesty: while `pr_flow_present` is false, NO produced manifest may
    claim `payload_kind == "pr_bitstream"` — a `pr_bitstream` claim requires a
    passing `pr_verify` (R82c), which cannot exist without the R82d artifacts.
  * R47b/R82c `pr_verified: bool` (v2.2.2): the INDEPENDENT observable seam for
    pr_verify success — default False, JSON back-compat (absent key → False), True
    iff pr_verify passed, and consistent with payload_kind (True exactly for
    pr_bitstream; False for mock_stub/ooc_metrics).  This closes the former
    observability gap; tests now read pr_verified directly.
  * R86.1/R86.5 surface: load_partial raises PyroLoadError (never OSError/
    PermissionError) on a JTAG/hw_server mechanism failure.

Coverage: AC-2b-3.  Requirements: R47b, R72, R80, R82, R83, R85, R86.1, R86.5.
"""
import json
import os
import tempfile

import pytest

import pyro.synth as psynth  # public Manifest + from_json/to_json (R47b)

import phase1_support
import phase2_support

try:
    import pyro.device as pdev
    _IMPORT_ERR = None
except Exception as exc:  # noqa: BLE001 — pending pyro.device is not a spec bug
    pdev = None
    _IMPORT_ERR = exc


def _need_pdev():
    if pdev is None:
        pytest.xfail(f"pending pyro.device implementation (R86): {_IMPORT_ERR!r}")


# ==========================================================================
# On-device PASS is gated: SKIP until pr_flow_present ∧ device_usable (R83/R82d).
# ==========================================================================
def test_on_device_pr_bitstream_roundtrip_is_gated():  # AC-2b-3 (R71/R82/R83/R85)
    """SKIP: building a pr_bitstream in-context against the locked static DCP,
    JTAG-loading it, and answering a MATCH_REQUEST byte-identically requires
    pr_flow_present ∧ device_usable — both false here (R82d/R83)."""
    pr_ok, pr_reason = phase2_support.pr_flow_present()
    dev_ok, dev_reason = phase2_support.device_usable()
    if not (pr_ok and dev_ok):
        reasons = [r for ok, r in ((pr_ok, pr_reason), (dev_ok, dev_reason)) if not ok]
        pytest.skip("; ".join(reasons))
    raise AssertionError(
        "pr_flow_present ∧ device_usable unexpectedly true — implement the "
        "in-context pr_bitstream build + pr_verify + JTAG load + MATCH round-trip "
        "assertions (R82/R85/R86.5)")


# ==========================================================================
# Manifest-layer honesty (LIVE): no pr_bitstream claim while pr_flow absent.
# ==========================================================================
def test_no_manifest_claims_pr_bitstream_while_pr_flow_absent():  # AC-2b-3 (R72c/R82c)
    """LIVE: while pr_flow_present is false, no manifest anywhere in the session
    bitstream cache may claim payload_kind == "pr_bitstream" (R72c/R82c honesty).
    Such a claim without a passing pr_verify + R82d artifacts is a defect."""
    present, _reason = phase2_support.pr_flow_present()
    assert present is False, (
        "precondition: pr_flow_present must be false on this host (R71/R82d)")
    cache_dir = os.environ.get("PYRO_CACHE_DIR")
    assert cache_dir, "conftest must have pinned PYRO_CACHE_DIR"
    offenders = [
        path for path, data in phase2_support.find_manifests(cache_dir)
        if data.get("payload_kind") == "pr_bitstream"
    ]
    assert not offenders, (
        f"manifests falsely claim payload_kind=='pr_bitstream' without a passing "
        f"pr_verify / R82d artifacts (R72c/R82c): {offenders}")


# ==========================================================================
# R47b/R82c pr_verified manifest field (LIVE, hardware-free — closes the gap).
# ==========================================================================
_MANIFEST_BASE = dict(
    pattern_hash="deadbeef", encoding=0, effective_flags=0, circ_flags=0,
    generator_version=1, harness_version=0x00010000,
    toolchain_version=phase2_support.VIVADO_TOOLCHAIN_VERSION,
    shell_version=phase2_support.SHELL_VERSION_MODEL,
    luts=10, ffs=20, bram_kb=0, dsps=0, fmax_mhz=300.0, met_timing=True,
)


def test_pr_verified_defaults_false():  # AC-2b-3 (R47b)
    """R47b: pr_verified default is False (an unqualified manifest is not verified)."""
    m = psynth.Manifest(**_MANIFEST_BASE)
    assert m.pr_verified is False


@pytest.mark.parametrize("value,payload_kind", [
    (True, "pr_bitstream"),   # consistent: verified <=> pr_bitstream
    (False, "ooc_metrics"),   # consistent: honest metrics, no device claim
])
def test_pr_verified_json_roundtrip(value, payload_kind):  # AC-2b-3 (R47b)
    """R47b: pr_verified survives a to_json -> from_json round-trip exactly, like
    payload_kind."""
    m = psynth.Manifest(**_MANIFEST_BASE, pr_verified=value, payload_kind=payload_kind)
    back = psynth.Manifest.from_json(m.to_json())
    assert back.pr_verified is value
    assert back.payload_kind == payload_kind
    assert json.loads(m.to_json())["pr_verified"] is value, (
        "to_json MUST emit pr_verified (R47b)")


def test_pr_verified_absent_key_is_false_backcompat():  # AC-2b-3 (R47b back-compat)
    """R47b: from_json of a legacy manifest with NO pr_verified key defaults it to
    False (back-compat exactly like payload_kind)."""
    data = json.loads(psynth.Manifest(**_MANIFEST_BASE).to_json())
    data.pop("pr_verified", None)
    assert "pr_verified" not in data
    back = psynth.Manifest.from_json(json.dumps(data))
    assert back.pr_verified is False


def test_mock_produced_manifest_carries_pr_verified_false():  # AC-2b-3 (R47b/R82c)
    """R47b/R82c: a manifest produced by the MOCK toolchain (a mock_stub payload)
    MUST carry pr_verified == False — no pr_verify ran.  Produced hardware-free via
    a real mock prewarm synthesis into an isolated cache."""
    with tempfile.TemporaryDirectory(prefix="pyro_ac2b3_prv_") as cache:
        phase1_support.run_worker(
            "prewarm_lifecycle", "error|warn", "an error and warn", cache_dir=cache)
        mans = phase2_support.find_manifests(cache)
        assert mans, "mock prewarm produced no manifest to inspect"
        for path, data in mans:
            assert data.get("payload_kind") in ("mock_stub", "ooc_metrics"), (
                f"{path}: unexpected payload_kind {data.get('payload_kind')!r} on "
                "the mock path (R72a)")
            assert data.get("pr_verified", False) is False, (
                f"{path}: mock/ooc manifest must have pr_verified==False (R47b/R82c)")


def _assert_pr_verified_consistent(path, data):
    """R82c consistency invariant: pr_verified == True IFF payload_kind ==
    'pr_bitstream'.  A manifest claiming pr_verified with a non-PR payload_kind (or
    a pr_bitstream without pr_verified) is a spec violation."""
    pv = bool(data.get("pr_verified", False))
    pk = data.get("payload_kind", "mock_stub")
    if pv:
        assert pk == "pr_bitstream", (
            f"{path}: pr_verified==True but payload_kind=={pk!r} — a verified claim "
            "on a non-PR payload is an R82c violation")
    if pk == "pr_bitstream":
        assert pv is True, (
            f"{path}: payload_kind=='pr_bitstream' but pr_verified!=True — an "
            "unverified PR claim is an R82c violation")
    if pk in ("mock_stub", "ooc_metrics"):
        assert pv is False, (
            f"{path}: {pk} manifest must have pr_verified==False (R47b)")


def test_all_produced_manifests_are_pr_verified_consistent():  # AC-2b-3 (R82c)
    """LIVE: every manifest reachable in the session cache AND a fresh mock synthesis
    obeys the R82c pr_verified<->payload_kind consistency rule."""
    cache_dir = os.environ.get("PYRO_CACHE_DIR")
    assert cache_dir
    checked = 0
    for path, data in phase2_support.find_manifests(cache_dir):
        _assert_pr_verified_consistent(path, data)
        checked += 1
    with tempfile.TemporaryDirectory(prefix="pyro_ac2b3_cons_") as cache:
        phase1_support.run_worker(
            "prewarm_lifecycle", "[A-Za-z_]\\w*", "id_42 x9", cache_dir=cache)
        for path, data in phase2_support.find_manifests(cache):
            _assert_pr_verified_consistent(path, data)
            checked += 1
    assert checked >= 1, "no manifests were available to check consistency against"


# ==========================================================================
# load_partial surface (R86.5): mechanism failure => PyroLoadError, no leakage.
# ==========================================================================
def _make_config():
    """R86.6 JTAG/hw_server config built directly from the normative DeviceConfig
    field names.  Defaults already carry an hw_server locator that is not running on
    this host (R85); a frozen dataclass rejects unknown kwargs (no silent drop)."""
    return pdev.DeviceConfig(
        iface="enp175s0f0",
        expected_spec16=phase2_support.PYRO_SHELL_SPEC16,
    )


def test_load_partial_raises_pyro_load_error_on_mechanism_failure(tmp_path):
    # AC-2b-3 (R86.1/R86.5)
    """R86.5: a JTAG/hw_server mechanism failure MUST surface as PyroLoadError, not
    OSError/PermissionError or a transport internal (R86.1).  With no hw_server and
    a nonexistent bitstream path, the only reachable outcome is a load failure —
    this never touches a real device (device_usable=false)."""
    _need_pdev()
    bogus = str(tmp_path / "nonexistent_pyro_rp.bit")
    assert not os.path.exists(bogus)
    try:
        pdev.load_partial(_make_config(), bogus)
    except pdev.PyroLoadError:
        pass  # expected mechanism failure
    except (OSError, PermissionError) as exc:  # pragma: no cover - spec violation
        pytest.fail(f"load_partial leaked {type(exc).__name__} instead of "
                    f"PyroLoadError (R86.1/R86.5)")
    else:  # pragma: no cover - spec violation
        pytest.fail("load_partial returned success with no hw_server and a bogus "
                    "bitstream path (R86.5 must raise PyroLoadError)")


def test_pyro_load_error_taxonomy():  # AC-2b-3 (R86.1)
    _need_pdev()
    assert issubclass(pdev.PyroLoadError, pdev.PyroDeviceError)
    assert not issubclass(pdev.PyroLoadError, OSError)
