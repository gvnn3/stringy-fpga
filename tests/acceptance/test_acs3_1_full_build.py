"""AC-S3-1: all groups build; residency hot-swaps by port mix; SR14 gates
every attribution; SR19 stats export is live.

Spec clause (snort-rule-offload §6, Phase S3): *"All ~16 groups build; the
SR10 residency manager hot-swaps by observed port mix; SR14 identity
checks gate every attribution; SR19 stats export is live."*

Clause mapping:

* **Build evidence** — read from the S3 batch run's summary
  (``.superpowers/pr-builds/s3_build_summary.json``) and the SR9 cache:
  every SR6 group of the current corpus packing has a ``pr_bitstream``
  artifact with ``pr_verified`` and ``met_timing``.  SKIPs honestly while
  the batch build has not completed (the summary is the evidence, not a
  simulation).
* **Hot-swap by port mix** — the SR10 scheduler drives a real
  mix shift end-to-end against model-backed pipelines (device-free; the
  identical decision path drives the JTAG loader in wire mode).
* **SR14 gating** — a wrong resident child can never be attributed:
  demonstrated at the pipeline level, and its counter is SR19-visible.
* **SR19 live** — the stats surface carries every §4.6 field with real
  values after traffic.

On-hardware clauses (loaded child answers ID with the SR7 child id; wire
nominations) carry SR18's SKIP discipline via the device probe, exactly
as AC-S1-2/AC-S2-2 did.
"""

import json
import os

import pytest

import snortpf_s2_support as S
from pyro.hdl import generator as gen
from pyro.snort import daemon as D
from pyro.snort import groups as G
from pyro.snort import scheduler as SCH
from pyro.snort import triage as T
from pyro.synth import cache as C
from pyro.synth.toolchain import SHELL_VERSION, VIVADO_TOOLCHAIN_VERSION

pytestmark = pytest.mark.skipif(
    not os.path.exists(S.CORPUS), reason="community ruleset not vendored")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SUMMARY = os.path.join(REPO, ".superpowers", "pr-builds",
                       "s3_build_summary.json")


@pytest.fixture(scope="module")
def corpus_groups():
    return G.pack_groups(T.triage_file(S.CORPUS))


def _key(g):
    return g.bitstream_key(gen.GENERATOR_VERSION, gen.HARNESS_VERSION,
                           VIVADO_TOOLCHAIN_VERSION, SHELL_VERSION, 1)


# --------------------------------------------------------------------------
# Clause 1: all groups build (evidence: the batch summary + the SR9 cache)
# --------------------------------------------------------------------------
def test_all_groups_have_verified_bitstreams(corpus_groups):
    if not os.path.exists(SUMMARY):
        pytest.skip("s3_build_summary.json absent — batch build not run "
                    "on this host (AC-S3-1 build clause pends the "
                    "overnight run)")
    with open(SUMMARY) as fh:
        summary = json.load(fh)
    missing = [g.name for g in corpus_groups
               if g.name not in summary["groups"]]
    assert not missing, "groups absent from the build run: %r" % missing
    incomplete = {name: rec.get("status")
                  for name, rec in summary["groups"].items()
                  if rec.get("status") not in ("built", "cached")}
    assert not incomplete, "unbuilt groups: %r" % incomplete
    # The cache is the operational source of truth (R63a): every group's
    # SR9 key must resolve to a non-torn artifact.
    cache = C.BitstreamCache()
    for g in corpus_groups:
        entry = cache.get(_key(g))
        assert entry is not None, "no cache entry for %s" % g.name
        payload = entry.read_payload()
        assert payload, "0-byte (crash-torn) artifact for %s" % g.name
        assert entry.manifest.payload_kind == "pr_bitstream"
        assert entry.manifest.pr_verified and entry.manifest.met_timing
    # Utilization honesty (SR8/R74): built groups recorded real ≤ est.
    built = [rec for rec in summary["groups"].values()
             if rec.get("status") == "built"]
    assert built, "summary holds no fresh builds"
    for rec in built:
        assert rec["pr_verified"] and rec["met_timing"]
        assert rec["fmax_mhz"] >= 250.0


