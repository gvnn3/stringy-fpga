"""R73a (v2.5.0) PR-link timing-gate scope + the A3.5 artifact-preservation fix.

R73a scopes the decisive post-route timing query of a ``pr_bitstream`` job to
the reconfigurable module (startpoint and/or endpoint in the ``pyro_rp`` cell,
axis_aclk 250 MHz domain — R73a.1), computes ``fmax_mhz`` from the scoped WNS
(R73a.2), fails the job on an EMPTY scoped path set (R73a.3), and keeps the
whole-design WNS as diagnostics only (R73a.6 — it MUST NOT feed ``met_timing``).

A3.5 (code bug, spec-independent): the partial-bitstream payload is read as
soon as the pr_verify gate passes — BEFORE any metric-parse/timing failure can
raise — and a post-verify failure preserves the workdir (the verified ``.bit``
+ reports) under a diagnostics path instead of ``rmtree``-destroying it.

Everything here runs WITHOUT Vivado: the Tcl is checked as text, and the
Python-side ordering/preservation is exercised against a fake ``vivado``
executable that fabricates the job dir the real flow would leave behind.
"""

import os
import tempfile

import pytest

import pyro.hdl as hdl
from pyro.synth import SynthesisFailed, ToolchainConfig
from pyro.synth.residency import _job_from_circuit
from pyro.synth.toolchain import VivadoToolchain

# ---------------------------------------------------------------------------
# 1. The generated TCL: scoped decisive query + empty-set failure branch.
# ---------------------------------------------------------------------------

TCL = VivadoToolchain._PR_FLOW_TCL


def test_pr_tcl_decisive_query_is_scoped_to_rp_cell_r73a1():
    # Both boundary directions are IN scope: a -from query AND a -to query
    # against the RP cell, setup analysis, axis_aclk domain filter.
    assert "-setup -from [_rp_cell]" in TCL
    assert "-setup -to [_rp_cell]" in TCL
    assert "GROUP == axis_aclk" in TCL
    # The scoped result carries its own marker, distinct from the whole-design one.
    assert "PYRO_METRIC:RP_WNS:" in TCL


def test_pr_tcl_empty_scoped_set_emits_failure_sentinel_r73a3():
    # An empty scoped path set must surface as the RP_WNS:NONE sentinel (which
    # the Python side maps to SynthesisFailed), never silently as a pass.
    assert "PYRO_METRIC:RP_WNS:NONE" in TCL
    # The sentinel is guarded by the emptiness check on the scoped set.
    scoped_guard = TCL.index("llength $_scoped] == 0")
    sentinel = TCL.index("PYRO_METRIC:RP_WNS:NONE")
    assert scoped_guard < sentinel


def test_pr_tcl_still_records_whole_design_wns_r73a6():
    # The unscoped whole-design query survives, for diagnostics only.
    assert "PYRO_METRIC:WNS:" in TCL
    # ... and the scoped (decisive) query comes first in the flow.
    assert TCL.index("PYRO_METRIC:RP_WNS:") < TCL.index(
        "set _p [get_timing_paths -quiet -max_paths 1 -nworst 1 -setup]\n")


def test_pr_tcl_substitution_leaves_no_sentinels():
    flow = (TCL.replace("@RPCELL@", "pyro_rp")
               .replace("@PART@", "xcu250-figd2104-2L-e")
               .replace("@STATIC_DCP@", "/x/static.dcp")
               .replace("@REFERENCE_DCP@", "/x/ref.dcp"))
    assert "@" not in flow


# ---------------------------------------------------------------------------
# 2. Python-side behavior against a fake vivado + fabricated job dir.
# ---------------------------------------------------------------------------

