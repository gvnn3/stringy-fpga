/*
 * pyro_rt.c — PYRO host-runtime C ABI 2.0.0 (L3), spec python-regex-offload
 * §7.3 (R37-R44) + §7.4 harness contract (R45-R50), model transport binding.
 *
 * This is a real native shared library (C11) that implements ABI 2.0.0 against
 * an *in-library software model* of the §7.4 harness (the `model://` transport).
 * It is the C twin of pyro/_circuit_model.py: it executes the SAME generated
 * automaton — shipped to it in the PR-bitstream artifact (pyro/synth/artifact.py)
 * — so a generator lowering bug surfaces identically in both models.  A C library
 * performs NO regex classification (that is Python L2, R8); it coordinates with
 * PYRO only through the on-disk artifact file format.
 *
 * ---------------------------------------------------------------------------
 * model:// binding conventions (documented Task-7 artifact-format extension)
 * ---------------------------------------------------------------------------
 * Because the fixed ABI `pyro_generate(ctx, pattern, len, flags, enc, out)` hands
 * a C library only raw pattern bytes — which it cannot classify or hash the way
 * the Python L2/identity layer does — the `pattern`/`len` arguments in the
 * model:// binding carry a small self-describing *circuit descriptor* the Python
 * glue (pyro/_native.py) builds from a GeneratedCircuit + its cache entry:
 *
 *   off  size  field
 *   0    8     magic "PYRODSC1"
 *   8    16    expected pattern_hash (R47a identity the host will demand)
 *   24   4     expected CIRC_FLAGS   (packed, R45 0x0028)
 *   28   4     encoding tag          (0=BYTES, 1=UTF8; informational)
 *   32   4     artifact-dir path length
 *   36   ...   artifact-dir path (bytes; contains artifact.bin + manifest.json)
 *
 * pyro_generate records the expected identity + artifact directory (tier COLD).
 * pyro_synth_request / pyro_circuit_status POLL that directory (the "job-state
 * file / cache" of the spec): FAILED.json => FALLBACK (R65); artifact.bin +
 * manifest.json present => WARM; else after a request => SYNTHESIZING; else COLD.
 * pyro_circuit_load reads the artifact, performs the R47b integrity + shell/
 * harness compatibility checks and the R47a identity trust-boundary check, then
 * (on success) parses the automaton into the model and becomes RESIDENT.
 *
 * All multi-byte fields are little-endian.  On any non-PYRO_OK return every `out`
 * pointer is left in a defined state (NULL handles, *out_count==0, status set) —
 * R44.  A single pyro_ctx is internally synchronized (pthread mutex); distinct
 * contexts are independent (R43/R32).  PYRO_E_NOT_RESIDENT / PYRO_E_SYNTH are
 * normal fallback routing, not device errors (R44/R51/R65).
 */
#define _POSIX_C_SOURCE 200809L

#include "pyro_rt.h"

#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* The 24-byte result-ring entry layout is normative (R47): keep the ABI struct
 * bit-for-bit identical to the on-wire ring entry. */
_Static_assert(sizeof(pyro_match) == 24, "pyro_match must be a 24-byte R47 ring entry");

/* --- normative harness / version constants (R42/R45) ----------------------*/
#define PYRO_ABI_VER          0x00020000u  /* ABI 2.0.0 (R37)                 */
#define HARNESS_VERSION       0x00020300u  /* §7.4 harness contract 2.3.0 (closure_passes) */
#define GENERATOR_VERSION     0x00020300u  /* L2 HDL generator 2.3.0 (closure_passes) */
#define SHELL_VERSION         0x0A000001u  /* target shell/PR-region id (R47b)*/
#define ID_MAGIC              0x5059524Fu  /* "PYRO" (R45 0x0000)             */
#define DATAPATH_BYTES        1u
#define PR_PARTITIONS         1u           /* single-tenant region (R64)      */

/* Advertised complexity + PR-region budget (R11/R13; mirrors hdl.estimator). */
#define CAP_MAX_STATES        1024u
#define CAP_MAX_PATTERNS      256u
#define CAP_MAX_REPEAT        255u
#define CAP_ALPHABET          256u
#define CAP_PR_LUTS           216000u
#define CAP_PR_FFS            432000u
#define CAP_PR_BRAM_KB        4320u
#define CAP_PR_DSPS           768u

/* STATUS register bits (R45 0x0014). */
#define ST_BUSY 0x1u
#define ST_DONE 0x2u
#define ST_ERR  0x4u
#define ST_OVF  0x8u

