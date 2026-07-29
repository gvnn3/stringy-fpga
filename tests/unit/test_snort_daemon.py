"""AC-S3-1 unit tests: the host filter daemon and the SR10 scheduler.

Everything device-free: transports are model-backed (the group's software
twin), clocks are fake, and the SR14 identity gate is exercised for real —
the model transport computes the same R78.5a value silicon would report.
"""

from typing import List

import pytest

from pyro._circuit_model import GroupCircuitModel
from pyro.snort import daemon as D
from pyro.snort import groups as G
from pyro.snort import scheduler as SCH
from pyro.snort import triage as T
from pyro.snort.rules import parse_rule


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make_group(*rule_texts, class_of=None):
    triaged = [(r, T.triage_rule(r))
               for r in (parse_rule(t, line_no=i + 1)
                         for i, t in enumerate(rule_texts))]
    gs = G.pack_groups(triaged, class_of=class_of) if class_of else \
        G.pack_groups(triaged)
    return gs


def model_pipeline(group, stats=None, out_cap=D.OUT_CAP):
    circuit = G.group_circuit(group)
    model = GroupCircuitModel(circuit)
    model.resident = True
    return D.NominationPipeline(D.ModelTransport(group, model),
                                stats or D.Stats(), out_cap)


FLOW80 = D.FlowKey("tcp", "10.0.0.2", 40001, "10.0.0.9", 80)
FLOW25 = D.FlowKey("tcp", "10.0.0.2", 40002, "10.0.0.9", 25)


def smtp_rule(sid, content='content:"EVILCMD";'):
    return ('alert tcp $EXTERNAL_NET any -> $HOME_NET 25 '
            '(msg:"t%d"; %s sid:%d;)' % (sid, content, sid))


# --------------------------------------------------------------------------
# SR13: the variable table
# --------------------------------------------------------------------------
def test_var_table_port_predicates():
    vt = D.VarTable()
    assert vt.port_holds("any", 80)
    assert vt.port_holds("$HTTP_PORTS", 8080)
    assert not vt.port_holds("$HTTP_PORTS", 25)
    assert vt.port_holds("!$HTTP_PORTS", 25)
    assert vt.port_holds("25", 25)
    assert vt.port_holds("[25,443]", 443)
    assert vt.port_holds("[1000:2000]", 1500)
    assert not vt.port_holds("[1000:2000]", 2500)
    assert vt.port_holds("!80", 81)


def test_var_table_is_data_not_identity():
    """SR13: two different variable tables must not change any group
    identity — the table never enters canonical bytes."""
    gs = make_group(smtp_rule(1))
    a = gs[0].canonical_bytes()
    # (nothing about VarTable is consulted during packing/hashing)
    vt = D.VarTable(port_vars={"$HTTP_PORTS": {9}})
    assert gs[0].canonical_bytes() == a
    assert vt.port_holds("$HTTP_PORTS", 9)


def test_relevant_versus_most_specific_class():
    vt = D.VarTable()
    rel = vt.relevant_port_classes(80)
    assert {"any", "literal", "$HTTP_PORTS"} <= rel
    # smallest matching set wins: $HTTP_PORTS beats its $FILE_DATA_PORTS
    # superset on 80; 110 is FILE_DATA-only; 999 matches no variable.
    assert vt.most_specific_class(80) == "$HTTP_PORTS"
    assert vt.most_specific_class(110) == "$FILE_DATA_PORTS"
    assert vt.most_specific_class(999) == "literal"


# --------------------------------------------------------------------------
# The histogram
# --------------------------------------------------------------------------
def test_histogram_decays_with_half_life():
    """Per-PORT counts (not per-class): the scheduler scores each rule's own
    port predicate, and aggregating to a class first destroys exactly the
    information that decision needs."""
    clk = FakeClock()
    vt = D.VarTable(port_vars={"$X": {1234}})
    h = D.PortMixHistogram(vt, half_life_s=10.0, clock=clk)
    h.observe(1234, 1000)
    assert h.snapshot()[1234] == pytest.approx(1000)
    clk.advance(10.0)
    assert h.snapshot()[1234] == pytest.approx(500, rel=1e-6)
    clk.advance(20.0)
    assert h.snapshot()[1234] == pytest.approx(125, rel=1e-6)


