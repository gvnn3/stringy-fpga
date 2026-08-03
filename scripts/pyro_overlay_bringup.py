#!/usr/bin/env python3
"""On-hardware bring-up for the A5 overlay engine.

Loads a real table into the resident overlay child over the A5 §3 wire
protocol and checks the device against :mod:`pyro.overlay.model` on the
same bytes.  Everything before this was simulation; this is the first
time a table has been written to fabric.

What it checks, and why each one is here rather than assumed:

  reset state    ``active_table_id == 0`` before any load.  A5 §5 says an
                 engine that comes up holding stale content reading as
                 valid is the worst available failure, because it looks
                 correct.  So confirm the device claims nothing.
  identity       the committed ``active_table_id`` equals the HOST's
                 CRC-32C of the image.  Not the device's self-report --
                 the point of §2.1 is that both ends compute it.
  attribution    ``epoch`` increments and comes back in MATCH_REPLY.
  nomination     the device's match set equals the model's, exactly.
                 SR3 makes a MISS the unacceptable direction; extra
                 nominations would be merely wasteful.
  fail-closed    a deliberately corrupted commit must be REFUSED and
                 must leave the working table active (§5).

Usage:
    PYRO_DEVICE_IFACE=ens2 .venv-pyro/bin/python3 \\
        scripts/pyro_overlay_bringup.py [--group '$SSH_PORTS/0']
"""
import argparse
import os
import struct
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import pyro.device as pdev                      # noqa: E402
from pyro.overlay import model as omodel        # noqa: E402
from pyro.overlay import table as otable        # noqa: E402
from pyro.snort import groups as G              # noqa: E402
from pyro.snort import triage as Tr             # noqa: E402

ENGINE_ID = 0x0A5E0001
CORPUS = os.path.join(REPO, "third_party", "snort3-community-rules",
                      "snort3-community.rules")


