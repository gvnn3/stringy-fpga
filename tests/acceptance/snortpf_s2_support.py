"""AC-S2-3 support: the SR16 differential-oracle harness (SNORT-PF Phase S2).

Everything the AC-S2-3 clauses need that is not itself an assertion: the
AC-S2-2 group, the *exact* two-sided nomination oracle, the daemon-shaped
chunking path (SR11/SR12), the pcap corpora, the Snort runner, the pinned
host-side variable table (SR13) and the SR4 false-positive classifier.

Why the oracle is exact, and why that matters
---------------------------------------------
Every slot of a SNORT-PF group is a **fixed-length literal** (spec §2
"Anchor"; ``pyro.snort.groups.slot_pattern`` lowers it with ``re.escape`` in
BYTES mode).  So for a given byte stream the set of nominations is not merely
bounded — it is *computable in closed form*: a case-folded ``bytes.find``
sweep, one per slot.  :func:`oracle_windows` computes it independently of the
generator (it never touches an automaton), and the tests assert **set
equality** against the model, per slot.

Set equality, not containment, is the whole point.  The Phase-S1 silicon
failure (docs/notebook.md, 2026-07-27 evening) was a *complete* circuit that
matched **everything** — a str-mode ``nocase`` lowering that over-approximated
to "any code point".  Every completeness-only (containment) test passed it.
A false-positive-rate heuristic would not have caught it reliably either: 23
of the 256 anchors in the real group are ≤ 5 bytes and fire legitimately and
often, so any "too many hits ⇒ broken" tripwire is either blind to the defect
or noisy on healthy circuits.  Exact set equality is the only gate that both
fails the match-everything circuit and passes a correct one, unconditionally.

The gates
---------
1. :func:`oracle_windows` vs :meth:`GroupCircuitModel.scan` — device-free,
   Snort-free, exact, both directions.  This is the primary gate.

   **It does not touch the emitted RTL.**  :func:`group_model` builds the
   circuit through :func:`pyro.snort.groups.group_circuit` and then wraps it in
   :class:`GroupCircuitModel`, which consumes only ``.automata``; ``.rtl`` is
   generated and discarded.  So this gate arbitrates the *lowering*, not the
   SR7 emitter — the skid, ``pend``, priority encoder, ``in_ready`` and the
   drain are all invisible to it.  ``tests/hw/xsim_diff.py``, invoked from
   ``test_acs2_3_oracle.py``'s gate 1b, is what executes the Verilog.
1b. The xsim RTL differential — see the test module's docstring.
2. The Snort differential (SR16): Snort arbitrates *rule* semantics, the
   oracle above arbitrates *window arithmetic*.  Completeness — every
   ``(case, sid)`` Snort alerts on is nominated — is asserted absolutely for
   the ``raw-anchor`` sub-tier and against a pinned per-buffer budget for
   ``normalized-buffer`` (SF11: those anchors can be blinded by encoding *by
   construction*, e.g. ``%2d`` in a URI that ``http_inspect`` normalizes and
   the wire does not).  False positives are a **counted metric** per SR4
   class, never a threshold.
"""

from __future__ import annotations

import os
import re
import struct
import subprocess
from typing import (Dict, FrozenSet, List, NamedTuple, Optional,
                    Sequence, Set, Tuple)

from pyro._circuit_model import GroupCircuitModel
from pyro.snort import groups as G
from pyro.snort import triage as T

REPO = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.abspath(__file__))))
CORPUS = os.path.join(REPO, "third_party", "snort3-community-rules",
                      "snort3-community.rules")

#: R78 corpus bound per ``MATCH_REQUEST`` (SR11).
CHUNK_BYTES = 1474

#: Snort 3 install used as the SR16 arbiter (spec §4.6).
SNORT_BIN = "/home/gnn/opt/snort3/bin/snort"
SNORT_LUA = "/home/gnn/opt/snort3/etc/snort/snort.lua"
SNORT_LIBS = "/home/gnn/opt/snort3/lib:/home/gnn/opt/snort3/lib64"


def snort_present() -> Tuple[bool, str]:
    """(available, reason) for the SR16 arbiter — never raises."""
    if not os.path.exists(SNORT_BIN):
        return False, "snort_present=false — %s not installed" % SNORT_BIN
    if not os.path.exists(SNORT_LUA):
        return False, "snort_present=false — %s missing" % SNORT_LUA
    return True, "snort_present=true — %s" % SNORT_BIN


# --------------------------------------------------------------------------
# The AC-S2-2 group
# --------------------------------------------------------------------------
_GROUP_CACHE: Dict[str, object] = {}


def ac_s2_2_group():
    """The pinned AC-S2-2 group: ``$HTTP_PORTS/0`` (256 rules → 253 slots)."""
    if "group" not in _GROUP_CACHE:
        triaged = T.triage_file(CORPUS)
        groups = G.pack_groups(triaged)
        group = [g for g in groups if g.port_class == "$HTTP_PORTS"][0]
        _GROUP_CACHE["group"] = group
        _GROUP_CACHE["triaged"] = triaged
        _GROUP_CACHE["by_line"] = {r.line_no: (r, res) for r, res in triaged}
    return _GROUP_CACHE["group"]


def rule_by_line(line_no: int):
    """``(Rule, TriageResult)`` for a sidecar row's provenance line."""
    ac_s2_2_group()
    return _GROUP_CACHE["by_line"][line_no]


def group_model(group, datapath_bytes: int = 1) -> GroupCircuitModel:
    """The resident software twin of ``group``'s circuit (SR16's model side).

    Built through :func:`pyro.snort.groups.group_circuit`, i.e. the *generated*
    automata — a lowering defect surfaces here instead of being masked.

    It consumes ``circuit.automata`` and NOT ``circuit.rtl``, so it is a twin
    of the lowering, not of the emitter.  The emitted Verilog is gated
    separately (``test_acs2_3_oracle.py`` gate 1b, via
    ``tests/hw/xsim_diff.py``) — do not read a green gate-1 as evidence about
    the SR7 hardware.
    """
    # Keyed on the group's canonical bytes, never on id(): a temporary
    # sub-group can be collected and its id reused, which would silently hand
    # back another group's model — a false pass with no visible cause.
    key = ("model", group.canonical_bytes(), datapath_bytes)
    if key not in _GROUP_CACHE:
        circuit = G.group_circuit(group, datapath_bytes=datapath_bytes)
        model = GroupCircuitModel(circuit)
        model.resident = True
        _GROUP_CACHE[key] = model
    return _GROUP_CACHE[key]