def test_histogram_class_view_aggregates_ports():
    clk = FakeClock()
    vt = D.VarTable()
    h = D.PortMixHistogram(vt, half_life_s=1e9, clock=clk)
    h.observe(80, 100)
    h.observe(8080, 50)
    h.observe(22, 30)
    by_cls = h.snapshot_by_class()
    assert by_cls["$HTTP_PORTS"] == pytest.approx(150)
    assert by_cls["$SSH_PORTS"] == pytest.approx(30)
    assert h.snapshot()[80] == pytest.approx(100)


def test_histogram_bounds_tracked_ports():
    clk = FakeClock()
    h = D.PortMixHistogram(D.VarTable(), half_life_s=1e9, clock=clk,
                           max_ports=4)
    for p in range(1000, 1010):
        h.observe(p, 10 * (p - 999))        # later ports are hotter
    snap = h.snapshot()
    assert len(snap) <= 4
    assert 1009 in snap                     # the hottest survives eviction


# --------------------------------------------------------------------------
# SR15 tripwires
# --------------------------------------------------------------------------
def test_tripwires_classify():
    assert D.tripwire_class(
        b"POST /x HTTP/1.1\r\nContent-Encoding: gzip\r\n\r\n") == \
        "content_encoding"
    assert D.tripwire_class(
        b"POST /x HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n") == \
        "chunked_te"
    dense = b"GET /" + b"%41" * 30 + b" HTTP/1.1\r\n\r\n"
    assert D.tripwire_class(dense) == "percent_density"
    assert D.tripwire_class(b"GET /plain HTTP/1.1\r\n\r\n") is None
    assert D.tripwire_class(b"EHLO mail\r\n") is None


# --------------------------------------------------------------------------
# The nomination pipeline
# --------------------------------------------------------------------------
def test_feed_nominates_and_attributes_by_sidecar():
    g = make_group(smtp_rule(101))[0]
    dm = D.FilterDaemon()
    dm.set_pipeline(model_pipeline(g, dm.stats))
    noms = dm.feed(FLOW25, b"HELO x\r\nEVILCMD now\r\n")
    assert [(n.gid, n.sid, n.kind) for n in noms] == [(1, 101, "match")]
    assert dm.feed(FLOW25, b"nothing to see") == []
    snap = dm.stats.snapshot()
    assert snap["nominations_by_tier"] == {"raw-anchor": 1}
    assert snap["resident"]["group"] == g.name
    assert snap["resident"]["rules"] == 1


def test_sr12_anchor_split_across_segments_is_still_nominated():
    g = make_group(smtp_rule(102))[0]
    dm = D.FilterDaemon()
    dm.set_pipeline(model_pipeline(g, dm.stats))
    assert dm.feed(FLOW25, b"xxxxEVIL") == []       # first half only
    noms = dm.feed(FLOW25, b"CMDyyyy")              # completes across seam
    assert {(n.gid, n.sid) for n in noms} == {(1, 102)}


def test_sr12_anchor_split_across_chunk_boundary_is_still_nominated():
    g = make_group(smtp_rule(103))[0]
    dm = D.FilterDaemon()
    dm.set_pipeline(model_pipeline(g, dm.stats))
    pre = b"\x00" * (D.CHUNK_BYTES - 4)             # anchor straddles chunk 0/1
    noms = dm.feed(FLOW25, pre + b"EVILCMD" + b"\x00" * 8)
    assert {(n.gid, n.sid) for n in noms} == {(1, 103)}
    # exactly one nomination end offset, absolute in the flow
    assert noms[0].end_off == len(pre) + len(b"EVILCMD")


