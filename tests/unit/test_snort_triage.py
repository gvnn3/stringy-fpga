"""Unit tests for SR1/SR2 triage: tiers, anchors, buffers, pcre classes.

Exercises the decision procedure of specs/snort-rule-offload.md §4.1 on
hand-built rules: sticky-buffer state machine (SF11), tier splits (SF8/
SF9), anchor selection (§2 Definitions), and the SF13 pcre
classification with its precedence and bounded-repeat scan.
"""

import pytest

from pyro.snort import rules as R
from pyro.snort import triage as T


def tri(line):
    return T.triage_rule(R.parse_rule(line, 1))


HDR = "alert tcp $EXTERNAL_NET any -> $HOME_NET any "


# --- tier decisions --------------------------------------------------------


def test_header_only_icmp():
    res = tri('alert icmp any any -> any any ( itype:8; icode:0; sid:1; )')
    assert res.tier == T.TIER_HEADER_ONLY
    assert res.subtier is None and res.anchor is None


@pytest.mark.parametrize("opts,why", [
    ('pcre:"/evil/"; sid:1;', "pcre-only"),
    ('byte_test:4,>,128,0; sid:1;', "byte_test-only"),
    ('content:!"not this"; sid:1;', "negated-content-only"),
    ('dsize:8; sid:1;', "dsize is a payload-length predicate"),
])
def test_always_forward_payload_dependent_no_positive_content(opts, why):
    res = tri(HDR + "( " + opts + " )")
    assert res.tier == T.TIER_ALWAYS_FORWARD, (opts, why, res.reason)


def test_one_byte_positive_content_is_an_anchor():
    # SF10: min anchor length is 1; any positive content qualifies.
    res = tri(HDR + '( content:"A"; sid:1; )')
    assert res.tier == T.TIER_ANCHOR
    assert res.anchor.pattern == b"A" and res.anchor.length == 1


def test_unparseable_rule_is_total_default_deny():
    rule = R.Rule("alert", "tcp", "a", "b", "->", "c", "d",
                  (R.Option("content", '"unterminated'),), 1, "raw")
    res = T.triage_rule(rule)
    assert res.tier == T.TIER_ALWAYS_FORWARD
    assert res.reason.startswith("parse error:")


def test_triage_file_total_on_header_level_parse_error(tmp_path):
    # A malformed line (6-field header, unterminated quote, ...) must not
    # abort the file: it becomes an always-forward row and the good rules
    # still triage (SF15 / SR1 default-deny; file-level totality).
    f = tmp_path / "t.rules"
    f.write_text(
        'alert tcp any any -> any any ( content:"ok1"; sid:1; )\n'
        'alert tcp any any -> any ( sid:2; )\n'          # 6 header fields
        'alert tcp any any -> any any ( msg:"untermin; sid:3; \n'
        'alert tcp any any -> any any ( content:"ok2"; sid:4; )\n')
    triaged = T.triage_file(str(f))
    assert len(triaged) == 4
    tiers = [res.tier for _, res in triaged]
    assert tiers[0] == T.TIER_ANCHOR and tiers[3] == T.TIER_ANCHOR
    assert tiers[1] == T.TIER_ALWAYS_FORWARD
    assert tiers[2] == T.TIER_ALWAYS_FORWARD
    assert triaged[1][1].reason.startswith("parse error:")
    assert triaged[1][0].parse_error


def test_non_byte_content_char_degrades_to_always_forward():
    # ord > 255 (non-Latin-1 unicode or a surrogateescape byte) is a
    # RuleParseError, so triage default-denies instead of crashing.
    res = tri(HDR + '( content:"€"; sid:1; )')
    assert res.tier == T.TIER_ALWAYS_FORWARD
    assert res.reason.startswith("parse error:")


# --- anchor selection (§2 Definitions) ------------------------------------


def test_declared_fast_pattern_beats_longer_content():
    res = tri(HDR + '( content:"looooooooooong"; '
                    'content:"fp",fast_pattern; sid:1; )')
    assert res.anchor.pattern == b"fp"
    assert res.anchor.declared_fast_pattern


def test_longest_positive_content_otherwise_ties_to_first():
    res = tri(HDR + '( content:"aaaa"; content:"bbbb"; content:"cc"; '
                    'sid:1; )')
    assert res.anchor.pattern == b"aaaa"  # tie -> first in rule order
    assert not res.anchor.declared_fast_pattern


def test_negated_content_never_anchor_but_positive_wins():
    res = tri(HDR + '( content:!"nononononono"; content:"yes"; sid:1; )')
    assert res.anchor.pattern == b"yes"
    assert "content" in res.dropped_options  # negated conjunct is dropped


def test_hex_decoded_length_drives_selection():
    # "|00 01 02 03 04|" is 5 decoded bytes > "abcd" (4).
    res = tri(HDR + '( content:"abcd"; content:"|00 01 02 03 04|"; sid:1; )')
    assert res.anchor.pattern == bytes(range(5))


def test_anchor_dedup_key_folds_nocase_only():
    a = T.Anchor(b"AbC", "pkt_data", False, 0, True)
    b = T.Anchor(b"AbC", "pkt_data", False, 0, False)
    assert a.dedup_key == b"abc" and b.dedup_key == b"AbC"


