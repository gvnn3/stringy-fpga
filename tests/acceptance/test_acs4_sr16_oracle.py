"""SR16 differential oracle over the S4 shared trie (AC-S4-1's model).

This is the SR16 run for the SHARED trie — the evidence SR17 condition
(b) asks for.  It arbitrates the trie's *nomination semantics*; it does
not enable suppression anywhere (AC-S4-2 stays gated on the separate
SR17 owner approval).

Two gates, in order of authority — the AC-S2-3 structure, re-based onto
the trie's global pattern-id space:

**Gate 1 — the exact two-sided oracle (device-free, Snort-free).**
Every trie pattern is a fixed-length folded literal, so the expected
hit set for any subject is computable in closed form: one overlapping
``bytes.find`` sweep per slot over the FOLDED subject, independent of
the automaton.  The tests assert **set equality** against
``SharedTrie.scan`` — both directions, per (pattern_id, end).  The
Phase-S1 lesson stands: a match-everything nominator passes every
containment-only check, so containment alone is worthless; the two
sabotage tests below keep the gate falsifiable in both directions.
The daemon-shaped chunk path (SR11's 1,474-byte requests, SR12's
15-byte overlap tail = cap − 1) is proven equal to the whole-stream
scan, including an anchor straddling the chunk boundary at every
possible offset.

**Gate 2 — the Snort differential (SR16).**  Snort 3 arbitrates rule
semantics over recorded pcaps: one TCP session per tcp/ip raw-anchor
rule carrying that rule's real anchor (1,519 of 1,759 — udp/icmp/ssl
rules cannot ride a TCP session and are counted, never silently
dropped), plus an SR12 random-segmentation variant and an R15
case-permutation variant per rule where they apply, plus the designed
AC-S2-3 corpus (SF11 percent-encoding blinding, wrong-port SR13 case,
benign control).  Completeness — every ``(case, gid:sid)`` Snort
alerts on is nominated by the trie's chunked path — is asserted
**absolutely for the raw-anchor sub-tier** (the only tier SR17 could
ever permit suppression for) and against a **pinned zero-slack
budget** for normalized-buffer (SF11: encoding can blind those by
construction).  False positives are a counted metric per SR4 class —
extended with ``anchor_cap``, the class the 16-byte prefix cap adds —
never a pass/fail threshold; the distribution is pinned instead.

**Anti-vacuity.**  Pinned minimum counts of alerted rules, raw-anchor
alerts, true positives and oracle hits: an empty corpus, a mis-parsed
alert stream or a silently-absent arbiter cannot pass trivially.
No Snort is an honest SKIP, never a pass (SR18/R83a).
"""

import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import snortpf_s2_support as S            # noqa: E402
from pyro.overlay import table as OT      # noqa: E402
from pyro.snort import groups as G        # noqa: E402
from pyro.snort import shared_trie as ST  # noqa: E402
from pyro.snort import triage as T        # noqa: E402

#: SR12 overlap tail for the shared trie: every pattern is capped at
#: CAP_BYTES, so cap − 1 carried bytes make any straddling occurrence
#: whole in exactly one request.
TAIL = ST.CAP_BYTES - 1

#: The SR4 class the S4 cap adds: only the 16-byte folded PREFIX of the
#: rule's anchor occurred on the wire, not the full anchor.
FP_ANCHOR_CAP = "anchor_cap"

# The SR13 host-side variable table, extended to every port variable
# this corpus' raw tier references — mirrors snort_defaults.lua
# verbatim (ORACLE_PORTS = '1024:', SSH_PORTS = '22', ...).  Values are
# site configuration, never compiled in (SR13).
S.PORT_VARS.update({
    "$ORACLE_PORTS": frozenset(range(1024, 65536)),
    "$SSH_PORTS": frozenset({22}),
    "$FTP_PORTS": frozenset({21, 2100, 3535}),
    "$MAIL_PORTS": frozenset({110, 143}),
    "$SIP_PORTS": frozenset({5060, 5061, 5600}),
    "$FILE_DATA_PORTS": frozenset(S.HTTP_PORTS | {110, 143}),
})


# --------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------
@pytest.fixture(scope="module")
def triaged():
    return T.triage_file(S.CORPUS)


