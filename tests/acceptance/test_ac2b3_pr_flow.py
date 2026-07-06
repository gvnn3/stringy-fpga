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
# R83a pr_flow_present evidence predicate (hermetic, all five arms + full-true).
# The probe consults the three R68 knobs (PYRO_PR_STATIC_DCP / PYRO_PR_REFERENCE_DCP /
# PYRO_PR_EVIDENCE_MANIFEST) in fixed order; the first unmet condition is named in the
# canonical `pr_flow_present=false — <first unmet R83a condition>` reason (R83/R83a).
# It flips TRUE iff the evidence manifest parses (R47b-consistency loader) and attests a
# verified pr_bitstream (payload_kind=='pr_bitstream' AND pr_verified==true).
# ==========================================================================
_STALE_MARKERS = ("PYRO_PR_FLOW", "not built/validated",
                  "not verifiable host-side")  # the retired v2.2.4 conservative literal

# R83a canonical first-unmet-condition reasons (exact spec 'Else:' wording).
_R83A_STATIC = ("pr_flow_present=false — static DCP not configured/unreadable "
                "(PYRO_PR_STATIC_DCP)")
_R83A_REFERENCE = ("pr_flow_present=false — reference DCP not configured/unreadable "
                   "(PYRO_PR_REFERENCE_DCP)")
_R83A_EVIDENCE_CFG = ("pr_flow_present=false — evidence manifest not "
                      "configured/unreadable (PYRO_PR_EVIDENCE_MANIFEST)")
_R83A_PARSE = "pr_flow_present=false — evidence manifest not parseable"
_R83A_NOT_VERIFIED = ("pr_flow_present=false — evidence manifest not pr_verified "
                      "(no verified pr_bitstream attested)")


def _assert_no_stale_placeholder(reason):
    """The sweep must retire every pre-R83a placeholder (PYRO_PR_FLOW env var, the
    'not built/validated' literal, and the v2.2.4 'not verifiable host-side' conservative
    literal) — R83a reasons name a concrete condition among the five."""
    assert reason.startswith("pr_flow_present=false — "), reason
    for marker in _STALE_MARKERS:
        assert marker not in reason, f"stale placeholder {marker!r} survived: {reason!r}"


def _fake_dcp(path):
    """A readable stand-in DCP (the probe only checks existence+readability at arms 1/2,
    R83a — DCP *contents* are Vivado-only and never inspected host-side)."""
    path.write_bytes(b"PK\x03\x04 fake vivado dcp container")
    return str(path)


def _write_manifest_json(path, *, pr_verified, payload_kind):
    """Write a schema-correct pyro.synth.Manifest JSON sidecar via the public API, so
    the R83a arm-4 loader (Manifest.from_json) parses it exactly as the spec defines."""
    m = psynth.Manifest(**_MANIFEST_BASE, pr_verified=pr_verified,
                        payload_kind=payload_kind)
    path.write_text(m.to_json(), encoding="utf-8")
    return str(path)


def _set_substrate(monkeypatch, tmp_path):
    """Configure arms 1+2 (both substrate DCPs present+readable) so a test can drive
    arms 3/4/5 in isolation."""
    monkeypatch.setenv("PYRO_PR_STATIC_DCP", _fake_dcp(tmp_path / "static.dcp"))
    monkeypatch.setenv("PYRO_PR_REFERENCE_DCP", _fake_dcp(tmp_path / "reference.dcp"))


# ---- Arm 1: static DCP not configured/unreadable -------------------------
def test_r83a_arm1_static_unset(monkeypatch):  # AC-2b-3 (R83a.1)
    """R83a step 1: with all knobs unset, the first unmet condition is the static DCP."""
    for k in ("PYRO_PR_STATIC_DCP", "PYRO_PR_REFERENCE_DCP", "PYRO_PR_EVIDENCE_MANIFEST"):
        monkeypatch.delenv(k, raising=False)
    present, reason = phase2_support.pr_flow_present()
    assert present is False
    _assert_no_stale_placeholder(reason)
    assert reason == _R83A_STATIC, reason


