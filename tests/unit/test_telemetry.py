"""Telemetry: snapshot shape, Prometheus form, accumulation, sid mapping.

No hardware anywhere: every test runs with ``cfg=None`` (the defined
host-only disposition — device section present, ``usable: false``, nothing
raises) or against synthetic state.  Corpus-derived numbers are injected as
small synthetic summaries so the suite never pays the multi-second
``pack_groups`` run (that laziness is itself part of the contract).
"""
import re

import pytest

import pyro.telemetry as T
from pyro.snort.groups import GroupSlot, RuleGroup, RuleRef


# --------------------------------------------------------------------------
# Fixtures: a small synthetic group whose anchors dedup (two rules, one slot)
# --------------------------------------------------------------------------
def tiny_group():
    """Slot 0 serves TWO rules (anchor dedup) — the sidecar list is the SR3
    fact under test: a hit must nominate every rule sharing the slot."""
    return RuleGroup("any", 0, (
        GroupSlot(0, b"/view-source", False,
                  (RuleRef(1, 100, "raw-anchor", "pkt_data", 1),
                   RuleRef(1, 101, "raw-anchor", "pkt_data", 2))),
        GroupSlot(1, b"/webspirs.cgi", False,
                  (RuleRef(1, 200, "raw-anchor", "pkt_data", 3),)),
        GroupSlot(2, b"dead", False, ()),          # tombstone
    ))


def reply(entries, ovf=False):
    return T.MatchReplySummary(len(entries), ovf, 7,
                               tuple((pid, 0, end) for pid, end in entries))


TINY_CORPUS = {
    "corpus_total_rules": 10,
    "groupable_rules": 7,
    "lowering_dropped": 2,
    "header_only_rules": 1,
    "always_forward_rules": 2,
    "groups": {"any/0": 3},
}


# --------------------------------------------------------------------------
# Snapshot shape (cfg=None: complete, host-only, never raises)
# --------------------------------------------------------------------------
def test_snapshot_shape_host_only():
    snap = T.collect_snapshot(cfg=None)
    # top-level contract keys, all REQUIRED
    for key in ("ts", "schema", "device", "switch", "matching", "loss",
                "missed", "scheduler"):
        assert key in snap, key
    assert snap["schema"] == "pyro-telemetry/1"
    assert isinstance(snap["ts"], float)

    dev = snap["device"]
    assert dev["usable"] is False
    assert dev["table"] is None and dev["perf"] is None

    sw = snap["switch"]
    for key in ("last_ms", "count", "history_ms", "wire_ms_est",
                "pr_baseline_s"):
        assert key in sw, key
    assert sw["pr_baseline_s"] == 13.6

    m = snap["matching"]
    for key in ("scans", "nominations_total", "by_sid", "ovf_events",
                "truncated_min"):
        assert key in m, key

    loss = snap["loss"]
    for key in ("netdev", "requests_sent", "replies_received",
                "request_loss"):
        assert key in loss, key
    assert loss["request_loss"] is None      # no requests: not measured 0

    ms = snap["missed"]
    for key in ("resident_hard_misses", "note", "ovf_truncation_events",
                "non_resident_rules", "lowering_dropped",
                "coverage_resident_rules", "corpus_total_rules"):
        assert key in ms, key
    # SR3: stated as invariant, and the note says so
    assert ms["resident_hard_misses"] == 0
    assert "SR3" in ms["note"] and "invariant" in ms["note"]
    # host-only + no injected corpus: coverage denominators honestly null,
    # never a fabricated zero (and never a multi-second pack_groups run)
    assert ms["corpus_total_rules"] is None
    assert ms["non_resident_rules"] is None

    sched = snap["scheduler"]
    for key in ("resident_group", "scores", "port_mix"):
        assert key in sched, key


def test_snapshot_serializes_to_json():
    import json
    doc = json.dumps(T.collect_snapshot(cfg=None))
    assert "pyro-telemetry/1" in doc


