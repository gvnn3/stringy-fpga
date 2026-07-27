#!/usr/bin/env python3
"""SNORT-PF Stage 1 CLI — SR1/SR2 rule triage (specs/snort-rule-offload.md).

Subcommands:

    triage <rules-file> [-o report.json] [--no-rules]
        Parse a Snort 3 rules file, classify every rule into its SR1 tier
        (header-only / anchor-compilable / always-forward) with the SR2
        sub-tier and §2 anchor selection, and emit the machine-readable
        triage report.  On the profiled community-ruleset snapshot the
        SF8-SF13 self-check invariants are asserted; any failure exits 1.

Equivalent module form:  python3 -m pyro.snort.triage <rules-file> -o out.json

Example:
    .venv-pyro/bin/python3 scripts/pyro_snort.py triage \\
        third_party/snort3-community-rules/snort3-community.rules \\
        -o triage-report.json
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyro.snort import triage as _triage  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_tri = sub.add_parser("triage", help="SR1/SR2 triage of a rules file")
    p_tri.add_argument("rules_file")
    p_tri.add_argument("-o", "--output", default=None)
    p_tri.add_argument("--no-rules", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "triage":
        fwd = [args.rules_file]
        if args.output:
            fwd += ["-o", args.output]
        if args.no_rules:
            fwd += ["--no-rules"]
        return _triage.main(fwd)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