/* Artifact binary format (pyro/synth/artifact.py). */
#define ART_MAGIC   "PYROART1"
#define ART_FMT_VER 1u
/* Header field offsets. */
#define AH_FMT       8
#define AH_HASH      12
#define AH_CIRCFLAGS 28
#define AH_ENC       32
#define AH_EFF       36
#define AH_GEN       40
#define AH_HARN      44
#define AH_SHELL     48
#define AH_NSTATES   52
#define AH_START     56
#define AH_ACCEPT    60
#define AH_NBYTE     64
#define AH_NEPS      68
#define AH_NASSERT   72
#define AH_BODY      76
#define ART_MIN_LEN  (AH_BODY + 4)     /* header + at least the 4-byte trailer */
#define BYTE_EDGE_SZ 40                /* src(4) dst(4) bitmap(32) */
#define EPS_EDGE_SZ  8                 /* src(4) dst(4) */
#define ASSERT_EDGE_SZ 12              /* src(4) dst(4) code(4) */

/* Circuit-descriptor format (see file header). */
#define DESC_MAGIC "PYRODSC1"
#define DESC_MIN_LEN 36

/* --------------------------------------------------------------------------
 * Little-endian readers + CRC-32 (zlib/binascii.crc32-compatible)
 * ------------------------------------------------------------------------ */
static uint32_t rd32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static uint32_t g_crc_table[256];
static pthread_once_t g_crc_once = PTHREAD_ONCE_INIT;

static void crc_init(void)
{
    for (uint32_t n = 0; n < 256; n++) {
        uint32_t c = n;
        for (int k = 0; k < 8; k++)
            c = (c & 1u) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
        g_crc_table[n] = c;
    }
}

static uint32_t crc32_of(const uint8_t *buf, size_t len)
{
    pthread_once(&g_crc_once, crc_init);
    uint32_t c = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; i++)
        c = g_crc_table[(c ^ buf[i]) & 0xFFu] ^ (c >> 8);
    return c ^ 0xFFFFFFFFu;
}

/* --------------------------------------------------------------------------
 * State-set bitset helpers
 * ------------------------------------------------------------------------ */
static inline void bs_set(uint64_t *bs, uint32_t i)  { bs[i >> 6] |= (uint64_t)1 << (i & 63); }
static inline int  bs_test(const uint64_t *bs, uint32_t i) { return (bs[i >> 6] >> (i & 63)) & 1u; }

/* --------------------------------------------------------------------------
 * Parsed automaton model (executes the generated recognizer, R19)
 * ------------------------------------------------------------------------ */
typedef struct {
    uint32_t n_states, start, accept;
    uint32_t encoding;               /* 0 BYTES, 1 UTF8 */
    /* CSR identity block (R45/R47a), populated from the artifact header. */
    uint32_t circ_id[4];
    uint32_t circ_flags;
    uint32_t harness_ver;
    uint32_t generator_ver;
    /* CSR adjacency (grouped-by-source), R9 lowering. */
    uint32_t n_byte, n_eps, n_assert;
    uint32_t *b_off;                 /* n_states+1 */
    uint32_t *b_dst;                 /* n_byte */
    uint8_t  *b_bitmap;              /* n_byte * 32, reordered to match b_dst */
    uint32_t *e_off;                 /* n_states+1 */
    uint32_t *e_dst;                 /* n_eps */
    uint32_t *a_off;                 /* n_states+1 */
    uint32_t *a_dst;                 /* n_assert */
    uint32_t *a_code;                /* n_assert */
    /* Per-scan scratch (guarded by the ctx lock — single-issue, R48). */
    size_t    words;                 /* bitset words = (n_states+63)/64 */
    uint64_t *cur;
    uint64_t *nxt;
    uint32_t *stack;                 /* worklist, size n_states */
    int       built;
} model_t;

struct pyro_ctx {
    int kind;                        /* 1 = model:// */
    pthread_mutex_t lock;
    pyro_circuit *resident;          /* single-tenant PR region (R64) */
    int force_misalign;              /* R49 test seam */
};

struct pyro_circuit {
    uint8_t  expected_hash[16];      /* R47a identity the host demands */
    uint32_t expected_circ_flags;
    uint32_t enc;                    /* descriptor encoding tag */
    uint32_t flags;                  /* ABI flags argument */
    char    *art_dir;                /* artifact directory (owns) */
    int      synth_requested;
    int      loaded;                 /* model valid & resident */
    pyro_circ_status tier;
    model_t  model;
    /* CSR live state (R45) written by scan. */
    uint32_t status;
    uint32_t out_count;
    pyro_ctx *ctx;                   /* owning ctx (R43: ctx outlives circuit) */
};

/* --------------------------------------------------------------------------
 * Zero-width assertion evaluator (mirrors artifact.py AC_* / _assert_ok)
 * ------------------------------------------------------------------------ */
static inline int is_word_byte(uint8_t b)
{
    return (b >= '0' && b <= '9') || (b >= 'A' && b <= 'Z') ||
           (b >= 'a' && b <= 'z') || b == '_';
}

