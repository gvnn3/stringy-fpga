"""The single source of truth for the R2/R3 routing thresholds.

This module exists so that exactly ONE literal for each threshold lives in the
tree.  :mod:`pyro._route` re-exports these names (tests read ``_route.S_MIN``),
and ``scripts/gen_route_config.py`` reads this module at build time to emit
``build/include/pyro_thresholds.h`` for the native routing extension.  The C
never carries its own copy of the numbers; a divergence is caught by the
``(_fast.S_MIN, _fast.N_REUSE) == (_route.S_MIN, _route.N_REUSE)`` round-trip
test (which also catches a stale in-tree ``.so``).

Keep this module import-free and literal-only: the generator execs it in a bare
namespace.
"""

S_MIN = 64 * 1024   # 64 KiB (R2 / R51 step 4 minimum corpus size)
N_REUSE = 32        # R2 / R51 step 4 per-pattern reuse threshold
