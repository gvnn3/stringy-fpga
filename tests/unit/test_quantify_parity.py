"""Unit tests for pyro.quantify.parity and pyro.quantify.fpga_bench.

Covers: the direct-reference oracle on synthetic corpora (overlapping
anchors, an anchor that is a substring of another), the parity pass
rules for both sides (Snort exact, FPGA superset-benign), and the
fpga_bench cell flow against a scripted fake device (JSON contract
shape, nomination mapping, counter fallbacks, timing math).  No
hardware, no network, no transports.
"""
import json
import os
from types import SimpleNamespace

import pytest

from pyro.quantify import fpga_bench, parity, traffic
from pyro.telemetry import MatchReplySummary

# ---------------------------------------------------------------------
# synthetic corpus helpers
# ---------------------------------------------------------------------


def _write_corpus(dirpath, payloads, *, group="g/0", density=0.0):
    """Hand-built payloads.bin + manifest.json (contract shape)."""
    os.makedirs(str(dirpath), exist_ok=True)
    packets, off = [], 0
    for i, p in enumerate(payloads):
        packets.append({"i": i, "off": off, "len": len(p),
                        "hits": []})
        off += len(p)
    manifest = {"schema": "pyro-quantify/1", "seed": 0,
                "group": group, "density": density,
                "count": len(payloads), "payload_bytes": off,
                "payloads_bin": "payloads.bin", "packets": packets}
    with open(os.path.join(str(dirpath), "payloads.bin"), "wb") as f:
        f.write(b"".join(payloads))
    with open(os.path.join(str(dirpath), "manifest.json"), "w") as f:
        json.dump(manifest, f)
    return manifest


# ---------------------------------------------------------------------
# oracle: occurrence sweep
# ---------------------------------------------------------------------


def test_occurrences_overlapping():
    assert parity.occurrences(b"aba", b"ababa") == [0, 2]
    assert parity.occurrences(b"aa", b"aaaa") == [0, 1, 2]
    assert parity.occurrences(b"x", b"aaa") == []
    assert parity.occurrences(b"", b"aaa") == []


def test_oracle_substring_and_overlap(tmp_path):
    # Pattern index space: sorted unique anchors.
    anchors = [b"needle", None, b"need", b"aba", b""]
    patterns = traffic.unique_patterns(anchors)
    assert patterns == [b"aba", b"need", b"needle"]

    payloads = [
        b"xx" + b"needle" + b"xx",   # hits "need" AND "needle"
        b"zz" + b"ababa" + b"zz",    # overlapping "aba" occurrences
        b"nothing to see here....",  # no hits
        b"need" + b"aba",            # substring anchor alone + "aba"
    ]
    _write_corpus(tmp_path, payloads)
    hits = parity.oracle_for_corpus(anchors, str(tmp_path))
    assert hits == {0: {1, 2}, 1: {0}, 3: {0, 1}}


def test_oracle_covers_generated_ground_truth(tmp_path):
    """Every generator-embedded hit must be found by the oracle."""
    anchors = [b"GET /index", b"\x00\x01evil\xff", b"needle"]
    man = traffic.generate(str(tmp_path), anchors, seed=7,
                           density=0.2, count=100,
                           group_name="g/0")
    hits = parity.oracle_for_corpus(anchors, str(tmp_path))
    embedded = 0
    for pkt in man["packets"]:
        for h in pkt["hits"]:
            embedded += 1
            assert h["pat"] in hits.get(pkt["i"], set())
    assert embedded == 20


def test_oracle_rejects_bad_schema(tmp_path):
    _write_corpus(tmp_path, [b"abc"])
    path = os.path.join(str(tmp_path), "manifest.json")
    with open(path) as f:
        man = json.load(f)
    man["schema"] = "other/9"
    with open(path, "w") as f:
        json.dump(man, f)
    with pytest.raises(ValueError):
        parity.oracle_for_corpus([b"abc"], str(tmp_path))


# ---------------------------------------------------------------------
# compare: pass rules and contract shape
# ---------------------------------------------------------------------

MAN = {"schema": "pyro-quantify/1", "group": "g/0", "density": 0.1,
       "count": 4}
ORACLE = {0: {1, 2}, 2: {0}}


def test_compare_snort_exact_pass():
    res = parity.compare(MAN, ORACLE, "snort", {0: {1, 2}, 2: {0}})
    assert res["pass"] is True
    assert res["missing"] == [] and res["extra"] == []
    assert res["oracle_hits"] == 3
    assert res["packets"] == 4


def test_compare_snort_fails_on_extra():
    obs = {0: {1, 2}, 2: {0}, 3: {1}}
    res = parity.compare(MAN, ORACLE, "snort", obs)
    assert res["pass"] is False
    assert res["missing"] == []
    assert res["extra"] == [[3, 1]]
    assert res["extra_count"] == 1


