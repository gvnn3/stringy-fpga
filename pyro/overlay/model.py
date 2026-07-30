"""Behavioural model of the overlay engine, executing a serialized table.

The third view of one artifact.  :mod:`pyro.overlay.table` builds and
serializes; this **walks the serialized image exactly as the fabric will** —
bitmap + popcount transition lookup, failure fallback, pre-unioned outputs —
so that a table which matches here must match on silicon, and a divergence
is a defect in one of the two rather than an ambiguity in the spec.

It deliberately reads the *image*, not the :class:`AhoCorasick` object.
Modelling from the in-memory automaton would silently skip the
serialization, which is precisely where a layout bug would live, and where
TABLE_ID is computed.

Residency and identity are modelled too (A5 §2.2, §5): the engine holds an
active image and a shadow, refuses to commit an incomplete or
CRC-mismatched shadow, exposes ``TABLE_ID``/``EPOCH``, and comes up after
reset with ``table_id == 0`` meaning *nothing valid* — never stale content
that reads as good.
"""

from __future__ import annotations

import struct
import threading
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from .table import (BITMAP_BYTES, HEADER_LEN, TABLE_FORMAT_VERSION,
                    TABLE_MAGIC, _HDR_FMT, table_id)


class TableMatch(NamedTuple):
    pattern_id: int
    end: int            # exact; start is recovered host-side as end - len
    epoch: int


class TableError(Exception):
    """A table load or commit was refused (A5 §5 fail-closed)."""


class TableImage:
    """Read-only accessor over a serialized table image."""

    __slots__ = ("buf", "n_states", "n_patterns", "engine_id",
                 "_bm", "_base", "_dense", "_fail", "_oidx", "_oflat")

    def __init__(self, buf: bytes):
        if len(buf) < HEADER_LEN:
            raise TableError("image shorter than header")
        f = struct.unpack(_HDR_FMT, buf[:HEADER_LEN])
        (magic, ver, engine_id, n_states, n_patterns,
         o_bm, o_base, o_dense, o_fail, o_oidx, o_oflat,
         _n_dense, _n_oflat) = f
        if magic != TABLE_MAGIC:
            raise TableError("bad table magic %r" % magic)
        if ver != TABLE_FORMAT_VERSION:
            raise TableError("table_format_version %d != %d"
                             % (ver, TABLE_FORMAT_VERSION))
        self.buf = buf
        self.n_states = n_states
        self.n_patterns = n_patterns
        self.engine_id = engine_id
        self._bm, self._base, self._dense = o_bm, o_base, o_dense
        self._fail, self._oidx, self._oflat = o_fail, o_oidx, o_oflat

    # -- the transition path the fabric implements ------------------------
    def _goto(self, state: int, b: int) -> Optional[int]:
        off = self._bm + state * BITMAP_BYTES
        bits = int.from_bytes(self.buf[off:off + BITMAP_BYTES], "little")
        if not (bits >> b) & 1:
            return None
        rank = bin(bits & ((1 << b) - 1)).count("1")   # popcount below b
        base = struct.unpack_from("<I", self.buf, self._base + state * 4)[0]
        return struct.unpack_from("<I", self.buf,
                                  self._dense + (base + rank) * 4)[0]

    def _failure(self, state: int) -> int:
        return struct.unpack_from("<I", self.buf, self._fail + state * 4)[0]

    def next_state(self, state: int, b: int) -> int:
        s = state
        while True:
            nxt = self._goto(s, b)
            if nxt is not None:
                return nxt
            if s == 0:
                return 0
            s = self._failure(s)

    def outputs(self, state: int) -> Tuple[int, ...]:
        off, cnt = struct.unpack_from("<II", self.buf, self._oidx + state * 8)
        if not cnt:
            return ()
        return struct.unpack_from("<%dI" % cnt, self.buf,
                                  self._oflat + off * 4)


