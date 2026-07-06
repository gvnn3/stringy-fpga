"""PYRO L2 HDL generation subpackage (spec §4 L2, Phase 1 / v2.0.0).

Public surface:

  * :func:`pyro.hdl.build_automaton`  — regex -> byte automaton (R9/R14).
  * :func:`pyro.hdl.estimate`         — resource estimator + fit gate (R11-R13).
  * :func:`pyro.hdl.generate`         — automaton -> synthesizable RTL circuit
                                        implementing the §7.4 harness contract.
  * version constants ``GENERATOR_VERSION`` / ``HARNESS_VERSION``.

Everything here is a pure, deterministic function of ``(pattern, flags, enc)``
(R8): the same input yields a byte-identical automaton and byte-identical RTL.
"""

from __future__ import annotations

from .automaton import (
    Automaton,
    ENC_BYTES,
    ENC_UTF8,
    OA_CROSS_LENGTH_CASEFOLD,
    OA_UNICODE_CATEGORY,
    OA_WORD_BOUNDARY_UTF8,
    build as build_automaton,
    byteset_to_ranges,
)
from .estimator import (
    ResourceEstimate,
    budget,
    estimate,
)
from .generator import (
    DATAPATH_BYTES,
    GENERATOR_VERSION,
    GeneratedCircuit,
    HARNESS_VERSION,
    ID_MAGIC,
    estimate_fp_rate,
    generate,
)
from .rp_wrapper import (
    ENGINE_MODULE,
    generate_rp_child,
    rp_child_id_from_hash,
)
from . import identity

__all__ = [
    "Automaton",
    "ENC_BYTES",
    "ENC_UTF8",
    "OA_CROSS_LENGTH_CASEFOLD",
    "OA_UNICODE_CATEGORY",
    "OA_WORD_BOUNDARY_UTF8",
    "build_automaton",
    "byteset_to_ranges",
    "ResourceEstimate",
    "budget",
    "estimate",
    "DATAPATH_BYTES",
    "GENERATOR_VERSION",
    "HARNESS_VERSION",
    "ID_MAGIC",
    "GeneratedCircuit",
    "estimate_fp_rate",
    "generate",
    "ENGINE_MODULE",
    "generate_rp_child",
    "rp_child_id_from_hash",
    "identity",
]