def test_compare_snort_fails_on_missing():
    res = parity.compare(MAN, ORACLE, "snort", {0: {1}, 2: {0}})
    assert res["pass"] is False
    assert res["missing"] == [[0, 2]]
    assert res["extra"] == []


def test_compare_fpga_superset_is_benign():
    obs = {0: {1, 2, 3}, 1: {0}, 2: {0}}
    res = parity.compare(MAN, ORACLE, "fpga", obs)
    assert res["pass"] is True
    assert res["missing"] == []
    assert res["extra"] == [[0, 3], [1, 0]]
    assert res["extra_count"] == 2


def test_compare_fpga_fails_on_missing():
    obs = {0: {1, 2, 3}}          # oracle's (2, 0) never nominated
    res = parity.compare(MAN, ORACLE, "fpga", obs)
    assert res["pass"] is False
    assert res["missing"] == [[2, 0]]


def test_compare_empty_oracle_and_empty_observed_pass():
    for side in ("snort", "fpga"):
        res = parity.compare(MAN, {}, side, {})
        assert res["pass"] is True
        assert res["oracle_hits"] == 0


def test_compare_rejects_unknown_side():
    with pytest.raises(ValueError):
        parity.compare(MAN, ORACLE, "suricata", {})


def test_compare_contract_shape():
    res = parity.compare(MAN, ORACLE, "fpga", {0: {1, 2}, 2: {0}})
    assert set(res) == {"schema", "group", "density", "side",
                        "packets", "oracle_hits", "missing", "extra",
                        "extra_count", "pass"}
    assert res["schema"] == "pyro-quantify-parity/1"
    assert res["group"] == "g/0"
    assert res["density"] == 0.1
    assert res["side"] == "fpga"
    json.dumps(res)               # JSON-serializable throughout


# ---------------------------------------------------------------------
# fpga_bench: nomination mapping
# ---------------------------------------------------------------------

# Slot order (pid space): dup anchors at pids 0 and 3 (nocase/exact
# variants of one byte-string), tombstone at pid 1.
SLOT_ANCHORS = (b"beta", None, b"alpha", b"beta")


def test_pattern_index_map_dedup_and_tombstones():
    m = fpga_bench.pattern_index_map(SLOT_ANCHORS)
    # unique sorted: [b"alpha", b"beta"]
    assert m == {0: 1, 2: 0, 3: 1}
    assert 1 not in m             # tombstone pid never mapped


# ---------------------------------------------------------------------
# fpga_bench: cell flow against a scripted fake device
# ---------------------------------------------------------------------


class FakeStatus:
    def __init__(self, epoch):
        self.epoch = epoch


class FakeDevice:
    """Scripted device: replies/counters consumed in scan order."""

    def __init__(self, replies, perfs, epoch0=4):
        self.replies = list(replies)
        self.perfs = list(perfs)
        self.epoch = epoch0
        self.loaded = []
        self.scanned = []

    def read_table_status(self):
        return FakeStatus(self.epoch)

    def load_table(self, image):
        self.loaded.append(image)
        self.epoch += 1
        return FakeStatus(self.epoch)

    def scan(self, subject):
        self.scanned.append(subject)
        return self.replies.pop(0)

    def read_perf_counters(self):
        return self.perfs.pop(0)


