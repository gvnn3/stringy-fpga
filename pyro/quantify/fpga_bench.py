"""FPGA-side benchmark driver for the fpga-vs-snort study (E1/E2).

Implements the device side of docs/studies/fpga-vs-snort.md section 3
for one (group, density) cell: time the table load (E2), then stream
every manifest payload through the scan path, reading the R45a
CYCLES/BYTES counters and the wall clock per scan (E1), and collect
the hardware nominations mapped into the study's shared pattern index
space (position in the sorted unique anchor byte-string list, see
pyro.quantify.traffic.unique_patterns).

Dependency injection: :func:`bench_cell` takes an ALREADY-OPEN device
object and never opens a transport itself.  The device object is
duck-typed with four methods, deliberately named after the
pyro.device operations an adapter would delegate to (each with the
DeviceConfig argument already bound):

  read_table_status() -> TableStatus-like with ``.epoch``, or None
      (pyro.device.read_table_status; None = the resident child
      predates A5 section 3 — not a fault, recorded as epoch 0)
  load_table(image: bytes) -> TableStatus-like with ``.epoch``
      (pyro.device.load_table; raises on refusal, in which case the
      cell aborts — a refused commit is not a switch)
  scan(subject: bytes) -> reply or None
      (one MATCH round-trip; the reply carries ``.entries`` rows
      whose FIRST element is the hardware pattern_id, plus an
      ``.ovf`` or ``.overflowed`` flag —
      pyro.telemetry.MatchReplySummary qualifies.  None = timeout,
      i.e. request loss; the scan is still recorded)
  read_perf_counters() -> Optional[Tuple[int, int]]
      (pyro.device.read_perf_counters: (cycles, bytes) or None for
      "counters unavailable" — never a fault)

Counter semantics (R45a): the device wrapper resets the counters
before EVERY match, so a read reflects only the most recent scan;
bench_cell therefore reads them immediately after each scan.  A
``None`` read, and the fingerprinted pre-2026-07-31 HARNESS_VER
constant, are both recorded as ``cycles == 0`` with ``bytes`` falling
back to the host-known subject length — consumers must treat a
zero-cycle scan as "engine rate unavailable", never divide by it.

The corpus payload bound (1460 B, pyro.quantify.traffic.MAX_PAYLOAD)
fits a single non-jumbo MATCH request (1474 B chunk capacity), so no
chunking is needed here.

Result: the ``pyro-quantify-fpga/1`` JSON documented in the study
contract, one document per cell.  Per-scan entries carry the
contract fields ``i``/``bytes``/``cycles``/``wall_s``/``noms`` plus
the additive ``ovf`` boolean and, only on request loss, ``lost``.
"""

import json
import os
import time
from typing import Dict, NamedTuple, Optional, Sequence, Tuple

from pyro.quantify import traffic

SCHEMA = "pyro-quantify-fpga/1"

#: A5 overlay engine identity (same constant as
#: scripts/pyro_telemetry_demo.py ENGINE_ID); the device gates
#: TABLE_BEGIN on it before a byte moves.
A5_ENGINE_ID = 0x0A5E0001

#: Engine clock in Hz.  Engine rate for one scan is
#: ``bytes * F_ENGINE_HZ / cycles`` (design doc section 3, E1).
F_ENGINE_HZ = 250_000_000

#: R45a counter value returned in BOTH halves by an overlay child
#: built before the 2026-07-31 fix: the HARNESS_VER CSR constant
#: through a one-cycle-early latch, not a measurement.  Nulled, same
#: as pyro.telemetry does.
_PERF_FINGERPRINT = (0x00020300 << 32) | 0x00020300


class GroupTable(NamedTuple):
    """One group lowered for the FPGA side of a cell.

    ``anchors`` is the per-slot anchor list in slot order (None for
    tombstones) — the exact sequence the image was built from, so a
    hardware pattern_id indexes it directly.
    """
    name: str
    image: bytes
    n_states: int
    anchors: Tuple[Optional[bytes], ...]


def group_table_from(group, *, engine_id: int = A5_ENGINE_ID,
                     capacity: Optional[int] = None) -> GroupTable:
    """Lower a pyro.snort.groups.RuleGroup to a :class:`GroupTable`.

    Same lowering as the telemetry demo's _build_table: anchors in
    slot order (tombstones kept as None so pattern_ids line up),
    built with pyro.overlay.table.build and serialized for
    ``engine_id``.  Raises ValueError when ``capacity`` (states) is
    given and the automaton exceeds it.
    """
    from pyro.overlay import table as otable

    anchors = tuple(None if s.tombstone else s.anchor
                    for s in group.slots)
    ac = otable.build(anchors)
    if capacity is not None and ac.n_states > capacity:
        raise ValueError("group %s needs %d states, engine holds %d"
                         % (group.name, ac.n_states, capacity))
    image = otable.serialize(ac, engine_id=engine_id)
    return GroupTable(group.name, image, ac.n_states, anchors)


