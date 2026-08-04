"""Oracle parity referee for the fpga-vs-snort study (E4).

For every (group, density) cell the referee compares each side's
observed per-packet pattern set against the Python oracle's expected
set.  Pass rule (study contract): the Snort side must equal the
oracle exactly (missing == [] and extra == []); the FPGA side must
miss nothing (missing == []) — over-nomination is benign by design
(SR5) and is only counted.

Oracle choice: this module implements the DIRECT reference matcher —
a stdlib bytes.find sweep of every unique anchor over every payload —
rather than reusing tests/acceptance/snortpf_s2_support.oracle_windows.
The project oracle speaks the group's SLOT index space (one slot per
lowered (pattern_eff, flags_eff) matcher, nocase anchors pre-folded,
lowered-regex slots evaluated with re), which is a different
identifier space from this study's shared pattern index: position in
the sorted unique anchor byte-string list
(pyro.quantify.traffic.unique_patterns).  The corpus embeds anchor
bytes verbatim, both engines are handed the same raw bytes, and E4
requires a referee independent of both engines' automata — so the
smallest correct implementation, with no code shared with either
side, is the exact-bytes occurrence sweep below.

A pattern "hits" a packet iff its bytes occur anywhere in that
packet's payload; multiplicity and offsets do not matter for parity.
"""

import json
import os
from typing import Dict, List, Optional, Sequence, Set, Tuple

from pyro.quantify import traffic

SCHEMA = "pyro-quantify-parity/1"

SIDES = ("snort", "fpga")


def occurrences(pattern: bytes, data: bytes) -> List[int]:
    """All start offsets of pattern in data, overlaps included.

    Plain bytes.find sweep; an empty pattern has no occurrences (it
    is a tombstone, not a match-everything wildcard).
    """
    if not pattern:
        return []
    out = []
    i = data.find(pattern)
    while i >= 0:
        out.append(i)
        i = data.find(pattern, i + 1)
    return out


def _load_corpus(corpus_dir: str) -> Tuple[dict, bytes]:
    """manifest.json + payloads.bin, schema- and bounds-checked."""
    with open(os.path.join(corpus_dir, "manifest.json")) as f:
        manifest = json.load(f)
    if manifest.get("schema") != traffic.SCHEMA:
        raise ValueError("unexpected manifest schema %r (want %r)"
                         % (manifest.get("schema"), traffic.SCHEMA))
    bin_name = manifest.get("payloads_bin", "payloads.bin")
    with open(os.path.join(corpus_dir, bin_name), "rb") as f:
        blob = f.read()
    for pkt in manifest["packets"]:
        if pkt["off"] + pkt["len"] > len(blob):
            raise ValueError(
                "packet %d payload (off %d, len %d) overruns "
                "payloads.bin (%d bytes)"
                % (pkt["i"], pkt["off"], pkt["len"], len(blob)))
    return manifest, blob


def oracle_for_corpus(anchors: Sequence[Optional[bytes]],
                      corpus_dir: str) -> Dict[int, Set[int]]:
    """Expected per-packet pattern hits for a traffic.py corpus.

    ``anchors`` is the group's anchor list (tombstones as None/empty
    are skipped); pattern indices are positions in
    traffic.unique_patterns(anchors) — the same index space the
    manifest, the Snort sid mapping (sid = 1000000 + index), and the
    FPGA nomination mapping use.

    Returns a SPARSE mapping {packet_index: set(pattern_index)};
    packets with no hits are absent.  This is the oracle verdict, not
    the generator's embedded-hit knowledge: accidental occurrences in
    random filler count too.
    """
    patterns = traffic.unique_patterns(anchors)
    manifest, blob = _load_corpus(corpus_dir)
    hits: Dict[int, Set[int]] = {}
    for pkt in manifest["packets"]:
        payload = blob[pkt["off"]:pkt["off"] + pkt["len"]]
        got = {idx for idx, pat in enumerate(patterns)
               if occurrences(pat, payload)}
        if got:
            hits[int(pkt["i"])] = got
    return hits


def _pair_set(per_packet: Dict[int, Set[int]]) -> Set[Tuple[int, int]]:
    return {(int(pkt), int(pat))
            for pkt, pats in per_packet.items() for pat in pats}


def compare(manifest: dict, oracle_hits: Dict[int, Set[int]],
            side: str, observed: Dict[int, Set[int]]) -> dict:
    """Referee one side of one cell; return the parity result dict.

    ``oracle_hits`` and ``observed`` are {packet_index:
    set(pattern_index)} (sparse; absent packets mean "no hits").
    ``side`` selects the pass rule: "snort" must equal the oracle
    exactly; "fpga" passes iff nothing is missing (extra nominations
    are benign over-nomination, reported and counted only).
    """
    if side not in SIDES:
        raise ValueError("side must be one of %r, not %r"
                         % (SIDES, side))
    want = _pair_set(oracle_hits)
    got = _pair_set(observed)
    missing = sorted(want - got)
    extra = sorted(got - want)
    ok = not missing and (side == "fpga" or not extra)
    return {
        "schema": SCHEMA,
        "group": str(manifest["group"]),
        "density": float(manifest["density"]),
        "side": side,
        "packets": int(manifest["count"]),
        "oracle_hits": len(want),
        "missing": [[pkt, pat] for pkt, pat in missing],
        "extra": [[pkt, pat] for pkt, pat in extra],
        "extra_count": len(extra),
        "pass": ok,
    }
