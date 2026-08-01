#!/usr/bin/env python3
"""Render camera-ready figures from a ``--paper`` run capture.

Separate from the collector on purpose: figures get restyled right up to a
camera-ready deadline, and none of that should ever require re-running
hardware.  Input is the ``run.json`` a ``--paper`` run writes; output is one
PDF (vector, embeddable) + PNG (for drafts) + CSV (the numbers themselves,
for the artifact appendix) per figure.

Conventions aimed at a two-column venue:
  * single-column width 3.35 in, compact heights; 8 pt text everywhere
  * serif text to sit inside a Times-set paper without shouting
  * Okabe–Ito palette: colorblind-safe and grayscale-distinguishable
  * no chartjunk: no titles (captions live in the paper), thin spines

The four figures and the claim each one carries:
  fig_switch_size   swap latency vs table size + LS fit — swap cost is
                    size-linear, so the mechanism scales with content
  fig_switch_gap    every swap in time on a log axis against the 13.6 s
                    PR baseline — the ~3-orders gap, visually
  fig_scan_cost     per-scan cycles vs bytes with 5 and 8 cyc/B guides —
                    the engine's measured cost model on silicon
  fig_nominations   cumulative nominations with swap marks + residency
                    strip — attribution continues seamlessly across swaps

Usage:
    pyro_paper_figs.py <run.json> [outdir]        # re-render any time
"""
import csv
import json
import os
import sys

# Okabe–Ito.
C = {"blue": "#0072B2", "orange": "#E69F00", "green": "#009E73",
     "red": "#D55E00", "purple": "#CC79A7", "sky": "#56B4E9",
     "yellow": "#F0E442", "black": "#000000"}
GROUP_COLORS = [C["blue"], C["orange"], C["green"], C["purple"], C["sky"]]

STYLE = {
    "font.family": "serif",
    "font.size": 8, "axes.labelsize": 8, "axes.titlesize": 8,
    "xtick.labelsize": 7, "ytick.labelsize": 7, "legend.fontsize": 7,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.linewidth": 0.6, "xtick.major.width": 0.6,
    "ytick.major.width": 0.6, "lines.linewidth": 1.0,
    "legend.frameon": False, "pdf.fonttype": 42, "ps.fonttype": 42,
    "figure.dpi": 200, "savefig.bbox": "tight", "savefig.pad_inches": 0.02,
}
W1 = 3.35   # single-column inches


def _save(fig, outdir, stem, made):
    for ext in ("pdf", "png"):
        path = os.path.join(outdir, "%s.%s" % (stem, ext))
        fig.savefig(path)
        made.append(path)


def _csv(outdir, stem, header, rows, made):
    path = os.path.join(outdir, "%s.csv" % stem)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    made.append(path)


def _group_color(names):
    return {n: GROUP_COLORS[i % len(GROUP_COLORS)]
            for i, n in enumerate(names)}


