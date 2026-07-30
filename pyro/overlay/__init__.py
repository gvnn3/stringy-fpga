"""Overlay engine: rule content written at run time as data (Amendment A5).

Approved 2026-07-30 on the evidence of `docs/studies/switch-cost-frontier.md`:
scheduling pays only while ``switch_cost / phase_length <= 0.3``, and JTAG
partial reconfiguration's measured 13.6 s confines it to phases of a minute
or longer — one to four orders of magnitude away from reacting to observed
traffic.  An overlay writes a 1.33 MB table in 0.54 ms over the measured
char-dev path and pays neither the bitstream transfer nor the 2.15 s
recovery tax.

The amendment's binding precondition is the identity layer, because a
loadable table destroys the premise SR14 rests on: today the bitstream IS
the rules, so ``rp_child_id`` attests content; with an overlay the
bitstream attests only an *engine*, and ``pattern_id`` means whatever the
loaded table says.  Hence three levels, all required before attribution:

* ``CIRC_ID``  — which engine   (exists, R47a)
* ``TABLE_ID`` — which rules    (CRC-32C over the received image)
* ``EPOCH``    — when           (per commit, stamped into every match)

Modules: :mod:`~pyro.overlay.table` builds and serializes an Aho-Corasick
table and computes its identity; :mod:`~pyro.overlay.model` executes the
serialized image exactly as the fabric will, and models residency, commit
and fail-closed behaviour.
"""

from __future__ import annotations

from .model import (  # noqa: F401
    OverlayEngineModel,
    TableError,
    TableImage,
    TableMatch,
)
from .table import (  # noqa: F401
    TABLE_FORMAT_VERSION,
    AhoCorasick,
    CaseSplitTable,
    ascii_fold,
    build,
    build_for_group,
    manifest,
    precision_delta,
    serialize,
    stats,
    strong_id,
    table_id,
)

__all__ = [
    "TABLE_FORMAT_VERSION",
    "AhoCorasick",
    "CaseSplitTable",
    "ascii_fold",
    "OverlayEngineModel",
    "TableError",
    "TableImage",
    "TableMatch",
    "build",
    "build_for_group",
    "manifest",
    "precision_delta",
    "serialize",
    "stats",
    "strong_id",
    "table_id",
]
