#!/usr/bin/env python3
"""Experiment CLI for the fpga-vs-snort quantify study.

Drives docs/studies/fpga-vs-snort.md end to end over the cell grid
(group x density).  Working outputs live under build/quantify/ (or
--root), one directory per group slug, one subdirectory per density:

    build/quantify/<slug>/               group-level artifacts:
        patterns.json                    sorted unique anchor list
        <stem>.rules, <stem>.conf        Snort 2 lowering (snort_lower)
        table.bin, table.json            A5 image (emitter, no device)
    build/quantify/<slug>/<d.dd>/        one cell:
        corpus.pcap, payloads.bin, manifest.json      (gen)
        snort-rep<N>.json, snort-baseline-rep<N>.json (snort)
        fpga result JSON                              (fpga, operator)
        parity-<side>.json                            (parity)
    build/quantify/run.json              aggregate (collect)

Subcommands:
    gen       generate corpora + rules + table images (no hardware)
    snort     run the local Snort 2.9 bench + decode-only baseline
    fpga      OPERATOR ONLY: drive the real device; fails closed
              unless PYRO_DEVICE_IFACE is set explicitly
    parity    referee every cell against the oracle, per side present
    collect   aggregate all cell JSONs into run.json
    figs      render figures from run.json (pyro_quantify_figs)
    software  gen + snort + parity + collect + figs (no hardware)

Seed schedule: cell seed = --seed base + crc32("<group>|<d.dd>"), so
every cell is reproducible independently of the grid it ran in.

The pyro.quantify.* modules are imported inside each handler so a
missing module never breaks unrelated subcommands.
"""

import argparse
import glob
import json
import os
import platform
import subprocess
import sys
import time
import zlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

DEFAULT_ROOT = os.path.join(REPO, "build", "quantify")
DEFAULT_RULES = os.path.join(REPO, "third_party",
                             "snort3-community-rules",
                             "snort3-community.rules")
PAPER_GROUPS = ["$SIP_PORTS/0", "$FTP_PORTS/0", "$ORACLE_PORTS/1",
                "literal/3", "any/0"]
DENSITIES = (0.0, 0.01, 0.10)
SEED_BASE = 20260804
ENGINE_ID = 0x0A5E0001          # A5 overlay engine identity
RUN_SCHEMA = "pyro-quantify-run/1"
FPGA_SCHEMA = "pyro-quantify-fpga/1"
SID_BASE = 1000000


def slug(name):
    """Group name -> directory slug: "$SIP_PORTS/0" -> SIP_PORTS-0."""
    return name.replace("$", "").replace("/", "-")


def density_dir(density):
    return "%.2f" % density


def cell_seed(base, group_name, density):
    """Stable per-cell seed: base + crc32 of the cell name."""
    key = "%s|%s" % (group_name, density_dir(density))
    return base + zlib.crc32(key.encode())


def _read_json(path):
    with open(path) as f:
        return json.load(f)


def _write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, sort_keys=True, indent=1)
        f.write("\n")


def discover_cells(root):
    """Every <root>/<slug>/<density>/ dir holding a manifest.json."""
    cells = []
    if not os.path.isdir(root):
        return cells
    for g in sorted(os.listdir(root)):
        gdir = os.path.join(root, g)
        if not os.path.isdir(gdir):
            continue
        for d in sorted(os.listdir(gdir)):
            cell = os.path.join(gdir, d)
            if os.path.isfile(os.path.join(cell, "manifest.json")):
                cells.append(cell)
    return cells


_GROUPS = None


def _load_groups(rules_path):
    """name -> RuleGroup for the corpus; triage runs once, cached."""
    global _GROUPS
    if _GROUPS is None:
        from pyro.snort import groups as G
        from pyro.snort import triage as Tr
        _GROUPS = {g.name: g
                   for g in G.pack_groups(Tr.triage_file(rules_path))}
    return _GROUPS


