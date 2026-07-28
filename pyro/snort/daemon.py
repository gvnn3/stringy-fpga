"""SNORT-PF host filter daemon (spec §4.5, AC-S3-1).

The S3 pipeline: traffic tap → SR13 header classify against the host-side
variable table → port-mix histogram (feeding the SR10 residency scheduler,
:mod:`pyro.snort.scheduler`) → SR11/SR12 chunked ``MATCH`` pipeline with
the SR14 identity gate → nomination stream → SR19 stats surface.

Design rules this module carries (each is a spec obligation, not taste):

* **SR5 — nomination, never verdict.**  Nothing here withholds traffic
  from Snort.  The daemon's only output is a stream of ``(gid, sid)``
  nominations (plus tripwired/always-forward markers); a verdict is
  Snort's alone.
* **SR13 — variables are data.**  The variable table is *loaded* (defaults
  mirror the shipped ``snort_defaults.lua``), never compiled into any
  bitstream, and never hashed into any identity or cache key.
* **SR14 — identity before attribution.**  Every scan batch is attributed
  only after the transport's ``rp_child_id`` equals the loaded group's
  expected low-32 (verified per batch, cheap); a mismatch routes to "no
  resident group" — all traffic unfiltered — never to a misattributed
  nomination.
* **SR12 — per-flow overlap tail.**  A flow carries the last
  ``overlap_tail`` bytes (= the group's ``max floating span − 1``,
  generalized by AC-S3-2) so a match split across chunk *or* segment
  boundaries is wholly inside some request.
* **SR15 — tripwires.**  gzip/deflate content-encoding, chunked
  transfer-encoding, and %-density above threshold on HTTP flows force
  nomination-independent forwarding for that flow, counted per class.
* **R41/R78.7 — OVF resume, the hardware-real form.**  The wrapper never
  reads ``start_off``; resume re-sends the TRIMMED corpus and re-labels
  host-side (the S2 harness's discovery, kept here verbatim).  A
  ``\\A``-anchored slot's window does NOT survive trimming, so when a
  buffer-aligned (tail_len == 0) request overflows, every ``\\A`` slot's
  rules are nominated wholesale — a sound over-approximation (SR3
  direction: never lose a nomination to a full ring).
"""

from __future__ import annotations

import ipaddress
import threading
import time
from typing import (Callable, Dict, Iterable, List, NamedTuple, Optional,
                    Sequence, Set, Tuple)

from . import groups as _groups

#: R78 MATCH_REQUEST payload bound (corpus profile, SR11).
CHUNK_BYTES = 1474

#: R78.7 reply bound: ≤ 61 24-byte entries per MATCH_REPLY.
OUT_CAP = 61

#: SR15 %-density threshold on HTTP request lines: above this fraction of
#: percent-escapes per byte the anchor may be encoding-blinded.
PERCENT_DENSITY_THRESHOLD = 0.05

_TRIPWIRE_CLASSES = ("content_encoding", "chunked_te", "percent_density")


# --------------------------------------------------------------------------
# SR13: the variable table (data, never compiled, never hashed)
# --------------------------------------------------------------------------
_DEFAULT_HTTP_PORTS = frozenset(int(p) for p in """
    80 81 311 383 591 593 901 1220 1414 1741 1830 2301 2381 2809 3037 3128
    3702 4343 4848 5250 6988 7000 7001 7144 7145 7510 7777 7779 8000 8008
    8014 8028 8080 8085 8088 8090 8118 8123 8180 8181 8243 8280 8300 8800
    8888 8899 9000 9060 9080 9090 9091 9443 9999 11371 34443 34444 41080
    50002 55555
""".split())

_DEFAULT_PORT_VARS: Dict[str, frozenset] = {
    "$HTTP_PORTS": _DEFAULT_HTTP_PORTS,
    "$FILE_DATA_PORTS": _DEFAULT_HTTP_PORTS | {110, 143},
    "$FTP_PORTS": frozenset({21, 2100, 3535}),
    "$ORACLE_PORTS": frozenset(range(1024, 65536)),
    "$SIP_PORTS": frozenset({5060, 5061, 5600}),
    "$SSH_PORTS": frozenset({22}),
}


