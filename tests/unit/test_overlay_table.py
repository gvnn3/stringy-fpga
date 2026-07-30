"""Overlay engine: AC semantics, serialization, identity, fail-closed load.

The load-bearing test is :func:`test_ac_matches_the_closed_form_oracle`:
the table engine is checked against an *independent* implementation
(``bytes.find`` per pattern), not against itself. That is the SR16
discipline applied to a new engine — a new matcher is only as trustworthy
as the oracle it disagrees with.
"""

import os

import pytest

from pyro.overlay import model as M
from pyro.overlay import table as T


def load(engine, image, chunk=64):
    engine.table_begin(len(image), T.table_id(image), engine.engine_id,
                       T.TABLE_FORMAT_VERSION, 1, 1)
    for off in range(0, len(image), chunk):
        engine.table_data(off, image[off:off + chunk])
    return engine.table_commit(T.table_id(image))


def engine_with(patterns, engine_id=0xABCD):
    ac = T.build(patterns)
    img = T.serialize(ac, engine_id=engine_id)
    eng = M.OverlayEngineModel(engine_id=engine_id)
    load(eng, img)
    return eng, ac, img


def occurrences(patterns, data):
    """Independent oracle: every (pattern_id, end) by plain substring search."""
    out = set()
    for pid, p in enumerate(patterns):
        if not p:
            continue
        i = data.find(p)
        while i >= 0:
            out.add((pid, i + len(p)))
            i = data.find(p, i + 1)
    return out


# --------------------------------------------------------------------------
# Semantics, against an independent oracle
# --------------------------------------------------------------------------
@pytest.mark.parametrize("patterns,data", [
    ([b"abc", b"bcd", b"cde", b"a"], b"xxabcdexx"),
    ([b"he", b"she", b"his", b"hers"], b"ushers he his hershe"),
    ([b"aa", b"aaa"], b"aaaaa"),                       # overlapping, nested
    ([b"x"], b"xxxxx"),                                # every position
    ([b"needle"], b"no match here"),
    ([b"\x00\x01", b"\xff"], bytes(range(256))),       # binary-safe
])
def test_ac_matches_the_closed_form_oracle(patterns, data):
    eng, _ac, _img = engine_with(patterns)
    hits, ovf = eng.scan(data, out_cap=1 << 20)
    assert not ovf
    assert {(h.pattern_id, h.end) for h in hits} == occurrences(patterns, data)


def test_tombstones_keep_their_pattern_id():
    """A None entry occupies its index and can never match — the same
    contract slots have in the bitstream groups (SR6)."""
    pats = [b"aaa", None, b"bbb"]
    eng, _ac, _img = engine_with(pats)
    hits, _ = eng.scan(b"aaabbb", out_cap=100)
    assert {h.pattern_id for h in hits} == {0, 2}


def test_every_match_reports_an_exact_end_offset():
    eng, _ac, _img = engine_with([b"cde"])
    hits, _ = eng.scan(b"abcdef", out_cap=10)
    assert [(h.pattern_id, h.end) for h in hits] == [(0, 5)]


def test_out_cap_overflows_rather_than_dropping_silently():
    eng, _ac, _img = engine_with([b"a", b"ab", b"abc"])
    hits, ovf = eng.scan(b"abc", out_cap=2)
    assert ovf and len(hits) == 2


# --------------------------------------------------------------------------
# Serialization is what the model reads, and what identity covers
# --------------------------------------------------------------------------
def test_serialization_is_deterministic():
    """TABLE_ID hashes the image, so a nondeterministic layout would make
    identity meaningless."""
    pats = [b"alpha", b"beta", b"gamma"]
    a = T.serialize(T.build(pats), engine_id=7)
    b = T.serialize(T.build(pats), engine_id=7)
    assert a == b and T.table_id(a) == T.table_id(b)


def test_different_rules_give_different_identity():
    a = T.serialize(T.build([b"alpha"]), engine_id=7)
    b = T.serialize(T.build([b"alphb"]), engine_id=7)
    assert T.table_id(a) != T.table_id(b)
    assert T.strong_id(a) != T.strong_id(b)


def test_table_id_is_never_zero():
    """Zero is reserved for 'nothing valid', the same convention
    rp_child_id uses."""
    assert T.table_id(b"") != 0
    assert T.table_id(T.serialize(T.build([b"q"]))) != 0


