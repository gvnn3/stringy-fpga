"""Telemetry: one snapshot document over the four operator metrics.

The owner named four numbers — **switch time, rules matched, packets
dropped, rules missed** — and this module makes each one first-class,
sourced from the ONE place it is actually true rather than the most
convenient one:

switch time      caller-clocked around :func:`pyro.device.load_table`
                 (:func:`timed_load_table`).  The device keeps no clock and
                 ``load_table`` takes no timestamps, so wall time on the
                 host is the only real measurement.  Anchors for scale:
                 12.3 ms load+commit for 2564 B; the full 1.64 MB corpus
                 table is 0.66 ms of wire time at 2.3 GiB/s; a JTAG PR
                 switch is 13.6 s.
rules matched    host-side aggregation of MATCH_REPLY entries.  The fabric
                 keeps no per-slot hit counters (audit gap), so cumulative
                 attribution lives here: ``pattern_id`` is the slot index
                 in the resident group, mapped to its ``gid:sid`` LIST via
                 the group sidecar — a slot serves every rule sharing its
                 anchor, and collapsing the list to one sid would be an SR3
                 completeness defect.
packets dropped  two honest halves.  (a) netdev counters from
                 ``/sys/class/net/<iface>/statistics`` — with the caveats
                 that they zero on every onic reload (so every JTAG swap)
                 and that EQDMA multi-queue loss is invisible to them
                 (measured: >=2 H2C queues silently swallow frames, no
                 tuser_err, up to 13% loss — bench with
                 ``PYRO_BENCH_QUEUES=1``).  (b) request-level loss counted
                 host-side as sent-vs-answered, because a scan timeout is
                 indistinguishable on the wire from "scanned, zero
                 matches" and nothing else in the repo counts it.
rules missed     decomposed, not averaged into one dishonest number:
                 * ``resident_hard_misses`` is **0 by the SR3 invariant**
                   — the device's nomination set equals the model's
                   exactly, verified by the two-sided oracle and the
                   on-silicon bringup.  It is stated as a verified
                   invariant, never presented as a runtime measurement.
                 * ``ovf_truncation_events`` counts replies where the
                   engine hit OUT_CAP=61 and set OVF — each one truncated
                   at least one entry (countable; the exact number lost is
                   not observable, only the OVF bit crosses the wire).
                 * ``non_resident_rules`` and ``lowering_dropped`` are the
                   coverage gap: groupable rules not currently resident,
                   and rules the triage/lowering chain could not express
                   at all (always-forward tier plus anchor rules without a
                   parseable sid — a rule that cannot be attributed cannot
                   be nominated).

PERF counters are deliberately nulled when an A5 overlay child is resident:
addresses 0x0058-0x0064 do not exist in that engine (reads fall to the CSR
default) and the wrapper's ST_PERF latch is one cycle early for the overlay
engine's registered readback, so a PERF_REPLY against the overlay child is
constant/skewed garbage, not a measurement.  Presence of a
TABLE_STATUS_REPLY is the fingerprint that the resident child is the
overlay engine, so that same reply gates the perf read.

The WIRE-MAC scheduler (``scheduler.wire_mac``) is read with the
SCHED_SET refusal probe: an INVALID mode (0xFF) the generated wrapper
refuses — nothing is written and the RR rotation is untouched — while
the SCHED_ACK still echoes the LIVE mode+quantum (proven on silicon
2026-08-06; :func:`pyro.macwire.read_sched` owns the idiom and the
never-write-to-read rule).  Per-program grant truth comes from where
each program counts it: P0 from the wire counters, P1 from the MAC
stats.  When the MAC child does not answer, the section is
absent-with-reason ({"available": false, ...}), never a fake number.

Corpus-derived numbers (total rules, groupable rules, lowering losses) come
from one lazily-computed, cached :func:`corpus_summary` — ``pack_groups``
over the 4,017-rule corpus takes seconds and its result is a pure function
of the rules file, so it is computed at most once per process.

Nothing here touches hardware unless handed a ``DeviceConfig``:
``collect_snapshot(cfg=None)`` is a complete host-only snapshot with the
device section null and ``usable: false``.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Dict, List, NamedTuple, Optional, Tuple

#: Snapshot schema identifier (top-level contract key).
SCHEMA = "pyro-telemetry/1"

#: Measured JTAG PR switch baseline, for contrast with table-swap times.
PR_BASELINE_S = 13.6

#: Measured table wire throughput (2.3 GiB/s on the jumbo shell) — the
#: denominator for ``wire_ms_est``.
WIRE_BYTES_PER_S = 2.3 * (1 << 30)

#: R78.7 reply bound: the engine truncates at 61 entries and sets OVF.
OUT_CAP = 61

#: The netdev counters that matter for drop accounting (contract keys).
NETDEV_KEYS = ("rx_dropped", "rx_errors", "rx_missed_errors",
               "tx_dropped", "rx_packets", "tx_packets")

#: SR3 statement carried verbatim into every snapshot: this is an invariant
#: (verified by the two-sided oracle and the on-silicon bringup, where the
#: device's nomination set equals the model's exactly), not a measurement.
SR3_NOTE = ("0 by SR3: hard misses among resident rules are a verified "
            "invariant (two-sided oracle + on-silicon bringup: device "
            "nomination set == model set, exactly), not a runtime "
            "measurement")

#: WIRE-MAC scheduler mode names (SCHED_SET modes; an unknown value
#: maps to a null name, never a guessed one).
SCHED_MODE_NAMES = {0: "p0_only", 1: "p1_only", 2: "round_robin",
                    3: "broadcast"}

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_RULES = os.path.join(_REPO, "third_party",
                              "snort3-community-rules",
                              "snort3-community.rules")


class MatchReplySummary(NamedTuple):
    """A parsed MATCH_REPLY, in the shape :meth:`TelemetryState.record_scan`
    consumes.  ``entries`` rows are ``(pattern_id, start, end)`` — start is
    dead telemetry from the overlay engine (hardwired 0, host derives from
    end), carried for shape-compatibility with the daemon's ScanResult."""
    count: int
    ovf: bool
    epoch: int
    entries: Tuple[Tuple[int, int, int], ...]

    # Duck-type alias so daemon.ScanResult (.overflowed) and this type
    # (.ovf) are interchangeable to record_scan.
    @property
    def overflowed(self) -> bool:
        return self.ovf


