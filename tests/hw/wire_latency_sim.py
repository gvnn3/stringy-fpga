#!/usr/bin/env python3
"""Beat-exact wire-frame latency of the generated pyro_rp under xsim.

Drives the exact RTL the card runs (pyro.hdl.rp_wrapper
generate_rp_child, max_frame_bytes=9600, engine_backpressure=True; a
sim-sized 4096-state engine via PYRO_OVERLAY_STATES for xsim speed)
through tb_pyro_rp_lat.v: load a 3-anchor table over the real R78
protocol -- the same three anchors as the silicon demo (the 14-byte
TX-generator L2 prefix, the 6-byte broadcast dst, the 8-byte
src+ethertype) -- then send TEN 64-byte synthetic TX-generator wire
frames (tuser src 0x0040) with idle gaps and read the testbench's
LAT_* timestamps.

Measured per wire frame, in 250 MHz cycles (4 ns each):

  latency_first  last input beat accepted -> first reply beat
                 presented on m_axis (m_axis_tvalid rise)
  latency_last   last input beat accepted -> last reply beat accepted
  busy           last input beat accepted -> s_axis_tready back high
                 (bounds the max sustainable wire frame rate)

This is the WHOLE wrapper path, frame arrival -> MATCH_REPLY.  The
engine core consumes ~5 cycles/byte at its FSM floor, so feeding the
64-byte frame alone is ~320+ cycles; total latency should land around
350-600 cycles.  The R45a PERF counters on silicon time only the scan
core -- this simulation's number is the end-to-end wrapper figure
those counters cannot see.

Prints a greppable summary block:

    WIRE_LAT_FIRST_CYCLES min=.. med=.. max=..
    WIRE_LAT_LAST_CYCLES  min=.. med=.. max=..
    WIRE_BUSY_CYCLES      min=.. med=.. max=..

Run:  .venv-pyro/bin/python3 tests/hw/wire_latency_sim.py
Exit: 0 pass; 1 on any inconsistency (latency spread across the ten
frames, missing/extra replies, reply/model mismatch); 2 if the pinned
Vivado is absent (honest skip).
"""
import os
import statistics
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
TB = os.path.join(TBDIR, "tb_pyro_rp_lat.v")
SLOT = 1
ENGINE_ID = 0x0A5E0001
MAXFRAME = 9600
MAXPAY = pdev.MAX_PAYLOAD_JUMBO
ETH_REQ = bytes(6) + b"\x02\x00\x00\x00\x00\x01" + b"\x88\xb5"

# tuser = {dst[15:0], src[15:0], size[15:0]} (R80); src bit 6 marks
# a wire frame (packet_adapter_rx.sv CMAC-0 tag).
SRC_HOST = 0x0001
SRC_WIRE = 0x0040

N_WIRE = 10        # wire frames to time
GAPCYC = 6000      # idle cycles after every accepted tlast
RUNCYC = 10000     # drain time after the last beat
SPREAD_MAX = 64    # allowed max-min cycle spread.  Latency is
                   # deterministic in the CONTENT, not constant
                   # across frames: the ten frames differ only in
                   # the 4 LE seq bytes at offset 14, and seq=2's
                   # leading 0x02 walks anchor 2's prefix
                   # (02 00 00 00 00 01 88 b5) for ~6 bytes at
                   # match-path cost (~9 vs 5 cyc/B) -- a real
                   # ~25-cycle excursion, not harness noise.
NS_PER_CYC = 4    # 250 MHz

# The silicon demo anchors, over the TX generator's fixed L2 header:
# bytes 0-5 dst ff:ff:ff:ff:ff:ff, 6-11 src 02:00:00:00:00:01,
# 12-13 ethertype 88b5.  pid = list index (otable.build).
A_SRC_ET = b"\x02\x00\x00\x00\x00\x01" + b"\x88\xb5"
ANCHORS = [b"\xff" * 6 + A_SRC_ET, b"\xff" * 6, A_SRC_ET]


def tx_gen_frame(seq):
    """One synthetic TX-generator frame: 64 bytes, 32-bit LE seq at
    byte 14, zeros to the end (hw/pyro_plugin/pyro_wire_tx_gen.sv)."""
    hdr = b"\xff" * 6 + b"\x02\x00\x00\x00\x00\x01" + b"\x88\xb5"
    f = hdr + struct.pack("<I", seq) + bytes(46)
    assert len(f) == 64
    return f


def frame_to_beats(frame):
    """Split an Ethernet frame into 64-byte AXIS beats."""
    out = []
    for off in range(0, len(frame), 64):
        chunk = frame[off:off + 64]
        keep = (1 << len(chunk)) - 1
        out.append((chunk.ljust(64, b"\x00"), keep,
                    off + 64 >= len(frame)))
    return out


def beats_to_frames(lines):
    """Reassemble reply frames from the testbench's beat log."""
    frames, cur = [], b""
    for ln in lines:
        parts = ln.split()
        if len(parts) != 3:
            continue
        data, keep = int(parts[0], 16), int(parts[1], 16)
        raw = data.to_bytes(64, "little")
        cur += raw[:bin(keep).count("1")]
        if parts[2] == "1":
            frames.append(cur)
            cur = b""
    return frames


