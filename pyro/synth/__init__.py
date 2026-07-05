"""PYRO synthesis subsystem (spec §7.5, Phase 1b).

Out-of-process synthesis service (R63) driven by a mock toolchain (R63b), a
persistent PR-bitstream cache with cold/warm/resident tiers (R4), the
synthesis-launch policy + prewarm (R4a/R62), single-tenant PR-region eviction
(R64), and the circuit-lifecycle stats (R66).

These are Python-level interfaces (Phase 1b); Task 7 binds them behind the
C ABI (ABI 2.0.0).  Nothing here is imported on the per-call routing hot path
except the light residency check consulted by :mod:`pyro._route`.
"""

from __future__ import annotations

from . import artifact
from .manifest import Manifest, payload_crc32
from .cache import (
    BitstreamCache,
    BitstreamKey,
    CacheEntry,
    default_root,
    key_digest,
    make_key,
)
from .toolchain import (
    MockToolchain,
    SHELL_VERSION,
    SynthJob,
    SynthesisFailed,
    TOOLCHAIN_VERSION,
    ToolchainConfig,
)
from .service import STATUS_FAILED, STATUS_OK, SynthesisService
from .residency import (
    N_SYNTH_DEFAULT,
    ROUTE_NOT_RESIDENT,
    ROUTE_PERMANENT_FALLBACK,
    ROUTE_RESIDENT,
    ROUTE_SYNTH,
    ResidencyManager,
    TIER_COLD,
    TIER_FALLBACK_ONLY,
    TIER_RESIDENT,
    TIER_SYNTHESIZING,
    TIER_WARM,
    get_manager,
    reset_manager,
)

__all__ = [
    "artifact",
    "Manifest", "payload_crc32",
    "BitstreamCache", "BitstreamKey", "CacheEntry", "default_root",
    "key_digest", "make_key",
    "MockToolchain", "SynthJob", "SynthesisFailed", "ToolchainConfig",
    "TOOLCHAIN_VERSION", "SHELL_VERSION",
    "SynthesisService", "STATUS_OK", "STATUS_FAILED",
    "ResidencyManager", "N_SYNTH_DEFAULT",
    "TIER_COLD", "TIER_SYNTHESIZING", "TIER_WARM", "TIER_RESIDENT",
    "TIER_FALLBACK_ONLY",
    "ROUTE_RESIDENT", "ROUTE_NOT_RESIDENT", "ROUTE_SYNTH",
    "ROUTE_PERMANENT_FALLBACK",
    "get_manager", "reset_manager",
]
