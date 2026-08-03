#!/usr/bin/env python3
"""Emit a WIDE differential vector set for tb_overlay_engine_multi.

The first RTL testbench ran one 6-byte subject against four toy patterns.
That was thin enough that a deliberate handshake sabotage went uncaught.
This generates real corpus anchors plus subjects chosen to excite the
cases a short vector cannot: matches at the head and tail of the buffer,
dense overlaps, random binary, and an empty subject.
"""
import os
import random
import sys

REPO = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from pyro.overlay import model as M          # noqa: E402
from pyro.overlay import table as T          # noqa: E402
from pyro.snort import groups as G           # noqa: E402
from pyro.snort import triage as Tr          # noqa: E402

ENGINE_ID = 0x0A5E0001


def main(outdir, group_name="$SSH_PORTS/0"):
    os.makedirs(outdir, exist_ok=True)
    rng = random.Random(20260730)
    corpus = os.path.join(REPO, "third_party", "snort3-community-rules",
                          "snort3-community.rules")
    g = next(x for x in G.pack_groups(Tr.triage_file(corpus))
             if x.name == group_name)
    anchors = [None if s.tombstone else s.anchor for s in g.slots]

    ac = T.build(anchors)
    img = T.serialize(ac, engine_id=ENGINE_ID)
    with open(os.path.join(outdir, "table.hex"), "w") as f:
        f.writelines("%02x\n" % b for b in img)

    subjects = [
        b"".join(a for a in anchors if a),
        b"\x00" * 40 + (anchors[0] or b"x"),
        (anchors[0] or b"x") + b"\x00" * 40,
        b"".join([(anchors[2] or b"x")] * 3),
        bytes(rng.randrange(256) for _ in range(200)),
    ]

    eng = M.OverlayEngineModel(engine_id=ENGINE_ID)
    eng.table_begin(len(img), T.table_id(img), ENGINE_ID,
                    T.TABLE_FORMAT_VERSION, ac.n_states, len(anchors))
    for o in range(0, len(img), 1474):
        eng.table_data(o, img[o:o + 1474])
    eng.table_commit(T.table_id(img))

    meta, sidx = [], []
    flat = bytearray()
    exp_lines = []
    for subj in subjects:
        hits, _ = eng.scan(subj, out_cap=1 << 16)
        exp = sorted({(h.pattern_id, h.end) for h in hits})
        sidx.append((len(flat), len(subj), len(exp)))
        flat += subj
        for pid, end in exp:
            exp_lines.append("%d %d" % (pid, end))
    with open(os.path.join(outdir, "subjects.hex"), "w") as f:
        f.writelines("%02x\n" % b for b in flat)
    with open(os.path.join(outdir, "expected.txt"), "w") as f:
        f.write("\n".join(exp_lines) + "\n")
    with open(os.path.join(outdir, "index.txt"), "w") as f:
        for off, ln, ne in sidx:
            f.write("%d %d %d\n" % (off, ln, ne))
    print("table=%d bytes states=%d patterns=%d" % (len(img), ac.n_states,
                                                    len(anchors)))
    print("subjects=%d total_bytes=%d expected_matches=%d"
          % (len(subjects), len(flat), len(exp_lines)))
    print("crc=0x%08x" % T.table_id(img))
    for i, (off, ln, ne) in enumerate(sidx):
        print("  subj[%d] off=%-5d len=%-4d expect=%d" % (i, off, ln, ne))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
