"""SR1 machine-readable triage report (spec §4.1; shapes per PYRO R31).

Builds the JSON triage report SR1 mandates: per-rule ``{gid, sid, msg,
tier, subtier, anchor, dropped_options, reason}`` rows plus an aggregate
section that reproduces the corpus ground truths SF8-SF13 (tier counts,
sub-tier split, anchor statistics, buffer histogram, pcre classification,
header histograms).  Everything is a plain dict of scalars/lists — the
``explain()``/``stats()`` flat-dict idiom.

The report embeds the corpus SHA-256.  When the hash equals the profiled
snapshot this module also runs the SF8-SF13 **self-check invariants**
(SF14, trie sizing, is a Phase-S2 build artifact, not a triage
aggregate); any
failure is a triage defect (SR1 determinism / AC-S2-1 gate), *not* a
corpus change, and is surfaced in ``invariant_failures``.  For a different
(future, SF15) ruleset drop the invariants are skipped — the numbers are
facts about one snapshot, not eternal truths.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from typing import Dict, List, Optional, Tuple

from . import triage as _triage
from .triage import (
    RAW_BUFFERS, SUBTIER_NORMALIZED, SUBTIER_RAW,
    TIER_ALWAYS_FORWARD, TIER_ANCHOR, TIER_HEADER_ONLY,
)

#: SHA-256 of the profiled snapshot of ``snort3-community.rules`` whose
#: full parse produced SF8-SF13.  Invariants below are asserted only when
#: the input hashes to exactly this.
PROFILED_CORPUS_SHA256 = (
    "8c0d9172127a7270ec8084ffbd16d3995df6d27aa0ccc232638f5f0293e68e19")

REPORT_VERSION = "1.1.0"  # 1.1.0: SR2 amendment 1.0.1 counts; two sub-tiers;
                          # SF12 flow count; total at file level (SF15)


# --- Small numeric helpers -------------------------------------------------


def _percentile(sorted_vals: List[int], p: float) -> int:
    """Nearest-rank percentile over a pre-sorted list (deterministic)."""
    if not sorted_vals:
        raise ValueError("percentile of empty list")
    k = max(1, math.ceil(p / 100.0 * len(sorted_vals)))
    return sorted_vals[k - 1]


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _bucket_buffer(buf: str) -> str:
    """SF11 buffer-histogram bucket for a best-anchor buffer.

    ``http_textual`` is SF11's "HTTP-textual" row: the http_* family plus
    the sip_* anchors the corpus folds in (1,893 total after the 1.0.1
    amendment).  Any other normalized buffer (dns_query, base64_data,
    js_data, vba_data, ...) buckets to ``other_normalized`` — zero on the
    profiled corpus (the check_invariants assertion), so a future ruleset
    drop that anchors there surfaces as a new bucket instead of silently
    inflating http_textual.  ``buffer_detail`` in the report keeps the
    exact per-buffer histogram."""
    if buf in RAW_BUFFERS:
        return "raw_pkt_data"
    if buf == "dce_stub_data":
        return "dce_stub_data"
    if buf == "file_data":
        return "file_data"
    if buf.startswith("http_") or buf.startswith("sip_"):
        return "http_textual"
    return "other_normalized"


def _safe_int(value, default=None):
    """Total int conversion for metadata values (sid/gid): a malformed
    value becomes ``default``, never an exception (one bad ``sid:12abc``
    must not abort the whole report)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --- Report builder --------------------------------------------------------


