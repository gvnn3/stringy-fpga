"""Schedulable tenants: distinct FPGA functions competing for one region.

A **tenant** is a unit of functionality that can be resident in `pyro_rp`.
Four exist today, and their heterogeneity is the point — a workload of
identical interchangeable jobs cannot distinguish one scheduler from
another, which is precisely why the earlier Snort-only experiments came out
flat (docs/studies/a5-working-set.md).  These four differ in footprint,
input view, latency tolerance and value function, so a scheduler finally
has something to be wrong about.

===============  ==========  ==============  =====================  ==========
tenant           kind        input view      value driven by        latency
===============  ==========  ==============  =====================  ==========
pyro-regex       regex       host buffers    pending scan bytes     batch
snortpf/<group>  pattern-set L4 payload      rules that could fire  per-flow
ip-match         ip-match    full frame      packets to classify    per-packet
header-match     header      full frame      packets to classify    per-packet
===============  ==========  ==============  =====================  ==========

**No new RTL.**  IP and header matching are expressed as fixed-offset
patterns through the existing generator, using the AC-S3-2 `\\A.{n}`
prefix lowering: "the 4 bytes at frame offset 26" is
``(?s:\\A.{26}\\xc0\\xa8\\x01\\x0a)``, and a /24 prefix is the same with the
last byte a wildcard.  This is a real finding in itself — the pattern
fabric already subsumes header classification; it just was never asked to.

**Input views are a real scheduling dimension.**  These tenants do not all
want the same bytes: the Snort groups want reassembled L4 payload, while
IP/header matching wants the raw frame from byte 0 (offsets are only
meaningful there).  In OS terms that is an address-space difference, and
a scheduler that co-locates tenants must satisfy it.
"""

from __future__ import annotations

import ipaddress
import re as _stdre
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

#: Byte offsets into an Ethernet II + IPv4 frame (IHL=5).  These are what
#: make header matching expressible as a fixed-offset pattern.
FRAME_OFFSETS = {
    "ethertype": 12,
    "ip_ttl": 22,
    "ip_proto": 23,
    "ip_src": 26,
    "ip_dst": 30,
    "l4": 34,            # TCP/UDP source port, or ICMP type
    "l4_dport": 36,
    "icmp_type": 34,
    "icmp_code": 35,
    "tcp_flags": 47,
}

#: Protocol numbers we can pin in the IP header.
_PROTO_NUM = {"tcp": 6, "udp": 17, "icmp": 1}

#: The generator's bounded-repeat ceiling (SF6 MAX_REPEAT); every offset we
#: use is far inside it, but assert rather than assume.
_MAX_OFFSET = 255


def _at(offset: int, literal: bytes, wildcard_tail: int = 0) -> bytes:
    """A pattern matching ``literal`` at exactly ``offset`` bytes into the
    scan buffer, optionally followed by ``wildcard_tail`` don't-care bytes.

    Emitted in the same shape :mod:`pyro.snort.lowering` produces, so it
    takes the identical, silicon-verified automaton path.
    """
    if not 0 <= offset <= _MAX_OFFSET:
        raise ValueError("offset %d outside the generator's repeat bound"
                         % offset)
    body = b"\\A" + (b".{%d}" % offset if offset else b"")
    body += _stdre.escape(literal)
    if wildcard_tail:
        body += b".{%d}" % wildcard_tail
    return b"(?s:" + body + b")"


class TenantDemand(NamedTuple):
    """What the scheduler has observed, per tenant-relevant signal."""

    scan_bytes: float = 0.0        # host regex work pending (pyro-regex)
    flow_bytes: float = 0.0        # payload bytes seen (snortpf groups)
    packets: float = 0.0           # frames seen (ip-match, header-match)
    port_mix: Optional[Dict[int, float]] = None   # bytes per dst port


