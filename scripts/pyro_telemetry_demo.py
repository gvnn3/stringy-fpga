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
# --serve: stdlib HTTP, single hardware reader
# --------------------------------------------------------------------------
def run_serve(cfg, state, port, interval):
    import http.server

    ring = deque(maxlen=HISTORY_MAX)
    ring.append(T.collect_snapshot(cfg, state))
    lock = threading.Lock()

    def collector():
        while True:
            time.sleep(interval)
            snap = T.collect_snapshot(cfg, state)
            with lock:
                ring.append(snap)

    threading.Thread(target=collector, daemon=True).start()
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
    print("serving on :%d (collect every %.1fs; iface=%s)"
          % (port, interval, cfg.iface if cfg else None), file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
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
    mode.add_argument("--demo", action="store_true",
                      help="scripted on-hardware demo (needs the overlay "
                           "child resident)")
    ap.add_argument("--iface", default=os.environ.get("PYRO_DEVICE_IFACE"),
                    help="onic netdev (default: PYRO_DEVICE_IFACE; never "
                         "guessed — R68)")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="--serve collection period, seconds (default 2)")
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
        if args.demo:
            # R68 fail-closed: a netdev is never guessed, and the demo
            # cannot run host-only.
            print("--demo needs --iface or PYRO_DEVICE_IFACE (R68: never "
                  "guess a netdev)", file=sys.stderr)
            return 2
        print("PYRO_DEVICE_IFACE not set — host-only snapshots (device "
              "section null; R68: never guess a netdev)", file=sys.stderr)

    if args.demo:
        return run_demo(cfg, args)

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
        return run_serve(cfg, state, args.serve, args.interval)
    return 2


if __name__ == "__main__":
    sys.exit(main())