def test_r83a_arm1_static_configured_but_missing(tmp_path, monkeypatch):  # AC-2b-3 (R83a.1)
    """R83a step 1: a configured-but-unresolvable static DCP path is 'unreadable' — same
    canonical reason (the probe never raises, fail-closed)."""
    missing = tmp_path / "nonexistent_static.dcp"
    assert not missing.exists()
    monkeypatch.setenv("PYRO_PR_STATIC_DCP", str(missing))
    monkeypatch.setenv("PYRO_PR_REFERENCE_DCP", _fake_dcp(tmp_path / "reference.dcp"))
    monkeypatch.setenv("PYRO_PR_EVIDENCE_MANIFEST",
                       _write_manifest_json(tmp_path / "ev.json", pr_verified=True,
                                            payload_kind="pr_bitstream"))
    present, reason = phase2_support.pr_flow_present()
    assert present is False and reason == _R83A_STATIC, reason


# ---- Arm 2: reference DCP not configured/unreadable ----------------------
def test_r83a_arm2_reference_unset(tmp_path, monkeypatch):  # AC-2b-3 (R83a.2)
    """R83a step 2 (fixed order): static present+readable, reference unset → reference."""
    monkeypatch.setenv("PYRO_PR_STATIC_DCP", _fake_dcp(tmp_path / "static.dcp"))
    monkeypatch.delenv("PYRO_PR_REFERENCE_DCP", raising=False)
    monkeypatch.delenv("PYRO_PR_EVIDENCE_MANIFEST", raising=False)
    present, reason = phase2_support.pr_flow_present()
    assert present is False
    _assert_no_stale_placeholder(reason)
    assert reason == _R83A_REFERENCE, reason


# ---- Arm 3: evidence manifest not configured/unreadable ------------------
def test_r83a_arm3_evidence_unset(tmp_path, monkeypatch):  # AC-2b-3 (R83a.3)
    """R83a step 3: both DCPs present, evidence-manifest knob unset → evidence config."""
    _set_substrate(monkeypatch, tmp_path)
    monkeypatch.delenv("PYRO_PR_EVIDENCE_MANIFEST", raising=False)
    present, reason = phase2_support.pr_flow_present()
    assert present is False
    _assert_no_stale_placeholder(reason)
    assert reason == _R83A_EVIDENCE_CFG, reason


def test_r83a_arm3_evidence_configured_but_missing(tmp_path, monkeypatch):
    # AC-2b-3 (R83a.3)
    """R83a step 3: a configured-but-nonexistent evidence path is 'unreadable' — same
    canonical reason (before any parse is attempted)."""
    _set_substrate(monkeypatch, tmp_path)
    missing = tmp_path / "nonexistent_evidence.json"
    assert not missing.exists()
    monkeypatch.setenv("PYRO_PR_EVIDENCE_MANIFEST", str(missing))
    present, reason = phase2_support.pr_flow_present()
    assert present is False and reason == _R83A_EVIDENCE_CFG, reason


# ---- Arm 4: evidence manifest not parseable ------------------------------
def test_r83a_arm4_corrupt_json(tmp_path, monkeypatch):  # AC-2b-3 (R83a.4)
    """R83a step 4: a corrupt (non-JSON) evidence file fails Manifest.from_json → the
    probe catches it (never raises) and reports 'not parseable'."""
    _set_substrate(monkeypatch, tmp_path)
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("this is not json {{{", encoding="utf-8")
    monkeypatch.setenv("PYRO_PR_EVIDENCE_MANIFEST", str(corrupt))
    present, reason = phase2_support.pr_flow_present()
    assert present is False
    _assert_no_stale_placeholder(reason)
    assert reason == _R83A_PARSE, reason


def test_r83a_arm4_inconsistent_manifest_rejected(tmp_path, monkeypatch):
    # AC-2b-3 (R83a.4 / R47b-consistency)
    """R83a step 4 + R47b-consistency (v2.2.3): an internally-inconsistent manifest
    (pr_verified==True but payload_kind!='pr_bitstream') MUST be rejected by the
    R47b-consistency-enforcing loader, so the probe reports 'not parseable' — it is NOT
    silently accepted and MUST NOT flip true.  Built via raw JSON so it bypasses the
    (also-rejecting) constructor."""
    _set_substrate(monkeypatch, tmp_path)
    data = json.loads(psynth.Manifest(**_MANIFEST_BASE).to_json())
    data["pr_verified"] = True
    data["payload_kind"] = "ooc_metrics"        # inconsistent: verified claim, non-PR payload
    ev = tmp_path / "inconsistent.json"
    ev.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("PYRO_PR_EVIDENCE_MANIFEST", str(ev))
    present, reason = phase2_support.pr_flow_present()
    assert present is False, (
        "an inconsistent evidence manifest must NOT flip pr_flow_present true (R47b-"
        "consistency / R83a.4)")
    # Spec-correct disposition is arm 4 (rejected at from_json).  If a build ever failed
    # to enforce R47b-consistency, from_json would accept it and the probe would fall to
    # arm 5 (payload_kind!='pr_bitstream') — still false, still no false PASS.
    assert reason in (_R83A_PARSE, _R83A_NOT_VERIFIED), reason


