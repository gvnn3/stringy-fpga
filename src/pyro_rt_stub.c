/*
 * pyro_rt_stub.c — Phase 0 stub of the PYRO host-runtime C ABI.
 *
 * Implements ONLY pyro_abi_version() (R37).  The rest of the §7.3 surface is
 * declared in pyro_rt.h and implemented in Phase 1.  This keeps the ABI shape
 * frozen and compilable now (AC-0-8) without pulling in any device logic.
 */
#include "pyro_rt.h"

/* ABI 1.0.0 -> 0x00010000 (MAJOR<<16 | MINOR<<8 | PATCH), R37. */
#define PYRO_ABI_MAJOR 1u
#define PYRO_ABI_MINOR 0u
#define PYRO_ABI_PATCH 0u

uint32_t pyro_abi_version(void)
{
    return (PYRO_ABI_MAJOR << 16) | (PYRO_ABI_MINOR << 8) | PYRO_ABI_PATCH;
}