static int assert_ok(uint32_t code, const uint8_t *buf, size_t pos, size_t n)
{
    switch (code) {
    case 0: return pos == 0;                                    /* AC_SOB: ^/\A */
    case 1: return pos == 0 || buf[pos - 1] == 0x0A;            /* AC_BOL */
    case 2: return pos == n;                                    /* AC_EOS: \Z  */
    case 3: return pos == n || (pos + 1 == n && buf[pos] == 0x0A); /* AC_EOB: $ */
    case 4: return pos == n || buf[pos] == 0x0A;               /* AC_EOL */
    case 5: {                                                   /* AC_WB: \b   */
        int before = pos > 0 && is_word_byte(buf[pos - 1]);
        int after  = pos < n && is_word_byte(buf[pos]);
        return before != after;
    }
    case 6: {                                                   /* AC_NWB: \B  */
        int before = pos > 0 && is_word_byte(buf[pos - 1]);
        int after  = pos < n && is_word_byte(buf[pos]);
        return before == after;
    }
    default: return 1;                                          /* AC_TRUE     */
    }
}

/* epsilon + assertion closure at absolute offset `pos` (mirrors _closure). */
static void closure(model_t *m, uint64_t *set, const uint8_t *buf,
                    size_t pos, size_t n)
{
    uint32_t top = 0;
    for (uint32_t s = 0; s < m->n_states; s++)
        if (bs_test(set, s))
            m->stack[top++] = s;
    while (top) {
        uint32_t s = m->stack[--top];
        for (uint32_t k = m->e_off[s]; k < m->e_off[s + 1]; k++) {
            uint32_t d = m->e_dst[k];
            if (!bs_test(set, d)) { bs_set(set, d); m->stack[top++] = d; }
        }
        for (uint32_t k = m->a_off[s]; k < m->a_off[s + 1]; k++) {
            uint32_t d = m->a_dst[k];
            if (!bs_test(set, d) && assert_ok(m->a_code[k], buf, pos, n)) {
                bs_set(set, d);
                m->stack[top++] = d;
            }
        }
    }
}

static int set_empty(const uint64_t *set, size_t words)
{
    for (size_t i = 0; i < words; i++)
        if (set[i]) return 0;
    return 1;
}

/* Execute the automaton over buf[start_off..n): emit candidate group-0 windows
 * (mirrors pyro._circuit_model._scan_windows exactly).  Writes up to out_cap
 * entries; sets *overflow when a further window exists beyond out_cap (R41/R47).
 */
static void scan_run(model_t *m, const uint8_t *buf, size_t n, uint64_t start_off,
                     pyro_match *out, size_t out_cap, size_t *out_count,
                     int *overflow)
{
    size_t words = m->words;
    uint64_t *cur = m->cur;
    size_t count = 0;
    *overflow = 0;

    for (uint64_t s = start_off; s <= (uint64_t)n; s++) {
        memset(cur, 0, words * sizeof(uint64_t));
        bs_set(cur, m->start);
        closure(m, cur, buf, (size_t)s, n);

        int have_accept = bs_test(cur, m->accept);
        uint64_t acc_end = s;
        size_t pos = (size_t)s;

        while (pos < n && !set_empty(cur, words)) {
            uint8_t b = buf[pos];
            memset(m->nxt, 0, words * sizeof(uint64_t));
            int moved_any = 0;
            for (uint32_t st = 0; st < m->n_states; st++) {
                if (!bs_test(cur, st)) continue;
                for (uint32_t k = m->b_off[st]; k < m->b_off[st + 1]; k++) {
                    const uint8_t *bm = m->b_bitmap + (size_t)k * 32;
                    if (bm[b >> 3] & (1u << (b & 7))) {
                        bs_set(m->nxt, m->b_dst[k]);
                        moved_any = 1;
                    }
                }
            }
            if (!moved_any) break;
            memcpy(cur, m->nxt, words * sizeof(uint64_t));
            closure(m, cur, buf, pos + 1, n);
            pos++;
            if (bs_test(cur, m->accept)) { have_accept = 1; acc_end = (uint64_t)pos; }
        }

        if (have_accept) {
            if (count < out_cap) {
                out[count].start = s;
                out[count].end = acc_end;
                out[count].pattern_id = 0;
                out[count].flags = (acc_end == s) ? PYRO_MATCH_ZERO_WIDTH : 0u;
                count++;
            } else {
                *overflow = 1;   /* a window beyond capacity exists (R47) */
                break;
            }
        }
    }
    *out_count = count;
}

/* --------------------------------------------------------------------------
 * Artifact parsing / verification
 * ------------------------------------------------------------------------ */
static void model_free(model_t *m)
{
    free(m->b_off);  free(m->b_dst);  free(m->b_bitmap);
    free(m->e_off);  free(m->e_dst);
    free(m->a_off);  free(m->a_dst);  free(m->a_code);
    free(m->cur);    free(m->nxt);    free(m->stack);
    memset(m, 0, sizeof(*m));
}

