"""Unit tests for pyro.quantify.snort_lower / snort_bench.

Covers the traps in the Snort side of the fpga-vs-snort study:
content escaping of rule-syntax and non-printable bytes, the
sid = 1000000 + index identity, fast-alert line parsing, and the
timestamp -> packet-index inversion.  One integration test runs the
real extracted Snort 2.9 tree when build/snort-local/root is present
and is skipped cleanly otherwise.
"""

import json
import os

import pytest

from pyro.quantify import snort_bench as B
from pyro.quantify import snort_lower as L

# Bytes that are rule syntax inside content:"..." plus non-printable
# extremes, spaces, and a mixed tail -- the round-trip gauntlet.
NASTY = [
    b"a;b",
    b'q"w',
    b"p|q",
    b"back\\slash",
    b"\x00",
    b"\xff",
    b" leading and trailing ",
    b'ev;il"str|ing\\\x00\xff end',
    b"\x01\x7f\x80 mid\ttab\nnl",
]


# --- escaping -------------------------------------------------------------


def test_escape_roundtrip_nasty():
    for anchor in NASTY:
        esc = L.escape_content(anchor)
        assert L.unescape_content(esc) == anchor, anchor


def test_escape_emits_no_forbidden_raw_bytes():
    for anchor in NASTY:
        esc = L.escape_content(anchor)
        # Outside |..| hex runs no ; " backslash, and every char is
        # printable ASCII; inside runs only hex digits and spaces.
        inside = False
        for c in esc:
            if c == "|":
                inside = not inside
                continue
            if inside:
                assert c in "0123456789ABCDEF "
            else:
                assert c not in ';"\\'
                assert 0x20 <= ord(c) <= 0x7E


def test_escape_plain_text_untouched():
    assert L.escape_content(b"evilstring") == "evilstring"
    assert L.escape_content(b"GET /index.html") == "GET /index.html"


def test_escape_groups_hex_runs():
    assert L.escape_content(b"a\x00\xffb") == "a|00 FF|b"


# --- rules emission -------------------------------------------------------


def test_write_rules_sid_mapping(tmp_path):
    anchors = [b"alpha", b"beta;x", b"\x00gamma"]
    rules_path, conf_path = L.write_rules(str(tmp_path), anchors,
                                          "$SIP_PORTS/0")
    lines = [ln for ln in open(rules_path).read().splitlines()
             if ln and not ln.startswith("#")]
    assert len(lines) == len(anchors)
    for i, (anchor, line) in enumerate(zip(anchors, lines)):
        assert "sid:%d;" % (L.SID_BASE + i) in line
        start = line.index('content:"') + len('content:"')
        end = line.index('";', start)
        assert L.unescape_content(line[start:end]) == anchor


def test_write_rules_conf_shape(tmp_path):
    rules_path, conf_path = L.write_rules(str(tmp_path), [b"x"],
                                          "any/0")
    conf = open(conf_path).read().splitlines()
    assert conf[0] == "config detection: search-method ac-q"
    assert conf[1] == "include %s" % os.path.abspath(rules_path)
    assert len(conf) == 2


def test_write_rules_empty_is_baseline(tmp_path):
    rules_path, _conf = L.write_rules(str(tmp_path), [], "baseline")
    body = [ln for ln in open(rules_path).read().splitlines()
            if ln and not ln.startswith("#")]
    assert body == []


def test_write_rules_rejects_empty_anchor(tmp_path):
    with pytest.raises(ValueError):
        L.write_rules(str(tmp_path), [b"ok", b""], "g")


def test_sanitize_name():
    assert L.sanitize_name("$SIP_PORTS/0") == "SIP_PORTS_0"
    assert L.sanitize_name("literal/3") == "literal_3"


# --- alert parsing --------------------------------------------------------

FIXTURE_ALERT = ('01/01/70-00:00:00.000002  [**] [1:1000000:1] '
                 'pyro-quantify smoke_0 pat 0 [**] [Priority: 0] '
                 '{TCP} 10.0.0.1:12345 -> 10.0.0.2:80')


def test_parse_alert_line_fixture():
    assert B.parse_alert_line(FIXTURE_ALERT) == (2, 1000000)


