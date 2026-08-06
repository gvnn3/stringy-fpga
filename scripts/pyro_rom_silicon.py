#!/usr/bin/env python3
"""S4 ROM-trie child on silicon: load the partial, replay the
differential (AC-S4-1's silicon follow-up; see notebook 6 Aug 2026).

Order of operations is safety-first and the order IS the experiment:

  0  model side: build the corpus trie, verify the on-disk partial's
     manifest attests exactly this build (identity before anything).
  1  CMAC loopback OFF.  The wiretap TX generator's frames nominate
     217-237 anchors each against the FULL corpus trie (measured in
     the model: 0xff/zero runs hit real binary and single-byte
     anchors), so loading the ROM child with loopback on floods ens2
     with ~245k overflowed wire MATCH_REPLYs/s.  Off FIRST.
  2  load_partial (JTAG + R85a in-band recovery, ~14-16 s).
  3  baked identity on silicon: TABLE_STATUS must show the model's
     TABLE_ID, epoch 1, caps 21,332, active_valid (boot expansion
     done) with no frame ever having primed it — the wrapper boot
     sweep's silicon proof.
  4  refusal probe: TABLE_BEGIN + TABLE_COMMIT must set commit_err
     and leave TABLE_ID/epoch untouched (v2.0.0 respect on silicon).
  5  host MATCH differential: the mixed-case subject; the reply must
     equal the model's (pattern_id, end) set exactly, epoch 1; R45a
     cycles/bytes in the engine's [4.9, 20] band.
  6  controlled wire window: loopback ON for --window seconds while
     sniffing ens2, then OFF.  Every wire reply must be structurally
     exact (count == 61 capped + OVF set — provisional A6's wire-OVF
     shape live on silicon — status bit2, ERR clear, epoch 1, slot 1,
     0-based monotone seq) and the R78.13 counters must climb
     slack-free (seen == scanned + drops).  Content-exact wire entry
     checks live in the xsim differential where stimulus is
     controlled; the generator's seq bytes vary here.

Usage:
    PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 \\
        scripts/pyro_rom_silicon.py [--window 1.0]

Leaves loopback OFF (the ROM child + generator combination floods;
re-enable deliberately with sudo -n scripts/pyro_cmac_loopback.py).
"""
import argparse
import json
import os
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))

import pyro.device as pdev                      # noqa: E402
from pyro.snort import shared_trie as ST        # noqa: E402
from pyro.snort import triage as Tr             # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES = os.path.join(REPO, "third_party", "snort3-community-rules",
                     "snort3-community.rules")
PARTIAL = os.path.join(REPO, "hw", "dfx", "build-wiretap", "partials",
                       "rom_trie.bit")
MANIFEST = os.path.join(REPO, "hw", "dfx", "build-wiretap", "partials",
                        "rom_trie_manifest.json")
LOOPBACK = os.path.join(REPO, "scripts", "pyro_cmac_loopback.py")
SLOT = 1
SUBJECT = b"the Attack used MALWARE and seCRETly tackled ACK"
MAXENT = 61