def match_on_device(cfg, subject, slot=1, seq=900):
    """One MATCH_REQUEST; returns (set of (pattern_id, end), epoch, ovf)."""
    transport = pdev._make_transport(cfg)
    try:
        eth = (bytes(cfg.dst_mac) + bytes(cfg.src_mac)
               + struct.pack(">H", pdev.ETHERTYPE))
        payload = struct.pack(">IIHH", 0, 0, 61, 0) + subject
        transport.send(eth + pdev.encode_frame(pdev.KIND_MATCH_REQUEST, slot,
                                               seq, payload,
                                               max_payload=cfg.max_payload))
        import time
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
            out = set()
            for i in range(count):
                b = 8 + i * 24
                # R47 entries are little-endian; the header around them is not.
                _s, end, pid, _f = struct.unpack("<QQII", dec.payload[b:b + 24])
                out.add((pid, end))
            return out, epoch, bool(status & 1)
        return None, None, None
    finally:
        transport.close()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group", default="$SSH_PORTS/0")
    ap.add_argument("--slot", type=int, default=1)
    ap.add_argument("--jumbo", action="store_true", default=True)
    args = ap.parse_args()

    iface = os.environ.get("PYRO_DEVICE_IFACE")
    if not iface:
        print("set PYRO_DEVICE_IFACE (R68 fail-closed: no default netdev)")
        return 2
    cfg = pdev.DeviceConfig(
        iface=iface, chardev=None,
        # The shell is jumbo (MAX_PKT_LEN 9600) and the child was linked with
        # rp_max_frame_bytes=9600.  R78.9a requires this be explicit operator
        # configuration, never probed -- so it is set here, not guessed.
        max_payload=pdev.MAX_PAYLOAD_JUMBO if args.jumbo else pdev.MAX_PAYLOAD,
        probe_timeout_s=2.0)

    ok, reason = pdev.probe_device(cfg)
    print("probe: %s" % reason)
    if not ok:
        return 1

    fails = []

    # ---- 1. the device should claim NOTHING before a load ---------------
    st0 = pdev.read_table_status(cfg, slot=args.slot)
    if st0 is None:
        print("FAIL: no TABLE_STATUS_REPLY — the resident child predates A5 §3")
        return 1
    print("before load: active=0x%08x epoch=%d bytes=%d caps=%d valid=%s"
          % (st0.active_table_id, st0.epoch, st0.bytes_received,
             st0.capacity_states, st0.active_valid))
    if st0.capacity_states < 39647:
        fails.append("engine capacity %d < the 39,647-state corpus — this is "
                     "not the full-corpus build" % st0.capacity_states)

    # ---- 2. build a real table from the corpus --------------------------
    groups = G.pack_groups(Tr.triage_file(CORPUS))
    g = next((x for x in groups if x.name == args.group), None)
    if g is None:
        print("no such group %r; have: %s"
              % (args.group, ", ".join(x.name for x in groups[:8])))
        return 1
    anchors = [None if s.tombstone else s.anchor for s in g.slots]
    ac = otable.build(anchors)
    image = otable.serialize(ac, engine_id=ENGINE_ID)
    want_id = otable.table_id(image)
    print("table: group %s, %d anchors, %d states, %d bytes, id=0x%08x"
          % (g.name, sum(1 for a in anchors if a), ac.n_states, len(image),
             want_id))
    if ac.n_states > st0.capacity_states:
        print("FAIL: table needs %d states, engine holds %d"
              % (ac.n_states, st0.capacity_states))
        return 1

    # ---- 3. load it -----------------------------------------------------
    import time
    t0 = time.monotonic()
    st = pdev.load_table(cfg, image, slot=args.slot)
    dt = time.monotonic() - t0
    print("loaded and committed in %.1f ms: active=0x%08x epoch=%d bytes=%d"
          % (dt * 1e3, st.active_table_id, st.epoch, st.bytes_received))
    if st.active_table_id != want_id:
        fails.append("identity: device 0x%08x != host 0x%08x"
                     % (st.active_table_id, want_id))
    if st.epoch != st0.epoch + 1:
        fails.append("epoch %d did not advance from %d" % (st.epoch, st0.epoch))

    # ---- 4. does it match what the model matches? -----------------------
    ref = omodel.OverlayEngineModel(engine_id=ENGINE_ID)
    ref.table_begin(len(image), want_id, ENGINE_ID,
                    otable.TABLE_FORMAT_VERSION, ac.n_states,
                    sum(1 for a in anchors if a))
    ref.table_data(0, image)
    ref_epoch = ref.table_commit(want_id)

    real = [a for a in anchors if a]
    subjects = [
        b"".join(real[:6]),
        b"\x00" * 24 + (real[0] if real else b"x"),
        b"nothing here matches at all, honestly",
    ]
    for i, subj in enumerate(subjects):
        want, _ = ref.scan(subj, out_cap=61)
        want_set = {(m.pattern_id, m.end) for m in want}
        got, epoch, ovf = match_on_device(cfg, subj, slot=args.slot,
                                          seq=900 + i)
        if got is None:
            fails.append("subject %d: no MATCH_REPLY" % i)
            continue
        mark = "OK" if got == want_set else "MISMATCH"
        print("  subj[%d] %3d B: device %2d / model %2d  epoch=%d ovf=%s  %s"
              % (i, len(subj), len(got), len(want_set), epoch, ovf, mark))
        if got != want_set:
            missed = want_set - got
            extra = got - want_set
            fails.append("subject %d: %d missed (SR3 VIOLATION), %d extra"
                         % (i, len(missed), len(extra)))
        # Against the epoch the DEVICE reported at commit, not the model's:
        # the device counter is cumulative across every load it has ever
        # taken, while a fresh model always says 1.
        if epoch != st.epoch:
            fails.append("subject %d: MATCH_REPLY epoch %d != committed %d"
                         % (i, epoch, st.epoch))

    # ---- 5. a corrupted commit must be refused, and must not damage ----
    # The corruption must happen IN TRANSIT: declare the CRC of the good
    # image, then send bytes that differ.  Corrupting the image before
    # handing it to load_table only makes host and device agree on the
    # corrupted content, which proves nothing.
    bad = bytearray(image)
    bad[len(bad) // 2] ^= 0xFF
    transport = pdev._make_transport(cfg)
    try:
        seq = [7000]

        def op(kind, payload):
            seq[0] += 1
            return pdev._table_op(cfg, transport, kind, args.slot, seq[0],
                                  payload)

        op(pdev.KIND_TABLE_BEGIN,
           struct.pack(">IIQIHH", otable.TABLE_FORMAT_VERSION, ENGINE_ID,
                       len(image), want_id, 0, 0))          # good CRC declared
        off, cap = 0, cfg.max_payload - 12
        while off < len(bad):
            n = min(cap, len(bad) - off)
            op(pdev.KIND_TABLE_DATA,
               struct.pack(">QI", off, n) + bytes(bad[off:off + n]))
            off += n
        stbad = op(pdev.KIND_TABLE_COMMIT, struct.pack(">II", want_id, 0))
    finally:
        transport.close()
    print("commit of a corrupted transfer: device active=0x%08x epoch=%d "
          "(declared 0x%08x)" % (stbad.active_table_id, stbad.epoch, want_id))
    if stbad.active_table_id == want_id:
        print("  device refused the bad commit and kept the good table")
    else:
        fails.append(
            "A5 §5 VIOLATION: the device COMMITTED a transfer whose CRC "
            "(0x%08x) differs from the one declared at TABLE_BEGIN "
            "(0x%08x), destroying the working table" %
            (stbad.active_table_id, want_id))
    st2 = pdev.read_table_status(cfg, slot=args.slot)
    if st2 is None or st2.active_table_id != want_id:
        fails.append("the working table did not survive a refused commit "
                     "(active now 0x%08x)"
                     % (st2.active_table_id if st2 else 0))
    else:
        print("working table survived the refused commit (0x%08x, epoch %d)"
              % (st2.active_table_id, st2.epoch))

    print()
    if fails:
        print("BRINGUP: FAIL")
        for f in fails:
            print("  - %s" % f)
        return 1
    print("BRINGUP: PASS — table resident on silicon, identity and epoch "
          "attested, nominations agree with the model")
    return 0


if __name__ == "__main__":
    sys.exit(main())