# The fake vivado fabricates exactly the on-disk state + stdout markers the
# real PR flow produces, in its cwd (the toolchain's workdir).  Modes (via the
# PYRO_TEST_FAKE_VIVADO_MODE env var, which reaches the subprocess because the
# adapter passes os.environ through):
#   ok           - pr_verify pass, scoped WNS +0.123, whole-design WNS -0.427
#   rp_none      - pr_verify pass, EMPTY scoped path set (R73a.3)
#   rp_fail      - pr_verify pass, scoped WNS -0.250 (genuine RM timing miss)
#   rp_fail_nobit- like rp_fail but no .bit on disk (ordering probe, A3.5)
#   no_util      - pr_verify pass + good .bit, but no utilization report
#   no_pass      - flow "succeeds" (exit 0) but pr_verify never passed
_FAKE_VIVADO = """\
#!/bin/sh
if [ "$1" = "-version" ]; then
    echo "vivado v2025.2 (64-bit)"
    exit 0
fi
mode="${PYRO_TEST_FAKE_VIVADO_MODE:-ok}"
printf '| CLB LUTs* | 123 |\\n| CLB Registers* | 456 |\\n' > util.rpt
printf 'whole-design timing summary (diagnostics)\\n' > timing.rpt
printf 'PR_VERIFY: the design checkpoints are compatible\\n' > pr_verify.rpt
printf 'FAKE_PARTIAL_BITSTREAM' > pyro_rp_partial.bit
if [ "$mode" = "no_pass" ]; then
    echo "PYRO_METRIC:DONE"
    exit 0
fi
echo "PYRO_METRIC:PR_VERIFY:PASS"
case "$mode" in
    rp_none)         echo "PYRO_METRIC:RP_WNS:NONE" ;;
    rp_fail)         echo "PYRO_METRIC:RP_WNS:-0.2500" ;;
    rp_fail_nobit)   rm -f pyro_rp_partial.bit
                     echo "PYRO_METRIC:RP_WNS:-0.2500" ;;
    no_util)         rm -f util.rpt
                     echo "PYRO_METRIC:RP_WNS:0.1230" ;;
    *)               echo "PYRO_METRIC:RP_WNS:0.1230" ;;
esac
echo "PYRO_METRIC:WNS:-0.4270"
echo "PYRO_METRIC:DONE"
exit 0
"""


@pytest.fixture
def pr_toolchain(tmp_path, monkeypatch):
    """A VivadoToolchain wired to the fake vivado, with all workdirs routed to
    an inspectable temp root.  Yields (toolchain, job, tmproot)."""
    vdir = tmp_path / "vivado_install"
    (vdir / "bin").mkdir(parents=True)
    exe = vdir / "bin" / "vivado"
    exe.write_text(_FAKE_VIVADO)
    exe.chmod(0o755)
    static = tmp_path / "static.dcp"
    static.write_bytes(b"locked static substrate")
    ref = tmp_path / "ref.dcp"
    ref.write_bytes(b"reference routed dcp")
    tmproot = tmp_path / "tmproot"
    tmproot.mkdir()
    # Route mkdtemp under our inspectable root so workdir fate is observable.
    monkeypatch.setattr(tempfile, "tempdir", str(tmproot))
    cfg = ToolchainConfig(
        kind="vivado", vivado_dir=str(vdir), pr_bitstream=True,
        static_dcp=str(static), reference_dcp=str(ref))
    job = _job_from_circuit(hdl.generate("abc[0-9]+"))
    return VivadoToolchain(cfg), job, tmproot


def _mode(monkeypatch, mode):
    monkeypatch.setenv("PYRO_TEST_FAKE_VIVADO_MODE", mode)


def _dirs(tmproot):
    return sorted(p for p in tmproot.iterdir() if p.is_dir())