@pytest.fixture(scope="module")
def trie(triaged):
    return ST.build_shared(triaged)


@pytest.fixture(scope="module")
def side(trie):
    return trie.sidecar()


# --------------------------------------------------------------------
# Gate 1 helpers: the closed-form oracle and the chunked daemon path
# --------------------------------------------------------------------
def oracle_hits(trie, data):
    """The exact expected ``(pattern_id, end)`` set — one overlapping
    ``bytes.find`` sweep per slot over the folded subject.  Never
    touches the automaton (the S2 oracle-independence discipline)."""
    subject = OT.ascii_fold(data)
    out = set()
    for s in trie.slots:
        p = s.pattern
        i = subject.find(p)
        while i >= 0:
            out.add((s.index, i + len(p)))
            i = subject.find(p, i + 1)
    return out


def _scan_ac(ac, data):
    """Scan an arbitrary automaton with the model's exact semantics."""
    subject = OT.ascii_fold(data)
    st = 0
    out = set()
    for i, b in enumerate(subject):
        st = ac.next_state(st, b)
        for pid in ac.out[st]:
            out.add((pid, i + 1))
    return out


def _noms(trie, side, data):
    """Whole-stream gid:sid nominations (sidecar passed in: building
    it per call is quadratic over a 4,000-case corpus)."""
    hits = set()
    for pid, _end in trie.scan(data):
        hits.update(side[pid])
    return hits


def trie_nominations(trie, side, segments):
    """The daemon-shaped chunked nomination set (SR11/SR12): chunk each
    segment as it arrives, prefix the previous TAIL bytes of the flow.
    This is the nomination path Gate 2 holds against Snort."""
    hits = set()
    for req in S.chunk_requests(segments, TAIL):
        for pid, _end in trie.scan(req.buf):
            hits.update(side[pid])
    return hits


def trie_corpora(trie, seed=20260806):
    """Adversarial subjects shaped at the automaton: patterns back to
    back, case-permuted runs, prefix-then-pattern restarts, byte
    floods, whole-alphabet noise, self-overlap, random splices.
    Seeded, so a failure reproduces."""
    rnd = random.Random(seed)
    slots = trie.slots
    out = [b"".join(s.pattern for s in slots[:40])]

    def perm(b):
        return bytes((c ^ 0x20) if 65 <= (c & 0xDF) <= 90
                     and rnd.random() < 0.5 else c for c in b)

    out.append(b"".join(perm(s.pattern) for s in slots[:40]))
    buf = bytearray()
    for s in slots[:30]:
        p = s.pattern
        buf += p[:max(1, len(p) - 1)] + p
    out.append(bytes(buf))
    out.append(bytes(rnd.randrange(256) for _ in range(1200)))
    out.append(b"/" * 400)
    out.append(b"a" * 400)
    out.append(bytes(range(256)) * 3)
    for s in slots:
        if len(s.pattern) > 3:
            out.append(s.pattern + s.pattern[1:] + s.pattern)
            break
    for _ in range(20):
        b = bytearray()
        for _ in range(25):
            p = rnd.choice(slots).pattern
            i = rnd.randrange(len(p))
            j = rnd.randrange(i + 1, len(p) + 1)
            b += p[i:j]
            if rnd.random() < 0.3:
                b += bytes([rnd.randrange(256)])
        out.append(bytes(b))
    return out


# --------------------------------------------------------------------
# Gate 1: exact equality, chunk arithmetic, falsifiability
# --------------------------------------------------------------------
def test_exact_oracle_equality_over_adversarial_corpora(trie):
    total = 0
    for subj in trie_corpora(trie):
        want = oracle_hits(trie, subj)
        got = set(trie.scan(subj))
        assert got == want, (
            "scan != closed-form oracle: missing %r extra %r"
            % (sorted(want - got)[:5], sorted(got - want)[:5]))
        total += len(want)
    # Anti-vacuity: the corpora genuinely exercise the automaton —
    # pinned at the measured (seeded, deterministic) hit count.
    assert total == 3742, "corpora drifted: %d hits" % total


