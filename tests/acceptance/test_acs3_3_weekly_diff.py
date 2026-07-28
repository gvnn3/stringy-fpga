"""AC-S3-3: the simulated weekly ruleset diff.

Spec clause (snort-rule-offload §6, Phase S3): *"A simulated weekly
ruleset diff (tens of rules) dirties ≤ 3 groups (SR6 tombstone stability,
SR9 cache keys); untouched groups hit the cache (PYRO R63a dedup); the
unfiltered window during rebuild is bounded and visible in SR19 stats."*

The diff simulated here is SF15-shaped (release diffs touch tens of
rules; the file is replaced wholesale):

* **8 deletions** from one group ($ORACLE_PORTS/1) → tombstones, indices
  retained, no renumbering anywhere;
* **5 modifications** (anchor content changed) in the same group →
  delete + insert within the group;
* **20 additions** (fresh sids, $HTTP_PORTS) → they join the first
  $HTTP_PORTS group with spare RULE capacity (/7; /0../6 hold 256 each).

Dirtiness is measured the only way that matters operationally: the SR9
**bitstream cache key**.  A group whose key is unchanged is served by the
R63a cache with zero tool time; the drill proves that with a real
:class:`~pyro.synth.cache.BitstreamCache` in a tmpdir, not by key
comparison alone.
"""

import os

import pytest

import snortpf_s2_support as S
from pyro.hdl import generator as gen
from pyro.snort import daemon as D
from pyro.snort import groups as G
from pyro.snort import triage as T
from pyro.synth import cache as C
from pyro.synth.manifest import Manifest
from pyro.synth.toolchain import SHELL_VERSION, VIVADO_TOOLCHAIN_VERSION

pytestmark = pytest.mark.skipif(
    not os.path.exists(S.CORPUS), reason="community ruleset not vendored")


def _key(group):
    return group.bitstream_key(gen.GENERATOR_VERSION, gen.HARNESS_VERSION,
                               VIVADO_TOOLCHAIN_VERSION, SHELL_VERSION, 1)


def _sid(rule) -> int:
    for o in rule.options:
        if o.key == "sid":
            return int(o.value)
    return -1


@pytest.fixture(scope="module")
def baseline():
    triaged = T.triage_file(S.CORPUS)
    return triaged, G.pack_groups(triaged)


@pytest.fixture(scope="module")
def weekly(baseline, tmp_path_factory):
    """The edited rules file + its repack against the baseline layout."""
    triaged, base_groups = baseline

    def group_sids(name):
        g = next(x for x in base_groups if x.name == name)
        return [r.sid for s in g.slots for r in s.rules]

    oracle1 = group_sids("$ORACLE_PORTS/1")
    delete = set(oracle1[:8])
    modify = set(oracle1[8:13])

    out_lines = []
    with open(S.CORPUS, encoding="utf-8", errors="surrogateescape") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                out_lines.append(line.rstrip("\n"))
                continue
            m = None
            import re as _re
            m = _re.search(r"\bsid:(\d+)", stripped)
            sid = int(m.group(1)) if m else -1
            if sid in delete:
                continue
            if sid in modify:
                # Change the rule's content payload: anchor changes, so the
                # rule is a delete+insert within its group (SR6).
                line = line.replace('content:"', 'content:"XZ', 1)
            out_lines.append(line.rstrip("\n"))
    for i in range(20):
        out_lines.append(
            'alert tcp $EXTERNAL_NET any -> $HOME_NET $HTTP_PORTS '
            '(msg:"weekly add %d"; flow:to_server,established; '
            'content:"/weekly-drill-%04d.cgi"; sid:%d; rev:1;)'
            % (i, i, 3900000 + i))

    path = tmp_path_factory.mktemp("weekly") / "weekly.rules"
    path.write_text("\n".join(out_lines) + "\n", encoding="utf-8",
                    errors="surrogateescape")
    new_triaged = T.triage_file(str(path))
    new_groups = G.repack_with_tombstones(base_groups, new_triaged)
    return {"delete": delete, "modify": modify,
            "new_triaged": new_triaged, "new_groups": new_groups,
            "n_edits": len(delete) + len(modify) + 20}


def test_diff_is_tens_of_rules(weekly):
    assert 30 <= weekly["n_edits"] <= 60          # SF15 'tens of rules'