def _find_fpga_result(cell):
    """Locate the cell's fpga result JSON by schema, name-agnostic."""
    for path in sorted(glob.glob(os.path.join(cell, "*.json"))):
        try:
            d = _read_json(path)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("schema") == FPGA_SCHEMA:
            return path, d
    return None, None


# ---------------------------------------------------------------- gen

def cmd_gen(args):
    from pyro.overlay import table as otable
    from pyro.quantify import snort_lower
    from pyro.quantify import traffic

    gs = _load_groups(args.rules)
    for n in args.groups:
        if n not in gs:
            print("warning: group %r not in corpus; skipped" % n,
                  file=sys.stderr)
    names = [n for n in args.groups if n in gs]
    if not names:
        print("error: none of the requested groups exist",
              file=sys.stderr)
        return 1

    for name in names:
        g = gs[name]
        gdir = os.path.join(args.root, slug(name))
        os.makedirs(gdir, exist_ok=True)

        # Slot-ordered anchors feed the emitter (device pattern-id
        # space); the sorted unique list is the shared study index.
        anchors = [None if s.tombstone else s.anchor
                   for s in g.slots]
        live = [a for a in anchors if a]
        patterns = traffic.unique_patterns(live)
        _write_json(os.path.join(gdir, "patterns.json"),
                    {"schema": "pyro-quantify-patterns/1",
                     "group": name,
                     "patterns": [p.hex() for p in patterns]})
        rules_path, conf_path = snort_lower.write_rules(gdir,
                                                        patterns,
                                                        name)
        # E4 requires every matching sid to alert, but two default
        # queues clip multi-match packets in this build (verified
        # empirically): ac-q's own match queue (max_queue_events,
        # default 5) and the event queue (max_queue 8 log 3).  Both
        # directives below are accepted by the local 2.9.20 build;
        # without them dense cells drop alerts and parity fails
        # spuriously.  Rewrite the conf with the limits raised.
        with open(conf_path, "w") as f:
            f.write("config detection: search-method ac-q "
                    "max_queue_events 4096\n"
                    "config event_queue: max_queue 4096 log 4096 "
                    "order_events content_length\n"
                    "include %s\n" % os.path.abspath(rules_path))

        # A5 image via the S2 group emitter: pure software, so the
        # table size exists even for software-only runs; table.bin is
        # what the operator's fpga subcommand loads.
        ac = otable.build(anchors)
        image = otable.serialize(ac, engine_id=ENGINE_ID)
        with open(os.path.join(gdir, "table.bin"), "wb") as f:
            f.write(image)
        _write_json(os.path.join(gdir, "table.json"),
                    {"schema": "pyro-quantify-table/1",
                     "group": name, "engine_id": ENGINE_ID,
                     "table_bytes": len(image),
                     "n_states": ac.n_states})
        print("gen %-24s table=%dB states=%d patterns=%d rules=%s"
              % (name, len(image), ac.n_states, len(patterns),
                 os.path.relpath(rules_path, REPO)))

        for d in args.densities:
            cell = os.path.join(gdir, density_dir(d))
            seed = cell_seed(args.seed, name, d)
            m = traffic.generate(cell, live, seed=seed, density=d,
                                 count=args.count, group_name=name)
            print("gen %-24s d=%.2f seed=%d packets=%d bytes=%d"
                  % (name, d, seed, m["count"], m["payload_bytes"]))
    return 0


# -------------------------------------------------------------- snort