def test_sr13_group_is_only_offered_relevant_flows():
    g = make_group('alert tcp any any -> any $HTTP_PORTS '
                   '(msg:"h"; content:"EVILCMD"; sid:7;)')[0]
    assert g.port_class == "$HTTP_PORTS"
    dm = D.FilterDaemon()
    dm.set_pipeline(model_pipeline(g, dm.stats))
    assert dm.feed(FLOW25, b"EVILCMD") == []        # port 25 ∉ $HTTP_PORTS
    assert {(n.gid, n.sid) for n in dm.feed(FLOW80, b"EVILCMD")} == {(1, 7)}


def test_sr14_identity_mismatch_routes_to_unfiltered():
    g = make_group(smtp_rule(104))[0]
    dm = D.FilterDaemon()
    pipe = model_pipeline(g, dm.stats)
    pipe._expected_child = 0xDEAD_BEEF              # sabotage the expectation
    dm.set_pipeline(pipe)
    assert dm.feed(FLOW25, b"EVILCMD") == []        # never misattributed
    snap = dm.stats.snapshot()
    assert snap["identity_mismatches"] == 1
    assert snap["resident"]["group"] is None        # dropped to unfiltered


def test_unfiltered_window_seconds_are_visible():
    clk = FakeClock()
    g = make_group(smtp_rule(105))[0]
    dm = D.FilterDaemon(clock=clk)
    clk.advance(5)                                  # 5 s unfiltered at start
    dm.set_pipeline(model_pipeline(g, dm.stats))
    clk.advance(100)                                # resident: not counted
    dm.set_pipeline(None)
    clk.advance(7)                                  # 7 more unfiltered
    snap = dm.stats.snapshot()
    assert snap["unfiltered_seconds"] == pytest.approx(12.0)


def test_tripwired_flow_is_marked_and_counted():
    g = make_group('alert tcp any any -> any $HTTP_PORTS '
                   '(msg:"h"; content:"EVILCMD"; sid:8;)')[0]
    dm = D.FilterDaemon()
    dm.set_pipeline(model_pipeline(g, dm.stats))
    payload = b"POST /u HTTP/1.1\r\nContent-Encoding: gzip\r\n\r\nEVILCMD"
    noms = dm.feed(FLOW80, payload)
    kinds = {n.kind for n in noms}
    assert "tripwire" in kinds                      # forced forward
    assert "match" in kinds                         # scanning continues
    assert dm.stats.snapshot()["tripwire_hits"]["content_encoding"] == 1


def test_ovf_resume_recovers_every_floating_window():
    rules = [smtp_rule(200 + i, 'content:"EVIL%02d";' % i) for i in range(6)]
    g = make_group(*rules)[0]
    dm = D.FilterDaemon()
    dm.set_pipeline(model_pipeline(g, dm.stats, out_cap=1))
    payload = b"..".join(b"EVIL%02d" % i for i in range(6))
    noms = dm.feed(FLOW25, payload)
    assert {n.sid for n in noms} == {200 + i for i in range(6)}
    assert dm.stats.snapshot()["ovf"]["resumes"] >= 5


def test_ovf_floods_anchored_slots_on_buffer_aligned_overflow():
    # One \A slot (udp depth rule) + enough floating matches to overflow.
    rules = ['alert udp any any -> any 53 (msg:"a"; '
             'content:"DNSX", depth 8; sid:300;)'] + \
            ['alert udp any any -> any 53 (msg:"f%d"; '
             'content:"FLOOD%d"; sid:%d;)' % (i, i, 301 + i)
             for i in range(3)]
    g = make_group(*rules)[0]              # dst 53 → the 'literal' class
    anchored = [s for s in g.slots
                if s.pattern_eff.startswith(b"(?s:\\A")]
    assert len(anchored) == 1
    dm = D.FilterDaemon()
    dm.set_pipeline(model_pipeline(g, dm.stats, out_cap=1))
    flow = D.FlowKey("udp", "10.0.0.2", 40003, "10.0.0.9", 53)
    payload = b"FLOOD0.FLOOD1.FLOOD2"               # overflows at out_cap=1
    noms = dm.feed(flow, payload)
    assert (1, 300) in {(n.gid, n.sid) for n in noms}   # flooded \A rule
    assert any(n.kind == "ovf_flood" for n in noms)
    assert dm.stats.snapshot()["ovf"]["anchored_floods"] >= 1


