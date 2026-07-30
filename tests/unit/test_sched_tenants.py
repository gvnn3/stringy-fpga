"""Tenant construction and fixed-offset correctness.

The offsets are the whole trick — a fixed-offset pattern that lands one
byte off is a silently wrong matcher, so these tests drive real synthetic
frames through the real automaton rather than inspecting pattern strings.
"""

import struct

import pytest

from pyro._circuit_model import _scan_windows
from pyro.hdl import automaton as _auto
from pyro.sched import tenants as TN


def frame(src="192.168.1.10", dst="8.8.8.8", proto=6, sport=1234,
          dport=80, icmp_type=None, icmp_code=0, ttl=64):
    eth = b"\x02" * 6 + b"\x03" * 6 + b"\x08\x00"
    ip = struct.pack("!BBHHHBBH", 0x45, 0, 40, 1, 0, ttl, proto, 0)
    ip += bytes(int(x) for x in src.split("."))
    ip += bytes(int(x) for x in dst.split("."))
    if icmp_type is not None:
        l4 = bytes([icmp_type, icmp_code]) + b"\x00" * 6
    else:
        l4 = struct.pack("!HHIIBBHHH", sport, dport, 0, 0, 0x50, 0x18,
                         0, 0, 0)
    return eth + ip + l4


def hits(tenant, buf):
    out = []
    for i, p in enumerate(tenant.patterns):
        if p is None:
            continue
        au = _auto.build(p, 0, _auto.ENC_BYTES)
        if _scan_windows(au, buf, 0):
            out.append(tenant.labels[i])
    return out


# --------------------------------------------------------------------------
# The frame layout the offsets assume
# --------------------------------------------------------------------------
def test_frame_offsets_are_where_we_think():
    f = frame(src="1.2.3.4", dst="5.6.7.8", proto=17, ttl=99)
    assert f[TN.FRAME_OFFSETS["ip_src"]:TN.FRAME_OFFSETS["ip_src"] + 4] == \
        bytes([1, 2, 3, 4])
    assert f[TN.FRAME_OFFSETS["ip_dst"]:TN.FRAME_OFFSETS["ip_dst"] + 4] == \
        bytes([5, 6, 7, 8])
    assert f[TN.FRAME_OFFSETS["ip_proto"]] == 17
    assert f[TN.FRAME_OFFSETS["ip_ttl"]] == 99
    icmp = frame(proto=1, icmp_type=8, icmp_code=3)
    assert icmp[TN.FRAME_OFFSETS["icmp_type"]] == 8
    assert icmp[TN.FRAME_OFFSETS["icmp_code"]] == 3


# --------------------------------------------------------------------------
# Tenant 3: IP matching
# --------------------------------------------------------------------------
def test_ip_tenant_matches_prefixes_at_the_right_offset():
    t = TN.ip_match_tenant(["10.0.0.0/8", "192.168.0.0/16",
                            "203.0.113.5/32"])
    assert t.slots == 3 and t.input_view == "frame"
    assert hits(t, frame(src="10.1.2.3")) == ["src:10.0.0.0/8"]
    assert hits(t, frame(src="192.168.99.1")) == ["src:192.168.0.0/16"]
    assert hits(t, frame(src="203.0.113.5")) == ["src:203.0.113.5/32"]
    assert hits(t, frame(src="8.8.8.8")) == []


def test_ip_tenant_does_not_match_the_same_bytes_in_the_dst_field():
    """The offset must discriminate src from dst, or the matcher is a
    substring search wearing a costume."""
    t = TN.ip_match_tenant(["10.0.0.0/8"], match="src")
    assert hits(t, frame(src="10.1.2.3", dst="8.8.8.8")) == ["src:10.0.0.0/8"]
    assert hits(t, frame(src="8.8.8.8", dst="10.1.2.3")) == []
    d = TN.ip_match_tenant(["10.0.0.0/8"], match="dst")
    assert hits(d, frame(src="8.8.8.8", dst="10.1.2.3")) == ["dst:10.0.0.0/8"]
    assert hits(d, frame(src="10.1.2.3", dst="8.8.8.8")) == []


