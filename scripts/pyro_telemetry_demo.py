#!/usr/bin/env python3
"""Telemetry collector CLI: snapshot, watch, serve, and a scripted HW demo.

Four modes over :mod:`pyro.telemetry`:

  --snapshot   one ``pyro-telemetry/1`` JSON document to stdout
  --watch N    one JSON line every N seconds (JSON-lines, for piping)
  --serve PORT stdlib HTTP server:
                 /              web/pyro_dashboard.html (served from the
                                repo file, never inlined here)
                 /snapshot.json the latest collected snapshot
                 /history.json  ring buffer, last 300 snapshots
                 /metrics       Prometheus exposition
               A single background thread does all hardware access; the
               HTTP handlers only read its ring, so two scrapes can never
               interleave raw-socket round-trips on the wire.
  --demo       scripted sequence against live hardware: status → build two
               group tables → load A (timed) → ~20 MATCH round-trips from
               corpus-derived subjects → swap to B (timed) → more scans →
               swap back (timed) → final JSON report.

The interface comes from --iface or PYRO_DEVICE_IFACE and is NEVER guessed
(R68).  Without one, snapshot/watch/serve degrade to host-only snapshots
(device section null, ``usable: false``) and --demo refuses to run.

MATCH round-trips reuse the working shape from
``scripts/pyro_overlay_bringup.py``: BE request header, entries decoded
LITTLE-endian ``<QQII`` (R47 packing — LE entries inside a BE header), and
the jumbo payload bound set explicitly (R78.9a: operator configuration,
never probed).

Usage:
    PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 \\
        scripts/pyro_telemetry_demo.py --demo
"""
import argparse
import json
import os
import struct
import sys
import threading
import time
import traceback
from collections import deque

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import pyro.device as pdev                      # noqa: E402
import pyro.telemetry as T                      # noqa: E402

#: A5 overlay engine identity, enforced by the device at commit.
ENGINE_ID = 0x0A5E0001

#: Repo-relative dashboard page --serve serves at "/".
DASHBOARD = os.path.join("web", "pyro_dashboard.html")

#: /history.json ring depth.
HISTORY_MAX = 300


def make_config(iface):
    """The one DeviceConfig shape this box uses: onic netdev control
    binding, jumbo shell (MAX_PKT_LEN 9600 — R78.9a explicit, never
    probed)."""
    return pdev.DeviceConfig(iface=iface, chardev=None,
                             max_payload=pdev.MAX_PAYLOAD_JUMBO,
                             probe_timeout_s=2.0)


def match_on_device(cfg, subject, slot=1, seq=900):
    """One MATCH round-trip → MatchReplySummary, or None on timeout.

    Same frame shape as pyro_overlay_bringup.match_on_device; returns the
    telemetry summary type so replies feed TelemetryState.record_scan
    directly.  ``start`` in each entry is recomputed nowhere — the overlay
    engine hardwires it to 0 and the demo only attributes by pattern_id.
    """
    transport = pdev._make_transport(cfg)
    try:
        eth = (bytes(cfg.dst_mac) + bytes(cfg.src_mac)
               + struct.pack(">H", pdev.ETHERTYPE))
        payload = struct.pack(">IIHH", 0, 0, 61, 0) + subject
        transport.send(eth + pdev.encode_frame(pdev.KIND_MATCH_REQUEST, slot,
                                               seq, payload,
                                               max_payload=cfg.max_payload))
        deadline = time.monotonic() + cfg.probe_timeout_s
        while time.monotonic() < deadline:
            reply = transport.recv(deadline - time.monotonic())
            if reply is None:
                break
            reply = bytes(reply)
            if (len(reply) < 14 + pdev.PYRO_HEADER_LEN
                    or struct.unpack(">H", reply[12:14])[0] != pdev.ETHERTYPE):
                continue
            try:
                dec = pdev.decode_frame(reply[14:], max_payload=cfg.max_payload)
            except pdev.PyroFrameError:
                continue
            if dec.seq != seq or dec.kind != pdev.KIND_MATCH_REPLY:
                continue
            count, status = struct.unpack(">HH", dec.payload[0:4])
            epoch = struct.unpack(">I", dec.payload[4:8])[0]
            entries = []
            for i in range(count):
                b = 8 + i * 24
                # R47 entries are little-endian; the header around them is not.
                s, e, pid, _f = struct.unpack("<QQII", dec.payload[b:b + 24])
                entries.append((int(pid), int(s), int(e)))
            return T.MatchReplySummary(count, bool(status & 1), epoch,
                                       tuple(entries))
        return None
    finally:
        transport.close()