# --------------------------------------------------------------------------
# The SR10 scheduler
# --------------------------------------------------------------------------
def _two_class_world():
    http = make_group('alert tcp any any -> any $HTTP_PORTS '
                      '(msg:"h"; content:"HTTPEVIL"; sid:1;)')[0]
    smtp = make_group(smtp_rule(2, 'content:"SMTPEVIL";'))[0]
    assert http.port_class == "$HTTP_PORTS" and smtp.port_class == "literal"
    return http, smtp


def _scheduler(dm, groups, clk, **kw):
    made: List[str] = []

    def load(group):
        made.append(group.name)
        return model_pipeline(group, dm.stats)

    kw.setdefault("hysteresis", 2.0)
    kw.setdefault("min_dwell_s", 300.0)
    kw.setdefault("challenge_s", 60.0)
    sch = SCH.ResidencyScheduler(dm, groups, load, clock=clk, **kw)
    return sch, made


def test_scheduler_loads_immediately_when_unfiltered():
    clk = FakeClock()
    dm = D.FilterDaemon(clock=clk)
    http, smtp = _two_class_world()
    sch, made = _scheduler(dm, [http, smtp], clk)
    dm.feed(FLOW80, b"x" * 500)
    assert sch.tick() == http.name
    assert dm.stats.snapshot()["resident"]["group"] == http.name
    assert dm.stats.snapshot()["swaps"] == 1


def test_scheduler_hysteresis_demands_sustained_decisive_lead():
    clk = FakeClock()
    dm = D.FilterDaemon(clock=clk)
    http, smtp = _two_class_world()
    sch, made = _scheduler(dm, [http, smtp], clk)
    dm.feed(FLOW80, b"x" * 1000)
    assert sch.tick() == http.name

    # An SMTP lead below 2x: never a swap.
    dm.feed(FLOW25, b"y" * 1500)
    clk.advance(500)
    dm.feed(FLOW25, b"y" * 100)   # keep some signal alive
    assert sch.tick() is None

    # A decisive lead must persist for challenge_s before the swap.
    dm.feed(FLOW25, b"y" * 100000)
    assert sch.tick() is None                       # challenge starts
    clk.advance(30)
    assert sch.tick() is None                       # not yet
    clk.advance(31)
    assert sch.tick() == smtp.name                  # sustained: swap
    assert dm.stats.snapshot()["swaps"] == 2


def test_scheduler_does_not_rotate_pointlessly_within_a_class():
    """Rotation was removed deliberately (2026-07-29 working-set study).

    At single-tenant residency, swapping 256 resident rules for a
    *different* 256 leaves instantaneous coverage unchanged while paying a
    ~16 s blind window — pure loss.  A swap must now be justified by the
    value function or it does not happen.
    """
    clk = FakeClock()
    dm = D.FilterDaemon(clock=clk)
    a = make_group(*[smtp_rule(400 + i, 'content:"AA%03d";' % i)
                     for i in range(300)])          # 2 literal groups
    assert len(a) == 2 and a[0].port_class == "literal"
    sch, made = _scheduler(dm, a, clk)
    dm.feed(FLOW25, b"x" * 1000)
    first = sch.tick()
    assert first in (a[0].name, a[1].name)
    for _ in range(3):
        clk.advance(301)
        dm.feed(FLOW25, b"x" * 1000)
        assert sch.tick() is None                   # no churn
    assert dm.stats.snapshot()["swaps"] == 1


def _universal_and_specific():
    """A universal ('any' dst_port) group and a port-25-only group, where
    the universal one holds MORE rules — the exact shape the old
    class-scored scheduler got wrong."""
    universal = ['alert tcp any any -> any any (msg:"u%d"; '
                 'content:"UNIV%03d"; sid:%d;)' % (i, i, 700 + i)
                 for i in range(40)]
    specific = ['alert tcp any any -> any 25 (msg:"s%d"; '
                'content:"SPEC%03d"; sid:%d;)' % (i, i, 800 + i)
                for i in range(5)]
    gs = make_group(*(universal + specific))
    uni = next(g for g in gs if g.port_class == "any")
    spec = next(g for g in gs if g.port_class == "literal")
    assert uni.rule_count == 40 and spec.rule_count == 5
    return uni, spec


