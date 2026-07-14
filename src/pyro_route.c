/* pyro_route.c — non-inline definitions for the native R51 decision core.
 *
 * The decision logic lives in include/pyro_route.h as a `static inline` so the
 * CPython extension (src/pyro_ext.c) inlines it into its own hot path — a
 * direct C call, no PLT crossing, no ctypes (R3c.1).  This TU exists so the
 * core is also reachable as an ordinary function from plain-C consumers and so
 * the thresholds baked into the build can be read back out.
 */
#include "pyro_route.h"

int pyro_route_decide(const pyro_route_in *in)
{
    return pyro_route_decide_inline(in);
}

uint64_t pyro_route_s_min(void)   { return (uint64_t)PYRO_S_MIN; }
uint64_t pyro_route_n_reuse(void) { return (uint64_t)PYRO_N_REUSE; }