def pattern_index_map(
        anchors: Sequence[Optional[bytes]]) -> Dict[int, int]:
    """Hardware pattern_id -> study pattern index.

    The hardware pattern_id is the slot position in ``anchors`` (the
    sequence passed to pyro.overlay.table.build); the study index is
    the position in the sorted unique anchor byte-string list
    (traffic.unique_patterns).  Two slots holding the same anchor
    bytes (nocase vs exact variants) map to the same study index;
    tombstones (None/empty) get no mapping — the engine never
    nominates them.
    """
    uniq = traffic.unique_patterns(anchors)
    idx_of = {p: i for i, p in enumerate(uniq)}
    return {pid: idx_of[bytes(a)]
            for pid, a in enumerate(anchors) if a}


def load_corpus(corpus_dir: str) -> Tuple[dict, bytes]:
    """Read manifest.json and payloads.bin from a traffic.py corpus.

    Returns (manifest, payload_blob) after validating the schema tag
    and that every packet's (off, len) slice lies inside the blob.
    """
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


def _reply_ovf(reply) -> bool:
    """OVF flag from a MatchReplySummary or daemon ScanResult."""
    if hasattr(reply, "ovf"):
        return bool(reply.ovf)
    return bool(reply.overflowed)


def bench_cell(dev, corpus_dir: str, table: GroupTable, *,
               clock=time.monotonic) -> dict:
    """Run one (group, density) cell; return the fpga result dict.

    ``dev`` is the already-open device object described in the module
    docstring.  ``clock`` is injectable for tests (monotonic seconds).

    Sequence: read epoch before, timed load_table (E2), then for each
    manifest packet in order: wall-clocked scan, counter read (the
    wrapper reset the counters at MATCH time, so the read covers
    exactly this scan), nominations mapped to study pattern indices.
    A nomination whose pattern_id has no live slot is a protocol
    violation and raises ValueError rather than being dropped.
    """
    manifest, blob = load_corpus(corpus_dir)
    if manifest["group"] != table.name:
        raise ValueError("corpus is for group %r, table is %r"
                         % (manifest["group"], table.name))
    pid_map = pattern_index_map(table.anchors)

    st0 = dev.read_table_status()
    epoch_before = int(st0.epoch) if st0 is not None else 0

    t0 = clock()
    st1 = dev.load_table(table.image)
    load_s = float(clock() - t0)
    epoch_after = int(st1.epoch)

    scans = []
    for pkt in manifest["packets"]:
        subject = blob[pkt["off"]:pkt["off"] + pkt["len"]]
        t = clock()
        reply = dev.scan(subject)
        wall_s = float(clock() - t)

        pc = dev.read_perf_counters()
        if (pc is not None and pc[0] == _PERF_FINGERPRINT
                and pc[1] == _PERF_FINGERPRINT):
            pc = None
        if pc is None:
            cycles, nbytes = 0, len(subject)
        else:
            cycles, nbytes = int(pc[0]), int(pc[1])

        rec = {"i": int(pkt["i"]), "bytes": nbytes, "cycles": cycles,
               "wall_s": wall_s}
        if reply is None:
            rec["noms"] = []
            rec["ovf"] = False
            rec["lost"] = True
        else:
            noms = set()
            for entry in reply.entries:
                pid = int(entry[0])
                if pid not in pid_map:
                    raise ValueError(
                        "device nominated pattern_id %d, which has "
                        "no live slot in group %s"
                        % (pid, table.name))
                noms.add(pid_map[pid])
            rec["noms"] = sorted(noms)
            rec["ovf"] = _reply_ovf(reply)
        scans.append(rec)

    return {
        "schema": SCHEMA,
        "group": str(manifest["group"]),
        "density": float(manifest["density"]),
        "table_bytes": len(table.image),
        "n_states": int(table.n_states),
        "load_s": load_s,
        "epoch_before": epoch_before,
        "epoch_after": epoch_after,
        "scans": scans,
    }


def write_result(result: dict, path: str) -> None:
    """Write one cell result as compact JSON (trailing newline)."""
    with open(path, "w") as f:
        json.dump(result, f, sort_keys=True, separators=(",", ":"))
        f.write("\n")