/* Build a grouped-by-source offset/target table from flat edge records.
 * `stride` is the record size; `code_out` (may be NULL) collects a trailing u32.
 * Returns 0 on success, -1 on OOM / out-of-range state. */
static int build_adj(uint32_t n_states, uint32_t n_edges, const uint8_t *recs,
                     size_t stride, uint32_t **off_out, uint32_t **dst_out,
                     uint8_t **bitmap_out /*40B recs only*/,
                     uint32_t **code_out /*12B recs only*/)
{
    uint32_t *off = calloc((size_t)n_states + 1, sizeof(uint32_t));
    uint32_t *dst = calloc(n_edges ? n_edges : 1, sizeof(uint32_t));
    if (!off || !dst) { free(off); free(dst); return -1; }
    uint8_t *bm = NULL;
    uint32_t *code = NULL;
    if (bitmap_out) {
        bm = calloc((size_t)(n_edges ? n_edges : 1) * 32, 1);
        if (!bm) { free(off); free(dst); return -1; }
    }
    if (code_out) {
        code = calloc(n_edges ? n_edges : 1, sizeof(uint32_t));
        if (!code) { free(off); free(dst); free(bm); return -1; }
    }
    /* counting sort by source state */
    for (uint32_t i = 0; i < n_edges; i++) {
        uint32_t src = rd32(recs + (size_t)i * stride);
        uint32_t d   = rd32(recs + (size_t)i * stride + 4);
        if (src >= n_states || d >= n_states) {
            free(off); free(dst); free(bm); free(code); return -1;
        }
        off[src + 1]++;
    }
    for (uint32_t s = 0; s < n_states; s++)
        off[s + 1] += off[s];
    uint32_t *cursor = malloc((size_t)n_states * sizeof(uint32_t));
    if (n_states && !cursor) { free(off); free(dst); free(bm); free(code); return -1; }
    for (uint32_t s = 0; s < n_states; s++) cursor[s] = off[s];
    for (uint32_t i = 0; i < n_edges; i++) {
        const uint8_t *rec = recs + (size_t)i * stride;
        uint32_t src = rd32(rec);
        uint32_t p = cursor[src]++;
        dst[p] = rd32(rec + 4);
        if (bm)   memcpy(bm + (size_t)p * 32, rec + 8, 32);
        if (code) code[p] = rd32(rec + 8);
    }
    free(cursor);
    *off_out = off;
    *dst_out = dst;
    if (bitmap_out) *bitmap_out = bm;
    if (code_out)   *code_out = code;
    return 0;
}

/* Verify + parse an artifact payload into `m`.  Returns PYRO_OK, or
 * PYRO_E_INVALID for a structurally corrupt / integrity-failed artifact. */
static pyro_status artifact_build(const uint8_t *buf, size_t len, model_t *m)
{
    memset(m, 0, sizeof(*m));
    if (len < ART_MIN_LEN) return PYRO_E_INVALID;
    if (memcmp(buf, ART_MAGIC, 8) != 0) return PYRO_E_INVALID;
    if (rd32(buf + AH_FMT) != ART_FMT_VER) return PYRO_E_INVALID;

    /* internal integrity trailer: crc32 over everything preceding it (R47b). */
    uint32_t stored = rd32(buf + len - 4);
    if (crc32_of(buf, len - 4) != stored) return PYRO_E_INVALID;

    uint32_t n_states = rd32(buf + AH_NSTATES);
    uint32_t start    = rd32(buf + AH_START);
    uint32_t accept   = rd32(buf + AH_ACCEPT);
    uint32_t n_byte   = rd32(buf + AH_NBYTE);
    uint32_t n_eps    = rd32(buf + AH_NEPS);
    uint32_t n_assert = rd32(buf + AH_NASSERT);
    if (n_states == 0 || start >= n_states || accept >= n_states)
        return PYRO_E_INVALID;

    /* bounds-check the body length against the declared edge counts. */
    size_t body = (size_t)n_byte * BYTE_EDGE_SZ + (size_t)n_eps * EPS_EDGE_SZ +
                  (size_t)n_assert * ASSERT_EDGE_SZ;
    if (AH_BODY + body + 4 != len) return PYRO_E_INVALID;

    const uint8_t *bp = buf + AH_BODY;
    const uint8_t *ep = bp + (size_t)n_byte * BYTE_EDGE_SZ;
    const uint8_t *ap = ep + (size_t)n_eps * EPS_EDGE_SZ;

    m->n_states = n_states; m->start = start; m->accept = accept;
    m->n_byte = n_byte; m->n_eps = n_eps; m->n_assert = n_assert;
    m->encoding    = rd32(buf + AH_ENC);
    m->circ_flags  = rd32(buf + AH_CIRCFLAGS);
    m->harness_ver = rd32(buf + AH_HARN);
    m->generator_ver = rd32(buf + AH_GEN);
    for (int i = 0; i < 4; i++)
        m->circ_id[i] = rd32(buf + AH_HASH + i * 4);

    if (build_adj(n_states, n_byte, bp, BYTE_EDGE_SZ,
                  &m->b_off, &m->b_dst, &m->b_bitmap, NULL) != 0)
        goto oom;
    if (build_adj(n_states, n_eps, ep, EPS_EDGE_SZ,
                  &m->e_off, &m->e_dst, NULL, NULL) != 0)
        goto oom;
    if (build_adj(n_states, n_assert, ap, ASSERT_EDGE_SZ,
                  &m->a_off, &m->a_dst, NULL, &m->a_code) != 0)
        goto oom;

    m->words = ((size_t)n_states + 63) / 64;
    m->cur   = malloc(m->words * sizeof(uint64_t));
    m->nxt   = malloc(m->words * sizeof(uint64_t));
    m->stack = malloc((size_t)n_states * sizeof(uint32_t));
    if (!m->cur || !m->nxt || !m->stack) goto oom;
    m->built = 1;
    return PYRO_OK;

oom:
    model_free(m);
    return PYRO_E_NOMEM;
}