# --------------------------------------------------------------------------
# Corpus-derived numbers: lazy, cached, computed at most once per process
# --------------------------------------------------------------------------
_corpus_cache: Dict[str, dict] = {}
_corpus_lock = threading.Lock()


def corpus_summary(rules_path: Optional[str] = None) -> dict:
    """Triage + pack the corpus once; return the coverage denominators.

    Cached per rules path because ``pack_groups`` over the community corpus
    takes seconds and the result is a pure function of the file's content
    (SR6 determinism).  Keys:

    * ``corpus_total_rules`` — every parsed rule (SF15 totality: parse
      failures become always-forward, never exceptions, so this is the
      whole file).
    * ``groupable_rules`` — anchor-compilable rules with a parseable sid;
      the population that CAN be resident.
    * ``lowering_dropped`` — rules with no circuit at all: the
      always-forward tier plus anchor rules lacking a sid (unattributable,
      therefore unpackable).
    * ``header_only_rules`` — decided host-side (SR13); not a coverage gap
      and reported separately so it is never conflated with one.
    * ``groups`` — ``{name: rule_count}`` for the resident-coverage lookup.
    """
    path = rules_path or _DEFAULT_RULES
    with _corpus_lock:
        hit = _corpus_cache.get(path)
        if hit is not None:
            return hit
        from .snort import groups as _groups
        from .snort import triage as _triage
        triaged = _triage.triage_file(path)
        packed = _groups.pack_groups(triaged)
        tiers: Dict[str, int] = {}
        for _rule, res in triaged:
            tiers[res.tier] = tiers.get(res.tier, 0) + 1
        groupable = sum(g.rule_count for g in packed)
        anchor = tiers.get(_triage.TIER_ANCHOR, 0)
        summary = {
            "corpus_total_rules": len(triaged),
            "groupable_rules": groupable,
            "lowering_dropped": (tiers.get(_triage.TIER_ALWAYS_FORWARD, 0)
                                 + (anchor - groupable)),
            "header_only_rules": tiers.get(_triage.TIER_HEADER_ONLY, 0),
            "always_forward_rules": tiers.get(_triage.TIER_ALWAYS_FORWARD, 0),
            "groups": {g.name: g.rule_count for g in packed},
        }
        _corpus_cache[path] = summary
        return summary


