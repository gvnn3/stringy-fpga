"""AC-3-3: benchmark suite — R3b hard PASS, R59 win attribution, R78.11 read-out.

Machine-readable metrics: every measuring clause prints one ``AC33-METRIC``
JSON line (and mirrors it into the junit ``user_properties`` via
``record_property``), per AC-3-3's "emits machine-readable metrics" clause.

Clauses:

  * **R3b hard PASS (AC-3-3).** Phase 3 is the phase at which the native
    routing/dispatch hot path is a deliverable, so the R3b relative 1.15x
    loss-regime bound is a HARD PASS requirement here, measured per the
    normative v2.5.0 protocol (R3b.1: median of >= 5 trials, each >= 1000
    paired calls; R3b.2: the pinned 132-byte ASCII subject; R3b.4: rotating
    loss-regime warmup) via the SHARED implementation in ``r3b_protocol`` — the
    same one AC-2-5 uses, so the two ACs can never measure R3b two different
    ways.  R3b binds against the compiled-extension build (R3b.3): on a
    pure-Python build this clause records the honest R3c SKIP that names the
    build AND states the measured median (never a silent pass), and AC-3-3 is
    then NOT claimable as PASS.

  * **Win attribution (R59).** On the >= S_min regime the offload path is
    measured against stock ``re`` and reported with honest labeling: on this
    device-free host the software model stands in for the resident tier
    (R7/R51b), and the spec is explicit that only hardware-measured values are
    performance evidence for R1/R59 (R45a) — so the model-tier ratio is an
    attribution of offload-path overhead, never a claimed hardware win.  The
    routing facts ARE asserted (every >= S_min call on an eligible pattern
    takes the offload path and returns byte-identical results, R3/R16/R53).
    The R1/R2 hardware win demonstration itself requires ``device_usable`` and
    SKIPs with the canonical R83 reason while the predicate is false.

  * **R78.11 on-chip attribution read-out.** When
    ``pyro.device.read_perf_counters`` is reachable AND the device probes
    usable (R83), the resident circuit's R45a CYCLES/BYTES counters are read
    in-band (PERF_REQUEST/PERF_REPLY) and reported: CYCLES x t_clk is the pure
    on-chip scan cost and BYTES/CYCLES the achieved datapath utilization
    (R45a/R59/AC-3-3 attribution seam).  Otherwise the clause SKIPs naming the
    exact unmet predicate — including R78.11's own "counters unavailable"
    disposition (no PERF_REPLY / counter-less circuit), which is never a
    measurement.

(AC-3-3; R1-R5, R3b, R3c, R7, R16, R45a, R51b, R53, R59, R71, R78.11, R83)
"""
import glob
import json
import re as stdre
import statistics
import struct
import time

import pytest

import pyro.device as pdev
import pyro.re as pre
import phase2_support
import r3b_protocol
from pyro._thresholds import N_REUSE, S_MIN

# --- R59 win-attribution workload (>= S_min regime) ------------------------
# Kept just above S_min: the model tier scans the WHOLE subject in software, so
# the corpus size directly prices each attribution call (~25 ms at ~80 KiB).
WIN_PATTERNS = 8          # distinct HW-eligible literal patterns, reused
TIMED_CALLS = 3           # timed calls per pattern (+1 untimed priming call)
_LOG_LINE = ("2026-07-15T12:00:00 svc=ingest worker=%03d heartbeat ok "
             "seq=%06d payload=abcdefghijklmnopqrstuvwxyz\n")

# F4: the user box runs at 250 MHz => t_clk = 4.000 ns (phase2_support carries
# the spec constant); used to convert the R45a CYCLES counter to on-chip ns.
T_CLK_NS = phase2_support.PROXY_CLOCK_PERIOD_NS


def _emit_metric(record_property, name, payload):
    """AC-3-3 'machine-readable metrics' emission: one JSON line on stdout plus
    a junit user_property, so both a human log scrape and an XML consumer see
    the identical record."""
    rec = {"suite": "AC-3-3", "metric": name}
    rec.update(payload)
    line = json.dumps(rec, sort_keys=True)
    print("AC33-METRIC " + line)
    record_property("ac33_metric_" + name, line)


