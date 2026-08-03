#!/usr/bin/env python3
"""Reproduce every quantitative claim in docs/spec-amendments-a5-overlay.md.

A design document whose arithmetic cannot be re-run is a document that
quietly goes stale.  Each figure below is derived from a measured input:
the 2.3 GiB/s char-dev rate (P2d), SF14's trie sizing, SF2's URAM budget,
R78.9a's jumbo payload, and the 0.3 frontier ratio
(docs/studies/switch-cost-frontier.md).
"""
GiB = 1024 ** 3
TABLE_16B = 1.33e6      # SF14: 16-byte-capped trie, all 3,896 anchors
TABLE_8B = 0.68e6       # SF14: 8-byte cap
URAM = 2.25e6           # SF2
RATE = 2.3 * GiB        # P2d, measured single-queue
JUMBO_PAYLOAD = 9568    # R78.9a
STD_PAYLOAD = 1474
R_STAR = 0.3            # frontier ratio


def main() -> int:
    print("write time @ %.2f GiB/s:" % (RATE / GiB))
    for name, sz in (("16 B cap", TABLE_16B), ("8 B cap", TABLE_8B)):
        print("  %-10s %.2f MB -> %.3f ms" % (name, sz / 1e6, sz / RATE * 1e3))

    print("\ndouble-buffer vs SF2 URAM (%.2f MB):" % (URAM / 1e6))
    for name, sz in (("16 B cap x2", 2 * TABLE_16B),
                     ("8 B cap x2", 2 * TABLE_8B)):
        print("  %-12s %.2f MB  %s"
              % (name, sz / 1e6, "FITS" if sz <= URAM else "does NOT fit"))

    swit = TABLE_16B / RATE
    print("\nsingle-region + quiesce: switch %.2f ms -> usable for phases "
          "above %.1f ms" % (swit * 1e3, swit / R_STAR * 1e3))

    print("\nR78-only transfer of a full 16 B-cap table:")
    for name, pay in (("jumbo (this shell)", JUMBO_PAYLOAD),
                      ("standard 1518 B", STD_PAYLOAD)):
        print("  %-20s %d frames" % (name, -(-int(TABLE_16B) // pay)))

    print(
    "\nbandwidth for a FULL rewrite, by phase length (need <= %.0f%% of P):" %
     (R_STAR * 100))
    for P in (1.0, 0.1, 0.01, 0.001):
        need = TABLE_16B / (R_STAR * P)
        print("  P=%-8s %9.1f MB/s  %s" % ("%gs" % P, need / 1e6,
              "OK" if need <= RATE else "EXCEEDS single-queue"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