# --------------------------------------------------------------------------
# Cumulative host counters
# --------------------------------------------------------------------------
class TelemetryState:
    """Cumulative host-side counters carried across snapshots.

    Everything here exists because the fabric cannot count it (no per-slot
    hit counters, no dropped-frame counters, no clock): switch timings,
    request/reply accounting, per-sid nomination totals, OVF events.  All
    methods are thread-safe; all counters are monotonic except the resident
    gauges.

    ``corpus`` optionally injects a pre-computed :func:`corpus_summary`
    dict (tests use a small synthetic one; live callers leave it ``None``
    and the real corpus is computed lazily on first use).
    """

    #: Bound on retained switch history (each entry is one residency swap;
    #: at the 300 s min-dwell cadence this is >a day of swaps).
    HISTORY_MAX = 300

    def __init__(self, corpus: Optional[dict] = None):
        self._lock = threading.Lock()
        self.corpus = corpus
        # switch
        self.switch_count = 0
        self.switch_history_ms: List[float] = []
        self.last_switch_ms: Optional[float] = None
        self.last_switch_bytes: Optional[int] = None
        # matching
        self.scans = 0
        self.nominations_total = 0
        self.by_sid: Dict[str, int] = {}
        self.ovf_events = 0
        # loss (host-derived: nothing else in the repo counts answered scans)
        self.requests_sent = 0
        self.replies_received = 0
        # residency gauges
        self.resident_group: Optional[str] = None
        self.resident_rules = 0
        self._sidecar: Dict[int, Tuple[str, ...]] = {}

    # -- residency ---------------------------------------------------------
    def set_resident(self, group) -> None:
        """Declare which group's sidecar resolves ``pattern_id`` → sids.

        Must be called at swap time, BEFORE scans against the new table are
        recorded: attributing a reply against the wrong sidecar is exactly
        the misattribution the SR14 identity discipline exists to prevent.
        ``None`` = nothing resident (attribution disabled, scans still
        counted).
        """
        with self._lock:
            if group is None:
                self.resident_group = None
                self.resident_rules = 0
                self._sidecar = {}
            else:
                self.resident_group = group.name
                self.resident_rules = group.rule_count
                self._sidecar = dict(group.sidecar())

    # -- switch ------------------------------------------------------------
    def record_switch(self, ms: float, n_bytes: int) -> None:
        """One completed table swap: measured wall ms and image size (the
        size feeds ``wire_ms_est`` at the measured 2.3 GiB/s)."""
        with self._lock:
            self.switch_count += 1
            self.last_switch_ms = float(ms)
            self.last_switch_bytes = int(n_bytes)
            self.switch_history_ms.append(float(ms))
            if len(self.switch_history_ms) > self.HISTORY_MAX:
                del self.switch_history_ms[0]

    # -- scans -------------------------------------------------------------
    def record_scan(self, reply) -> None:
        """One MATCH round-trip.  ``reply`` is ``None`` for a timeout (the
        request is still counted — sent-vs-answered IS the loss metric,
        because an unanswered scan is otherwise indistinguishable from
        "scanned, zero matches"), else anything with ``.entries`` rows
        whose first element is ``pattern_id`` and an ``.overflowed``/
        ``.ovf`` flag — :class:`MatchReplySummary` and the daemon's
        ``ScanResult`` both qualify."""
        with self._lock:
            self.requests_sent += 1
            if reply is None:
                return
            self.replies_received += 1
            self.scans += 1
            ovf = bool(getattr(reply, "overflowed", False)
                       or getattr(reply, "ovf", False))
            if ovf:
                self.ovf_events += 1
            for ent in getattr(reply, "entries", ()):
                pid = int(ent[0]) if isinstance(ent, (tuple, list)) \
                    else int(ent)
                for key in self._sidecar.get(pid, ()):
                    self.by_sid[key] = self.by_sid.get(key, 0) + 1
                    self.nominations_total += 1

    # -- read side ---------------------------------------------------------
    def view(self) -> dict:
        """A consistent copy of every counter (one lock hold, no tearing)."""
        with self._lock:
            return {
                "switch_count": self.switch_count,
                "switch_history_ms": list(self.switch_history_ms),
                "last_switch_ms": self.last_switch_ms,
                "last_switch_bytes": self.last_switch_bytes,
                "scans": self.scans,
                "nominations_total": self.nominations_total,
                "by_sid": dict(self.by_sid),
                "ovf_events": self.ovf_events,
                "requests_sent": self.requests_sent,
                "replies_received": self.replies_received,
                "resident_group": self.resident_group,
                "resident_rules": self.resident_rules,
            }


def timed_load_table(cfg, image: bytes, state: Optional[TelemetryState],
                     slot: int = 1):
    """:func:`pyro.device.load_table` with the wall clock the device lacks.

    Records the switch into ``state`` only on SUCCESS — a refused commit
    leaves the old table active (A5 §5 fail-closed), so it is not a switch
    and recording it would corrupt the switch-time series.  The measured
    time includes one round-trip per chunk plus host-side CRC, i.e. it is
    the operator-visible swap latency, not pure wire time.  Returns the
    committed :class:`pyro.device.TableStatus`; raises
    :class:`pyro.device.PyroLoadError` exactly as ``load_table`` does.
    """
    from . import device as _device
    t0 = time.monotonic()
    st = _device.load_table(cfg, image, slot=slot)
    dt_ms = (time.monotonic() - t0) * 1e3
    if state is not None:
        state.record_switch(dt_ms, len(image))
    return st


# --------------------------------------------------------------------------
# Snapshot assembly
# --------------------------------------------------------------------------
def _read_netdev(iface: str) -> Optional[Dict[str, Optional[int]]]:
    """Sysfs counters for the control binding.  Caveats travel with the
    data (see the Prometheus HELP text): counters zero on onic reload —
    which the wedge recovery performs on every JTAG swap — and EQDMA
    multi-queue loss never ticks them at all."""
    base = "/sys/class/net/%s/statistics" % iface
    if not os.path.isdir(base):
        return None
    out: Dict[str, Optional[int]] = {}
    for key in NETDEV_KEYS:
        try:
            with open(os.path.join(base, key)) as f:
                out[key] = int(f.read().strip())
        except (OSError, ValueError):
            out[key] = None
    return out