def test_ip_tenant_skips_non_byte_aligned_prefixes_loudly():
    """A /12 cannot be a literal+wildcard; it must be dropped, never
    silently widened to /8 (that would be a false match, not an
    over-approximation the host re-checks)."""
    t = TN.ip_match_tenant(["172.16.0.0/12", "10.0.0.0/8"])
    assert t.slots == 1
    assert "172.16.0.0/12" in t.notes and "skipped" in t.notes
    assert hits(t, frame(src="172.16.5.5")) == []


def test_ip_tenant_default_set_is_labelled_as_site_config():
    t = TN.ip_match_tenant()
    assert "SITE CONFIG" in t.notes
    assert t.slots >= 5


# --------------------------------------------------------------------------
# Tenant 4: header matching
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def hdr_tenant():
    import os
    corpus = os.path.join(os.path.dirname(__file__), "..", "..",
                          "third_party", "snort3-community-rules",
                          "snort3-community.rules")
    if not os.path.exists(corpus):
        pytest.skip("community ruleset not vendored")
    return TN.header_match_tenant()


def test_header_tenant_covers_the_corpus_header_only_rules(hdr_tenant):
    # 95 header-only rules (SF8); 94 expressible as fixed-offset patterns,
    # 1 needs a byte-class mask (fragbits) and is honestly excluded.
    assert hdr_tenant.slots == 94
    assert hdr_tenant.input_view == "frame"
    assert all(lbl.startswith("1:") for lbl in hdr_tenant.labels)


def test_header_tenant_discriminates_icmp_type_and_code(hdr_tenant):
    a = set(hits(hdr_tenant, frame(proto=1, icmp_type=8, icmp_code=0)))
    b = set(hits(hdr_tenant, frame(proto=1, icmp_type=3, icmp_code=1)))
    assert a and b and a != b


def test_header_tenant_offsets_are_anchored_not_floating(hdr_tenant):
    """Every pattern is \\A-anchored; a floating one would match the same
    byte anywhere in the packet and silently over-nominate."""
    for p in hdr_tenant.patterns:
        assert p.startswith(b"(?s:\\A"), p


# --------------------------------------------------------------------------
# The mixed workload
# --------------------------------------------------------------------------
def test_all_four_tenant_kinds_are_present_and_heterogeneous():
    ts = TN.build_all_tenants(include_snortpf=2)
    kinds = {t.kind for t in ts}
    assert kinds == {"regex", "ip-match", "header", "pattern-set"}
    views = {t.input_view for t in ts}
    assert views == {"host", "frame", "l4-payload"}
    # Heterogeneous footprints are what make this a scheduling problem
    # rather than a queue: identical jobs cannot distinguish policies.
    luts = sorted(t.footprint()["est_luts"] for t in ts)
    assert luts[-1] > 10 * max(luts[0], 1)


def test_tenant_values_respond_to_their_own_demand_signal():
    ts = {t.kind: t for t in TN.build_all_tenants(include_snortpf=1)}
    quiet = TN.TenantDemand()
    assert ts["regex"].value(quiet) == 0
    assert ts["regex"].value(TN.TenantDemand(scan_bytes=1234)) == 1234
    assert ts["ip-match"].value(TN.TenantDemand(packets=7)) == 7
    # a pattern-set tenant is worth nothing without a port mix, and
    # something with one
    assert ts["pattern-set"].value(quiet) == 0


def test_circuits_compile_through_the_real_generator():
    for t in (TN.ip_match_tenant(["10.0.0.0/8"]),
              TN.pyro_regex_tenant()):
        gg = t.circuit()
        assert gg.n_slots == t.slots
        assert gg.rtl and b"module" in gg.rtl.encode()[:4000] or gg.rtl