def loopback(*args):
    r = subprocess.run(["sudo", "-n", LOOPBACK, *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("loopback %s FAILED:\n%s%s" % (args, r.stdout, r.stderr))
        sys.exit(1)


def roundtrip(cfg, kind, payload, timeout_s=2.0, seq=99):
    tr = pdev._make_transport(cfg)
    try:
        eth = (bytes(cfg.dst_mac) + bytes(cfg.src_mac)
               + struct.pack(">H", pdev.ETHERTYPE))
        tr.send(eth + pdev.encode_frame(kind, SLOT, seq, payload))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            raw = tr.recv(0.5)
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
            if dec.seq != seq:
                continue
            return dec
        return None
    finally:
        tr.close()


def expected_entries(trie, data):
    """(pattern_id, end) in the device's emission order: by position,
    then oflat (sorted-pid) order within a state, truncated at MAXENT
    exactly as the reply cap does."""
    from pyro.overlay import table as otable
    out, st = [], 0
    for i, b in enumerate(otable.ascii_fold(data)):
        st = trie.ac.next_state(st, b)
        for pid in sorted(trie.ac.out[st]):
            out.append((pid, i + 1))
    return out[:MAXENT], len(out) > MAXENT


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
    errors = []

    # ---- 0. model + identity chain ---------------------------------
    print("=== 0. corpus trie (model side) ===")
    trie = ST.build_shared(Tr.triage_file(RULES), 16)
    man = trie.manifest()
    with open(MANIFEST) as f:
        disk = json.load(f)
    if disk["table_id"] != man["table_id"] or \
            disk["strong_id"] != man["strong_id"]:
        print("FATAL: on-disk partial manifest %s does not attest this "
              "corpus build %s" % (disk["table_id"], man["table_id"]))
        return 1
    want_id = int(man["table_id"], 16)
    exp_host, exp_ovf = expected_entries(trie, SUBJECT)
    print("table_id %s, %d states; host subject -> %d entries%s"
          % (man["table_id"], trie.n_states, len(exp_host),
             " +OVF" if exp_ovf else ""))

    # ---- 1. loopback OFF, always -----------------------------------
    print("=== 1. CMAC loopback OFF (flood guard) ===")
    loopback("--off")

    # ---- 2. load ----------------------------------------------------
    if not args.skip_load:
        print("=== 2. load_partial %s ===" % os.path.basename(PARTIAL))
        t0 = time.time()
        pdev.load_partial(cfg, PARTIAL)
        print("loaded in %.1f s (JTAG + R85a recovery)"
              % (time.time() - t0))

    # ---- 3. baked identity ------------------------------------------
    print("=== 3. baked identity on silicon ===")
    st = pdev.read_table_status(cfg, slot=SLOT)
    if st is None:
        print("FATAL: no TABLE_STATUS_REPLY from the child")
        return 1
    print("active=0x%08x epoch=%d caps=%d flags=0x%x bytes=%d"
          % (st.active_table_id, st.epoch, st.capacity_states,
             st.status_flags, st.bytes_received))
    if st.active_table_id != want_id:
        errors.append("TABLE_ID 0x%08x != baked 0x%08x"
                      % (st.active_table_id, want_id))
    if st.epoch != ST.ROM_EPOCH:
        errors.append("epoch %d != %d" % (st.epoch, ST.ROM_EPOCH))
    if st.capacity_states != trie.n_states:
        errors.append("caps %d != %d" % (st.capacity_states,
                                         trie.n_states))
    if not (st.status_flags & 0x4):
        errors.append("active_valid clear — boot expansion did not "
                      "complete on silicon")

    # ---- 4. refusal probe -------------------------------------------
    print("=== 4. A5 commit refusal on silicon ===")
    roundtrip(cfg, pdev.KIND_TABLE_BEGIN,
              struct.pack(">IIQIHH", 1, ST.S4_ENGINE_ID, 64, want_id,
                          0, 0), seq=101)
    dec = roundtrip(cfg, pdev.KIND_TABLE_COMMIT,
                    struct.pack(">II", want_id, 0), seq=102)
    if dec is None or dec.kind != pdev.KIND_TABLE_STATUS_REPLY:
        errors.append("no status reply to the refused commit")
    else:
        a, _sh, ep, fl, _nb, _caps, _err = struct.unpack(
            ">IIIIQII", dec.payload[0:32])
        print("after refused commit: active=0x%08x epoch=%d flags=0x%x"
              % (a, ep, fl))
        if a != want_id or ep != ST.ROM_EPOCH:
            errors.append("refused commit disturbed the baked table")
        if not (fl & 0x8):
            errors.append("commit_err not visible (flags=0x%x)" % fl)

    # ---- 5. host MATCH differential ---------------------------------
    print("=== 5. host MATCH differential ===")
    t0 = time.perf_counter()
    dec = roundtrip(cfg, pdev.KIND_MATCH_REQUEST,
                    struct.pack(">QHH", 0, MAXENT, 0) + SUBJECT,
                    seq=103)
    rtt = time.perf_counter() - t0
    if dec is None or dec.kind != pdev.KIND_MATCH_REPLY:
        errors.append("no MATCH_REPLY")
    else:
        count, mstat = struct.unpack(">HH", dec.payload[0:4])
        epoch = struct.unpack(">I", dec.payload[4:8])[0]
        got = []
        for i in range(count):
            _s, e, pid, _f = struct.unpack_from(
                "<QQII", dec.payload, 8 + 24 * i)
            got.append((pid, e))
        print("MATCH_REPLY count=%d status=0x%x epoch=%d rtt=%.0f us"
              % (count, mstat, epoch, rtt * 1e6))
        if epoch != ST.ROM_EPOCH:
            errors.append("MATCH epoch %d != %d" % (epoch, ST.ROM_EPOCH))
        if sorted(got) != sorted(exp_host):
            errors.append("host matches differ:\n  device %s\n"
                          "  model  %s" % (sorted(got),
                                           sorted(exp_host)))
        if bool(mstat & 1) != exp_ovf:
            errors.append("OVF mismatch: device %d, model %s"
                          % (mstat & 1, exp_ovf))
    perf = pdev.read_perf_counters(cfg, slot=SLOT, with_wire=True)
    if perf is None:
        errors.append("no PERF_REPLY")
        w0 = None
    else:
        cyc, byt, w0 = perf
        ratio = cyc / byt if byt else 0.0
        print("PERF: cycles=%d bytes=%d (%.2f cyc/B); wire %s"
              % (cyc, byt, ratio, tuple(w0) if w0 else None))
        if byt != len(SUBJECT):
            errors.append("PERF bytes %d != subject %d"
                          % (byt, len(SUBJECT)))
        if byt and not (4.9 <= ratio <= 20):
            errors.append("cyc/B %.2f outside [4.9, 20]" % ratio)

    # ---- 6. controlled wire window ----------------------------------
    print("=== 6. wire window (%.1f s of loopback) ===" % args.window)
    loopback("--keep")
    replies = []
    tr = pdev._make_transport(cfg)
    try:
        deadline = time.monotonic() + args.window
        while time.monotonic() < deadline:
            raw = tr.recv(0.2)
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
            if (dec.kind == pdev.KIND_MATCH_REPLY
                    and len(dec.payload) >= 4
                    and struct.unpack(">HH", dec.payload[0:4])[1] & 0x4):
                replies.append(dec)
                if len(replies) >= 200:
                    break                      # plenty of evidence
    finally:
        tr.close()
        loopback("--off")
    time.sleep(0.2)
    perf2 = pdev.read_perf_counters(cfg, slot=SLOT, with_wire=True)
    w1 = perf2[2] if perf2 else None
    print("captured %d wire replies; counters %s -> %s"
          % (len(replies), tuple(w0) if w0 else None,
             tuple(w1) if w1 else None))
    if not replies:
        errors.append("no wire MATCH_REPLYs captured in the window")
    seqs = [d.seq for d in replies]
    for i, d in enumerate(replies[:50]):
        count, mstat = struct.unpack(">HH", d.payload[0:4])
        epoch = struct.unpack(">I", d.payload[4:8])[0]
        if d.flags != 0:
            errors.append("wire reply %d: header flags != 0" % i)
            break
        if d.slot != SLOT:
            errors.append("wire reply %d: slot %d" % (i, d.slot))
            break
        if count != MAXENT or not (mstat & 1):
            errors.append("wire reply %d: count=%d status=0x%x — "
                          "expected the 61-entry OVF shape" %
                          (i, count, mstat))
            break
        if mstat & 2:
            errors.append("wire reply %d: ERR set" % i)
            break
        if epoch != ST.ROM_EPOCH:
            errors.append("wire reply %d: epoch %d" % (i, epoch))
            break
    if seqs and seqs != sorted(seqs):
        errors.append("wire reply seq not monotone")
    if w0 and w1:
        ws, wsc, wd, wn = (w1[i] - w0[i] for i in range(4))
        print("window deltas: seen=+%d scanned=+%d drops=+%d noms=+%d"
              % (ws, wsc, wd, wn))
        if ws != wsc + wd:
            errors.append("counter slack: seen != scanned + drops")
        if wsc and wn != wsc * MAXENT:
            # noms counts REPORTED entries; every generator frame
            # overflows, so noms must be exactly 61 per scanned frame.
            errors.append("noms %d != %d scanned x %d"
                          % (wn, wsc, MAXENT))

    print()
    if errors:
        print("ROM_SILICON: FAIL")
        for e in errors:
            print("  - %s" % e)
        return 1
    print("ROM_SILICON: PASS — baked identity, refusal, host match set,"
          " and the wire path all exact on silicon; loopback left OFF")
    return 0


if __name__ == "__main__":
    sys.exit(main())
