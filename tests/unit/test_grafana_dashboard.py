"""Grafana dashboard JSON: parses, the Scheduler row is additive, and
every new panel references only metrics prometheus_text() emits.

No hardware anywhere: the WIRE-MAC scheduler section that
prometheus_text() reads is synthesized onto a ``cfg=None`` snapshot,
exactly the way test_prometheus_table_metrics_from_device_section
synthesizes a device section.  The dashboard file is the unit under
test; the emitted-metric set is derived dynamically so a rename on
either side (panel expr or exporter) fails here, not in Grafana.
"""
import json
import os
import re

import pyro.telemetry as T

_HERE = os.path.dirname(os.path.abspath(__file__))
_DASH = os.path.join(_HERE, os.pardir, os.pardir, "grafana",
                     "pyro-overlay-dashboard.json")

# Panels appended for the WIRE-MAC scheduler row: ids 14+ live strictly
# below the pre-existing grid (which ends at y = 22 + h = 8 = 30).
_NEW_ID_MIN = 14
_OLD_MAX_Y = 30

_PROMQL_METRIC_RE = re.compile(r"pyro_[a-z0-9_]+")


def load_dashboard():
    with open(_DASH) as fh:
        return json.load(fh)


def new_panels(dash):
    return [p for p in dash["panels"] if p["id"] >= _NEW_ID_MIN]


def sched_snapshot():
    """Host-only snapshot with the WIRE-MAC scheduler section
    synthesized as a live probe + counter read would produce it."""
    snap = T.collect_snapshot(cfg=None)
    sched = dict(snap["scheduler"])
    sched["wire_mac"] = {
        "available": True,
        "mode": 2,
        "quantum": 4,
        "mode_name": "round_robin",
        "p0_wire": {"seen": 100, "scanned": 90, "drops": 10, "noms": 5},
        "p1_mac": {"seen": 50, "digested": 40, "skip_nonip": 6,
                   "skip_nokey": 4, "reports_sent": 3,
                   "records_lost": 0},
        "derived": {"wire_total": 150,
                    "p0_share": 100 / 150,
                    "p1_share": 50 / 150,
                    "zero_slack_residual_p0": 0,
                    "zero_slack_residual_p1": 0},
    }
    snap["scheduler"] = sched
    return snap


def emitted_types():
    """{metric_name: type} for every metric prometheus_text emits from
    a snapshot whose scheduler section is populated."""
    text = T.prometheus_text(sched_snapshot())
    types = {}
    for line in text.strip().split("\n"):
        if line.startswith("# TYPE "):
            _, _, name, mtype = line.split()
            types[name] = mtype
    return types


# --------------------------------------------------------------------------
# File shape and layout
# --------------------------------------------------------------------------
def test_dashboard_parses_and_keeps_identity():
    dash = load_dashboard()
    assert dash["uid"] == "pyro-overlay"
    assert dash["schemaVersion"] == 39
    ids = [p["id"] for p in dash["panels"]]
    assert len(ids) == len(set(ids)), "duplicate panel ids"


def test_scheduler_row_is_additive():
    """New panels append below every pre-existing panel; nothing in the
    original grid moved to make room."""
    dash = load_dashboard()
    added = new_panels(dash)
    assert added, "Scheduler row panels (ids >= 14) missing"
    for p in dash["panels"]:
        gp = p["gridPos"]
        if p["id"] >= _NEW_ID_MIN:
            assert gp["y"] >= _OLD_MAX_Y, (p["id"], gp)
        else:
            assert gp["y"] + gp["h"] <= _OLD_MAX_Y, (p["id"], gp)


def test_scheduler_panels_use_dashboard_conventions():
    dash = load_dashboard()
    declared = {r["id"] for r in dash["__requires"]
                if r["type"] == "panel"}
    for p in new_panels(dash):
        assert p["type"] in declared, p["type"]
        assert p["datasource"] == {"type": "prometheus",
                                   "uid": "${DS_PROMETHEUS}"}, p["id"]


# --------------------------------------------------------------------------
# Every new panel references only metrics prometheus_text emits
# --------------------------------------------------------------------------
def test_new_panels_reference_only_emitted_metrics():
    types = emitted_types()
    for p in new_panels(load_dashboard()):
        for target in p["targets"]:
            names = set(_PROMQL_METRIC_RE.findall(target["expr"]))
            assert names, target
            missing = names - set(types)
            assert not missing, (p["title"], sorted(missing))


def test_new_panels_rate_only_over_counters():
    """rate() is only meaningful over cumulative counters; a gauge
    inside rate() would render as silent nonsense."""
    types = emitted_types()
    for p in new_panels(load_dashboard()):
        for target in p["targets"]:
            for m in re.findall(r"rate\((pyro_[a-z0-9_]+)",
                                target["expr"]):
                assert types.get(m) == "counter", (target["expr"], m)
