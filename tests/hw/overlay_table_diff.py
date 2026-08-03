#!/usr/bin/env python3
"""A5 §3 differential: drive a real table load through pyro_rp under xsim.

The engine differential (``tb_overlay_engine_multi.v``) proves the *engine*
matches the model.  It says nothing about whether a host can ever get a
table into that engine, because it pokes the engine's ports directly.  This
drives the actual wire protocol -- TABLE_BEGIN / TABLE_DATA / TABLE_COMMIT /
TABLE_STATUS_REQUEST / MATCH_REQUEST as R78 frames -- through the generated
``pyro_rp`` wrapper, which is the path the card will take.

That distinction is the whole reason this file exists.  The engine met
timing and passed its differential while the wrapper had no way to reach
its table CSRs at all: the road to the engine was missing and no
engine-level test could have noticed.

Checks, in order of what would hurt most to get wrong:

  1. the committed ``active_table_id`` equals the host's CRC-32C of the
     image -- end-to-end identity (A5 §2.1), not a device self-report
  2. ``epoch`` increments on commit and reaches the host in MATCH_REPLY
     bytes 4-7 (SR14' attribution)
  3. matches equal :mod:`pyro.overlay.model` on the same subject
  4. an out-of-order chunk is REFUSED (error 8) and leaves the active table
     untouched -- the fail-closed property, tested by trying to break it

Run:  .venv-pyro/bin/python3 tests/hw/overlay_table_diff.py
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
import pyro.hdl.rp_wrapper as _wrap             # noqa: E402
from pyro.overlay import model as omodel        # noqa: E402
from pyro.overlay import table as otable        # noqa: E402

TBDIR = os.path.dirname(os.path.abspath(__file__))
TB = os.path.join(TBDIR, "tb_pyro_rp_wd.v")
SLOT = 1
ENGINE_ID = 0x0A5E0001
MAXFRAME = 9600
MAXPAY = pdev.MAX_PAYLOAD_JUMBO
ETH_REQ = bytes(6) + b"\x02\x00\x00\x00\x00\x01" + b"\x88\xb5"


def frame_to_beats(frame: bytes):
    """Split an Ethernet frame into 64-byte AXIS beats."""
    out = []
    for off in range(0, len(frame), 64):
        chunk = frame[off:off + 64]
        keep = (1 << len(chunk)) - 1
        out.append((chunk.ljust(64, b"\x00"), keep, off + 64 >= len(frame)))
    return out


def beats_to_frames(lines):
    """Reassemble reply frames from the testbench's beat log."""
    frames, cur = [], b""
    for ln in lines:
        parts = ln.split()
        if len(parts) != 3:
            continue
        data, keep, last = int(parts[0], 16), int(parts[1], 16), parts[2] == "1"
        raw = data.to_bytes(64, "little")
        n = bin(keep).count("1")
        cur += raw[:n]
        if last:
            frames.append(cur)
            cur = b""
    return frames


def req(kind, seq, payload):
    return ETH_REQ + pdev.encode_frame(kind, SLOT, seq, payload,
                                       max_payload=MAXPAY)


