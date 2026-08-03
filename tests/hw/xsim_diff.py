#!/usr/bin/env python3
"""P2b xsim differential: generated engine + pyro_rp wrapper vs the reference.

Builds the engine (``--datapath N``) and matching wrapper, drives real R78
frames through ``tb_pyro_rp.v`` under the pinned Vivado's xsim, and compares
every reply byte-for-byte against a Python composer that replicates the
harness reply layout.  The MATCH-entry reference mirrors the ENGINE's
semantics exactly (continuous-seeding NFA end detection; for a wide engine,
ends coalesced to the highest per beat — pyro.hdl.generator._emit_rtl_wide):

  * ID_REQUEST  -> exact ID_REPLY bytes
  * MATCH       -> exact MATCH_REPLY bytes (count/status/entries)
  * wrong slot  -> exact STATUS/ERROR (PYRO_E_NOT_RESIDENT)
  * PERF        -> BYTES exact; CYCLES bounded (<= ceil(len/N) + margin),
                   proving the widened datapath actually consumes N B/cyc.

Run:  python3 tests/hw/xsim_diff.py [--datapath 8] [--pattern 'abc[a-f]{2}']
Needs the pinned Vivado (PYRO_VIVADO or the R70a-pin default); exits 2 if
absent (honest skip for callers).

This is not only a hand-run script: ``tests/acceptance/test_acs2_3_oracle.py``
imports :func:`main` and calls it as the AC-S2-3 **RTL gate** (gate 1b) — the
only check in the suite that executes the emitted Verilog at all, since the
SR16 oracle runs against ``GroupCircuitModel`` over the automata and never
reads ``GeneratedGroup.rtl``.  Keep :func:`main` importable and argv-driven.
"""
import argparse
import os
import re as stdre
import shutil
import struct
import subprocess
import sys
import tempfile

REPO = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.abspath(__file__))))
sys.path.insert(0, REPO)

import pyro.device as pdev
import pyro.hdl.automaton as _auto
import pyro.hdl.generator as _gen
import pyro.hdl.rp_wrapper as _wrap
import pyro._circuit_model as _cm

TB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tb_pyro_rp.v")
SLOT = 1
ETH_REQ = bytes(6) + b"\x02\x00\x00\x00\x00\x01" + b"\x88\xb5"  # dst|src|type


# ---------------------------------------------------------------------------
# Reference: engine-semantics end detection + wide-beat coalescing
# ---------------------------------------------------------------------------
def engine_end_set(au, buf: bytes):
    """Accept-end positions of the continuous-seeding NFA (RTL semantics)."""
    is_bytes = au.enc == _cm.ENC_BYTES
    ends = []
    cur = _cm._closure(au, (au.start,), buf, 0, is_bytes)
    for pos in range(len(buf)):
        b = buf[pos]
        moved = {e.target for st in cur for e in au.edges[st]
                 if e.kind == _auto.E_BYTE and b in e.payload}
        moved.add(au.start)                       # continuous search seeding
        cur = _cm._closure(au, tuple(moved), buf, pos + 1, is_bytes)
        if au.accept in cur:
            ends.append(pos + 1)
    return ends