def test_snapshot_with_injected_corpus():
    state = T.TelemetryState(corpus=TINY_CORPUS)
    state.set_resident(tiny_group())
    snap = T.collect_snapshot(cfg=None, state=state)
    ms = snap["missed"]
    assert ms["corpus_total_rules"] == 10
    assert ms["lowering_dropped"] == 2
    assert ms["coverage_resident_rules"] == 3      # tiny_group rule_count
    assert ms["non_resident_rules"] == 7 - 3
    assert snap["scheduler"]["resident_group"] == "any/0"


# --------------------------------------------------------------------------
# TelemetryState accumulation
# --------------------------------------------------------------------------
def test_state_switch_accumulation():
    state = T.TelemetryState()
    state.record_switch(12.3, 2564)
    state.record_switch(9.9, 1000)
    snap = T.collect_snapshot(cfg=None, state=state)
    sw = snap["switch"]
    assert sw["count"] == 2
    assert sw["last_ms"] == 9.9
    assert sw["history_ms"] == [12.3, 9.9]
    # wire estimate for the LAST image at 2.3 GiB/s, in ms
    assert sw["wire_ms_est"] == pytest.approx(1000 / (2.3 * 2**30) * 1e3,
                                              rel=1e-3)


def test_state_request_loss_counts_timeouts():
    state = T.TelemetryState()
    state.set_resident(tiny_group())
    state.record_scan(reply([(1, 13)]))
    state.record_scan(None)                        # timeout: sent, unanswered
    state.record_scan(reply([], ovf=False))
    snap = T.collect_snapshot(cfg=None, state=state)
    loss = snap["loss"]
    assert loss["requests_sent"] == 3
    assert loss["replies_received"] == 2
    assert loss["request_loss"] == pytest.approx(1 / 3, rel=1e-4)
    assert snap["matching"]["scans"] == 2          # answered scans only


def test_state_ovf_events_and_truncated_min():
    state = T.TelemetryState()
    state.set_resident(tiny_group())
    state.record_scan(reply([(0, 12)], ovf=True))
    state.record_scan(reply([(0, 12)], ovf=True))
    state.record_scan(reply([(0, 12)]))
    snap = T.collect_snapshot(cfg=None, state=state)
    assert snap["matching"]["ovf_events"] == 2
    # lower bound: each OVF reply truncated at least one entry
    assert snap["matching"]["truncated_min"] == 2
    assert snap["missed"]["ovf_truncation_events"] == 2


def test_state_history_bounded():
    state = T.TelemetryState()
    for i in range(T.TelemetryState.HISTORY_MAX + 50):
        state.record_switch(float(i), 100)
    view = state.view()
    assert len(view["switch_history_ms"]) == T.TelemetryState.HISTORY_MAX
    assert view["switch_count"] == T.TelemetryState.HISTORY_MAX + 50
    assert view["switch_history_ms"][-1] == float(
        T.TelemetryState.HISTORY_MAX + 49)


# --------------------------------------------------------------------------
# sid mapping (the SR3 list fact: one slot nominates EVERY sharing rule)
# --------------------------------------------------------------------------
def test_sid_mapping_nominates_every_rule_on_a_slot():
    state = T.TelemetryState()
    state.set_resident(tiny_group())
    # one hit on the dedup'd slot 0, one on slot 1
    state.record_scan(reply([(0, 12), (1, 13)]))
    snap = T.collect_snapshot(cfg=None, state=state)
    m = snap["matching"]
    # slot 0 carries TWO rules — collapsing to one would be an SR3 defect
    assert m["by_sid"] == {"1:100": 1, "1:101": 1, "1:200": 1}
    assert m["nominations_total"] == 3


def test_sid_mapping_unknown_pid_and_tombstone_ignored():
    state = T.TelemetryState()
    state.set_resident(tiny_group())
    state.record_scan(reply([(2, 4), (99, 5)]))    # tombstone + out-of-range
    snap = T.collect_snapshot(cfg=None, state=state)
    assert snap["matching"]["by_sid"] == {}
    assert snap["matching"]["nominations_total"] == 0