# --------------------------------------------------------------------------
# --demo: the scripted on-hardware sequence
# --------------------------------------------------------------------------
def _build_table(group, capacity):
    """Group → (image, n_states); refuses a table the engine cannot hold."""
    from pyro.overlay import table as otable
    anchors = [None if s.tombstone else s.anchor for s in group.slots]
    ac = otable.build(anchors)
    if capacity is not None and ac.n_states > capacity:
        raise SystemExit("group %s needs %d states, engine holds %d"
                         % (group.name, ac.n_states, capacity))
    return otable.serialize(ac, engine_id=ENGINE_ID), ac.n_states


def _subjects_for(group, n):
    """Corpus-derived scan subjects: runs of real anchors with filler, plus
    a guaranteed-negative subject so zero-match replies are exercised too."""
    anchors = [s.anchor for s in group.slots if not s.tombstone]
    out = []
    for i in range(n):
        if i % 5 == 4:
            out.append(b"negative subject: nothing in the corpus says this")
            continue
        picks = anchors[(i * 3) % max(1, len(anchors)):][:4] or [b"x"]
        out.append((b"\x00" * (i % 7)) + b" ".join(picks) + b"\xff" * (i % 5))
    return out


def _scan_phase(cfg, group, state, subjects, seq_base):
    """Drive one batch of MATCH round-trips, recording every outcome —
    including timeouts, which ARE the request-loss measurement."""
    for i, subj in enumerate(subjects):
        reply = match_on_device(cfg, subj, seq=seq_base + i)
        state.record_scan(reply)