/* --------------------------------------------------------------------------
 * Small filesystem + JSON helpers (manifest cross-checks, R47b)
 * ------------------------------------------------------------------------ */
static int path_join(char *dst, size_t cap, const char *dir, const char *name)
{
    int r = snprintf(dst, cap, "%s/%s", dir, name);
    return (r > 0 && (size_t)r < cap) ? 0 : -1;
}

static int file_exists(const char *dir, const char *name)
{
    char p[4096];
    if (path_join(p, sizeof p, dir, name) != 0) return 0;
    return access(p, F_OK) == 0;
}

/* Read a whole file, NUL-terminating the returned buffer (len excludes NUL). */
static uint8_t *read_file(const char *dir, const char *name, size_t *out_len)
{
    char p[4096];
    if (path_join(p, sizeof p, dir, name) != 0) return NULL;
    FILE *f = fopen(p, "rb");
    if (!f) return NULL;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return NULL; }
    long n = ftell(f);
    if (n < 0) { fclose(f); return NULL; }
    if (fseek(f, 0, SEEK_SET) != 0) { fclose(f); return NULL; }
    uint8_t *b = malloc((size_t)n + 1);
    if (!b) { fclose(f); return NULL; }
    size_t got = fread(b, 1, (size_t)n, f);
    fclose(f);
    if (got != (size_t)n) { free(b); return NULL; }
    b[n] = 0;
    *out_len = (size_t)n;
    return b;
}

/* Extract a string value for `key` from our own compact manifest JSON
 * (json.dumps sort_keys, separators=(",",":")): `"key":"value"`. */
static int json_get_str(const char *json, const char *key, char *out, size_t cap)
{
    char tok[128];
    if (snprintf(tok, sizeof tok, "\"%s\":\"", key) >= (int)sizeof tok) return -1;
    const char *p = strstr(json, tok);
    if (!p) return -1;
    p += strlen(tok);
    size_t i = 0;
    while (*p && *p != '"') {
        if (i + 1 >= cap) return -1;
        out[i++] = *p++;
    }
    if (*p != '"') return -1;
    out[i] = 0;
    return 0;
}

/* Extract a non-negative integer value for `key`: `"key":123`. */
static int json_get_u64(const char *json, const char *key, uint64_t *out)
{
    char tok[128];
    if (snprintf(tok, sizeof tok, "\"%s\":", key) >= (int)sizeof tok) return -1;
    const char *p = strstr(json, tok);
    if (!p) return -1;
    p += strlen(tok);
    if (*p < '0' || *p > '9') return -1;
    uint64_t v = 0;
    while (*p >= '0' && *p <= '9') v = v * 10 + (uint64_t)(*p++ - '0');
    *out = v;
    return 0;
}

/* --------------------------------------------------------------------------
 * Lifecycle tier polling (R4/R51 tier bookkeeping over the artifact directory)
 * ------------------------------------------------------------------------ */
static pyro_circ_status poll_tier(const pyro_circuit *c)
{
    if (c->loaded) return PYRO_CIRC_RESIDENT;
    if (file_exists(c->art_dir, "FAILED.json")) return PYRO_CIRC_FALLBACK;
    if (file_exists(c->art_dir, "artifact.bin") &&
        file_exists(c->art_dir, "manifest.json"))
        return PYRO_CIRC_WARM;
    if (c->synth_requested) return PYRO_CIRC_SYNTHESIZING;
    return PYRO_CIRC_COLD;
}

/* --------------------------------------------------------------------------
 * Public ABI
 * ------------------------------------------------------------------------ */
uint32_t pyro_abi_version(void)
{
    return PYRO_ABI_VER;
}