def test_sid_mapping_follows_resident_swap():
    other = RuleGroup("literal", 0, (
        GroupSlot(0, b"xyz", False, (RuleRef(1, 900, None, "pkt_data", 1),)),
    ))
    state = T.TelemetryState()
    state.set_resident(tiny_group())
    state.record_scan(reply([(0, 3)]))
    state.set_resident(other)                      # swap BEFORE new scans
    state.record_scan(reply([(0, 3)]))
    view = state.view()
    assert view["by_sid"] == {"1:100": 1, "1:101": 1, "1:900": 1}
    assert view["resident_group"] == "literal/0"
    assert view["resident_rules"] == 1


def test_daemon_scanresult_duck_type():
    """The daemon's ScanResult (.entries rows (pid, start, end),
    .overflowed) must feed record_scan unchanged."""
    from pyro.snort.daemon import ScanResult
    state = T.TelemetryState()
    state.set_resident(tiny_group())
    state.record_scan(ScanResult(((1, 0, 13),), True))
    view = state.view()
    assert view["by_sid"] == {"1:200": 1}
    assert view["ovf_events"] == 1


# --------------------------------------------------------------------------
# Prometheus exposition
# --------------------------------------------------------------------------
_METRIC_RE = re.compile(
    r'^pyro_[a-z0-9_]+(\{[a-z_]+="[^"]*"(,[a-z_]+="[^"]*")*\})? '
    r'-?\d+(\.\d+)?([eE][-+]?\d+)?$')


def full_snapshot():
    state = T.TelemetryState(corpus=TINY_CORPUS)
    state.set_resident(tiny_group())
    state.record_switch(12.3, 2564)
    state.record_scan(reply([(0, 12), (1, 13)], ovf=True))
    state.record_scan(None)
    return T.collect_snapshot(cfg=None, state=state)


def test_prometheus_every_line_well_formed():
    text = T.prometheus_text(full_snapshot())
    assert text.endswith("\n")
    for line in text.strip().split("\n"):
        assert (line.startswith("# HELP pyro_")
                or line.startswith("# TYPE pyro_")
                or _METRIC_RE.match(line)), line


def test_prometheus_every_metric_has_help_and_type():
    text = T.prometheus_text(full_snapshot())
    helped, typed, sampled = set(), set(), set()
    for line in text.strip().split("\n"):
        if line.startswith("# HELP "):
            helped.add(line.split()[2])
        elif line.startswith("# TYPE "):
            name, mtype = line.split()[2], line.split()[3]
            assert mtype in ("gauge", "counter"), line
            typed.add(name)
        else:
            sampled.add(line.split("{")[0].split(" ")[0])
    assert sampled and sampled == helped == typed


def test_prometheus_owner_named_metrics_present():
    text = T.prometheus_text(full_snapshot())
    for name in ("pyro_switch_last_ms", "pyro_nominations_total",
                 "pyro_missed_nonresident_rules"):
        assert "\n%s " % name in text, name
    # labeled per-sid family, both rules of the dedup'd slot
    assert 'pyro_nominations_by_sid{sid="1:100"} 1' in text
    assert 'pyro_nominations_by_sid{sid="1:101"} 1' in text
    # SR3 invariant is exported and its HELP says invariant, not measurement
    assert "\npyro_missed_resident_hard 0" in text
    help_line = [ln for ln in text.split("\n")
                 if ln.startswith("# HELP pyro_missed_resident_hard")][0]
    assert "SR3" in help_line and "invariant" in help_line


def test_prometheus_nulls_are_omitted_not_zeroed():
    text = T.prometheus_text(T.collect_snapshot(cfg=None))
    # host-only: no device, no netdev, no corpus — absent, never a fake 0
    assert "pyro_netdev_rx_dropped" not in text
    assert "pyro_corpus_total_rules" not in text
    assert "pyro_perf_cycles" not in text
    assert "pyro_request_loss_ratio" not in text
    # but the process-local counters and constants are there
    assert "\npyro_switch_pr_baseline_s 13.6" in text
    assert "\npyro_device_usable 0" in text


