"""AC-S2-1 corpus gate: SR1 triage reproduces SF8-SF14 exactly.

Runs the full triage over the profiled snapshot of
``third_party/snort3-community-rules/snort3-community.rules`` (4,017
rules) and asserts every corpus ground truth from spec §1.2 (SF8-SF14, as
amended by 1.0.1: SR2 raw 1,759 / normalized 2,137 with the dce fold) as
a literal constant — the oracle numbers, restated here independently of
``pyro.snort.report.ORACLE`` so a defect in the invariant checker cannot
mask a defect in the triage.  Also asserts SR1 determinism (two parses of
the same file produce byte-identical reports).
"""

import json
import os

import pytest

from pyro.snort import report as SR
from pyro.snort import triage as ST

CORPUS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "third_party", "snort3-community-rules", "snort3-community.rules")

pytestmark = pytest.mark.skipif(
    not os.path.exists(CORPUS), reason="community ruleset not vendored")


@pytest.fixture(scope="module")
def report():
    rep = SR.build_report(CORPUS)
    assert rep["corpus"]["profiled_snapshot"], (
        "corpus SHA-256 drifted from the SF8-SF14 profile snapshot; "
        "re-baseline the oracle numbers via spec change control (SR2), "
        "do not edit this test")
    return rep


def test_rule_count_and_tiers(report):
    agg = report["aggregates"]
    assert report["corpus"]["rule_count"] == 4017
    assert agg["tiers"] == {"header-only": 95, "anchor-compilable": 3896,
                            "always-forward": 26}
    # zero-positive-content split: 121 = 95 header-only + 26 always-forward
    assert agg["zero_positive_content_rules"] == 121


def test_header_only_proto_breakdown(report):
    assert report["aggregates"]["header_only_by_proto"] == {
        "icmp": 79, "tcp": 10, "udp": 5, "ip": 1}


def test_subtiers_and_buffer_histogram(report):
    # SR2 amendment 1.0.1: exactly two sub-tier values; sid 42886
    # (http_header:field user-agent) is normalized-buffer, and the 5
    # dce_stub_data rules fold into normalized-buffer (2,132 + 5).
    agg = report["aggregates"]
    assert agg["subtiers"] == {"raw-anchor": 1759,
                               "normalized-buffer": 2137}
    bh = agg["buffer_histogram"]
    assert bh["raw_pkt_data"] == 1759
    assert bh["http_textual"] == 1893
    assert bh["file_data"] == 239
    assert bh["dce_stub_data"] == 5
    assert bh.get("other_normalized", 0) == 0
    assert sum(bh.values()) == 3896
    # dce stays visible in the detail histogram (SR2 note)
    assert agg["buffer_detail"]["dce_stub_data"] == 5


def test_anchor_statistics(report):
    agg = report["aggregates"]
    assert agg["declared_fast_pattern_anchors"] == 2168
    assert agg["unique_anchors"] == {"count": 3199, "total_bytes": 57849}
    assert agg["anchor_length"] == {
        "count": 3896, "min": 1, "p10": 4, "p25": 7, "median": 12,
        "p75": 20, "p90": 35, "max": 214, "mean": 16.5,
        "under_4_bytes": 213}


def test_pcre_classification(report):
    p = report["aggregates"]["pcre"]
    assert p["occurrences"] == 1080 and p["rules"] == 1027
    assert (p["clean"], p["backref"], p["lookaround"], p["reject"]) == \
        (799, 239, 42, 0)
    assert p["clean"] + p["backref"] + p["lookaround"] + p["reject"] == 1080
    assert p["r_flag_occurrences"] == 111
    assert p["clean_repeats_ge_256"] == 63
    assert p["clean_repeat_bound_p90"] == 432
    assert p["clean_repeat_bound_max"] == 1075


def test_sf8_header_shape(report):
    hdr = report["aggregates"]["header"]
    assert hdr["proto"] == {"tcp": 3637, "udp": 211, "icmp": 125,
                            "ip": 22, "http": 20, "ssl": 2}
    dp = hdr["dst_port"]
    for port, want in [("$HTTP_PORTS", 2001), ("any", 765),
                       ("$ORACLE_PORTS", 291), ("25", 121), ("21", 86),
                       ("111", 63), ("445", 49), ("139", 46), ("143", 34),
                       ("53", 28)]:
        assert dp.get(port) == want, (port, dp.get(port), want)
    var = hdr["variables"]
    for name, want in [("$EXTERNAL_NET", 3947), ("$HOME_NET", 2556),
                       ("$HTTP_SERVERS", 954), ("$SQL_SERVERS", 318)]:
        assert var.get(name) == want, (name, var.get(name), want)
    assert hdr["distinct_variables"] == 13


def test_self_check_invariants_green(report):
    assert report["invariant_failures"] == []


def test_per_rule_rows_shape(report):
    rows = report["rules"]
    assert len(rows) == 4017
    for row in rows[:50]:
        assert set(row) >= {"gid", "sid", "msg", "tier", "subtier",
                            "anchor", "dropped_options", "reason"}
    # spot-check sid:105 (corpus line 1): 16-byte raw anchor (1 printable
    # + 7 hex pairs + 6 printables + 2 hex pairs; matches its depth 16)
    row = next(r for r in rows if r["sid"] == 105)
    assert row["tier"] == "anchor-compilable"
    assert row["subtier"] == "raw-anchor"
    assert row["anchor"]["length"] == 16
    assert row["anchor"]["buffer"] == "pkt_data"
    # spot-check sid:224 (line 37): content-free icmp, header-only (SF8)
    row = next(r for r in rows if r["sid"] == 224)
    assert row["tier"] == "header-only" and row["anchor"] is None
    # spot-check sid:42886 (line 3484, SR2 amendment 1.0.1): the anchor
    # follows http_header:field user-agent — normalized-buffer, not raw
    row = next(r for r in rows if r["sid"] == 42886)
    assert row["subtier"] == "normalized-buffer"
    assert row["anchor"]["buffer"] == "http_header"


def test_sr1_determinism(report):
    again = SR.build_report(CORPUS)
    assert json.dumps(report, sort_keys=True) == \
        json.dumps(again, sort_keys=True)


def test_tier_flow_conservation(report):
    # Every rule lands in exactly one tier (SR1 "exactly one tier").
    agg = report["aggregates"]
    assert sum(agg["tiers"].values()) == report["corpus"]["rule_count"]
    assert sum(agg["subtiers"].values()) == agg["tiers"]["anchor-compilable"]
    assert agg["subtiers"]["raw-anchor"] == \
        report["aggregates"]["buffer_histogram"]["raw_pkt_data"]