def sidecar_index(group) -> Dict[int, Tuple[Tuple[int, int], ...]]:
    """``slot -> ((gid, sid), ...)`` — one slot can serve several rules."""
    return {s.index: tuple((r.gid, r.sid) for r in s.rules)
                           for s in group.slots}


# --------------------------------------------------------------------------
# The exact oracle (primary gate) — independent of the generator
# --------------------------------------------------------------------------
class Window(NamedTuple):
    slot: int
    start: int
    end: int


def anchor_occurrences(anchor: bytes, nocase: bool, data: bytes,
                       base: int = 0) -> List[Tuple[int, int]]:
    """Every (possibly overlapping) occurrence of ``anchor`` in ``data``.

    ``nocase`` folds with :meth:`bytes.lower`, which is ASCII-only — exactly
    PYRO R15's 2-byte fold set and exactly what ``slot_pattern`` lowers, with
    no Unicode fold anywhere near it (the S1 defect).
    """
    hay = data.lower() if nocase else data
    needle = anchor.lower() if nocase else anchor
    out: List[Tuple[int, int]] = []
    if not needle:
        return out
    i = hay.find(needle)
    while i >= 0:
        out.append((base + i, base + i + len(needle)))
        i = hay.find(needle, i + 1)
    return out


def slot_is_literal(slot) -> bool:
    """True when the slot's matcher is the S2 anchor-only lowering (the
    ``bytes.find`` oracle path); False for an AC-S3-2 chain/prefix/fused
    slot (the :func:`lowered_occurrences` path)."""
    import re as _re
    return (slot.pattern_eff == _re.escape(slot.anchor)
            and slot.flags_eff == (_re.IGNORECASE if slot.nocase else 0))


#: Bound on a lowered match's length for the closed-form enumerator: the
#: SR3 span cap plus slack for the enclosing group syntax.  Anything the
#: automaton could match beyond this would be a lowering-invariant bug the
#: unit suite pins separately (``tail_span <= SPAN_CAP``).
_LOWERED_SPAN_BOUND = 400


def lowered_occurrences(pattern: bytes, flags: int, data: bytes,
                        base: int = 0) -> List[Tuple[int, int]]:
    """Every ``(start, longest_end)`` of a lowered slot pattern in ``data``,
    computed with **stdlib re** — independent of the automaton and the
    model under test (the S2 oracle's independence discipline, upgraded
    from ``bytes.find`` to the chain-bearing patterns of AC-S3-2).

    Longest-end semantics match the model's leftmost-longest per start:
    for each start the enumeration tries ends from the span bound down and
    keeps the first (= longest) exact match.  ``\\A`` in a pattern binds to
    ``data[0]`` exactly as the automaton's AT_BEGINNING does.
    """
    pat = re.compile(pattern, flags)
    n = len(data)
    out: List[Tuple[int, int]] = []
    for s in range(n + 1):
        hi = min(n, s + _LOWERED_SPAN_BOUND)
        for e in range(hi, s, -1):
            if pat.fullmatch(data, s, e):
                out.append((base + s, base + e))
                break
    return out


def oracle_windows(group, data: bytes, base: int = 0) -> Set[Window]:
    """The exact expected window set for ``data`` — closed form, per slot.

    Tombstoned slots (SR6) keep their ``pattern_id`` and can never match, so
    they contribute nothing.  Anchor-only slots take the ``bytes.find``
    path (byte-identical to S2); lowered slots take the independent
    stdlib-re enumerator.
    """
    out: Set[Window] = set()
    for slot in group.slots:
        if slot.tombstone:
            continue
        if slot_is_literal(slot):
            occs = anchor_occurrences(slot.anchor, slot.nocase, data, base)
        else:
            occs = lowered_occurrences(slot.pattern_eff, slot.flags_eff,
                                       data, base)
        for s, e in occs:
            out.add(Window(slot.index, s, e))
    return out