class VarTable:
    """SR13 host-side deployment variables: port lists + net sets.

    The shipped defaults mirror ``snort_defaults.lua`` (as the S2
    differential harness does); a site loads its own values.  ~13 entries
    (SF16).  Net variables default to ``any``.
    """

    def __init__(self,
                 port_vars: Optional[Dict[str, Iterable[int]]] = None,
                 net_vars: Optional[Dict[str, Sequence[str]]] = None):
        self.port_vars = {k: frozenset(v) for k, v in
                          (port_vars or _DEFAULT_PORT_VARS).items()}
        self.net_vars = {k: tuple(ipaddress.ip_network(c) for c in v)
                         for k, v in (net_vars or {}).items()}

    def port_holds(self, token: str, port: int) -> bool:
        tok = (token or "any").strip()
        if not tok or tok.lower() == "any":
            return True
        neg = tok.startswith("!")
        if neg:
            tok = tok[1:]
        if tok.startswith("$"):
            member = port in self.port_vars.get(tok, frozenset())
        else:
            member = self._token_ports(tok, port)
        return member != neg

    @staticmethod
    def _token_ports(tok: str, port: int) -> bool:
        tok = tok.strip("[]")
        for piece in tok.split(","):
            piece = piece.strip()
            if not piece:
                continue
            if ":" in piece:
                lo, _, hi = piece.partition(":")
                lo_v = int(lo) if lo else 0
                hi_v = int(hi) if hi else 65535
                if lo_v <= port <= hi_v:
                    return True
            elif piece.isdigit() and int(piece) == port:
                return True
        return False

    def net_holds(self, token: str, ip: str) -> bool:
        tok = (token or "any").strip()
        if not tok or tok.lower() == "any" or tok.startswith("$") and \
                tok not in self.net_vars:
            return True                    # default: every net var is 'any'
        neg = tok.startswith("!")
        if neg:
            tok = tok[1:]
        nets = self.net_vars.get(tok)
        if nets is None:
            try:
                nets = (ipaddress.ip_network(tok, strict=False),)
            except ValueError:
                return True                # unparseable token: never filter
        addr = ipaddress.ip_address(ip)
        member = any(addr in n for n in nets)
        return member != neg

    def relevant_port_classes(self, dst_port: int) -> Set[str]:
        """Which SR6 port classes a flow with this destination port could
        hold rules for.  ``any`` and ``literal`` are always offered (a
        literal-class group's rules carry their own header predicates,
        re-checked per rule by Snort — SR13's 'extra work, never a missed
        rule' contract)."""
        out = {"any", "literal"}
        for var, ports in self.port_vars.items():
            if dst_port in ports:
                out.add(var)
        return out

    def most_specific_class(self, dst_port: int) -> str:
        """The ONE class a packet is counted under for SR10 *scheduling*
        (not for the SR13 offer decision, which uses the full relevant
        set): the matching port variable with the FEWEST members — most
        specific by set size, so $HTTP_PORTS beats its superset
        $FILE_DATA_PORTS on port 80 — sorted-name tie-break, else
        ``literal``.  ``any`` is never counted here — the scheduler gives
        it a traffic-share floor instead, or the catch-all class would
        either dominate every mix or starve entirely."""
        hits = [(len(ports), var) for var, ports in self.port_vars.items()
                if dst_port in ports]
        if hits:
            return min(hits)[1]
        return "literal"