def test_prometheus_table_metrics_from_device_section():
    """Prometheus rendering of a device section as a live probe would
    produce it (synthesized here — no hardware in the unit suite)."""
    snap = T.collect_snapshot(cfg=None)
    snap["device"] = {
        "usable": True, "reason": "static_shell_id=0x02020000",
        "iface": "ens2", "static_shell_id": 0x02020000,
        "table": {"active_table_id": 0x1234, "shadow_table_id": 0,
                  "epoch": 3, "status_flags": 0x4, "bytes_received": 2564,
                  "capacity_states": 40960, "error": 0,
                  "active_valid": True, "load_open": False,
                  "commit_err": False},
        "perf": {"cycles": None, "bytes": None, "bytes_per_cycle": None,
                 "throughput_mb_s": None, "note": "overlay child"},
    }
    text = T.prometheus_text(snap)
    assert "\npyro_table_epoch 3" in text
    assert "\npyro_table_active_valid 1" in text
    assert "\npyro_table_capacity_states 40960" in text
    assert "pyro_perf_cycles" not in text        # overlay child: no lie


# --------------------------------------------------------------------------
# timed_load_table (seam-injected transport failure: no recording)
# --------------------------------------------------------------------------
def test_timed_load_table_records_nothing_on_failure():
    """A refused/failed load is not a switch: the old table stays active
    (A5 §5), so the switch series must not record it."""
    import pyro.device as pdev
    state = T.TelemetryState()
    cfg = pdev.DeviceConfig(iface=None, chardev=None)   # R68 fail-closed
    with pytest.raises(pdev.PyroLoadError):
        T.timed_load_table(cfg, b"\x00" * 64, state)
    assert state.view()["switch_count"] == 0


def test_shell_id_parsed_from_probe_reason():
    reason = ("device_usable=true — static_shell_id=0x02020000, "
              "transport: CAP_NET_RAW present")
    assert T._shell_id_from(reason) == 0x02020000
    assert T._shell_id_from("device_usable=false — probe: no reply") is None


# --------------------------------------------------------------------------
# WIRE-MAC scheduler section (refusal-probe read; absent-with-reason)
# --------------------------------------------------------------------------
def _usable_cfg(monkeypatch):
    """A 'usable device' with every read seam-injected: no transport is
    ever opened (the monkeypatched module functions ARE the device)."""
    import pyro.device as pdev
    monkeypatch.setattr(pdev, "probe_device",
                        lambda cfg: (True,
                                     "static_shell_id=0x02020000"))
    monkeypatch.setattr(pdev, "read_table_status",
                        lambda cfg, slot=1: None)

    def perf(cfg, slot=1, with_wire=False):
        wire = pdev.WireCounters(seen=60, scanned=58, drops=2, noms=5)
        return (100, 800, wire) if with_wire else (100, 800)

    monkeypatch.setattr(pdev, "read_perf_counters", perf)
    return pdev.DeviceConfig(iface=None, chardev=None)


def test_wire_sched_host_only_absent_with_reason():
    wm = T.collect_snapshot(cfg=None)["scheduler"]["wire_mac"]
    assert wm["available"] is False
    assert "host-only" in wm["reason"]
    assert "mode" not in wm                # nothing fabricated


def test_wire_sched_absent_when_no_sched_ack(monkeypatch):
    """A classic overlay child drops the 0x11 kind (R78.4): the section
    is absent WITH the reason, and no counter is read past it."""
    import pyro.macwire as mw
    cfg = _usable_cfg(monkeypatch)
    monkeypatch.setattr(mw, "read_sched", lambda cfg, slot=1: None)
    snap = T.collect_snapshot(cfg=cfg, include_corpus=False)
    wm = snap["scheduler"]["wire_mac"]
    assert wm["available"] is False
    assert "SCHED_ACK" in wm["reason"] and "R78.4" in wm["reason"]
    assert "mode" not in wm and "p0_wire" not in wm