class OverlayEngineModel:
    """Resident engine with a writable table (A5 §2.2/§3/§5).

    Thread-safe, because the daemon scans on one thread while the residency
    scheduler may commit on another — the exact race the epoch exists for.
    """

    def __init__(self, engine_id: int = 0, capacity_states: int = 1 << 20,
                 capacity_patterns: int = 1 << 16):
        self.engine_id = int(engine_id)
        self.capacity_states = capacity_states
        self.capacity_patterns = capacity_patterns
        self._lock = threading.Lock()
        # A5 §5: after reset there is NO valid table.
        self._active: Optional[TableImage] = None
        self._active_id = 0
        self._epoch = 0
        self._shadow = bytearray()
        self._shadow_declared = 0
        self._shadow_crc_expected = 0
        self._shadow_open = False
        self._shadow_written = 0

    # -- observability (the TABLE_STATUS surface) --------------------------
    @property
    def table_id(self) -> int:
        return self._active_id

    @property
    def epoch(self) -> int:
        return self._epoch

    def status(self) -> dict:
        with self._lock:
            shadow_id = (table_id(bytes(self._shadow))
                         if self._shadow_complete() else 0)
            return {
                "active_table_id": self._active_id,
                "shadow_table_id": shadow_id,
                "epoch": self._epoch,
                "shadow_open": self._shadow_open,
                "shadow_bytes_received": self._shadow_written,
                "shadow_declared": self._shadow_declared,
                "shadow_valid": self._shadow_complete(),
                "engine_id": self.engine_id,
                "table_format_version": TABLE_FORMAT_VERSION,
                "capacity_states": self.capacity_states,
                "capacity_patterns": self.capacity_patterns,
            }

    def _shadow_complete(self) -> bool:
        return (self._shadow_open
                and self._shadow_declared > 0
                and self._shadow_written == self._shadow_declared
                and len(self._shadow) == self._shadow_declared)

    # -- the load protocol -------------------------------------------------
    def table_begin(self, total_bytes: int, expected_crc: int,
                    engine_id: int, table_format_version: int,
                    n_states: int, n_patterns: int, mode: int = 0) -> None:
        """Gate compatibility BEFORE a byte moves (A5 §3)."""
        with self._lock:
            if engine_id != self.engine_id:
                raise TableError("engine_id 0x%08x != resident 0x%08x"
                                 % (engine_id, self.engine_id))
            if table_format_version != TABLE_FORMAT_VERSION:
                raise TableError("table_format_version %d unsupported"
                                 % table_format_version)
            if n_states > self.capacity_states:
                raise TableError("n_states %d exceeds capacity %d"
                                 % (n_states, self.capacity_states))
            if n_patterns > self.capacity_patterns:
                raise TableError("n_patterns %d exceeds capacity %d"
                                 % (n_patterns, self.capacity_patterns))
            if total_bytes <= 0:
                raise TableError("total_bytes must be positive")
            if mode == 1:                       # delta-from-active
                if self._active is None:
                    raise TableError("delta mode with no active table")
                self._shadow = bytearray(self._active.buf)
                self._shadow_written = len(self._shadow)
            else:
                self._shadow = bytearray(total_bytes)
                self._shadow_written = 0
            self._shadow_declared = total_bytes
            self._shadow_crc_expected = expected_crc & 0xFFFFFFFF
            self._shadow_open = True

    def table_data(self, offset: int, chunk: bytes) -> None:
        """Offset-addressed, therefore idempotent and restartable."""
        with self._lock:
            if not self._shadow_open:
                raise TableError("TABLE_DATA with no open transfer")
            end = offset + len(chunk)
            if offset < 0 or end > self._shadow_declared:
                raise TableError("chunk [%d,%d) outside declared %d bytes"
                                 % (offset, end, self._shadow_declared))
            # Count only bytes not previously written, so a retransmit does
            # not inflate the completion count into a false "complete".
            prev = self._written_mask_count(offset, end)
            self._shadow[offset:end] = chunk
            self._mark_written(offset, end)
            self._shadow_written += (end - offset) - prev

    # A merged interval set: the honest way to count "bytes actually
    # received" when chunks may be retransmitted or arrive out of order.
    def _mark_written(self, lo: int, hi: int) -> None:
        iv = getattr(self, "_iv", None)
        if iv is None:
            iv = self._iv = []  # lazily created per instance
        iv.append((lo, hi))
        iv.sort()
        merged = []
        for a, b in iv:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        self._iv = merged

    def _written_mask_count(self, lo: int, hi: int) -> int:
        iv = getattr(self, "_iv", None) or []
        tot = 0
        for a, b in iv:
            s, e = max(a, lo), min(b, hi)
            if s < e:
                tot += e - s
        return tot

    def table_commit(self, expected_crc: int) -> int:
        """Activate the shadow iff it is whole and its CRC matches.

        Refusal leaves the active table untouched — a failed commit must
        never degrade what was working (A5 §5).
        """
        with self._lock:
            if not self._shadow_open:
                raise TableError("commit with no open transfer")
            if self._shadow_written != self._shadow_declared:
                raise TableError("incomplete: %d of %d bytes"
                                 % (self._shadow_written,
                                    self._shadow_declared))
            got = table_id(bytes(self._shadow))
            if got != (expected_crc & 0xFFFFFFFF):
                raise TableError("CRC mismatch: device 0x%08x != host 0x%08x"
                                 % (got, expected_crc & 0xFFFFFFFF))
            if got != self._shadow_crc_expected:
                raise TableError("CRC differs from the value declared at "
                                 "TABLE_BEGIN")
            img = TableImage(bytes(self._shadow))      # parse before commit
            self._active = img
            self._active_id = got
            self._epoch += 1
            self._shadow_open = False
            self._iv = []
            return self._epoch

    def table_abort(self) -> None:
        with self._lock:
            self._shadow = bytearray()
            self._shadow_open = False
            self._shadow_written = 0
            self._shadow_declared = 0
            self._iv = []

    def reset(self) -> None:
        """After reset nothing is valid — never stale content (A5 §5)."""
        with self._lock:
            self._active = None
            self._active_id = 0
            self._epoch = 0
            self._shadow = bytearray()
            self._shadow_open = False
            self._shadow_written = 0
            self._shadow_declared = 0
            self._iv = []

    # -- matching ----------------------------------------------------------
    def scan(self, data: bytes, out_cap: int = 61
             ) -> Tuple[Tuple[TableMatch, ...], bool]:
        """Scan, reporting (pattern_id, end, epoch) per output.

        The epoch is stamped from the table that produced the match, which
        is what makes attribution across a commit safe (A5 §2.2/SR14′).
        """
        with self._lock:
            img, epoch = self._active, self._epoch
        if img is None:
            return (), False                    # no valid table: no claims
        out: List[TableMatch] = []
        overflow = False
        st = 0
        for i, b in enumerate(data):
            st = img.next_state(st, b)
            if st == 0:
                continue
            for pid in img.outputs(st):
                if len(out) >= out_cap:
                    overflow = True
                    break
                out.append(TableMatch(pid, i + 1, epoch))
            if overflow:
                break
        return tuple(out), overflow