def test_met_timing_and_fmax_follow_scoped_wns_r73a1_r73a2(
        pr_toolchain, monkeypatch):
    # THE point of A3: whole-design WNS is -0.427 (the flashed static's CMAC
    # violation), yet the job PASSES because the RP-scoped WNS (+0.123) gates.
    _mode(monkeypatch, "ok")
    tc, job, tmproot = pr_toolchain
    payload, manifest = tc.run(job)
    assert payload == b"FAKE_PARTIAL_BITSTREAM"
    assert manifest.payload_kind == "pr_bitstream"
    assert manifest.pr_verified is True
    assert manifest.met_timing is True                      # scoped, not -0.427
    assert manifest.fmax_mhz == pytest.approx(1000.0 / (4.0 - 0.123))  # R73a.2
    assert manifest.luts == 123 and manifest.ffs == 456
    # Success still cleans the workdir up (no preservation on the happy path).
    assert _dirs(tmproot) == []


def test_empty_scoped_path_set_fails_loudly_r73a3(pr_toolchain, monkeypatch):
    _mode(monkeypatch, "rp_none")
    tc, job, tmproot = pr_toolchain
    with pytest.raises(SynthesisFailed, match="R73a.3"):
        tc.run(job)


def test_rm_side_timing_miss_still_fails_r73a1(pr_toolchain, monkeypatch):
    # The narrower gate is still a gate: a genuine RM-scoped violation fails.
    _mode(monkeypatch, "rp_fail")
    tc, job, tmproot = pr_toolchain
    with pytest.raises(SynthesisFailed, match=r"RP-scoped WNS=-0\.250"):
        tc.run(job)


def test_payload_read_precedes_timing_gate_a35(pr_toolchain, monkeypatch):
    # Ordering probe: scoped timing FAILS *and* the .bit is missing.  With the
    # A3.5 ordering (read payload immediately after the pr_verify gate) the
    # missing-bitstream error fires FIRST; the pre-fix ordering would have
    # raised the timing miss instead.
    _mode(monkeypatch, "rp_fail_nobit")
    tc, job, tmproot = pr_toolchain
    with pytest.raises(SynthesisFailed) as ei:
        tc.run(job)
    assert "no partial bitstream" in str(ei.value)
    assert "RP-scoped WNS" not in str(ei.value)


@pytest.mark.parametrize("mode,expect", [
    ("rp_none", "R73a.3"),                 # empty scoped set (R73a.3)
    ("rp_fail", "timing not met"),         # genuine RM timing miss
    ("no_util", "no utilization report"),  # post-verify metric-parse failure
])
def test_post_verify_failure_preserves_verified_partial_a35(
        pr_toolchain, monkeypatch, mode, expect):
    # After pr_verify has passed, ANY failure must preserve the workdir (the
    # verified .bit + reports) under a diagnostics path, not rmtree it.
    _mode(monkeypatch, mode)
    tc, job, tmproot = pr_toolchain
    with pytest.raises(SynthesisFailed) as ei:
        tc.run(job)
    msg = str(ei.value)
    assert expect in msg
    dirs = _dirs(tmproot)
    assert len(dirs) == 1, "exactly the preserved workdir must remain"
    preserved = dirs[0]
    assert preserved.name.endswith("-preserved")
    # The exception tells the operator where the evidence lives.
    assert str(preserved) in msg
    # The pr_verify-passed partial and the reports survived intact.
    assert (preserved / "pyro_rp_partial.bit").read_bytes() == \
        b"FAKE_PARTIAL_BITSTREAM"
    assert (preserved / "pr_verify.rpt").is_file()
    assert (preserved / "timing.rpt").is_file()


def test_pre_verify_failure_still_cleans_up_r65(pr_toolchain, monkeypatch):
    # R65 semantics unchanged: a failure BEFORE the pr_verify gate passes
    # (here: no PASS marker at all) raises SynthesisFailed and the workdir is
    # destroyed as before — nothing worth preserving exists.
    _mode(monkeypatch, "no_pass")
    tc, job, tmproot = pr_toolchain
    with pytest.raises(SynthesisFailed, match="pr_verify did not pass"):
        tc.run(job)
    assert _dirs(tmproot) == []
