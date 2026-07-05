/*
 * pyro_rt.h — PYRO host-runtime C ABI (L3), spec python-regex-offload §7.3.
 *
 * FROZEN for ABI 1.0.0 (R37).  This header defines exactly the types, enums,
 * and function signatures of §7.3 (R37-R44).  All functions are `extern "C"`;
 * integer widths are fixed; the ABI is versioned.  Phase 0 ships only the
 * `pyro_abi_version()` implementation (src/pyro_rt_stub.c); the remaining
 * signatures are declared so bindings and tests can compile against a stable
 * surface while the runtime is implemented in later phases.
 */
#ifndef PYRO_RT_H
#define PYRO_RT_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* --- R37: ABI version -----------------------------------------------------
 * Returns a packed MAJOR<<16 | MINOR<<8 | PATCH.  This header is ABI 1.0.0,
 * i.e. 0x00010000.  A caller MUST refuse a library whose MAJOR differs.
 */
uint32_t pyro_abi_version(void);

/* --- R38: types -----------------------------------------------------------*/
typedef struct pyro_ctx  pyro_ctx;    /* opaque runtime context             */
typedef struct pyro_prog pyro_prog;   /* opaque compiled automaton program  */

typedef enum {
    PYRO_OK            = 0,
    PYRO_E_UNSUPPORTED = 1,  /* pattern not HW-eligible */
    PYRO_E_CAPACITY    = 2,  /* exceeds device limits    */
    PYRO_E_DEVICE      = 3,  /* transport/device error   */
    PYRO_E_INVALID     = 4,  /* bad argument             */
    PYRO_E_NOMEM       = 5,
    PYRO_E_TIMEOUT     = 6
} pyro_status;

typedef enum {
    PYRO_ENC_BYTES = 0,
    PYRO_ENC_UTF8  = 1
} pyro_encoding;

/* one reported match window, all offsets in transport units (bytes) */
typedef struct {
    uint64_t start;   /* inclusive, byte offset into transported buffer */
    uint64_t end;     /* exclusive */
    uint32_t pattern_id;
    uint32_t flags;   /* bit0: verified, bit1: zero_width */
} pyro_match;

/* --- R39: lifecycle -------------------------------------------------------*/
pyro_status pyro_ctx_open(pyro_ctx **out, const char *transport_uri);
void        pyro_ctx_close(pyro_ctx *ctx);

/* --- R40: compile/load ----------------------------------------------------*/
pyro_status pyro_compile(pyro_ctx *ctx, const uint8_t *pattern, size_t len,
                         uint32_t flags, pyro_encoding enc, pyro_prog **out);
void        pyro_prog_free(pyro_prog *p);
pyro_status pyro_prog_load(pyro_ctx *ctx, pyro_prog *p);   /* resident on dev */

/* --- R41: scan ------------------------------------------------------------*/
pyro_status pyro_scan(pyro_ctx *ctx, pyro_prog *p,
                      const uint8_t *buf, size_t len,
                      uint64_t start_off,
                      pyro_match *out, size_t out_cap, size_t *out_count);

/* --- R42: capability query ------------------------------------------------*/
typedef struct {
    uint32_t max_states, max_patterns, max_repeat;
    uint32_t alphabet;      /* 256 for byte engine */
    uint32_t engine_version;
    uint32_t engine_kind;   /* 1=aho-corasick, 2=nfa, 0=model */
} pyro_caps;
pyro_status pyro_caps_get(pyro_ctx *ctx, pyro_caps *out);

#ifdef __cplusplus
}  /* extern "C" */
#endif

#endif /* PYRO_RT_H */