def test_manifest_carries_the_audit_chain():
    ac = T.build([b"aa", b"bb"])
    img = T.serialize(ac, engine_id=0x1234)
    m = T.manifest(img, ac, 0x1234, rule_keys=[["1:100"], ["1:200", "1:201"]])
    assert m["engine_id"] == 0x1234
    assert m["table_id"] == "0x%08x" % T.table_id(img)
    assert m["strong_id"] == T.strong_id(img).hex()
    assert m["sidecar"]["1"] == ["1:200", "1:201"]
    assert m["image_bytes"] == len(img)


# --------------------------------------------------------------------------
# Fail-closed load protocol (A5 §5)
# --------------------------------------------------------------------------
def test_nothing_is_valid_after_reset():
    eng, _ac, img = engine_with([b"zz"])
    assert eng.table_id != 0
    eng.reset()
    assert eng.table_id == 0 and eng.epoch == 0
    assert eng.scan(b"zz")[0] == ()      # no table, no claims


def test_wrong_engine_is_refused_before_any_transfer():
    img = T.serialize(T.build([b"a"]), engine_id=1)
    eng = M.OverlayEngineModel(engine_id=2)
    with pytest.raises(M.TableError, match="engine_id"):
        eng.table_begin(len(img), T.table_id(img), 1,
                        T.TABLE_FORMAT_VERSION, 1, 1)


def test_capacity_is_gated_at_begin():
    eng = M.OverlayEngineModel(engine_id=1, capacity_states=4)
    with pytest.raises(M.TableError, match="n_states"):
        eng.table_begin(100, 1, 1, T.TABLE_FORMAT_VERSION, 5, 1)
    with pytest.raises(M.TableError, match="n_patterns"):
        eng.table_begin(100, 1, 1, T.TABLE_FORMAT_VERSION, 1, 1 << 30)


def test_incomplete_transfer_cannot_commit():
    ac = T.build([b"hello"])
    img = T.serialize(ac, engine_id=5)
    eng = M.OverlayEngineModel(engine_id=5)
    eng.table_begin(len(img), T.table_id(img), 5, T.TABLE_FORMAT_VERSION,
                    ac.n_states, 1)
    eng.table_data(0, img[:20])
    with pytest.raises(M.TableError, match="incomplete"):
        eng.table_commit(T.table_id(img))
    assert eng.table_id == 0


def test_crc_mismatch_is_refused_and_leaves_the_active_table_intact():
    eng, ac, good = engine_with([b"good"])
    before_id, before_epoch = eng.table_id, eng.epoch
    bad_ac = T.build([b"evil"])
    bad = T.serialize(bad_ac, engine_id=eng.engine_id)
    eng.table_begin(len(bad), T.table_id(bad), eng.engine_id,
                    T.TABLE_FORMAT_VERSION, bad_ac.n_states, 1)
    for off in range(0, len(bad), 64):
        eng.table_data(off, bad[off:off + 64])
    with pytest.raises(M.TableError, match="CRC mismatch"):
        eng.table_commit(0xDEADBEEF)
    assert eng.table_id == before_id and eng.epoch == before_epoch
    assert {h.pattern_id for h in eng.scan(b"good")[0]} == {0}


def test_out_of_range_chunk_is_refused():
    eng = M.OverlayEngineModel(engine_id=1)
    eng.table_begin(100, 1, 1, T.TABLE_FORMAT_VERSION, 1, 1)
    with pytest.raises(M.TableError, match="outside declared"):
        eng.table_data(90, b"x" * 20)


def test_retransmitted_chunks_do_not_fake_completion():
    """Byte accounting must count coverage, not arrivals, or a duplicated
    chunk would let an incomplete image commit."""
    ac = T.build([b"abcdef"])
    img = T.serialize(ac, engine_id=9)
    eng = M.OverlayEngineModel(engine_id=9)
    eng.table_begin(len(img), T.table_id(img), 9, T.TABLE_FORMAT_VERSION,
                    ac.n_states, 1)
    half = len(img) // 2
    for _ in range(5):                       # same chunk five times
        eng.table_data(0, img[:half])
    assert eng.status()["shadow_bytes_received"] == half
    with pytest.raises(M.TableError, match="incomplete"):
        eng.table_commit(T.table_id(img))


def test_transfer_is_restartable_out_of_order():
    ac = T.build([b"restart"])
    img = T.serialize(ac, engine_id=3)
    eng = M.OverlayEngineModel(engine_id=3)
    eng.table_begin(len(img), T.table_id(img), 3, T.TABLE_FORMAT_VERSION,
                    ac.n_states, 1)
    chunks = [(o, img[o:o + 50]) for o in range(0, len(img), 50)]
    for off, c in reversed(chunks):          # arrive backwards
        eng.table_data(off, c)
    assert eng.table_commit(T.table_id(img)) == 1


