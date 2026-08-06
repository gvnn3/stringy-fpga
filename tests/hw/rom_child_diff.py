#!/usr/bin/env python3
"""S4 differential: drive the ROM child through pyro_rp under xsim.

The ROM engine (``pyro_ac_rom_engine.v``) bakes its table from $readmemh
files, so the questions worth asking are different from the A5 load
differential's:

  1. IDENTITY FROM CONFIGURATION: with no frame ever sent, the child's
     first TABLE_STATUS_REPLY must show the baked TABLE_ID, epoch 1 and
     the array capacity — proving the wrapper's boot CSR sweep latched
     the baked identity (without it the wrapper reports epoch 0 and the
     wire gate drops everything; found by inspection, pinned here).
  2. WIRE-FIRST: the FIRST frame the child ever sees is a nominating
     raw wire frame — no host warm-up, no status poll first.  A ROM
     child is valid from configuration, so it must scan and reply
     (epoch 1, status bit2).  This is the acceptance the boot sweep
     exists for.
  3. CASE FOLD IN FABRIC: subjects arrive MIXED-CASE and matches must
     equal the model's folded scan — for nocase patterns and for
     case-sensitive ones (the deliberate SR4 ``case_fold``
     over-approximation).
  4. v2.0.0 RESPECT: a complete, well-formed A5 load sequence (BEGIN /
     DATA / COMMIT with a correct CRC) must be REFUSED — commit_err
     visible, active table and epoch untouched — and a wire frame
     during that open load is dropped and counted.
  5. R45a/R78.13 surfaces carry over: per-scan CYCLES/BYTES, and the
     additive wire counters stay slack-free (seen == scanned + drops).

Run:  .venv-pyro/bin/python3 tests/hw/rom_child_diff.py
Exits 2 if the pinned Vivado is absent (honest skip).
"""
import os
import struct
import subprocess
import sys
import tempfile

REPO = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

import pyro.device as pdev                      # noqa: E402
import pyro.hdl.rom_child as rc                 # noqa: E402
import pyro.hdl.rp_wrapper as _wrap             # noqa: E402
from pyro.overlay import table as otable        # noqa: E402
from pyro.snort import shared_trie as ST        # noqa: E402

TBDIR = os.path.dirname(os.path.abspath(__file__))
TB = os.path.join(TBDIR, "tb_pyro_rp_wd.v")
SLOT = 1
MAXFRAME = 9600
MAXPAY = pdev.MAX_PAYLOAD_JUMBO
ETH_REQ = bytes(6) + b"\x02\x00\x00\x00\x00\x01" + b"\x88\xb5"
SRC_HOST = 0x0001
SRC_WIRE = 0x0040

WIRE_ETH = (b"\x02\x00\x00\x00\x00\x63" + b"\x02\x00\x00\x00\x00\x64" +
            b"\x08\x00")
# Mixed case ON THE WIRE: the fold happens in fabric, so a shouting
# attacker must nominate exactly like a lowercase one.
W_NOM = WIRE_ETH + b"telemetry: the ATTACK dropped MalWare on the host"
W_CLEAN = WIRE_ETH + b"the quick brown fox jumps over the lazy dog"
W_NOM2 = WIRE_ETH + b"repacked MALWARE rides a second Attack wave"


def frame_to_beats(frame):
    out = []
    for off in range(0, len(frame), 64):
        chunk = frame[off:off + 64]
        keep = (1 << len(chunk)) - 1
        out.append((chunk.ljust(64, b"\x00"), keep,
                    off + 64 >= len(frame)))
    return out


def beats_to_frames(lines):
    frames, cur = [], b""
    for ln in lines:
        parts = ln.split()
        if len(parts) != 3:
            continue
        data, keep, last = (int(parts[0], 16), int(parts[1], 16),
                            parts[2] == "1")
        raw = data.to_bytes(64, "little")
        cur += raw[:bin(keep).count("1")]
        if last:
            frames.append(cur)
            cur = b""
    return frames