def req(kind, seq, payload):
    return ETH_REQ + pdev.encode_frame(kind, SLOT, seq, payload,
                                       max_payload=MAXPAY)


def first_in(vals, lo, hi):
    """First timestamp strictly after lo and at most hi, else None."""
    for v in vals:
        if lo < v <= hi:
            return v
    return None


def stats(vals):
    return min(vals), statistics.median(vals), max(vals)


def main(argv=None):
    vivado = os.environ.get("PYRO_VIVADO", "/usr/local/cad/2025.2/Vivado")
    xvlog = os.path.join(vivado, "bin", "xvlog")
    if not os.path.exists(xvlog):
        print("SKIP: no Vivado at %s" % vivado)
        return 2

    # ---- table + host-model oracle --------------------------------------
    ac = otable.build(ANCHORS)
    image = otable.serialize(ac, engine_id=ENGINE_ID)
    want_id = otable.table_id(image)
    ref = omodel.OverlayEngineModel(engine_id=ENGINE_ID)
    ref.table_begin(len(image), want_id, ENGINE_ID,
                    otable.TABLE_FORMAT_VERSION, ac.n_states, len(ANCHORS))
    ref.table_data(0, image)
    ref_epoch = ref.table_commit(want_id)
    print("table: %d bytes, %d states, table_id=0x%08x, epoch=%d"
          % (len(image), ac.n_states, want_id, ref_epoch))

    wire_frames = [tx_gen_frame(s) for s in range(N_WIRE)]
    wire_exp = [ref.scan(f)[0] for f in wire_frames]

    # ---- frame sequence: real protocol load, then the wire frames -------
    cap = MAXPAY - 12
    frames, seq = [], 0

    def add(kind, payload):
        nonlocal seq
        seq += 1
        frames.append((req(kind, seq, payload), SRC_HOST))

    add(pdev.KIND_TABLE_BEGIN,
        struct.pack(">IIQIHH", otable.TABLE_FORMAT_VERSION, ENGINE_ID,
                    len(image), want_id, 0, 0))
    off = 0
    while off < len(image):
        n = min(cap, len(image) - off)
        add(pdev.KIND_TABLE_DATA,
            struct.pack(">QI", off, n) + image[off:off + n])
        off += n
    add(pdev.KIND_TABLE_COMMIT, struct.pack(">II", want_id, 0))
    n_table = len(frames)
    for f in wire_frames:
        frames.append((f, SRC_WIRE))

    beats = []
    for fbytes, src in frames:
        tu = (src << 16) | (len(fbytes) & 0xFFFF)
        for data, keep, last in frame_to_beats(fbytes):
            beats.append((data, keep, last, tu))
    print("driving %d table frames + %d wire frames / %d beats"
          % (n_table, N_WIRE, len(beats)))

    # ---- run it ---------------------------------------------------------
    work = tempfile.mkdtemp(prefix="pyro_wire_lat_")
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

    # Small engine for simulation speed only (see overlay_table_diff.py).
    run(["xvlog", "-sv", "-d", "PYRO_BUILD16=0", "-d", "PYRO_RP_CHILD_ID=1",
         "-d", "PYRO_OVERLAY_STATES=4096",
         "eng.v", "rp.v", TB])
    run(["xelab", "-debug", "off", "tb_pyro_rp_lat", "-s", "tb",
         "-generic_top", "NBEATS=%d" % len(beats),
         "-generic_top", "RUNCYC=%d" % RUNCYC,
         "-generic_top", "GAPCYC=%d" % GAPCYC,
         "-generic_top", "MAXB=%d" % max(4096, len(beats) + 16)])
    out = run(["xsim", "tb", "-runall"])

    # ---- parse the LAT_* timestamps -------------------------------------
    lat = {"LAT_IN": [], "LAT_OUT_FIRST": [], "LAT_OUT_LAST": [],
           "LAT_READY": [], "LAT_UNREADY": []}
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) == 2 and parts[0] in lat:
            lat[parts[0]].append(int(parts[1]))

    errors = []
    if "TB_HANG" in out:
        errors.append("testbench hang: %s"
                      % [l for l in out.splitlines() if "TB_HANG" in l])
    if "TB_DONE" not in out:
        errors.append("no TB_DONE -- simulation did not finish")

    ins = lat["LAT_IN"]
    if len(ins) != n_table + N_WIRE:
        errors.append("%d LAT_IN lines, expected %d (one per frame)"
                      % (len(ins), n_table + N_WIRE))
    if len(lat["LAT_OUT_FIRST"]) != n_table + N_WIRE:
        errors.append("%d LAT_OUT_FIRST lines, expected %d (one reply "
                      "per frame)"
                      % (len(lat["LAT_OUT_FIRST"]), n_table + N_WIRE))
    if errors:
        print("\nWIRE_LAT: FAIL")
        for e in errors:
            print("  - %s" % e)
        return 1

    wire_in = ins[n_table:]
    # Every table-op reply must be over before the first wire frame is
    # accepted, or ordering-based pairing is unsound.
    if lat["LAT_OUT_LAST"][n_table - 1] >= wire_in[0]:
        errors.append("table-op replies overlap the wire phase "
                      "(last table reply at %d, first wire accept %d) "
                      "-- raise GAPCYC"
                      % (lat["LAT_OUT_LAST"][n_table - 1], wire_in[0]))

    INF = 1 << 62
    lat_first, lat_last, busy = [], [], []
    for i, t in enumerate(wire_in):
        nxt = wire_in[i + 1] if i + 1 < len(wire_in) else INF
        of = first_in(lat["LAT_OUT_FIRST"], t, nxt)
        ol = first_in(lat["LAT_OUT_LAST"], t, nxt)
        if of is None or ol is None:
            errors.append("wire frame %d (in@%d): no reply before the "
                          "next frame" % (i, t))
            continue
        un = first_in(lat["LAT_UNREADY"], t, nxt)
        if un is None:
            b = 0          # ready never dropped: back-to-back capable
        else:
            rd = first_in(lat["LAT_READY"], un, INF)
            if rd is None:
                errors.append("wire frame %d: tready never rose again"
                              % i)
                continue
            b = rd - t
        lat_first.append(of - t)
        lat_last.append(ol - t)
        busy.append(b)
        print("wire frame %d: in@%d first=%d last=%d busy=%d"
              % (i, t, of - t, ol - t, b))

    # ---- replies: one per wire frame, matching the model ----------------
    with open(os.path.join(work, "out_beats.txt")) as f:
        replies = beats_to_frames(f.read().splitlines())
    wire_replies = []
    for fr in replies:
        try:
            dec = pdev.decode_frame(fr[14:], max_payload=MAXPAY)
        except pdev.PyroFrameError as exc:
            errors.append("undecodable reply: %s" % exc)
            continue
        if (dec.kind == pdev.KIND_MATCH_REPLY and len(dec.payload) >= 4
                and struct.unpack(">HH", dec.payload[0:4])[1] & 0x4):
            wire_replies.append(dec)
    if len(wire_replies) != N_WIRE:
        errors.append("%d wire MATCH_REPLYs, expected %d"
                      % (len(wire_replies), N_WIRE))
    for i, (dec, wm) in enumerate(zip(wire_replies, wire_exp)):
        count, mstat = struct.unpack(">HH", dec.payload[0:4])
        epoch = struct.unpack(">I", dec.payload[4:8])[0]
        got = set()
        for j in range(count):
            base = 8 + j * 24
            start, end, pid, flags = struct.unpack(
                "<QQII", dec.payload[base:base + 24])
            got.add((pid, end))
        want = {(m.pattern_id, m.end) for m in wm}
        if count < 3:
            errors.append("wire reply %d: %d entries, want 3+"
                          % (i, count))
        if got != want:
            errors.append("wire reply %d: matches differ:\n"
                          "  device %s\n  model  %s"
                          % (i, sorted(got), sorted(want)))
        if dec.flags != 0:
            errors.append("wire reply %d: header flags 0x%x != 0"
                          % (i, dec.flags))
        if dec.slot != SLOT:
            errors.append("wire reply %d: slot %d != %d"
                          % (i, dec.slot, SLOT))
        if dec.seq != i:
            errors.append("wire reply %d: wire seq %d != %d"
                          % (i, dec.seq, i))
        if epoch != ref_epoch:
            errors.append("wire reply %d: epoch %d != %d"
                          % (i, epoch, ref_epoch))

    # ---- report ---------------------------------------------------------
    if len(lat_first) == N_WIRE:
        for name, vals in (("WIRE_LAT_FIRST_CYCLES", lat_first),
                           ("WIRE_LAT_LAST_CYCLES ", lat_last),
                           ("WIRE_BUSY_CYCLES     ", busy)):
            lo, med, hi = stats(vals)
            print("%s min=%d med=%g max=%d" % (name, lo, med, hi))
            print("%s  min=%gns med=%gns max=%gns (at %d ns/cycle)"
                  % (name.strip().replace("_CYCLES", "_NS"),
                     lo * NS_PER_CYC, med * NS_PER_CYC,
                     hi * NS_PER_CYC, NS_PER_CYC))
            if hi - lo > SPREAD_MAX:
                errors.append("%s spread %d > %d cycles -- the pipeline "
                              "should be deterministic"
                              % (name.strip(), hi - lo, SPREAD_MAX))
    else:
        errors.append("only %d of %d wire frames produced timings"
                      % (len(lat_first), N_WIRE))

    if errors:
        print("\nWIRE_LAT: FAIL")
        for e in errors:
            print("  - %s" % e)
        return 1
    print("\nWIRE_LAT: PASS -- %d wire frames, one 3-entry reply each, "
          "deterministic latency" % N_WIRE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
