"""Run the local Snort 2.9 against a generated corpus (fpga-vs-snort).

Implements the E1/E2/E3 Snort side of docs/studies/fpga-vs-snort.md:
N repeats of `snort -r corpus.pcap -A fast -k none -U -y -c group.conf`
over the tree extracted under build/snort-local/root, one result JSON
per repeat (schema pyro-quantify-snort/1).

Empirical notes (validated against Snort 2.9.20 GRE build 82):

- The exit summary goes to STDERR and is printed only WITHOUT -q
  (this build suppresses it entirely under -q), so the bench runs
  un-quiet and parses stderr:
      Run time for packet processing was 1.612 seconds
      Snort processed 3 packets.
- `-U -y` makes fast-alert timestamps UTC with a 2-digit year
  (MM/DD/YY-HH:MM:SS.ffffff), which inverts unambiguously to the
  pcap timestamp index encoding ts_sec = i // 1000000,
  ts_usec = i % 1000000 (all epochs land in 1970, %y maps 70->1970).
- startup_s is measured per repeat by timing a full run over a
  1-packet pcap: exec + config + rules compile dominate, packet
  processing is negligible.  That is Snort's analog of our table
  load (E2).
"""

import calendar
import datetime
import json
import os
import re
import resource
import shutil
import struct
import subprocess
import time

from . import snort_lower
from . import traffic

SCHEMA = "pyro-quantify-snort/1"
SEARCH_METHOD = "ac-q"

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
DEFAULT_SNORT_ROOT = os.path.join(_REPO_ROOT, "build", "snort-local",
                                  "root")

_RUN_TIME_RE = re.compile(
    r"Run time for packet processing was\s+([0-9]+(?:\.[0-9]+)?)"
    r"\s+seconds")
_PROCESSED_RE = re.compile(r"Snort processed\s+([0-9]+)\s+packets")
_ANALYZED_RE = re.compile(r"Analyzed:\s+([0-9]+)")

# 01/01/70-00:00:03.500042  [**] [1:1000000:1] msg [**] ...
_ALERT_RE = re.compile(
    r"^(\d{2}/\d{2}/\d{2}-\d{2}:\d{2}:\d{2})\.(\d{6})\s+"
    r"\[\*\*\]\s+\[(\d+):(\d+):(\d+)\]")

_PCAP_GLOBAL = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0,
                           65535, 1)


def snort_available(snort_root=None):
    """True iff the local extracted Snort tree is present."""
    root = snort_root or DEFAULT_SNORT_ROOT
    return os.access(os.path.join(root, "usr", "sbin", "snort"),
                     os.X_OK)


def _ld_library_path(root):
    """Every dir under root holding a shared object, per the recipe."""
    dirs = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if ".so" in name:
                dirs.add(dirpath)
                break
    return ":".join(sorted(dirs))


def _snort_env(root):
    env = dict(os.environ)
    ld = _ld_library_path(root)
    prior = env.get("LD_LIBRARY_PATH")
    env["LD_LIBRARY_PATH"] = ld + (":" + prior if prior else "")
    return env


def write_pcap(path, payloads):
    """Minimal pcap of TCP frames; timestamps encode the index."""
    with open(path, "wb") as f:
        f.write(_PCAP_GLOBAL)
        for i, payload in enumerate(payloads):
            frame = traffic.build_frame(payload, i)
            f.write(struct.pack("<IIII", i // 1000000, i % 1000000,
                                len(frame), len(frame)))
            f.write(frame)
    return path


def timestamp_to_index(stamp, usec):
    """Invert the pcap encoding: MM/DD/YY-HH:MM:SS (+usec) -> index.

    Timestamps are UTC (-U) with a 2-digit year (-y); %y maps 70-99
    to 1970-1999, so the small ts_sec values the generator writes
    round-trip exactly: index = epoch_sec * 1000000 + usec.
    """
    dt = datetime.datetime.strptime(stamp, "%m/%d/%y-%H:%M:%S")
    return calendar.timegm(dt.timetuple()) * 1000000 + int(usec)


def parse_alert_line(line):
    """One fast-alert line -> (pkt_index, sid), or None."""
    m = _ALERT_RE.match(line)
    if not m:
        return None
    stamp, usec, _gid, sid, _rev = m.groups()
    return timestamp_to_index(stamp, usec), int(sid)


def parse_alert_file(path):
    """Alert file -> [{"pkt": i, "sid": s}, ...] in file order."""
    alerts = []
    if not os.path.exists(path):
        return alerts
    with open(path, errors="replace") as f:
        for line in f:
            hit = parse_alert_line(line)
            if hit is not None:
                alerts.append({"pkt": hit[0], "sid": hit[1]})
    return alerts