def test_wire_sched_section_live(monkeypatch):
    import pyro.macwire as mw
    cfg = _usable_cfg(monkeypatch)
    monkeypatch.setattr(mw, "read_sched",
                        lambda cfg, slot=1: mw.SchedState(mode=2,
                                                          quantum=4))
    monkeypatch.setattr(mw, "read_mac_stats",
                        lambda cfg, slot=1: mw.MacStats(40, 30, 6, 4,
                                                        3, 0))
    snap = T.collect_snapshot(cfg=cfg, include_corpus=False)
    wm = snap["scheduler"]["wire_mac"]
    assert wm["available"] is True
    assert wm["mode"] == 2 and wm["mode_name"] == "round_robin"
    assert wm["quantum"] == 4
    assert wm["p0_wire"] == {"seen": 60, "scanned": 58, "drops": 2,
                             "noms": 5}
    assert wm["p1_mac"] == {"seen": 40, "digested": 30,
                            "skip_nonip": 6, "skip_nokey": 4,
                            "reports_sent": 3, "records_lost": 0}
    der = wm["derived"]
    assert der["wire_total"] == 100
    assert der["p0_share"] == pytest.approx(0.6)
    assert der["p1_share"] == pytest.approx(0.4)
    assert der["zero_slack_residual_p0"] == 0
    assert der["zero_slack_residual_p1"] == 0
    # the SR10 residency scheduler keys are untouched by the new
    # section — different scheduler, namespaced key
    for key in ("resident_group", "scores", "port_mix"):
        assert key in snap["scheduler"]


def test_wire_sched_p0_nulled_when_wire_counters_missing(monkeypatch):
    import pyro.device as pdev
    import pyro.macwire as mw
    cfg = _usable_cfg(monkeypatch)
    monkeypatch.setattr(pdev, "read_perf_counters",
                        lambda cfg, slot=1, with_wire=False:
                        (100, 800, None) if with_wire else (100, 800))
    monkeypatch.setattr(mw, "read_sched",
                        lambda cfg, slot=1: mw.SchedState(0, 1))
    monkeypatch.setattr(mw, "read_mac_stats",
                        lambda cfg, slot=1: mw.MacStats(10, 10, 0, 0,
                                                        1, 0))
    snap = T.collect_snapshot(cfg=cfg, include_corpus=False)
    wm = snap["scheduler"]["wire_mac"]
    assert wm["available"] is True and wm["mode_name"] == "p0_only"
    assert wm["p0_wire"]["seen"] is None
    assert "note" in wm["p0_wire"]         # absent WITH the reason
    der = wm["derived"]
    assert der["wire_total"] is None
    assert der["p0_share"] is None and der["p1_share"] is None
    assert der["zero_slack_residual_p0"] is None
    assert der["zero_slack_residual_p1"] == 0


def test_wire_sched_status0_violation_propagates(monkeypatch):
    """read_sched raising on an APPLIED invalid mode must stay loud:
    a snapshot that swallows it would report the lie's neighborhood
    as healthy."""
    import pyro.macwire as mw
    cfg = _usable_cfg(monkeypatch)

    def boom(cfg, slot=1):
        raise RuntimeError("SCHED_ACK status 0 for the refusal probe")

    monkeypatch.setattr(mw, "read_sched", boom)
    with pytest.raises(RuntimeError):
        T.collect_snapshot(cfg=cfg, include_corpus=False)


# --------------------------------------------------------------------------
# Prometheus: WIRE-MAC scheduler metrics (synthesized section — no HW)
# --------------------------------------------------------------------------
def wire_mac_section():
    return {
        "available": True, "mode": 2, "quantum": 4,
        "mode_name": "round_robin",
        "p0_wire": {"seen": 60, "scanned": 58, "drops": 2, "noms": 5},
        "p1_mac": {"seen": 40, "digested": 30, "skip_nonip": 6,
                   "skip_nokey": 4, "reports_sent": 3,
                   "records_lost": 0},
        "derived": {"wire_total": 100, "p0_share": 0.6,
                    "p1_share": 0.4, "zero_slack_residual_p0": 0,
                    "zero_slack_residual_p1": 0},
    }


