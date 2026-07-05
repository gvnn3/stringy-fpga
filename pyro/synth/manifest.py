"""PR bitstream artifact manifest (spec §7.4 R47b).

The synthesis service (R63) produces, for each bitstream-cache key, a **PR
bitstream artifact** plus a self-describing **manifest** (this module).  The
manifest lets the host verify — *before* a PR load (R40 ``pyro_circuit_load``) —
that an artifact is loadable into the live device and intact, and lets the
benchmark suite (R59) attribute re-verification cost via the R19c
over-approximation declaration.

The manifest is a plain, JSON-serializable record (stdlib ``json``), so it
survives process restarts on disk (R4/R63d) and could later be produced by a
real Vivado flow (Phase 2) without changing the host-side contract.
"""

from __future__ import annotations

import binascii
import json
from dataclasses import dataclass, field, asdict
from typing import List, Optional


@dataclass(frozen=True)
class Manifest:
    """R47b manifest fields (the ones the spec requires *at least*).

    ``pattern_hash`` is the hex of the 128-bit hash baked into ``CIRC_ID*``
    (R47a) — the same value the host uses for the identity trust boundary.
    ``integrity_hash`` is a CRC-32 (hex) over the bitstream payload (R47b), and
    is checked against the artifact bytes before a load.
    """

    # --- identity (R47a, mirrored into CIRC_ID*/CIRC_FLAGS) ----------------
    pattern_hash: str                 # hex of the 16-byte pattern hash
    encoding: int                     # PYRO_ENC_BYTES(0) / PYRO_ENC_UTF8(1)
    effective_flags: int              # canonicalized post-inline-extraction flags
    circ_flags: int                   # packed CIRC_FLAGS word (R45 0x0028)
    # --- versions / target region (R47b: artifact is not portable) ---------
    generator_version: int
    harness_version: int
    toolchain_version: int
    shell_version: int                # target shell / PR-region identifier
    # --- resource utilization + timing from P&R (R47b) ---------------------
    luts: int
    ffs: int
    bram_kb: int
    dsps: int
    fmax_mhz: float                   # achieved Fmax
    met_timing: bool                  # timing closed at the target clock
    # --- over-approximation declaration (R19c) -----------------------------
    over_approx_classes: List[str] = field(default_factory=list)
    estimated_fp_rate: float = 0.0
    # --- integrity (R47b) --------------------------------------------------
    integrity_hash: str = ""          # hex CRC-32 of the bitstream payload
    payload_len: int = 0

    # -- (de)serialization --------------------------------------------------
    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        data = json.loads(text)
        data["over_approx_classes"] = list(data.get("over_approx_classes", []))
        return cls(**data)

    # -- host-side checks performed before a PR load (R47b) -----------------
    def integrity_ok(self, payload: bytes) -> bool:
        """True iff ``payload`` matches the recorded integrity hash + length."""
        return (len(payload) == self.payload_len
                and payload_crc32(payload) == self.integrity_hash)

    def compatible_with(self, shell_version: int, harness_version: int) -> bool:
        """R47b: the artifact is only loadable into a compatible shell/harness."""
        return (int(shell_version) == self.shell_version
                and int(harness_version) == self.harness_version)


def payload_crc32(payload: bytes) -> str:
    """Hex CRC-32 of a bitstream payload (R47b integrity hash)."""
    return format(binascii.crc32(payload) & 0xFFFFFFFF, "08x")