pyro_status pyro_ctx_open(pyro_ctx **out, const char *transport_uri)
{
    if (out) *out = NULL;
    if (!out || !transport_uri) return PYRO_E_INVALID;

    if (strncmp(transport_uri, "model://", 8) == 0) {
        pyro_ctx *ctx = calloc(1, sizeof(*ctx));
        if (!ctx) return PYRO_E_NOMEM;
        if (pthread_mutex_init(&ctx->lock, NULL) != 0) { free(ctx); return PYRO_E_NOMEM; }
        ctx->kind = 1;
        *out = ctx;
        return PYRO_OK;
    }
    /* The QDMA char-dev and raw-Ethernet bindings are named in the spec's
     * binding table but require driver/char-dev enablement absent on this host
     * (F5).  Their URIs parse; opening them reports a device error (no device). */
    if (strncmp(transport_uri, "qdma://", 7) == 0 ||
        strncmp(transport_uri, "eth://", 6) == 0)
        return PYRO_E_DEVICE;

    return PYRO_E_INVALID;
}

void pyro_ctx_close(pyro_ctx *ctx)
{
    if (!ctx) return;
    /* Circuits are caller-owned (R43); we only drop our resident reference. */
    pthread_mutex_destroy(&ctx->lock);
    free(ctx);
}

pyro_status pyro_generate(pyro_ctx *ctx, const uint8_t *pattern, size_t len,
                          uint32_t flags, pyro_encoding enc, pyro_circuit **out)
{
    if (out) *out = NULL;
    if (!ctx || !pattern || !out) return PYRO_E_INVALID;

    /* model:// binding: `pattern` is a circuit descriptor (see file header). */
    if (len < DESC_MIN_LEN || memcmp(pattern, DESC_MAGIC, 8) != 0)
        return PYRO_E_INVALID;
    uint32_t path_len = rd32(pattern + 32);
    if ((size_t)DESC_MIN_LEN + path_len != len) return PYRO_E_INVALID;

    pyro_circuit *c = calloc(1, sizeof(*c));
    if (!c) return PYRO_E_NOMEM;
    memcpy(c->expected_hash, pattern + 8, 16);
    c->expected_circ_flags = rd32(pattern + 24);
    c->enc = rd32(pattern + 28);
    c->flags = flags;
    (void)enc;  /* encoding is carried by the descriptor + artifact header */
    c->art_dir = malloc((size_t)path_len + 1);
    if (!c->art_dir) { free(c); return PYRO_E_NOMEM; }
    memcpy(c->art_dir, pattern + 36, path_len);
    c->art_dir[path_len] = 0;
    c->tier = PYRO_CIRC_COLD;
    c->ctx = ctx;
    *out = c;
    return PYRO_OK;
}

pyro_status pyro_synth_request(pyro_ctx *ctx, pyro_circuit *c)
{
    if (!ctx || !c) return PYRO_E_INVALID;
    pthread_mutex_lock(&ctx->lock);
    c->synth_requested = 1;             /* idempotent; non-blocking (R40) */
    c->tier = poll_tier(c);
    pthread_mutex_unlock(&ctx->lock);
    return PYRO_OK;
}

pyro_status pyro_circuit_status(pyro_ctx *ctx, pyro_circuit *c,
                                pyro_circ_status *out)
{
    if (out) *out = PYRO_CIRC_FALLBACK;   /* R44 defined-state default */
    if (!ctx || !c || !out) return PYRO_E_INVALID;
    pthread_mutex_lock(&ctx->lock);
    c->tier = poll_tier(c);
    *out = c->tier;
    pthread_mutex_unlock(&ctx->lock);
    return PYRO_OK;
}