def _shell_id_from(reason: str) -> Optional[int]:
    """``static_shell_id`` from a probe reason string — probe_device only
    surfaces the value inside its reason (audit), so it is parsed back out
    rather than re-probed."""
    marker = "static_shell_id=0x"
    i = reason.find(marker)
    if i < 0:
        return None
    hex_part = reason[i + len(marker): i + len(marker) + 8]
    try:
        return int(hex_part, 16)
    except ValueError:
        return None


def _device_section(cfg) -> dict:
    """Probe → table status → perf, in that order, because silence is
    overloaded (audit): a missing reply means "child predates the kind"
    ONLY after a successful probe; before one it could equally be a wedged
    card or a swapped-out data plane, so nothing past a failed probe is
    read at all."""
    if cfg is None:
        return {"usable": False,
                "reason": "no DeviceConfig — host-only snapshot",
                "iface": None, "static_shell_id": None,
                "table": None, "perf": None}
    from . import device as _device
    usable, reason = _device.probe_device(cfg)
    sec: dict = {"usable": bool(usable), "reason": reason,
                 "iface": cfg.iface,
                 "static_shell_id": _shell_id_from(reason),
                 "table": None, "perf": None}
    if not usable:
        return sec

    st = _device.read_table_status(cfg, slot=1)
    if st is not None:
        sec["table"] = {
            "active_table_id": st.active_table_id,
            "shadow_table_id": st.shadow_table_id,
            "epoch": st.epoch,
            "status_flags": st.status_flags,
            "bytes_received": st.bytes_received,
            "capacity_states": st.capacity_states,
            "error": st.error,
            "active_valid": st.active_valid,
            "load_open": st.load_open,
            # bit3 clears on the next good commit; the engine's sticky
            # refused-commit flag (A_STATUS bit2) is not host-visible.
            "commit_err": bool(st.status_flags & 0x8),
        }
        # Overlay child: R45a counters exist as of 2026-07-31 (engine maps
        # 0x0058-0x0064; the wrapper's ST_PERF holds each address two
        # cycles, correct for this engine's registered CSR bus).  A child
        # built BEFORE that fix returns the CSR default (HARNESS_VER
        # 0x00020300) in every half through a one-cycle-early latch — a
        # constant we can fingerprint exactly, so stale garbage is nulled
        # rather than charted.
        _PRE_FIX_GARBAGE = (0x00020300 << 32) | 0x00020300
        pc = _device.read_perf_counters(cfg, slot=1)
        if pc is None:
            sec["perf"] = {
                "cycles": None, "bytes": None,
                "bytes_per_cycle": None, "throughput_mb_s": None,
                "note": "no PERF_REPLY (transient or non-resident slot)",
            }
        elif pc[0] == _PRE_FIX_GARBAGE and pc[1] == _PRE_FIX_GARBAGE:
            sec["perf"] = {
                "cycles": None, "bytes": None,
                "bytes_per_cycle": None, "throughput_mb_s": None,
                "note": ("overlay child predates the 2026-07-31 R45a fix: "
                         "PERF_REPLY is the HARNESS_VER constant, not a "
                         "measurement — reload the current partial"),
            }
        else:
            cycles, nbytes = pc
            bpc = (nbytes / cycles) if cycles else None
            sec["perf"] = {
                # Most-recent-scan only: the wrapper resets the counters
                # before every MATCH (W4).  CYCLES includes feed stalls —
                # it is the scan latency the host experiences, so at the
                # engine's ~8 cyc/B the throughput figure is honest, not
                # a datasheet number.
                "cycles": cycles, "bytes": nbytes,
                "bytes_per_cycle": None if bpc is None else round(bpc, 4),
                "throughput_mb_s": (None if bpc is None
                                    else round(bpc * 250e6 / 1e6, 2)),
            }
        return sec

    sec["table_note"] = ("no TABLE_STATUS_REPLY — resident child predates "
                         "A5 §3 (unknown kinds dropped, R78.4); expected "
                         "disposition, not a fault")
    pc = _device.read_perf_counters(cfg, slot=1)
    if pc is None:
        sec["perf"] = {
            "cycles": None, "bytes": None,
            "bytes_per_cycle": None, "throughput_mb_s": None,
            "note": ("counters unavailable (R78.11): child predates "
                     "v2.4.0 or slot not resident — not a fault"),
        }
    else:
        cycles, nbytes = pc
        bpc = (nbytes / cycles) if cycles else None
        sec["perf"] = {
            "cycles": cycles, "bytes": nbytes,
            "bytes_per_cycle": None if bpc is None else round(bpc, 4),
            # bytes/cycle × 250 MHz — most-recent-scan only (the wrapper
            # resets the counters before every scan, R78.11).
            "throughput_mb_s": (None if bpc is None
                                else round(bpc * 250e6 / 1e6, 2)),
        }
    return sec


