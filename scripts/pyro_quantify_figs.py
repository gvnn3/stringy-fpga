#!/usr/bin/env python3
"""Render figures from a pyro-quantify run capture (fpga-vs-snort).

Standalone on purpose: stdlib + matplotlib only, no pyro imports, no
hardware.  Input is the run.json that `pyro_quantify.py collect`
writes; output is one PDF (vector) + PNG (drafts) + CSV (the plotted
numbers) per figure, in the conventions of scripts/pyro_paper_figs.py.

Figures and the claim each one carries:
  fig_q_throughput  payload MB/s vs group table size — Snort raw,
                    Snort net of the decode-only baseline, FPGA
                    engine rate (R45a counters), FPGA end-to-end
  fig_q_switch      switch/ready time vs table KB — FPGA load_table
                    against Snort exec + rules-compile startup
  fig_q_cpu         host CPU-seconds per payload-GB per side
  parity_table      per (group, density, side): missing/extra/pass
                    (CSV + aligned text rendering)

A software-only run renders whatever sides exist; absent series are
skipped, never faked.

Usage:
    pyro_quantify_figs.py <run.json> [outdir]
"""
import csv
import json
import os
import sys

# Okabe–Ito.
C = {"blue": "#0072B2", "orange": "#E69F00", "green": "#009E73",
     "red": "#D55E00", "purple": "#CC79A7", "sky": "#56B4E9",
     "yellow": "#F0E442", "black": "#000000"}
GROUP_COLORS = [C["blue"], C["orange"], C["green"], C["purple"],
                C["sky"]]

# Fixed side identities (never cycled): Snort pair cool, FPGA pair
# warm/green; red stays reserved for baselines.
SIDES = {"snort": (C["blue"], "o", "snort (ac-q)"),
         "snort_net": (C["sky"], "s", "snort - decode"),
         "fpga_engine": (C["green"], "^", "fpga engine"),
         "fpga_e2e": (C["orange"], "D", "fpga end-to-end")}

STYLE = {
    "font.family": "serif",
    "font.size": 8, "axes.labelsize": 8, "axes.titlesize": 8,
    "xtick.labelsize": 7, "ytick.labelsize": 7, "legend.fontsize": 7,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.linewidth": 0.6, "xtick.major.width": 0.6,
    "ytick.major.width": 0.6, "lines.linewidth": 1.0,
    "legend.frameon": False, "pdf.fonttype": 42, "ps.fonttype": 42,
    "figure.dpi": 200, "savefig.bbox": "tight",
    "savefig.pad_inches": 0.02,
}
W1 = 3.35   # single-column inches
F_CLK = 250e6   # A5 engine clock; engine rate = bytes * F_CLK / cyc


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