# ---- Arm 5: evidence manifest not pr_verified ----------------------------
@pytest.mark.parametrize("pr_verified,payload_kind", [
    (False, "ooc_metrics"),   # honest metrics, no device claim (consistent)
    (False, "mock_stub"),     # mock payload (consistent)
])
def test_r83a_arm5_not_verified(tmp_path, monkeypatch, pr_verified, payload_kind):
    # AC-2b-3 (R83a.5)
    """R83a step 5: a parseable, CONSISTENT manifest that does not attest a verified
    pr_bitstream (payload_kind!='pr_bitstream' / pr_verified!=true) → 'not pr_verified'.
    """
    _set_substrate(monkeypatch, tmp_path)
    ev = _write_manifest_json(tmp_path / "ev.json", pr_verified=pr_verified,
                              payload_kind=payload_kind)
    monkeypatch.setenv("PYRO_PR_EVIDENCE_MANIFEST", ev)
    present, reason = phase2_support.pr_flow_present()
    assert present is False
    _assert_no_stale_placeholder(reason)
    assert reason == _R83A_NOT_VERIFIED, reason


# ---- Full-true polarity: all five R83a conditions satisfied --------------
def test_r83a_full_true_flips_present(tmp_path, monkeypatch):  # AC-2b-3 (R83a full)
    """R83a: with both substrate DCPs present+readable AND an evidence manifest that
    parses and attests payload_kind=='pr_bitstream' with pr_verified==true, the probe
    flips TRUE with an empty reason.  This is the sole host-observable true polarity
    (the passing-pr_verify proxy, R82c/R82d)."""
    _set_substrate(monkeypatch, tmp_path)
    ev = _write_manifest_json(tmp_path / "evidence.json", pr_verified=True,
                              payload_kind="pr_bitstream")
    monkeypatch.setenv("PYRO_PR_EVIDENCE_MANIFEST", ev)
    present, reason = phase2_support.pr_flow_present()
    assert present is True, (
        f"all five R83a conditions satisfied but probe stayed false: {reason!r}")
    assert reason == "", f"true polarity must carry an empty reason, got {reason!r}"