def _wire_sched_section(cfg, device_sec: dict) -> dict:
    """WIRE-MAC RR scheduler state + per-program grant truth.

    The mode/quantum read is the REFUSAL PROBE (see
    :func:`pyro.macwire.read_sched`): an invalid ``SCHED_SET`` the
    wrapper refuses without touching any state, whose ack echoes the
    live values.  Grant truth is per-program, from the one place each
    is true: P0 from the wire counters (``seen == scanned + drops``,
    no slack), P1 from the MAC stats (``seen == digested + nonip +
    nokey``, no slack) — each program counts the wire frames granted
    to it, so the RR split IS ``p0.seen`` vs ``p1.seen``.  Whenever a
    number cannot be read honestly the section (or field) is absent
    with a reason, never fabricated; a device that ACKs the invalid
    probe with status 0 APPLIED it, and :func:`pyro.macwire.read_sched`
    raises rather than report that as truth (a snapshot that lies is
    worse than one that fails loudly).
    """
    if cfg is None:
        return {"available": False,
                "reason": "no DeviceConfig — host-only snapshot"}
    if not device_sec.get("usable"):
        return {"available": False,
                "reason": ("device probe failed — nothing past a "
                           "failed probe is read (silence is "
                           "overloaded)")}
    from . import device as _device
    from . import macwire as _macwire
    st = _macwire.read_sched(cfg, slot=1)
    if st is None:
        return {"available": False,
                "reason": ("no SCHED_ACK — resident child has no "
                           "WIRE-MAC scheduler (classic overlay child "
                           "drops the unknown kind, R78.4); expected "
                           "disposition, not a fault")}
    sec: dict = {
        "available": True,
        "mode": st.mode,
        "quantum": st.quantum,
        "mode_name": SCHED_MODE_NAMES.get(st.mode),
    }
    pc = _device.read_perf_counters(cfg, slot=1, with_wire=True)
    wire = pc[2] if pc is not None else None
    if wire is None:
        sec["p0_wire"] = {
            "seen": None, "scanned": None, "drops": None, "noms": None,
            "note": ("no wire counters (no PERF_REPLY, or child "
                     "pre-OQ-2) — P0 grant truth unavailable"),
        }
    else:
        sec["p0_wire"] = {"seen": wire.seen, "scanned": wire.scanned,
                          "drops": wire.drops, "noms": wire.noms}
    ms = _macwire.read_mac_stats(cfg, slot=1)
    if ms is None:
        sec["p1_mac"] = {
            "seen": None, "digested": None, "skip_nonip": None,
            "skip_nokey": None, "reports_sent": None,
            "records_lost": None,
            "note": ("no MAC_STAT_REPLY (transient) — P1 grant truth "
                     "unavailable"),
        }
    else:
        sec["p1_mac"] = {
            "seen": ms.seen, "digested": ms.digested,
            "skip_nonip": ms.skip_nonip, "skip_nokey": ms.skip_nokey,
            "reports_sent": ms.reports_sent,
            "records_lost": ms.records_lost,
        }
    total = (None if wire is None or ms is None
             else wire.seen + ms.seen)
    sec["derived"] = {
        "wire_total": total,
        # 0 granted frames: 0/0 has no honest value, so the shares are
        # null until a frame has been granted (never a fake 0).
        "p0_share": (None if not total
                     else round(wire.seen / total, 6)),
        "p1_share": (None if not total
                     else round(ms.seen / total, 6)),
        # Conservation residuals: healthy is EXACTLY 0; anything else
        # means torn counters or unaccounted frames.
        "zero_slack_residual_p0": (
            None if wire is None
            else wire.seen - wire.scanned - wire.drops),
        "zero_slack_residual_p1": (
            None if ms is None
            else ms.seen - ms.digested - ms.skip_nonip
            - ms.skip_nokey),
    }
    return sec