def cmd_snort(args):
    from pyro.quantify import snort_bench
    from pyro.quantify import snort_lower

    if not snort_bench.snort_available():
        print("error: local snort tree not found under "
              "build/snort-local/root", file=sys.stderr)
        return 1
    cells = discover_cells(args.root)
    if not cells:
        print("error: no generated cells under %s (run gen first)"
              % args.root, file=sys.stderr)
        return 1

    for cell in cells:
        manifest = _read_json(os.path.join(cell, "manifest.json"))
        gdir = os.path.dirname(cell)
        stem = snort_lower.sanitize_name(manifest["group"])
        rules = os.path.join(gdir, stem + ".rules")
        conf = os.path.join(gdir, stem + ".conf")
        if not os.path.isfile(conf):
            print("error: %s missing (run gen first)" % conf,
                  file=sys.stderr)
            return 1
        res = snort_bench.run_bench(cell, rules, conf, cell,
                                    repeats=args.repeats)
        base = snort_bench.run_bench(cell, rules, conf, cell,
                                     repeats=args.repeats,
                                     baseline=True)
        med = sorted(r["run_s"] for r in res)[len(res) // 2]
        medb = sorted(r["run_s"] for r in base)[len(base) // 2]
        print("snort %-24s d=%.2f run_s(med)=%.3f base=%.3f "
              "alerts=%d" % (manifest["group"], manifest["density"],
                             med, medb, len(res[0]["alerts"])))
    return 0


# --------------------------------------------------------------- fpga

def _match_on_device(cfg, subject, slot=1, seq=900, timeout_s=2.0):
    """One MATCH round-trip; the validated demo helper, verbatim.

    Returns a pyro.telemetry.MatchReplySummary or None on timeout
    (request loss).  Device-facing but transport-injectable: a
    DeviceConfig with transport_factory runs this hardware-free.
    """
    import struct

    import pyro.device as pdev
    import pyro.telemetry as T

    transport = pdev._make_transport(cfg)
    try:
        eth = (bytes(cfg.dst_mac) + bytes(cfg.src_mac)
               + struct.pack(">H", pdev.ETHERTYPE))
        payload = struct.pack(">IIHH", 0, 0, 61, 0) + subject
        transport.send(eth + pdev.encode_frame(
            pdev.KIND_MATCH_REQUEST, slot, seq, payload,
            max_payload=cfg.max_payload))
        deadline = time.monotonic() + timeout_s
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                return None
            reply = transport.recv(remain)
            if reply is None:
                return None
            try:
                dec = pdev.decode_frame(reply[14:],
                                        max_payload=cfg.max_payload)
            except Exception:
                continue
            if (dec.seq != seq
                    or dec.kind != pdev.KIND_MATCH_REPLY):
                continue
            count, status = struct.unpack(">HH", dec.payload[0:4])
            epoch = struct.unpack(">I", dec.payload[4:8])[0]
            entries = []
            for i in range(count):
                # R47 entries LITTLE-endian inside the BE header.
                s, e, pid, _f = struct.unpack(
                    "<QQII",
                    dec.payload[8 + i * 24:8 + i * 24 + 24])
                entries.append((pid, s, e))
            return T.MatchReplySummary(count, bool(status & 1),
                                       epoch, tuple(entries))
    finally:
        transport.close()


class _BoundDevice:
    """pyro.device ops with the DeviceConfig bound.

    The duck type pyro.quantify.fpga_bench.bench_cell expects; never
    opens a transport until an operation runs.
    """

    def __init__(self, pdev, cfg, slot=1):
        self._pdev = pdev
        self._cfg = cfg
        self._slot = slot
        self._seq = 900

    def read_table_status(self):
        return self._pdev.read_table_status(self._cfg,
                                            slot=self._slot)

    def load_table(self, image):
        return self._pdev.load_table(self._cfg, image,
                                     slot=self._slot)

    def scan(self, subject):
        self._seq = (self._seq % 0xFFFF) + 1
        return _match_on_device(self._cfg, subject, slot=self._slot,
                                seq=self._seq)

    def read_perf_counters(self):
        return self._pdev.read_perf_counters(self._cfg,
                                             slot=self._slot)


def cmd_fpga(args):
    """OPERATOR ONLY: drives the real device over the A5 transport."""
    iface = os.environ.get("PYRO_DEVICE_IFACE")
    if not iface:
        print("error: PYRO_DEVICE_IFACE is not set.  This subcommand "
              "touches the real device and fails closed rather than "
              "guess an interface; set PYRO_DEVICE_IFACE explicitly "
              "to run the hardware side.", file=sys.stderr)
        return 2

    import pyro.device as pdev
    from pyro.quantify import fpga_bench

    cfg = pdev.DeviceConfig(
        iface=iface, chardev=None,
        max_payload=(pdev.MAX_PAYLOAD_JUMBO if args.jumbo
                     else pdev.MAX_PAYLOAD),
        probe_timeout_s=2.0)
    ok, reason = pdev.probe_device(cfg)
    if not ok:
        print("error: device probe failed: %s" % reason,
              file=sys.stderr)
        return 1
    dev = _BoundDevice(pdev, cfg)
    st = dev.read_table_status()
    capacity = st.capacity_states if st is not None else None

    cells = discover_cells(args.root)
    if not cells:
        print("error: no generated cells under %s (run gen first)"
              % args.root, file=sys.stderr)
        return 1

    gs = _load_groups(args.rules)
    tables = {}
    want_d = set("%.2f" % d for d in args.densities)
    for cell in cells:
        manifest = _read_json(os.path.join(cell, "manifest.json"))
        name = manifest["group"]
        if name not in args.groups:
            continue
        if ("%.2f" % manifest["density"]) not in want_d:
            continue
        if name not in tables:
            if name not in gs:
                print("error: group %r not in corpus" % name,
                      file=sys.stderr)
                return 1
            tables[name] = fpga_bench.group_table_from(
                gs[name], capacity=capacity)
        table = tables[name]
        res = fpga_bench.bench_cell(dev, cell, table)
        fpga_bench.write_result(res, os.path.join(cell,
                                                  "fpga.json"))
        lost = sum(1 for s in res["scans"] if s.get("lost"))
        print("fpga %-24s d=%.2f load_s=%.4f scans=%d lost=%d"
              % (name, manifest["density"], res["load_s"],
                 len(res["scans"]), lost))
    return 0


# ------------------------------------------------------------- parity

def cmd_parity(args):
    from pyro.quantify import parity

    nfail = 0
    nrun = 0
    for cell in discover_cells(args.root):
        manifest = _read_json(os.path.join(cell, "manifest.json"))
        gdir = os.path.dirname(cell)

        # The unique-pattern index space; patterns.json (written by
        # gen) avoids re-running triage.  unique_patterns() of the
        # already-unique sorted list is the identity, so it can feed
        # oracle_for_corpus as the anchors argument directly.
        pj = os.path.join(gdir, "patterns.json")
        if os.path.isfile(pj):
            patterns = [bytes.fromhex(h)
                        for h in _read_json(pj)["patterns"]]
        else:
            g = _load_groups(args.rules).get(manifest["group"])
            if g is None:
                print("error: no patterns.json and group %r not in "
                      "corpus; skipping %s"
                      % (manifest["group"], cell), file=sys.stderr)
                nfail += 1
                continue
            patterns = [s.anchor for s in g.slots
                        if not s.tombstone]

        sides = []
        sp = os.path.join(cell, "snort-rep0.json")
        if os.path.isfile(sp):
            observed = {}
            for a in _read_json(sp).get("alerts") or []:
                observed.setdefault(int(a["pkt"]), set()).add(
                    int(a["sid"]) - SID_BASE)
            sides.append(("snort", observed))
        _fpath, fres = _find_fpga_result(cell)
        if fres is not None:
            observed = {}
            for s in fres.get("scans") or []:
                if s.get("noms"):
                    observed[int(s["i"])] = set(s["noms"])
            sides.append(("fpga", observed))
        if not sides:
            continue

        oracle = parity.oracle_for_corpus(patterns, cell)
        for side, observed in sides:
            res = parity.compare(manifest, oracle, side, observed)
            _write_json(os.path.join(cell, "parity-%s.json" % side),
                        res)
            nrun += 1
            if not res["pass"]:
                nfail += 1
            print("parity %-24s d=%.2f %-5s hits=%d miss=%d "
                  "extra=%d %s"
                  % (manifest["group"], manifest["density"], side,
                     res["oracle_hits"], len(res["missing"]),
                     res["extra_count"],
                     "PASS" if res["pass"] else "FAIL"))
    if nrun == 0:
        print("parity: no sides with results found", file=sys.stderr)
    return 1 if nfail else 0


# ------------------------------------------------------------ collect

def _slim_snort(r):
    keep = ("repeat", "search_method", "startup_s", "run_s",
            "wall_s", "utime_s", "stime_s", "maxrss_kb", "packets",
            "payload_bytes")
    d = {k: r[k] for k in keep if k in r}
    d["alert_count"] = len(r.get("alerts") or [])
    return d


def _slim_fpga(r):
    keep = ("table_bytes", "n_states", "load_s", "epoch_before",
            "epoch_after", "utime_s", "stime_s")
    d = {k: r[k] for k in keep if k in r}
    d["scans"] = [{"i": s.get("i"), "bytes": s.get("bytes"),
                   "cycles": s.get("cycles"),
                   "wall_s": s.get("wall_s"),
                   "nom_count": len(s.get("noms") or [])}
                  for s in r.get("scans") or []]
    return d


def _slim_parity(r):
    d = dict(r)
    for k in ("missing", "extra"):
        v = d.get(k) or []
        d.setdefault(k + "_count", len(v))
        if len(v) > 20:
            d[k] = v[:20]
            d[k + "_truncated"] = True
    return d


def _env_info(args):
    info = {"python": sys.version.split()[0],
            "platform": platform.platform(),
            "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "seed_base": args.seed,
            "root": os.path.abspath(args.root)}
    try:
        rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
        if rev.returncode == 0:
            info["git_rev"] = rev.stdout.decode().strip()
    except OSError:
        pass
    return info


def cmd_collect(args):
    cells = discover_cells(args.root)
    if not cells:
        print("error: no generated cells under %s" % args.root,
              file=sys.stderr)
        return 1

    out_cells = []
    for cell in cells:
        manifest = _read_json(os.path.join(cell, "manifest.json"))
        gdir = os.path.dirname(cell)

        tinfo = {}
        tj = os.path.join(gdir, "table.json")
        if os.path.isfile(tj):
            tinfo = _read_json(tj)

        def _reps(pattern, schema):
            reps = []
            for p in sorted(glob.glob(os.path.join(cell, pattern))):
                try:
                    d = _read_json(p)
                except (OSError, ValueError):
                    continue
                if d.get("schema") == schema:
                    reps.append(_slim_snort(d))
            return reps

        _fpath, fres = _find_fpga_result(cell)
        parities = []
        for p in sorted(glob.glob(os.path.join(cell,
                                               "parity-*.json"))):
            try:
                d = _read_json(p)
            except (OSError, ValueError):
                continue
            if d.get("schema") == "pyro-quantify-parity/1":
                parities.append(_slim_parity(d))

        fpga = _slim_fpga(fres) if fres is not None else None
        table_bytes = ((fpga or {}).get("table_bytes")
                       or tinfo.get("table_bytes"))
        n_states = ((fpga or {}).get("n_states")
                    or tinfo.get("n_states"))
        out_cells.append({
            "group": manifest["group"],
            "density": manifest["density"],
            "dir": os.path.relpath(cell, args.root),
            "seed": manifest["seed"],
            "count": manifest["count"],
            "payload_bytes": manifest["payload_bytes"],
            "table_bytes": table_bytes,
            "n_states": n_states,
            "snort": _reps("snort-rep*.json",
                           "pyro-quantify-snort/1"),
            "snort_baseline": _reps("snort-baseline-rep*.json",
                                    "pyro-quantify-snort/1"),
            "fpga": fpga,
            "parity": parities,
        })

    run = {"schema": RUN_SCHEMA, "env": _env_info(args),
           "cells": out_cells}
    path = os.path.join(args.root, "run.json")
    _write_json(path, run)
    print("collect: %d cells -> %s" % (len(out_cells), path))
    return 0


# --------------------------------------------------------------- figs

def _figs_module():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "pyro_quantify_figs.py")
    spec = importlib.util.spec_from_file_location(
        "pyro_quantify_figs", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cmd_figs(args):
    run_path = (getattr(args, "run_json", None)
                or os.path.join(args.root, "run.json"))
    if not os.path.isfile(run_path):
        print("error: %s not found (run collect first)" % run_path,
              file=sys.stderr)
        return 1
    outdir = (getattr(args, "outdir", None)
              or os.path.dirname(run_path) or ".")
    for p in _figs_module().render(run_path, outdir):
        print("wrote %s" % p)
    return 0


# ----------------------------------------------------------- software

def cmd_software(args):
    """Everything that runs without hardware, in dependency order."""
    rc = cmd_gen(args)
    if rc:
        return rc
    rc = cmd_snort(args)
    if rc:
        return rc
    parity_rc = cmd_parity(args)
    if parity_rc:
        print("warning: parity step failed (rc=%d); continuing to "
              "collect/figs so the run is inspectable" % parity_rc,
              file=sys.stderr)
    rc = cmd_collect(args)
    if rc:
        return rc
    rc = cmd_figs(args)
    return rc or parity_rc


# ---------------------------------------------------------------- cli

def _density_list(text):
    try:
        vals = [float(t) for t in text.split(",") if t.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError("bad density list: %r"
                                         % text)
    for v in vals:
        if not 0.0 <= v <= 1.0:
            raise argparse.ArgumentTypeError("density out of [0,1]: "
                                             "%r" % v)
    return vals


def _group_list(text):
    return [t.strip() for t in text.split(",") if t.strip()]


def _parent():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--root", default=DEFAULT_ROOT,
                   help="working directory (default build/quantify)")
    p.add_argument("--seed", type=int, default=SEED_BASE,
                   help="seed base (default %d)" % SEED_BASE)
    p.add_argument("--rules", default=DEFAULT_RULES,
                   help="snort3 community rules path")
    p.add_argument("--groups", type=_group_list,
                   default=list(PAPER_GROUPS),
                   help="comma-separated group names "
                        "(default: the five paper groups)")
    p.add_argument("--densities", type=_density_list,
                   default=list(DENSITIES),
                   help="comma-separated densities "
                        "(default 0.0,0.01,0.10)")
    p.add_argument("--count", type=int, default=20000,
                   help="packets per cell (default 20000)")
    p.add_argument("--repeats", type=int, default=5,
                   help="snort repeats per cell (default 5)")
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="pyro_quantify.py",
        description="fpga-vs-snort quantify experiment driver")
    sub = ap.add_subparsers(dest="cmd", required=True)
    par = _parent()

    handlers = [
        ("gen", cmd_gen, "generate corpora, rules, table images"),
        ("snort", cmd_snort, "run the local Snort bench + baseline"),
        ("fpga", cmd_fpga,
         "OPERATOR ONLY: drive the real device (needs "
         "PYRO_DEVICE_IFACE)"),
        ("parity", cmd_parity, "referee cells against the oracle"),
        ("collect", cmd_collect, "aggregate cell JSONs to run.json"),
        ("figs", cmd_figs, "render figures from run.json"),
        ("software", cmd_software,
         "gen + snort + parity + collect + figs (no hardware)"),
    ]
    for name, fn, help_text in handlers:
        sp = sub.add_parser(name, parents=[par], help=help_text)
        sp.set_defaults(func=fn)
        if name == "fpga":
            sp.add_argument("--jumbo", action="store_true",
                            help="use jumbo max_payload (explicit "
                                 "operator opt-in)")
        if name == "figs":
            sp.add_argument("run_json", nargs="?", default=None,
                            help="run.json (default "
                                 "<root>/run.json)")
            sp.add_argument("outdir", nargs="?", default=None,
                            help="output dir (default: next to "
                                 "run.json)")

    args = ap.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