# --------------------------------------------------------------------------
# The port-mix histogram (feeds SR10 scheduling)
# --------------------------------------------------------------------------
class PortMixHistogram:
    """Exponentially-decayed byte counts per SR6 port class.

    ``half_life_s`` sets rotation inertia: SF20's ~14–45 s swap cost means
    the mix must be measured over minutes, not packets (the scheduler adds
    hysteresis on top — this is just the measurement).
    """

    def __init__(self, var_table: VarTable, half_life_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self._vt = var_table
        self._clock = clock
        self._half_life = float(half_life_s)
        self._counts: Dict[str, float] = {}
        self._stamp = clock()
        self._lock = threading.Lock()

    def _decay(self, now: float) -> None:
        dt = now - self._stamp
        if dt <= 0:
            return
        f = 0.5 ** (dt / self._half_life)
        for k in self._counts:
            self._counts[k] *= f
        self._stamp = now

    def observe(self, dst_port: int, n_bytes: int) -> None:
        now = self._clock()
        with self._lock:
            self._decay(now)
            cls = self._vt.most_specific_class(dst_port)
            self._counts[cls] = self._counts.get(cls, 0.0) + n_bytes

    def snapshot(self) -> Dict[str, float]:
        with self._lock:
            self._decay(self._clock())
            return dict(self._counts)


# --------------------------------------------------------------------------
# SR15 tripwires
# --------------------------------------------------------------------------
def tripwire_class(payload: bytes) -> Optional[str]:
    """Cheap per-segment tripwire check (SR15).  Returns the class name
    or None.  Deliberately byte-level and over-eager: a tripwire only
    forces *forwarding*, which is always sound."""
    low = payload[:2048].lower()
    if b"content-encoding:" in low and (
            b"gzip" in low or b"deflate" in low):
        return "content_encoding"
    if b"transfer-encoding:" in low and b"chunked" in low:
        return "chunked_te"
    if low.startswith((b"get ", b"post ", b"head ", b"put ", b"delete ")):
        line = low.split(b"\r\n", 1)[0]
        if line and line.count(b"%") / len(line) > PERCENT_DENSITY_THRESHOLD:
            return "percent_density"
    return None


# --------------------------------------------------------------------------
# Transports (the SR11 seam): model-backed for device-free, wire for silicon
# --------------------------------------------------------------------------
class ScanResult(NamedTuple):
    entries: Tuple[Tuple[int, int, int], ...]   # (pattern_id, start, end)
    overflowed: bool


class ModelTransport:
    """Device-free transport: the group's software twin (R7/SR16).

    ``child_id`` reproduces the R78.5a CSR readback so the SR14 gate is
    exercised for real even without a device.
    """

    def __init__(self, group, model):
        self.group = group
        self._model = model

    def child_id(self) -> int:
        from ..hdl import generator as gen
        return self.group.rp_child_id(gen.GENERATOR_VERSION,
                                      gen.HARNESS_VERSION,
                                      self._model.circuit.datapath_bytes
                                      if hasattr(self._model.circuit,
                                                 "datapath_bytes") else 1)

    def scan(self, payload: bytes, out_cap: int = OUT_CAP) -> ScanResult:
        entries, overflowed = self._model.scan(payload, 0, out_cap)
        return ScanResult(tuple((m.pattern_id, m.start, m.end)
                                for m in entries), overflowed)


class WireTransport:
    """R78 raw-Ethernet transport (SR11) over the ``onic`` control binding.

    Thin adapter over :mod:`pyro.device`'s frame machinery; constructed
    lazily so the daemon core stays importable on device-free hosts.
    ``iface`` comes from ``PYRO_DEVICE_IFACE`` (no default — A4/F3).
    """

    def __init__(self, iface: str, group):
        from .. import device as _device
        self._device = _device
        self._iface = iface
        self.group = group

    def child_id(self) -> int:
        cfg = self._device.DeviceConfig(iface=self._iface)
        ok, ident = self._device.probe_device(cfg)
        if not ok:
            return 0                       # SR14: routes to 'no resident'
        return int(getattr(ident, "rp_child_id", 0))

    def scan(self, payload: bytes, out_cap: int = OUT_CAP) -> ScanResult:
        cfg = self._device.DeviceConfig(iface=self._iface)
        entries, overflowed = self._device.match_scan(cfg, payload,
                                                      out_cap=out_cap)
        return ScanResult(tuple(entries), overflowed)


# --------------------------------------------------------------------------
# SR19 stats surface
# --------------------------------------------------------------------------
class Stats:
    """SR19 counters, PYRO AC-3-4 shape.  All monotonic except gauges."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self.nominations_by_tier: Dict[str, int] = {}
        self.reverify_confirms = 0
        self.reverify_rejects = 0
        self.rejects_by_class: Dict[str, int] = {}
        self.tripwire_hits: Dict[str, int] = {c: 0 for c in _TRIPWIRE_CLASSES}
        self.identity_mismatches = 0
        self.ovf_resumes = 0
        self.ovf_anchored_floods = 0
        self.requests = 0
        self.segments = 0
        self.resident_child_id: int = 0
        self.resident_group: Optional[str] = None
        self.resident_rules = 0
        self._unfiltered_since: Optional[float] = clock()
        self.unfiltered_seconds_total = 0.0
        self.swaps = 0

    # -- residency bookkeeping (SR10/SR19 unfiltered-window seconds) -------
    def set_resident(self, group_name: Optional[str], child_id: int,
                     n_rules: int) -> None:
        with self._lock:
            now = self._clock()
            if group_name is None:
                if self._unfiltered_since is None:
                    self._unfiltered_since = now
            else:
                if self._unfiltered_since is not None:
                    self.unfiltered_seconds_total += (
                        now - self._unfiltered_since)
                    self._unfiltered_since = None
            self.resident_group = group_name
            self.resident_child_id = child_id
            self.resident_rules = n_rules

    def nominate(self, tier: Optional[str]) -> None:
        with self._lock:
            key = tier or "unknown"
            self.nominations_by_tier[key] = \
                self.nominations_by_tier.get(key, 0) + 1

    def reverified(self, confirmed: bool, oa_class: str = "unclassified"):
        with self._lock:
            if confirmed:
                self.reverify_confirms += 1
            else:
                self.reverify_rejects += 1
                self.rejects_by_class[oa_class] = \
                    self.rejects_by_class.get(oa_class, 0) + 1

    def snapshot(self) -> dict:
        with self._lock:
            now = self._clock()
            unfiltered = self.unfiltered_seconds_total
            if self._unfiltered_since is not None:
                unfiltered += now - self._unfiltered_since
            return {
                "nominations_by_tier": dict(self.nominations_by_tier),
                "reverify": {"confirms": self.reverify_confirms,
                             "rejects": self.reverify_rejects,
                             "rejects_by_class": dict(self.rejects_by_class)},
                "tripwire_hits": dict(self.tripwire_hits),
                "identity_mismatches": self.identity_mismatches,
                "ovf": {"resumes": self.ovf_resumes,
                        "anchored_floods": self.ovf_anchored_floods},
                "requests": self.requests,
                "segments": self.segments,
                "resident": {"group": self.resident_group,
                             "rp_child_id": "0x%08x" % self.resident_child_id,
                             "rules": self.resident_rules},
                "unfiltered_seconds": round(unfiltered, 3),
                "swaps": self.swaps,
            }


# --------------------------------------------------------------------------
# The nomination pipeline (SR11/SR12/SR14 + the OVF rules)
# --------------------------------------------------------------------------
class FlowKey(NamedTuple):
    proto: str
    src_ip: str
    src_port: int
    dst_ip: str
    dst_port: int


class _FlowState:
    __slots__ = ("tail", "tripwired", "offset", "last_seen")

    def __init__(self):
        self.tail = b""
        self.tripwired: Optional[str] = None
        self.offset = 0            # absolute flow offset of the next byte
        self.last_seen = 0.0


class Nomination(NamedTuple):
    gid: int
    sid: int
    flow: FlowKey
    end_off: int               # absolute flow offset of the window end
    kind: str                  # "match" | "tripwire" | "ovf_flood"


class NominationPipeline:
    """Chunk → scan → resume → attribute, for ONE resident group."""

    def __init__(self, transport, stats: Stats, out_cap: int = OUT_CAP):
        self.transport = transport
        self.group = transport.group
        self.stats = stats
        self.out_cap = out_cap
        self._sidecar = self.group.sidecar()
        self._tier = {s.index: (s.rules[0].subtier if s.rules else None)
                      for s in self.group.slots}
        self._expected_child = None
        # \A-anchored slots: their windows cannot survive an OVF resume
        # trim, so a buffer-aligned request that overflows floods them.
        self._anchored = tuple(
            s.index for s in self.group.slots
            if not s.tombstone and s.pattern_eff.startswith(b"(?s:\\A"))
        self._span = max(1, self.group.max_tail_span)

    def expected_child_id(self) -> int:
        if self._expected_child is None:
            from ..hdl import generator as gen
            self._expected_child = self.group.rp_child_id(
                gen.GENERATOR_VERSION, gen.HARNESS_VERSION, 1)
        return self._expected_child

    def identity_ok(self) -> bool:
        """SR14: the resident child is the group we think it is."""
        ok = self.transport.child_id() == self.expected_child_id()
        if not ok:
            self.stats.identity_mismatches += 1
        return ok

    def scan_request(self, buf: bytes, base: int, tail_len: int,
                     flow: FlowKey) -> List[Nomination]:
        """One SR11 request (+R41 resume).  ``base`` is the absolute flow
        offset of ``buf[0]``; ``tail_len`` its SR12 carry-over prefix."""
        out: List[Nomination] = []
        self.stats.requests += 1
        start_off = 0
        while start_off <= len(buf):
            res = self.transport.scan(buf[start_off:], self.out_cap)
            for pid, s, e in res.entries:
                for key in self._sidecar.get(pid, ()):
                    gid, sid = key.split(":")
                    self.stats.nominate(self._tier.get(pid))
                    out.append(Nomination(int(gid), int(sid), flow,
                                          base + start_off + e, "match"))
            if not res.overflowed:
                break
            self.stats.ovf_resumes += 1
            if start_off == 0 and tail_len == 0 and self._anchored:
                # Buffer-aligned request overflowed: \A windows may be in
                # the truncated remainder and cannot survive the trim —
                # nominate every \A slot's rules (sound over-approximation).
                self.stats.ovf_anchored_floods += 1
                for pid in self._anchored:
                    for key in self._sidecar.get(pid, ()):
                        gid, sid = key.split(":")
                        self.stats.nominate(self._tier.get(pid))
                        out.append(Nomination(int(gid), int(sid), flow,
                                              base + start_off, "ovf_flood"))
            if not res.entries:
                break                     # cannot make progress; flooded above
            last_end = max(e for _p, _s, e in res.entries)
            start_off = max(start_off + 1, start_off + last_end - self._span)
        return out


# --------------------------------------------------------------------------
# The daemon
# --------------------------------------------------------------------------
class FilterDaemon:
    """Composition root: feed it segments (or attach a tap), read
    nominations + stats.  Holds at most one resident pipeline (SR10
    single-tenant slot 1)."""

    def __init__(self, var_table: Optional[VarTable] = None,
                 clock: Callable[[], float] = time.monotonic,
                 flow_idle_s: float = 120.0, max_flows: int = 4096):
        self.vars = var_table or VarTable()
        self.stats = Stats(clock)
        self.histogram = PortMixHistogram(self.vars, clock=clock)
        self._clock = clock
        self._flow_idle = flow_idle_s
        self._max_flows = max_flows
        self._flows: Dict[FlowKey, _FlowState] = {}
        self._pipeline: Optional[NominationPipeline] = None
        self._lock = threading.Lock()

    # -- residency (driven by the SR10 scheduler) --------------------------
    def set_pipeline(self, pipeline: Optional[NominationPipeline]) -> None:
        with self._lock:
            self._pipeline = pipeline
            if pipeline is None:
                self.stats.set_resident(None, 0, 0)
            else:
                self.stats.set_resident(
                    pipeline.group.name, pipeline.expected_child_id(),
                    pipeline.group.rule_count)

    # -- the per-segment path ---------------------------------------------
    def feed(self, flow: FlowKey, payload: bytes) -> List[Nomination]:
        """One client→server segment.  Returns this segment's nominations
        (empty while unfiltered — Snort sees everything regardless, SR5)."""
        if not payload:
            return []
        self.stats.segments += 1
        self.histogram.observe(flow.dst_port, len(payload))
        st = self._flow(flow)
        out: List[Nomination] = []

        cls = st.tripwired or tripwire_class(payload)
        if cls and st.tripwired is None:
            st.tripwired = cls
            self.stats.tripwire_hits[cls] = \
                self.stats.tripwire_hits.get(cls, 0) + 1
        if st.tripwired:
            out.append(Nomination(0, 0, flow, st.offset, "tripwire"))

        pipe = self._pipeline
        if pipe is not None and self._relevant(pipe, flow):
            if pipe.identity_ok():
                tail = pipe.group.overlap_tail
                buf = st.tail + payload
                base = st.offset - len(st.tail)
                pos = 0
                while pos < len(buf):
                    take = min(CHUNK_BYTES, len(buf) - pos)
                    # every request keeps its own SR12 carry-over prefix
                    lo = max(0, pos - tail) if pos else 0
                    chunk = buf[lo:pos + take]
                    out.extend(pipe.scan_request(
                        chunk, base + lo, pos - lo if pos else len(st.tail),
                        flow))
                    pos += take
                st.tail = buf[-tail:] if tail else b""
            else:
                self.set_pipeline(None)   # SR14: no resident group
        st.offset += len(payload)
        st.last_seen = self._clock()
        return out

    def _relevant(self, pipe: NominationPipeline, flow: FlowKey) -> bool:
        """SR13: offer a payload only to a group its port could hold."""
        return pipe.group.port_class in \
            self.vars.relevant_port_classes(flow.dst_port)

    def _flow(self, key: FlowKey) -> _FlowState:
        st = self._flows.get(key)
        if st is None:
            if len(self._flows) >= self._max_flows:
                self._evict()
            st = self._flows[key] = _FlowState()
        return st

    def _evict(self) -> None:
        now = self._clock()
        stale = [k for k, v in self._flows.items()
                 if now - v.last_seen > self._flow_idle]
        for k in stale:
            del self._flows[k]
        if len(self._flows) >= self._max_flows:
            oldest = min(self._flows, key=lambda k: self._flows[k].last_seen)
            del self._flows[oldest]


# --------------------------------------------------------------------------
# AF_PACKET tap (OQ-5: the control-binding mirror) — live use only
# --------------------------------------------------------------------------
def afpacket_tap(daemon: FilterDaemon, iface: str,
                 should_stop: Callable[[], bool] = lambda: False) -> None:
    """Bind AF_PACKET on ``iface`` and feed TCP/UDP client payloads to the
    daemon.  Runs until ``should_stop()``.  Requires CAP_NET_RAW; the
    interface is operator-named (A4/F3: no default, no scanning)."""
    import socket
    import struct as _struct
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                      socket.htons(0x0003))
    s.bind((iface, 0))
    s.settimeout(0.5)
    try:
        while not should_stop():
            try:
                frame = s.recv(65535)
            except socket.timeout:
                continue
            if len(frame) < 34 or frame[12:14] != b"\x08\x00":
                continue
            ihl = (frame[14] & 0x0F) * 4
            proto = frame[23]
            if proto not in (6, 17):
                continue
            src_ip = ".".join(str(b) for b in frame[26:30])
            dst_ip = ".".join(str(b) for b in frame[30:34])
            l4 = 14 + ihl
            if proto == 6:
                if len(frame) < l4 + 20:
                    continue
                sport, dport = _struct.unpack("!HH", frame[l4:l4 + 4])
                doff = (frame[l4 + 12] >> 4) * 4
                payload = frame[l4 + doff:]
                key = FlowKey("tcp", src_ip, sport, dst_ip, dport)
            else:
                sport, dport = _struct.unpack("!HH", frame[l4:l4 + 4])
                payload = frame[l4 + 8:]
                key = FlowKey("udp", src_ip, sport, dst_ip, dport)
            if payload:
                daemon.feed(key, payload)
    finally:
        s.close()
