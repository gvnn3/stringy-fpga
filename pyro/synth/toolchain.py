"""Mock synthesis toolchain (spec §7.5 R63b).

R63b requires that, when no real Vivado flow is present (F5) and **in all tests**,
the synthesis service run a **mock toolchain** that consumes the generator's RTL
+ metadata and emits a **stub PR artifact + manifest** (R47b) for the software
model (R7), so every phase is testable without Vivado.

The mock is a pure, deterministic function of its :class:`SynthJob`: the same job
yields a byte-identical stub artifact and an identical manifest.  It supports two
fault modes for the R65 failure-semantics tests:

  * ``"error"``   — the toolchain raises :class:`SynthesisFailed` (does-not-fit /
    timing-fail / tool-error), and
  * ``"timeout"`` — the toolchain reports a timeout outcome (R63e per-job
    timeout fired).

A configurable ``latency`` (default **0.0**, near-zero for fast tests) models the
minutes-long real flow without actually waiting.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import artifact as _artifact
from .manifest import Manifest, payload_crc32

# Stock compiler captured at ``import pyro`` time (pyro._model is imported
# transitively by pyro/__init__, always before install() can patch stdlib re).
# This module is imported LAZILY — possibly while pyro.install() is active —
# so module-level ``re.compile`` here would capture the *patched* compiler and
# route pyro's own report-parsing regexes through the dispatch layer (spurious
# R66 stats / reuse ticks, launch-policy pollution; AC-3-1 counter-wedge
# review).  ``re`` is still imported above for the (unpatched) flag constants.
from .._model import _stock_compile

# R73a.6 job diagnostics channel (whole-design WNS record, never a gate).
_log = logging.getLogger("pyro.synth.toolchain")

# Pinned tool/shell versions — part of the R4/R47b cache key precisely because
# artifacts are NOT portable across tool/shell versions (P11).  A real Phase-2
# flow substitutes the true Vivado + open-nic-shell versions here.
TOOLCHAIN_VERSION = 0x00000100   # mock toolchain v0.1.0 (packed)
SHELL_VERSION = 0x0A000001       # OpenNIC target shell / PR-region id (opaque)

# R75/R70a-pin: the vivado toolchain's toolchain_version encodes the actual
# Vivado version, packed (YY << 24) | (RR << 16) | build.  For the pinned
# **Vivado 2025.2** (R70a-pin, v2.2.1) this is 0x19020000 (YY=25=0x19, RR=2,
# build=0).  Used as the R4/R47b cache-key component for the vivado kind, keeping
# mock (0x00000100) and vivado artifacts on distinct keys (R75a).  The adapter
# re-derives the concrete value from the tool's own `vivado -version` at run time,
# but the host-side key needs a static pin, so this constant records the pinned
# install (2025.2).  (The prior 2023.1 pin 0x17010000 is retired: 2023.1 segfaults
# at batch-process exit on this host's Ubuntu 24.04 / glibc 2.39, corrupting the
# exit-code integrity R77 relies on — see R70a-pin and the v2.2.1 changelog.)
VIVADO_TOOLCHAIN_VERSION = 0x19020000

# R70a-pin: the pinned Vivado 2025.2 install directory (normative, v2.2.1).  This
# records the pinned location for documentation/`load_partial` (R86.5); it is NOT
# a resolution default for the vivado synth adapter — R70 forbids library-side
# defaulting/scanning of PYRO_VIVADO, so the adapter still resolves its install
# dir from the sampled ToolchainConfig.vivado_dir (None => unavailable).
PINNED_VIVADO_DIR = "/usr/local/cad/2025.2/Vivado"

# R77: default per-job Vivado timeout (30 min, OOC); overridable via ToolchainConfig.
VIVADO_JOB_TIMEOUT = 1800.0

# R84 (v2.2.2): the per-job timeout for a **pr_bitstream** job (60 min).  A PR job
# includes a full in-context place-and-route link against the locked static plus
# pr_verify, and is heavier than the OOC-only job governed by R77 — hence the
# distinct, larger default.  The R77 kill-the-process-tree discipline applies to
# PR jobs verbatim.  Overridable via ToolchainConfig.pr_job_timeout_s.
VIVADO_PR_JOB_TIMEOUT = 3600.0


class SynthesisFailed(Exception):
    """The (mock) toolchain could not produce a fitting/timing-clean artifact."""


class ConfigurationError(Exception):
    """A build *input* is inconsistent — NOT a synthesis outcome (R65).

    The distinction is load-bearing, not cosmetic.  :class:`SynthesisFailed`
    means "this design does not fit / does not close timing on this toolchain",
    which :mod:`pyro.synth.service` records as a **permanent** negative cache
    entry (R65) keyed by the R4/SR9 bitstream key; residency then treats the
    key as terminal (``TIER_FALLBACK_ONLY``, resubmission refused).  That is
    correct for a property of the design.

    An inconsistent job — an engine/wrapper pairing mismatch, say — is a
    property of the *caller*, not of the design, and it is invisible to the
    cache key (``engine_backpressure`` and the engine's own datapath width are
    nowhere in ``descriptor_key``).  Caching it as an R65 failure would
    launder a fixable configuration gap into an indistinguishable,
    permanent "this group does not fit" verdict **on the very key the fix
    needs** — the group could then never be built, at any generator version.
    So this exception is reported to the caller and deliberately NOT cached.
    """


@dataclass(frozen=True)
class SynthJob:
    """A self-contained synthesis job (primitives only, so it is picklable and
    crosses the process boundary to the out-of-process service, R63).

    Carries everything the mock toolchain needs — the RTL text and the metadata
    plumbed from :class:`pyro.hdl.GeneratedCircuit` — without pickling the live
    automaton object.
    """

    # identity / key components (R47a/R4)
    pattern_hash: str                 # hex of the 16-byte CIRC_ID* hash
    encoding: int
    effective_flags: int
    circ_flags: int
    generator_version: int
    harness_version: int
    # RTL + resource estimate (from the generator/estimator)
    rtl: str
    luts: int
    ffs: int
    bram_kb: int
    dsps: int
    # R19c over-approximation declaration
    over_approx_classes: Tuple[str, ...] = ()
    estimated_fp_rate: float = 0.0
    # Serialized automaton body (Task-7 extension) — the state/edge tables the
    # native C model (src/pyro_rt.c) executes, from
    # :func:`pyro.synth.artifact.serialize_automaton_body`.  Empty for a job
    # built without a live automaton (the artifact then carries only its header).
    automaton_table: bytes = b""
    # P2b widened datapath (R42): bytes/cycle of the engine in `rtl`.  The PR
    # flow emits the matching wrapper (generate_rp_child datapath_bytes); 1 =
    # the original single-byte engine + v2 wrapper, byte-identical to pre-P2b.
    datapath_bytes: int = 1
    # SNORT-PF SR7: does the engine in `rtl` back-pressure via `in_ready`?
    # ``None`` (the default) means DERIVE IT FROM THE RTL — the engine text
    # either declares the port or it does not, so this is not an independent
    # degree of freedom and a hand-assembled group job cannot forget it into a
    # deaf wrapper.  An explicit ``True``/``False`` that contradicts the RTL is
    # a :class:`ConfigurationError`, never a cacheable R65 failure.
    engine_backpressure: Optional[bool] = None


@dataclass(frozen=True)
class ToolchainConfig:
    """Behavioral configuration for the synthesis toolchain (frozen, picklable).

    R70a: the selected toolchain kind + its parameters cross the process boundary
    to the out-of-process worker (R63) inside this dataclass.  The Phase-2 fields
    are **additive with mock-preserving defaults**, so a ``ToolchainConfig()`` with
    no overrides is byte-identical to the pre-2.1.0 default (kind ``"mock"``) and
    Phase-0/1 behavior is unchanged.
    """

    # -- mock behavioral knobs (R63b; test-tunable) -------------------------
    latency: float = 0.0              # seconds to model the (minutes-long) flow
    fail_mode: str = "ok"             # "ok" | "error" | "timeout"
    fmax_mhz: float = 300.0           # achieved Fmax the mock reports
    met_timing: bool = True
    # -- toolchain selection + real-flow parameters (R70a, additive) --------
    kind: str = "mock"                # "mock" | "vivado"
    vivado_dir: Optional[str] = None  # Vivado install dir (PYRO_VIVADO); None=absent
    part: str = "xcu250-figd2104-2L-e"    # target U250 part (R71)
    target_clock_mhz: float = 250.0   # OOC clock constraint (R73 proxy)
    job_timeout_s: float = VIVADO_JOB_TIMEOUT  # per-job OOC timeout (R77)
    # -- PR (pr_bitstream) mode + its R82d substrate (v2.2.3/v2.2.4, additive) --
    # R88 (v2.2.4) blesses this mechanism: `pr_bitstream` is the explicit,
    # construction-pinned (R70b) request for the R82 PR link flow.  When True the
    # adapter REQUIRES the R82d substrate paths below — an absent/unresolvable path
    # is a loud SynthesisFailed (R88/R82c/R65), NEVER a silent fall-back to
    # ooc_metrics (R88/R70a honesty).  static_dcp/reference_dcp are populated from
    # the R68 env knobs PYRO_PR_STATIC_DCP/PYRO_PR_REFERENCE_DCP (v2.2.4), sampled
    # at the R35a points and passed through by the residency service.
    pr_bitstream: bool = False        # R88: request the R82 pr_bitstream flow
    static_dcp: Optional[str] = None  # R82b/R88 locked static DCP (linking substrate)
    reference_dcp: Optional[str] = None  # R82c/R82d/R88 reference routed DCP for pr_verify
    # R78.9a (v2.6.0/P2c): wrapper frame-buffer size for the PR flow.  Coupled
    # to the SUBSTRATE (the shell the static DCP was built from): 1536 for a
    # MAX_PKT_LEN=1518 shell (default, pre-P2c-identical), 9600 for a jumbo
    # shell's DCPs.  Mismatches are safe but wasteful/limiting (see
    # rp_wrapper.generate_rp_child docstring).
    rp_max_frame_bytes: int = 1536
    # P2e (v2.7.0): v4 frame-parallel core count for the wrapper.  1 =
    # classic single-core emission (byte-identical); 2..4 = the demux/mux
    # top with N unmodified cores (aggregate B/cyc scales by core count —
    # the recurrence caps a single engine's width).
    rp_cores: int = 1
    # R68/R83a (v2.2.5) evidence-manifest path: consulted ONLY by the R83a
    # pr_flow_present availability predicate, never by a synthesis job; absence
    # keeps the report false rather than failing anything.
    pr_evidence_manifest: Optional[str] = None
    rp_cell: str = "pyro_rp"          # reconfigurable-partition cell name (R80 boundary)
    # R84 (v2.2.4) names BOTH per-job timeout fields: job_timeout_s (OOC, 1800 s,
    # R77) above and pr_job_timeout_s (PR, 3600 s) here; the adapter selects the PR
    # field when pr_bitstream == True (R88), else the OOC field.
    pr_job_timeout_s: float = VIVADO_PR_JOB_TIMEOUT  # per-job PR timeout (R84)
    # -- PR timing-closure strategy (additive; None/False = the exact
    #    pre-knob flow, byte-identical TCL).  Vivado P&R is deterministic
    #    for identical inputs, so a link that misses timing by a hair
    #    re-runs to the identical miss; a RETRY must vary the strategy.
    #    These knobs do not enter any SR9/R47a identity or cache key — any
    #    met-timing artifact is equally valid for its key; the strategy is
    #    how the tool got there, not what was built.  (S3 evidence,
    #    2026-07-29: 3/7 group links missed at -0.046..-0.120 ns on
    #    route-dominated RM<->static boundary paths; default flow has no
    #    phys_opt_design step at all.)
    pr_place_directive: Optional[str] = None   # place_design -directive X
    pr_route_directive: Optional[str] = None   # route_design -directive X
    pr_phys_opt: bool = False                  # phys_opt_design post-place
                                               # + post-route


class MockToolchain:
    """Deterministic RTL -> (stub bitstream, R47b manifest) mock (R63b)."""

    def __init__(self, config: ToolchainConfig = ToolchainConfig()):
        self.config = config

    def run(self, job: SynthJob) -> Tuple[bytes, Manifest]:
        """Synthesize ``job`` to a stub artifact + manifest, or raise.

        Deterministic: the payload is a hash-derived stub over the RTL + identity,
        so the same job always yields byte-identical output.  Honors the
        configured latency and fault mode (R63b/R65/R63e).
        """
        cfg = self.config
        if cfg.latency:
            time.sleep(cfg.latency)
        if cfg.fail_mode == "error":
            raise SynthesisFailed("mock toolchain: RTL does not fit / fails timing")
        if cfg.fail_mode == "timeout":
            raise SynthesisFailed("mock toolchain: per-job timeout (R63e)")

        payload = _stub_payload(job)
        manifest = Manifest(
            pattern_hash=job.pattern_hash,
            encoding=job.encoding,
            effective_flags=job.effective_flags,
            circ_flags=job.circ_flags,
            generator_version=job.generator_version,
            harness_version=job.harness_version,
            toolchain_version=TOOLCHAIN_VERSION,
            shell_version=SHELL_VERSION,
            luts=job.luts,
            ffs=job.ffs,
            bram_kb=job.bram_kb,
            dsps=job.dsps,
            fmax_mhz=cfg.fmax_mhz,
            met_timing=cfg.met_timing,
            over_approx_classes=list(job.over_approx_classes),
            estimated_fp_rate=job.estimated_fp_rate,
            integrity_hash=payload_crc32(payload),
            payload_len=len(payload),
            payload_kind="mock_stub",   # R72a: metrics configured, not measured
        )
        return payload, manifest


def _stub_payload(job: SynthJob) -> bytes:
    """The deterministic PR bitstream artifact payload (``artifact.bin``).

    Not a real Xilinx bitstream — a real Phase-2 flow replaces this — but a
    **self-describing binary artifact** (:mod:`pyro.synth.artifact`) that carries
    the R47a/R47b header (baked identity, target shell/harness, CRC-32 integrity
    trailer) plus the serialized automaton the native C model executes.  Stable
    for a fixed job, so the mock toolchain stays deterministic (R63b).
    """
    return _artifact.build_artifact(
        pattern_hash16=bytes.fromhex(job.pattern_hash),
        circ_flags=job.circ_flags,
        encoding=job.encoding,
        effective_flags=job.effective_flags,
        generator_version=job.generator_version,
        harness_version=job.harness_version,
        shell_version=SHELL_VERSION,
        body=job.automaton_table,
    )


# ============================================================================
# Real Vivado out-of-context (OOC) toolchain (spec §7.6 R70-R77)
# ============================================================================
#
# The `vivado` adapter drives a real Vivado batch flow (read_verilog + XDC ->
# synth_design -mode out_of_context -> opt/place/route -> report_utilization +
# report_timing_summary) for the physical U250 part, parses the genuine
# post-route metrics, and returns them in the manifest (R72c).  It is duck-typed
# to the same `run(job) -> (payload, Manifest)` contract as MockToolchain and is
# selected at the service seam (service.py) when config.kind == "vivado".
#
# Honesty (R72): no PR bitstream can be produced on this host (pr_flow_present ==
# false), so the payload is the SAME model-exec PYROART1 container the mock emits
# (the software model executes it, R51b/R72); only the manifest metrics are real,
# and the manifest is tagged payload_kind="ooc_metrics" so nothing claims a
# device load (R72a/R72b).
#
# Fail-safe (R70/R71): if PYRO_VIVADO does not resolve to a runnable vivado, the
# adapter is *unavailable* — run() raises SynthesisFailed, which the service maps
# to permanent fallback (R65).  The mock is NEVER silently substituted (that
# would serve a mock stub under a vivado cache key, R75a).
#
# Every failure mode (tool error, unparseable report, timing miss, timeout) maps
# to SynthesisFailed; run() never lets any other exception escape (R63e/R65).

# report_utilization row parsers ("| CLB LUTs | 197 | ..." style tables).
# 2025.2 renders the row as "CLB LUTs*" (footnote asterisk) in both flat and
# -cells-scoped reports (verified empirically on this host, W3-b); the marker
# is absent in other configurations, so it is optional here.
_RE_CLB_LUTS = _stock_compile(r"^\|\s*CLB LUTs\*?\s*\|\s*(\d+)\s*\|", re.MULTILINE)
_RE_CLB_FFS = _stock_compile(r"^\|\s*CLB Registers\*?\s*\|\s*(\d+)\s*\|", re.MULTILINE)
# WNS marker emitted by the flow tcl.  Vivado's `format %.4f` guarantees a plain
# fixed-point decimal ("PYRO_METRIC:WNS:2.4050"), so no exponent/odd formatting
# can slip past this regex; the sentinel "NONE" (no setup timing paths) is
# matched separately and mapped to SynthesisFailed (R65).
_RE_WNS = _stock_compile(r"^PYRO_METRIC:WNS:(-?\d+\.\d+)\s*$", re.MULTILINE)
_RE_WNS_NONE = _stock_compile(r"^PYRO_METRIC:WNS:NONE\s*$", re.MULTILINE)
# R73a.1 (v2.5.0): the RP-scoped WNS marker for the PR link flow — the DECISIVE
# post-route WNS, taken over exactly the reconfigurable module's paths
# (startpoint and/or endpoint in the pyro_rp cell, axis_aclk domain).  Same
# fixed-point `format %.4f` guarantee as the whole-design WNS marker above.
_RE_RP_WNS = _stock_compile(r"^PYRO_METRIC:RP_WNS:(-?\d+\.\d+)\s*$", re.MULTILINE)
# R73a.3: an EMPTY scoped path set is a job failure, not a pass.
_RE_RP_WNS_NONE = _stock_compile(r"^PYRO_METRIC:RP_WNS:NONE\s*$", re.MULTILINE)
# R82c: emitted only after pr_verify -full_check passes (the PR flow's hard gate).
_RE_PR_VERIFY_PASS = _stock_compile(r"^PYRO_METRIC:PR_VERIFY:PASS\s*$", re.MULTILINE)
# `vivado -version` first line: "vivado v2025.2 (64-bit)" (R70a-pin).  The
# regex is release-agnostic (major.minor), so it also parses the retired 2023.1.
_RE_VIVADO_VER = _stock_compile(r"v(\d+)\.(\d+)")


# NOTE (R70a-pin, v2.2.1): the pinned **Vivado 2025.2** officially supports this
# host OS (Ubuntu 24.04 / glibc 2.39) and starts cleanly with **no libtinfo.so.5
# shim** — verified on this host.  The prior ``_libtinfo5_shim`` helper (needed by
# the retired 2023.1 pin, which dlopen'd the missing ``libtinfo.so.5`` SONAME) is
# therefore removed and no shim is applied to the 2025.2 subprocess environment.


class VivadoToolchain:
    """Real OOC synth+P&R adapter (R70-R77).  Duck-typed to MockToolchain."""

    # The Vivado batch script.  create_clock is applied via read_xdc *after*
    # synth_design (get_ports needs the elaborated netlist); the WNS is emitted
    # as a machine-readable marker while the enumerated report files are still
    # written for the audit trail (report_utilization / report_timing_summary).
    # NB: use a @PART@ sentinel + str.replace (NOT str.format): the tcl body
    # contains literal Tcl braces ({llength ...}) that str.format would misparse.
    _FLOW_TCL = (
        "read_verilog design.v\n"
        "synth_design -top pyro_circuit -part @PART@ -mode out_of_context\n"
        "read_xdc constrs.xdc\n"
        "opt_design\n"
        "place_design\n"
        "route_design\n"
        "report_utilization -file util.rpt\n"
        "report_timing_summary -file timing.rpt\n"
        "set _p [get_timing_paths -max_paths 1 -nworst 1 -setup]\n"
        "if {[llength $_p] == 0} {\n"
        "    puts \"PYRO_METRIC:WNS:NONE\"\n"
        "} else {\n"
        "    set _wns [get_property SLACK [lindex $_p 0]]\n"
        "    puts \"PYRO_METRIC:WNS:[format %.4f $_wns]\"\n"
        "}\n"
        "puts \"PYRO_METRIC:DONE\"\n"
    )

    # R82 DFX partial-bitstream (pr_bitstream) link flow.  Substitution uses
    # @SENTINEL@ + str.replace (NOT str.format): the Tcl body has literal braces.
    #   1. OOC-synth the wrapped RP child (pyro_rp instantiating the engine).
    #   2. Open the LOCKED static DCP (R82b substrate); black-box + read the RM
    #      into the reconfigurable cell (R80), implement in-context.
    #   3. Timing (R73/R73a): the DECISIVE post-route query is scoped to the
    #      reconfigurable module (startpoint and/or endpoint in the RP cell,
    #      axis_aclk 250 MHz domain, R73a.1); an empty scoped set fails the job
    #      (R73a.3).  The whole-design WNS is emitted too, but as job
    #      diagnostics ONLY (R73a.6) — it never feeds met_timing.
    #   4. pr_verify against the reference routed DCP (R82c) — HARD GATE: a
    #      failure errors the job (non-zero exit) => SynthesisFailed (R65).
    #   5. write_bitstream -cell => the partial .bit (R85 loadable artifact).
    _PR_FLOW_TCL = (
        "read_verilog -sv design.v\n"
        "read_verilog -sv pyro_rp.sv\n"
        "synth_design -top @RPCELL@ -part @PART@ -mode out_of_context "
        "-verilog_define PYRO_BUILD16=0\n"
        "write_checkpoint -force rm_synth.dcp\n"
        "close_design\n"
        "open_checkpoint @STATIC_DCP@\n"
        # In the LOCKED static DCP (R82b) the RP cell is already a black box, so
        # REF_NAME/ORIG_REF_NAME no longer read "@RPCELL@" — the property that
        # survives lock_design is HD.RECONFIGURABLE (empirically verified against
        # this host's locked DCP; the finish-lock flow locates the cell the same
        # way).  Fall back to the ref-name match for a non-blackboxed substrate.
        "set _rp [get_cells -hierarchical -filter {HD.RECONFIGURABLE == 1}]\n"
        "if {[llength $_rp] != 1} {\n"
        "    set _rp [get_cells -hierarchical -filter "
        "{ORIG_REF_NAME == \"@RPCELL@\" || REF_NAME == \"@RPCELL@\"}]\n"
        "}\n"
        "if {[llength $_rp] != 1} {\n"
        "    error \"PYRO_PR: expected exactly one @RPCELL@ cell, found: $_rp\"\n"
        "}\n"
        # Netlist mutations invalidate cached cell objects (and glob chars like
        # g_intf[0] break bare get_cells patterns), so keep the immutable NAME
        # string and re-query with an exact -filter match after every mutation.
        "set _rp_name [get_property NAME $_rp]\n"
        "proc _rp_cell {} { upvar #0 _rp_name n; "
        "return [get_cells -hierarchical -filter \"NAME == $n\"] }\n"
        "if {![get_property IS_BLACKBOX [_rp_cell]]} {\n"
        "    update_design -cell [_rp_cell] -black_box\n"
        "}\n"
        "read_checkpoint -cell [_rp_cell] rm_synth.dcp\n"
        "opt_design\n"
        # Timing-closure knobs (additive; all three sentinels substitute to
        # "" by default, keeping this flow byte-identical to the pre-knob
        # text).  Needed because Vivado P&R is DETERMINISTIC for identical
        # inputs: a job that misses timing by -0.05 ns re-runs to the exact
        # same -0.05 ns, so a retry MUST vary the strategy (measured
        # 2026-07-29: 3 of the first 7 S3 group links missed by
        # -0.046..-0.120 ns on route-dominated RM<->static boundary paths).
        "place_design@PLACE_DIRECTIVE@\n"
        "@POST_PLACE_PHYS_OPT@"
        "route_design@ROUTE_DIRECTIVE@\n"
        "@POST_ROUTE_PHYS_OPT@"
        # W3: scope utilization to the RM cell so PR manifests report the PATTERN's
        # resources (consistent with the OOC path / R74), not static+RM whole-device.
        "report_utilization -cells [_rp_cell] -file util.rpt\n"
        "report_timing_summary -file timing.rpt\n"
        # R73a.1: the DECISIVE post-route timing query is scoped to the
        # reconfigurable module — setup paths whose startpoint AND/OR endpoint
        # lies in the RP cell (both boundary directions IN scope).  Paths lying
        # entirely in the static region are OUT of scope: they are the static
        # build's flash-time record (R73a.4/R73a.5), not a per-pattern fact.
        #
        # NB (2026-07-15, first real HW partial): a hierarchical cell's own pins
        # are timing *through* points, not start/endpoints, so
        # `get_timing_paths -from/-to [_rp_cell]` returns ZERO paths — it must be
        # scoped over the RP's LEAF cells (-from/-to) plus its boundary pins
        # (-through) to capture intra-RM and static<->RM boundary paths.  The RP
        # is single-clock (the axis_aclk user-box 250 MHz domain, F4), so every
        # cell-scoped path is in that domain by construction; the previous
        # literal `GROUP == axis_aclk` filter both mis-named the routed clock
        # (it is axis_aclk_0) and was redundant with the cell scope — it emptied
        # the set and tripped R73a.3 on a partial that in fact met timing
        # (RM WNS +0.023 ns).  If the RP ever becomes multi-clock, re-add a
        # domain guard here (R73a.4).
        # -quiet: an empty result is handled explicitly below (R73a.3), never
        # papered over by tool warnings.
        "set _rpn [get_property NAME [_rp_cell]]\n"
        "set _leaves [get_cells -quiet -hierarchical "
        "-filter \"PRIMITIVE_LEVEL == LEAF && NAME =~ $_rpn/*\"]\n"
        "set _bpins [get_pins -quiet -of_objects [_rp_cell]]\n"
        "set _pf [get_timing_paths -quiet -max_paths 1 -nworst 1 -setup "
        "-from $_leaves]\n"
        "set _pt [get_timing_paths -quiet -max_paths 1 -nworst 1 -setup "
        "-to $_leaves]\n"
        "set _pb [get_timing_paths -quiet -max_paths 1 -nworst 1 -setup "
        "-through $_bpins]\n"
        "set _scoped [concat $_pf $_pt $_pb]\n"
        "if {[llength $_scoped] == 0} {\n"
        # R73a.3: an empty scoped path set is a FAILURE, not a pass — a
        # narrower gate must not degrade into no gate.  Emit the sentinel (the
        # Python side raises SynthesisFailed on it) and let the flow continue,
        # so pr_verify/write_bitstream still produce the diagnostics artifacts
        # the A3.5 preservation path keeps for the post-mortem.
        "    puts \"PYRO_METRIC:RP_WNS:NONE\"\n"
        "} else {\n"
        "    set _rp_wns 1.0e9\n"
        "    foreach _pp $_scoped {\n"
        "        set _s [get_property SLACK $_pp]\n"
        "        if {$_s < $_rp_wns} { set _rp_wns $_s }\n"
        "    }\n"
        "    puts \"PYRO_METRIC:RP_WNS:[format %.4f $_rp_wns]\"\n"
        "}\n"
        # R73a.6: the whole-design WNS is recorded for job diagnostics ONLY —
        # it MUST NOT feed met_timing (the known-accepted -0.427 ns static CMAC
        # violation, R73a.5, lives on this query).
        "set _p [get_timing_paths -quiet -max_paths 1 -nworst 1 -setup]\n"
        "if {[llength $_p] == 0} {\n"
        "    puts \"PYRO_METRIC:WNS:NONE\"\n"
        "} else {\n"
        "    set _wns [get_property SLACK [lindex $_p 0]]\n"
        "    puts \"PYRO_METRIC:WNS:[format %.4f $_wns]\"\n"
        "}\n"
        "write_checkpoint -force config_routed.dcp\n"
        # W6: pr_verify gate hardened — not merely catch{}.  Write the report,
        # then REQUIRE an explicit compatibility statement AND reject on any
        # failure/critical token, so a non-raising failure value or a CRITICAL
        # WARNING can never yield a false PASS (R82c honesty).
        "if {[catch {pr_verify -full_check -file pr_verify.rpt "
        "@REFERENCE_DCP@ config_routed.dcp} _pv]} {\n"
        "    error \"PYRO_PR: pr_verify raised: $_pv\"\n"
        "}\n"
        "set _fh [open pr_verify.rpt r]\n"
        "set _rpt [read $_fh]\n"
        "close $_fh\n"
        # W6-b: anchored tokens mirroring _RE_PR_VERIFY_FAIL/_RE_PR_VERIFY_OK —
        # a passing report's "Number of differences found: 0" / "PASSED" must
        # not be rejected; nonzero counts and real failure statements must be.
        "if {[regexp -nocase -line "
        "{critical\\s+warning|\\mnot\\s+compatible\\M|\\mincompatible\\M"
        "|\\mfailed\\M(?!\\s*:\\s*0)"
        "|^\\s*ERROR[: ]|(?:differences|mismatches)[^:\\n]*:\\s*[1-9]} "
        "$_rpt]} {\n"
        "    error \"PYRO_PR: pr_verify report has failure/critical tokens (R82c)\"\n"
        "}\n"
        "if {![regexp -nocase {\\mcompatible\\M|\\mpassed\\M} $_rpt]} {\n"
        "    error \"PYRO_PR: pr_verify report lacks compatibility confirmation (R82c)\"\n"
        "}\n"
        "puts \"PYRO_METRIC:PR_VERIFY:PASS\"\n"
        "write_bitstream -force -cell [_rp_cell] pyro_rp_partial.bit\n"
        "puts \"PYRO_METRIC:DONE\"\n"
    )

    def __init__(self, config: ToolchainConfig = ToolchainConfig()):
        self.config = config
        self._toolchain_version: Optional[int] = None   # cached, lazy (R75)

    # -- vivado executable resolution (R70/R71 fail-safe) ------------------
    def _vivado_exe(self) -> str:
        """The resolved ``vivado`` executable, or raise SynthesisFailed if the
        configured install dir does not resolve to a runnable Vivado (R70/R71:
        toolchain-absent => fail-safe permanent fallback, never mock)."""
        d = self.config.vivado_dir
        if not d:
            raise SynthesisFailed(
                "vivado toolchain unavailable: PYRO_VIVADO unset (R70/R71)")
        exe = os.path.join(d, "bin", "vivado")
        if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
            raise SynthesisFailed(
                f"vivado toolchain unavailable: no executable at {exe} (R70/R71)")
        return exe

    # -- actual Vivado version (R75) ---------------------------------------
    def _resolve_toolchain_version(self, exe: str, env: dict) -> int:
        """Derive the packed toolchain_version from ``vivado -version`` (R75):
        ``(YY << 24) | (RR << 16) | build``.  Falls back to the pinned
        VIVADO_TOOLCHAIN_VERSION if the probe fails/does not parse."""
        if self._toolchain_version is not None:
            return self._toolchain_version
        ver = VIVADO_TOOLCHAIN_VERSION
        try:
            out = subprocess.run(
                [exe, "-version"], env=env, cwd=None,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=120)
            m = _RE_VIVADO_VER.search(out.stdout or "")
            if m:
                yy = int(m.group(1)) % 100        # 2023 -> 23
                rr = int(m.group(2))              # .1
                ver = ((yy & 0xFF) << 24) | ((rr & 0xFF) << 16) | 0
        except (OSError, subprocess.SubprocessError):
            pass
        self._toolchain_version = ver
        return ver

    # -- the contract (R63b duck type) -------------------------------------
    def run(self, job: SynthJob) -> Tuple[bytes, Manifest]:
        """Drive real Vivado OOC synth+P&R for ``job``; return (payload,
        manifest) with genuine post-route metrics, or raise SynthesisFailed.

        NEVER lets an exception other than SynthesisFailed escape (R63e/R65).

        Mode (R72/R82): ``config.pr_bitstream`` selects the R82 partial-bitstream
        link flow (real device artifact); otherwise the OOC honest-metrics flow
        (R72 ``ooc_metrics``) runs.  The mode is fixed on the config the manager
        pinned at construction (R70b).
        """
        try:
            if self.config.pr_bitstream:
                return self._run_pr(job)
            return self._run(job)
        except SynthesisFailed:
            raise
        except Exception as exc:          # contain ordinary failures (R63e)
            # KeyboardInterrupt / SystemExit are BaseException (not Exception) and
            # deliberately propagate untouched so an operator interrupt or worker
            # shutdown is never masked as a synthesis failure.
            raise SynthesisFailed(f"vivado toolchain error: {exc!r}") from exc

    def _run(self, job: SynthJob) -> Tuple[bytes, Manifest]:
        exe = self._vivado_exe()
        cfg = self.config
        workdir = tempfile.mkdtemp(prefix="pyro_vivado_")
        try:
            # 1. Materialize inputs: generated Verilog + generated XDC (R73).
            with open(os.path.join(workdir, "design.v"), "w") as f:
                f.write(job.rtl)
            period_ns = 1000.0 / float(cfg.target_clock_mhz)   # 250 MHz -> 4.000
            with open(os.path.join(workdir, "constrs.xdc"), "w") as f:
                # R73: constrain the OOC circuit clock to the target period.
                f.write(f"create_clock -period {period_ns:.3f} "
                        f"-name clk [get_ports clk]\n")
            with open(os.path.join(workdir, "flow.tcl"), "w") as f:
                f.write(self._FLOW_TCL.replace("@PART@", cfg.part))

            # 2. Build the Vivado subprocess environment.  The pinned 2025.2
            #    starts cleanly on this host OS with no libtinfo.so.5 shim
            #    (R70a-pin), so the environment is inherited unmodified.
            env = dict(os.environ)

            # R75/R75a: the manifest records the *probed* Vivado version, while
            # the R4 cache key uses the *pinned* VIVADO_TOOLCHAIN_VERSION
            # (residency.bitstream_key).  Assert they agree so key and manifest
            # can never diverge: a mismatch (e.g. PYRO_VIVADO points at a version
            # other than the recorded 2025.2 pin) fails synthesis with a clear
            # diagnostic rather than silently keying an artifact under one version
            # while labelling it another.
            tool_ver = self._resolve_toolchain_version(exe, env)
            if tool_ver != VIVADO_TOOLCHAIN_VERSION:
                raise SynthesisFailed(
                    f"vivado version 0x{tool_ver:08x} does not match the pinned "
                    f"toolchain_version 0x{VIVADO_TOOLCHAIN_VERSION:08x} "
                    f"(cache-key/manifest consistency, R75/R75a)")

            # 3. Run Vivado in batch with a hard per-job timeout (R77).  A new
            #    session group lets us kill the *entire* process tree on expiry.
            #    -stack 2000: synth_design overruns its default stack on the
            #    generator's per-stage closure loops once NSTATES climbs past
            #    ~100 (Synth 8-7098, first hit by the 151-state SNORT-PF S1
            #    child); the flag only raises the synth thread's stack.
            cmd = [exe, "-mode", "batch", "-source", "flow.tcl",
                   "-stack", "2000", "-nojournal", "-log", "vivado.log"]
            proc = subprocess.Popen(
                cmd, cwd=workdir, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, start_new_session=True)
            try:
                out, _ = proc.communicate(timeout=float(cfg.job_timeout_s))
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)          # R77: kill the whole tree
                try:
                    out, _ = proc.communicate(timeout=30)
                except (subprocess.SubprocessError, OSError):
                    out = ""
                raise SynthesisFailed(
                    f"vivado exceeded per-job timeout "
                    f"({cfg.job_timeout_s:g}s); process tree killed (R77)")
            if proc.returncode != 0:
                raise SynthesisFailed(
                    f"vivado exited with status {proc.returncode} "
                    f"(synth/P&R failed) — see log")

            # 4. Parse the genuine post-route metrics (R72c).  Prefer the report
            #    file for utilization; the WNS marker for timing.
            util_path = os.path.join(workdir, "util.rpt")
            try:
                with open(util_path) as f:
                    util_txt = f.read()
            except OSError as exc:
                raise SynthesisFailed(
                    f"vivado produced no utilization report: {exc}")
            luts = _parse_first_int(_RE_CLB_LUTS, util_txt)
            ffs = _parse_first_int(_RE_CLB_FFS, util_txt)
            if luts is None or ffs is None:
                raise SynthesisFailed(
                    "could not parse CLB LUTs / CLB Registers from util report")
            out = out or ""
            if _RE_WNS_NONE.search(out):
                # No post-route setup timing paths: a degenerate/anomalous P&R
                # result for a clocked recognizer (pyro_circuit always has
                # registered paths).  Fail safe (R65) rather than fabricate a
                # met-timing verdict from an absent WNS.
                raise SynthesisFailed(
                    "no post-route setup timing paths — cannot verify timing (R65)")
            m = _RE_WNS.search(out)
            if m is None:
                raise SynthesisFailed(
                    "could not parse post-route WNS from vivado output")
            wns = float(m.group(1))

            # 5. Timing verdict (R73): 250 MHz proxy.
            met_timing = wns >= 0.0
            # Achieved Fmax at the target period.  Guard the denominator: WNS can
            # approach (never exceed, physically) the period for a near-zero-delay
            # path; the 1e-6 floor keeps fmax finite (JSON-serializable) instead
            # of dividing by zero.
            fmax_mhz = 1000.0 / max(period_ns - wns, 1e-6)
            if not met_timing:
                # R65/R73: a timing miss is a synthesis failure -> permanent
                # fallback with a diagnostic (no caller-visible error).
                raise SynthesisFailed(
                    f"timing not met at {cfg.target_clock_mhz:g} MHz: "
                    f"WNS={wns:.3f} ns (R73)")

            # 6. Payload (R72): the SAME model-exec PYROART1 container the mock
            #    emits — NOT a device bitstream.  Only the manifest is real.
            payload = _stub_payload(job)
            manifest = Manifest(
                pattern_hash=job.pattern_hash,
                encoding=job.encoding,
                effective_flags=job.effective_flags,
                circ_flags=job.circ_flags,
                generator_version=job.generator_version,
                harness_version=job.harness_version,
                toolchain_version=tool_ver,          # R75: real Vivado version
                shell_version=SHELL_VERSION,          # R75a: unchanged model value
                luts=luts,                            # R72c: genuine post-route
                ffs=ffs,                              # R72c: genuine post-route
                # R72c enumerates luts/ffs/fmax_mhz/met_timing as the genuine
                # post-route fields; bram/dsp are carried from the estimate and
                # remain honest because the one-hot NFA datapath uses no DSPs
                # (0) and only the harness's fixed 16 KiB result-ring BRAM
                # reserve — i.e. the estimate is exact-to-conservative here, not
                # a fabricated P&R number.
                bram_kb=job.bram_kb,
                dsps=job.dsps,
                fmax_mhz=fmax_mhz,                    # R73
                met_timing=met_timing,                # R73
                over_approx_classes=list(job.over_approx_classes),
                estimated_fp_rate=job.estimated_fp_rate,
                integrity_hash=payload_crc32(payload),
                payload_len=len(payload),
                payload_kind="ooc_metrics",           # R72a: honest, not a bitstream
            )
            return payload, manifest
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # -- R82 partial-bitstream (pr_bitstream) link flow --------------------
    def _run_pr(self, job: SynthJob) -> Tuple[bytes, Manifest]:
        """Drive the R82 DFX partial-bitstream flow for ``job`` and return
        (partial .bit bytes, ``pr_bitstream`` manifest), or raise SynthesisFailed.

        Fail-loud (R70a/R82): the R82d substrate paths (locked static DCP + the
        reference routed DCP) are REQUIRED.  An absent or non-existent path is a
        SynthesisFailed — NEVER a silent fall-back to ``ooc_metrics`` (that would
        serve a non-device artifact under a device-loadable claim).  pr_verify is
        a HARD GATE (R82c): if it does not pass, no partial is written and the job
        fails (permanent fallback, R65).

        Timing (R73a, v2.5.0): ``met_timing``/``fmax_mhz`` are decided by the
        RP-SCOPED post-route WNS (paths with startpoint and/or endpoint in the
        ``pyro_rp`` cell, axis_aclk 250 MHz domain — R73a.1/R73a.2); an empty
        scoped path set fails the job (R73a.3); the whole-design WNS is recorded
        in job diagnostics only and never gates (R73a.6).  A3.5: the payload is
        read as soon as pr_verify passes, and a post-verify failure preserves
        the workdir instead of destroying the verified partial.
        """
        exe = self._vivado_exe()
        cfg = self.config

        # R82b/R82d fail-loud substrate check (before spending any tool time).
        if not cfg.static_dcp:
            raise SynthesisFailed(
                "pr_bitstream requested but static_dcp (R82b locked static "
                "substrate) is unset — refusing to fall back to ooc_metrics (R82)")
        if not cfg.reference_dcp:
            raise SynthesisFailed(
                "pr_bitstream requested but reference_dcp (R82c/R82d pr_verify "
                "reference) is unset — refusing to fall back to ooc_metrics (R82)")
        if not os.path.isfile(cfg.static_dcp):
            raise SynthesisFailed(
                f"pr_bitstream: static_dcp not found: {cfg.static_dcp!r} (R82b)")
        if not os.path.isfile(cfg.reference_dcp):
            raise SynthesisFailed(
                f"pr_bitstream: reference_dcp not found: {cfg.reference_dcp!r} (R82c)")

        # The RP-child wrapper is the same for every pattern (params differ); the
        # engine RTL is job.rtl.  Import lazily to keep the OOC path light.
        from ..hdl.rp_wrapper import (
            check_engine_pairing, generate_rp_child, _declares_in_ready)

        # SNORT-PF SR7.  `engine_backpressure` is a property of the ENGINE TEXT,
        # not an independent build knob: the engine either declares `in_ready`
        # or it does not.  Derive it, so a hand-assembled group job that never
        # set the field cannot produce a deaf wrapper (the silent byte-drop),
        # and treat an explicit contradiction as a configuration error.
        derived_bp = _declares_in_ready(job.rtl)
        asked_bp = getattr(job, "engine_backpressure", None)
        if asked_bp is not None and bool(asked_bp) != derived_bp:
            raise ConfigurationError(
                "SynthJob.engine_backpressure=%r contradicts the engine RTL, "
                "which %s an in_ready port — the wrapper is emitted from the "
                "engine text, so fix the job, not the wrapper"
                % (asked_bp, "declares" if derived_bp else "does not declare"))

        workdir = tempfile.mkdtemp(prefix="pyro_vivado_pr_")
        # A3.5: once the pr_verify gate has passed, a good (verified) partial
        # exists on disk; any later failure must PRESERVE the workdir (the .bit
        # + reports) under a diagnostics path instead of rmtree-destroying
        # hours of P&R output unread.
        preserve_on_failure = False
        preserved: Optional[str] = None
        try:
            with open(os.path.join(workdir, "design.v"), "w") as f:
                f.write(job.rtl)                       # generated engine (pyro_circuit)
            # RP-child wrapper (R80); width must match the engine (P2b),
            # frame buffer must match the substrate shell (R78.9a/P2c).
            wrapper = generate_rp_child(
                job.pattern_hash,
                datapath_bytes=getattr(job, "datapath_bytes", 1),
                max_frame_bytes=int(cfg.rp_max_frame_bytes),
                cores=int(getattr(cfg, "rp_cores", 1)),
                engine_backpressure=derived_bp)
            # SNORT-PF SR7: an unconnected .in_ready and an under-driven
            # in_keep are both legal Verilog and both drop bytes silently.
            # Both texts exist only here, so this is the only place the pairing
            # can be checked at all.  A mismatch is a CONFIGURATION error, not
            # an R65 synthesis failure: the pairing inputs (`datapath_bytes`
            # agreement between `job.rtl` and the wrapper) are not in the
            # bitstream key, so caching a negative here would permanently poison
            # the very key the fix has to reuse.
            try:
                check_engine_pairing(job.rtl, wrapper)
            except ValueError as exc:
                raise ConfigurationError(str(exc)) from exc
            with open(os.path.join(workdir, "pyro_rp.sv"), "w") as f:
                f.write(wrapper)
            phys_opt = "phys_opt_design\n" if cfg.pr_phys_opt else ""
            flow = (self._PR_FLOW_TCL
                    .replace("@RPCELL@", cfg.rp_cell)
                    .replace("@PART@", cfg.part)
                    .replace("@STATIC_DCP@", os.path.abspath(cfg.static_dcp))
                    .replace("@REFERENCE_DCP@", os.path.abspath(cfg.reference_dcp))
                    .replace("@PLACE_DIRECTIVE@",
                             " -directive %s" % cfg.pr_place_directive
                             if cfg.pr_place_directive else "")
                    .replace("@ROUTE_DIRECTIVE@",
                             " -directive %s" % cfg.pr_route_directive
                             if cfg.pr_route_directive else "")
                    .replace("@POST_PLACE_PHYS_OPT@", phys_opt)
                    .replace("@POST_ROUTE_PHYS_OPT@", phys_opt))
            with open(os.path.join(workdir, "flow.tcl"), "w") as f:
                f.write(flow)

            env = dict(os.environ)   # 2025.2 needs no shim (R70a-pin)

            # R75/R75a: same pinned-version consistency guard as the OOC flow —
            # a partial and the static it links against MUST be the same release
            # (R82a); a mismatch fails rather than key an artifact under the wrong
            # toolchain_version.
            tool_ver = self._resolve_toolchain_version(exe, env)
            if tool_ver != VIVADO_TOOLCHAIN_VERSION:
                raise SynthesisFailed(
                    f"vivado version 0x{tool_ver:08x} does not match the pinned "
                    f"toolchain_version 0x{VIVADO_TOOLCHAIN_VERSION:08x} "
                    f"(same-release rule R82a; cache-key/manifest consistency R75)")

            # R84: PR jobs use VIVADO_PR_JOB_TIMEOUT (3600 s) not R77's 1800 s;
            # the R77 kill-the-process-tree discipline applies verbatim.
            cmd = [exe, "-mode", "batch", "-source", "flow.tcl",
                   "-stack", "2000",  # Synth 8-7098 guard, same as OOC flow
                   "-nojournal", "-log", "vivado.log"]
            proc = subprocess.Popen(
                cmd, cwd=workdir, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, start_new_session=True)
            try:
                out, _ = proc.communicate(timeout=float(cfg.pr_job_timeout_s))
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)               # R77 (via R84)
                try:
                    out, _ = proc.communicate(timeout=30)
                except (subprocess.SubprocessError, OSError):
                    out = ""
                raise SynthesisFailed(
                    f"vivado pr_bitstream job exceeded per-job timeout "
                    f"({cfg.pr_job_timeout_s:g}s); process tree killed (R84/R77)")
            out = out or ""
            if proc.returncode != 0:
                # A pr_verify failure raises `error` in Tcl → non-zero exit → here.
                raise SynthesisFailed(
                    f"vivado pr_bitstream flow failed (exit {proc.returncode}): "
                    f"synth/link/pr_verify error — see log (R82c/R65)")

            # R82c HARD GATE: the partial is honest only if pr_verify passed.  The
            # marker is emitted solely on a passing pr_verify; its absence (even
            # with exit 0) means we MUST NOT claim pr_bitstream.
            if _RE_PR_VERIFY_PASS.search(out) is None:
                raise SynthesisFailed(
                    "pr_verify did not pass (no PR_VERIFY:PASS marker) — refusing "
                    "to claim payload_kind=pr_bitstream (R82c/R72c honesty)")
            # W6 defense-in-depth: independently re-read the pr_verify report and
            # require an explicit compatibility statement with no failure/critical
            # token, so a false PASS cannot slip past the marker alone.
            _validate_pr_verify_report(os.path.join(workdir, "pr_verify.rpt"))

            # A3.5: the pr_verify gate has passed — a good verified partial now
            # exists on disk.  From here on, a failure preserves the workdir.
            preserve_on_failure = True

            # A3.5: read the partial bitstream payload IMMEDIATELY — before any
            # metric-parse or timing-gate failure can raise — so the artifact
            # can never again be destroyed unread on a post-verify failure.
            # Payload = the REAL partial bitstream bytes (R72/R85 device artifact).
            bit_path = os.path.join(workdir, "pyro_rp_partial.bit")
            try:
                with open(bit_path, "rb") as f:
                    payload = f.read()
            except OSError as exc:
                raise SynthesisFailed(
                    f"pr_verify passed but no partial bitstream was written: {exc} "
                    f"(R82c)")
            if not payload:
                raise SynthesisFailed("partial bitstream is empty (R82c)")

            # Post-route metrics (R72c/R73), same parsing as the OOC flow.
            util_path = os.path.join(workdir, "util.rpt")
            try:
                with open(util_path) as f:
                    util_txt = f.read()
            except OSError as exc:
                raise SynthesisFailed(
                    f"vivado pr_bitstream produced no utilization report: {exc}")
            luts = _parse_first_int(_RE_CLB_LUTS, util_txt)
            ffs = _parse_first_int(_RE_CLB_FFS, util_txt)
            if luts is None or ffs is None:
                raise SynthesisFailed(
                    "could not parse CLB LUTs / CLB Registers from PR util report")
            period_ns = 1000.0 / float(cfg.target_clock_mhz)

            # R73a.6: the whole-design WNS is job diagnostics ONLY.  It MUST
            # NOT feed met_timing — the flashed static's known-accepted
            # -0.427 ns CMAC violation (R73a.5) lives on this query, and gating
            # on it would fail every partial forever.
            m_whole = _RE_WNS.search(out)
            whole_wns: Optional[float] = (
                float(m_whole.group(1)) if m_whole else None)

            # R73a.1/R73a.2: met_timing and fmax_mhz are decided by the
            # RP-SCOPED WNS (startpoint and/or endpoint in the pyro_rp cell,
            # axis_aclk domain), never by the whole-design worst path.
            if _RE_RP_WNS_NONE.search(out):
                # R73a.3: an empty scoped path set is a FAILURE, not a pass — a
                # narrower gate must not degrade into no gate.
                raise SynthesisFailed(
                    f"R73a.3: the RP-scoped post-route timing query returned NO "
                    f"setup paths — refusing to pass an ungated partial "
                    f"(whole-design WNS={whole_wns}, diagnostics only) (R65)")
            m = _RE_RP_WNS.search(out)
            if m is None:
                raise SynthesisFailed(
                    "could not parse RP-scoped post-route WNS from PR vivado "
                    "output (R73a.1)")
            wns = float(m.group(1))               # the R73a.1 scoped WNS
            met_timing = wns >= 0.0               # R73/R73a.1
            fmax_mhz = 1000.0 / max(period_ns - wns, 1e-6)   # R73a.2
            _log.info(
                "PR link post-route timing: RP-scoped WNS=%.4f ns (decides "
                "met_timing, R73a.1); whole-design WNS=%s ns (diagnostics "
                "only, R73a.6)", wns,
                "NONE" if whole_wns is None else format(whole_wns, ".4f"))
            if not met_timing:
                raise SynthesisFailed(
                    f"PR link timing not met at {cfg.target_clock_mhz:g} MHz: "
                    f"RP-scoped WNS={wns:.3f} ns (R73/R73a.1; whole-design "
                    f"WNS={whole_wns} recorded per R73a.6, not gated)")

            manifest = Manifest(
                pattern_hash=job.pattern_hash,
                encoding=job.encoding,
                effective_flags=job.effective_flags,
                circ_flags=job.circ_flags,
                generator_version=job.generator_version,
                harness_version=job.harness_version,
                toolchain_version=tool_ver,          # R75/R82a: pinned 2025.2
                shell_version=SHELL_VERSION,          # unchanged (R75a/R81 note)
                luts=luts,                            # R72c: genuine post-route
                ffs=ffs,
                bram_kb=job.bram_kb,
                dsps=job.dsps,
                fmax_mhz=fmax_mhz,                    # R73
                met_timing=met_timing,                # R73
                over_approx_classes=list(job.over_approx_classes),
                estimated_fp_rate=job.estimated_fp_rate,
                integrity_hash=payload_crc32(payload),  # over the real .bit (R47b)
                payload_len=len(payload),
                payload_kind="pr_bitstream",          # R72: device-loadable artifact
                pr_verified=True,                     # R47b/R82c: pr_verify passed
            )
            return payload, manifest
        except Exception as exc:
            # A3.5: after the pr_verify gate has passed, the workdir holds a
            # good verified partial (+ reports) — preserve it for diagnosis
            # instead of destroying it.  R65 semantics are unchanged: the job
            # still fails as SynthesisFailed (permanent fallback); only the
            # disposal of the on-disk evidence changes.  KeyboardInterrupt /
            # SystemExit are BaseException and propagate untouched (see run()).
            if not preserve_on_failure:
                raise
            preserved = _preserve_pr_workdir(workdir)
            raise SynthesisFailed(
                f"pr_bitstream job failed AFTER pr_verify passed: {exc} — "
                f"workdir (partial .bit + reports) preserved for diagnosis at "
                f"{preserved} (A3.5)") from exc
        finally:
            if preserved is None:
                shutil.rmtree(workdir, ignore_errors=True)


def _preserve_pr_workdir(workdir: str) -> str:
    """A3.5: keep a ``pr_verify``-passed PR workdir (the partial ``.bit`` plus
    ``util.rpt``/``timing.rpt``/``pr_verify.rpt``/``vivado.log``) under a
    diagnostics path instead of destroying it.  Renames the directory to a
    ``-preserved`` sibling; if the rename fails the original directory is left
    in place (still preserved).  Returns the path holding the artifacts."""
    diag = workdir + "-preserved"
    try:
        os.rename(workdir, diag)
        return diag
    except OSError:
        return workdir


# W6: pr_verify report failure/critical tokens and the required compatibility token.
# W6-b: failure tokens are word/line-anchored so that a PASSING report's
# benign phrasing ("Number of differences found: 0", "No mismatches found",
# "pr_verify: PASSED") is not rejected, while any real failure statement,
# nonzero difference/mismatch count, or ERROR/CRITICAL line still is.
# ("incompatible" cannot satisfy the OK pattern: \bcompatible\b has no word
# boundary inside "incompatible".)  Final phrasing check against live
# pr_verify output remains a release gate for the first real PR job.
_RE_PR_VERIFY_FAIL = _stock_compile(
    r"critical\s+warning|\bnot\s+compatible\b|\bincompatible\b|\bfailed\b(?!\s*:\s*0\b)"
    r"|^\s*ERROR[: ]|(?:differences|mismatches)[^:\n]*:\s*[1-9]",
    re.IGNORECASE | re.MULTILINE)
_RE_PR_VERIFY_OK = _stock_compile(r"\bcompatible\b|\bpassed\b", re.IGNORECASE)


def _validate_pr_verify_report(path: str) -> None:
    """W6: independently confirm ``pr_verify`` success from its report file.

    Raises :class:`SynthesisFailed` unless the report exists, contains an explicit
    compatibility statement, and carries no failure/critical-warning token — so a
    non-raising ``pr_verify`` failure value can never yield a false ``pr_bitstream``
    claim (R82c honesty), independent of the in-Tcl gate.
    """
    try:
        with open(path) as f:
            rpt = f.read()
    except OSError as exc:
        raise SynthesisFailed(
            f"pr_verify report missing/unreadable: {exc} (R82c)")
    if _RE_PR_VERIFY_FAIL.search(rpt):
        raise SynthesisFailed(
            "pr_verify report contains failure/critical tokens (R82c)")
    if _RE_PR_VERIFY_OK.search(rpt) is None:
        raise SynthesisFailed(
            "pr_verify report lacks an explicit compatibility statement (R82c)")


def _parse_first_int(regex, text: str) -> Optional[int]:
    m = regex.search(text)
    return int(m.group(1)) if m else None


def _kill_process_tree(proc: "subprocess.Popen") -> None:
    """R77: SIGKILL the entire process group started by ``proc`` (Vivado spawns
    a loader + unwrapped child).  ``start_new_session=True`` made ``proc`` a
    process-group leader, so one killpg reaps the whole tree.  Best-effort."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