def build_report(path: str, include_rules: bool = True) -> dict:
    """Parse + triage ``path`` and return the SR1 report dict."""
    triaged = _triage.triage_file(path)

    per_rule: List[dict] = []
    tier_counts: Counter = Counter()
    subtier_counts: Counter = Counter()
    header_only_proto: Counter = Counter()
    buffer_detail: Counter = Counter()
    buffer_buckets: Counter = Counter()
    anchor_lengths: List[int] = []
    unique_anchors: Dict[bytes, int] = {}  # dedup_key -> literal length
    declared_fp = 0
    zero_positive = 0
    warnings_total = 0

    proto_counts: Counter = Counter()
    dport_counts: Counter = Counter()
    var_rule_counts: Counter = Counter()
    all_vars: set = set()

    pcre_occ = 0
    pcre_rules = 0
    pcre_class: Counter = Counter()
    pcre_r_flag = 0
    clean_repeat_maxes: List[int] = []  # per clean occurrence with a repeat
    flow_established = 0  # SF12: rules matching over the reassembled stream
    parse_error_rules = 0

    for rule, res in triaged:
        # -- tiers (counted for every line, including parse errors) -----
        tier_counts[res.tier] += 1

        if rule.parse_error is not None:
            # Unparseable line (SF15 totality): always-forward row only —
            # it has no trustworthy header fields or options to histogram.
            parse_error_rules += 1
            if include_rules:
                per_rule.append({
                    "gid": None, "sid": None, "msg": "",
                    "line": rule.line_no,
                    "tier": res.tier, "subtier": None, "anchor": None,
                    "dropped_options": [],
                    "reason": res.reason,
                    "warnings": [],
                })
            continue

        # -- header histograms (SF8; dst = syntactic RHS even for "<>";
        #    headerless service rules have no port tokens to count) ------
        proto_counts[rule.proto] += 1
        if not rule.headerless:
            dport_counts[rule.dst_port] += 1
        # Variables are counted per OCCURRENCE across the four net/port
        # fields (SF8's $HOME_NET 2,556 counts one bidirectional rule
        # twice), not per rule.
        for field in (rule.src_net, rule.src_port,
                      rule.dst_net, rule.dst_port):
            for tok in field.replace("[", " ").replace("]", " ") \
                            .replace(",", " ").replace("!", " ").split():
                if tok.startswith("$"):
                    var_rule_counts[tok] += 1
                    all_vars.add(tok)
        # SF12: per-flow overlap-tail sizing depends on this count.
        if any(o.key == "flow" and o.value and "established" in o.value
               for o in rule.options):
            flow_established += 1

        if res.tier == TIER_HEADER_ONLY:
            header_only_proto[rule.proto] += 1
        if not any(not ci.content.negated for ci in res.contents):
            zero_positive += 1
        if res.subtier:
            subtier_counts[res.subtier] += 1
        if res.anchor is not None:
            a = res.anchor
            anchor_lengths.append(a.length)
            unique_anchors.setdefault(a.dedup_key, a.length)
            buffer_detail[a.buffer] += 1
            buffer_buckets[_bucket_buffer(a.buffer)] += 1
            if a.declared_fast_pattern:
                declared_fp += 1
        warnings_total += len(res.warnings)

        # -- pcre (SF13; per occurrence AND per rule) -------------------
        if res.pcres:
            pcre_rules += 1
        for pi in res.pcres:
            pcre_occ += 1
            pcre_class[pi.pcre_class] += 1
            if pi.pcre.relative:
                pcre_r_flag += 1
            if (pi.pcre_class == "clean"
                    and pi.max_bounded_repeat is not None):
                clean_repeat_maxes.append(pi.max_bounded_repeat)

        # -- per-rule row (SR1 explain()-shaped) ------------------------
        if include_rules:
            opt = {o.key: o.value for o in rule.options}
            anchor_json: Optional[dict] = None
            if res.anchor is not None:
                anchor_json = {
                    "hex": res.anchor.pattern.hex(),
                    "length": res.anchor.length,
                    "buffer": res.anchor.buffer,
                    "nocase": res.anchor.nocase,
                    "declared_fast_pattern":
                        res.anchor.declared_fast_pattern,
                }
            per_rule.append({
                "gid": _safe_int(opt.get("gid") or 1),
                "sid": _safe_int(opt.get("sid")),
                "msg": (opt.get("msg") or "").strip('"'),
                "line": rule.line_no,
                "tier": res.tier,
                "subtier": res.subtier,
                "anchor": anchor_json,
                "dropped_options": list(res.dropped_options),
                "reason": res.reason,
                "warnings": list(res.warnings),
            })

    lengths_sorted = sorted(anchor_lengths)
    anchor_stats: Optional[dict] = None
    if lengths_sorted:
        anchor_stats = {
            "count": len(lengths_sorted),
            "min": lengths_sorted[0],
            "p10": _percentile(lengths_sorted, 10),
            "p25": _percentile(lengths_sorted, 25),
            "median": _percentile(lengths_sorted, 50),
            "p75": _percentile(lengths_sorted, 75),
            "p90": _percentile(lengths_sorted, 90),
            "max": lengths_sorted[-1],
            "mean": round(sum(lengths_sorted) / len(lengths_sorted), 1),
            "under_4_bytes": sum(1 for x in lengths_sorted if x < 4),
        }

    # SF13 repeat statistics: the max bounded-repeat count per CLEAN
    # occurrence that has any bounded repeat (n=416 on the profiled
    # corpus).  The ≥256 subset (63) is what SF6's MAX_REPEAT=255 rejects;
    # the spec's "(p90 432, max 1,075)" are percentiles of THIS
    # distribution — the bound distribution across clean patterns — not of
    # the 63-element tail (whose p90 would be trivially its own max).
    repeats_sorted = sorted(clean_repeat_maxes)
    pcre_agg = {
        "occurrences": pcre_occ,
        "rules": pcre_rules,
        "clean": pcre_class.get("clean", 0),
        "backref": pcre_class.get("backref", 0),
        "lookaround": pcre_class.get("lookaround", 0),
        "reject": pcre_class.get("reject", 0),
        "r_flag_occurrences": pcre_r_flag,
        "clean_repeats_ge_256":
            sum(1 for v in repeats_sorted if v >= 256),
        "clean_repeat_bound_p90":
            _percentile(repeats_sorted, 90) if repeats_sorted else None,
        "clean_repeat_bound_max":
            repeats_sorted[-1] if repeats_sorted else None,
    }

    aggregates = {
        "tiers": {
            TIER_HEADER_ONLY: tier_counts.get(TIER_HEADER_ONLY, 0),
            TIER_ANCHOR: tier_counts.get(TIER_ANCHOR, 0),
            TIER_ALWAYS_FORWARD: tier_counts.get(TIER_ALWAYS_FORWARD, 0),
        },
        "header_only_by_proto": dict(header_only_proto),
        # SR2: exactly two sub-tier values; the 5 dce_stub_data rules are
        # inside normalized-buffer (visible in buffer_histogram/detail).
        "subtiers": {
            SUBTIER_RAW: subtier_counts.get(SUBTIER_RAW, 0),
            SUBTIER_NORMALIZED: subtier_counts.get(SUBTIER_NORMALIZED, 0),
        },
        "zero_positive_content_rules": zero_positive,
        "declared_fast_pattern_anchors": declared_fp,
        "unique_anchors": {
            "count": len(unique_anchors),
            "total_bytes": sum(unique_anchors.values()),
        },
        "anchor_length": anchor_stats,
        "buffer_histogram": dict(buffer_buckets),
        "buffer_detail": dict(sorted(buffer_detail.items())),
        "pcre": pcre_agg,
        "header": {
            "proto": dict(proto_counts.most_common()),
            "dst_port": dict(dport_counts.most_common(20)),
            "variables": dict(var_rule_counts.most_common()),
            "distinct_variables": len(all_vars),
        },
        "flow_established_rules": flow_established,  # SF12
        "parse_error_rules": parse_error_rules,      # SF15 totality
        "warnings_total": warnings_total,
    }

    sha = _sha256_file(path)
    report = {
        "report_version": REPORT_VERSION,
        "spec": "snort-rule-offload 1.0.1",
        "requirements": ["SR1", "SR2"],
        "corpus": {
            "path": path,
            "sha256": sha,
            "rule_count": len(triaged),
            "profiled_snapshot": sha == PROFILED_CORPUS_SHA256,
        },
        "aggregates": aggregates,
    }
    if include_rules:
        report["rules"] = per_rule
    if sha == PROFILED_CORPUS_SHA256:
        report["invariant_failures"] = check_invariants(aggregates)
    return report


