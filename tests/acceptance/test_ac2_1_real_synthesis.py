"""AC-2-1: real Vivado OOC synth + place-and-route with an honest manifest.

LIVE clauses (require toolchain_present, R70/R71):
  * a HW-eligible pattern is synthesized by REAL Vivado OOC synth+P&R for the
    target part; the manifest records payload_kind=="ooc_metrics" (R72a) with
    GENUINE post-route luts/ffs (>0) (R72c), met_timing True and fmax_mhz implied
    by met_timing at the 250 MHz proxy clock (R73), toolchain_version encoding the
    real Vivado 2025.2 (0x19020000, != mock 0x00000100) (R70a-pin/R75), fitting the
    R11 PR budget;
  * the artifact populates the bitstream cache and reloads WARM across a simulated
    process restart sharing PYRO_CACHE_DIR (R4/R63d).

SKIP clause (requires pr_flow_present, R71 — FALSE here):
  * production of a genuine loadable PR bitstream (payload_kind=="pr_bitstream").

NOTE (slow): each real Vivado run is minutes; the calibration corpus is
synthesized ONCE by the session-scoped `vivado_corpus` fixture and reused here and
in AC-2-3/AC-2-4.  When toolchain_present is false (the default mock config), the
fixture SKIPs with reason toolchain_present=false (R71: SKIP, never PASS).
(R11, R47b, R63, R70–R75, P1)
"""
import ctypes

import pytest

import phase2_support
from phase2_support import vivado_corpus  # noqa: F401 — shared session fixture


def _primary(corpus):
    """The entry for the first pattern whose vivado worker actually RAN (per-
    pattern crash isolation).  Fails clearly if every worker crashed."""
    pat = phase2_support.first_ran_pattern(corpus)
    if pat is None:
        errs = {p: e.get("worker_error") for p, e in corpus.items()}
        pytest.fail(f"no calibration pattern's vivado worker ran (all crashed): {errs}")
    return corpus[pat]


def _model_caps():
    """R11/R42 PR-region budget via the model binding's caps, or None if the C
    library is unavailable (then the budget-fit sub-assertion is skipped and fit
    is taken as implied by a successful ooc synthesis, R12)."""
    try:
        import abi_ctypes as abi
        lib = abi.load()
    except Exception:
        return None
    ctx = ctypes.c_void_p()
    if lib.pyro_ctx_open(ctypes.byref(ctx), b"model://") != abi.PYRO_OK:
        return None
    try:
        caps = abi.PyroCaps()
        if lib.pyro_caps_get(ctx, ctypes.byref(caps)) != abi.PYRO_OK:
            return None
        return {"pr_luts": caps.pr_luts, "pr_ffs": caps.pr_ffs,
                "pr_partitions": caps.pr_partitions}
    finally:
        lib.pyro_ctx_close(ctx)


def test_real_ooc_manifest_is_honest_ooc_metrics(vivado_corpus):
    """LIVE: the real Vivado flow writes exactly one ooc_metrics manifest carrying
    genuine post-route metrics (R72a/R72c)."""
    entry = _primary(vivado_corpus)
    assert entry["worker"]["synth_succeeded"] >= 1, entry["worker"]["last"]
    oocs = entry["all_manifests"]
    assert len(oocs) >= 1, (
        "real vivado run produced no ooc_metrics manifest in the cache "
        f"(cache_files={entry['worker']['cache_files']})")
    man = entry["manifest"]
    assert man["payload_kind"] == "ooc_metrics", (
        f"payload_kind must be 'ooc_metrics' on the vivado path, got "
        f"{man.get('payload_kind')!r} (R72a)")
    # R72c: genuine post-route utilization, not the estimator's numbers.
    assert isinstance(man["luts"], int) and man["luts"] > 0, man
    assert isinstance(man["ffs"], int) and man["ffs"] > 0, man


def test_real_ooc_met_timing_and_fmax_proxy(vivado_corpus):
    """LIVE: met_timing True at the 250 MHz proxy and fmax_mhz implied by met_timing
    (WNS>=0 => fmax = 1000/(4.000 - WNS) >= 250) (R73)."""
    man = _primary(vivado_corpus)["manifest"]
    assert man is not None, "no ooc_metrics manifest — real synthesis did not succeed (R72c)"
    assert man["met_timing"] is True, (
        "a synthesized+cached ooc artifact must have met timing at 250 MHz (R73); "
        "met_timing==False would fail synthesis -> permanent fallback (R65)")
    # R73: met_timing == (WNS >= 0) at the 4.000 ns constraint, so achieved Fmax
    # >= the 250 MHz target clock.
    assert float(man["fmax_mhz"]) >= float(phase2_support.PROXY_CLOCK_MHZ), (
        f"fmax_mhz {man['fmax_mhz']} < 250 MHz contradicts met_timing==True (R73)")


