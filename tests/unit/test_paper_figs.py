"""Renderer test: figures from a synthetic run, no hardware, no display.

The collection side needs a card; the rendering side must not, because
camera-ready restyling happens at deadlines when the card may be wedged,
remote, or busy.  Skips (not fails) without matplotlib — the CSVs beside
each figure are the reviewable artifact either way.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "scripts"))

matplotlib = pytest.importorskip("matplotlib")
import pyro_paper_figs  # noqa: E402


def _synth_run(tmp_path):
    groups = {"a/0": 1000, "b/0": 87000}
    swaps, scans, t, ep = [], [], 0.0, 1
    for k in range(4):
        g = list(groups)[k % 2]
        swaps.append({"t": t, "group": g, "bytes": groups[g],
                      "ms": 12 + groups[g] / 1024 * 0.16, "epoch": ep + k})
        for i in range(5):
            t += 0.5
            scans.append({"t": t, "group": g, "subject_bytes": 30,
                          "nominations": i % 3, "epoch": ep + k,
                          "replied": True, "cycles": 30 * 6, "bytes": 30})
    run = {"meta": {"generated": "t", "duration_s": t, "iface": None,
                    "pr_baseline_s": 13.6, "clock_mhz": 250,
                    "groups": {g: {"bytes": b, "rules": 1}
                               for g, b in groups.items()},
                    "engine_capacity_states": 40960},
           "swaps": swaps, "scans": scans, "final_snapshot": {}}
    p = tmp_path / "run.json"
    p.write_text(json.dumps(run))
    return str(p)


def test_render_produces_all_artifacts(tmp_path):
    made = pyro_paper_figs.render(_synth_run(tmp_path), str(tmp_path))
    names = {os.path.basename(m) for m in made}
    for stem in ("fig_switch_size", "fig_switch_gap", "fig_scan_cost",
                 "fig_nominations"):
        for ext in ("csv", "pdf", "png"):
            assert "%s.%s" % (stem, ext) in names, (stem, ext)
    # every PDF is a real PDF, every CSV has a header + rows
    for m in made:
        if m.endswith(".pdf"):
            assert open(m, "rb").read(5) == b"%PDF-"
        if m.endswith(".csv"):
            assert len(open(m).read().splitlines()) >= 2


def test_render_handles_missing_perf(tmp_path):
    """Scans whose perf read timed out (cycles=None) must not crash the
    scan-cost figure — they are simply absent from it."""
    p = _synth_run(tmp_path)
    run = json.loads(open(p).read())
    for s in run["scans"]:
        s["cycles"] = None
    open(p, "w").write(json.dumps(run))
    made = pyro_paper_figs.render(p, str(tmp_path))
    assert any(m.endswith("fig_scan_cost.pdf") for m in made)