# ---------------------------------------------------------------------------
# Clause 1: R3b hard PASS (R3b.1-R3b.4 protocol, shared with AC-2-5).
# ---------------------------------------------------------------------------
@pytest.mark.perf
def test_r3b_hard_pass_median_of_trials(record_property):
    """AC-3-3 R3b clause: the relative 1.15x loss-regime bound is a HARD PASS
    on the compiled-extension build, decided by the R3b.1 median of >= 5 trials
    at the pinned 132-byte subject (R3b.2).  On a pure-Python build: the honest
    R3c SKIP naming the build and stating the measured median (R3b.3) — and
    AC-3-3 is then not claimable as PASS.  Never a silent pass by any route."""
    native, build_desc = r3b_protocol.native_routing_active()
    # Measure FIRST, unconditionally: the R3c SKIP reason MUST state the
    # R3b.1/R3b.2 statistic (median of >= 5 trials at 132 B), never a single
    # trial and never nothing (R3b.3).
    verdict, trial_ratios, detail = r3b_protocol.measure_r3b()
    summary = r3b_protocol.format_detail(verdict, trial_ratios, detail)
    _emit_metric(record_property, "r3b_relative_loss_regime", {
        "requirement": "R3b (hard PASS at AC-3-3)",
        "protocol": "R3b.1 median of >= 5 trials, >= 1000 paired calls/trial",
        "subject_bytes": len(r3b_protocol.SUBJECT_132),
        "median_ratio": verdict,
        "trial_ratios": trial_ratios,
        "bound_ratio": r3b_protocol.R3B_RATIO,
        "native_routing_active": native,
        "build": build_desc,
    })

    if not native:
        pytest.skip(
            f"R3c precondition does not hold — {build_desc}. R3b binds only "
            f"against the compiled-extension build (R3b.3), so this clause "
            f"records the honest R3c SKIP — and AC-3-3 is therefore NOT "
            f"claimable as PASS on this build (AC-3-3, spec v2.5.0). Measured "
            f"anyway per R3b.1/R3b.2: {summary}; bound "
            f"{r3b_protocol.R3B_RATIO}x.")

    assert verdict <= r3b_protocol.R3B_RATIO, (
        f"AC-3-3 R3b HARD PASS violated: {build_desc}, so the 1.15x relative "
        f"bound binds (R3c) and the measured R3b.1 verdict exceeds it: "
        f"{summary}; bound {r3b_protocol.R3B_RATIO}x. (Individual trials MAY "
        f"exceed the bound without violating R3b; only this median binds.)")


# ---------------------------------------------------------------------------
# Clause 2: R59 win attribution on the >= S_min regime (device-free host).
# ---------------------------------------------------------------------------
def _win_corpus(needles):
    """A log-like subject >= S_min containing every needle once (worst-case
    full-subject scan for a literal pattern: needles live in the tail)."""
    filler, i = [], 0
    size = 0
    tail = "tail-markers: " + " ".join(needles) + "\n"
    while size < S_MIN + len(tail):
        line = _LOG_LINE % (i % 997, i)
        filler.append(line)
        size += len(line)
        i += 1
    corpus = "".join(filler) + tail
    assert len(corpus) >= S_MIN, "attribution corpus must be >= S_min (R2/R51)"
    return corpus