def test_real_toolchain_version_is_vivado_not_mock(vivado_corpus):
    """LIVE: toolchain_version encodes the pinned real Vivado 2025.2 (0x19020000)
    and differs from the mock sentinel (0x00000100), so mock/vivado artifacts
    occupy distinct cache keys (R70a-pin/R75/R75a).  Per R74a the stale 2023.1
    encoding (0x17010000) is NOT accepted as 2025.2 evidence."""
    man = _primary(vivado_corpus)["manifest"]
    assert man is not None, "no ooc_metrics manifest — real synthesis did not succeed (R72c)"
    tv = int(man["toolchain_version"])
    assert tv != phase2_support.MOCK_TOOLCHAIN_VERSION, (
        "vivado manifest must NOT report the mock toolchain_version (R75)")
    assert tv != phase2_support.VIVADO_2023_1_TOOLCHAIN_VERSION, (
        "vivado manifest must NOT report the stale 2023.1 toolchain_version "
        "(0x17010000) — not valid 2025.2 evidence (R74a)")
    assert tv == phase2_support.VIVADO_TOOLCHAIN_VERSION, (
        f"toolchain_version {tv:#010x} != pinned Vivado 2025.2 encoding "
        f"{phase2_support.VIVADO_TOOLCHAIN_VERSION:#010x} (R70a-pin/R75)")
    # SHELL_VERSION stays the model-harness value until a real PR flow exists.
    if "shell_version" in man:
        assert int(man["shell_version"]) == phase2_support.SHELL_VERSION_MODEL, (
            "shell_version must remain the model-harness value until pr_flow "
            "present (R75)")


def test_real_ooc_manifest_fits_pr_budget(vivado_corpus):
    """LIVE: genuine post-route utilization fits the advertised R11 PR-region
    budget (R11/R12).  Budget from the model caps when the C lib is available;
    otherwise fit is implied by a successful ooc synthesis (R12)."""
    man = _primary(vivado_corpus)["manifest"]
    assert man is not None, "no ooc_metrics manifest — real synthesis did not succeed (R72c)"
    caps = _model_caps()
    if caps is None:
        pytest.skip("model caps unavailable (libpyro_rt.so not built); budget-fit "
                    "is implied by a successful ooc synthesis (R12)")
    assert man["luts"] <= caps["pr_luts"], (
        f"post-route luts {man['luts']} exceed PR budget {caps['pr_luts']} (R11)")
    assert man["ffs"] <= caps["pr_ffs"], (
        f"post-route ffs {man['ffs']} exceed PR budget {caps['pr_ffs']} (R11)")


def test_real_artifact_reloads_warm_across_restart(vivado_corpus):
    """LIVE: a FRESH process sharing PYRO_CACHE_DIR finds the vivado artifact WARM
    (or resident) WITHOUT re-launching synthesis — the cold->warm transition
    happens at most once per (toolchain_version) key (R4 invariant / R63d)."""
    import phase1_support
    pat = phase2_support.first_ran_pattern(vivado_corpus)
    if pat is None:
        pytest.fail("no calibration pattern's vivado worker ran (all crashed)")
    entry = vivado_corpus[pat]
    read, _o, _e = phase1_support.run_worker(
        "persist_read", pat, cache_dir=entry["cache"],
        extra_env={"PYRO_TOOLCHAIN": "vivado",
                   "PYRO_VIVADO": phase2_support._vivado_install_dir()},
        timeout=120)
    assert read["initial_status"] in ("warm", "resident"), (
        f"fresh process saw {read['initial_status']} — vivado artifact did not "
        "persist warm across restart (R4/R63d)")
    assert read["synth_launched"] == 0, (
        "fresh process re-launched synthesis instead of reusing the warm vivado "
        "artifact (R4 invariant)")


def test_no_ooc_manifest_claims_pr_bitstream(vivado_corpus):
    """R72/R72c honesty: with pr_flow_present false, NO manifest may claim to be a
    loadable device bitstream (payload_kind must never be 'pr_bitstream' here)."""
    for entry in vivado_corpus.values():
        for _p, man in phase2_support.find_manifests(entry["cache"]):
            assert man.get("payload_kind") != "pr_bitstream", (
                "no loadable PR bitstream can be produced without pr_flow_present "
                f"(R72); manifest claims pr_bitstream: {man}")


def test_loadable_pr_bitstream_requires_pr_flow():
    """SKIP clause: production of a genuine loadable PR bitstream
    (payload_kind=='pr_bitstream') requires pr_flow_present, FALSE here (R71/R72)."""
    present, reason = phase2_support.pr_flow_present()
    if not present:
        pytest.skip(reason)
    # Unreachable on this host; a real PR flow would assert a pr_bitstream artifact.
    raise AssertionError("pr_flow_present unexpectedly true — implement PR-bitstream "
                         "assertion (R72 pr_bitstream)")