def render(run_path, outdir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update(STYLE)

    run = json.load(open(run_path))
    swaps, scans = run["swaps"], run["scans"]
    names = list(run["meta"]["groups"])
    col = _group_color(names)
    pr_s = run["meta"]["pr_baseline_s"]
    made = []
    os.makedirs(outdir, exist_ok=True)

    # ---- fig_switch_size: latency vs table size + fit -------------------
    xs = [s["bytes"] / 1024.0 for s in swaps]
    ys = [s["ms"] for s in swaps]
    _csv(outdir, "fig_switch_size", ["table_kb", "swap_ms", "group"],
         [(round(x, 3), round(y, 3), s["group"])
          for x, y, s in zip(xs, ys, swaps)], made)
    fig, ax = plt.subplots(figsize=(W1, 1.9))
    for n in names:
        pts = [(x, y) for x, y, s in zip(xs, ys, swaps) if s["group"] == n]
        if pts:
            ax.scatter(*zip(*pts), s=9, color=col[n], label=n, zorder=3)
    if len(xs) >= 2:                        # least-squares y = a + b·x
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        b = (sum((x - mx) * (y - my) for x, y in zip(xs, ys))
             / max(1e-9, sum((x - mx) ** 2 for x in xs)))
        a = my - b * mx
        xf = [min(xs), max(xs)]
        ax.plot(xf, [a + b * x for x in xf], "--", color="0.45", lw=0.8,
                zorder=2,
                label="fit: %.1f ms + %.3f ms/KB" % (a, b))
    ax.set_xlabel("table image size (KB)")
    ax.set_ylabel("swap latency (ms)")
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper left", handletextpad=0.3, borderaxespad=0.1)
    _save(fig, outdir, "fig_switch_size", made)
    plt.close(fig)

    # ---- fig_switch_gap: swaps in time, log axis, PR baseline -----------
    _csv(outdir, "fig_switch_gap", ["t_s", "swap_ms", "group"],
         [(round(s["t"], 2), round(s["ms"], 3), s["group"]) for s in swaps],
         made)
    fig, ax = plt.subplots(figsize=(W1, 1.9))
    for n in names:
        pts = [(s["t"], s["ms"]) for s in swaps if s["group"] == n]
        if pts:
            ax.scatter(*zip(*pts), s=9, color=col[n], label=n, zorder=3)
    ax.axhline(pr_s * 1e3, color=C["red"], lw=0.9, ls=":", zorder=2)
    ax.text(0.98, pr_s * 1e3 * 0.55, "JTAG partial reconfiguration "
            "(%.1f s, measured)" % pr_s, transform=ax.get_yaxis_transform(),
            ha="right", va="top", fontsize=7, color=C["red"])
    ax.set_yscale("log")
    ax.set_ylim(1, pr_s * 1e3 * 4)
    ax.set_xlabel("time (s)")
    ax.set_ylabel("switch cost (ms, log)")
    ax.legend(loc="center right", handletextpad=0.3, borderaxespad=0.1)
    _save(fig, outdir, "fig_switch_gap", made)
    plt.close(fig)

    # ---- fig_scan_cost: cycles vs bytes, cyc/B guides -------------------
    pts = [(s["bytes"], s["cycles"], s["group"]) for s in scans
           if s.get("cycles") is not None and s.get("bytes")]
    _csv(outdir, "fig_scan_cost", ["scan_bytes", "cycles", "group"], pts,
         made)
    fig, ax = plt.subplots(figsize=(W1, 1.9))
    for n in names:
        p = [(b, c) for b, c, g in pts if g == n]
        if p:
            ax.scatter(*zip(*p), s=9, color=col[n], label=n, zorder=3)
    if pts:
        bmax = max(b for b, _, _ in pts)
        for slope, ls in ((5, ":"), (8, "--")):
            ax.plot([0, bmax], [0, slope * bmax], ls, color="0.45", lw=0.8,
                    zorder=2)
            ax.text(bmax, slope * bmax, " %d cyc/B" % slope, fontsize=7,
                    color="0.3", va="center")
    ax.set_xlabel("scan length (bytes)")
    ax.set_ylabel("scan cycles")
    ax.set_xlim(left=0)
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper left", handletextpad=0.3, borderaxespad=0.1)
    _save(fig, outdir, "fig_scan_cost", made)
    plt.close(fig)

    # ---- fig_nominations: cumulative + residency strip ------------------
    ts, cum, tot = [], [], 0
    for s in scans:
        tot += s["nominations"] or 0
        ts.append(s["t"])
        cum.append(tot)
    _csv(outdir, "fig_nominations", ["t_s", "cumulative_nominations"],
         [(round(t, 2), c) for t, c in zip(ts, cum)], made)
    fig, ax = plt.subplots(figsize=(W1, 2.1))
    # residency strip along the bottom: who was resident when
    end_t = run["meta"]["duration_s"]
    strip_h = (max(cum) if cum else 1) * 0.06
    for i, s in enumerate(swaps):
        t1 = swaps[i + 1]["t"] if i + 1 < len(swaps) else end_t
        ax.axvspan(s["t"], t1, ymin=0, ymax=0.05, color=col[s["group"]],
                   lw=0)
    for s in swaps:
        ax.axvline(s["t"], color="0.75", lw=0.5, zorder=1)
    ax.plot(ts, cum, color=C["black"], lw=1.1, zorder=3)
    ax.set_xlabel("time (s)  —  vertical lines: table swaps; "
                  "strip: resident group")
    ax.set_ylabel("cumulative nominations")
    ax.set_xlim(0, end_t)
    ax.set_ylim(bottom=-strip_h * 0.2)
    handles = [plt.Line2D([], [], marker="s", ls="", color=col[n],
                          markersize=5, label=n) for n in names]
    ax.legend(handles=handles, loc="upper left", handletextpad=0.2,
              borderaxespad=0.1)
    _save(fig, outdir, "fig_nominations", made)
    plt.close(fig)

    return made


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    run_path = argv[0]
    outdir = argv[1] if len(argv) > 1 else os.path.dirname(run_path) or "."
    for p in render(run_path, outdir):
        print("wrote %s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