def test_prometheus_wire_sched_metrics():
    snap = T.collect_snapshot(cfg=None)
    snap["scheduler"]["wire_mac"] = wire_mac_section()
    text = T.prometheus_text(snap)
    assert "\npyro_sched_mode 2" in text
    assert "\npyro_sched_quantum 4" in text
    assert 'pyro_sched_seen_total{program="p0"} 60' in text
    assert 'pyro_sched_seen_total{program="p1"} 40' in text
    assert 'pyro_sched_share{program="p0"} 0.6' in text
    assert 'pyro_sched_share{program="p1"} 0.4' in text
    assert 'pyro_sched_zero_slack_residual{program="p0"} 0' in text
    assert 'pyro_sched_zero_slack_residual{program="p1"} 0' in text
    assert "\npyro_wire_scanned_total 58" in text
    assert "\npyro_wire_drops_total 2" in text
    assert "\npyro_wire_noms_total 5" in text
    assert "\npyro_mac_digested_total 30" in text
    assert "\npyro_mac_records_lost_total 0" in text
    # types: cumulative device counters are counters; control state
    # and derived ratios are gauges
    assert "# TYPE pyro_sched_seen_total counter" in text
    assert "# TYPE pyro_mac_digested_total counter" in text
    assert "# TYPE pyro_sched_mode gauge" in text
    assert "# TYPE pyro_sched_quantum gauge" in text
    assert "# TYPE pyro_sched_share gauge" in text
    assert "# TYPE pyro_sched_zero_slack_residual gauge" in text
    # every line stays well-formed with the new families present
    for line in text.strip().split("\n"):
        assert (line.startswith("# HELP pyro_")
                or line.startswith("# TYPE pyro_")
                or _METRIC_RE.match(line)), line


def test_prometheus_wire_sched_absent_when_unavailable():
    text = T.prometheus_text(T.collect_snapshot(cfg=None))
    assert "pyro_sched_mode" not in text
    assert "pyro_sched_seen_total" not in text
    assert "pyro_wire_scanned_total" not in text
    assert "pyro_mac_digested_total" not in text


def test_prometheus_wire_sched_partial_p0_omitted():
    """Null p0 numbers are OMITTED (no fake 0), including the labeled
    families: p1 samples stand alone, and a family with no samples
    emits neither HELP nor TYPE."""
    snap = T.collect_snapshot(cfg=None)
    wm = wire_mac_section()
    wm["p0_wire"] = {"seen": None, "scanned": None, "drops": None,
                     "noms": None, "note": "no wire counters"}
    wm["derived"] = {"wire_total": None, "p0_share": None,
                     "p1_share": None, "zero_slack_residual_p0": None,
                     "zero_slack_residual_p1": 0}
    snap["scheduler"]["wire_mac"] = wm
    text = T.prometheus_text(snap)
    assert "pyro_wire_scanned_total" not in text
    assert 'pyro_sched_seen_total{program="p0"}' not in text
    assert 'pyro_sched_seen_total{program="p1"} 40' in text
    assert "pyro_sched_share" not in text
    assert 'pyro_sched_zero_slack_residual{program="p0"}' not in text
    assert 'pyro_sched_zero_slack_residual{program="p1"} 0' in text
    # HELP/TYPE discipline holds: everything sampled is documented,
    # nothing documented lacks a sample
    helped, typed, sampled = set(), set(), set()
    for line in text.strip().split("\n"):
        if line.startswith("# HELP "):
            helped.add(line.split()[2])
        elif line.startswith("# TYPE "):
            typed.add(line.split()[2])
        else:
            sampled.add(line.split("{")[0].split(" ")[0])
    assert sampled == helped == typed