# --- SF8-SF13 self-check invariants ---------------------------------------

#: The oracle numbers, straight from spec §1.2 (SF8-SF13) as amended by
#: 1.0.1 (SR2: sid 42886's ``http_header:field user-agent`` anchor is
#: normalized-buffer — raw 1,760→1,759, normalized 2,131+5 dce→2,137
#: with the dce fold; SF11 histogram follows).  These are facts about the
#: profiled snapshot; ``check_invariants`` compares the report against
#: every one of them.
ORACLE = {
    "tiers": {TIER_HEADER_ONLY: 95, TIER_ANCHOR: 3896,
              TIER_ALWAYS_FORWARD: 26},
    "header_only_by_proto": {"icmp": 79, "tcp": 10, "udp": 5, "ip": 1},
    "subtiers": {SUBTIER_RAW: 1759, SUBTIER_NORMALIZED: 2137},
    "zero_positive_content_rules": 121,
    "declared_fast_pattern_anchors": 2168,
    "unique_anchors": {"count": 3199, "total_bytes": 57849},
    "anchor_length": {"count": 3896, "min": 1, "p10": 4, "p25": 7,
                      "median": 12, "p75": 20, "p90": 35, "max": 214,
                      "mean": 16.5, "under_4_bytes": 213},
    "buffer_histogram": {"raw_pkt_data": 1759, "http_textual": 1893,
                         "file_data": 239, "dce_stub_data": 5},
    "pcre": {"occurrences": 1080, "rules": 1027, "clean": 799,
             "backref": 239, "lookaround": 42, "reject": 0,
             "r_flag_occurrences": 111, "clean_repeats_ge_256": 63,
             "clean_repeat_bound_p90": 432, "clean_repeat_bound_max": 1075},
    "proto": {"tcp": 3637, "udp": 211, "icmp": 125, "ip": 22,
              "http": 20, "ssl": 2},
    "dst_port": {"$HTTP_PORTS": 2001, "any": 765, "$ORACLE_PORTS": 291,
                 "25": 121, "21": 86, "111": 63, "445": 49, "139": 46,
                 "143": 34, "53": 28},
    "variables": {"$EXTERNAL_NET": 3947, "$HOME_NET": 2556,
                  "$HTTP_SERVERS": 954, "$SQL_SERVERS": 318},
    "rule_count": 4017,
    "distinct_variables": 13,
    "flow_established_rules": 3618,  # SF12
}