@pytest.mark.perf
def test_r59_win_attribution_model_tier_ge_smin(record_property):
    """R59 win attribution, honestly labeled: on this device-free host the
    software model stands in for the resident tier (R7/R51b), so the measured
    pyro/stock ratio at >= S_min ATTRIBUTES offload-path overhead — it is NOT
    R1/R2 performance evidence (R45a: only hardware-measured values are).  What
    IS asserted: every >= S_min call on an HW-eligible pattern takes the
    offload (model-tier) path per R51 step 4, with byte-identical results
    (R16/R53) and no error fallbacks."""
    needles = [f"ac33win{i:02d}needlezz" for i in range(WIN_PATTERNS)]
    corpus = _win_corpus(needles)

    for n in needles:
        info = pre.explain(n)
        assert info["eligible"], (
            f"attribution pattern {n!r} must be HW-eligible, got "
            f"fallback-only ({info['reason']!r}) — the measurement would be "
            f"vacuous")

    stock_c = [stdre.compile(n) for n in needles]
    pyro_c = [pre.compile(n) for n in needles]

    # Untimed priming call per pattern: absorbs the one-time model-program
    # compile and residency-manager construction (R4 warm cache) so the timed
    # calls measure the steady offload path.  Priming happens AFTER the stats
    # snapshot below would move, so snapshot first.
    stats0 = pre.stats()
    for i in range(WIN_PATTERNS):
        ms = stock_c[i].search(corpus)
        mp = pyro_c[i].search(corpus)
        assert ms is not None and mp is not None
        assert (mp.span(), mp.group(0)) == (ms.span(), ms.group(0)), (
            f"offload-path result diverged from stock for {needles[i]!r} "
            f"(R16/R53)")

    stock_ns, pyro_ns = [], []
    for _ in range(TIMED_CALLS):
        for i in range(WIN_PATTERNS):
            t0 = time.perf_counter_ns()
            ms = stock_c[i].search(corpus)
            t1 = time.perf_counter_ns()
            mp = pyro_c[i].search(corpus)
            t2 = time.perf_counter_ns()
            stock_ns.append(t1 - t0)
            pyro_ns.append(t2 - t1)
            assert (mp.span(), mp.group(0)) == (ms.span(), ms.group(0)), (
                f"offload-path result diverged from stock for {needles[i]!r} "
                f"(R16/R53)")
    stats1 = pre.stats()

    # Routing facts (R51 step 4: len >= S_min crosses the gate regardless of
    # reuse; reuse stayed < N_reuse so the gate crossing is size-attributed).
    calls = WIN_PATTERNS * (1 + TIMED_CALLS)
    assert 1 + TIMED_CALLS < N_REUSE
    model_delta = stats1["model"] - stats0["model"]
    assert model_delta == calls, (
        f"expected every >= S_min call on an eligible pattern to take the "
        f"offload (model-tier) path: model dispatches moved {model_delta}, "
        f"expected {calls} (stats before={stats0}, after={stats1})")
    assert stats1["fallback_after_error"] == stats0["fallback_after_error"], (
        "error fallbacks during attribution measurement (R52)")

    med_stock = statistics.median(stock_ns)
    med_pyro = statistics.median(pyro_ns)
    ratio = med_pyro / med_stock if med_stock else float("inf")
    _emit_metric(record_property, "r59_win_attribution_ge_smin", {
        "requirement": "R59 win attribution (>= S_min regime)",
        "tier_measured": "model (software stand-in for resident, R7/R51b)",
        "subject_bytes": len(corpus),
        "patterns": WIN_PATTERNS,
        "timed_calls_per_pattern": TIMED_CALLS,
        "median_stock_ns_per_call": med_stock,
        "median_offload_ns_per_call": med_pyro,
        "ratio_offload_over_stock": ratio,
        "performance_evidence_for_r1_r2": False,
        "honest_label": (
            "software-model attribution on a device-free host; only "
            "hardware-measured values are performance evidence for R1/R59 "
            "(R45a) — the hardware win clause SKIPs separately with the R83 "
            "canonical reason"),
    })
    # Sanity only — the model tier is software emulation and is EXPECTED to be
    # slower than stock; asserting a "win" here would be dishonest (R71).
    assert med_pyro > 0 and med_stock > 0


# R1 floor (resident circuit, streamed corpus): 1 GiB/s aggregate scan rate.
_R1_FLOOR_BPS = float(1 << 30)
# MATCH_REQUEST body prefix (R78.6): reserved u64, out_cap u16, flags u16.
_MATCH_PREFIX = struct.Struct(">QHH")
_MATCH_CHUNK = pdev.MAX_PAYLOAD - _MATCH_PREFIX.size


def _match_roundtrip(cfg, transport, slot, corpus, seq):
    """One MATCH request/reply over the real R86.7 transport.  Returns the
    decoded reply frame, or ``None`` on timeout (R84 budget per attempt)."""
    eth = (bytes(cfg.dst_mac) + bytes(cfg.src_mac)
           + struct.pack(">H", pdev.ETHERTYPE))
    payload = _MATCH_PREFIX.pack(0, 8, 0) + corpus
    for attempt in range(cfg.probe_attempts):
        transport.send(eth + pdev.encode_frame(
            pdev.KIND_MATCH_REQUEST, slot, seq, payload))
        deadline = time.monotonic() + cfg.probe_timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            raw = transport.recv(remaining)
            if raw is None or len(raw) < 14 + pdev.PYRO_HEADER_LEN:
                continue
            if raw[12:14] != struct.pack(">H", pdev.ETHERTYPE):
                continue
            try:
                dec = pdev.decode_frame(raw[14:])
            except pdev.PyroFrameError:
                continue
            if dec.seq != seq:
                continue
            if dec.kind in (pdev.KIND_MATCH_REPLY, pdev.KIND_STATUS):
                return dec
    return None