def test_abort_discards_the_shadow():
    eng, _ac, _img = engine_with([b"keep"])
    eng.table_begin(50, 1, eng.engine_id, T.TABLE_FORMAT_VERSION, 1, 1)
    eng.table_abort()
    assert not eng.status()["shadow_open"]
    with pytest.raises(M.TableError, match="no open transfer"):
        eng.table_commit(1)


# --------------------------------------------------------------------------
# Epoch: temporal identity (A5 §2.2, SR14′)
# --------------------------------------------------------------------------
def test_epoch_increments_per_commit_and_stamps_matches():
    eng, _ac, _img = engine_with([b"one"])
    assert eng.epoch == 1
    assert all(h.epoch == 1 for h in eng.scan(b"one")[0])
    ac2 = T.build([b"two"])
    img2 = T.serialize(ac2, engine_id=eng.engine_id)
    load(eng, img2)
    assert eng.epoch == 2
    hits = eng.scan(b"two")[0]
    assert hits and all(h.epoch == 2 for h in hits)
    assert eng.scan(b"one")[0] == ()         # old rules genuinely gone


def test_matches_never_straddle_a_commit():
    """A match carries the epoch of the table that produced it, so the host
    can never attribute it with the wrong sidecar."""
    eng, _ac, _img = engine_with([b"aaa"])
    first = eng.scan(b"aaa")[0]
    ac2 = T.build([b"aaa", b"bbb"])
    load(eng, T.serialize(ac2, engine_id=eng.engine_id))
    second = eng.scan(b"aaa")[0]
    assert first[0].epoch == 1 and second[0].epoch == 2
    assert first[0].epoch != second[0].epoch


# --------------------------------------------------------------------------
# Against the real corpus
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def corpus_group():
    from pyro.snort import groups as G
    from pyro.snort import triage as Tr
    path = os.path.join(os.path.dirname(__file__), "..", "..",
                        "third_party", "snort3-community-rules",
                        "snort3-community.rules")
    if not os.path.exists(path):
        pytest.skip("community ruleset not vendored")
    return next(g for g in G.pack_groups(Tr.triage_file(path))
                if g.name == "$HTTP_PORTS/0")


def test_real_group_anchors_round_trip_through_the_image(corpus_group):
    anchors = [None if s.tombstone else s.anchor for s in corpus_group.slots]
    ac = T.build(anchors)
    img = T.serialize(ac, engine_id=1)
    eng = M.OverlayEngineModel(engine_id=1)
    load(eng, img, chunk=1474)               # R78-sized chunks
    subject = b"GET /view-source HTTP/1.0\r\n\r\n"
    hits, _ = eng.scan(subject, out_cap=1 << 16)
    assert {(h.pattern_id, h.end) for h in hits} == occurrences(anchors, subject)


def test_overlay_is_sound_versus_the_lowered_circuits(corpus_group):
    """SR3: anchor-only AC must never MISS what the lowered chain circuits
    catch. Extra nominations are the price; missed ones would be a defect."""
    for subject in (b"GET /view-source HTTP/1.0",
                    b"POST /x \x00\x01\x86\xa0\x00\x00\x00\x00\x00\x00\x00\x03",
                    b"benign traffic here"):
        d = T.precision_delta(corpus_group, subject)
        assert d["sound"], d
        assert d["missed"] == 0


# --------------------------------------------------------------------------
# Case handling — the defect the corpus caught on first run
# --------------------------------------------------------------------------
def test_ascii_fold_is_ascii_only():
    """A Unicode-aware fold is what shipped a match-everything circuit in
    S1; the fold set must be exactly the 2-byte ASCII one (R15)."""
    assert T.ascii_fold(b"AbC-9\xc3\x89") == b"abc-9\xc3\x89"
    assert T.ascii_fold(bytes([0xDF])) == bytes([0xDF])   # not folded


def test_nocase_anchor_matches_mixed_case():
    """The bug the AC-S2-3 corpus found: a slot stores its anchor FOLDED,
    so a case-blind build misses mixed-case traffic entirely."""
    tbl = T.CaseSplitTable([b"evilcmd"], [True])
    for subject in (b"xxEviLCmDxx", b"xxEVILCMDxx", b"xxevilcmdxx"):
        assert [pid for pid, _e in tbl.scan(subject)] == [0], subject


