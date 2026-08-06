"""AC-S4-1 acceptance: the ROM-baked shared-trie child (SNORT-PF §6).

Three gates, in the phase's own order of proof:

  A. **Residency + fit, device-free.**  The full-corpus 16-byte-capped
     shared trie holds all 3,896 anchor-compilable rules at once and
     fits SF2's envelope through the SR8 table model.  This is the
     claim "kills the instantaneous-coverage cap" reduced to
     arithmetic.
  B. **Soundness under the S4 lowerings, device-free.**  Nominations
     survive case permutation (the fabric ``case_fold``) and anchor
     truncation (``anchor_cap``) on adversarially-cased subjects —
     the SR16 clause that suppression would later lean on, checked at
     nomination strength here.
  C. **The built child, artifact-gated.**  When a ``rom_trie`` build
     tree exists (hw/dfx/build*/…), its manifest must be byte-exact
     with a fresh corpus build (identity discipline: the .bit on disk
     attests THIS ruleset) and its link log must show the in-context
     timing gate MET and pr_verify OK.  Without a build tree the gate
     SKIPs with the honest R83a-shaped reason — a SKIP is never a
     PASS (SR18), and this file never fakes a timing number.
"""

import json
import os

import pytest

from pyro.hdl import estimator as E
from pyro.overlay import table as T
from pyro.snort import shared_trie as ST

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
RULES = os.path.join(REPO, "third_party", "snort3-community-rules",
                     "snort3-community.rules")


@pytest.fixture(scope="module")
def trie16():
    from pyro.snort import triage as Tr
    if not os.path.exists(RULES):
        pytest.skip("community ruleset not vendored")
    return ST.build_shared(Tr.triage_file(RULES), 16)


# ---------------------------------------------------------------------
# Gate A — residency + fit
# ---------------------------------------------------------------------
def test_gate_a_all_anchors_resident_and_fitting(trie16):
    covered = set()
    for keys in trie16.sidecar().values():
        covered.update(keys)
    assert len(covered) == 3896, \
        "instantaneous resident coverage is not the full anchor set"
    tr = sum(len(g) for g in trie16.ac.goto)
    outs = sum(len(o) for o in trie16.ac.out)
    est = E.estimate_table(trie16.n_states, tr, outs)
    assert est["fits_uram"] and est["fits_bram"], est


# ---------------------------------------------------------------------
# Gate B — soundness under case_fold + anchor_cap
# ---------------------------------------------------------------------
def _permute_case(data: bytes, seed: int) -> bytes:
    import random
    rng = random.Random(seed)
    out = bytearray()
    for b in data:
        if ((0x41 <= b <= 0x5A) or (0x61 <= b <= 0x7A)) \
                and rng.random() < 0.5:
            b ^= 0x20
        out.append(b)
    return bytes(out)


def test_gate_b_case_and_cap_never_lose_a_nomination(trie16):
    """For a spread of resident slots, embed the slot's pattern under
    random case permutation; the owning rules must still be nominated.
    The capped slots are the interesting half: their trie pattern is a
    16-byte PREFIX, so this also proves truncation kept completeness."""
    slots = [s for s in trie16.slots if len(s.pattern) >= 4]
    sample = slots[:: max(1, len(slots) // 60)]
    capped_in_sample = [s for s in sample if s.capped]
    assert capped_in_sample, "sample missed every capped slot"
    side = trie16.sidecar()
    for i, s in enumerate(sample):
        subject = (b"\x01noise " + _permute_case(s.pattern, seed=i)
                   + b" noise\xfe")
        got = trie16.nominations(subject)
        assert set(side[s.index]) <= got, \
            "slot %d lost its nomination under case permutation" % s.index


# ---------------------------------------------------------------------
# Gate C — the built child (artifact-gated, SR18-honest)
# ---------------------------------------------------------------------
def _build_trees():
    out = []
    dfx = os.path.join(REPO, "hw", "dfx")
    for d in sorted(os.listdir(dfx)):
        man = os.path.join(dfx, d, "partials", "rom_trie_manifest.json")
        link = os.path.join(dfx, d, "_rom_rm", "_link.out")
        verify = os.path.join(dfx, d, "_rom_rm", "_verify.out")
        if os.path.exists(man):
            out.append((os.path.join(dfx, d), man, link, verify))
    return out


def test_gate_c_built_child_attests_this_ruleset(trie16):
    trees = _build_trees()
    if not trees:
        pytest.skip("pr_flow_present=false — no rom_trie build tree "
                    "under hw/dfx/ (run hw/dfx/build_rom_rm.sh)")
    fresh = trie16.manifest()
    for tree, man_path, link, verify in trees:
        with open(man_path) as f:
            man = json.load(f)
        assert man["table_id"] == fresh["table_id"], \
            "%s attests table %s but this corpus builds %s" \
            % (tree, man["table_id"], fresh["table_id"])
        assert man["strong_id"] == fresh["strong_id"]
        assert man["n_rules"] == 3896
        bit = os.path.join(tree, "partials", "rom_trie.bit")
        if not os.path.exists(bit):
            pytest.skip("pr_flow_present=false — manifest without "
                        ".bit in %s (build incomplete)" % tree)
        with open(link) as f:
            log = f.read()
        assert "BUILD_RM_rom_trie_TIMING_MET" in log, \
            "%s: in-context timing gate not met" % tree
        with open(verify) as f:
            vlog = f.read()
        assert "PR_VERIFY_OK rom_trie" in vlog
        assert "PR_VERIFY_FAIL" not in vlog