class Tenant:
    """One schedulable FPGA function."""

    __slots__ = ("name", "kind", "input_view", "patterns", "labels",
                 "_circuit", "artifact", "notes", "_value_fn")

    def __init__(self, name: str, kind: str, input_view: str,
                 patterns: Sequence[Optional[bytes]],
                 labels: Sequence[str], value_fn,
                 artifact: Optional[str] = None, notes: str = ""):
        self.name = name
        self.kind = kind
        self.input_view = input_view      # "frame" | "l4-payload" | "host"
        self.patterns = list(patterns)
        self.labels = list(labels)        # what slot i means, for attribution
        self.artifact = artifact
        self.notes = notes
        self._circuit = None
        self._value_fn = value_fn

    # -- footprint ---------------------------------------------------------
    @property
    def slots(self) -> int:
        return len(self.patterns)

    def circuit(self, datapath_bytes: int = 1):
        """Compile (and cache) this tenant's circuit through the existing
        generator — the same path the silicon-verified groups take."""
        if self._circuit is None:
            from ..hdl import generator as _gen
            flags = [0] * len(self.patterns)
            self._circuit = _gen.generate_group(
                self.patterns, flags, datapath_bytes=datapath_bytes)
        return self._circuit

    def automata(self):
        from ..hdl import automaton as _auto
        return [None if p is None else _auto.build(p, 0, _auto.ENC_BYTES)
                for p in self.patterns]

    def footprint(self) -> Dict[str, int]:
        """Slots, states and byte-edges — byte-edges being what post-route
        LUTs actually track (measured: ~2.49 LUTs/byte-edge at dpb=1)."""
        from ..hdl import automaton as _auto
        states = edges = 0
        for au in self.automata():
            if au is None:
                continue
            states += au.n_states
            for st in range(au.n_states):
                for e in au.edges[st]:
                    if e.kind == _auto.E_BYTE:
                        edges += 1
        return {"slots": self.slots, "states": states, "byte_edges": edges,
                "est_luts": int(edges * 2.49)}

    # -- scheduling --------------------------------------------------------
    def value(self, demand: TenantDemand) -> float:
        """Expected worth of being resident, given observed demand.

        Deliberately per-kind and NOT comparable across kinds without an
        explicit weighting — making that incomparability visible is part of
        the research (a scheduler over heterogeneous tenants needs a
        currency, and inventing one silently is how you get the
        value-blind bug the A5 study found)."""
        return float(self._value_fn(demand))

    def describe(self) -> dict:
        d = {"name": self.name, "kind": self.kind,
             "input_view": self.input_view, "artifact": self.artifact,
             "notes": self.notes}
        d.update(self.footprint())
        return d

    def __repr__(self) -> str:
        return "<Tenant %s kind=%s slots=%d>" % (self.name, self.kind,
                                                 self.slots)


# --------------------------------------------------------------------------
# Tenant 1 — PYRO regex (exists on silicon: the x4 frame-parallel child)
# --------------------------------------------------------------------------
def pyro_regex_tenant(pattern: bytes = rb"abc[a-f]{2}",
                      artifact: Optional[str] = None) -> Tenant:
    """The PYRO product surface: one general regex, wide datapath.

    Its value is pending host scan work, which is a completely different
    signal from anything the Snort tenants care about — the point of
    including it.
    """
    return Tenant(
        name="pyro-regex", kind="regex", input_view="host",
        patterns=[pattern], labels=["re:%s" % pattern.decode("latin-1")],
        value_fn=lambda d: d.scan_bytes,
        artifact=artifact,
        notes="8 B/cycle x4 child on silicon; batch latency tolerance")


# --------------------------------------------------------------------------
# Tenant 2 — SNORT-PF rule groups (21 of them, all built)
# --------------------------------------------------------------------------
def snortpf_tenants(groups=None, artifacts: Optional[Dict[str, str]] = None
                    ) -> List[Tenant]:
    """One tenant per SR6 rule group.  Value is the scheduler's fixed
    function: rules that could fire on the observed port mix."""
    from ..snort import daemon as _daemon
    from ..snort import groups as _groups
    from ..snort import triage as _triage
    import os

    if groups is None:
        corpus = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            "third_party", "snort3-community-rules",
            "snort3-community.rules")
        groups = _groups.pack_groups(_triage.triage_file(corpus))

    vt = _daemon.VarTable()
    out = []
    for g in groups:
        pats, labels = [], []
        for s in g.slots:
            pats.append(None if s.tombstone else s.pattern_eff)
            labels.append(",".join(r.key for r in s.rules))
        toks = [[r.dst_port for r in s.rules] for s in g.slots]

        def _v(d, _toks=toks, _vt=vt):
            mix = d.port_mix or {}
            if not mix:
                return 0.0
            total = 0.0
            for port, nbytes in mix.items():
                fires = sum(1 for ts in _toks for t in ts
                            if _vt.port_holds(t, port))
                total += nbytes * fires
            return total

        out.append(Tenant(
            name="snortpf/%s" % g.name, kind="pattern-set",
            input_view="l4-payload", patterns=pats, labels=labels,
            value_fn=_v,
            notes="%d rules on %d slots" % (g.rule_count, g.n_slots)))
    return out


# --------------------------------------------------------------------------
# Tenant 3 — IP matching (NEW; no new RTL)
# --------------------------------------------------------------------------
#: Structurally real, contents are SITE CONFIGURATION (SR13/SF16): these are
#: the net-variable prefixes the daemon evaluates host-side today, plus the
#: handful of literal-IP rule headers the corpus actually carries.  They are
#: NOT threat intelligence and are not presented as such.
DEFAULT_NET_SET = [
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",     # RFC1918 ($HOME_NET)
    "127.0.0.0/8", "169.254.0.0/16",                     # loopback/link-local
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",  # RFC5737 doc ranges
    "3.3.3.3/32",                                        # corpus: sid 251 et al
]


