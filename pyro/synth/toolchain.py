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

# Pinned tool/shell versions — part of the R4/R47b cache key precisely because
# artifacts are NOT portable across tool/shell versions (P11).  A real Phase-2
# flow substitutes the true Vivado + open-nic-shell versions here.
TOOLCHAIN_VERSION = 0x00000100   # mock toolchain v0.1.0 (packed)
SHELL_VERSION = 0x0A000001       # OpenNIC target shell / PR-region id (opaque)

# R75: the vivado toolchain's toolchain_version encodes the actual Vivado
# version, packed (YY << 24) | (RR << 16) | build.  For Vivado 2023.1 this is
# 0x17010000 (YY=23=0x17, RR=1, build=0).  Used as the R4/R47b cache-key
# component for the vivado kind, keeping mock (0x00000100) and vivado artifacts
# on distinct keys (R75a).  The adapter re-derives the concrete value from the
# tool's own `vivado -version` at run time, but the host-side key needs a static
# pin, so this constant records the operator-selected install (2023.1).
VIVADO_TOOLCHAIN_VERSION = 0x17010000

# R77: default per-job Vivado timeout (30 min); overridable via ToolchainConfig.
VIVADO_JOB_TIMEOUT = 1800.0


class SynthesisFailed(Exception):
    """The (mock) toolchain could not produce a fitting/timing-clean artifact."""


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
    job_timeout_s: float = VIVADO_JOB_TIMEOUT  # per-job Vivado timeout (R77)


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
_RE_CLB_LUTS = re.compile(r"^\|\s*CLB LUTs\s*\|\s*(\d+)\s*\|", re.MULTILINE)
_RE_CLB_FFS = re.compile(r"^\|\s*CLB Registers\s*\|\s*(\d+)\s*\|", re.MULTILINE)
# WNS marker emitted by the flow tcl.  Vivado's `format %.4f` guarantees a plain
# fixed-point decimal ("PYRO_METRIC:WNS:2.4050"), so no exponent/odd formatting
# can slip past this regex; the sentinel "NONE" (no setup timing paths) is
# matched separately and mapped to SynthesisFailed (R65).
_RE_WNS = re.compile(r"^PYRO_METRIC:WNS:(-?\d+\.\d+)\s*$", re.MULTILINE)
_RE_WNS_NONE = re.compile(r"^PYRO_METRIC:WNS:NONE\s*$", re.MULTILINE)
# `vivado -version` first line: "vivado v2023.1 (64-bit)".
_RE_VIVADO_VER = re.compile(r"v(\d+)\.(\d+)")


def _libtinfo5_shim(workdir: str) -> Optional[str]:
    """Return a dir to prepend to LD_LIBRARY_PATH so Vivado can find
    ``libtinfo.so.5``, or ``None`` if no shim is needed / possible.

    Host quirk (contained + commented): this Ubuntu ships only
    ``libtinfo.so.6``; Vivado's launcher dlopen's the SONAME ``libtinfo.so.5``
    and aborts at startup without it.  When ``libtinfo.so.5`` cannot be loaded we
    create a per-job shim dir with a symlink ``libtinfo.so.5 -> <system
    libtinfo.so.6>`` and hand it back to the caller, which prepends it to
    ``LD_LIBRARY_PATH`` **for the Vivado subprocess only** (never the parent
    process).  If ``libtinfo.so.5`` already loads, or no ``.so.6`` is found, we
    return None and let Vivado run/fail on its own.
    """
    import ctypes
    import ctypes.util

    try:
        ctypes.CDLL("libtinfo.so.5")
        return None                      # already resolvable — no shim needed
    except OSError:
        pass
    # Locate the system libtinfo.so.6 (soname via find_library, then common
    # multiarch dirs as a fallback).
    candidates = []
    soname = ctypes.util.find_library("tinfo")   # e.g. "libtinfo.so.6"
    if soname:
        for d in ("/lib/x86_64-linux-gnu", "/usr/lib/x86_64-linux-gnu",
                  "/lib64", "/usr/lib64", "/usr/lib", "/lib"):
            candidates.append(os.path.join(d, soname))
    candidates += [
        "/lib/x86_64-linux-gnu/libtinfo.so.6",
        "/usr/lib/x86_64-linux-gnu/libtinfo.so.6",
    ]
    target = next((c for c in candidates if os.path.exists(c)), None)
    if target is None:
        return None
    shim = os.path.join(workdir, "libshim")
    os.makedirs(shim, exist_ok=True)
    link = os.path.join(shim, "libtinfo.so.5")
    if not os.path.lexists(link):
        os.symlink(os.path.realpath(target), link)
    return shim


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
        """
        try:
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

            # 2. Build the Vivado subprocess environment (libtinfo.so.5 shim,
            #    contained to this subprocess only).
            env = dict(os.environ)
            shim = _libtinfo5_shim(workdir)
            if shim:
                prev = env.get("LD_LIBRARY_PATH", "")
                env["LD_LIBRARY_PATH"] = shim + (os.pathsep + prev if prev else "")

            # R75/R75a: the manifest records the *probed* Vivado version, while
            # the R4 cache key uses the *pinned* VIVADO_TOOLCHAIN_VERSION
            # (residency.bitstream_key).  Assert they agree so key and manifest
            # can never diverge: a mismatch (e.g. PYRO_VIVADO points at a version
            # other than the recorded 2023.1 pin) fails synthesis with a clear
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
            cmd = [exe, "-mode", "batch", "-source", "flow.tcl",
                   "-nojournal", "-log", "vivado.log"]
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