def test_diff_dirties_at_most_three_groups(baseline, weekly):
    _, base_groups = baseline
    base_keys = {g.name: _key(g) for g in base_groups}
    new_keys = {g.name: _key(g) for g in weekly["new_groups"]}
    dirty = sorted(n for n in new_keys
                   if base_keys.get(n) != new_keys[n])
    assert len(dirty) <= 3, "dirty groups: %r" % dirty
    # and the dirty set is exactly where the edits went: deletions +
    # modifications in $ORACLE_PORTS/1, additions in the first
    # $HTTP_PORTS group with spare RULE capacity (/0../6 are full at 256)
    assert set(dirty) == {"$ORACLE_PORTS/1", "$HTTP_PORTS/7"}


def test_deletions_leave_tombstones_and_never_renumber(baseline, weekly):
    _, base_groups = baseline
    base = next(g for g in base_groups if g.name == "$ORACLE_PORTS/1")
    new = next(g for g in weekly["new_groups"]
               if g.name == "$ORACLE_PORTS/1")
    deleted = weekly["delete"]
    modified = weekly["modify"]
    # every UNTOUCHED surviving rule keeps its exact (slot, pattern)
    # placement; a modified rule is a delete+insert and moves to a fresh
    # slot index (never a reused one)
    base_place = {r.sid: (s.index, s.pattern_eff)
                  for s in base.slots for r in s.rules}
    new_place = {r.sid: (s.index, s.pattern_eff)
                 for s in new.slots for r in s.rules}
    for sid, place in new_place.items():
        if sid in modified:
            assert place[0] >= base.n_slots       # fresh index appended
            assert place != base_place[sid]
        else:
            assert base_place[sid] == place
    # deleted sids are gone; their slots survive as tombstones
    assert not deleted & set(new_place)
    tombs = {s.index for s in new.slots if s.tombstone}
    only_deleted = {base_place[sid][0] for sid in deleted
                    if sid in base_place}
    assert tombs >= only_deleted
    # old indices never renumber; modified rules only APPEND slots
    assert new.n_slots == base.n_slots + len(modified)


def test_untouched_groups_hit_the_real_cache(baseline, weekly, tmp_path):
    _, base_groups = baseline
    cache = C.BitstreamCache(root=tmp_path)
    for g in base_groups:                          # "last week's" build
        key = _key(g)
        man = Manifest(
            pattern_hash=g.group_hash(gen.GENERATOR_VERSION,
                                      gen.HARNESS_VERSION).hex(),
            encoding=0, effective_flags=0, circ_flags=0,
            generator_version=gen.GENERATOR_VERSION,
            harness_version=gen.HARNESS_VERSION,
            toolchain_version=VIVADO_TOOLCHAIN_VERSION,
            shell_version=SHELL_VERSION,
            luts=1, ffs=1, bram_kb=0, dsps=0, fmax_mhz=250.0,
            met_timing=True)
        cache.put(key, b"\x5a" * 64, man)
    hits = sum(1 for g in weekly["new_groups"] if cache.has(_key(g)))
    misses = [g.name for g in weekly["new_groups"]
              if not cache.has(_key(g))]
    assert hits == len(weekly["new_groups"]) - len(misses)
    assert len(misses) <= 3                       # only the dirty rebuild
    assert hits >= 18                             # R63a: zero tool time


def test_unfiltered_window_is_bounded_and_sr19_visible(baseline, weekly):
    """During a dirty group's rebuild its rules are simply unfiltered
    (SR10); the window is bounded by the rebuild and visible in SR19."""
    _, base_groups = baseline

    class Clk:
        t = 0.0

        def __call__(self):
            return self.t

    clk = Clk()
    dm = D.FilterDaemon(clock=clk)
    from pyro._circuit_model import GroupCircuitModel
    small = next(g for g in base_groups if g.name == "$SSH_PORTS/0")
    circuit = G.group_circuit(small)
    model = GroupCircuitModel(circuit)
    model.resident = True
    dm.set_pipeline(D.NominationPipeline(D.ModelTransport(small, model),
                                         dm.stats))
    clk.t += 100                                   # filtered: not counted
    dm.set_pipeline(None)                          # rebuild window opens
    clk.t += 45                                    # one SF20-scale rebuild
    circuit2 = G.group_circuit(small)
    model2 = GroupCircuitModel(circuit2)
    model2.resident = True
    dm.set_pipeline(D.NominationPipeline(D.ModelTransport(small, model2),
                                         dm.stats))
    snap = dm.stats.snapshot()
    assert snap["unfiltered_seconds"] == pytest.approx(45.0)
    assert snap["resident"]["group"] == "$SSH_PORTS/0"