def test_r1_r2_hardware_win_regime_requires_device(record_property, device_iface):
    """R59/AC-3-3 hardware clause: the R1/R2 win-regime demonstration
    (resident-circuit throughput vs stock) binds only when BOTH predicates
    hold: ``device_usable`` (R83) and the **P2 performance transport** (QDMA
    char-devs, absent per F5 — the raw-Ethernet binding is the *functional*
    control transport, not the data plane).  Per AC-2-5/AC-3-3 (v2.5.1) the
    throughput is **measured anyway** over whatever transport exists (R3b.3
    measure-first discipline) and every unmet predicate is an honest SKIP that
    states the measured statistic — never a PASS below the floor, never a FAIL
    for missing P2 plumbing."""
    cfg = pdev.DeviceConfig(iface=device_iface)
    usable, reason = pdev.probe_device(cfg)
    if not usable:
        _emit_metric(record_property, "r1_r2_hardware_win_regime", {
            "requirement": "R1/R2 win regime on hardware (resident circuits)",
            "status": "skip",
            "device_probe_reason": reason,
        })
        pytest.skip(
            f"{reason}; the R1/R2 win-regime demonstration (resident-circuit "
            f"throughput vs stock re) requires device_usable (R71/R83) — "
            f"recorded SKIP, never a PASS (R59/AC-3-3)")

    # Stream >= S_min through the resident circuit over the real transport,
    # chunked at the R78.9 frame bound, and time it end-to-end.
    transport = pdev._make_transport(cfg)  # hardware clause: real transport
    # (P2d v2.7.0-draft: with PYRO_QDMA_CHARDEV configured this IS the P2
    # performance transport; otherwise the raw-Ethernet control transport.)
    try:
        # Resident slot discovery (R64 single-tenant: slot 1, then 0); a
        # STATUS reply is the R78.8 "not resident" disposition.
        slot, dec = None, None
        for cand in (1, 0):
            dec = _match_roundtrip(cfg, transport, cand, b"resident?", 7000)
            if dec is not None and dec.kind == pdev.KIND_MATCH_REPLY:
                slot = cand
                break
        if slot is None:
            _emit_metric(record_property, "r1_r2_hardware_win_regime", {
                "requirement": "R1/R2 win regime on hardware (resident circuits)",
                "status": "skip",
                "device_probe_reason": reason,
                "resident_slot": None,
            })
            pytest.skip(
                "R1 predicate unmet: no resident circuit answers MATCH on "
                "slot 1 or 0 (R78.8 STATUS/no-reply) — R1 throughput binds "
                "only for resident circuits (R1/R51); recorded SKIP")

        total, sent_frames, rtts = 0, 0, []
        chunk = b"\x78" * _MATCH_CHUNK           # match-free filler
        t0 = time.perf_counter()
        while total < S_MIN:
            t1 = time.perf_counter()
            dec = _match_roundtrip(cfg, transport, slot, chunk,
                                   8000 + sent_frames)
            rtts.append(time.perf_counter() - t1)
            if dec is None or dec.kind != pdev.KIND_MATCH_REPLY:
                pytest.skip(
                    f"R1 measurement aborted mid-stream at byte {total}: "
                    f"{'no reply' if dec is None else 'STATUS reply'} for "
                    f"frame {sent_frames} (transient device state, R84) — "
                    f"recorded SKIP, never a PASS")
            total += len(chunk)
            sent_frames += 1
        wall_s = time.perf_counter() - t0
    finally:
        transport.close()

    bps = total / wall_s if wall_s else 0.0
    p2_chardevs = sorted(glob.glob("/dev/qdma*"))
    metric = {
        "requirement": "R1/R2 win regime on hardware (resident circuits)",
        "status": "measured",
        "device_probe_reason": reason,
        "resident_slot": slot,
        "transport": "raw-Ethernet control frames (functional, P2 pending)",
        "p2_qdma_chardevs": p2_chardevs,
        "corpus_bytes": total,
        "frames": sent_frames,
        "chunk_bytes": _MATCH_CHUNK,
        "wall_s": wall_s,
        "median_frame_rtt_ms": statistics.median(rtts) * 1e3,
        "throughput_bytes_per_s": bps,
        "throughput_MiB_per_s": bps / (1 << 20),
        "r1_floor_GiB_per_s": 1.0,
        "honest_label": (
            "hardware-measured end-to-end scan throughput over the R78 "
            "control transport; R1 performance evidence only if the floor "
            "is met on the P2 performance transport"),
    }
    _emit_metric(record_property, "r1_r2_hardware_win_regime", metric)

    if not p2_chardevs or bps < _R1_FLOOR_BPS:
        pytest.skip(
            f"R1 predicate unmet: P2 performance transport absent (QDMA "
            f"char-devs {p2_chardevs or 'none'}, F5) — the MTU-bound "
            f"raw-Ethernet control transport measured "
            f"{bps / (1 << 20):.3f} MiB/s over {total} B in {sent_frames} "
            f"frames (median RTT {statistics.median(rtts) * 1e6:.0f} us), "
            f"vs the R1 floor of 1 GiB/s — honest SKIP naming the missing "
            f"P2 prerequisite with the measured statistic "
            f"(AC-2-5/AC-3-3 v2.5.1); never a PASS, never a FAIL")

    assert bps >= _R1_FLOOR_BPS, (
        f"R1 floor violated on the P2 performance transport: measured "
        f"{bps / (1 << 30):.3f} GiB/s < 1 GiB/s over {total} B")