pyro_status pyro_circuit_load(pyro_ctx *ctx, pyro_circuit *c)
{
    if (!ctx || !c) return PYRO_E_INVALID;

    pthread_mutex_lock(&ctx->lock);

    /* R65: a recorded synthesis failure is permanent fallback (PYRO_E_SYNTH). */
    if (file_exists(c->art_dir, "FAILED.json")) {
        c->tier = PYRO_CIRC_FALLBACK;
        pthread_mutex_unlock(&ctx->lock);
        return PYRO_E_SYNTH;
    }

    size_t art_len = 0, man_len = 0;
    uint8_t *art = read_file(c->art_dir, "artifact.bin", &art_len);
    if (!art) {                          /* not warm yet (cold/synthesizing) */
        c->tier = poll_tier(c);
        pthread_mutex_unlock(&ctx->lock);
        return PYRO_E_NOT_RESIDENT;
    }
    uint8_t *man = read_file(c->art_dir, "manifest.json", &man_len);
    if (!man) {
        free(art);
        c->tier = poll_tier(c);
        pthread_mutex_unlock(&ctx->lock);
        return PYRO_E_NOT_RESIDENT;
    }

    /* All of the following R47b/R47a refusals "treat the pattern as fallback,
     * not a device error" (R47b): report PYRO_E_NOT_RESIDENT and stay unloaded. */
    pyro_status rc = PYRO_E_NOT_RESIDENT;
    model_t m;
    int have_model = 0;

    /* --- R47b manifest cross-checks (mismatch/corrupt manifest rejection) --- */
    char hexbuf[64];
    uint64_t man_len_field = 0, man_crc = 0;
    /* payload_len must equal the actual artifact length */
    if (json_get_u64((const char *)man, "payload_len", &man_len_field) != 0 ||
        man_len_field != art_len)
        goto done;
    /* integrity_hash (crc32 hex of the whole artifact.bin) must match */
    if (json_get_str((const char *)man, "integrity_hash", hexbuf, sizeof hexbuf) != 0)
        goto done;
    man_crc = strtoull(hexbuf, NULL, 16);
    if ((uint32_t)man_crc != crc32_of(art, art_len))
        goto done;

    /* --- artifact structural integrity + parse (R47b internal trailer) --- */
    if (artifact_build(art, art_len, &m) != PYRO_OK)
        goto done;
    have_model = 1;

    /* manifest pattern_hash must match the artifact's baked identity (R47a) */
    if (json_get_str((const char *)man, "pattern_hash", hexbuf, sizeof hexbuf) == 0) {
        uint8_t man_hash[16];
        int ok = (strlen(hexbuf) == 32);
        for (int i = 0; ok && i < 16; i++) {
            char t[3] = { hexbuf[i * 2], hexbuf[i * 2 + 1], 0 };
            man_hash[i] = (uint8_t)strtoul(t, NULL, 16);
        }
        uint8_t art_hash[16];
        for (int i = 0; i < 4; i++)
            for (int j = 0; j < 4; j++)
                art_hash[i * 4 + j] = (uint8_t)(m.circ_id[i] >> (j * 8));
        if (!ok || memcmp(man_hash, art_hash, 16) != 0)
            goto done;
    } else {
        goto done;
    }

    /* --- R47b shell/PR-region + harness compatibility --- */
    if (m.harness_ver != HARNESS_VERSION)
        goto done;
    if (rd32(art + AH_SHELL) != SHELL_VERSION)
        goto done;

    /* --- R47a identity trust boundary: baked identity == demanded identity -- */
    {
        uint8_t art_hash[16];
        for (int i = 0; i < 4; i++)
            for (int j = 0; j < 4; j++)
                art_hash[i * 4 + j] = (uint8_t)(m.circ_id[i] >> (j * 8));
        if (memcmp(art_hash, c->expected_hash, 16) != 0)
            goto done;
        if (m.circ_flags != c->expected_circ_flags)
            goto done;
    }

    /* --- success: evict any prior resident (single-tenant, R64), go resident */
    if (ctx->resident && ctx->resident != c) {
        pyro_circuit *old = ctx->resident;
        old->loaded = 0;
        model_free(&old->model);
        old->tier = PYRO_CIRC_WARM;
    }
    if (c->loaded) model_free(&c->model);   /* reload of same circuit */
    c->model = m;
    have_model = 0;                          /* ownership moved into c */
    c->loaded = 1;
    c->tier = PYRO_CIRC_RESIDENT;
    c->status = 0;
    c->out_count = 0;
    ctx->resident = c;
    rc = PYRO_OK;

done:
    if (have_model) model_free(&m);
    free(art);
    free(man);
    pthread_mutex_unlock(&ctx->lock);
    return rc;
}

void pyro_circuit_free(pyro_circuit *c)
{
    if (!c) return;
    if (c->ctx) {
        pthread_mutex_lock(&c->ctx->lock);
        if (c->ctx->resident == c) c->ctx->resident = NULL;
        pthread_mutex_unlock(&c->ctx->lock);
    }
    if (c->model.built) model_free(&c->model);
    free(c->art_dir);
    free(c);
}