# --- sticky buffers and SR2 sub-tiers (SF11) -------------------------------


def test_initial_buffer_is_pkt_data_raw_subtier():
    res = tri(HDR + '( content:"raw"; sid:1; )')
    assert res.subtier == T.SUBTIER_RAW
    assert res.anchor.buffer == "pkt_data"


def test_sticky_buffer_applies_to_subsequent_contents():
    res = tri(HDR + '( http_uri; content:"/admin"; sid:1; )')
    assert res.subtier == T.SUBTIER_NORMALIZED
    assert res.anchor.buffer == "http_uri"


def test_pkt_data_resets_to_raw_mid_rule():
    res = tri(HDR + '( http_uri; content:"u"; pkt_data; '
                    'content:"rawer",fast_pattern; sid:1; )')
    assert res.anchor.buffer == "pkt_data"
    assert res.subtier == T.SUBTIER_RAW


def test_http_raw_variants_are_normalized_not_raw():
    # SF11: the raw-anchor soundness rationale holds only for
    # pkt_data/raw_data; http_raw_* still need the HTTP inspector.
    res = tri(HDR + '( http_raw_uri; content:"%2e%2e"; sid:1; )')
    assert res.subtier == T.SUBTIER_NORMALIZED


def test_dce_stub_data_is_normalized_subtier():
    # SR2: exactly two sub-tiers; dce_stub_data folds into
    # normalized-buffer (visible per-buffer in the report histograms).
    res = tri(HDR + '( dce_stub_data; content:"|05 00|"; sid:1; )')
    assert res.subtier == T.SUBTIER_NORMALIZED
    assert res.anchor.buffer == "dce_stub_data"


def test_sip_valued_key_is_match_option_not_sticky():
    # sip_method:options / sip_stat_code:4 do not move the cursor; the
    # anchor stays in pkt_data (SF11 raw 1,759, amendment 1.0.1).
    res = tri(HDR + '( sip_method:options; '
                    'content:"SIP/2.0",fast_pattern,nocase; sid:1; )')
    assert res.anchor.buffer == "pkt_data"
    assert res.subtier == T.SUBTIER_RAW
    res2 = tri(HDR + '( sip_header; content:"() {"; sid:2; )')
    assert res2.anchor.buffer == "sip_header"
    assert res2.subtier == T.SUBTIER_NORMALIZED


def test_valued_http_buffer_key_still_selects_buffer():
    # SR2 amendment 1.0.1: http_header:field <name> (sid 42886) and
    # http_param:"x" narrow the cursor WITHIN their buffer — the content
    # is evaluated in the inspector-normalized field, not pkt_data.
    res = tri('alert http ( http_header:field user-agent; '
              'content:"HttpBrowser/1.0",fast_pattern,nocase; sid:42886; )')
    assert res.anchor.buffer == "http_header"
    assert res.subtier == T.SUBTIER_NORMALIZED
    res2 = tri(HDR + '( http_param:"id",nocase; content:"x"; sid:2; )')
    assert res2.anchor.buffer == "http_param"
    assert res2.subtier == T.SUBTIER_NORMALIZED


# --- pcre classification (SF13) --------------------------------------------


@pytest.mark.parametrize("body,cls", [
    (r"(\x22|\x27)cookie\1", "backref"),
    (r"(?P<q>x)(?P=q)", "backref"),
    (r"a\g{1}b", "backref"),
    (r"(?P<q>x)\k<q>", "backref"),   # PCRE named backref \k<name>
    (r"(?P<q>x)\k{q}", "backref"),
    (r"foo(?!bar)", "lookaround"),
    (r"(?<=x)y", "lookaround"),
    (r"a*+b", "reject"),          # possessive
    (r"(?>atomic)", "reject"),    # atomic group
    (r"^GET /[a-z]{4}", "clean"),
    (r"[\1]", "clean"),           # \1 in a class is octal, not a backref
    (r"a\\1", "clean"),           # escaped backslash then literal 1
])
def test_classify_pcre_classes(body, cls):
    got, _ = T.classify_pcre(body)
    assert got == cls, (body, got)


def test_classify_pcre_precedence_backref_over_lookaround():
    # Edge-case #12: classes partition by precedence.
    got, _ = T.classify_pcre(r"(x)(?=y)\1")
    assert got == "backref"


@pytest.mark.parametrize("body,expect", [
    (r"[a-z]{300}", 300),
    (r"x{10,432}", 432),
    (r"y{1000,}", 1000),   # unbounded floor counts as its bound
    (r"z{2}", 2),
    (r"nope", None),
])
def test_bounded_repeat_scan(body, expect):
    _, max_rep = T.classify_pcre(body)
    assert max_rep == expect, body


def test_pcre_with_content_stays_anchor_tier():
    res = tri(HDR + '( content:"GET"; pcre:"/foo[0-9]{300}/R"; sid:1; )')
    assert res.tier == T.TIER_ANCHOR
    assert res.pcres[0].pcre.relative
    assert res.pcres[0].pcre_class == "clean"
    assert res.pcres[0].max_bounded_repeat == 300