def test_equality_gate_rejects_a_match_everything_nominator(trie):
    """The S1 sabotage: a nominator that fires every pattern at every
    offset passes any containment-only completeness check.  Only the
    set-equality gate fails it — demonstrated, not assumed."""
    subj = b"GET /index.html HTTP/1.1\r\nHost: x\r\n\r\n"
    want = oracle_hits(trie, subj)
    fake = {(s.index, e) for s in trie.slots
            for e in range(1, len(subj) + 1)}
    assert want <= fake            # completeness-only would PASS it
    assert fake != want            # the equality gate is what bites


def test_equality_gate_catches_a_missing_pattern(trie):
    """The other direction: drop one pattern from the automaton (an
    index-aligned unmatchable stand-in, so every other pid keeps its
    id) and the equality gate must report the lost nomination."""
    victim = next(s for s in trie.slots if len(s.pattern) >= 8)
    subj = b"left " + victim.pattern + b" right"
    pats = [s.pattern for s in trie.slots]
    pats[victim.index] = b"\xfe" * 16
    got = _scan_ac(OT.build(pats), subj)
    want = oracle_hits(trie, subj)
    assert got != want
    assert (victim.index, 5 + len(victim.pattern)) in want - got


def test_chunk_boundary_straddle_every_offset(trie, side):
    """A 16-byte pattern straddling the 1,474-byte request boundary at
    every possible cut still nominates, and the chunked path equals
    the whole-stream scan exactly."""
    slot = next(s for s in trie.slots if len(s.pattern) == ST.CAP_BYTES)
    refs = set(side[slot.index])
    for cut in range(1, len(slot.pattern)):
        stream = (b"\x01" * (S.CHUNK_BYTES - cut) + slot.pattern
                  + b"\x01" * 40)
        whole = _noms(trie, side, stream)
        chunked = trie_nominations(trie, side, (stream,))
        assert chunked == whole, "cut=%d" % cut
        assert refs <= chunked, "cut=%d lost the straddler" % cut


def test_chunked_equals_whole_stream_over_corpora(trie, side):
    """Random TCP segmentation + SR11 chunking + the SR12 tail is
    exactly the whole-stream nomination set: the tail can never miss
    (every pattern ≤ cap) and a contiguous substring can never
    invent."""
    for i, subj in enumerate(trie_corpora(trie)):
        segs = S.random_segmentation(subj, seed=i, max_seg=80)
        assert (trie_nominations(trie, side, segs)
                == _noms(trie, side, subj)), "subject %d" % i