class FakeClock:
    """Monotonic clock advancing 1 s per reading."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


def _cell_fixture(tmp_path):
    payloads = [b"beta stew", b"alpha!", b"zzzz", b"no hits at all"]
    man = _write_corpus(tmp_path, payloads, group="g/0", density=0.25)
    table = fpga_bench.GroupTable("g/0", b"\x00IMG" * 8, 17,
                                  SLOT_ANCHORS)
    fp = fpga_bench._PERF_FINGERPRINT
    replies = [
        # pids 0 and 3 both map to study index 1 (dedup).
        MatchReplySummary(2, False, 5, ((0, 0, 4), (3, 0, 9))),
        MatchReplySummary(1, True, 5, ((2, 0, 5),)),
        None,                                     # request loss
        MatchReplySummary(0, False, 5, ()),       # clean zero-match
    ]
    perfs = [(1000, 200), (2000, 400), None, (fp, fp)]
    return man, payloads, table, FakeDevice(replies, perfs)


def test_bench_cell_contract_and_perf_math(tmp_path):
    man, payloads, table, dev = _cell_fixture(tmp_path)
    res = fpga_bench.bench_cell(dev, str(tmp_path), table,
                                clock=FakeClock())

    assert set(res) == {"schema", "group", "density", "table_bytes",
                        "n_states", "load_s", "epoch_before",
                        "epoch_after", "scans"}
    assert res["schema"] == "pyro-quantify-fpga/1"
    assert res["group"] == "g/0"
    assert res["density"] == 0.25
    assert res["table_bytes"] == len(table.image)
    assert res["n_states"] == 17
    assert res["epoch_before"] == 4
    assert res["epoch_after"] == 5
    assert res["load_s"] == pytest.approx(1.0)   # one clock tick
    assert dev.loaded == [table.image]
    # Scanned subjects are the manifest payload slices, in order.
    assert dev.scanned == payloads

    scans = res["scans"]
    assert [s["i"] for s in scans] == [0, 1, 2, 3]
    assert all(s["wall_s"] == pytest.approx(1.0) for s in scans)

    # Scan 0: two pids collapse to one study index; counters taken.
    assert scans[0]["noms"] == [1]
    assert scans[0]["ovf"] is False
    assert (scans[0]["cycles"], scans[0]["bytes"]) == (1000, 200)
    # Scan 1: OVF propagated; engine-rate math per design doc E1.
    assert scans[1]["noms"] == [0]
    assert scans[1]["ovf"] is True
    rate = (scans[1]["bytes"] * fpga_bench.F_ENGINE_HZ
            / scans[1]["cycles"])
    assert rate == pytest.approx(50e6)
    # Scan 2: request loss — counted, empty noms, counters None.
    assert scans[2]["lost"] is True
    assert scans[2]["noms"] == []
    assert scans[2]["cycles"] == 0
    assert scans[2]["bytes"] == len(payloads[2])
    # Scan 3: pre-fix fingerprint counters nulled, zero-match reply.
    assert "lost" not in scans[3]
    assert scans[3]["noms"] == []
    assert scans[3]["cycles"] == 0
    assert scans[3]["bytes"] == len(payloads[3])
    json.dumps(res)


def test_bench_cell_feeds_parity_referee(tmp_path):
    """End-to-end: bench scans -> observed sets -> fpga pass rule."""
    man, payloads, table, dev = _cell_fixture(tmp_path)
    res = fpga_bench.bench_cell(dev, str(tmp_path), table,
                                clock=FakeClock())
    observed = {s["i"]: set(s["noms"])
                for s in res["scans"] if s["noms"]}
    # Oracle over the same slot anchors: pkt0 has "beta", pkt1 has
    # "alpha" — exactly what the fake device nominated.
    oracle = parity.oracle_for_corpus(table.anchors, str(tmp_path))
    verdict = parity.compare(man, oracle, "fpga", observed)
    assert verdict["pass"] is True
    assert verdict["extra_count"] == 0


def test_bench_cell_unknown_pid_raises(tmp_path):
    _write_corpus(tmp_path, [b"x"], group="g/0")
    table = fpga_bench.GroupTable("g/0", b"IMG", 3, SLOT_ANCHORS)
    dev = FakeDevice([MatchReplySummary(1, False, 5, ((1, 0, 1),))],
                     [(1, 1)])
    with pytest.raises(ValueError):
        fpga_bench.bench_cell(dev, str(tmp_path), table)


def test_bench_cell_group_mismatch_raises(tmp_path):
    _write_corpus(tmp_path, [b"x"], group="other/0")
    table = fpga_bench.GroupTable("g/0", b"IMG", 3, SLOT_ANCHORS)
    with pytest.raises(ValueError):
        fpga_bench.bench_cell(FakeDevice([], []), str(tmp_path),
                              table)


def test_bench_cell_no_table_status_means_epoch_zero(tmp_path):
    _write_corpus(tmp_path, [b"x"], group="g/0")
    table = fpga_bench.GroupTable("g/0", b"IMG", 3, SLOT_ANCHORS)
    dev = FakeDevice([MatchReplySummary(0, False, 1, ())], [(5, 1)])
    dev.read_table_status = lambda: None          # pre-A5 child
    res = fpga_bench.bench_cell(dev, str(tmp_path), table)
    assert res["epoch_before"] == 0
    assert res["epoch_after"] == 5


def test_write_result_round_trips(tmp_path):
    man, payloads, table, dev = _cell_fixture(tmp_path)
    res = fpga_bench.bench_cell(dev, str(tmp_path), table,
                                clock=FakeClock())
    out = os.path.join(str(tmp_path), "fpga.json")
    fpga_bench.write_result(res, out)
    with open(out) as f:
        assert json.load(f) == res


# ---------------------------------------------------------------------
# group_table_from: lowering a (fake) RuleGroup
# ---------------------------------------------------------------------


def _fake_group():
    slots = (SimpleNamespace(tombstone=False, anchor=b"abc"),
             SimpleNamespace(tombstone=True, anchor=b"dead"),
             SimpleNamespace(tombstone=False, anchor=b"bcd"))
    return SimpleNamespace(name="g/1", slots=slots)


def test_group_table_from_builds_serialized_image():
    gt = fpga_bench.group_table_from(_fake_group())
    assert gt.name == "g/1"
    assert gt.anchors == (b"abc", None, b"bcd")
    assert gt.image.startswith(b"PYROTBL\x00")
    assert gt.n_states > 0
    assert len(gt.image) > 56     # header + at least the root state


def test_group_table_from_enforces_capacity():
    with pytest.raises(ValueError):
        fpga_bench.group_table_from(_fake_group(), capacity=1)
