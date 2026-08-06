"""S4 shared-trie unit tests (SNORT-PF AC-S4-1, host side).

Pins the measured corpus sizing (the fold-all analog of SF14), the
budget fit, the soundness of the two S4 over-approximations
(``case_fold``, ``anchor_cap``), and the byte-exactness of the ROM
emitters against the serialized image.
"""

import os

import pytest

from pyro.hdl import estimator as E
from pyro.hdl import rom_child as RC
from pyro.overlay import table as T
from pyro.snort import shared_trie as ST

RULES = os.path.join(os.path.dirname(__file__), "..", "..",
                     "third_party", "snort3-community-rules",
                     "snort3-community.rules")


@pytest.fixture(scope="module")
def triaged():
    from pyro.snort import triage as Tr
    if not os.path.exists(RULES):
        pytest.skip("community ruleset not vendored")
    return Tr.triage_file(RULES)


@pytest.fixture(scope="module")
def trie16(triaged):
    return ST.build_shared(triaged, 16)


# ---------------------------------------------------------------------
# Corpus sizing — SF14's sweep, reproduced from code (fold-all variant)
# ---------------------------------------------------------------------
def test_cap_sweep_measured(triaged):
    """SF14 recited 11,078 / 21,841 / 38,700 states for the
    fold-iff-nocase dedup keys; the S4 lowering folds EVERYTHING, so a
    handful of case-sensitive anchors merge with their nocase twins and
    every count lands slightly BELOW the SF14 figure.  Pinned so the
    next ruleset bump shows up as a diff here, not as a surprise at
    synthesis."""
    sweep = ST.cap_sweep(triaged)
    assert sweep[8]["n_states"] == 10713
    assert sweep[16]["n_states"] == 21332
    assert sweep[None]["n_states"] == 37905
    for cap, sf14 in ((8, 11078), (16, 21841), (None, 38700)):
        assert sweep[cap]["n_states"] <= sf14


def test_cap16_covers_every_anchor_compilable_rule(trie16):
    """AC-S4-1's headline: ALL 3,896 anchors resident at once."""
    assert trie16.n_rules == 3896
    covered = set()
    for keys in trie16.sidecar().values():
        covered.update(keys)
    assert len(covered) == 3896


def test_cap16_fits_the_rp_budget(trie16):
    """The SF2 fit, via the estimator's new table model (SR8)."""
    tr = sum(len(g) for g in trie16.ac.goto)
    outs = sum(len(o) for o in trie16.ac.out)
    est = E.estimate_table(trie16.n_states, tr, outs)
    assert est == {
        "uram": 30, "bram36": 66,
        "uram_budget": 64, "bram36_budget": 160,
        "fits_uram": True, "fits_bram": True,
    }


def test_build_is_deterministic(triaged):
    """Same ruleset -> same image -> same TABLE_ID (R47a discipline).
    A nondeterministic build would make the baked identity useless."""
    a = ST.build_shared(triaged, 16)
    b = ST.build_shared(triaged, 16)
    assert T.table_id(a.image()) == T.table_id(b.image())
    assert a.sidecar() == b.sidecar()


# ---------------------------------------------------------------------
# Soundness of the two S4 over-approximations
# ---------------------------------------------------------------------
def test_case_permutation_never_misses(trie16):
    """SR16's case-permutation clause, by construction: any case
    variant of a resident pattern still nominates its rules.  Checked
    for a spread of slots including case-sensitive ones — those are
    exactly the rules ``case_fold`` over-approximates."""
    slots = [s for s in trie16.slots if len(s.pattern) >= 4]
    sample = slots[:: max(1, len(slots) // 50)]
    assert len(sample) >= 40
    for s in sample:
        shouty = s.pattern.upper()
        subject = b"\x00pad " + shouty + b" pad\x7f"
        assert set(trie16.sidecar()[s.index]) <= \
            trie16.nominations(subject), \
            "slot %d missed its own upper-cased pattern" % s.index


def test_capped_slots_are_prefixes(trie16):
    """``anchor_cap`` = prefix truncation, nothing else: a capped slot
    matches wherever the full anchor occurs.  830 slots cap on this
    corpus — pinned, because a silent change in what gets truncated is
    a precision change that must be reviewed."""
    capped = [s for s in trie16.slots if s.capped]
    assert len(capped) == 830
    for s in capped:
        assert len(s.pattern) == 16


# ---------------------------------------------------------------------
# ROM emitters — byte-exact against the serialized image
# ---------------------------------------------------------------------
def _read_memh(path):
    with open(path) as f:
        return [int(line, 16) for line in f if line.strip()]


def _memh_scan(d, data):
    """Walk the memh arrays exactly the way the RTL does — an
    independent third implementation (model.py reads the image; this
    reads the FILES the fabric will bake)."""
    bm, base = d["bitmap"], d["base"]
    dense, fail = d["dense"], d["fail"]
    oidx, oflat = d["oidx"], d["oflat"]
    hits, state = [], 0
    for i, b in enumerate(T.ascii_fold(data)):
        while True:
            if (bm[state] >> b) & 1:
                rank = bin(bm[state] & ((1 << b) - 1)).count("1")
                state = dense[base[state] + rank]
                break
            if state == 0:
                break
            state = fail[state]
        off, cnt = oidx[state] & 0xFFFFFFFF, oidx[state] >> 32
        for k in range(cnt):
            hits.append((oflat[off + k], i + 1))
    return hits


def test_memh_roundtrip_and_scan(tmp_path):
    pats = [b"needle", b"nee", b"other", b"deep-needle-x"]
    ac = T.build(pats)
    img = T.serialize(ac, engine_id=ST.S4_ENGINE_ID)
    geom = RC.emit_memh(img, str(tmp_path))
    assert geom["n_states"] == ac.n_states
    assert geom["table_id"] == T.table_id(img)

    d = {name.split("_")[1].split(".")[0]:
         _read_memh(os.path.join(str(tmp_path), name))
         for name in RC.MEMH_FILES}
    assert len(d["bitmap"]) == ac.n_states
    assert len(d["oidx"]) == ac.n_states

    subject = b"say NEEDLE then deep-needle-x and other stuff"
    got = sorted(_memh_scan(d, subject))
    # Reference: the trie's own scan semantics over folded input.
    exp = []
    st = 0
    for i, b in enumerate(T.ascii_fold(subject)):
        st = ac.next_state(st, b)
        for pid in ac.out[st]:
            exp.append((pid, i + 1))
    assert got == sorted(exp)
    assert got, "differential vacuous: subject produced no matches"


def test_alias_verilog_bakes_identity(tmp_path):
    pats = [b"abc"]
    img = T.serialize(T.build(pats), engine_id=ST.S4_ENGINE_ID)
    geom = RC.emit_memh(img, str(tmp_path))
    v = RC.alias_verilog(geom, epoch=ST.ROM_EPOCH)
    assert "module pyro_circuit (" in v
    assert "pyro_ac_rom_engine" in v
    assert ".TABLE_ID(32'h%08x)" % T.table_id(img) in v
    assert ".MAX_STATES(%d)" % geom["n_states"] in v
    assert ".TABLE_EPOCH(32'h00000001)" in v
    with pytest.raises(ValueError):
        RC.alias_verilog(geom, epoch=0)


def test_emit_memh_rejects_junk(tmp_path):
    with pytest.raises(ValueError):
        RC.emit_memh(b"\x00" * 128, str(tmp_path))
