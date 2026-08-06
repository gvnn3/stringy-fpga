#!/usr/bin/env python3
"""AC-M3 multi-program MAC+wire child on silicon: load the partial,
prove keying, scheduling, and P0/P1 coexistence on the card.

Order of operations is safety-first and the order IS the experiment:

  0  pre-flight: the partial exists and is non-zero size (PR-build
     outputs are crash-fragile), and probe_device says the wiretap
     static (SPEC16 0x0202) answers.
  1  CMAC loopback OFF.  Never JTAG-load with wire traffic running;
     the generator would be feeding a half-configured region.
  2  load_partial (JTAG + R85a in-band recovery, ~14-16 s), unless
     --skip-load says the child is already resident.
  3  identity: the ID probe's rp_child_id must equal the multi-MAC
     child's 0x01005E0A before anything is trusted.
  4  MAC_STAT baseline: the six counters obey the zero-slack
     invariant (seen == digested + skip_nonip + skip_nokey) EXACTLY;
     after RESET_AFTER_RECONFIG they must all be zero.
  5  scheduler: SCHED_SET rr quantum 1 acks (mode 2, quantum 1,
     status 0); a deliberately bad mode acks with nonzero status;
     a follow-up good SCHED_SET confirms the settings survived.
  6  keying — the silicon rekey regression (the fixed blocker):
     KEY_A commits and the MAC_KEY_ACK keycheck equals
     pyro.macwire.key_check(KEY_A) exactly; re-key KEY_B yields the
     NEW keycheck; back to KEY_A yields KEY_A's again.  Any mismatch
     means the device digests under a key the host does not hold.
  7  P0 nominations: a minimal A5 table (the alphabetic pattern
     b"MALWARE" — tx_gen frames are seq bytes + zero padding, so no
     wire frame can ever match it and flood the link), committed at
     epoch 1; a host MATCH_REQUEST containing the pattern nominates
     with the committed epoch echoed.
  8  wire window: loopback ON for --window seconds; DURING the
     window another MATCH_REQUEST must still nominate — the AC-M3
     clause: nominations flow WHILE RR dispatches wire frames.
     Deltas across the window: both zero-slack invariants EXACT
     (P1 seen == digested + skip_nonip + skip_nokey; P0 wire
     seen == scanned + drops).  tx_gen frames carry ethertype
     0x88B5 and are non-IP, so the P1 digested delta is 0 and the
     skip_nonip delta equals the P1 seen delta.  MAC_REPORT records
     are silicon-untestable in loopback (nothing IP ever crosses
     the wire), an honest limitation this script prints.  RR
     fairness: each program's wire-seen delta lands within
     [0.4, 0.6] of their sum (packet-atomic RR, quantum 1).
  9  epilogue: loopback OFF (left off), final stats, one
     AC_M3_<check> line per step and AC_M3_RESULT last.

Usage:
    PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 \\
        scripts/pyro_mac_silicon.py [--window 1.0] [--skip-load]
"""
import argparse
import os
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))

import pyro.device as pdev                      # noqa: E402
import pyro.macwire as mw                       # noqa: E402
from pyro.overlay import table as otable        # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARTIAL = os.path.join(REPO, "hw", "dfx", "build-wiretap", "partials",
                       "multi_mac_wire.bit")
LOOPBACK = os.path.join(REPO, "scripts", "pyro_cmac_loopback.py")
EXPECT_CHILD_ID = 0x01005E0A
SLOT = 1
ENGINE_ID = 0x0A5E0001
KEY_A = bytes(range(16))            # the SipHash paper key
KEY_B = bytes(range(15, -1, -1))
PATTERN = b"MALWARE"
SUBJECT = b"quarantine the MALWARE sample before it spreads"
MAXENT = 61