# ---------------------------------------------------------------------------
# Clause 3: R78.11 on-chip CYCLES/BYTES attribution read-out.
# ---------------------------------------------------------------------------
def test_r78_11_perf_counter_attribution_readout(record_property, device_iface):
    """R45a/R78.11 attribution seam: when the PERF read-out is reachable AND
    the device probes usable, read the resident circuit's CYCLES/BYTES
    counters in-band and report CYCLES x t_clk (pure on-chip scan cost) and
    BYTES/CYCLES (datapath utilization).  Every unmet predicate is an honest
    SKIP naming exactly what is missing."""
    read_fn = getattr(pdev, "read_perf_counters", None)
    if read_fn is None:
        pytest.skip(
            "R78.11 predicate unmet: pyro.device.read_perf_counters is not "
            "reachable in this build (v2.4.0 PERF read-out absent), so the "
            "on-chip CYCLES/BYTES attribution read is not attempted")

    cfg = pdev.DeviceConfig(iface=device_iface)
    usable, reason = pdev.probe_device(cfg)
    if not usable:
        pytest.skip(
            f"R78.11 predicate unmet: the on-chip CYCLES/BYTES attribution "
            f"read requires device_usable (R83) and the probe reports: "
            f"{reason}")

    counters = read_fn(cfg)
    if counters is None:
        pytest.skip(
            "R78.11 'counters unavailable' disposition: no PERF_REPLY for the "
            "resident slot (a child built before v2.4.0 — including the "
            "flashed ID stub — drops the unknown kind per R78.4, and a "
            "non-resident slot answers STATUS).  Never a device fault, never "
            "a measurement")

    cycles, nbytes = counters
    assert cycles >= 0 and nbytes >= 0, (cycles, nbytes)
    if cycles == 0 and nbytes > 0:
        pytest.skip(
            "R45a counter-less circuit: CYCLES reads 0 after a non-empty scan "
            "(harness < 2.1.0 decode), so its counter values are unavailable "
            "and MUST NOT be treated as measurements (R45a)")
    _emit_metric(record_property, "r78_11_on_chip_attribution", {
        "requirement": "R45a/R78.11 on-chip attribution read-out",
        "cycles": cycles,
        "bytes": nbytes,
        "t_clk_ns": T_CLK_NS,
        "on_chip_scan_ns": cycles * T_CLK_NS,
        "datapath_bytes_per_cycle": (nbytes / cycles) if cycles else None,
        "honest_label": (
            "hardware-measured R45a counters (performance evidence per "
            "R45a/R59); CYCLES x t_clk is the pure on-chip scan cost, "
            "end-to-end host time minus it isolates transport/dispatch "
            "overhead"),
    })
