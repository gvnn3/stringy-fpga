"""Unit tests for the Snort 3 rule parser (snort-rule-offload SR1).

Covers the parser traps that shift the SF8-SF14 corpus counts if
mishandled: quote-aware option splitting, hex-run decoding, escapes,
negated content, the ``fast_pattern`` vs ``fast_pattern_offset``/
``_length`` substring trap, pcre flag parsing, and headerless Snort 3
service rules.
"""

import pytest

from pyro.snort import rules as R


# --- header / option-chain scanning ---------------------------------------


def _rule(line):
    return R.parse_rule(line, 1)


def test_basic_header_fields():
    r = _rule('alert tcp $HOME_NET 2589 -> $EXTERNAL_NET any '
              '( msg:"x"; sid:1; )')
    assert (r.action, r.proto) == ("alert", "tcp")
    assert (r.src_net, r.src_port) == ("$HOME_NET", "2589")
    assert (r.direction, r.dst_net, r.dst_port) == ("->", "$EXTERNAL_NET",
                                                    "any")
    assert not r.headerless
    assert [o.key for o in r.options] == ["msg", "sid"]


def test_bidirectional_and_port_ranges():
    r = _rule('alert udp any 19 <> any 12345:12346 ( sid:2; )')
    assert r.direction == "<>"
    assert r.dst_port == "12345:12346"  # syntactic RHS counts as dst (SF8)


def test_bracketed_lists_survive_header_split():
    r = _rule('alert tcp [1.2.3.4, 5.6.7.8] any -> any [31335,35555] '
              '( sid:3; )')
    assert r.src_net == "[1.2.3.4, 5.6.7.8]"  # kept verbatim, one field
    assert r.dst_port == "[31335,35555]"


def test_headerless_service_rule_defaults():
    r = _rule('alert http ( msg:"m"; sid:4; )')
    assert r.headerless
    assert (r.src_net, r.src_port, r.direction, r.dst_net, r.dst_port) == \
        ("any", "any", "->", "any", "any")


@pytest.mark.parametrize("bad", [
    'alert tcp any any -> any any msg:"no parens"',
    'alert tcp any any => any any ( sid:1; )',      # bad direction
    'alert tcp any any -> any ( sid:1; )',          # 6 fields
    'alert tcp any any -> any any ( msg:"unterminated; sid:1; )',
])
def test_parse_errors_raise_with_reason(bad):
    with pytest.raises(R.RuleParseError) as ei:
        _rule(bad)
    assert ei.value.reason  # boundary converts, never leaks (repo idiom)


def test_semicolon_inside_quotes_not_a_separator():
    # Edge-case #2: naive split(';') corrupts the chain.
    r = _rule('alert tcp any any -> any any '
              '( msg:"a;b\\";c"; content:"x|3B|y;z"; sid:5; )')
    assert [o.key for o in r.options] == ["msg", "content", "sid"]
    assert r.options[0].value == '"a;b\\";c"'


# --- content decoding ------------------------------------------------------


def test_decode_corpus_line_1_16_bytes():
    # Corpus line 1 (sid:105): decoded length = printables + one byte per
    # hex pair = 1 + 7 + 6 + 2 = 16 — consistent with the rule's own
    # ``depth 16`` (a depth may not be shorter than its content).
    c = R.parse_content('"2|00 00 00 06 00 00 00|Drives|24 00|",depth 16')
    assert c.pattern == b"2\x00\x00\x00\x06\x00\x00\x00Drives\x24\x00"
    assert len(c.pattern) == 16
    assert c.modifiers == (("depth", "16"),)
    assert not c.negated and not c.fast_pattern


@pytest.mark.parametrize("raw,expect", [
    ("|41 42|C", b"ABC"),
    ("|4142|", b"AB"),                 # whitespace optional
    ("|0d 0A|", b"\r\n"),              # case-insensitive hex
    ("a\\|b", b"a|b"),                 # escaped pipe is literal
    ('a\\"b', b'a"b'),
    ("a\\\\b", b"a\\b"),               # escaped backslash
    ("${", b"${"),
])
def test_decode_content_pattern(raw, expect):
    assert R.decode_content_pattern(raw) == expect


def test_decode_malformed_hex_rejected():
    with pytest.raises(R.RuleParseError):
        R.decode_content_pattern("|4G|")
    with pytest.raises(R.RuleParseError):
        R.decode_content_pattern("|414|")   # odd digit count
    with pytest.raises(R.RuleParseError):
        R.decode_content_pattern("|41")     # unterminated run


def test_negated_content():
    c = R.parse_content('!"qazwsx.hsq",nocase')
    assert c.negated
    assert c.pattern == b"qazwsx.hsq"
    assert ("nocase", None) in c.modifiers


def test_fast_pattern_substring_trap():
    # Edge-case #1: fast_pattern_offset/_length must NOT count as a bare
    # declared fast_pattern.
    bare = R.parse_content('"abc",fast_pattern')
    both = R.parse_content(
        '"abc",fast_pattern,fast_pattern_offset 0,fast_pattern_length 2')
    only_off = R.parse_content('"abc",fast_pattern_offset 4')
    assert bare.fast_pattern
    assert both.fast_pattern          # bare mod present alongside
    assert not only_off.fast_pattern  # no bare mod => not declared


def test_modifier_arg_may_be_byte_extract_variable():
    # Edge-case #6: identifiers accepted where numbers are expected.
    c = R.parse_content('"x",within var_name,distance 4')
    assert ("within", "var_name") in c.modifiers


# --- pcre parsing ----------------------------------------------------------


def test_pcre_flags_after_last_delimiter():
    p = R.parse_pcre('"/foo\\/bar[0-9]{2}/smiR"')
    assert p.pattern == "foo\\/bar[0-9]{2}"
    assert p.flags == "smiR"
    assert p.relative
    assert not p.negated
    assert p.warnings == ()


def test_pcre_negated_and_no_r():
    p = R.parse_pcre('!"/^x/i"')
    assert p.negated and not p.relative


def test_pcre_legacy_buffer_letter_warns_not_raises():
    # Edge-case #10: Snort 2 buffer letters => warning, buffer comes from
    # sticky-buffer state.
    p = R.parse_pcre('"/x/Ui"')
    assert any("legacy" in w for w in p.warnings)


# --- file-level parse ------------------------------------------------------


def test_parse_rules_file_skips_comments_joins_continuations(tmp_path):
    f = tmp_path / "t.rules"
    f.write_text(
        "# comment\n"
        "\n"
        'alert tcp any any -> any any ( msg:"one"; sid:1; )\n'
        "alert udp any any -> \\\n"
        'any any ( msg:"two"; sid:2; )\n')
    rules = R.parse_rules_file(str(f))
    assert [dict((o.key, o.value) for o in r.options)["sid"]
            for r in rules] == ["1", "2"]
    assert rules[0].line_no == 3