def coalesce(ends, n: int):
    """Wide-engine per-beat coalescing: highest end per n-byte beat."""
    if n == 1:
        return list(ends)
    best = {}
    for e in ends:
        best[(e - 1) // n] = max(best.get((e - 1) // n, 0), e)
    return [best[k] for k in sorted(best)]


def group_entries(automata, buf: bytes):
    """Reference ring for an SR7 GROUP engine: every (end, pattern_id).

    The group engine does **no** per-beat coalescing (unlike the wide
    single-pattern engine): each slot's accept at each lane becomes its own
    entry, and the priority encoder drains ascending (lane, slot) — i.e.
    ascending ``(end, pattern_id)``, the ring order
    ``pyro._circuit_model.GroupCircuitModel`` pins.  That makes this reference
    WIDTH-INDEPENDENT: the same expected bytes gate datapath_bytes 1 and 8.
    """
    ents = []
    for pid, au in enumerate(automata):
        # tombstoned slot (SR6): never matches
        if au is None:
            continue
        for e in engine_end_set(au, buf):
            ents.append((e, pid))
    ents.sort()
    return ents


# ---------------------------------------------------------------------------
# Expected-reply composers (mirror pyro_rp's ST_BHDR/ST_BENT layout)
# ---------------------------------------------------------------------------
def _reply_shell(req: bytes, kind: int, payload: bytes) -> bytes:
    eth = req[6:12] + req[0:6] + b"\x88\xb5"      # MAC swap
    hdr = struct.pack(">BBBBHIHH", 0x50, 0x01, kind, 0,
                      struct.unpack(">H", req[18:20])[0],
                      struct.unpack(">I", req[20:24])[0],
                      len(payload), 0)
    frame = eth + hdr + payload
    return frame + bytes(max(0, 60 - len(frame)))  # L2 min zero-pad


def expect_id(req, spec16, build16, wirever, child_id):
    return _reply_shell(req, 0x02, struct.pack(">HHII", spec16, build16,
                                               wirever, child_id)[:12])


def expect_status(req, code=7):
    return _reply_shell(req, 0x05, struct.pack(">I", code))


def expect_match(req, ends, cap, maxent=61):
    """``ends`` is a list of ``end`` (pattern_id 0) or of ``(end,
    pattern_id)``."""
    pairs = [e if isinstance(e, tuple) else (e, 0) for e in ends]
    cap_eff = min(cap, maxent)
    entries = pairs[:cap_eff]
    ovf = 1 if len(pairs) > cap_eff else 0
    payload = struct.pack(">HH", len(entries), ovf) + bytes(4)
    for end, pid in entries:
        payload += struct.pack("<QQII", 0, end, pid, 1)   # R47 little-endian
    return _reply_shell(req, 0x04, payload)


# ---------------------------------------------------------------------------
# Frame <-> beat plumbing
# ---------------------------------------------------------------------------
def frame_to_beats(frame: bytes):
    frame = frame + bytes(max(0, 60 - len(frame)))
    beats = []
    for off in range(0, len(frame), 64):
        chunk = frame[off:off + 64]
        keep = (1 << len(chunk)) - 1
        beats.append((chunk + bytes(64 - len(chunk)), keep,
                      1 if off + 64 >= len(frame) else 0))
    return beats


def beats_to_frames(lines):
    frames, cur = [], b""
    for ln in lines:
        d_hex, k_hex, last = ln.split()
        data = bytes.fromhex(d_hex)[::-1]          # %h prints MSB first
        keep = int(k_hex, 16)
        nbytes = bin(keep).count("1")
        cur += data[:nbytes]
        if last == "1":
            frames.append(cur)
            cur = b""
    return frames


def mk_match_req(seq, corpus, out_cap, slot=SLOT, max_payload=1486):
    payload = struct.pack(">QHH", 0, out_cap, 0) + corpus
    return ETH_REQ + pdev.encode_frame(pdev.KIND_MATCH_REQUEST, slot, seq,
                                       payload, max_payload=max_payload)


# ---------------------------------------------------------------------------
def main(argv=None):
    """Run one differential; returns 0 (pass), 1 (mismatch) or 2 (no Vivado).

    ``argv`` is accepted so this is callable **in-process** from pytest —
    ``tests/acceptance/test_acs2_3_oracle.py`` invokes it as the AC-S2-3 RTL
    gate, and being importable is what lets that suite sabotage the emitter
    and prove the gate bites.  Nothing else about the script changes.
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--datapath", type=int, default=8)
    ap.add_argument("--pattern", default="abc[a-f]{2}")
    ap.add_argument("--group", default=None,
                    help="comma-separated slot patterns: builds the SR7 GROUP "
                         "engine (N automata, one harness) instead of the "
                         "single-pattern engine.  An empty element is a "
                         "tombstoned slot (SR6).")
    ap.add_argument("--max-frame", type=int, default=1536,
                    help="wrapper MAX_FRAME_BYTES: 1536 or 9600 (R78.9a)")
    ap.add_argument("--engines", type=int, default=1,
                    help="v4 frame-parallel core count (P2e); 1 = classic")
    ap.add_argument("--keep-workdir", action="store_true")
    args = ap.parse_args(argv)
    n = args.datapath
    max_payload = args.max_frame - 14 - 14 - 4   # R78.9/R78.9a accounting
    max_corpus = max_payload - 12                # MATCH body prefix

    vivado_dir = os.environ.get("PYRO_VIVADO")
    if not vivado_dir:
        from pyro.synth.toolchain import PINNED_VIVADO_DIR
        vivado_dir = PINNED_VIVADO_DIR
    xvlog = os.path.join(vivado_dir, "bin", "xvlog")
    if not os.path.isfile(xvlog):
        print(f"SKIP: no Vivado at {vivado_dir!r}")
        return 2

    group = None
    if args.group is not None:
        # "pat" | "pat/i" (nocase, the R15 ASCII fold most Snort anchors carry)
        # | "" (a tombstoned slot, SR6).
        group, gflags = [], []
        for p in args.group.split(","):
            ic = p.endswith("/i")
            group.append(p[:-2].encode("latin-1") if ic
                         else (p.encode("latin-1") or None))
            gflags.append(stdre.IGNORECASE if ic else 0)
        circ = _gen.generate_group(group, gflags, datapath_bytes=n)
        automata = circ.automata
    else:
        circ = _gen.generate(args.pattern, 0, datapath_bytes=n)
        automata = (circ.automaton,)
    child_id = _wrap.rp_child_id_from_hash(circ.pattern_hash16.hex())
    sv = _wrap.generate_rp_child(circ.pattern_hash16.hex(), datapath_bytes=n,
                                 max_frame_bytes=args.max_frame,
                                 cores=args.engines,
                                 engine_backpressure=group is not None)
    au = automata[0]

    # ---- request schedule + expected replies -----------------------------
    if group is not None:
        # Corpora chosen for SIMULTANEOUS and overlapping accepts: with slots
        # like ("abc", "bc", "c") every "abc" makes three slots accept on the
        # SAME byte (three ring entries, one byte) — the case the priority
        # encoder, the feed stall and the skid buffer exist for.
        corpora = [
            b"xxabcdeyyabcffz",
            b"abcabcabc",                          # back-to-back multi-accepts
            b"no matches at all here",
            b"abcab" * 60,                         # 300 B, saturating drains
            (b"padpad" * 20) + b"abcd",            # accepts on the FINAL byte
            b"a",
            b"abc",                                # whole corpus == one match
        ]
    else:
        corpora = [
            b"xxabcdeyyabcffz",                    # 2 windows, short beat
            b"abcdd",                              # exactly one, len==5
            b"no matches at all here",             # zero windows
            b"abcab" * 60,                         # 300 B, many windows
            (b"padpad" * 20) + b"abcfe",           # match at the very end
            b"a",                                  # tiny corpus
        ]
    if args.max_frame > 1536:
        # R78.9a jumbo: a max-size corpus with matches at the head, middle,
        # and final byte — exercises the widened word-select slices end to end.
        body = bytearray(b"\x78" * max_corpus)
        body[0:5] = b"abcde"
        mid = (max_corpus // 2) & ~7
        body[mid:mid + 5] = b"abcff"
        body[max_corpus - 5:] = b"abcfa"
        corpora.append(bytes(body))
    def ref(c):
        """Expected ring for corpus ``c`` — group or single-pattern engine."""
        if group is not None:
            return group_entries(automata, c)
        return coalesce(engine_end_set(au, c), n)

    reqs, expected = [], []
    seq = 100
    r = ETH_REQ + pdev.encode_frame(pdev.KIND_ID_REQUEST, 0, seq, b"")
    reqs.append(r)
    expected.append(expect_id(r, 0x0202, 0, _wrap.DEFAULT_WIRE_HARNESS_VERSION,
                              child_id))
    for c in corpora:
        seq += 1
        r = mk_match_req(seq, c, out_cap=8, max_payload=max_payload)
        reqs.append(r)
        expected.append(expect_match(r, ref(c), 8))
    # cap overflow: cap=1 on the many-window corpus.  For a group this also
    # exercises HAZARD (b) — the dropped entries must still retire from `pend`
    # or the engine never asserts DONE and the reply never comes.
    seq += 1
    r = mk_match_req(seq, b"abcab" * 60, out_cap=1)
    reqs.append(r)
    expected.append(expect_match(r, ref(b"abcab" * 60), 1))
    # out_cap = 0: overflow, NOT "no match" (S1 protocol note 1) — every accept
    # is dropped, so the whole drain runs with the ring closed.
    if group is not None:
        seq += 1
        r = mk_match_req(seq, b"abcabc", out_cap=0)
        reqs.append(r)
        expected.append(expect_match(r, ref(b"abcabc"), 0))
        last_scan = b"abcabc"
    else:
        last_scan = b"abcab" * 60
    # wrong slot -> NOT_RESIDENT
    seq += 1
    r = mk_match_req(seq, b"abcde", out_cap=8, slot=0)
    reqs.append(r)
    expected.append(expect_status(r))
    # PERF after the last real scan (fields checked loosely below).
    # engines>1: skipped — the tb streams frames back-to-back, so the PERF
    # frame races the last_done update in the v4 top; on silicon PERF is
    # always sequential-after-drain (pyro_hw), where the routing is exact.
    if args.engines == 1:
        seq += 1
        perf_req = ETH_REQ + \
            pdev.encode_frame(pdev.KIND_PERF_REQUEST, SLOT, seq, b"")
        reqs.append(perf_req)
        expected.append(None)                      # special-cased

    # ---- write workdir + run xsim ----------------------------------------
    wd = tempfile.mkdtemp(prefix="pyro_xsim_diff_")
    try:
        with open(os.path.join(wd, "engine.v"), "w") as f:
            f.write(circ.rtl)
        with open(os.path.join(wd, "pyro_rp.sv"), "w") as f:
            f.write(sv)
        beats = [b for r in reqs for b in frame_to_beats(r)]
        with open(os.path.join(wd, "stim_d.memh"), "w") as fd, \
             open(os.path.join(wd, "stim_k.memh"), "w") as fk, \
             open(os.path.join(wd, "stim_l.memb"), "w") as fl:
            for d, k, l in beats:
                fd.write(d[::-1].hex() + "\n")     # verilog %h MSB-first
                fk.write(f"{k:016x}\n")
                fl.write(f"{l}\n")
        # A group engine adds a stall cycle per pending accept, so give the
        # tail-drain generous headroom (it only costs simulated time).
        runcyc = sum(len(r) for r in reqs) * (6 if group else 2) + 8000
        env = dict(os.environ)
        cmds = [
            [xvlog, "engine.v"],
            [xvlog, "-sv", "pyro_rp.sv", TB],
            [os.path.join(vivado_dir, "bin", "xelab"), "tb_pyro_rp",
             "-s", "diff_sim", "-generic_top", f"NBEATS={len(beats)}",
             "-generic_top", f"RUNCYC={runcyc}"],
            [os.path.join(vivado_dir, "bin", "xsim"), "diff_sim", "-R"],
        ]
        for cmd in cmds:
            p = subprocess.run(cmd, cwd=wd, env=env, capture_output=True,
                               text=True, timeout=600)
            if p.returncode != 0:
                print(f"FAIL: {' '.join(os.path.basename(c) for c in cmd[:1])} "
                      f"rc={p.returncode}\n{p.stdout[-2000:]}\n"
                      f"{p.stderr[-500:]}")
                return 1
        with open(os.path.join(wd, "out_beats.txt")) as f:
            got = beats_to_frames(
                [ln for ln in f.read().splitlines() if ln.strip()])
    finally:
        if args.keep_workdir:
            print(f"workdir kept: {wd}")
        else:
            shutil.rmtree(wd, ignore_errors=True)

    # ---- compare -----------------------------------------------------------
    # Replies are matched BY SEQ, not position: the v4 frame-parallel top
    # legitimately reorders completions (a short corpus on core B finishes
    # before a long one on core A).  Single-core order is deterministic and
    # unaffected by keying.
    fails = 0
    if len(got) != len(expected):
        print(f"FAIL: reply count {len(got)} != expected {len(expected)}")
        return 1
    def _seq_of(fr):
        return struct.unpack(">I", fr[20:24])[0]
    got_by_seq = {}
    for g in got:
        got_by_seq.setdefault(_seq_of(g), g)
    if len(got_by_seq) != len(got):
        print("FAIL: duplicate reply seq")
        return 1
    reordered = []
    for e, r in zip(expected, reqs):
        want_seq = _seq_of(r)
        if want_seq not in got_by_seq:
            print(f"FAIL: no reply for seq {want_seq}")
            return 1
        reordered.append(got_by_seq[want_seq])
    got = reordered
    for i, (g, e) in enumerate(zip(got, expected)):
        if e is None:                              # PERF: loose field checks
            dec = pdev.decode_frame(g[14:])
            cyc, byt = struct.unpack(">QQ", dec.payload[:16])
            # the last scan is the last MATCH request that reached the engine
            # (the slot-miss after it never scans).
            scan_len = len(last_scan)
            # A group engine stalls the feed one cycle per pending accept plus
            # the feeder's one-cycle reaction, so CYCLES is bounded by the
            # idealized ceil(len/N) PLUS the drain, not by ceil(len/N) alone.
            drain = 3 * len(ref(last_scan)) if group is not None else 0
            bound = -(-scan_len // n) + drain + 16
            ok = (byt == scan_len) and (0 < cyc <= bound)
            print(f"[{i}] PERF  BYTES={byt} CYCLES={cyc} "
                  f"(<= {bound}) {'OK' if ok else 'FAIL'}")
            fails += 0 if ok else 1
        elif g == e:
            kind = ["ID", "MATCH", "STATUS"][
                0 if i == 0 else 2 if len(e) == 60 and e[16] == 5 else 1]
            print(f"[{i}] {kind:6s} "
                  f"exact ({len(g)} B) OK")
        else:
            fails += 1
            print(f"[{i}] MISMATCH ({len(g)} vs {len(e)} B)")
            for off in range(min(len(g), len(e))):
                if g[off] != e[off]:
                    print(f"     first diff @byte {off}: got {g[off]:02x} "
                          f"want {e[off]:02x}")
                    break
    print(f"\n{'PASS' if fails == 0 else 'FAIL'}: "
          f"{len(expected) - fails}/{len(expected)} replies match "
          f"(datapath={n}, max_frame={args.max_frame}, "
          f"pattern={args.pattern!r})")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
