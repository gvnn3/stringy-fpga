/*
 * pyro_rt.h — PYRO host-runtime C ABI (L3), spec python-regex-offload §7.3.
 *
 * ABI 2.0.0 (R37): the per-pattern-circuit ABI of the v2.0.0 architecture.
 * This REPLACES the Phase-0 shape-only 1.0.0 stub (a sanctioned break; R37's
 * version-history note records the 1.0.0 stub as a Phase-0 checkpoint that
 * remains valid on branch phase0-pyro — AC-0-8 — and is superseded here).
 * `pyro_abi_version()` returns 0x00020000.
 *
 * All functions are `extern "C"`; integer widths are fixed; every multi-byte
 * field is little-endian.  On any non-PYRO_OK return, all `out` pointers are
 * left in a defined state (NULL handles, *out_count == 0, status set) — R44.
 * A single `pyro_ctx` is internally synchronized; distinct contexts are
 * independent (R43/R32).
 */
#ifndef PYRO_RT_H
#define PYRO_RT_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* --- R37: ABI version -----------------------------------------------------
 * Packed MAJOR<<16 | MINOR<<8 | PATCH.  This header is ABI 2.0.0 => 0x00020000.
 * A caller MUST refuse a library whose MAJOR differs from the one it expects.
 */
uint32_t pyro_abi_version(void);

/* --- R38: types -----------------------------------------------------------*/
typedef struct pyro_ctx     pyro_ctx;     /* opaque runtime context           */
typedef struct pyro_circuit pyro_circuit; /* opaque generated-circuit handle  */

typedef enum {
    PYRO_OK             = 0,
    PYRO_E_UNSUPPORTED  = 1,  /* pattern not HW-eligible                       */
    PYRO_E_CAPACITY     = 2,  /* circuit exceeds PR-region resource budget     */
    PYRO_E_DEVICE       = 3,  /* transport/device error                        */
    PYRO_E_INVALID      = 4,  /* bad argument                                  */
    PYRO_E_NOMEM        = 5,
    PYRO_E_TIMEOUT      = 6,
    PYRO_E_NOT_RESIDENT = 7,  /* circuit not resident; caller must fall back   */
    PYRO_E_SYNTH        = 8   /* synthesis failed; pattern permanently FB      */
} pyro_status;

typedef enum {
    PYRO_ENC_BYTES = 0,
    PYRO_ENC_UTF8  = 1
} pyro_encoding;

/* circuit lifecycle tier (mirrors R4 / R31) */
typedef enum {
    PYRO_CIRC_COLD         = 0,  /* no artifact; synthesis needed              */
    PYRO_CIRC_SYNTHESIZING = 1,  /* synthesis in flight (service)             */
    PYRO_CIRC_WARM         = 2,  /* artifact cached; PR-load needed            */
    PYRO_CIRC_RESIDENT     = 3,  /* loaded + identity-verified; dispatchable   */
    PYRO_CIRC_FALLBACK     = 4   /* permanently fallback-only (R65)            */
} pyro_circ_status;

/* pyro_match.flags bits (R38/R47) */
#define PYRO_MATCH_VERIFIED   0x1u  /* bit0: circuit's advisory verify bit     */
#define PYRO_MATCH_ZERO_WIDTH 0x2u  /* bit1: zero-width (start == end)         */

/* one reported match window, all offsets in transport units (bytes) */
typedef struct {
    uint64_t start;       /* inclusive, byte offset into transported buffer   */
    uint64_t end;         /* exclusive                                        */
    uint32_t pattern_id;
    uint32_t flags;       /* bit0: verified, bit1: zero_width                 */
} pyro_match;

/* --- R39: lifecycle -------------------------------------------------------*/
pyro_status pyro_ctx_open(pyro_ctx **out, const char *transport_uri);
void        pyro_ctx_close(pyro_ctx *ctx);

/* --- R40: generate / synthesize / load ------------------------------------*/
pyro_status pyro_generate(pyro_ctx *ctx, const uint8_t *pattern, size_t len,
                          uint32_t flags, pyro_encoding enc,
                          pyro_circuit **out);
pyro_status pyro_synth_request(pyro_ctx *ctx, pyro_circuit *c);
pyro_status pyro_circuit_status(pyro_ctx *ctx, pyro_circuit *c,
                                pyro_circ_status *out);
pyro_status pyro_circuit_load(pyro_ctx *ctx, pyro_circuit *c);
void        pyro_circuit_free(pyro_circuit *c);

/* --- R41: scan ------------------------------------------------------------*/
pyro_status pyro_scan(pyro_ctx *ctx, pyro_circuit *c,
                      const uint8_t *buf, size_t len,
                      uint64_t start_off,
                      pyro_match *out, size_t out_cap, size_t *out_count);

/* --- R42: capability query ------------------------------------------------*/
typedef struct {
    uint32_t max_states, max_patterns, max_repeat;
    uint32_t alphabet;           /* 256 for byte datapath                     */
    uint32_t generator_version;  /* L2 HDL generator version                  */
    uint32_t harness_version;    /* §7.4 harness contract version             */
    uint32_t datapath_bytes;     /* bytes/cycle of the harness datapath       */
    uint32_t pr_luts, pr_ffs, pr_bram_kb, pr_dsps;  /* PR-region budget (R11) */
    uint32_t pr_partitions;      /* >=1; 1 => single-tenant region (R64)      */
} pyro_caps;
pyro_status pyro_caps_get(pyro_ctx *ctx, pyro_caps *out);

/* --- test seam (NOT part of the stable ABI contract) ----------------------
 * Force the next scan's internal 64-byte-aligned DMA staging buffer to be
 * deliberately misaligned, so a test can prove the R49 alignment check fires
 * (the model "asserts these to catch host bugs").  Auto-clears after one scan.
 */
void        pyro_ctx_debug_force_misalign(pyro_ctx *ctx, int on);

/* Read a normative R45 CSR register of the ctx's resident circuit by offset
 * (e.g. 0x0000 ID, 0x0018..0x0024 CIRC_ID0..3, 0x0028 CIRC_FLAGS, 0x0014 STATUS,
 * 0x004C OUT_COUNT).  Returns 0 when no circuit is resident.  A test-only seam
 * so the harness-contract tests (R58/AC-1-2) can inspect the identity block and
 * STATUS bits the model maintains; NOT part of the stable ABI contract.
 */
uint32_t    pyro_ctx_debug_csr_read(pyro_ctx *ctx, uint32_t offset);

#ifdef __cplusplus
}  /* extern "C" */
#endif

#endif /* PYRO_RT_H */
