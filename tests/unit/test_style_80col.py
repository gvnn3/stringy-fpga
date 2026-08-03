"""The 80-column rule (FreeBSD style(9)), enforced as a ratchet.

Standing order (2026-08-03): all code and documentation wrap at 80
columns.  The tree carries legacy debt predating the order, so this is
a RATCHET, not a gate: every file may have at most as many over-80
lines as the checked-in baseline records, and a file that improves
should have its baseline lowered (delete its entry once it reaches 0).
New files get no allowance at all.

Regenerate the baseline ONLY when intentionally lowering it:
    python3 - <<'PY'  # see tests/unit/style80_baseline.json
PY
Raising a number in the baseline is never the fix; wrap the line.
"""
import json
import os
import subprocess

import pytest

REPO = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASELINE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "style80_baseline.json")
EXCLUDE = ("third_party/", "build/", "amd-case-filing/")
SUFFIXES = (".py", ".v", ".sv", ".md", ".tcl", ".sh")


def _tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=REPO,
                         capture_output=True, text=True).stdout
    return [f for f in out.splitlines()
            if f.endswith(SUFFIXES) and not f.startswith(EXCLUDE)]


def test_eighty_columns_ratchet():
    allowed = json.load(open(BASELINE))
    over = {}
    for f in _tracked_files():
        path = os.path.join(REPO, f)
        try:
            with open(path, errors="replace") as fh:
                n = sum(1 for l in fh if len(l.rstrip("\n")) > 80)
        except OSError:
            continue
        if n > allowed.get(f, 0):
            over[f] = (n, allowed.get(f, 0))
    assert not over, (
        "80-column regressions (got > allowed): %s — wrap the lines; "
        "never raise the baseline" % over)