def check_invariants(agg: dict) -> List[str]:
    """Compare aggregates against every SF8-SF13 oracle number.

    Returns a list of human-readable failure strings (empty = gate green).
    Total function: never raises on a malformed aggregate — a missing key
    is itself a failure.
    """
    fails: List[str] = []

    def eq(label: str, got, want):
        if got != want:
            fails.append("%s: got %r, want %r" % (label, got, want))

    tiers = agg.get("tiers", {})
    for k, v in ORACLE["tiers"].items():
        eq("tiers[%s]" % k, tiers.get(k), v)
    eq("tier sum", sum(tiers.values()), ORACLE["rule_count"])
    for k, v in ORACLE["header_only_by_proto"].items():
        eq("header_only_by_proto[%s]" % k,
           agg.get("header_only_by_proto", {}).get(k), v)
    eq("header_only proto sum",
       sum(agg.get("header_only_by_proto", {}).values()),
       ORACLE["tiers"][TIER_HEADER_ONLY])
    for k, v in ORACLE["subtiers"].items():
        eq("subtiers[%s]" % k, agg.get("subtiers", {}).get(k), v)
    eq("zero_positive_content_rules",
       agg.get("zero_positive_content_rules"),
       ORACLE["zero_positive_content_rules"])
    eq("declared_fast_pattern_anchors",
       agg.get("declared_fast_pattern_anchors"),
       ORACLE["declared_fast_pattern_anchors"])
    for k, v in ORACLE["unique_anchors"].items():
        eq("unique_anchors[%s]" % k,
           agg.get("unique_anchors", {}).get(k), v)
    al = agg.get("anchor_length") or {}
    for k, v in ORACLE["anchor_length"].items():
        eq("anchor_length[%s]" % k, al.get(k), v)
    bh = agg.get("buffer_histogram", {})
    for k, v in ORACLE["buffer_histogram"].items():
        eq("buffer_histogram[%s]" % k, bh.get(k, 0), v)
    eq("buffer_histogram[other_normalized]",
       bh.get("other_normalized", 0), 0)
    for k, v in ORACLE["pcre"].items():
        eq("pcre[%s]" % k, agg.get("pcre", {}).get(k), v)
    hdr = agg.get("header", {})
    for k, v in ORACLE["proto"].items():
        eq("proto[%s]" % k, hdr.get("proto", {}).get(k), v)
    for k, v in ORACLE["dst_port"].items():
        eq("dst_port[%s]" % k, hdr.get("dst_port", {}).get(k), v)
    for k, v in ORACLE["variables"].items():
        eq("variables[%s]" % k, hdr.get("variables", {}).get(k), v)
    eq("distinct_variables", hdr.get("distinct_variables"),
       ORACLE["distinct_variables"])
    eq("flow_established_rules", agg.get("flow_established_rules"),
       ORACLE["flow_established_rules"])
    eq("parse_error_rules", agg.get("parse_error_rules"), 0)
    return fails