def test_scheduler_prefers_the_higher_value_group_over_the_port_match():
    """THE FIX. Traffic is entirely port 25, so the old scheduler elected
    the port-25 class and loaded a 5-rule group.  Value-aware scoring sees
    that the 'any'-token rules also fire on port 25 — 40 of them — and
    picks the universal group instead."""
    clk = FakeClock()
    dm = D.FilterDaemon(clock=clk)
    uni, spec = _universal_and_specific()
    sch, made = _scheduler(dm, [uni, spec], clk)
    dm.feed(FLOW25, b"x" * 5000)                    # 100% port-25 traffic
    assert sch.tick() == uni.name
    s = sch.scores()
    assert s[uni.name] > s[spec.name]               # 40 rules vs 5
    # and it stays put: no later tick finds a better candidate
    for _ in range(3):
        clk.advance(301)
        dm.feed(FLOW25, b"x" * 5000)
        assert sch.tick() is None


def test_scheduler_swaps_when_the_resident_stops_being_relevant():
    """Value-aware does not mean inert: a resident whose rules cannot fire
    on the observed traffic loses to one whose rules can."""
    clk = FakeClock()
    dm = D.FilterDaemon(clock=clk)
    uni, spec = _universal_and_specific()
    sch, made = _scheduler(dm, [uni, spec], clk, min_dwell_s=10.0,
                           challenge_s=5.0)
    # Force the low-value group resident, then show HTTP traffic.
    dm.set_pipeline(model_pipeline(spec, dm.stats))
    for _ in range(4):
        dm.feed(FLOW80, b"y" * 5000)                # port 80: spec fires on 0
        clk.advance(6)
        r = sch.tick()
        if r:
            break
    assert dm.stats.snapshot()["resident"]["group"] == uni.name


def test_scores_count_each_rules_own_port_predicate():
    dm = D.FilterDaemon()
    uni, spec = _universal_and_specific()
    sch, _ = _scheduler(dm, [uni, spec], FakeClock())
    assert sch.rules_that_could_fire(uni, 25) == 40   # 'any' fires anywhere
    assert sch.rules_that_could_fire(uni, 80) == 40
    assert sch.rules_that_could_fire(spec, 25) == 5   # port-25 rules
    assert sch.rules_that_could_fire(spec, 80) == 0   # ...and nowhere else


def test_scheduler_failed_load_stays_unfiltered_and_retries():
    clk = FakeClock()
    dm = D.FilterDaemon(clock=clk)
    http, smtp = _two_class_world()
    calls = []

    def flaky(group):
        calls.append(group.name)
        if len(calls) == 1:
            raise RuntimeError("vivado ate the bitstream")
        return model_pipeline(group, dm.stats)

    sch = SCH.ResidencyScheduler(dm, [http, smtp], flaky, clock=clk,
                                 min_dwell_s=10.0)
    dm.feed(FLOW80, b"x" * 500)
    assert sch.tick() is None                       # failed: unfiltered
    assert dm.stats.snapshot()["resident"]["group"] is None
    clk.advance(11)
    dm.feed(FLOW80, b"x" * 500)
    assert sch.tick() == http.name                  # retried and recovered
    assert calls == [http.name, http.name]


def test_scheduler_rejects_identity_mismatched_load():
    clk = FakeClock()
    dm = D.FilterDaemon(clock=clk)
    http, smtp = _two_class_world()

    def wrong(group):
        pipe = model_pipeline(group, dm.stats)
        pipe._expected_child = 0x1BADCAFE           # will not match readback
        return pipe

    sch = SCH.ResidencyScheduler(dm, [http, smtp], wrong, clock=clk)
    dm.feed(FLOW80, b"x" * 500)
    assert sch.tick() is None
    assert dm.stats.snapshot()["resident"]["group"] is None
    assert dm.stats.identity_mismatches >= 1
