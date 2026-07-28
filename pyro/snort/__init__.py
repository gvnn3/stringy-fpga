"""SNORT-PF Stage 1: Snort 3 rule parsing and SR1/SR2 triage.

Public surface of the ``pyro.snort`` subpackage (spec
``specs/snort-rule-offload.md`` v1.0.1, §3 Stage 1, §4.1 SR1/SR2):

- :mod:`pyro.snort.rules` — deterministic Snort 3 rule tokenizer/parser;
- :mod:`pyro.snort.triage` — tier classification + anchor selection;
- :mod:`pyro.snort.report` — machine-readable SR1 triage report with the
  SF8-SF13 self-check invariants;
- :mod:`pyro.snort.groups` — SR6 rule grouping (port class, stable sid
  order, GROUP_MAX, anchor dedup, tombstones) with the SR7 group hash /
  ``rp_child_id`` and the SR9 bitstream-cache key.

CLI: ``python -m pyro.snort.triage <rules-file> -o report.json`` (also
wrapped by ``scripts/pyro_snort.py`` per repo conventions).
"""

from __future__ import annotations

TRIAGE_VERSION = "1.0.0"  # SR1 triage implementation version

from .rules import (  # noqa: E402
    Content,
    Option,
    Pcre,
    Rule,
    RuleParseError,
    parse_rule,
    parse_rules_file,
)
from .triage import (  # noqa: E402
    Anchor,
    TriageResult,
    classify_pcre,
    triage_file,
    triage_rule,
)
from .report import build_report, check_invariants  # noqa: E402
from .groups import (  # noqa: E402
    GROUP_MAX,
    GroupSlot,
    RuleGroup,
    RuleRef,
    group_automata,
    group_circuit,
    group_hash,
    pack_groups,
    port_class,
    repack_with_tombstones,
    slot_automaton,
    slot_pattern,
)

__all__ = [
    "TRIAGE_VERSION",
    "GROUP_MAX",
    "Anchor",
    "GroupSlot",
    "RuleGroup",
    "RuleRef",
    "Content",
    "Option",
    "Pcre",
    "Rule",
    "RuleParseError",
    "TriageResult",
    "build_report",
    "check_invariants",
    "classify_pcre",
    "group_automata",
    "group_circuit",
    "group_hash",
    "pack_groups",
    "port_class",
    "repack_with_tombstones",
    "slot_automaton",
    "slot_pattern",
    "parse_rule",
    "parse_rules_file",
    "triage_file",
    "triage_rule",
]