def _prefix_patterns(cidr: str, offset: int) -> Optional[bytes]:
    """A fixed-offset pattern matching any address in ``cidr``.

    Only prefixes on a byte boundary (/8, /16, /24, /32) are expressible as
    a literal + wildcard tail.  A non-byte-aligned prefix would need a byte
    CLASS on the partial byte; returning None keeps that case honest and
    visible rather than silently widening the match.
    """
    net = ipaddress.ip_network(cidr, strict=False)
    if net.version != 4:
        return None
    bits = net.prefixlen
    if bits % 8:
        return None
    nbytes = bits // 8
    literal = net.network_address.packed[:nbytes]
    return _at(offset, literal, wildcard_tail=4 - nbytes)


def ip_match_tenant(cidrs: Sequence[str] = None, match: str = "src"
                    ) -> Tenant:
    """Match source (or destination) IPv4 addresses against a prefix set.

    Implemented as fixed-offset patterns at frame byte 26 (src) or 30
    (dst) — no new hardware.  This offloads the net-variable membership
    test the daemon does host-side for every rule on every flow (SR13).
    """
    cidrs = list(cidrs if cidrs is not None else DEFAULT_NET_SET)
    offset = FRAME_OFFSETS["ip_src" if match == "src" else "ip_dst"]
    pats, labels, skipped = [], [], []
    for c in cidrs:
        p = _prefix_patterns(c, offset)
        if p is None:
            skipped.append(c)
            continue
        pats.append(p)
        labels.append("%s:%s" % (match, c))
    note = ("%d byte-aligned prefixes at frame offset %d; contents are SITE "
            "CONFIG (SR13), not threat data" % (len(pats), offset))
    if skipped:
        note += "; %d non-byte-aligned prefixes skipped: %s" % (
            len(skipped), ",".join(skipped))
    return Tenant(name="ip-match/%s" % match, kind="ip-match",
                  input_view="frame", patterns=pats, labels=labels,
                  value_fn=lambda d: d.packets, notes=note)


# --------------------------------------------------------------------------
# Tenant 4 — packet-header matching (NEW; no new RTL, REAL corpus data)
# --------------------------------------------------------------------------
def header_match_tenant(triaged=None, max_rules: int = 256) -> Tenant:
    """The corpus's 95 **header-only** rules (SF8), offloaded.

    These are decidable from the L3/L4 header alone and are handled
    host-side today (SR13).  79 are ICMP itype/icode, which lower to a
    literal at frame offset 34/35; the rest key on proto, ports, TTL or
    flags.  Real Snort data, real host work removed — the honest
    header-matching workload.
    """
    import os
    from ..snort import triage as _triage

    if triaged is None:
        corpus = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            "third_party", "snort3-community-rules",
            "snort3-community.rules")
        triaged = _triage.triage_file(corpus)

    pats, labels, undecidable = [], [], 0
    for rule, res in triaged:
        if res.tier != _triage.TIER_HEADER_ONLY:
            continue
        opts = {}
        for o in rule.options:
            opts.setdefault(o.key, o.value)
        sid = opts.get("sid", "?")
        proto = rule.proto.lower()

        pat = None
        if proto == "icmp" and "itype" in opts:
            try:
                itype = int(str(opts["itype"]).strip())
            except (TypeError, ValueError):
                itype = None
            if itype is not None and 0 <= itype <= 255:
                if "icode" in opts:
                    try:
                        icode = int(str(opts["icode"]).strip())
                    except (TypeError, ValueError):
                        icode = None
                    if icode is not None and 0 <= icode <= 255:
                        pat = _at(FRAME_OFFSETS["icmp_type"],
                                  bytes([itype, icode]))
                if pat is None:
                    pat = _at(FRAME_OFFSETS["icmp_type"], bytes([itype]))
        elif proto in _PROTO_NUM and "ttl" in opts:
            try:
                ttl = int(str(opts["ttl"]).strip().lstrip("<>="))
                pat = _at(FRAME_OFFSETS["ip_ttl"], bytes([ttl & 0xFF]))
            except (TypeError, ValueError):
                pat = None
        elif proto in _PROTO_NUM:
            # Fall back to pinning the IP protocol number: a genuine
            # (over-approximating) necessary condition, which is exactly
            # the SR3 contract the rest of the system runs on.
            pat = _at(FRAME_OFFSETS["ip_proto"], bytes([_PROTO_NUM[proto]]))

        if pat is None:
            undecidable += 1
            continue
        pats.append(pat)
        labels.append("1:%s" % sid)
        if len(pats) >= max_rules:
            break

    return Tenant(
        name="header-match", kind="header", input_view="frame",
        patterns=pats, labels=labels, value_fn=lambda d: d.packets,
        notes="%d of the corpus's header-only rules as fixed-offset frame "
              "patterns (%d not expressible without byte-class masks)"
              % (len(pats), undecidable))


# --------------------------------------------------------------------------
def build_all_tenants(include_snortpf: int = 3) -> List[Tenant]:
    """The four-tenant mixed workload.  ``include_snortpf`` bounds how many
    rule groups to include so the set stays inspectable."""
    out = [pyro_regex_tenant(),
           ip_match_tenant(),
           header_match_tenant()]
    out.extend(snortpf_tenants()[:include_snortpf])
    return out
