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

import hashlib
import time
from dataclasses import dataclass, field
from typing import List, Tuple

from .manifest import Manifest, payload_crc32

# Pinned tool/shell versions — part of the R4/R47b cache key precisely because
# artifacts are NOT portable across tool/shell versions (P11).  A real Phase-2
# flow substitutes the true Vivado + open-nic-shell versions here.
TOOLCHAIN_VERSION = 0x00000100   # mock toolchain v0.1.0 (packed)
SHELL_VERSION = 0x0A000001       # OpenNIC target shell / PR-region id (opaque)

# Magic prefix of a stub PR bitstream payload (so a real loader can reject it).
_STUB_MAGIC = b"PYROSTUB"


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


@dataclass(frozen=True)
class ToolchainConfig:
    """Behavioral configuration for the mock toolchain (test-tunable, R63b)."""

    latency: float = 0.0              # seconds to model the (minutes-long) flow
    fail_mode: str = "ok"             # "ok" | "error" | "timeout"
    fmax_mhz: float = 300.0           # achieved Fmax the mock reports
    met_timing: bool = True


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
        )
        return payload, manifest


def _stub_payload(job: SynthJob) -> bytes:
    """A deterministic stub PR bitstream payload derived from the RTL + identity.

    Not a real bitstream — a real flow (Phase 2) replaces this — but stable and
    integrity-checkable so the harness contract (R47b) is exercised end to end.
    """
    h = hashlib.sha256()
    h.update(job.rtl.encode("utf-8"))
    h.update(bytes.fromhex(job.pattern_hash))
    h.update(int(job.circ_flags).to_bytes(4, "little"))
    h.update(int(job.generator_version).to_bytes(4, "little"))
    h.update(int(TOOLCHAIN_VERSION).to_bytes(4, "little"))
    h.update(int(SHELL_VERSION).to_bytes(4, "little"))
    return _STUB_MAGIC + h.digest()