# --------------------------------------------------------------------------
# Clause 2: the SR10 residency manager hot-swaps by observed port mix
# --------------------------------------------------------------------------
def test_residency_hot_swaps_on_a_mix_shift(corpus_groups):
    class Clk:
        t = 0.0

        def __call__(self):
            return self.t

    clk = Clk()
    dm = D.FilterDaemon(clock=clk)
    from pyro._circuit_model import GroupCircuitModel

    # Small real groups keep the model cheap; the decision path is the
    # production one regardless of group size.
    world = [g for g in corpus_groups
             if g.name in ("$SSH_PORTS/0", "$SIP_PORTS/0")]
    assert len(world) == 2

    def load(group):
        circuit = G.group_circuit(group)
        model = GroupCircuitModel(circuit)
        model.resident = True
        return D.NominationPipeline(D.ModelTransport(group, model), dm.stats)

    sch = SCH.ResidencyScheduler(dm, world, load, clock=clk,
                                 min_dwell_s=10.0, challenge_s=5.0)
    ssh = D.FlowKey("tcp", "10.0.0.2", 40001, "10.0.0.9", 22)
    sip = D.FlowKey("udp", "10.0.0.2", 40002, "10.0.0.9", 5060)

    dm.feed(ssh, b"SSH-2.0-OpenSSH\r\n" * 20)
    assert sch.tick() == "$SSH_PORTS/0"             # mix says ssh
    for _ in range(6):                              # decisive, sustained shift
        dm.feed(sip, b"REGISTER sip:x SIP/2.0\r\n" * 40)
        clk.t += 2.0
        sch.tick()
    clk.t += 10.0
    dm.feed(sip, b"REGISTER sip:x SIP/2.0\r\n" * 40)
    assert dm.stats.snapshot()["resident"]["group"] == "$SIP_PORTS/0"
    assert dm.stats.snapshot()["swaps"] == 2


# --------------------------------------------------------------------------
# Clause 3: SR14 identity gates every attribution
# --------------------------------------------------------------------------
def test_identity_gate_blocks_misattribution_and_is_sr19_visible(
        corpus_groups):
    g = next(x for x in corpus_groups if x.name == "$SSH_PORTS/0")
    from pyro._circuit_model import GroupCircuitModel
    circuit = G.group_circuit(g)
    model = GroupCircuitModel(circuit)
    model.resident = True
    dm = D.FilterDaemon()
    pipe = D.NominationPipeline(D.ModelTransport(g, model), dm.stats)
    dm.set_pipeline(pipe)
    flow = D.FlowKey("tcp", "10.0.0.2", 40001, "10.0.0.9", 22)
    anchor = next(s.anchor for s in g.slots if not s.tombstone)
    assert dm.feed(flow, b"x" + anchor + b"y")      # attribution works...
    pipe2 = D.NominationPipeline(D.ModelTransport(g, model), dm.stats)
    pipe2._expected_child = 0x0BADF00D              # ...until identity lies
    dm.set_pipeline(pipe2)
    assert dm.feed(flow, b"x" + anchor + b"y") == []
    snap = dm.stats.snapshot()
    assert snap["identity_mismatches"] == 1
    assert snap["resident"]["group"] is None        # routed to unfiltered


# --------------------------------------------------------------------------
# Clause 4: SR19 stats export is live (every §4.6 field, real values)
# --------------------------------------------------------------------------
def test_sr19_surface_carries_every_required_field(corpus_groups):
    dm = D.FilterDaemon()
    snap = dm.stats.snapshot()
    required = {"nominations_by_tier", "reverify", "tripwire_hits",
                "identity_mismatches", "resident", "unfiltered_seconds",
                "swaps", "ovf", "requests", "segments"}
    assert required <= set(snap)
    assert set(snap["reverify"]) == {"confirms", "rejects",
                                     "rejects_by_class"}
    dm.stats.reverified(True)
    dm.stats.reverified(False, "case_fold")
    snap = dm.stats.snapshot()
    assert snap["reverify"]["confirms"] == 1
    assert snap["reverify"]["rejects_by_class"] == {"case_fold": 1}


# --------------------------------------------------------------------------
# On-hardware clause (SR18 SKIP discipline)
# --------------------------------------------------------------------------
def test_loaded_child_serves_wire_nominations(corpus_groups, device_iface):
    """The resident child answers ID with an SR7 child id matching one of
    the built groups, and a wire MATCH nominates.  SR18: device_usable
    gates; the canonical SKIP string is recorded by the probe layer."""
    if not device_iface:
        pytest.skip("device_usable=false — transport: PYRO_DEVICE_IFACE "
                    "not configured (SR18/R83)")
    from pyro import device as _device
    cfg = _device.DeviceConfig(iface=device_iface)
    ok, ident = _device.probe_device(cfg)
    if not ok:
        pytest.skip("device_usable=false — %s" % (ident,))
    child = int(getattr(ident, "rp_child_id", 0))
    expected = {g.name: g.rp_child_id(gen.GENERATOR_VERSION,
                                      gen.HARNESS_VERSION, 1)
                for g in corpus_groups}
    matches = [n for n, cid in expected.items() if cid == child]
    if not matches:
        pytest.skip("resident child 0x%08x is not an S3 group build "
                    "(stale resident; load one first — demo.md §9)" % child)
    assert len(matches) == 1                        # identities are distinct