def req(kind, seq, payload):
    return ETH_REQ + pdev.encode_frame(kind, SLOT, seq, payload,
                                       max_payload=MAXPAY)


def fold_scan(ac, data):
    """(pattern_id, end) over folded input — the trie reference."""
    out, st = [], 0
    for i, b in enumerate(otable.ascii_fold(data)):
        st = ac.next_state(st, b)
        for pid in ac.out[st]:
            out.append((pid, i + 1))
    return out


def main(argv=None):
    vivado = os.environ.get("PYRO_VIVADO", "/usr/local/cad/2025.2/Vivado")
    if not os.path.exists(os.path.join(vivado, "bin", "xvlog")):
        print("SKIP: no Vivado at %s" % vivado)
        return 2

    # ---- bake a small trie ---------------------------------------------
    # "attack"/"malware" are the S4-folded (all-lowercase) residents;
    # "SecReT" exercises the fold of a case-sensitive-looking pattern
    # (it is baked folded, so it must match any case on the wire).
    anchors = [ST._fold_cap(p, 16)
               for p in (b"attack", b"tack", b"ack", b"malware",
                         b"ware", b"SecReT")]
    ac = otable.build(anchors)
    image = otable.serialize(ac, engine_id=ST.S4_ENGINE_ID)
    want_id = otable.table_id(image)
    subject = b"the Attack used MALWARE and seCRETly tackled ACK"
    ref_matches = fold_scan(ac, subject)
    print("ROM table: %d bytes, %d states, table_id=0x%08x"
          % (len(image), ac.n_states, want_id))
    print("model: %d matches on the mixed-case subject"
          % len(ref_matches))

    # ---- frame sequence -------------------------------------------------
    frames, plan, wire_want = [], [], []
    seq = 0

    def add(kind, payload, tag):
        nonlocal seq
        seq += 1
        frames.append((req(kind, seq, payload), SRC_HOST))
        plan.append((seq, kind, tag))

    def add_wire(frame, tag, nominate):
        frames.append((frame, SRC_WIRE))
        if nominate:
            wire_want.append((tag, frame))

    # Check 2: the FIRST frame ever is a nominating wire frame.
    add_wire(W_NOM, "wire-first", True)
    add_wire(W_CLEAN, "wire-clean", False)
    # Check 1: baked identity on the status surface.
    add(pdev.KIND_TABLE_STATUS_REQUEST, b"", "status-boot")
    # Check 3: mixed-case host scan.
    add(pdev.KIND_MATCH_REQUEST,
        struct.pack(">IIHH", 0, 0, 61, 0) + subject, "match")
    add(pdev.KIND_PERF_REQUEST, b"", "perf")
    # Check 4: a complete well-formed A5 load, refused.
    add(pdev.KIND_TABLE_BEGIN,
        struct.pack(">IIQIHH", otable.TABLE_FORMAT_VERSION,
                    ST.S4_ENGINE_ID, len(image), want_id, 0, 0),
        "begin-refused")
    # A wire frame during the open load: dropped and counted, exactly
    # as on a loadable child.
    add_wire(W_NOM, "wire-during-load", False)
    off, cap = 0, MAXPAY - 12
    while off < len(image):
        n = min(cap, len(image) - off)
        add(pdev.KIND_TABLE_DATA,
            struct.pack(">QI", off, n) + image[off:off + n],
            "data@%d" % off)
        off += n
    add(pdev.KIND_TABLE_COMMIT, struct.pack(">II", want_id, 0),
        "commit-refused")
    add(pdev.KIND_TABLE_STATUS_REQUEST, b"", "status-after-refusal")
    # The baked table must still be scanning after the refusal.
    add_wire(W_NOM2, "wire-after-refusal", True)
    add(pdev.KIND_PERF_REQUEST, b"", "perf-final")

    beats = []
    for fbytes, src in frames:
        tu = (src << 16) | (len(fbytes) & 0xFFFF)
        for data, keep, last in frame_to_beats(fbytes):
            beats.append((data, keep, last, tu))
    print("driving %d frames / %d beats" % (len(frames), len(beats)))

    # ---- run it ---------------------------------------------------------
    work = tempfile.mkdtemp(prefix="pyro_rom_diff_")
    geom = rc.emit_memh(image, work)
    rtl = _wrap.generate_rp_child(otable.strong_id(image).hex(),
                                  max_frame_bytes=MAXFRAME,
                                  engine_backpressure=True)
    with open(os.path.join(work, "rp.v"), "w") as f:
        f.write(rtl)
    with open(os.path.join(work, "eng.v"), "w") as f:
        f.write(open(os.path.join(
            REPO, "hw", "rtl", "pyro_ac_rom_engine.v")).read())
        f.write(rc.alias_verilog(geom, epoch=ST.ROM_EPOCH))

    with open(os.path.join(work, "stim_d.memh"), "w") as fd, \
         open(os.path.join(work, "stim_k.memh"), "w") as fk, \
         open(os.path.join(work, "stim_l.memb"), "w") as fl, \
         open(os.path.join(work, "stim_u.memh"), "w") as fu:
        for data, keep, last, tu in beats:
            fd.write("%s\n" % data[::-1].hex())
            fk.write("%016x\n" % keep)
            fl.write("%d\n" % (1 if last else 0))
            fu.write("%012x\n" % tu)

    env = dict(os.environ,
               PATH=os.path.join(vivado, "bin") + ":" + os.environ["PATH"])

    def run(cmd):
        p = subprocess.run(cmd, cwd=work, env=env, capture_output=True,
                           text=True)
        if p.returncode != 0:
            print("FAIL: %s\n%s\n%s"
                  % (cmd[0], p.stdout[-3000:], p.stderr[-2000:]))
            sys.exit(1)
        return p.stdout

    run(["xvlog", "-sv", "-d", "PYRO_BUILD16=0", "-d", "PYRO_RP_CHILD_ID=1",
         "eng.v", "rp.v", TB])
    run(["xelab", "-debug", "off", "tb_pyro_rp_wd", "-s", "tb",
         "-generic_top", "NBEATS=%d" % len(beats),
         "-generic_top", "RUNCYC=%d" % (len(image) * 4 + 8000),
         "-generic_top", "MAXB=%d" % max(4096, len(beats) + 16)])
    run(["xsim", "tb", "-runall"])

    with open(os.path.join(work, "out_beats.txt")) as f:
        replies = beats_to_frames(f.read().splitlines())
    print("got %d reply frames" % len(replies))

    # ---- check ----------------------------------------------------------
    errors = []
    by_seq, wire_replies = {}, []
    for fr in replies:
        try:
            dec = pdev.decode_frame(fr[14:], max_payload=MAXPAY)
        except pdev.PyroFrameError as exc:
            errors.append("undecodable reply: %s" % exc)
            continue
        if (dec.kind == pdev.KIND_MATCH_REPLY and len(dec.payload) >= 4
                and struct.unpack(">HH", dec.payload[0:4])[1] & 0x4):
            wire_replies.append(dec)
        else:
            by_seq[dec.seq] = dec

    for s, kind, tag in plan:
        if s not in by_seq:
            errors.append("no reply to seq %d (%s)" % (s, tag))

    def status_of(s):
        d = by_seq.get(s)
        if d is None or d.kind != pdev.KIND_TABLE_STATUS_REPLY:
            return None
        a, sh, ep, fl, nb, caps, err = struct.unpack(
            ">IIIIQII", d.payload[0:32])
        return pdev.TableStatus(a, sh, ep, fl, nb, caps, err)

    # 1. Baked identity, before any TABLE_* traffic ever ran.
    s_boot = [s for s, k, t in plan if t == "status-boot"][0]
    st = status_of(s_boot)
    if st is None:
        errors.append("no boot TABLE_STATUS_REPLY")
    else:
        print("boot status: active=0x%08x epoch=%d caps=%d bytes=%d "
              "flags=0x%x" % (st.active_table_id, st.epoch,
                              st.capacity_states, st.bytes_received,
                              st.status_flags))
        if st.active_table_id != want_id:
            errors.append("baked TABLE_ID 0x%08x != host CRC 0x%08x"
                          % (st.active_table_id, want_id))
        if st.epoch != ST.ROM_EPOCH:
            errors.append("baked epoch %d != %d" % (st.epoch, ST.ROM_EPOCH))
        if st.capacity_states != ac.n_states:
            errors.append("caps %d != baked n_states %d"
                          % (st.capacity_states, ac.n_states))

    # 3. Mixed-case host scan against the folded model.
    s_match = [s for s, k, t in plan if t == "match"][0]
    dm = by_seq.get(s_match)
    if dm is None or dm.kind != pdev.KIND_MATCH_REPLY:
        errors.append("no MATCH_REPLY")
    else:
        count, mstat = struct.unpack(">HH", dm.payload[0:4])
        epoch = struct.unpack(">I", dm.payload[4:8])[0]
        got = set()
        for i in range(count):
            base = 8 + i * 24
            _s, end, pid, _f = struct.unpack(
                "<QQII", dm.payload[base:base + 24])
            got.add((pid, end))
        want = {(pid, end) for pid, end in ref_matches}
        print("MATCH_REPLY: count=%d status=0x%x epoch=%d"
              % (count, mstat, epoch))
        if epoch != ST.ROM_EPOCH:
            errors.append("MATCH_REPLY epoch %d != baked %d (SR14')"
                          % (epoch, ST.ROM_EPOCH))
        if got != want:
            errors.append("case-fold matches differ:\n  device %s\n"
                          "  model  %s" % (sorted(got), sorted(want)))

    s_perf = [s for s, k, t in plan if t == "perf"][0]
    dp = by_seq.get(s_perf)
    if dp is None or dp.kind != pdev.KIND_PERF_REPLY:
        errors.append("no PERF_REPLY")
    elif len(dp.payload) < 32:
        errors.append("PERF_REPLY payload %d B, want 32" % len(dp.payload))
    else:
        cycles, nbytes = struct.unpack(">QQ", dp.payload[0:16])
        ws, wsc, wd, wn = struct.unpack(">IIII", dp.payload[16:32])
        print("PERF: cycles=%d bytes=%d (%.2f cyc/B); wire seen=%d "
              "scanned=%d drops=%d noms=%d"
              % (cycles, nbytes,
                 cycles / nbytes if nbytes else 0.0, ws, wsc, wd, wn))
        if nbytes != len(subject):
            errors.append("PERF bytes %d != subject length %d"
                          % (nbytes, len(subject)))
        if nbytes and not (4.9 <= cycles / nbytes <= 20):
            errors.append("PERF cycles/byte %.2f outside [4.9, 20]"
                          % (cycles / nbytes))
        n_nom1 = len(fold_scan(ac, W_NOM))
        if (ws, wsc, wd, wn) != (2, 2, 0, n_nom1):
            errors.append("wire counters (%d,%d,%d,%d) != (2,2,0,%d) — "
                          "a ROM child has NO epoch-0 drop window"
                          % (ws, wsc, wd, wn, n_nom1))

    # 4. The refused load: commit_err visible, baked table untouched.
    s_ref = [s for s, k, t in plan if t == "status-after-refusal"][0]
    str_ = status_of(s_ref)
    if str_ is None:
        errors.append("no status after the refused load")
    else:
        print("after refused load: active=0x%08x epoch=%d flags=0x%x"
              % (str_.active_table_id, str_.epoch, str_.status_flags))
        if str_.active_table_id != want_id:
            errors.append("refused load changed TABLE_ID to 0x%08x"
                          % str_.active_table_id)
        if str_.epoch != ST.ROM_EPOCH:
            errors.append("refused load bumped epoch to %d" % str_.epoch)
        if not (str_.status_flags & 0x8):
            errors.append("commit_err not visible after refused commit "
                          "(flags=0x%x)" % str_.status_flags)

    # 2 + wire replies (both nominating frames, in order).
    wire_exp = [(tag, wf, fold_scan(ac, wf)) for tag, wf in wire_want]
    if len(wire_replies) != len(wire_exp):
        errors.append("%d wire MATCH_REPLYs, expected %d"
                      % (len(wire_replies), len(wire_exp)))
    for i, (dec, (tag, wf, wm)) in enumerate(zip(wire_replies, wire_exp)):
        count, mstat = struct.unpack(">HH", dec.payload[0:4])
        epoch = struct.unpack(">I", dec.payload[4:8])[0]
        got = set()
        for j in range(count):
            base = 8 + j * 24
            _s, end, pid, _f = struct.unpack(
                "<QQII", dec.payload[base:base + 24])
            got.add((pid, end))
        want = set(wm)
        print("wire MATCH_REPLY[%d] (%s): count=%d status=0x%x epoch=%d "
              "slot=%d seq=%d" % (i, tag, count, mstat, epoch,
                                  dec.slot, dec.seq))
        if dec.flags != 0:
            errors.append("%s: header flags 0x%x != 0 (R78.3)"
                          % (tag, dec.flags))
        if dec.slot != SLOT or dec.seq != i:
            errors.append("%s: slot/seq (%d,%d) != (%d,%d)"
                          % (tag, dec.slot, dec.seq, SLOT, i))
        if mstat & 0x2:
            errors.append("%s: ERR bit set on a wire reply" % tag)
        if epoch != ST.ROM_EPOCH:
            errors.append("%s: epoch %d != baked %d — the boot sweep "
                          "did not latch" % (tag, epoch, ST.ROM_EPOCH))
        if got != want:
            errors.append("%s: matches differ:\n  device %s\n  model  %s"
                          % (tag, sorted(got), sorted(want)))

    # 5. Final wire accounting: 4 seen = 3 scanned + 1 dropped (the one
    # during the open load).
    s_pf = [s for s, k, t in plan if t == "perf-final"][0]
    dpf = by_seq.get(s_pf)
    if dpf is None or dpf.kind != pdev.KIND_PERF_REPLY or \
            len(dpf.payload) < 32:
        errors.append("no usable final PERF_REPLY")
    else:
        cycles, nbytes = struct.unpack(">QQ", dpf.payload[0:16])
        ws, wsc, wd, wn = struct.unpack(">IIII", dpf.payload[16:32])
        print("final PERF: bytes=%d wire seen=%d scanned=%d drops=%d "
              "noms=%d" % (nbytes, ws, wsc, wd, wn))
        want_noms = sum(len(m) for _, _, m in wire_exp)
        if (ws, wsc, wd, wn) != (4, 3, 1, want_noms):
            errors.append("final wire counters (%d,%d,%d,%d) != "
                          "(4,3,1,%d)" % (ws, wsc, wd, wn, want_noms))
        if ws != wsc + wd:
            errors.append("wire counter slack: seen %d != scanned %d "
                          "+ drops %d" % (ws, wsc, wd))

    if errors:
        print("\nROM_DIFF: FAIL")
        for e in errors:
            print("  - %s" % e)
        return 1
    print("\nROM_DIFF: PASS — baked identity live from configuration, "
          "wire-first reply, case fold exact, A5 load refused, counters "
          "slack-free")
    return 0


if __name__ == "__main__":
    sys.exit(main())