def _median(vals):
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return None
    if n % 2:
        return s[n // 2]
    return 0.5 * (s[n // 2 - 1] + s[n // 2])


def _table_kb(cell):
    tb = (cell.get("fpga") or {}).get("table_bytes") \
        or cell.get("table_bytes")
    return tb / 1024.0 if tb else None


# ------------------------------------------------- per-cell series

def _snort_mbps(cell):
    """Payload MB/s per repeat from Snort's own run time."""
    out = []
    for r in cell.get("snort") or []:
        run_s = r.get("run_s") or 0.0
        pb = r.get("payload_bytes") or 0
        if run_s > 0 and pb > 0:
            out.append(pb / 1e6 / run_s)
    return out


def _snort_net_mbps(cell):
    """MB/s per repeat over run time net of the decode baseline."""
    base = [b.get("run_s") or 0.0
            for b in cell.get("snort_baseline") or []]
    base = [b for b in base if b > 0]
    if not base:
        return []
    b0 = _median(base)
    out = []
    for r in cell.get("snort") or []:
        net = (r.get("run_s") or 0.0) - b0
        pb = r.get("payload_bytes") or 0
        if net > 0 and pb > 0:
            out.append(pb / 1e6 / net)
    return out


def _fpga_rates(cell):
    """(engine MB/s from counters, end-to-end MB/s from wall)."""
    f = cell.get("fpga")
    if not f:
        return None, None
    cb = cc = 0
    wb, wall = 0, 0.0
    for s in f.get("scans") or []:
        by = s.get("bytes") or 0
        cy = s.get("cycles")
        if by and cy:
            cb += by
            cc += cy
        w = s.get("wall_s")
        if by and w:
            wb += by
            wall += w
    eng = cb * F_CLK / cc / 1e6 if cc else None
    e2e = wb / 1e6 / wall if wall > 0 else None
    return eng, e2e


def _snort_startup(cell):
    return [r["startup_s"] for r in cell.get("snort") or []
            if r.get("startup_s")]


def _snort_cpu_per_gb(cell):
    out = []
    for r in cell.get("snort") or []:
        pb = r.get("payload_bytes") or 0
        if pb > 0:
            cpu = (r.get("utime_s") or 0.0) + (r.get("stime_s")
                                               or 0.0)
            out.append(cpu / (pb / 1e9))
    return out


def _fpga_cpu_per_gb(cell):
    """Host CPU of the feeding driver, if the bench recorded it."""
    f = cell.get("fpga") or {}
    if "utime_s" not in f and "stime_s" not in f:
        return None
    pb = cell.get("payload_bytes") or 0
    if pb <= 0:
        return None
    cpu = (f.get("utime_s") or 0.0) + (f.get("stime_s") or 0.0)
    return cpu / (pb / 1e9)


def _mmm(vals):
    """(median, min, max) or (None, None, None)."""
    if not vals:
        return None, None, None
    return _median(vals), min(vals), max(vals)


def _fmt(v, nd=3):
    return "" if v is None else round(v, nd)


# ----------------------------------------------------------- render

def render(run_path, outdir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update(STYLE)

    run = json.load(open(run_path))
    cells = run["cells"]
    made = []
    os.makedirs(outdir, exist_ok=True)

    def whiskered(ax, pts, key):
        """pts: [(x, med, lo, hi)]; styled per SIDES[key]."""
        if not pts:
            return False
        color, marker, label = SIDES[key]
        xs, med, lo, hi = zip(*pts)
        ax.errorbar(xs, med,
                    yerr=[[m - a for m, a in zip(med, lo)],
                          [b - m for m, b in zip(med, hi)]],
                    fmt=marker, ms=3, color=color, lw=0,
                    elinewidth=0.6, capsize=1.5, label=label,
                    zorder=3)
        return True

    def dots(ax, pts, key):
        """pts: [(x, y)]; styled per SIDES[key]."""
        if not pts:
            return False
        color, marker, label = SIDES[key]
        ax.scatter(*zip(*pts), s=9, color=color, marker=marker,
                   label=label, zorder=3)
        return True

    # ---- fig_q_throughput: payload MB/s vs table size ---------------
    rows = []
    p_sn, p_net, p_eng, p_e2e = [], [], [], []
    for cell in cells:
        kb = _table_kb(cell)
        sn = _mmm(_snort_mbps(cell))
        net = _mmm(_snort_net_mbps(cell))
        eng, e2e = _fpga_rates(cell)
        rows.append([cell["group"], "%.2f" % cell["density"],
                     _fmt(kb), _fmt(sn[0]), _fmt(sn[1]), _fmt(sn[2]),
                     _fmt(net[0]), _fmt(net[1]), _fmt(net[2]),
                     _fmt(eng), _fmt(e2e)])
        if kb is None:
            continue
        if sn[0] is not None:
            p_sn.append((kb,) + sn)
        if net[0] is not None:
            p_net.append((kb,) + net)
        if eng is not None:
            p_eng.append((kb, eng))
        if e2e is not None:
            p_e2e.append((kb, e2e))
    _csv(outdir, "fig_q_throughput",
         ["group", "density", "table_kb", "snort_mbps_med",
          "snort_mbps_min", "snort_mbps_max", "snort_net_mbps_med",
          "snort_net_mbps_min", "snort_net_mbps_max",
          "fpga_engine_mbps", "fpga_e2e_mbps"], rows, made)
    fig, ax = plt.subplots(figsize=(W1, 1.9))
    any_series = whiskered(ax, p_sn, "snort")
    any_series |= whiskered(ax, p_net, "snort_net")
    any_series |= dots(ax, p_eng, "fpga_engine")
    any_series |= dots(ax, p_e2e, "fpga_e2e")
    ax.set_xlabel("table image size (KB)")
    ax.set_ylabel("payload MB/s")
    ax.set_xlim(left=0)
    ax.set_ylim(bottom=0)
    if any_series:
        ax.legend(loc="upper right", handletextpad=0.3,
                  borderaxespad=0.1)
    _save(fig, outdir, "fig_q_throughput", made)
    plt.close(fig)

    # ---- fig_q_switch: switch/ready time vs table KB ----------------
    rows = []
    p_load, p_start = [], []
    for cell in cells:
        kb = _table_kb(cell)
        load = (cell.get("fpga") or {}).get("load_s")
        st = _mmm(_snort_startup(cell))
        rows.append([cell["group"], "%.2f" % cell["density"],
                     _fmt(kb), _fmt(load, 6), _fmt(st[0], 6),
                     _fmt(st[1], 6), _fmt(st[2], 6)])
        if kb is None:
            continue
        if load is not None:
            p_load.append((kb, load))
        if st[0] is not None:
            p_start.append((kb,) + st)
    _csv(outdir, "fig_q_switch",
         ["group", "density", "table_kb", "fpga_load_s",
          "snort_startup_med_s", "snort_startup_min_s",
          "snort_startup_max_s"], rows, made)
    fig, ax = plt.subplots(figsize=(W1, 1.9))
    color, marker, _label = SIDES["fpga_engine"]
    have = []
    if p_load:
        ax.scatter(*zip(*p_load), s=9, color=C["green"], marker="^",
                   label="fpga load_table", zorder=3)
        have += [y for _x, y in p_load]
    if p_start:
        xs, med, lo, hi = zip(*p_start)
        ax.errorbar(xs, med,
                    yerr=[[m - a for m, a in zip(med, lo)],
                          [b - m for m, b in zip(med, hi)]],
                    fmt="o", ms=3, color=C["blue"], lw=0,
                    elinewidth=0.6, capsize=1.5,
                    label="snort startup", zorder=3)
        have += list(med)
    # Log y only when the two mechanisms are decades apart.
    if have and min(have) > 0 and max(have) / min(have) > 50:
        ax.set_yscale("log")
        ax.set_ylim(min(have) * 0.5, max(have) * 4)
        ax.set_ylabel("switch/ready time (s, log)")
    else:
        ax.set_ylim(bottom=0)
        ax.set_ylabel("switch/ready time (s)")
    ax.set_xlabel("table image size (KB)")
    ax.set_xlim(left=0)
    if have:
        ax.legend(loc="center right", handletextpad=0.3,
                  borderaxespad=0.1)
    _save(fig, outdir, "fig_q_switch", made)
    plt.close(fig)

    # ---- fig_q_cpu: CPU-seconds per payload-GB ----------------------
    rows = []
    p_scpu, p_fcpu = [], []
    for cell in cells:
        kb = _table_kb(cell)
        sc = _mmm(_snort_cpu_per_gb(cell))
        fc = _fpga_cpu_per_gb(cell)
        rows.append([cell["group"], "%.2f" % cell["density"],
                     _fmt(kb), _fmt(sc[0]), _fmt(sc[1]),
                     _fmt(sc[2]), _fmt(fc)])
        if kb is None:
            continue
        if sc[0] is not None:
            p_scpu.append((kb,) + sc)
        if fc is not None:
            p_fcpu.append((kb, fc))
    _csv(outdir, "fig_q_cpu",
         ["group", "density", "table_kb", "snort_cpu_s_per_gb_med",
          "snort_cpu_s_per_gb_min", "snort_cpu_s_per_gb_max",
          "fpga_cpu_s_per_gb"], rows, made)
    fig, ax = plt.subplots(figsize=(W1, 1.9))
    any_series = False
    if p_scpu:
        xs, med, lo, hi = zip(*p_scpu)
        ax.errorbar(xs, med,
                    yerr=[[m - a for m, a in zip(med, lo)],
                          [b - m for m, b in zip(med, hi)]],
                    fmt="o", ms=3, color=C["blue"], lw=0,
                    elinewidth=0.6, capsize=1.5,
                    label="snort (utime+stime)", zorder=3)
        any_series = True
    if p_fcpu:
        ax.scatter(*zip(*p_fcpu), s=9, color=C["orange"], marker="D",
                   label="fpga feed driver", zorder=3)
        any_series = True
    ax.set_xlabel("table image size (KB)")
    ax.set_ylabel("CPU-seconds per payload-GB")
    ax.set_xlim(left=0)
    ax.set_ylim(bottom=0)
    if any_series:
        ax.legend(loc="upper right", handletextpad=0.3,
                  borderaxespad=0.1)
    _save(fig, outdir, "fig_q_cpu", made)
    plt.close(fig)

    # ---- parity_table: CSV + aligned text rendering -----------------
    header = ["group", "density", "side", "packets", "oracle_hits",
              "missing", "extra", "pass"]
    rows = []
    for cell in cells:
        for p in cell.get("parity") or []:
            miss = p.get("missing_count",
                         len(p.get("missing") or []))
            extra = p.get("extra_count", len(p.get("extra") or []))
            rows.append([cell["group"], "%.2f" % cell["density"],
                         p.get("side", "?"), p.get("packets", ""),
                         p.get("oracle_hits", ""), miss, extra,
                         "PASS" if p.get("pass") else "FAIL"])
    _csv(outdir, "parity_table", header, rows, made)
    path = os.path.join(outdir, "parity_table.txt")
    cols = [header] + [[str(v) for v in r] for r in rows]
    widths = [max(len(r[i]) for r in cols)
              for i in range(len(header))]
    with open(path, "w") as f:
        for j, r in enumerate(cols):
            f.write("  ".join(v.ljust(w)
                              for v, w in zip(r, widths)).rstrip()
                    + "\n")
            if j == 0:
                f.write("  ".join("-" * w for w in widths) + "\n")
    made.append(path)

    return made


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    run_path = argv[0]
    outdir = argv[1] if len(argv) > 1 else os.path.dirname(run_path) \
        or "."
    for p in render(run_path, outdir):
        print("wrote %s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