# ---- Opportunistic real-artifact check (non-hermetic; skips if absent) ----
def test_r83a_real_evidence_manifest_if_present(monkeypatch):  # AC-2b-3 (R83a.4/.5)
    """Opportunistic (non-hermetic): if the first real PR job has emitted an evidence
    manifest at /usr/local/cad/gn262/pyro/patterns/pattern_*_manifest.json, assert it
    satisfies R83a arms 4-5 (parses via the R47b-consistency loader AND attests a
    verified pr_bitstream).  Skips cleanly when no such artifact exists yet."""
    import glob
    candidates = sorted(glob.glob(
        "/usr/local/cad/gn262/pyro/patterns/pattern_*_manifest.json"))
    if not candidates:
        pytest.skip("no real PR evidence manifest present yet "
                    "(/usr/local/cad/gn262/pyro/patterns/pattern_*_manifest.json)")
    real = candidates[0]
    # Arm 4: parses via the R47b-consistency-enforcing loader without raising.
    with open(real, "r", encoding="utf-8") as fh:
        m = psynth.Manifest.from_json(fh.read())
    # Arm 5: attests a verified pr_bitstream.
    assert m.payload_kind == "pr_bitstream", (
        f"{real}: real evidence manifest payload_kind={m.payload_kind!r}, not "
        "'pr_bitstream' (R83a.5)")
    assert m.pr_verified is True, (
        f"{real}: real evidence manifest pr_verified!=True (R83a.5/R82c)")
    # And end-to-end: pointing the probe at it flips pr_flow_present true (needs the
    # substrate DCP knobs too; use the same real artifacts as the operator would).
    static = "/usr/local/cad/gn262/pyro/open-nic-shell/build/au250_pyro_pr/pr/pyro_static_locked.dcp"
    reference = "/usr/local/cad/gn262/pyro/open-nic-shell/build/au250_pyro_pr/pr/pyro_static_id_stub_routed.dcp"
    if not (os.path.isfile(static) and os.path.isfile(reference)):
        pytest.skip("real evidence manifest present but substrate DCPs absent")
    monkeypatch.setenv("PYRO_PR_STATIC_DCP", static)
    monkeypatch.setenv("PYRO_PR_REFERENCE_DCP", reference)
    monkeypatch.setenv("PYRO_PR_EVIDENCE_MANIFEST", real)
    present, reason = phase2_support.pr_flow_present()
    assert present is True, f"real R83a inputs present but probe false: {reason!r}"


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
    False (back-compat exactly like payload_kind).  Absent-key manifests are always
    mock_stub/ooc_metrics (pr_verified False), so they satisfy R47b-consistency."""
    data = json.loads(psynth.Manifest(**_MANIFEST_BASE).to_json())
    data.pop("pr_verified", None)
    assert "pr_verified" not in data
    back = psynth.Manifest.from_json(json.dumps(data))
    assert back.pr_verified is False


# --- R47b-consistency: boundary rejection of inconsistent manifests (v2.2.3) ---
# Enforcement is landing in parallel (coder); a manifest that is ACCEPTED where the
# spec requires rejection is reported as pending-implementation (xfail), a WRONG
# error type as a real failure, and correct rejection as PASS.
_INCONSISTENT = [
    (True, "ooc_metrics"),    # verified claim on a non-PR payload
    (True, "mock_stub"),      # verified claim on a mock payload
    (False, "pr_bitstream"),  # PR payload without a verified claim
]


def _assert_rejects(build):
    """R47b-consistency: `build()` MUST raise ValueError or a pyro.synth manifest
    error.  Accept either; distinguish pending-impl (accepted) from a wrong type."""
    try:
        build()
    except ValueError:
        return                                   # accepted taxonomy (R47b-consistency)
    except Exception as exc:  # noqa: BLE001
        if type(exc).__module__.startswith("pyro"):
            return                               # a pyro.synth manifest error is fine
        pytest.fail(
            f"R47b-consistency must raise ValueError / a pyro.synth manifest error, "
            f"got {type(exc).__module__}.{type(exc).__name__}: {exc}")
    else:
        pytest.xfail("R47b-consistency boundary rejection not yet landed (v2.2.3; "
                     "coder implementing in parallel)")


@pytest.mark.parametrize("pv,pk", _INCONSISTENT,
                         ids=[f"{pv}+{pk}" for pv, pk in _INCONSISTENT])
def test_manifest_ctor_rejects_inconsistent(pv, pk):  # AC-2b-3 (R47b-consistency)
    """R47b-consistency (v2.2.3): the Manifest constructor MUST reject a manifest
    violating pr_verified == (payload_kind == 'pr_bitstream')."""
    _assert_rejects(lambda: psynth.Manifest(**_MANIFEST_BASE, pr_verified=pv,
                                            payload_kind=pk))


@pytest.mark.parametrize("pv,pk", _INCONSISTENT,
                         ids=[f"{pv}+{pk}" for pv, pk in _INCONSISTENT])
def test_manifest_from_json_rejects_inconsistent(pv, pk):  # AC-2b-3 (R47b-consistency)
    """R47b-consistency (v2.2.3): Manifest.from_json MUST reject an inconsistent
    on-disk/hand-edited manifest, so corruption cannot enter the system."""
    # Build the JSON without going through the (also-rejecting) constructor.
    data = json.loads(psynth.Manifest(**_MANIFEST_BASE).to_json())
    data["pr_verified"] = pv
    data["payload_kind"] = pk
    _assert_rejects(lambda: psynth.Manifest.from_json(json.dumps(data)))


@pytest.mark.parametrize("pv,pk", [
    (True, "pr_bitstream"),   # verified PR bitstream — consistent
    (False, "ooc_metrics"),   # honest metrics — consistent
    (False, "mock_stub"),     # mock stub — consistent
])
def test_manifest_accepts_consistent(pv, pk):  # AC-2b-3 (R47b-consistency)
    """The consistent combinations MUST still be accepted by ctor and from_json."""
    m = psynth.Manifest(**_MANIFEST_BASE, pr_verified=pv, payload_kind=pk)
    assert m.pr_verified is pv and m.payload_kind == pk
    back = psynth.Manifest.from_json(m.to_json())
    assert back.pr_verified is pv and back.payload_kind == pk


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