def collect_snapshot(cfg=None, state: Optional[TelemetryState] = None,
                     daemon=None, scheduler=None,
                     include_corpus: Optional[bool] = None,
                     rules_path: Optional[str] = None) -> dict:
    """One ``pyro-telemetry/1`` snapshot document.

    ``cfg=None`` is a complete host-only snapshot: the device section is
    present with ``usable: false`` and null table/perf, and nothing raises.
    ``state`` carries the cumulative host counters; without it the
    cumulative sections are zeros/nulls (a snapshot is still well-formed).
    ``daemon``/``scheduler`` optionally populate the scheduler section from
    the live SR10/SR19 objects.

    ``include_corpus`` controls the multi-second corpus computation:
    default (``None``) computes it exactly when hardware is in play
    (``cfg`` given) or the state carries an injected summary — so the four
    owner metrics are non-null whenever hardware is reachable (contract),
    while a host-only snapshot stays cheap.
    """
    view = state.view() if state is not None else TelemetryState().view()

    corpus: Optional[dict] = None
    if state is not None and state.corpus is not None:
        corpus = state.corpus
    else:
        if include_corpus is None:
            include_corpus = cfg is not None
        if include_corpus:
            corpus = corpus_summary(rules_path)

    device = _device_section(cfg)

    last_bytes = view["last_switch_bytes"]
    switch = {
        "last_ms": view["last_switch_ms"],
        "count": view["switch_count"],
        "history_ms": view["switch_history_ms"],
        "wire_ms_est": (None if last_bytes is None
                        else round(last_bytes / WIRE_BYTES_PER_S * 1e3, 6)),
        "pr_baseline_s": PR_BASELINE_S,
    }

    matching = {
        "scans": view["scans"],
        "nominations_total": view["nominations_total"],
        "by_sid": view["by_sid"],
        "ovf_events": view["ovf_events"],
        # Each OVF reply truncated at LEAST one entry beyond OUT_CAP=61;
        # the exact count lost is not observable (only the OVF bit crosses
        # the wire), so this is a certified lower bound, nothing more.
        "truncated_min": view["ovf_events"],
    }

    sent = view["requests_sent"]
    got = view["replies_received"]
    loss = {
        "netdev": (_read_netdev(cfg.iface)
                   if cfg is not None and getattr(cfg, "iface", None)
                   else None),
        "requests_sent": sent,
        "replies_received": got,
        "request_loss": (None if sent == 0
                         else round((sent - got) / sent, 6)),
    }

    resident_rules = view["resident_rules"]
    missed = {
        "resident_hard_misses": 0,
        "note": SR3_NOTE,
        "ovf_truncation_events": view["ovf_events"],
        "non_resident_rules": (None if corpus is None else
                               corpus["groupable_rules"] - resident_rules),
        "lowering_dropped": (None if corpus is None
                             else corpus["lowering_dropped"]),
        "coverage_resident_rules": resident_rules,
        "corpus_total_rules": (None if corpus is None
                               else corpus["corpus_total_rules"]),
    }

    sched = {
        "resident_group": view["resident_group"],
        "scores": None,
        "port_mix": None,
    }
    if scheduler is not None:
        try:
            sched["scores"] = dict(scheduler.scores())
        except Exception:
            sched["scores"] = None      # a torn scheduler never breaks a read
    if daemon is not None:
        try:
            sched["port_mix"] = dict(daemon.histogram.snapshot_by_class())
            if sched["resident_group"] is None:
                pipe = daemon._pipeline
                if pipe is not None:
                    sched["resident_group"] = pipe.group.name
        except Exception:
            sched["port_mix"] = None

    # The WIRE-MAC RR dispatch scheduler (specs/wire-mac-offload.md) is
    # a DIFFERENT scheduler from the SR10 residency one whose keys live
    # above, so it is namespaced under its own key rather than mixed in.
    sched["wire_mac"] = _wire_sched_section(cfg, device)

    return {
        "ts": time.time(),
        "schema": SCHEMA,
        "device": device,
        "switch": switch,
        "matching": matching,
        "loss": loss,
        "missed": missed,
        "scheduler": sched,
    }


# --------------------------------------------------------------------------
# Prometheus exposition
# --------------------------------------------------------------------------
def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    return "%.10g" % float(v)