pyro_status pyro_scan(pyro_ctx *ctx, pyro_circuit *c,
                      const uint8_t *buf, size_t len, uint64_t start_off,
                      pyro_match *out, size_t out_cap, size_t *out_count)
{
    if (out_count) *out_count = 0;
    if (!ctx || !c || !out_count) return PYRO_E_INVALID;
    if (out_cap && !out) return PYRO_E_INVALID;
    if (len && !buf) return PYRO_E_INVALID;

    pthread_mutex_lock(&ctx->lock);   /* single-issue against the PR region (R48) */

    if (!c->loaded || ctx->resident != c) {
        pthread_mutex_unlock(&ctx->lock);
        return PYRO_E_NOT_RESIDENT;    /* R41 guard; caller must fall back */
    }

    /* R47a: re-read the identity block and confirm it matches before dispatch. */
    uint8_t baked[16];
    for (int i = 0; i < 4; i++)
        for (int j = 0; j < 4; j++)
            baked[i * 4 + j] = (uint8_t)(c->model.circ_id[i] >> (j * 8));
    if (memcmp(baked, c->expected_hash, 16) != 0 ||
        c->model.circ_flags != c->expected_circ_flags) {
        pthread_mutex_unlock(&ctx->lock);
        return PYRO_E_NOT_RESIDENT;
    }

    c->status = ST_BUSY;

    /* R49: DMA buffers are 64-byte aligned.  Model DMA-in by copying the borrowed
     * input into a 64-byte-aligned staging buffer and assert the alignment (the
     * model "asserts these to catch host bugs").  The alignment enforcement is a
     * defined error return (not an abort) so the conformance test can observe it. */
    size_t stage_sz = (len + 63) & ~(size_t)63;
    if (stage_sz == 0) stage_sz = 64;
    uint8_t *staging = aligned_alloc(64, stage_sz);
    /* Result-ring DMA staging is likewise a 64-byte-aligned buffer (R47/R49). */
    uint8_t *ring_probe = aligned_alloc(64, 64);
    if (!staging || !ring_probe) {
        free(staging); free(ring_probe);
        c->status = ST_DONE | ST_ERR;
        pthread_mutex_unlock(&ctx->lock);
        return PYRO_E_NOMEM;
    }
    if (len) memcpy(staging, buf, len);

    const uint8_t *dma_in = staging;
    if (ctx->force_misalign) {
        ctx->force_misalign = 0;         /* auto-clears after one scan */
        dma_in = staging + 1;            /* deliberately break the R49 contract */
    }
    if (((uintptr_t)dma_in & 63u) != 0 || ((uintptr_t)ring_probe & 63u) != 0) {
        free(staging); free(ring_probe);
        c->status = ST_DONE | ST_ERR;
        pthread_mutex_unlock(&ctx->lock);
        return PYRO_E_INVALID;           /* R49 alignment assertion fired */
    }

    int overflow = 0;
    size_t count = 0;
    scan_run(&c->model, dma_in, len, start_off, out, out_cap, &count, &overflow);

    free(staging);
    free(ring_probe);

    c->out_count = (uint32_t)count;
    c->status = ST_DONE | (overflow ? ST_OVF : 0u);
    *out_count = count;
    pthread_mutex_unlock(&ctx->lock);
    return PYRO_OK;
}

pyro_status pyro_caps_get(pyro_ctx *ctx, pyro_caps *out)
{
    if (!ctx || !out) return PYRO_E_INVALID;
    out->max_states = CAP_MAX_STATES;
    out->max_patterns = CAP_MAX_PATTERNS;
    out->max_repeat = CAP_MAX_REPEAT;
    out->alphabet = CAP_ALPHABET;
    out->generator_version = GENERATOR_VERSION;
    out->harness_version = HARNESS_VERSION;
    out->datapath_bytes = DATAPATH_BYTES;
    out->pr_luts = CAP_PR_LUTS;
    out->pr_ffs = CAP_PR_FFS;
    out->pr_bram_kb = CAP_PR_BRAM_KB;
    out->pr_dsps = CAP_PR_DSPS;
    out->pr_partitions = PR_PARTITIONS;
    return PYRO_OK;
}

/* --- test-only seams (NOT part of the stable ABI) -------------------------
 * Compiled and exported only under PYRO_TESTING so a production build of the
 * frozen ABI does not expose these mutators (see include/pyro_rt.h + Makefile).
 */
#ifdef PYRO_TESTING
void pyro_ctx_debug_force_misalign(pyro_ctx *ctx, int on)
{
    if (!ctx) return;
    pthread_mutex_lock(&ctx->lock);
    ctx->force_misalign = on ? 1 : 0;
    pthread_mutex_unlock(&ctx->lock);
}

uint32_t pyro_ctx_debug_csr_read(pyro_ctx *ctx, uint32_t offset)
{
    if (!ctx) return 0;
    uint32_t v = 0;
    pthread_mutex_lock(&ctx->lock);
    pyro_circuit *c = ctx->resident;
    if (c && c->loaded) {
        const model_t *m = &c->model;
        switch (offset) {
        case 0x0000: v = ID_MAGIC; break;
        case 0x0004: v = m->harness_ver; break;
        case 0x0008: v = (DATAPATH_BYTES & 0xFFFFu) | ((PR_PARTITIONS & 0xFFFFu) << 16); break;
        case 0x000C: v = m->generator_ver; break;
        case 0x0014: v = c->status; break;
        case 0x0018: v = m->circ_id[0]; break;
        case 0x001C: v = m->circ_id[1]; break;
        case 0x0020: v = m->circ_id[2]; break;
        case 0x0024: v = m->circ_id[3]; break;
        case 0x0028: v = m->circ_flags; break;
        case 0x004C: v = c->out_count; break;
        default: v = 0; break;
        }
    }
    pthread_mutex_unlock(&ctx->lock);
    return v;
}
#endif /* PYRO_TESTING */