def test_case_permuted_segmented_nominations(trie, side):
    """R15 fold-all, under segmentation: any case permutation of any
    pattern nominates that slot's rules — sampled across the trie,
    always including a capped slot."""
    step = max(1, len(trie.slots) // 40)
    sample = list(trie.slots[::step])
    sample.append(next(s for s in trie.slots if s.capped))
    for s in sample:
        for k, p in enumerate(S.case_permutations(s.pattern)[:4]):
            subj = b"xx " + p + b" yy"
            segs = S.random_segmentation(subj, seed=s.index + k,
                                         max_seg=6)
            got = trie_nominations(trie, side, segs)
            assert set(side[s.index]) <= got, (
                "slot %d perm %d not nominated" % (s.index, k))


# --------------------------------------------------------------------
# Gate 2 helpers: corpus construction and SR4 classification
# --------------------------------------------------------------------
_DPORT = {}


def resolve_dport(token):
    """A concrete destination port satisfying the rule's dst token,
    under the extended SR13 table.  Fails loud on an unsatisfiable
    token rather than guessing."""
    tok = (token or "any").strip() or "any"
    if tok in _DPORT:
        return _DPORT[tok]
    for cand in (80, 21, 25, 110, 143, 443, 22, 53, 111, 445, 139,
                 1024, 5060, 8080):
        if S._port_holds(tok, cand):
            _DPORT[tok] = cand
            return cand
    for p in range(1, 65536):
        if S._port_holds(tok, p):
            _DPORT[tok] = p
            return p
    raise AssertionError("unsatisfiable dst port token %r" % tok)


def build_sweep_cases(triaged):
    """One TCP session per tcp/ip raw-anchor rule, carrying the rule's
    real anchor as the payload (offset-0: the position most raw rules
    constrain to), plus an SR12 random-segmentation variant and — for
    nocase rules — an R15 case-permutation variant.  Rules whose proto
    cannot ride a TCP session are returned in ``excluded`` so the
    coverage bound is stated, never silent."""
    by_line = {r.line_no: (r, res) for r, res in triaged}
    entries = G.groupable_entries(triaged)
    raw = [e for e in entries if e.ref.subtier == T.SUBTIER_RAW]
    excluded = {}
    cases = []
    port = 20000
    for e in raw:
        rule, _res = by_line[e.line_no]
        proto = rule.proto.lower()
        if proto not in ("tcp", "ip"):
            excluded[proto] = excluded.get(proto, 0) + 1
            continue
        dport = resolve_dport(rule.dst_port)
        a = e.anchor
        port += 1
        cases.append(S.Case("r%d" % e.sid, port, dport, (a,),
                            e.ref.buffer))
        if len(a) > 1:
            port += 1
            cases.append(S.Case(
                "r%d_seg" % e.sid, port, dport,
                S.random_segmentation(a, e.sid, max_seg=5),
                "SR12 split"))
        if e.nocase and any(65 <= (c & 0xDF) <= 90 for c in a):
            port += 1
            cases.append(S.Case(
                "r%d_perm" % e.sid, port, dport,
                (S.case_permutations(a)[-1],), "R15 permuted"))
    assert port < 40000, "sweep ports collide with the designed corpus"
    return cases, excluded


def classify_s4(rule, res, entry, flow, stream, folded):
    """Most-specific SR4 class for a nomination Snort rejected, with
    the two classes the S4 lowering adds ahead of the group-shaped
    classifier: ``anchor_cap`` (only the capped folded prefix occurred)
    and fold-all ``case_fold`` (case-sensitive rule, folded-only
    occurrence — the classifier's own branch covers only nocase)."""
    try:
        if not S.header_holds(rule, flow):
            return S.FP_HEADER
    except KeyError:
        # A literal net token the SR13 table does not model.  The
        # corpus carries exactly two ('64.245.58.0/23' and
        # '255.255.255.255' — pinned below), and neither contains the
        # pcap's fixed flow addresses, so the rule cannot fire on
        # these flows: header_predicate by construction.
        return S.FP_HEADER
    if OT.ascii_fold(entry.anchor) not in folded:
        return FP_ANCHOR_CAP
    if res.anchor is not None and not entry.nocase:
        if not S.anchor_occurrences(res.anchor.pattern, False, stream):
            return S.FP_CASE_FOLD
    return S.classify_fp(rule, res, entry, flow, stream)


@pytest.fixture(scope="module")
def sr16(trie, side, triaged, tmp_path_factory):
    ok, why = S.snort_present()
    if not ok:
        pytest.skip(why)          # R83a: an honest SKIP, never a pass
    return run_sr16(trie, side, triaged,
                    str(tmp_path_factory.mktemp("sr16")))


def run_sr16(trie, side, triaged, wd):
    """The full Gate-2 run: corpus, pcap, Snort, differential."""
    by_line = {r.line_no: (r, res) for r, res in triaged}
    entries = G.groupable_entries(triaged)
    index = {(e.gid, e.sid): e for e in entries}
    sweep, excluded = build_sweep_cases(triaged)
    cases = sweep + S.build_cases()
    rules_path = os.path.join(wd, "anchor.rules")
    with open(rules_path, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(by_line[e.line_no][0].raw + "\n")
    pcap = S.write_pcap(os.path.join(wd, "sr16.pcap"), cases)
    alerts = S.run_snort(rules_path, pcap, wd)
    amap = S.alerts_by_case(alerts, cases)

    tp = 0
    misses_raw = []
    misses_norm = []
    fp_by_class = {c: 0 for c in S.FP_CLASSES + (FP_ANCHOR_CAP,)}
    alerted_keys = set()
    alerted_raw_keys = set()
    cases_with_alerts = 0
    foreign = []
    for case in cases:
        noms = trie_nominations(trie, side, case.segments)
        alerted = amap.get(case.name, set())
        if alerted:
            cases_with_alerts += 1
        alerted_strs = set()
        for key in sorted(alerted):
            e = index.get(key)
            if e is None:
                foreign.append((case.name,) + key)
                continue
            alerted_keys.add(key)
            kstr = "%d:%d" % key
            alerted_strs.add(kstr)
            raw_tier = e.ref.subtier == T.SUBTIER_RAW
            if raw_tier:
                alerted_raw_keys.add(key)
            if kstr in noms:
                tp += 1
            elif raw_tier:
                misses_raw.append((case.name,) + key + (e.ref.buffer,))
            else:
                misses_norm.append(
                    (case.name,) + key + (e.ref.buffer,))
        stream = case.stream
        folded = OT.ascii_fold(stream)
        for kstr in sorted(noms - alerted_strs):
            gid, sid = (int(x) for x in kstr.split(":"))
            e = index[(gid, sid)]
            rule, res = by_line[e.line_no]
            cls = classify_s4(rule, res, e, case.flow, stream, folded)
            fp_by_class[cls] += 1
    return dict(
        n_cases=len(cases), n_rules=len(entries), excluded=excluded,
        alerted=len(alerted_keys), alerted_raw=len(alerted_raw_keys),
        tp=tp, misses_raw=misses_raw, misses_norm=misses_norm,
        fp_by_class=fp_by_class, foreign=foreign,
        cases_with_alerts=cases_with_alerts)


# --------------------------------------------------------------------
# Gate 2: the Snort differential
# --------------------------------------------------------------------
def test_raw_anchor_completeness_is_absolute(sr16):
    """SR16's absolute clause for the only tier SR17 could ever permit
    suppression on: zero exceptions, not 'few'."""
    assert sr16["misses_raw"] == [], (
        "raw-anchor completeness misses: %r" % sr16["misses_raw"][:10])


def test_normalized_buffer_misses_are_the_pinned_sf11_budget(sr16):
    """Normalized-buffer misses must be exactly the SF11 blinding the
    corpus deliberately carries — the two percent-encoded URIs whose
    anchors exist only after http_inspect normalization.  Zero slack:
    any new miss fails."""
    got = sorted((case, gid, sid)
                 for case, gid, sid, _buf in sr16["misses_norm"])
    assert got == [("percent_encoded_uri", 1, 835),
                   ("percent_encoded_uri2", 1, 824)], (
        "normalized-buffer misses drifted: %r" % got)


def test_false_positive_distribution_is_pinned(sr16):
    """FPs are a counted metric per SR4 class, never a gate — but the
    distribution is pinned at the measured 2026-08-06 run, because
    'some class applies' is a tautology and the SHAPE is the signal.
    ``anchor_cap`` (38,205: 16-byte prefix families cross-nominate)
    and fold-all ``case_fold`` (2,455) are the two classes the S4
    lowering adds; every rejected nomination lands in a named class
    (unclassified == 0)."""
    assert sr16["fp_by_class"] == {
        S.FP_HEADER: 34505, S.FP_CASE_FOLD: 2455,
        S.FP_ANCHOR_STRIP: 5381, S.FP_DROPPED: 1292,
        S.FP_UNCLASSIFIED: 0, FP_ANCHOR_CAP: 38205,
    }, sr16["fp_by_class"]


def test_anti_vacuity_pinned_minimums(sr16):
    """The differential is powered: floors pinned at the 2026-08-06
    measured run with zero slack.  Snort confirms 350 distinct
    raw-anchor rules on this corpus — the completeness claim is
    measured over those, not over a handful of hand-picked cases."""
    assert sr16["n_cases"] == 3925, sr16["n_cases"]
    assert sr16["alerted"] >= 360, sr16["alerted"]
    assert sr16["alerted_raw"] >= 350, sr16["alerted_raw"]
    assert sr16["tp"] >= 914, sr16["tp"]
    assert sr16["cases_with_alerts"] >= 875, sr16["cases_with_alerts"]
    assert sr16["foreign"] == [], sr16["foreign"][:5]


def test_precision_delta_tiers_separate_strictly(trie, triaged):
    """Falsifiability for the precision harness: every tier boundary
    is demonstrated with a subject built to sit exactly on it, so a
    harness that collapsed two tiers could not pass."""
    prep = ST.precision_prepare(trie, triaged)
    # cap: the 16-byte folded PREFIX of a >16-byte anchor, alone —
    # the trie nominates, the uncapped tier must not.
    long_r = next(r for r in prep["recs"]
                  if len(r[1]) > ST.CAP_BYTES)
    d = ST.precision_delta_shared(trie, prep,
                                  long_r[1][:ST.CAP_BYTES])
    assert d.sound and d.extra_cap >= 1, d
    # fold: a case-sensitive anchor presented folded — uncapped
    # nominates, the A5 anchor tier (fold iff nocase) must not.
    cs = next(r for r in prep["recs"]
              if not r[3] and r[2] != r[1]
              and len(r[1]) <= ST.CAP_BYTES)
    d = ST.precision_delta_shared(trie, prep, cs[1])
    assert d.sound and d.extra_fold >= 1, d
    # chain: an anchor whose lowered chain needs more than the anchor
    # — the anchor tier nominates, the lowered tier must not.
    ch = next(r for r in prep["recs"]
              if r[4] is not None and not r[4].search(r[2]))
    d = ST.precision_delta_shared(trie, prep, ch[2])
    assert d.sound and d.extra_chain >= 1, d


def test_precision_delta_at_cap16_is_pinned(trie, triaged):
    """The S4 trie's over-nomination vs the AC-S3-2 lowered chains,
    measured over the SR16 corpus' unique streams (sweep plain + R15
    permuted + designed; the _seg variants are byte-identical streams
    and are excluded) and pinned at the 2026-08-06 run.

    The decomposition is the number the cap-16 decision owes SF14:
    of 52,249 trie nomination events, the 16-byte prefix cap alone
    contributes 26,245 (50.2%) — capped prefixes collapse rule
    families — fold-all contributes 5,896 (11.3%), anchor-only vs
    chains 5,921 (11.3%; the price A5 already paid), and 14,187
    (27.2%) survive the lowered chains.  All over-nomination, never
    missed detections: soundness holds on every stream."""
    prep = ST.precision_prepare(trie, triaged)
    sweep, _exc = build_sweep_cases(triaged)
    streams = ([c.stream for c in sweep
                if not c.name.endswith("_seg")]
               + [c.stream for c in S.build_cases()])
    assert len(streams) == 2436, len(streams)
    tot = dict.fromkeys(("trie", "uncapped", "anchor", "lowered",
                         "extra_cap", "extra_fold", "extra_chain"), 0)
    for s in streams:
        d = ST.precision_delta_shared(trie, prep, s)
        assert d.sound, "soundness chain broken on %r..." % s[:40]
        for f in tot:
            tot[f] += getattr(d, f)
    assert tot == {"trie": 52249, "uncapped": 26004, "anchor": 20108,
                   "lowered": 14187, "extra_cap": 26245,
                   "extra_fold": 5896, "extra_chain": 5921}, tot


def test_literal_net_tokens_are_the_known_two(triaged):
    """``classify_s4``'s KeyError branch (literal net token ⇒
    header_predicate) is sound only while every literal token in the
    corpus excludes the pcap's fixed flow addresses.  Enumerated and
    pinned, with the exclusion proven, not assumed."""
    import ipaddress
    by_line = {r.line_no: (r, res) for r, res in triaged}
    toks = set()
    for e in G.groupable_entries(triaged):
        r = by_line[e.line_no][0]
        for t in (r.src_net, r.dst_net):
            if t != "any" and not (t.startswith("$")
                                   and "[" not in t):
                toks.add(t)
    assert toks == {"64.245.58.0/23", "255.255.255.255"}, toks
    for ip in (S.CLI_IP_S, S.SRV_IP_S):
        for t in toks:
            assert (ipaddress.ip_address(ip)
                    not in ipaddress.ip_network(t)), (ip, t)


def test_excluded_protocols_are_counted(triaged):
    """The coverage bound, stated: raw-anchor rules that cannot ride a
    TCP session.  Pinned so a ruleset drift is visible here."""
    _cases, excluded = build_sweep_cases(triaged)
    assert excluded == {"udp": 197, "icmp": 41, "ssl": 2}, excluded