def test_case_sensitive_anchor_does_not_match_other_case():
    """Exactness in the other direction: merging both cases onto shared
    states would over-approximate here, which is why the design splits."""
    tbl = T.CaseSplitTable([b"EvilCmd"], [False])
    assert [pid for pid, _e in tbl.scan(b"xxEvilCmdxx")] == [0]
    assert tbl.scan(b"xxevilcmdxx") == []


def test_mixed_case_sensitivity_in_one_table_stays_exact():
    tbl = T.CaseSplitTable([b"abc", b"abc"], [True, False])
    assert sorted(pid for pid, _e in tbl.scan(b"ABC")) == [0]      # ci only
    assert sorted(pid for pid, _e in tbl.scan(b"abc")) == [0, 1]   # both


def test_nocase_build_is_linear_not_exponential():
    """Branching the trie on both cases makes a 20-letter nocase anchor
    2^20 states; the build must stay proportional to pattern length."""
    pat = b"abcdefghijklmnopqrst"        # 20 letters
    tbl = T.CaseSplitTable([pat], [True])
    assert tbl.n_states < 4 * len(pat)


# --------------------------------------------------------------------------
# Full-corpus scale — the sizes the engine is actually meant to hold
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def full_corpus_table():
    from pyro.snort import groups as G
    from pyro.snort import triage as Tr
    path = os.path.join(os.path.dirname(__file__), "..", "..",
                        "third_party", "snort3-community-rules",
                        "snort3-community.rules")
    if not os.path.exists(path):
        pytest.skip("community ruleset not vendored")
    pats, nc = [], []
    for g in G.pack_groups(Tr.triage_file(path)):
        for s in g.slots:
            pats.append(None if s.tombstone else s.anchor)
            nc.append(bool(s.nocase))
    return T.CaseSplitTable(pats, nc), pats, nc


def test_full_corpus_fits_the_rp_budget(full_corpus_table):
    """SF2's RP envelope is 160 BRAM36 + 64 URAM.  The placement decision
    (bitmap and out_idx to URAM, the rest to BRAM) is arithmetic, so pin
    it here rather than rediscovering it at synthesis time."""
    tbl, _pats, _nc = full_corpus_table
    n = tbl.n_states
    tr = sum(len(g) for g in tbl.ci.goto) + sum(len(g) for g in tbl.cs.goto)
    outs = sum(len(o) for o in tbl.ci.out) + sum(len(o) for o in tbl.cs.out)

    def uram(w, d):     # URAM288 = 4096 deep x 72 wide
        return ((w + 71) // 72) * ((d + 4095) // 4096)

    def bram(w, d):
        return ((w * d) + (36 * 1024) - 1) // (36 * 1024)

    u = uram(256, n) + uram(64, n)                      # bitmap + oidx
    b = bram(32, n) * 2 + bram(32, tr) + bram(32, outs)  # base/fail/dense/oflat
    assert u <= 64, "URAM %d over the 64 budget" % u
    assert b <= 160, "BRAM36 %d over the 160 budget" % b
    # ...and the reason both had to move: bitmap alone in BRAM blows it.
    assert bram(256, n) > 160


def test_full_corpus_loads_and_matches_at_scale(full_corpus_table):
    """Build -> serialize -> load through the A5 protocol in jumbo-sized
    chunks -> scan, all at the real 39,647-state scale, checked against an
    independent oracle rather than against the engine itself."""
    tbl, pats, nc = full_corpus_table
    img = T.serialize(tbl.ci, engine_id=0x0A5E0001)
    assert len(img) > 1_000_000, "expected a corpus-scale image"

    eng = M.OverlayEngineModel(engine_id=0x0A5E0001,
                               capacity_states=40960,
                               capacity_patterns=1 << 16)
    eng.table_begin(len(img), T.table_id(img), 0x0A5E0001,
                    T.TABLE_FORMAT_VERSION, tbl.ci.n_states,
                    len(tbl.ci.patterns))
    for o in range(0, len(img), 9568):        # R78.9a jumbo payload
        eng.table_data(o, img[o:o + 9568])
    assert eng.table_commit(T.table_id(img)) == 1

    subj = T.ascii_fold(b"GET /view-source?f=/etc/passwd HTTP/1.1\r\n\r\n")
    hits, _ = eng.scan(subj, out_cap=1 << 16)
    got = {(h.pattern_id, h.end) for h in hits}
    exp = set()
    for pid, p in enumerate(tbl.ci.patterns):
        if not p:
            continue
        k = subj.find(p)
        while k >= 0:
            exp.add((pid, k + len(p)))
            k = subj.find(p, k + 1)
    assert got == exp and got, "scale scan diverged from the oracle"