def test_parse_alert_line_large_index():
    line = ('01/01/70-00:00:03.500042  [**] [1:1000017:1] m [**] '
            '[Priority: 0] {TCP} 10.0.0.1:1 -> 10.0.0.2:2')
    assert B.parse_alert_line(line) == (3500042, 1000017)


def test_parse_alert_line_rejects_noise():
    assert B.parse_alert_line("") is None
    assert B.parse_alert_line("not an alert line") is None


def test_timestamp_to_index_roundtrip():
    import time
    for index in (0, 1, 999999, 1000000, 3500042, 86400 * 1000000):
        sec, usec = divmod(index, 1000000)
        stamp = time.strftime("%m/%d/%y-%H:%M:%S", time.gmtime(sec))
        assert B.timestamp_to_index(stamp, usec) == index


def test_parse_alert_file(tmp_path):
    path = tmp_path / "alert"
    path.write_text(FIXTURE_ALERT + "\n" + FIXTURE_ALERT + "\n")
    assert B.parse_alert_file(str(path)) == [
        {"pkt": 2, "sid": 1000000},
        {"pkt": 2, "sid": 1000000},
    ]
    assert B.parse_alert_file(str(tmp_path / "missing")) == []


# --- summary parsing ------------------------------------------------------

FIXTURE_SUMMARY = """\
Commencing packet processing (pid=1361601)
===========================================================
Run time for packet processing was 1.612 seconds
Snort processed 3 packets.
Snort ran for 0 days 0 hours 0 minutes 1 seconds
   Pkts/sec:            3
===========================================================
Packet I/O Totals:
   Received:            3
   Analyzed:            3 (100.000%)
"""


def test_parse_summary_fixture():
    assert B.parse_summary(FIXTURE_SUMMARY) == (1.612, 3)


def test_parse_summary_absent():
    assert B.parse_summary("nothing to see") == (None, None)


# --- integration: the real local snort ------------------------------------


@pytest.mark.skipif(not B.snort_available(),
                    reason="build/snort-local/root absent")
def test_run_bench_real_snort(tmp_path):
    corpus = tmp_path / "corpus"
    out = tmp_path / "out"
    corpus.mkdir()

    payloads = [b"xx evilstring yy", b"benign payload here"]
    B.write_pcap(str(corpus / "corpus.pcap"), payloads)
    off = 0
    pkts = []
    with open(corpus / "payloads.bin", "wb") as f:
        for i, p in enumerate(payloads):
            f.write(p)
            pkts.append({"i": i, "off": off, "len": len(p),
                         "hits": []})
            off += len(p)
    manifest = {"schema": "pyro-quantify/1", "seed": 7,
                "group": "itest/0", "density": 0.5,
                "count": len(payloads), "payload_bytes": off,
                "payloads_bin": "payloads.bin", "packets": pkts}
    (corpus / "manifest.json").write_text(json.dumps(manifest))

    rules_path, conf_path = L.write_rules(str(out), [b"evilstring"],
                                          "itest/0")
    results = B.run_bench(str(corpus), rules_path, conf_path,
                          str(out), repeats=1)
    assert len(results) == 1
    r = results[0]
    assert r["schema"] == "pyro-quantify-snort/1"
    assert r["group"] == "itest/0"
    assert r["density"] == 0.5
    assert r["repeat"] == 0
    assert r["search_method"] == "ac-q"
    assert r["alerts"] == [{"pkt": 0, "sid": 1000000}]
    assert r["packets"] == len(payloads)
    assert r["payload_bytes"] == off
    assert r["run_s"] >= 0.0
    assert r["wall_s"] > 0.0
    assert r["startup_s"] > 0.0
    assert r["maxrss_kb"] > 0
    # The per-repeat result JSON is on disk and identical.
    on_disk = json.load(open(out / "snort-rep0.json"))
    assert on_disk == r

    # Re-running over the same outdir must not double-count: snort
    # APPENDS to an existing fast-alert file, so the bench has to
    # start each invocation with a fresh log dir.
    again = B.run_bench(str(corpus), rules_path, conf_path,
                        str(out), repeats=1)
    assert again[0]["alerts"] == [{"pkt": 0, "sid": 1000000}]