# --------------------------------------------------------------------------
# AC-S3-2: exemplar subjects derived from a lowered pattern's parse tree
# --------------------------------------------------------------------------
def slot_exemplar(slot, maximal: bool = False) -> bytes:
    """A subject that MUST satisfy the slot's lowered pattern.

    Built by walking the stdlib parse tree of ``pattern_eff``: literals
    verbatim, gaps at their minimum (or maximum, ``maximal=True``) width
    with ``0x00`` filler, classes by their first admitted byte, branches by
    their minimal (or widest) alternative.  Independent of the automaton;
    used to drive the model↔oracle equality gates on chain slots.
    """
    try:
        import re._parser as _sre
    except ImportError:  # pragma: no cover
        import sre_parse as _sre  # type: ignore
    parsed = _sre.parse(slot.pattern_eff, slot.flags_eff)
    MAXREPEAT = _sre.MAXREPEAT

    def in_bytes(av) -> int:
        members: Set[int] = set()
        negate = False
        for op, val in av:
            name = str(op)
            if name == "NEGATE":
                negate = True
            elif name == "LITERAL":
                members.add(val)
            elif name == "RANGE":
                members.update(range(val[0], val[1] + 1))
            elif name == "CATEGORY":
                members.update(_category_bytes(str(val)))
        if negate:
            for b in range(256):
                if b not in members:
                    return b
            raise AssertionError("empty negated class")
        return min(members)

    def _category_bytes(cat: str) -> Set[int]:
        if "DIGIT" in cat and "NOT" not in cat:
            return set(b"0123456789")
        if "WORD" in cat and "NOT" not in cat:
            return set(b"abcdefghijklmnopqrstuvwxyz"
                       b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
        if "SPACE" in cat and "NOT" not in cat:
            return set(b" \t\r\n\f\v")
        if "NOT" in cat:
            # '.' is neither digit, word, nor space
            return {0x2E}
        raise AssertionError("unhandled category %s" % cat)

    def walk(seq) -> bytes:
        out = bytearray()
        for op, av in seq:
            name = str(op)
            if name == "LITERAL":
                out.append(av)
            elif name == "NOT_LITERAL":
                out.append(0x00 if av != 0x00 else 0x01)
            elif name == "ANY":
                out.append(0x00)
            elif name == "IN":
                out.append(in_bytes(av))
            elif name == "AT":
                continue
            elif name == "SUBPATTERN":
                out += walk(av[3])
            elif name == "BRANCH":
                alts = [walk(b) for b in av[1]]
                out += (max(alts, key=len) if maximal
                        else min(alts, key=len))
            elif name in ("MAX_REPEAT", "MIN_REPEAT"):
                mn, mx, sub = av
                count = int(mn)
                if maximal and mx is not MAXREPEAT:
                    count = int(mx)
                body = walk(sub)
                out += body * count
            else:
                raise AssertionError("unhandled construct %s" % name)
        return bytes(out)

    subject = walk(list(parsed))
    # NOT_ categories in _category_bytes returned '.', which IS a word
    # byte's complement only for digit/space — verify the exemplar really
    # matches, fail loud otherwise (an exemplar bug must never pass as a
    # completeness result).
    assert re.compile(slot.pattern_eff, slot.flags_eff).fullmatch(subject), \
        "exemplar does not satisfy its own pattern: %r" % slot.pattern_eff
    return subject


def model_windows(model: GroupCircuitModel, data: bytes,
                  base: int = 0) -> Set[Window]:
    """The model's window set for ``data`` (ring order is checked
    separately)."""
    entries, overflowed = model.scan(data, 0, 1 << 40)
    assert not overflowed, "out_cap unexpectedly exceeded in the oracle path"
    return {Window(m.pattern_id, base + m.start, base + m.end)
                   for m in entries}


# --------------------------------------------------------------------------
# The daemon-shaped chunking path (SR11 transport + SR12 overlap tail)
# --------------------------------------------------------------------------
class Request(NamedTuple):
    """One ``MATCH_REQUEST`` as the S3 daemon would emit it."""

    base: int        # absolute flow offset of ``buf[0]``
    buf: bytes       # overlap tail + chunk payload
    tail_len: int    # how many leading bytes are the SR12 carry-over


def chunk_requests(segments: Sequence[bytes], tail: int,
                   chunk_bytes: int = CHUNK_BYTES) -> List[Request]:
    """Chunk a flow's client→server segments the way the daemon must (SR12).

    Segments are chunked **as they arrive** — the daemon does not have the
    reassembled stream — and each request is prefixed with the previous
    ``tail`` bytes of the flow (``group.overlap_tail`` = max anchor − 1), so an
    anchor split across a chunk *or* a TCP segment boundary is still wholly
    inside exactly one request.  Every emitted buffer is a contiguous
    substring of the flow, so this can never *invent* a nomination either.
    """
    stream = b"".join(segments)
    out: List[Request] = []
    off = 0
    for seg in segments:
        pos = 0
        while pos < len(seg):
            take = min(chunk_bytes, len(seg) - pos)
            start = off + pos
            tstart = max(0, start - tail)
            out.append(Request(tstart, stream[tstart:start + take],
                               start - tstart))
            pos += take
        off += len(seg)
    return out


def nominate(model: GroupCircuitModel, segments: Sequence[bytes], tail: int,
             chunk_bytes: int = CHUNK_BYTES) -> Set[Window]:
    """Run the chunked daemon path over a flow; absolute-offset windows."""
    out: Set[Window] = set()
    for req in chunk_requests(segments, tail, chunk_bytes):
        out |= model_windows(model, req.buf, req.base)
    return out


def max_windows_per_start(group) -> Tuple[int, Tuple[int, ...]]:
    """The R41 resume bound: most slots that can accept from ONE start offset.

    Two slots report windows at the same start exactly when one anchor is a
    prefix of the other (under the R15 fold where either is ``nocase``).  This
    is the bound the ``start_off`` resume needs — see
    :func:`nominate_with_resume` — and it is a *measurable property of the
    ruleset*, so the S3 daemon can check it against ``out_cap`` at load time
    instead of discovering it as a lost nomination.  Returns
    ``(depth, slots_of_the_deepest_chain)``.

    The fold, stated exactly.  Slot *j* (anchor ``B``, ``nocase`` ``nb``) can
    accept at the same offset as slot *i* (anchor ``A``, ``nocase`` ``na``)
    when some concrete input ``P`` of length ``len(A)`` satisfies both.  ``P``
    is pinned byte-for-byte to ``A`` when ``na`` is false and free in case when
    it is true, so:

    ==========  ==========  =============================================
    ``na``      ``nb``      condition
    ==========  ==========  =============================================
    False       False       ``A.startswith(B)``
    False       True        ``A.lower().startswith(B.lower())``
    True        False       ``A.lower().startswith(B.lower())`` — ``P``'s
                            case is free, so it can be made to equal ``B``
    True        True        ``A.lower().startswith(B.lower())``
    ==========  ==========  =============================================

    i.e. **case-fold both sides whenever EITHER slot is nocase**, and compare
    raw only when neither is.  Pre-folding ``A`` by its *own* ``nocase`` and
    then testing it against a raw ``B`` (the pre-fix form) silently returns
    False in the (nocase over case-sensitive-with-uppercase) quadrant — e.g.
    ``cfusion_encrypt``/i over a case-sensitive ``CFUSION_`` — under-reporting
    the depth, so a daemon that resumed at exactly the reported bound would
    lose a nomination with nothing on the wire.  The AC-S2-2 group never enters
    that quadrant (brute-forced: true depth 2 == the pinned 2), which is
    precisely why the pinned corpus could not catch it.
    """
    live = [s for s in group.slots if not s.tombstone]
    folded = [(s.index, s.anchor, bool(s.nocase)) for s in live]
    best, chain = 0, ()
    for _idx, a, na in folded:
        hits = tuple(
            j for (j, b, nb) in folded
            if (a.lower().startswith(b.lower()) if (na or nb)
                else a.startswith(b)))
        if len(hits) > best:
            best, chain = len(hits), hits
    return best, chain


def nominate_with_resume(model: GroupCircuitModel, group,
                         segments: Sequence[bytes], out_cap: int,
                         chunk_bytes: int = CHUNK_BYTES) -> Set[Window]:
    """Nominate under a tight ``out_cap``, resuming on ``OVF`` (R41/R78.7).

    An ``OVF`` reply is "no room", not "no match" (S1 protocol note 1): the
    host MUST re-issue the request with ``start_off`` advanced or it silently
    drops nominations.  The ring keeps the earliest-*ending* entries, so every
    window with ``end < last_end`` was returned and any lost window has
    ``start >= last_end - max_anchor``; that is the natural resume point.  It
    can fail to advance (all kept entries ending within one anchor length of
    ``start_off``), so the rule is
    ``start_off' = max(start_off + 1, last_end - max_anchor)``.

    **The +1 is where completeness is bounded**: it skips any window that
    *starts* at ``start_off`` and was truncated away, which can only happen
    when more than ``out_cap`` slots accept from that one offset — i.e. when
    ``out_cap < max_windows_per_start(group)`` (2 for the AC-S2-2 group,
    against R78.7's ``out_cap`` ceiling of 61).  The bound is measured, not
    assumed, and ``test_r41_resume_bound_is_measured_and_pinned`` demonstrates
    the loss below it rather than letting it stay theoretical.

    **``start_off`` is a HOST-SIDE LABEL, not a device-side scan origin.**
    This is the one detail an S3 daemon must not get wrong.  The R78.6
    ``MATCH_REQUEST`` header has a ``start_off`` field (bytes 28..35) and
    R78.7 says the host "resumes the scan with ``start_off`` advanced", but
    the emitted ``pyro_rp`` wrapper **never reads it**: ``ST_WAIT_BUSY`` sets
    ``feed_idx <= 0`` and ``ST_FEED`` computes ``feed_addr = 40 + feed_idx``,
    so the corpus is always fed from byte 0 (``grep -n start_off
    pyro/hdl/rp_wrapper.py`` is empty, and every wire caller sends 0).
    Re-issuing the *identical* request with a bumped ``start_off`` therefore
    returns the identical truncated ring forever — livelock, or with a bounded
    retry a silent loss of every window past ``out_cap``.

    So the resume this models is the one the hardware can actually perform:
    **re-send the TRIMMED corpus** ``req.buf[start_off:]`` and add
    ``start_off`` back to the returned offsets on the host.  It is exactly
    equivalent to a device-side origin (each slot's automaton is re-seeded at
    every start, so scanning ``buf[k:]`` yields precisely the windows of
    ``buf`` with ``start >= k``, shifted), and it needs no new hardware.
    """
    tail = group.overlap_tail
    span = group.max_anchor_len
    out: Set[Window] = set()
    for req in chunk_requests(segments, tail, chunk_bytes):
        start_off = 0
        guard = 0
        while start_off <= len(req.buf):
            guard += 1
            assert guard < 20000, "resume loop did not terminate"
            # The wire request: same slot, out_cap, and header start_off=0 —
            # only the PAYLOAD is trimmed.  The model is handed exactly the
            # bytes the device would be fed.
            entries, overflowed = model.scan(req.buf[start_off:], 0, out_cap)
            base = req.base + start_off          # host-side re-labelling
            for m in entries:
                out.add(Window(m.pattern_id, base + m.start, base + m.end))
            if not overflowed:
                break
            assert entries, ("OVF with an empty ring at out_cap=%d: the host "
                             "cannot make progress" % out_cap)
            start_off = max(start_off + 1,
                            start_off + max(m.end for m in entries) - span)
    return out


def nominated_rules(group, windows) -> Set[Tuple[int, int]]:
    """``{(gid, sid)}`` for a window set, via the sidecar (SR7)."""
    index = sidecar_index(group)
    out: Set[Tuple[int, int]] = set()
    for w in windows:
        out |= set(index[w.slot])
    return out


# --------------------------------------------------------------------------
# Host-side variable table (SR13) + header predicate
# --------------------------------------------------------------------------
# Mirrors /home/gnn/opt/snort3/etc/snort/snort_defaults.lua verbatim: the SR16
# differential is only meaningful if the daemon's table and the arbiter's are
# the same table.  Values are *configuration*, never compiled into a bitstream
# (SR13) — which is why they live here and not in pyro.snort.
HTTP_PORTS: FrozenSet[int] = frozenset(int(p) for p in """
    80 81 311 383 591 593 901 1220 1414 1741 1830 2301 2381 2809 3037 3128
    3702 4343 4848 5250 6988 7000 7001 7144 7145 7510 7777 7779 8000 8008
    8014 8028 8080 8085 8088 8090 8118 8123 8180 8181 8243 8280 8300 8800
    8888 8899 9000 9060 9080 9090 9091 9443 9999 11371 34443 34444 41080
    50002 55555
""".split())

#: Every net variable resolves to ``any`` under the shipped defaults
#: (``HOME_NET = 'any'``; ``HTTP_SERVERS = HOME_NET``), so net predicates are
#: vacuously true here.  Listed explicitly so an unknown token fails loud.
ANY_NETS = frozenset({
    "any", "$HOME_NET", "$EXTERNAL_NET", "$HTTP_SERVERS", "$SQL_SERVERS",
    "$SMTP_SERVERS", "$DNS_SERVERS", "$FTP_SERVERS", "$TELNET_SERVERS",
    "$SIP_SERVERS", "$SSH_SERVERS", "$AIM_SERVERS",
})

PORT_VARS = {"$HTTP_PORTS": HTTP_PORTS}


class Flow(NamedTuple):
    proto: str
    src_ip: str
    src_port: int
    dst_ip: str
    dst_port: int


def _port_holds(token: str, port: int) -> bool:
    tok = (token or "any").strip()
    if not tok or tok.lower() == "any":
        return True
    if tok in PORT_VARS:
        return port in PORT_VARS[tok]
    if tok.startswith("$"):
        raise KeyError(
    "unknown port variable %r (SR13 table incomplete)" %
     tok)
    neg = tok.startswith("!")
    body = tok[1:] if neg else tok
    body = body.strip("[]")
    hit = False
    for part in body.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            lo, _, hi = part.partition(":")
            lo_i = int(lo) if lo else 0
            hi_i = int(hi) if hi else 65535
            hit = hit or (lo_i <= port <= hi_i)
        elif part.isdigit():
            hit = hit or (port == int(part))
    return (not hit) if neg else hit


def _net_holds(token: str) -> bool:
    tok = (token or "any").strip()
    if tok in ANY_NETS or tok.lower() == "any":
        return True
    raise KeyError("unknown net token %r (SR13 table incomplete)" % tok)


def header_holds(rule, flow: Flow) -> bool:
    """Does ``flow`` satisfy the rule's L3/L4 header predicate (SR13)?

    This is the predicate the daemon evaluates host-side *before* attributing
    a nomination; a nomination that fails it is the ``header_predicate`` SR4
    over-approximation class — the price of keying groups on a port *class*.
    """
    if rule.proto.lower() not in ("tcp", "ip", "http"):
        return False
    fwd = (
    _net_holds(
        rule.src_net) and _port_holds(
            rule.src_port,
            flow.src_port) and _net_holds(
                rule.dst_net) and _port_holds(
                    rule.dst_port,
                     flow.dst_port))
    if fwd:
        return True
    if rule.direction == "<>":
        return (_net_holds(rule.dst_net)
                and _port_holds(rule.dst_port, flow.src_port)
                and _net_holds(rule.src_net)
                and _port_holds(rule.src_port, flow.dst_port))
    return False


# --------------------------------------------------------------------------
# SR4 over-approximation classes
# --------------------------------------------------------------------------
FP_HEADER = "header_predicate"
FP_CASE_FOLD = "case_fold"
FP_ANCHOR_STRIP = "anchor_strip"
FP_DROPPED = "dropped_conjuncts"
FP_UNCLASSIFIED = "unclassified"

FP_CLASSES = (FP_HEADER, FP_CASE_FOLD, FP_ANCHOR_STRIP, FP_DROPPED,
              FP_UNCLASSIFIED)

_POSITIONAL_MODS = frozenset({"offset", "depth", "distance", "within"})


def classify_fp(rule, res, slot, flow: Flow, stream: bytes) -> str:
    """The MOST SPECIFIC SR4 class explaining one nomination Snort rejected.

    Precedence is fixed and total (the last class is a catch-all), because
    "some class applies" is a tautology on this corpus — all 256 rules carry
    a non-empty ``dropped_options`` list (``flow:`` alone guarantees it), so
    asserting ``class is not None`` would assert nothing.  The *distribution*
    is what the test pins.

    1. ``header_predicate`` — the flow does not satisfy the rule's header
       (SR13 would drop it before attribution).  Most specific: no payload
       reasoning is needed at all.
    2. ``case_fold`` — the anchor is only present under the R15 ASCII fold;
       the rule's exact-case content bytes do not occur in the stream.
    3. ``anchor_strip`` — a *position* constraint was dropped: the anchor
       lives in a normalized sticky buffer (SF11: buffer start/extent is
       unobservable in raw bytes) or carries
       ``offset``/``depth``/``distance``/``within``.
    4. ``dropped_conjuncts`` — some other conjunct of the rule (pcre,
       byte_test, flowbits, a second content, …) is not in the circuit.
    """
    if not header_holds(rule, flow):
        return FP_HEADER
    if slot.nocase and res.anchor is not None:
        exact = res.anchor.pattern
        if not anchor_occurrences(exact, False, stream):
            return FP_CASE_FOLD
    positional = False
    if res.anchor is not None:
        for ci in res.contents:
            if ci.option_index != res.anchor.option_index:
                continue
            positional = any(name in _POSITIONAL_MODS
                             for name, _ in ci.content.modifiers)
    if (res.subtier == T.SUBTIER_NORMALIZED) or positional:
        return FP_ANCHOR_STRIP
    if res.dropped_options:
        return FP_DROPPED
    return FP_UNCLASSIFIED


# --------------------------------------------------------------------------
# pcap construction (shape of tests/data/snortpf/make_s1_pcaps.py)
# --------------------------------------------------------------------------
CLI_MAC = bytes.fromhex("020000000001")
SRV_MAC = bytes.fromhex("020000000002")
CLI_IP = bytes([192, 168, 50, 10])
SRV_IP = bytes([10, 0, 0, 21])
CLI_IP_S = "192.168.50.10"
SRV_IP_S = "10.0.0.21"


def _csum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    s = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def _tcp(src, dst, sport, dport, seq, ack, flags, payload=b""):
    tcp = struct.pack("!HHIIBBHHH", sport, dport, seq, ack, (5 << 4), flags,
                      65535, 0, 0) + payload
    pseudo = src + dst + struct.pack("!BBH", 0, 6, len(tcp))
    tcp = tcp[:16] + struct.pack("!H", _csum(pseudo + tcp)) + tcp[18:]
    ip = struct.pack("!BBHHHBBH", 0x45, 0, 20 + len(tcp), 0x1234, 0, 64, 6,
                     0) + src + dst
    ip = ip[:10] + struct.pack("!H", _csum(ip)) + ip[12:]
    eth = (SRV_MAC + CLI_MAC if src ==
           CLI_IP else CLI_MAC + SRV_MAC) + b"\x08\x00"
    return eth + ip + tcp


class Case(NamedTuple):
    """One corpus case: exactly ONE established TCP session.

    The client port is the case key — Snort's ``alert_fast`` output names the
    flow, so a single Snort run over one pcap holding every case is
    attributable back to cases without any per-case process.
    """

    name: str
    sport: int
    dport: int
    segments: Tuple[bytes, ...]     # client → server payloads, in order
    note: str = ""

    @property
    def stream(self) -> bytes:
        return b"".join(self.segments)

    @property
    def flow(self) -> Flow:
        return Flow("tcp", CLI_IP_S, self.sport, SRV_IP_S, self.dport)


def case_packets(case: Case) -> List[bytes]:
    """SYN / SYN-ACK / ACK then each client segment, each server-ACKed."""
    seq_c, seq_s = 1000, 5000
    pkts = [
    _tcp(
        CLI_IP,
        SRV_IP,
        case.sport,
        case.dport,
        seq_c,
        0,
        0x02),
        _tcp(
            SRV_IP,
            CLI_IP,
            case.dport,
            case.sport,
            seq_s,
            seq_c + 1,
            0x12),
            _tcp(
                CLI_IP,
                SRV_IP,
                case.sport,
                case.dport,
                seq_c + 1,
                seq_s + 1,
                0x10),
                 ]
    seq_c += 1
    seq_s += 1
    for seg in case.segments:
        pkts.append(_tcp(CLI_IP, SRV_IP, case.sport, case.dport, seq_c, seq_s,
                         0x18, seg))
        seq_c += len(seg)
        pkts.append(_tcp(SRV_IP, CLI_IP, case.dport, case.sport, seq_s, seq_c,
                         0x10))
    return pkts


def write_pcap(path: str, cases: Sequence[Case]) -> str:
    with open(path, "wb") as fh:
        fh.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        usec = 0
        for case in cases:
            for pkt in case_packets(case):
                fh.write(struct.pack("<IIII", 1700000000, usec, len(pkt),
                                     len(pkt)))
                fh.write(pkt)
                usec += 100
    return path


# --------------------------------------------------------------------------
# The corpora (SR16 §"recorded pcaps")
# --------------------------------------------------------------------------
def _get(uri: bytes, host: bytes = b"victim.example") -> bytes:
    return (b"GET " + uri + b" HTTP/1.1\r\nHost: " + host
            + b"\r\nUser-Agent: acs23\r\nAccept: */*\r\n\r\n")


def _post(body: bytes, uri: bytes = b"/cgi-bin/handler") -> bytes:
    return (b"POST " + uri + b" HTTP/1.1\r\nHost: victim.example\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)


def build_cases() -> List[Case]:
    """The AC-S2-3 corpus: real anchors from the real group, one session each.

    Deliberately mixed so the suite cannot pass vacuously *or* trivially:
    URI anchors Snort confirms, URI anchors Snort rejects (SR4 FPs with a
    reason), raw-anchor bodies, a case-permuted anchor (R15), a
    percent-encoded anchor that ``http_inspect`` normalizes and the wire never
    carries (the SF11 blinding of the normalized-buffer sub-tier), an anchor
    split across two TCP segments (SR12), a flow on a non-``$HTTP_PORTS``
    port (SR13's header predicate), and a benign session.
    """
    cases: List[Case] = []
    port = 40000

    def add(name, segments, dport=80, note=""):
        nonlocal port
        port += 1
        cases.append(Case(name, port, dport,
                          tuple(segments), note))

    # -- normalized-buffer (http_uri) anchors, verbatim on the wire ---------
    for uri in (b"/wwwboard/passwd.txt", b"/view-source", b"/test-cgi",
                b"/php.cgi", b"/hsx.cgi", b"/yabb", b"/glimpse",
                b"/nph-test-cgi"):
        add("uri" + uri.decode().replace("/", "_"), [_get(uri + b"?a=1")])

    # -- raw-anchor (pkt_data) anchors in a POST body ----------------------
    add("raw_pccs", [_post(b"x=pccsmysqladm/incs/dbconnect.inc&y=2")])
    add("raw_cfusion_encrypt", [_post(b"q=CFUSION_ENCRYPT()&z=1")])
    add("raw_template_traversal", [_post(b"template=../../etc/passwd")])
    add("raw_cf_setdatasource",
        [_post(b"a=CF_SETDATASOURCEUSERNAME()&b=2")])

    # -- R15 ASCII fold: a nocase anchor in mixed case ---------------------
    add("case_permuted_uri", [_get(b"/TeSt-CGI?a=1")],
        note="nocase anchor '/test-cgi' in mixed case")
    add("case_permuted_raw", [_post(b"q=cFuSiOn_EnCrYpT()&z=1")],
        note="nocase raw anchor in mixed case")

    # -- SF11 blinding: %-encoded URI, normalized by http_inspect only -----
    add("percent_encoded_uri", [_get(b"/test%2dcgi?a=1")],
        note="Snort normalizes %2d -> '-'; the wire never carries the anchor")
    add("percent_encoded_uri2", [_get(b"/php%2ecgi?a=1")],
        note="Snort normalizes %2e -> '.'; the wire never carries the anchor")

    # -- SR12: anchor split across two TCP segments ------------------------
    req = _get(b"/wwwboard/passwd.txt?a=1")
    cut = req.index(b"passwd") + 3
    add("segment_split_anchor", [req[:cut], req[cut:]],
        note="anchor straddles the segment boundary — SR12 tail carries it")

    # -- SR13: the same payload on a port outside $HTTP_PORTS --------------
    add("wrong_port_ftp", [_post(b"template=../../etc/passwd")], dport=21,
        note="header predicate fails for every rule in the group")

    # -- negative control --------------------------------------------------
    add("benign", [_get(b"/index.html"), _get(b"/images/logo.png")],
        note="no anchor anywhere")
    return cases


#: Bytes that cannot be carried verbatim in an HTTP request line.
_URI_HOSTILE = frozenset(b"\r\n\x00 \t#")


def build_anchor_sweep_cases(group) -> List[Case]:
    """One session per slot, carrying that slot's real anchor (253 cases).

    This is what makes the differential non-anecdotal: every anchor in the
    group is exercised against Snort, so the completeness claim is measured
    over the whole group rather than over a handful of hand-picked rules.
    An anchor destined for an ``http_*`` buffer rides in the request URI when
    it can (that is where the rule expects it); everything else rides in a
    POST body.  Anchors that cannot be spelled in a request line (embedded
    CR/LF/NUL/space) go in the body too — Snort will not alert on those, and
    the resulting nominations are counted, classified SR4 false positives.
    """
    cases: List[Case] = []
    port = 42000
    for slot in group.slots:
        if slot.tombstone:
            continue
        port += 1
        anchor = slot.anchor
        buffer = slot.rules[0].buffer
        uri_ok = (buffer.startswith("http_") and anchor.startswith(b"/")
                  and not (set(anchor) & _URI_HOSTILE))
        if uri_ok:
            seg = _get(anchor + (b"&z=1" if b"?" in anchor else b"?z=1"))
        else:
            seg = _post(b"q=" + anchor + b"&z=1")
        cases.append(Case("slot%03d" % slot.index, port, 80, (seg,), buffer))
    return cases


def all_cases(group) -> List[Case]:
    """The full AC-S2-3 corpus: designed cases + the per-anchor sweep."""
    return build_cases() + build_anchor_sweep_cases(group)


# --------------------------------------------------------------------------
# SR12 fuzzing support: a small sub-group of REAL slots
# --------------------------------------------------------------------------
def fuzz_subgroup(group, n: int = 12):
    """A sub-group of ``n`` real slots, re-indexed 0..n-1.

    The SR12 fuzz is quadratic-ish (every split offset × every anchor), so it
    runs against a representative subset rather than all 253 slots: the
    longest anchor (which *sets* ``overlap_tail``), the shortest (the ones a
    false-positive heuristic would trip over), a ``nocase`` and a
    case-sensitive slot, then an even spread — deterministic, no sampling.
    The full group is fuzzed too, on fewer offsets (see the test module).
    """
    live = [s for s in group.slots if not s.tombstone]
    by_len = sorted(live, key=lambda s: (s.length, s.index))
    picked: List[object] = []

    def take(slot):
        if slot is not None and slot.index not in [p.index for p in picked]:
            picked.append(slot)

    take(by_len[-1])                                     # longest anchor
    take(by_len[0])                                      # shortest anchor
    take(next((s for s in live if s.nocase), None))
    take(next((s for s in live if not s.nocase), None))
    take(next((s for s in live if len(s.rules) > 1), None))   # a shared slot
    step = max(1, len(live) // max(1, n - len(picked)))
    for i in range(0, len(live), step):
        if len(picked) >= n:
            break
        take(live[i])
    picked = picked[:n]
    slots = tuple(G.GroupSlot(i, s.anchor, s.nocase, s.rules)
                  for i, s in enumerate(picked))
    return G.RuleGroup(group.port_class, 900, slots, group.group_max)


def adversarial_corpora(group, seed: int = 20260728) -> List[bytes]:
    """Deterministic property corpora aimed at the emitter, not at Snort.

    Anchors back to back (accept on the same byte from several slots), their
    own prefixes immediately before them (partial-match restarts), whole-
    alphabet noise, single-byte floods, and self-overlapping anchors — the
    shapes where a shared-harness priority encoder, a feed stall or a skid
    would show up as a wrong window set.  Seeded, so a failure reproduces.
    """
    import random as _random

    rnd = _random.Random(seed)
    live = [s for s in group.slots if not s.tombstone]
    out: List[bytes] = [b"".join(s.anchor for s in live[:40])]

    def perm(b: bytes) -> bytes:
        return bytes((c ^ 0x20) if 65 <= (c & 0xDF) <=
                     90 and rnd.random() < 0.5 else c for c in b)

    out.append(b"".join(perm(s.anchor) for s in live[:40]))
    buf = bytearray()
    for s in live[:30]:
        a = s.anchor
        buf += a[:max(1, len(a) - 1)] + a          # prefix, then the anchor
    out.append(bytes(buf))
    out.append(bytes(rnd.randrange(256) for _ in range(1200)))
    out.append(b"/" * 400)
    out.append(b"a" * 400)
    out.append(bytes(range(256)) * 3)
    for s in live:
        if s.length > 3:
            out.append(s.anchor + s.anchor[1:] + s.anchor)
            break
    for _ in range(20):
        b = bytearray()
        for _ in range(25):
            a = rnd.choice(live).anchor
            i = rnd.randrange(len(a))
            j = rnd.randrange(i + 1, len(a) + 1)
            b += a[i:j]
            if rnd.random() < 0.3:
                b += bytes([rnd.randrange(256)])
        out.append(bytes(b))
    return out


def random_segmentation(data: bytes, seed: int, max_seg: int = 80
                        ) -> Tuple[bytes, ...]:
    """Split ``data`` into pseudo-random TCP-segment-sized pieces."""
    import random as _random

    rnd = _random.Random(seed)
    segs: List[bytes] = []
    pos = 0
    while pos < len(data):
        n = rnd.randrange(1, max_seg)
        segs.append(data[pos:pos + n])
        pos += n
    return tuple(segs)


def case_permutations(anchor: bytes, limit: int = 8) -> List[bytes]:
    """Deterministic ASCII case permutations of ``anchor`` (R15 fold set)."""
    out = [anchor, anchor.upper(), anchor.lower()]
    alt = bytes((c ^ 0x20) if (65 <= (c & 0xDF) <= 90) and (i % 2 == 0) else c
                for i, c in enumerate(anchor))
    out.append(alt)
    out.append(bytes((c ^ 0x20) if (65 <= (c & 0xDF) <= 90) and (i % 2) else c
                     for i, c in enumerate(anchor)))
    # A few reproducible pseudo-random permutations (no `random` global state).
    for seed in range(1, limit):
        h = seed * 2654435761
        buf = bytearray()
        for i, c in enumerate(anchor):
            h = (h * 1103515245 + 12345) & 0xFFFFFFFF
            buf.append((c ^ 0x20) if (65 <= (c & 0xDF) <= 90) and (h >> 16) & 1
                       else c)
        out.append(bytes(buf))
    seen: List[bytes] = []
    for b in out:
        if b not in seen:
            seen.append(b)
    return seen


# --------------------------------------------------------------------------
# Snort runner (the SR16 arbiter)
# --------------------------------------------------------------------------
def write_group_rules(group, path: str) -> int:
    """Write exactly the group's rules, verbatim, one per line."""
    lines = []
    for slot in group.slots:
        for ref in slot.rules:
            rule, _res = rule_by_line(ref.line_no)
            lines.append(rule.raw)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return len(lines)


_ALERT_RE = re.compile(
    r"\[\*\*\]\s*\[(\d+):(\d+):(\d+)\].*?\{(\w+)\}\s+"
    r"([\d.]+):(\d+)\s+->\s+([\d.]+):(\d+)")


class Alert(NamedTuple):
    gid: int
    sid: int
    src_ip: str
    src_port: int
    dst_ip: str
    dst_port: int


def run_snort(rules_path: str, pcap_path: str, workdir: str) -> List[Alert]:
    """Replay ``pcap_path`` through Snort with ``rules_path``; parse alerts.

    Uses the shipped ``snort.lua`` so the arbiter's variable table is the
    stock one this module mirrors (see :data:`HTTP_PORTS`).
    """
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = SNORT_LIBS
    cmd = [SNORT_BIN, "-c", SNORT_LUA, "-R", rules_path, "-r", pcap_path,
           "-A", "alert_fast", "-q", "-l", workdir,
           "--lua", "alert_fast = { file = false }"]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                          timeout=300)
    if proc.returncode != 0:
        raise RuntimeError("snort failed (rc=%d): %s"
                           % (proc.returncode, proc.stderr[-2000:]))
    alerts: List[Alert] = []
    for line in proc.stdout.splitlines():
        m = _ALERT_RE.search(line)
        if m:
            alerts.append(Alert(int(m.group(1)), int(m.group(2)),
                                m.group(5), int(m.group(6)),
                                m.group(7), int(m.group(8))))
    return alerts


def rule_index(group) -> Dict[Tuple[int, int], Tuple[object, object]]:
    """``(gid, sid) -> (slot, RuleRef)`` for every rule in the group."""
    out: Dict[Tuple[int, int], Tuple[object, object]] = {}
    for slot in group.slots:
        for ref in slot.rules:
            out[(ref.gid, ref.sid)] = (slot, ref)
    return out


class Differential(NamedTuple):
    """The SR16 differential result — completeness diffs + counted FPs."""

    true_positives: int                       # alerted AND nominated
    # (case,gid,sid,subtier,buffer)
    misses: Tuple[Tuple[str, int, int, str, str], ...]
    fp_by_class: Dict[str, int]
    fp_rows: Tuple[Tuple[str, int, int, str], ...]       # (case,gid,sid,class)
    alerted_sids: FrozenSet[int]
    alerted_raw_anchor: int                   # alerts on raw-anchor rules
    alerted_raw_sids: FrozenSet[int]          # distinct raw-anchor sids
    cases_with_alerts: int
    nominations: Dict[str, Set[Window]]


def run_differential(group, model, cases: Sequence[Case],
                     alerts: Dict[str, Set[Tuple[int, int]]],
                     nominations: Optional[Dict[str, Set[Window]]] = None
                     ) -> Differential:
    """Compare the nomination path against Snort's verdicts, case by case.

    Completeness (SR16) is ``alerted ⊆ nominated``; every element of
    ``alerted \\ nominated`` is a completeness **miss** and is reported with
    its sub-tier and buffer so the caller can apply the absolute gate to
    ``raw-anchor`` and the pinned budget to ``normalized-buffer``.  Every
    element of ``nominated \\ alerted`` is a false positive and is classified
    into exactly one SR4 class (:func:`classify_fp`).
    """
    index = rule_index(group)
    tp = 0
    misses: List[Tuple[str, int, int, str, str]] = []
    fp_rows: List[Tuple[str, int, int, str]] = []
    fp_by_class: Dict[str, int] = {c: 0 for c in FP_CLASSES}
    alerted_sids: Set[int] = set()
    alerted_raw_sids: Set[int] = set()
    alerted_raw = 0
    cases_with_alerts = 0
    noms: Dict[str, Set[Window]] = {}
    for case in cases:
        windows = (nominations or {}).get(case.name)
        if windows is None:
            windows = nominate(model, case.segments, group.overlap_tail)
        noms[case.name] = windows
        nominated = nominated_rules(group, windows)
        alerted = alerts.get(case.name, set())
        if alerted:
            cases_with_alerts += 1
        for key in sorted(alerted):
            slot, ref = index[key]
            _rule, res = rule_by_line(ref.line_no)
            alerted_sids.add(key[1])
            if res.subtier == T.SUBTIER_RAW:
                alerted_raw += 1
                alerted_raw_sids.add(key[1])
            if key in nominated:
                tp += 1
            else:
                misses.append((case.name, key[0], key[1], res.subtier,
                               ref.buffer))
        for key in sorted(nominated - alerted):
            slot, ref = index[key]
            rule, res = rule_by_line(ref.line_no)
            cls = classify_fp(rule, res, slot, case.flow, case.stream)
            fp_by_class[cls] += 1
            fp_rows.append((case.name, key[0], key[1], cls))
    return Differential(tp, tuple(misses), fp_by_class, tuple(fp_rows),
                        frozenset(alerted_sids), alerted_raw,
                        frozenset(alerted_raw_sids), cases_with_alerts, noms)


def alerts_by_case(alerts: Sequence[Alert],
                   cases: Sequence[Case]) -> Dict[str, Set[Tuple[int, int]]]:
    """``case name -> {(gid, sid)}``, keyed on the case's client port.

    Fails loud on a server→client alert: the nomination path only offers
    client→server payloads to a ``$HTTP_PORTS`` group, so a to_client alert
    would make the differential unsound rather than merely failing.
    """
    by_port = {c.sport: c.name for c in cases}
    out: Dict[str, Set[Tuple[int, int]]] = {c.name: set() for c in cases}
    for a in alerts:
        if a.src_ip != CLI_IP_S or a.src_port not in by_port:
            raise AssertionError(
    "alert on an unexpected flow (server->client or unknown "
    "port): %r — the C2S-only nomination path cannot see it" %
     (a,))
        out[by_port[a.src_port]].add((a.gid, a.sid))
    return out