def loopback(*args):
    r = subprocess.run(["sudo", "-n", LOOPBACK, *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("loopback %s FAILED:\n%s%s" % (args, r.stdout, r.stderr))
        sys.exit(1)


def roundtrip(cfg, kind, payload, timeout_s=2.0, seq=99, slot=SLOT):
    r = pdev._make_transport(cfg)
    try:
        eth = (bytes(cfg.dst_mac) + bytes(cfg.src_mac)
               + struct.pack(">H", pdev.ETHERTYPE))
        r.send(eth + pdev.encode_frame(kind, slot, seq, payload))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            raw = r.recv(0.5)
            if raw is None:
                continue
            raw = bytes(raw)
            if len(raw) < 14 or struct.unpack(
                    ">H", raw[12:14])[0] != pdev.ETHERTYPE:
                continue
            try:
                dec = pdev.decode_frame(raw[14:])
            except pdev.PyroFrameError:
                continue
            # Unsolicited frames carry AUTONOMOUS seq counters that can
            # collide with ours; skip them by kind/origin BEFORE the seq
            # match, exactly as pyro.macwire._mac_request does: MAC_REPORT
            # (0x0E), and wire-origin MATCH_REPLYs (payload status bit2,
            # R78.13 — the mid-window request would otherwise take one).
            if dec.kind == pdev.KIND_MAC_REPORT:
                continue
            if (dec.kind == pdev.KIND_MATCH_REPLY
                    and len(dec.payload) >= 4
                    and struct.unpack(">HH", dec.payload[0:4])[1] & 0x4):
                continue
            if dec.seq != seq:
                continue
            return dec
        return None
    finally:
        r.close()


def id_probe(cfg, seq=41):
    """rp_child_id from the ID_REPLY (payload[8:12] u32 BE), or None."""
    dec = roundtrip(cfg, pdev.KIND_ID_REQUEST, b"", seq=seq, slot=0)
    if dec is None or dec.kind != pdev.KIND_ID_REPLY \
            or len(dec.payload) < 12:
        return None
    return struct.unpack(">I", dec.payload[8:12])[0]


def match_request(cfg, subject, seq):
    """One host MATCH_REQUEST -> (count, status, epoch) or None."""
    dec = roundtrip(cfg, pdev.KIND_MATCH_REQUEST,
                    struct.pack(">QHH", 0, MAXENT, 0) + subject,
                    seq=seq)
    if dec is None or dec.kind != pdev.KIND_MATCH_REPLY \
            or len(dec.payload) < 8:
        return None
    count, status = struct.unpack(">HH", dec.payload[0:4])
    epoch = struct.unpack(">I", dec.payload[4:8])[0]
    return count, status, epoch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=float, default=1.0)
    ap.add_argument("--skip-load", action="store_true",
                    help="child already resident; skip JTAG")
    args = ap.parse_args()

    if not os.environ.get("PYRO_DEVICE_IFACE"):
        print("SKIP: PYRO_DEVICE_IFACE not set (R68 fail-closed)")
        return 2
    cfg = pdev.DeviceConfig()

    results = []

    def step(name, errs, note=""):
        ok = not errs
        for e in errs:
            print("  - %s" % e)
        print("AC_M3_%s: %s%s" % (name, "PASS" if ok else "FAIL",
                                  (" — " + note) if note and ok else ""))
        results.append(ok)
        return ok

    def finish():
        ok = all(results)
        print()
        print("AC_M3_RESULT: %s" % ("PASS" if ok else "FAIL"))
        return 0 if ok else 1

    # ---- 0. pre-flight ---------------------------------------------
    print("=== 0. pre-flight ===")
    errs = []
    if not os.path.exists(PARTIAL):
        errs.append("partial missing: %s" % PARTIAL)
    elif os.path.getsize(PARTIAL) == 0:
        errs.append("partial is ZERO bytes (crash-truncated?): %s"
                    % PARTIAL)
    else:
        print("partial %s (%d bytes)"
              % (os.path.basename(PARTIAL), os.path.getsize(PARTIAL)))
    ok, reason = pdev.probe_device(cfg)
    print("probe: %s" % reason)
    if not ok:
        errs.append("device not usable: %s" % reason)
    if not step("preflight", errs):
        return finish()

    # ---- 1. loopback OFF, always ----------------------------------
    print("=== 1. CMAC loopback OFF (JTAG safety) ===")
    loopback("--off")
    step("loopback_off", [])

    # ---- 2. load ---------------------------------------------------
    if args.skip_load:
        print("=== 2. load skipped (--skip-load) ===")
        step("load", [], "skipped by request")
    else:
        print("=== 2. load_partial %s ===" % os.path.basename(PARTIAL))
        t0 = time.time()
        try:
            pdev.load_partial(cfg, PARTIAL)
            print("loaded in %.1f s (JTAG + R85a recovery)"
                  % (time.time() - t0))
            step("load", [])
        except pdev.PyroDeviceError as e:
            step("load", ["load_partial failed: %s" % e])
            return finish()

    # ---- 3. identity -----------------------------------------------
    print("=== 3. child identity ===")
    errs = []
    child = id_probe(cfg)
    if child is None:
        errs.append("no ID_REPLY from the child")
    else:
        print("rp_child_id=0x%08x" % child)
        if child != EXPECT_CHILD_ID:
            errs.append("rp_child_id 0x%08x != expected 0x%08x"
                        % (child, EXPECT_CHILD_ID))
    if not step("identity", errs):
        return finish()

    # ---- 4. MAC_STAT baseline --------------------------------------
    print("=== 4. MAC_STAT baseline ===")
    errs = []
    st = mw.read_mac_stats(cfg, slot=SLOT)
    if st is None:
        errs.append("no MAC_STAT_REPLY — MAC program not resident?")
    else:
        print("stats: %s" % (st,))
        if not st.zero_slack:
            errs.append("zero-slack VIOLATION: seen %d != %d + %d + %d"
                        % (st.seen, st.digested, st.skip_nonip,
                           st.skip_nokey))
        if not args.skip_load and any(st):
            errs.append("counters nonzero after RESET_AFTER_RECONFIG: "
                        "%s" % (st,))
    if not step("stat_baseline", errs):
        return finish()

    # ---- 5. scheduler ----------------------------------------------
    print("=== 5. scheduler: rr quantum 1, then a refused mode ===")
    errs = []
    ack = mw.set_sched(cfg, mw.SCHED_RR, quantum=1, slot=SLOT)
    if ack is None:
        errs.append("no SCHED_ACK for rr/1")
    else:
        print("rr/1 ack: %s" % (ack,))
        if (ack.mode, ack.quantum, ack.status) != (mw.SCHED_RR, 1, 0):
            errs.append("rr/1 ack wrong: %s" % (ack,))
    # encode_sched_set refuses a bad mode client-side (it mirrors the
    # device contract), so the refusal probe hand-packs the payload.
    dec = roundtrip(cfg, pdev.KIND_SCHED_SET,
                    struct.pack(">BBH", 7, 0, 1), seq=51)
    if dec is None or dec.kind != pdev.KIND_SCHED_ACK:
        errs.append("no SCHED_ACK for the bad mode probe")
    else:
        try:
            bad = mw.decode_sched_ack(dec)
        except pdev.PyroFrameError as e:
            bad = None
            errs.append("malformed bad-mode SCHED_ACK: %s" % e)
        if bad is not None:
            print("bad-mode ack: %s" % (bad,))
            if bad.status == 0:
                errs.append("device ACCEPTED mode 7 (status 0)")
    ack2 = mw.set_sched(cfg, mw.SCHED_RR, quantum=1, slot=SLOT)
    if ack2 is None:
        errs.append("no SCHED_ACK on the post-refusal confirm")
    elif (ack2.mode, ack2.quantum, ack2.status) != (mw.SCHED_RR, 1, 0):
        errs.append("settings disturbed by the refused mode: %s"
                    % (ack2,))
    step("sched", errs)

    # ---- 6. keying (the silicon rekey regression) ------------------
    print("=== 6. key commit / re-key / re-commit ===")
    errs = []
    for key_id, key, tag in ((1, KEY_A, "KEY_A"), (2, KEY_B, "KEY_B"),
                             (3, KEY_A, "KEY_A again")):
        want = mw.key_check(key)
        ack = mw.load_key(cfg, key_id, key, slot=SLOT)
        if ack is None:
            errs.append("%s: no MAC_KEY_ACK" % tag)
            break
        print("%s: key_id=%d keycheck=0x%016x (want 0x%016x)"
              % (tag, ack.key_id, ack.keycheck, want))
        if ack.key_id != key_id:
            errs.append("%s: key_id echo %d != %d"
                        % (tag, ack.key_id, key_id))
        if ack.keycheck != want:
            errs.append("%s: keycheck 0x%016x != 0x%016x — the device "
                        "is NOT digesting under the host's key"
                        % (tag, ack.keycheck, want))
            break
    if not step("rekey", errs):
        # A wrong resident key would poison every P1 delta below.
        return finish()

    # ---- 7. P0 nominations -----------------------------------------
    print("=== 7. minimal A5 table + host nomination ===")
    errs = []
    ac = otable.build([PATTERN])
    image = otable.serialize(ac, engine_id=ENGINE_ID)
    want_id = otable.table_id(image)
    print("table: 1 anchor, %d states, %d bytes, id=0x%08x"
          % (ac.n_states, len(image), want_id))
    epoch = None
    try:
        tst = pdev.load_table(cfg, image, slot=SLOT)
        epoch = tst.epoch
        print("committed: active=0x%08x epoch=%d"
              % (tst.active_table_id, tst.epoch))
        if tst.epoch != 1:
            errs.append("epoch %d != 1 after the first post-reconfig "
                        "commit" % tst.epoch)
    except pdev.PyroDeviceError as e:
        errs.append("load_table failed: %s" % e)
    if epoch is not None:
        m = match_request(cfg, SUBJECT, seq=71)
        if m is None:
            errs.append("no MATCH_REPLY to the host subject")
        else:
            count, mstat, mepoch = m
            print("MATCH_REPLY count=%d status=0x%x epoch=%d"
                  % (count, mstat, mepoch))
            if count <= 0:
                errs.append("pattern in subject but count == 0 "
                            "(SR3 direction)")
            if mepoch != epoch:
                errs.append("MATCH epoch %d != committed %d"
                            % (mepoch, epoch))
    if not step("nominate", errs):
        return finish()

    # ---- 8. wire window --------------------------------------------
    print("=== 8. wire window (%.1f s of loopback) ===" % args.window)
    errs = []
    s0 = mw.read_mac_stats(cfg, slot=SLOT)
    p0 = pdev.read_perf_counters(cfg, slot=SLOT, with_wire=True)
    w0 = p0[2] if p0 else None
    if s0 is None or w0 is None:
        errs.append("pre-window counters unavailable (stats %s, "
                    "wire %s)" % (s0 is not None, w0 is not None))
        step("wire_window", errs)
        return finish()
    loopback("--keep")
    s1 = w1 = None
    try:
        time.sleep(args.window / 2.0)
        # The AC-M3 clause: a host nomination DURING wire dispatch.
        m = match_request(cfg, SUBJECT, seq=81)
        if m is None:
            errs.append("mid-window MATCH_REQUEST got no reply")
        else:
            count, _mstat, mepoch = m
            print("mid-window MATCH_REPLY count=%d epoch=%d"
                  % (count, mepoch))
            if count <= 0:
                errs.append("mid-window count == 0 — nominations "
                            "stalled while RR dispatched wire frames")
            if mepoch != epoch:
                errs.append("mid-window epoch %d != %d"
                            % (mepoch, epoch))
        time.sleep(args.window / 2.0)
    finally:
        loopback("--off")
    time.sleep(0.2)
    s1 = mw.read_mac_stats(cfg, slot=SLOT)
    p1 = pdev.read_perf_counters(cfg, slot=SLOT, with_wire=True)
    w1 = p1[2] if p1 else None
    if s1 is None or w1 is None:
        errs.append("post-window counters unavailable")
        step("wire_window", errs)
        return finish()
    ds = mw.MacStats(*(b - a for a, b in zip(s0, s1)))
    dw = pdev.WireCounters(*(b - a for a, b in zip(w0, w1)))
    print("P1 deltas: seen=+%d digested=+%d skip_nonip=+%d "
          "skip_nokey=+%d" % (ds.seen, ds.digested, ds.skip_nonip,
                              ds.skip_nokey))
    print("P0 wire deltas: seen=+%d scanned=+%d drops=+%d noms=+%d"
          % (dw.seen, dw.scanned, dw.drops, dw.noms))
    print("NOTE: MAC_REPORT records are silicon-untestable in "
          "loopback — tx_gen emits only non-IP 0x88B5 frames, so "
          "nothing is ever digested here; report content is covered "
          "by the xsim differential, not this run.")
    if ds.seen != ds.digested + ds.skip_nonip + ds.skip_nokey:
        errs.append("P1 zero-slack VIOLATION on deltas: %d != %d+%d+%d"
                    % (ds.seen, ds.digested, ds.skip_nonip,
                       ds.skip_nokey))
    if dw.seen != dw.scanned + dw.drops:
        errs.append("P0 wire slack: seen +%d != scanned +%d + "
                    "drops +%d" % (dw.seen, dw.scanned, dw.drops))
    if ds.digested != 0:
        errs.append("P1 digested +%d != 0 — loopback carries no IP"
                    % ds.digested)
    if ds.skip_nonip != ds.seen:
        errs.append("P1 skip_nonip +%d != seen +%d (tx_gen frames "
                    "are non-IP 0x88B5)" % (ds.skip_nonip, ds.seen))
    tot = dw.seen + ds.seen
    if tot <= 0:
        errs.append("no wire frames dispatched in the window")
    else:
        for name, d in (("P0", dw.seen), ("P1", ds.seen)):
            share = d / tot
            print("RR share %s: %d/%d = %.3f" % (name, d, tot, share))
            if not 0.4 <= share <= 0.6:
                errs.append("RR fairness: %s share %.3f outside "
                            "[0.4, 0.6]" % (name, share))
    step("wire_window", errs)

    # ---- 9. epilogue ------------------------------------------------
    print("=== 9. epilogue (loopback stays OFF) ===")
    loopback("--off")
    st = mw.read_mac_stats(cfg, slot=SLOT)
    pf = pdev.read_perf_counters(cfg, slot=SLOT, with_wire=True)
    print("final MAC stats: %s" % (st,))
    print("final wire counters: %s" % (pf[2] if pf else None,))
    step("epilogue", [])
    return finish()


if __name__ == "__main__":
    sys.exit(main())