def prometheus_text(snapshot: dict) -> str:
    """Prometheus exposition of a snapshot.  Null metrics are OMITTED, not
    zeroed — a scrape must never present "we did not measure this" as a
    measured 0 (the same honesty rule the missed section follows).  HELP
    text carries each metric's caveat, because a metric whose caveat is in
    a doc nobody reads WILL be misread (the netdev counters especially).
    """
    lines: List[str] = []

    def emit(name: str, mtype: str, help_text: str, value,
             labels: Optional[Dict[str, str]] = None) -> None:
        if value is None:
            return
        lines.append("# HELP %s %s" % (name, help_text))
        lines.append("# TYPE %s %s" % (name, mtype))
        if labels:
            lab = ",".join('%s="%s"' % (k, _esc(str(v)))
                           for k, v in sorted(labels.items()))
            lines.append("%s{%s} %s" % (name, lab, _fmt(value)))
        else:
            lines.append("%s %s" % (name, _fmt(value)))

    def emit_family(name: str, mtype: str, help_text: str,
                    samples: Dict[str, float], label: str) -> None:
        if not samples:
            return
        lines.append("# HELP %s %s" % (name, help_text))
        lines.append("# TYPE %s %s" % (name, mtype))
        for key in sorted(samples):
            if samples[key] is None:
                continue
            lines.append('%s{%s="%s"} %s'
                         % (name, label, _esc(str(key)), _fmt(samples[key])))

    dev = snapshot["device"]
    emit("pyro_device_usable", "gauge",
         "1 when the R84 ID probe passed (CAP_NET_RAW + valid ID_REPLY with "
         "matching SPEC16); every device metric below is meaningful only "
         "while this is 1 — silence past a failed probe is overloaded.",
         dev["usable"])
    tbl = dev.get("table")
    if tbl:
        emit("pyro_table_epoch", "gauge",
             "Commit generation counter; increments only on a CRC-verified "
             "commit, cumulative since engine reset. 0 = no valid table "
             "ever committed.", tbl["epoch"])
        emit("pyro_table_active_valid", "gauge",
             "TBL_STATUS bit2: a committed table is resident and scanning.",
             tbl["active_valid"])
        emit("pyro_table_load_open", "gauge",
             "TBL_STATUS bit1: a table transfer is open (shadow filling).",
             tbl["load_open"])
        emit("pyro_table_commit_err", "gauge",
             "TBL_STATUS bit3: the LAST commit was refused (fail-closed, "
             "old table untouched). Clears on the next good commit; the "
             "engine's sticky refused flag is not host-visible.",
             tbl["commit_err"])
        emit("pyro_table_bytes_received", "gauge",
             "Bytes accepted into the open transfer; also the sequential-"
             "delivery gate (a chunk at any other offset is rejected, "
             "error 8).", tbl["bytes_received"])
        emit("pyro_table_capacity_states", "gauge",
             "Engine MAX_STATES — a build constant, not a measurement; "
             "<39647 identifies a non-full-corpus build.",
             tbl["capacity_states"])
    perf = dev.get("perf") or {}
    emit("pyro_perf_cycles", "gauge",
         "R45a cycle count of the MOST RECENT scan only (the wrapper "
         "resets counters before every scan). Overlay-child CYCLES "
         "include feed stalls (~8-10 cyc/B); omitted only against a "
         "pre-2026-07-31 overlay child (fingerprinted constant).",
         perf.get("cycles"))
    emit("pyro_perf_bytes", "gauge",
         "R45a bytes the engine consumed in the most recent scan (post "
         "length clamp). Same availability caveats as pyro_perf_cycles.",
         perf.get("bytes"))
    emit("pyro_perf_bytes_per_cycle", "gauge",
         "bytes/cycles of the last scan (~1 B/cyc/core for generated "
         "group engines; ~0.1-0.12 for the overlay child).",
         perf.get("bytes_per_cycle"))
    emit("pyro_perf_throughput_mb_s", "gauge",
         "bytes_per_cycle x 250 MHz, MB/s — utilization of the last scan.",
         perf.get("throughput_mb_s"))

    sw = snapshot["switch"]
    emit("pyro_switch_last_ms", "gauge",
         "Wall-clock ms of the last successful table swap, host-timed "
         "around load_table (includes per-chunk round-trips + host CRC). "
         "Anchor: 12.3 ms for 2564 B.", sw["last_ms"])
    emit("pyro_switch_count", "counter",
         "Successful table swaps recorded this process (failed/refused "
         "commits are not switches and are not counted).", sw["count"])
    emit("pyro_switch_wire_ms_est", "gauge",
         "Estimated pure wire time of the last image at the measured "
         "2.3 GiB/s — the lower bound the measured swap time approaches.",
         sw["wire_ms_est"])
    emit("pyro_switch_pr_baseline_s", "gauge",
         "Measured JTAG PR reconfiguration baseline (13.6 s), the cost a "
         "table swap replaces.", sw["pr_baseline_s"])

    m = snapshot["matching"]
    emit("pyro_scans_total", "counter",
         "MATCH round-trips that received a reply.", m["scans"])
    emit("pyro_nominations_total", "counter",
         "Rule nominations attributed host-side: one per gid:sid per match "
         "entry (a slot nominates EVERY rule sharing its anchor — SR3).",
         m["nominations_total"])
    emit_family("pyro_nominations_by_sid", "counter",
                "Nominations per rule, keyed gid:sid via the resident "
                "group's sidecar. Host-aggregated: the fabric keeps no "
                "per-slot hit counters.", m["by_sid"], "sid")
    emit("pyro_ovf_events_total", "counter",
         "Replies with the OVF bit set: the engine truncated at "
         "OUT_CAP=61 entries; count==61 with OVF means 'at least 61'.",
         m["ovf_events"])
    emit("pyro_truncated_min", "counter",
         "Certified LOWER BOUND on match entries lost to truncation "
         "(>=1 per OVF reply; the exact loss is not wire-observable).",
         m["truncated_min"])

    loss = snapshot["loss"]
    nd = loss.get("netdev") or {}
    nd_help = {
        "rx_dropped": "netdev rx_dropped",
        "rx_errors": "netdev rx_errors",
        "rx_missed_errors": "netdev rx_missed_errors",
        "tx_dropped": "netdev tx_dropped",
        "rx_packets": "netdev rx_packets (denominator)",
        "tx_packets": "netdev tx_packets (denominator)",
    }
    for key in NETDEV_KEYS:
        emit("pyro_netdev_%s" % key, "counter",
             nd_help[key] + " on the control binding. CAVEATS: zeroes on "
             "onic reload (every JTAG swap — deltas spanning a swap are "
             "invalid) and EQDMA multi-queue loss never ticks it "
             "(measured up to 13% silent loss; bench with "
             "PYRO_BENCH_QUEUES=1).", nd.get(key))
    emit("pyro_requests_sent_total", "counter",
         "MATCH requests sent by this process's telemetry-tracked paths.",
         loss["requests_sent"])
    emit("pyro_replies_received_total", "counter",
         "MATCH replies received; sent minus received is the only "
         "request-loss signal — the device cannot report frames that "
         "never arrived.", loss["replies_received"])
    emit("pyro_request_loss_ratio", "gauge",
         "(sent - replied) / sent. Omitted until a request has been sent.",
         loss["request_loss"])

    ms = snapshot["missed"]
    emit("pyro_missed_resident_hard", "gauge",
         "Hard misses among resident rules. " + SR3_NOTE + ".",
         ms["resident_hard_misses"])
    emit("pyro_missed_ovf_truncation_events", "counter",
         "OVF truncation events (see pyro_truncated_min): the countable "
         "component of 'rules missed'.", ms["ovf_truncation_events"])
    emit("pyro_missed_nonresident_rules", "gauge",
         "Groupable rules NOT currently resident — the coverage gap the "
         "SR10 scheduler manages; Snort still sees this traffic (SR5).",
         ms["non_resident_rules"])
    emit("pyro_missed_lowering_dropped", "gauge",
         "Rules the compile chain cannot express at all (always-forward "
         "tier + anchor rules without a parseable sid).",
         ms["lowering_dropped"])
    emit("pyro_coverage_resident_rules", "gauge",
         "Rules covered by the resident group right now.",
         ms["coverage_resident_rules"])
    emit("pyro_corpus_total_rules", "gauge",
         "Total parsed corpus rules (SF15 totality: parse failures triage "
         "to always-forward, never exceptions).", ms["corpus_total_rules"])

    sched = snapshot["scheduler"]
    if sched.get("scores"):
        emit_family("pyro_scheduler_score", "gauge",
                    "SR10 residency value V(g) = sum over ports of decayed "
                    "bytes x rules of g that could fire there.",
                    sched["scores"], "group")
    if sched.get("port_mix"):
        emit_family("pyro_port_mix_bytes", "gauge",
                    "Exponentially-decayed byte mix per port class (SR19 "
                    "display view; scheduling uses the per-port form).",
                    sched["port_mix"], "class")

    wm = sched.get("wire_mac") or {}
    if wm.get("available"):
        def by_program(p0, p1):
            # emit_family emits HELP/TYPE for any non-empty dict, so
            # null samples are dropped BEFORE the call (a family with
            # no samples must not appear at all).
            return {k: v for k, v in (("p0", p0), ("p1", p1))
                    if v is not None}

        emit("pyro_sched_mode", "gauge",
             "WIRE-MAC dispatch mode (0 p0_only, 1 p1_only, "
             "2 round_robin, 3 broadcast), read via the SCHED_SET "
             "refusal probe: an invalid mode (0xFF) is refused, "
             "nothing is written, the rotation is untouched, and the "
             "ACK echoes live state.", wm.get("mode"))
        emit("pyro_sched_quantum", "gauge",
             "RR quantum (consecutive wire frames granted per program "
             "per turn), same refusal-probe read.", wm.get("quantum"))
        p0 = wm.get("p0_wire") or {}
        p1 = wm.get("p1_mac") or {}
        emit_family("pyro_sched_seen_total", "counter",
                    "Wire frames GRANTED to each program (p0 from the "
                    "wire counters, p1 from the MAC stats — each "
                    "program counts its own grants, so the RR split is "
                    "p0 vs p1 of this family).",
                    by_program(p0.get("seen"), p1.get("seen")),
                    "program")
        emit("pyro_wire_scanned_total", "counter",
             "P0 wire frames scanned (seen == scanned + drops holds "
             "with no slack).", p0.get("scanned"))
        emit("pyro_wire_drops_total", "counter",
             "P0 wire frames dropped before scan.", p0.get("drops"))
        emit("pyro_wire_noms_total", "counter",
             "P0 wire-scan nominations.", p0.get("noms"))
        emit("pyro_mac_digested_total", "counter",
             "P1 frames digested (seen == digested + skip_nonip + "
             "skip_nokey holds with no slack).", p1.get("digested"))
        emit("pyro_mac_skip_nonip_total", "counter",
             "P1 frames skipped: non-IP EtherType (exists only once a "
             "key is live — MR15).", p1.get("skip_nonip"))
        emit("pyro_mac_skip_nokey_total", "counter",
             "P1 frames skipped: no committed key (fail-closed — EVERY "
             "wire frame counts here until a key is live).",
             p1.get("skip_nokey"))
        emit("pyro_mac_reports_sent_total", "counter",
             "MAC_REPORT frames the device emitted.",
             p1.get("reports_sent"))
        emit("pyro_mac_records_lost_total", "counter",
             "MAC records lost to report backpressure — healthy is 0.",
             p1.get("records_lost"))
        der = wm.get("derived") or {}
        emit_family("pyro_sched_share", "gauge",
                    "Per-program fraction of all granted wire frames "
                    "(0..1); omitted until any frame has been granted "
                    "(0/0 has no honest value).",
                    by_program(der.get("p0_share"),
                               der.get("p1_share")), "program")
        emit_family("pyro_sched_zero_slack_residual", "gauge",
                    "Per-program conservation residual — healthy is "
                    "EXACTLY 0 (p0: seen-scanned-drops; p1: seen-"
                    "digested-nonip-nokey); nonzero means torn "
                    "counters or unaccounted frames.",
                    by_program(der.get("zero_slack_residual_p0"),
                               der.get("zero_slack_residual_p1")),
                    "program")

    return "\n".join(lines) + "\n"
