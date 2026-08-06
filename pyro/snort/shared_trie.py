"""SNORT-PF Phase S4: the ROM-baked shared anchor trie (AC-S4-1).

One Aho-Corasick automaton holding every anchor-compilable rule's anchor
at once — the "instantaneous resident coverage" ceiling §5 prices — baked
into the child bitstream at synthesis (no runtime table writes,
respecting PYRO's v2.0.0 no-loadable-data ruling; the ROM child is a
*different child* from the A5 overlay engine, which keeps its loadable
slot under PYRO A5).

Case handling: FOLD EVERYTHING, in both the trie and the fabric.
--------------------------------------------------------------------
The A5 CaseSplitTable is exact but needs two automata; a single-engine
child would either miss mixed-case nocase traffic (fold-iff-nocase over
raw input — a completeness defect, the one failure SR16 forbids) or must
over-approximate.  S4 chooses the sound over-approximation: every anchor
is ASCII-folded into the trie and the engine folds each input byte the
same way, so a case-sensitive anchor matches case-blind.  That only
ENLARGES the recognized language (SR3 discipline), Snort re-verifies
every nomination (SR5), and SR4 already names the class: ``case_fold``.
SR16's case-permutation fuzz passes by construction.

The 16-byte cap (SF14) truncates each folded anchor to its 16-byte
PREFIX.  A prefix occurs wherever the full anchor occurs, so capping is
also a pure over-approximation — declared as ``anchor_cap``.

Pattern ids are FIRST-CLAIM order over the SR6-stable entry list
(sid-order), exactly the dedup discipline ``groups._build_group`` uses,
so the id space is deterministic in the ruleset and independent of file
order.  The sidecar maps each id to EVERY rule whose folded, capped
anchor landed on it.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

from ..overlay import table as _otable
from . import groups as _groups

#: SF14's cap: folded anchors are truncated to this prefix length.
CAP_BYTES = 16

#: The S4 ROM engine identity ("S4" in ASCII), distinct from the A5
#: loadable engine (0x0A5E0001) so an attribution can never confuse a
#: baked table with a loaded one.
S4_ENGINE_ID = 0x53340001

#: The ROM child's baked epoch.  Epoch 0 means "no valid table" across
#: the whole protocol (and gates the R78.13 wire path), so the constant
#: MUST be non-zero; it never increments because the table never changes
#: without a re-synthesis, which rolls the child identity instead.
ROM_EPOCH = 1


class TrieSlot(NamedTuple):
    """One trie pattern and every rule it nominates (the sidecar row)."""

    index: int                       # pattern_id
    pattern: bytes                   # folded, capped anchor bytes
    capped: bool                     # True iff truncation occurred
    rules: Tuple[_groups.RuleRef, ...]


class SharedTrie(NamedTuple):
    """The S4 build product: automaton + sidecar + provenance."""

    slots: Tuple[TrieSlot, ...]
    ac: _otable.AhoCorasick
    cap: Optional[int]
    n_rules: int

    @property
    def n_states(self) -> int:
        return self.ac.n_states

    def image(self, engine_id: int = S4_ENGINE_ID) -> bytes:
        return _otable.serialize(self.ac, engine_id=engine_id)

    def sidecar(self) -> Dict[int, Tuple[str, ...]]:
        """``pattern_id -> (gid:sid, ...)`` (spec §2 sidecar table)."""
        return {s.index: tuple(r.key for r in s.rules)
                for s in self.slots}

    def scan(self, data: bytes) -> List[Tuple[int, int]]:
        """(pattern_id, end) over FOLDED input — the reference semantics
        the ROM engine must reproduce byte for byte (the engine folds in
        fabric; the model folds here)."""
        out = []
        subject = _otable.ascii_fold(data)
        st = 0
        for i, b in enumerate(subject):
            st = self.ac.next_state(st, b)
            for pid in self.ac.out[st]:
                out.append((pid, i + 1))
        return out

    def nominations(self, data: bytes) -> set:
        """gid:sid set nominated for ``data`` — the SR16 model side."""
        side = self.sidecar()
        hits = set()
        for pid, _end in self.scan(data):
            hits.update(side[pid])
        return hits

    def manifest(self, engine_id: int = S4_ENGINE_ID) -> dict:
        """A5-shaped manifest plus the S4-specific declarations."""
        img = self.image(engine_id)
        m = _otable.manifest(
            img, self.ac, engine_id,
            rule_keys=[tuple(r.key for r in s.rules)
                       for s in self.slots])
        m["cap_bytes"] = self.cap
        m["n_rules"] = self.n_rules
        m["rom_epoch"] = ROM_EPOCH
        # SR4: the classes this lowering ADDS on top of each rule's own.
        m["oa_classes_added"] = ["case_fold"] + (
            ["anchor_cap"] if any(s.capped for s in self.slots) else [])
        m["subtier_counts"] = _subtier_counts(self.slots)
        return m


def _subtier_counts(slots: Iterable[TrieSlot]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for s in slots:
        for r in s.rules:
            k = r.subtier or "?"
            counts[k] = counts.get(k, 0) + 1
    return counts


def _fold_cap(anchor: bytes, cap: Optional[int]) -> bytes:
    """The S4 lowering of one anchor: ASCII fold, then prefix-cap.

    ``anchor`` is the entry's dedup form (already folded iff nocase);
    folding again is idempotent for those and folds the case-sensitive
    ones — the deliberate ``case_fold`` over-approximation.
    """
    folded = _otable.ascii_fold(anchor)
    return folded[:cap] if cap else folded


def build_shared(triaged, cap: Optional[int] = CAP_BYTES) -> SharedTrie:
    """Build the S4 shared trie from a triage run.

    Deterministic: entries come from ``groups.groupable_entries`` (SR6
    stable sid-order), dedup is first-claim, and the automaton build is
    itself deterministic — so the image, TABLE_ID and sidecar are pure
    functions of the ruleset, as R47a's identity discipline requires.
    """
    entries = _groups.groupable_entries(triaged)
    order: List[bytes] = []
    claims: Dict[bytes, List[_groups.GroupEntry]] = {}
    capped: Dict[bytes, bool] = {}
    for e in entries:
        key = _fold_cap(e.anchor, cap)
        if key not in claims:
            claims[key] = []
            order.append(key)
            capped[key] = bool(cap) and len(e.anchor) > cap
        claims[key].append(e)
    slots = tuple(
        TrieSlot(i, pat, capped[pat],
                 tuple(e.ref for e in claims[pat]))
        for i, pat in enumerate(order))
    ac = _otable.build([s.pattern for s in slots])
    return SharedTrie(slots, ac, cap, len(entries))


class PrecisionDelta(NamedTuple):
    """One subject's nomination counts, tier by tier (gid:sid space).

    ``trie ⊇ uncapped ⊇ anchor ⊇ lowered`` is the soundness chain —
    checked by :func:`precision_delta_shared`, never assumed.  The
    three ``extra_*`` fields decompose the S4 trie's over-nomination:
    ``extra_cap`` is what the 16-byte prefix cap adds over full folded
    anchors, ``extra_fold`` what fold-all adds over the A5 case
    discipline (folded iff nocase), ``extra_chain`` what anchor-only
    matching adds over the AC-S3-2 lowered chains (the price A5
    already paid; not S4-specific).  All of it is host re-verification
    work, never missed detections (SR3/SR5).
    """

    trie: int
    uncapped: int
    anchor: int
    lowered: int
    extra_cap: int
    extra_fold: int
    extra_chain: int
    sound: bool


def precision_prepare(trie: SharedTrie, triaged) -> dict:
    """Precompute what the tier sweep needs once per corpus: the
    sidecar and, per rule, the folded full anchor, the dedup anchor,
    and a compiled matcher for the 306 chain-bearing lowerings (the
    3,590 anchor-escape entries reuse the anchor occurrence test)."""
    import re as _re

    entries = _groups.groupable_entries(triaged)
    recs = []
    for e in entries:
        pat = e.pattern if e.pattern else _re.escape(e.anchor)
        fl = e.flags if e.flags >= 0 else (
            _re.IGNORECASE if e.nocase else 0)
        chain = (pat != _re.escape(e.anchor)
                 or fl != (_re.IGNORECASE if e.nocase else 0))
        recs.append((e.ref.key, _otable.ascii_fold(e.anchor),
                     e.anchor, e.nocase,
                     _re.compile(pat, fl) if chain else None))
    return {"side": trie.sidecar(), "recs": recs}


def precision_tiers(trie: SharedTrie, prep: dict, subject: bytes
                    ) -> Tuple[set, set, set, set]:
    """The four nomination sets for ``subject``: S4 trie, uncapped
    folded anchors, A5 anchor tier (fold iff nocase), lowered chains.
    ``bytes.lower`` is exactly the R15 2-byte fold (ASCII-only), the
    same set :func:`pyro.overlay.table.ascii_fold` implements."""
    fs = subject.lower()
    t: set = set()
    for pid, _end in trie.scan(subject):
        t.update(prep["side"][pid])
    unc: set = set()
    anc: set = set()
    low: set = set()
    for key, ffull, anchor, nocase, rx in prep["recs"]:
        if ffull in fs:
            unc.add(key)
            if (anchor in fs) if nocase else (anchor in subject):
                anc.add(key)
                if rx is None:
                    low.add(key)
        if rx is not None and rx.search(subject):
            low.add(key)
    return t, unc, anc, low


def precision_delta_shared(trie: SharedTrie, prep: dict,
                           subject: bytes) -> PrecisionDelta:
    """SF14's cap and the S4 fold, priced on ``subject`` — the
    per-group :func:`pyro.overlay.table.precision_delta` generalized
    to the shared trie's gid:sid space."""
    t, unc, anc, low = precision_tiers(trie, prep, subject)
    return PrecisionDelta(
        trie=len(t), uncapped=len(unc), anchor=len(anc),
        lowered=len(low),
        extra_cap=len(t - unc), extra_fold=len(unc - anc),
        extra_chain=len(anc - low),
        sound=(low <= anc and anc <= unc and unc <= t))


def cap_sweep(triaged,
              caps: Tuple[Optional[int], ...] = (8, 16, None)) -> dict:
    """SF14, reproduced from code rather than recited from the spec:
    trie states and image bytes at each anchor cap."""
    out = {}
    for cap in caps:
        t = build_shared(triaged, cap)
        img = t.image()
        out[cap] = {
            "n_states": t.n_states,
            "n_patterns": len(t.slots),
            "image_bytes": len(img),
            "bytes_per_state": round(len(img) / max(1, t.n_states), 1),
        }
    return out