def parse_summary(text):
    """Snort exit summary -> (run_s, packets); None where absent."""
    m = _RUN_TIME_RE.search(text)
    run_s = float(m.group(1)) if m else None
    m = _PROCESSED_RE.search(text) or _ANALYZED_RE.search(text)
    packets = int(m.group(1)) if m else None
    return run_s, packets


def _run_snort(root, pcap, conf, logdir):
    """One snort subprocess; wall clock + RUSAGE_CHILDREN deltas."""
    # Snort APPENDS to an existing fast-alert file, so a re-run over
    # the same cell would double-count alerts (and a regenerated
    # corpus would leave stale ones behind).  Start every invocation
    # with a fresh log dir.
    if os.path.isdir(logdir):
        shutil.rmtree(logdir)
    os.makedirs(logdir, exist_ok=True)
    cmd = [os.path.join(root, "usr", "sbin", "snort"),
           "-r", pcap, "-A", "fast", "-k", "none", "-U", "-y",
           "-l", logdir,
           "--daq-dir",
           os.path.join(root, "usr", "lib", "x86_64-linux-gnu",
                        "daq"),
           "-c", conf]
    r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.monotonic()
    proc = subprocess.run(cmd, env=_snort_env(root),
                          stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    wall_s = time.monotonic() - t0
    r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    out = proc.stdout.decode(errors="replace")
    err = proc.stderr.decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError("snort exited %d: %s"
                           % (proc.returncode, err[-2000:]))
    # ru_maxrss for RUSAGE_CHILDREN is a high-water mark across all
    # reaped children, so its before/after delta is 0 from the
    # second child on.  Every child this bench reaps is the same
    # snort binary, so the post-run high-water IS the per-child
    # figure; report that.
    return {
        "wall_s": wall_s,
        "utime_s": r1.ru_utime - r0.ru_utime,
        "stime_s": r1.ru_stime - r0.ru_stime,
        "maxrss_kb": r1.ru_maxrss,
        "summary": err + out,
        "alert_path": os.path.join(logdir, "alert"),
    }


def run_bench(corpus_dir, rules_path, conf_path, outdir, *,
              repeats=5, baseline=False, snort_root=None):
    """Run snort over corpus_dir/corpus.pcap N times.

    Emits one pyro-quantify-snort/1 JSON per repeat under outdir and
    returns the list of result dicts.  baseline=True ignores
    rules_path/conf_path and runs with an EMPTY rules file (the
    decode-only control of section 4); its results are named
    snort-baseline-rep<N>.json.
    """
    root = snort_root or DEFAULT_SNORT_ROOT
    with open(os.path.join(corpus_dir, "manifest.json")) as f:
        manifest = json.load(f)
    pcap = os.path.join(corpus_dir, "corpus.pcap")
    os.makedirs(outdir, exist_ok=True)

    if baseline:
        rules_path, conf_path = snort_lower.write_rules(
            outdir, [], "baseline")

    # E2 probe corpus: one packet, so a timed run is startup cost.
    startup_pcap = write_pcap(os.path.join(outdir, "startup.pcap"),
                              [b"pyro-quantify startup probe."])

    tag = "snort-baseline" if baseline else "snort"
    results = []
    for rep in range(repeats):
        pre = _run_snort(root, startup_pcap, conf_path,
                         os.path.join(outdir,
                                      "%s-rep%d-startup-log"
                                      % (tag, rep)))
        run = _run_snort(root, pcap, conf_path,
                         os.path.join(outdir,
                                      "%s-rep%d-log" % (tag, rep)))
        run_s, packets = parse_summary(run["summary"])
        if run_s is None:
            raise RuntimeError("snort summary not found (rep %d); "
                               "tail: %s"
                               % (rep, run["summary"][-2000:]))
        result = {
            "schema": SCHEMA,
            "group": manifest["group"],
            "density": manifest["density"],
            "repeat": rep,
            "search_method": SEARCH_METHOD,
            "startup_s": pre["wall_s"],
            "run_s": run_s,
            "wall_s": run["wall_s"],
            "utime_s": run["utime_s"],
            "stime_s": run["stime_s"],
            "maxrss_kb": run["maxrss_kb"],
            "packets": (packets if packets is not None
                        else manifest["count"]),
            "payload_bytes": manifest["payload_bytes"],
            "alerts": parse_alert_file(run["alert_path"]),
        }
        path = os.path.join(outdir, "%s-rep%d.json" % (tag, rep))
        with open(path, "w") as f:
            json.dump(result, f, sort_keys=True, indent=1)
            f.write("\n")
        results.append(result)
    return results