def main(argv=None):
    vivado = os.environ.get("PYRO_VIVADO", "/usr/local/cad/2025.2/Vivado")
    xvlog = os.path.join(vivado, "bin", "xvlog")
    if not os.path.exists(xvlog):
        print("SKIP: no Vivado at %s" % vivado)
        return 2

    # ---- build a small but non-trivial table ---------------------------
    anchors = [b"attack", b"tack", b"ack", b"malware", b"ware"]
    ac = otable.build(anchors)
    image = otable.serialize(ac, engine_id=ENGINE_ID)
    want_id = otable.table_id(image)
    subject = b"the attack used malware and tackled ack"

    ref = omodel.OverlayEngineModel(engine_id=ENGINE_ID)
    ref.table_begin(len(image), want_id, ENGINE_ID,
                    otable.TABLE_FORMAT_VERSION, ac.n_states, len(anchors))
    ref.table_data(0, image)
    ref_epoch = ref.table_commit(want_id)
    ref_matches, _ = ref.scan(subject)
    print("table: %d bytes, %d states, table_id=0x%08x" %
          (len(image), ac.n_states, want_id))
    print("model: epoch=%d, %d matches on the subject"
          % (ref_epoch, len(ref_matches)))

    # ---- compose the frame sequence -------------------------------------
    cap = MAXPAY - 12
    frames, plan = [], []
    seq = 0

    def add(kind, payload, tag):
        nonlocal seq
        seq += 1
        frames.append(req(kind, seq, payload))
        plan.append((seq, kind, tag))

    add(pdev.KIND_TABLE_BEGIN,
        struct.pack(">IIQIHH", otable.TABLE_FORMAT_VERSION, ENGINE_ID,
                    len(image), want_id, 0, 0), "begin")
    off = 0
    while off < len(image):
        n = min(cap, len(image) - off)
        add(pdev.KIND_TABLE_DATA,
            struct.pack(">QI", off, n) + image[off:off + n],
            "data@%d" % off)
        off += n
    add(pdev.KIND_TABLE_COMMIT, struct.pack(">II", want_id, 0), "commit")
    add(pdev.KIND_TABLE_STATUS_REQUEST, b"", "status")
    add(pdev.KIND_MATCH_REQUEST, struct.pack(">IIHH", 0, 0, 61, 0) + subject,
        "match")
    # R45a counters for the scan just performed.  The wrapper resets them
    # before each scan (W4), so this must show exactly that scan: BYTES equal
    # to the subject length, CYCLES showing the ~8 cycles/byte signature.
    # The old ST_PERF read the overlay child's registered CSR bus a cycle
    # early and returned HARNESS_VER constants that decoded plausibly -- an
    # exact BYTES check is what makes that failure mode impossible to miss.
    add(pdev.KIND_PERF_REQUEST, b"", "perf")
    # Fail-closed probe: a chunk claiming an offset the engine is not at.
    # It must be refused, and the active table must survive it.
    add(pdev.KIND_TABLE_DATA, struct.pack(">QI", 999999, 4) + b"XXXX",
        "bad-offset")
    add(pdev.KIND_TABLE_STATUS_REQUEST, b"", "status-after-bad")

    # A5 §5, the property that matters most: a transfer corrupted IN FLIGHT
    # must be refused and must leave the working table alone.  Declare the
    # good image's CRC at BEGIN, then send bytes that differ.  Corrupting the
    # image before the host sees it proves nothing -- both ends would simply
    # agree on the corrupted content, which is how this was first mis-tested.
    corrupt = bytearray(image)
    corrupt[len(corrupt) // 2] ^= 0xFF
    add(pdev.KIND_TABLE_BEGIN,
        struct.pack(">IIQIHH", otable.TABLE_FORMAT_VERSION, ENGINE_ID,
                    len(image), want_id, 0, 0), "begin-corrupt")
    off = 0
    while off < len(corrupt):
        n = min(cap, len(corrupt) - off)
        add(pdev.KIND_TABLE_DATA,
            struct.pack(">QI", off, n) + bytes(corrupt[off:off + n]),
            "data-corrupt@%d" % off)
        off += n
    add(pdev.KIND_TABLE_COMMIT, struct.pack(">II", want_id, 0),
        "commit-corrupt")

    beats = [b for f in frames for b in frame_to_beats(f)]
    print("driving %d frames / %d beats" % (len(frames), len(beats)))

    # ---- run it ---------------------------------------------------------
    work = tempfile.mkdtemp(prefix="pyro_tbl_diff_")
    rtl = _wrap.generate_rp_child("0a5e000100000000000000000000a5e1",
                                  max_frame_bytes=MAXFRAME,
                                  engine_backpressure=True)
    with open(os.path.join(work, "rp.v"), "w") as f:
        f.write(rtl)
    with open(os.path.join(work, "eng.v"), "w") as f:
        for name in ("pyro_overlay_engine.v", "pyro_circuit_overlay_top.v"):
            f.write(open(os.path.join(REPO, "hw", "rtl", name)).read())
    with open(os.path.join(work, "stim_d.memh"), "w") as fd, \
         open(os.path.join(work, "stim_k.memh"), "w") as fk, \
         open(os.path.join(work, "stim_l.memb"), "w") as fl:
        for data, keep, last in beats:
            fd.write("%s\n" % data[::-1].hex())
            fk.write("%016x\n" % keep)
            fl.write("%d\n" % (1 if last else 0))

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

    # Small engine for simulation speed only -- xsim walks every array, and
    # the shipped 40960-state size makes this differential take tens of
    # minutes for no extra coverage: the table under test has 24 states.
    # Synthesis never sees this define (see pyro_circuit_overlay_top.v).
    run(["xvlog", "-sv", "-d", "PYRO_BUILD16=0", "-d", "PYRO_RP_CHILD_ID=1",
         "-d", "PYRO_OVERLAY_STATES=4096",
         "eng.v", "rp.v", TB])
    run(["xelab", "-debug", "off", "tb_pyro_rp_wd", "-s", "tb",
         "-generic_top", "NBEATS=%d" % len(beats),
         # The tb runs RUNCYC cycles AFTER the last beat, unconditionally, so
         # this is pure simulated time -- size it to the work, not to a round
         # number.  The load is one byte/cycle plus a few hundred cycles of
         # per-frame overhead; 4x the image plus 8k covers the scan and the
         # two status round-trips with room to spare.
         "-generic_top", "RUNCYC=%d" % (len(image) * 4 + 8000),
         "-generic_top", "MAXB=%d" % max(4096, len(beats) + 16)])
    run(["xsim", "tb", "-runall"])

    with open(os.path.join(work, "out_beats.txt")) as f:
        replies = beats_to_frames(f.read().splitlines())
    print("got %d reply frames" % len(replies))

    # ---- check ----------------------------------------------------------
    errors = []
    by_seq = {}
    for fr in replies:
        try:
            dec = pdev.decode_frame(fr[14:], max_payload=MAXPAY)
        except pdev.PyroFrameError as exc:
            errors.append("undecodable reply: %s" % exc)
            continue
        by_seq[dec.seq] = dec

    def status_of(s):
        d = by_seq.get(s)
        if d is None or d.kind != pdev.KIND_TABLE_STATUS_REPLY:
            return None
        a, sh, ep, fl, nb, caps, err = struct.unpack(">IIIIQII",
                                                     d.payload[0:32])
        return pdev.TableStatus(a, sh, ep, fl, nb, caps, err)

    for s, kind, tag in plan:
        if s not in by_seq:
            errors.append("no reply to seq %d (%s)" % (s, tag))

    s_commit = [s for s, k, t in plan if t == "commit"][0]
    st = status_of(s_commit)
    if st is None:
        errors.append("commit produced no TABLE_STATUS_REPLY")
    else:
        print("after commit: active=0x%08x shadow=0x%08x epoch=%d bytes=%d "
              "caps=%d err=%d flags=0x%x (load_open=%s active_valid=%s)"
              % (st.active_table_id, st.shadow_table_id,
                 st.epoch, st.bytes_received, st.capacity_states,
                 st.error, st.status_flags, st.load_open, st.active_valid))
        if st.active_table_id != want_id:
            errors.append("active_table_id 0x%08x != host CRC 0x%08x"
                          % (st.active_table_id, want_id))
        if st.epoch != 1:
            errors.append("epoch %d != 1 after first commit" % st.epoch)
        if st.bytes_received != len(image):
            errors.append("device took %d of %d bytes"
                          % (st.bytes_received, len(image)))
        if st.error:
            errors.append("device error %d on commit" % st.error)

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
            # R47 entries are LITTLE-endian (the host C struct), while the
            # PYRO control header around them is big-endian.  Mixing the two
            # up reads 10 as 0x0A00000000000000, which looks like a device
            # fault and is not one -- cf. xsim_diff.expect_match.
            start, end, pid, flags = struct.unpack(
                "<QQII", dm.payload[base:base + 24])
            got.add((pid, end))
        want = {(m.pattern_id, m.end) for m in ref_matches}
        print("MATCH_REPLY: count=%d status=0x%x epoch=%d"
              % (count, mstat, epoch))
        if epoch != ref_epoch:
            errors.append("MATCH_REPLY epoch %d != %d (SR14')"
                          % (epoch, ref_epoch))
        if got != want:
            errors.append("matches differ:\n  device %s\n  model  %s"
                          % (sorted(got), sorted(want)))

    s_perf = [s for s, k, t in plan if t == "perf"][0]
    dp = by_seq.get(s_perf)
    if dp is None or dp.kind != pdev.KIND_PERF_REPLY:
        errors.append("no PERF_REPLY (kind=%s)"
                      % (hex(dp.kind) if dp else None))
    else:
        cycles, nbytes = struct.unpack(">QQ", dp.payload[0:16])
        print("PERF_REPLY: cycles=%d bytes=%d (%.2f cyc/B over %d B subject)"
              % (cycles, nbytes, cycles / nbytes if nbytes else 0.0,
                 len(subject)))
        if nbytes != len(subject):
            errors.append("PERF bytes %d != subject length %d — the R45a "
                          "read is broken again" % (nbytes, len(subject)))
        # Bounds from the FSM, not from vibes.  A root-miss byte takes
        # exactly 5 cycles (IDLE->FETCH->FETCH2->RANK->RANK2), so a
        # match-free subject sits just above 5 cyc/B -- measured 5.08 on
        # silicon -- and match-heavy subjects run higher (9.28 here).
        # The first version of this band said [6, 20] and only passed
        # because this subject has matches; the floor is 5.
        if nbytes and not (4.9 <= cycles / nbytes <= 20):
            errors.append("PERF cycles/byte %.2f outside [4.9, 20]"
                          % (cycles / nbytes))

    s_bad = [s for s, k, t in plan if t == "bad-offset"][0]
    stb = status_of(s_bad)
    if stb is None:
        errors.append("bad-offset chunk produced no status reply")
    elif stb.error != pdev.PYRO_E_TABLE_SEQUENCE:
        errors.append("out-of-order chunk NOT refused: error=%d (want %d)"
                      % (stb.error, pdev.PYRO_E_TABLE_SEQUENCE))
    else:
        print("out-of-order chunk refused with error %d (as designed)"
              % stb.error)

    s_after = [s for s, k, t in plan if t == "status-after-bad"][0]
    sta = status_of(s_after)
    if sta is None:
        errors.append("no status after the bad chunk")
    elif sta.active_table_id != want_id:
        errors.append("active table was damaged by a refused chunk: "
                      "0x%08x != 0x%08x" % (sta.active_table_id, want_id))
    else:
        print("active table survived the refused chunk (0x%08x)"
              % sta.active_table_id)

    s_cc = [s for s, k, t in plan if t == "commit-corrupt"][0]
    stcc = status_of(s_cc)
    if stcc is None:
        errors.append("corrupted commit produced no status reply")
    elif stcc.active_table_id != want_id:
        errors.append(
            "A5 §5 VIOLATION: a transfer whose CRC differs from the declared "
            "0x%08x was COMMITTED (active now 0x%08x) — the working table was "
            "destroyed by a bad load" % (want_id, stcc.active_table_id))
    else:
        print("corrupted transfer refused; working table intact (0x%08x, "
              "epoch %d, commit_err=%s)"
              % (stcc.active_table_id, stcc.epoch,
                 bool(stcc.status_flags & 0x8)))

    if errors:
        print("\nTBL_DIFF: FAIL")
        for e in errors:
            print("  - %s" % e)
        return 1
    print("\nTBL_DIFF: PASS — table loaded, committed, attributed, and matched "
          "over the real wire protocol")
    return 0


if __name__ == "__main__":
    sys.exit(main())