def run_demo(cfg, args):
    from pyro.snort import groups as G
    from pyro.snort import triage as Tr

    state = T.TelemetryState()
    ok, reason = pdev.probe_device(cfg)
    print("probe: %s" % reason, file=sys.stderr)
    if not ok:
        return 1

    st0 = pdev.read_table_status(cfg, slot=1)
    if st0 is None:
        print("no TABLE_STATUS_REPLY — resident child predates A5 §3; "
              "the demo needs the overlay child", file=sys.stderr)
        return 1
    print("device: active=0x%08x epoch=%d caps=%d"
          % (st0.active_table_id, st0.epoch, st0.capacity_states),
          file=sys.stderr)

    rules = os.path.join(REPO, "third_party", "snort3-community-rules",
                         "snort3-community.rules")
    packed = G.pack_groups(Tr.triage_file(rules))
    by_name = {g.name: g for g in packed}
    for name in (args.group_a, args.group_b):
        if name not in by_name:
            print("no such group %r; have: %s"
                  % (name, ", ".join(sorted(by_name)[:10])), file=sys.stderr)
            return 1
    ga, gb = by_name[args.group_a], by_name[args.group_b]
    img_a, states_a = _build_table(ga, st0.capacity_states)
    img_b, states_b = _build_table(gb, st0.capacity_states)
    print("tables: %s %d states %d B; %s %d states %d B"
          % (ga.name, states_a, len(img_a), gb.name, states_b, len(img_b)),
          file=sys.stderr)

    # Load A (timed), scan; swap to B (timed), scan; swap back (timed).
    # set_resident BEFORE each phase's scans: attribution against the wrong
    # sidecar is the misattribution SR14 exists to prevent.
    phases = []
    for phase, (grp, img, n_scans, seq0) in enumerate((
            (ga, img_a, args.scans, 1000),
            (gb, img_b, max(1, args.scans // 2), 2000),
            (ga, img_a, 5, 3000))):
        st = T.timed_load_table(cfg, img, state, slot=1)
        state.set_resident(grp)
        _scan_phase(cfg, grp, state, _subjects_for(grp, n_scans), seq0)
        phases.append({"group": grp.name, "epoch": st.epoch,
                       "active_table_id": "0x%08x" % st.active_table_id,
                       "switch_ms": state.last_switch_ms,
                       "scans_after": state.scans})
        print("phase %d: %s resident (epoch %d), swap %.1f ms"
              % (phase, grp.name, st.epoch, state.last_switch_ms),
              file=sys.stderr)

    snap = T.collect_snapshot(cfg, state)
    view = state.view()
    hist = view["switch_history_ms"]
    report = {
        "demo": {
            "phases": phases,
            "switch": {
                "count": view["switch_count"],
                "history_ms": hist,
                "min_ms": min(hist), "max_ms": max(hist),
                "mean_ms": sum(hist) / len(hist),
                "pr_baseline_s": T.PR_BASELINE_S,
            },
            "per_sid_nominations": view["by_sid"],
            "requests_sent": view["requests_sent"],
            "replies_received": view["replies_received"],
        },
        "snapshot": snap,
    }
    json.dump(report, sys.stdout, indent=2)
    print()
    return 0


# --------------------------------------------------------------------------
# --paper: timed run -> raw series + camera-ready figures
# --------------------------------------------------------------------------
#: Groups the paper run rotates through, chosen to SPAN table size (1 KB ->
#: 146 KB) so the size-vs-latency figure has leverage, not two clusters.
PAPER_GROUPS = ["$SIP_PORTS/0", "$FTP_PORTS/0", "$ORACLE_PORTS/1",
                "literal/3", "any/0"]
PAPER_SWAP_EVERY_S = 8.0     # ~4 swaps at 30 s, ~60 at 500 s
PAPER_SCAN_GAP_S = 0.4       # scan cadence between swaps


def run_paper(cfg, args):
    """Drive the system for --paper seconds; write raw series + figures.

    Collection and rendering are deliberately separate: everything measured
    lands in ``run.json`` (with per-figure CSVs beside it), and the figures
    are rendered FROM that file by scripts/pyro_paper_figs.py -- so a figure
    can be restyled for a camera-ready deadline without re-running hardware,
    and the CSVs are the artifact a reviewer can check.
    """
    from pyro.snort import groups as G
    from pyro.snort import triage as Tr

    seconds = float(args.paper)
    outdir = args.paper_out or os.path.join(
        REPO, "docs", "studies", "paper-data",
        time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(outdir, exist_ok=True)

    ok, reason = pdev.probe_device(cfg)
    print("probe: %s" % reason, file=sys.stderr)
    if not ok:
        return 1
    st0 = pdev.read_table_status(cfg, slot=1)
    if st0 is None:
        print("no TABLE_STATUS_REPLY — the paper run needs the overlay "
              "child resident", file=sys.stderr)
        return 1

    rules = os.path.join(REPO, "third_party", "snort3-community-rules",
                         "snort3-community.rules")
    gs = {g.name: g for g in G.pack_groups(Tr.triage_file(rules))}
    names = [n for n in PAPER_GROUPS if n in gs]
    tables = {n: _build_table(gs[n], st0.capacity_states)[0] for n in names}
    subjects = {n: _subjects_for(gs[n], 10) for n in names}
    print("groups: %s" % ", ".join("%s (%d B)" % (n, len(tables[n]))
                                   for n in names), file=sys.stderr)

    state = T.TelemetryState()
    swaps, scans = [], []            # the two event series
    seq = [20000]
    t0 = time.monotonic()

    def now():
        return time.monotonic() - t0

    def swap_to(name):
        st = T.timed_load_table(cfg, tables[name], state)
        state.set_resident(gs[name])
        swaps.append({"t": now(), "group": name,
                      "bytes": len(tables[name]),
                      "ms": state.switch_history_ms[-1],
                      "epoch": st.epoch})
        print("  t=%6.1fs swap -> %-16s %6d B  %6.1f ms  epoch %d"
              % (swaps[-1]["t"], name, len(tables[name]),
                 swaps[-1]["ms"], st.epoch), file=sys.stderr)

    cur = 0
    swap_to(names[0])
    next_swap = PAPER_SWAP_EVERY_S
    i_subj = 0
    while now() < seconds:
        name = names[cur]
        subj = subjects[name][i_subj % len(subjects[name])]
        i_subj += 1
        seq[0] += 1
        reply = match_on_device(cfg, subj, seq=seq[0])
        state.record_scan(reply)
        pc = pdev.read_perf_counters(cfg, slot=1)
        scans.append({
            "t": now(), "group": name, "subject_bytes": len(subj),
            # reply is a MatchReplySummary (or None on timeout) — the same
            # object record_scan consumed.
            "nominations": (reply.count if reply is not None else None),
            "epoch": (reply.epoch if reply is not None else None),
            "replied": reply is not None,
            "cycles": pc[0] if pc else None,
            "bytes": pc[1] if pc else None,
        })
        if now() >= next_swap and now() < seconds:
            cur = (cur + 1) % len(names)
            swap_to(names[cur])
            next_swap += PAPER_SWAP_EVERY_S
        time.sleep(PAPER_SCAN_GAP_S)

    run = {
        "meta": {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "duration_s": seconds, "iface": cfg.iface,
            "pr_baseline_s": 13.6, "clock_mhz": 250,
            "groups": {n: {"bytes": len(tables[n]),
                           "rules": gs[n].rule_count} for n in names},
            "engine_capacity_states": st0.capacity_states,
        },
        "swaps": swaps, "scans": scans,
        "final_snapshot": T.collect_snapshot(cfg, state),
    }
    run_path = os.path.join(outdir, "run.json")
    with open(run_path, "w") as f:
        json.dump(run, f, indent=1, sort_keys=True)
        f.write("\n")
    print("wrote %s (%d swaps, %d scans)"
          % (run_path, len(swaps), len(scans)), file=sys.stderr)

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import pyro_paper_figs
    made = pyro_paper_figs.render(run_path, outdir)
    for m in made:
        print("wrote %s" % m, file=sys.stderr)
    print(json.dumps({"outdir": outdir, "swaps": len(swaps),
                      "scans": len(scans), "figures": made}))
    return 0


# --------------------------------------------------------------------------
# --serve: stdlib HTTP, single hardware reader
# --------------------------------------------------------------------------
def run_serve(cfg, state, port, interval, drive=None):
    import http.server

    # --drive: the serve process generates its own live activity, because a
    # fresh TelemetryState knows only about work done in THIS process — a
    # passive server shows a correct but empty switch history.  drive is
    # (group_a, group_b, swap_every_ticks) or None.  Everything runs inside
    # the one collector thread: scans, swaps, and snapshots strictly
    # interleave, so two wire operations can never race for replies (the
    # HTTP handlers only read the ring).
    drv = None
    if drive is not None and cfg is not None:
        from pyro.snort import groups as G
        from pyro.snort import triage as Tr
        ga, gb, swap_every = drive
        st0 = pdev.read_table_status(cfg, slot=1)
        if st0 is None:
            raise SystemExit("--drive needs the overlay child resident")
        cap = st0.capacity_states
        rules = os.path.join(REPO, "third_party", "snort3-community-rules",
                             "snort3-community.rules")
        gs = {g.name: g for g in G.pack_groups(Tr.triage_file(rules))}
        for name in (ga, gb):
            if name not in gs:
                raise SystemExit("no such group %r" % name)
        tables = {n: _build_table(gs[n], cap)[0] for n in (ga, gb)}
        subjects = {n: _subjects_for(gs[n], 10) for n in (ga, gb)}
        T.timed_load_table(cfg, tables[ga], state)
        state.set_resident(gs[ga])
        drv = {"names": (ga, gb), "cur": 0, "tick": 0,
               "swap_every": swap_every, "tables": tables,
               "groups": {n: gs[n] for n in (ga, gb)},
               "subjects": subjects, "seq": 5000}

    ring = deque(maxlen=HISTORY_MAX)
    ring.append(T.collect_snapshot(cfg, state))
    lock = threading.Lock()

    # A dead collector must be LOUD.  Without the guard below, any
    # exception here (e.g. read_sched's deliberate RuntimeError when a
    # status==0 ACK claims an invalid mode was APPLIED) kills this
    # daemon thread silently and the server keeps serving the frozen
    # ring — stale /metrics forever, the opposite of the honesty rule.
    fail = {}

    def collector():
        try:
            _collect_loop()
        except Exception:
            fail["tb"] = traceback.format_exc()
            server.shutdown()

    def _collect_loop():
        while True:
            time.sleep(interval)
            if drv is not None:
                drv["tick"] += 1
                cur = drv["names"][drv["cur"]]
                # a couple of scans per tick keeps nominations/perf moving
                subs = drv["subjects"][cur]
                for subj in subs[(drv["tick"] * 2) % len(subs):][:2]:
                    drv["seq"] += 1
                    state.record_scan(match_on_device(cfg, subj,
                                                      seq=drv["seq"]))
                if drv["tick"] % drv["swap_every"] == 0:
                    drv["cur"] ^= 1
                    nxt = drv["names"][drv["cur"]]
                    # set_resident BEFORE the next scans: attributing a
                    # reply against the old sidecar is the SR14
                    # misattribution the setter's docstring warns about.
                    T.timed_load_table(cfg, drv["tables"][nxt], state)
                    state.set_resident(drv["groups"][nxt])
            snap = T.collect_snapshot(cfg, state)
            with lock:
                ring.append(snap)

    dashboard_path = os.path.join(REPO, DASHBOARD)

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                try:
                    with open(dashboard_path, "rb") as f:
                        self._send(200, "text/html; charset=utf-8", f.read())
                except OSError:
                    self._send(404, "text/plain",
                               b"missing " + DASHBOARD.encode())
            elif path == "/snapshot.json":
                with lock:
                    snap = ring[-1]
                self._send(200, "application/json",
                           json.dumps(snap).encode())
            elif path == "/history.json":
                with lock:
                    hist = list(ring)
                self._send(200, "application/json",
                           json.dumps(hist).encode())
            elif path == "/metrics":
                with lock:
                    snap = ring[-1]
                self._send(200, "text/plain; version=0.0.4; charset=utf-8",
                           T.prometheus_text(snap).encode())
            else:
                self._send(404, "text/plain", b"not found")

        def log_message(self, fmt, *fmt_args):   # quiet by default
            pass

    server = http.server.ThreadingHTTPServer(("", port), Handler)
    threading.Thread(target=collector, daemon=True).start()
    print("serving on :%d (collect every %.1fs; iface=%s)"
          % (port, interval, cfg.iface if cfg else None), file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    if fail:
        print(fail["tb"], file=sys.stderr, end="")
        print("collector thread died; refusing to serve stale "
              "telemetry", file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--snapshot", action="store_true",
                      help="one JSON snapshot to stdout")
    mode.add_argument("--watch", type=float, metavar="N",
                      help="JSON-lines snapshot every N seconds")
    mode.add_argument("--serve", type=int, metavar="PORT",
                      help="HTTP server: /, /snapshot.json, /history.json, "
                           "/metrics")
    mode.add_argument("--paper", type=float, metavar="SECONDS",
                      help="timed run (30/60/500 s) -> raw series + "
                           "camera-ready figures under --paper-out")
    mode.add_argument("--demo", action="store_true",
                      help="scripted on-hardware demo (needs the overlay "
                           "child resident)")
    ap.add_argument("--iface", default=os.environ.get("PYRO_DEVICE_IFACE"),
                    help="onic netdev (default: PYRO_DEVICE_IFACE; never "
                         "guessed — R68)")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="--serve collection period, seconds (default 2)")
    ap.add_argument("--drive", action="store_true",
                    help="--serve only: generate live activity (scans every "
                         "tick, group swap every --swap-every ticks) so the "
                         "dashboard shows real switch/nomination series")
    ap.add_argument("--paper-out", default=None,
                    help="--paper output dir (default docs/studies/"
                         "paper-data/<timestamp>)")
    ap.add_argument("--swap-every", type=int, default=15,
                    help="--drive swap period in ticks (default 15 = ~30 s)")
    ap.add_argument("--group-a", default="$SSH_PORTS/0",
                    help="--demo first group (default $SSH_PORTS/0)")
    ap.add_argument("--group-b", default="literal/0",
                    help="--demo second group (default literal/0)")
    ap.add_argument("--scans", type=int, default=20,
                    help="--demo MATCH round-trips in phase A (default 20)")
    args = ap.parse_args(argv)

    if args.iface:
        cfg = make_config(args.iface)
    else:
        cfg = None
        if args.demo or args.paper:
            # R68 fail-closed: a netdev is never guessed, and the demo
            # cannot run host-only.
            print("--demo/--paper needs --iface or PYRO_DEVICE_IFACE "
                  "(R68: never guess a netdev)", file=sys.stderr)
            return 2
        print("PYRO_DEVICE_IFACE not set — host-only snapshots (device "
              "section null; R68: never guess a netdev)", file=sys.stderr)

    if args.demo:
        return run_demo(cfg, args)
    if args.paper:
        return run_paper(cfg, args)

    state = T.TelemetryState()
    if args.snapshot:
        json.dump(T.collect_snapshot(cfg, state), sys.stdout, indent=2)
        print()
        return 0
    if args.watch is not None:
        try:
            while True:
                print(json.dumps(T.collect_snapshot(cfg, state)), flush=True)
                time.sleep(max(0.1, args.watch))
        except KeyboardInterrupt:
            return 0
    if args.serve is not None:
        drive = ((args.group_a, args.group_b, max(1, args.swap_every))
                 if args.drive else None)
        return run_serve(cfg, state, args.serve, args.interval, drive=drive)
    return 2


if __name__ == "__main__":
    sys.exit(main())
