/*
 * abi_conformance.c — standalone C driver of the PYRO ABI 2.0.0 model runtime.
 *
 * Exercises the full async lifecycle (open -> generate -> synth_request ->
 * status -> load -> scan -> free -> close) plus the key R44 guards, entirely in
 * C so it can run under valgrind (leak-/error-clean, R43/R44) without CPython.
 * It builds a tiny automaton artifact + manifest on disk in the exact on-wire
 * format (pyro/synth/artifact.py + pyro/synth/manifest.py) so the runtime's
 * R47a/R47b verification runs against a real, correct artifact.
 *
 * Exit code 0 == all checks passed; non-zero == a check failed (line reported).
 */
#define _DEFAULT_SOURCE
#define _POSIX_C_SOURCE 200809L

#include "pyro_rt.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define HARNESS_VERSION 0x00020000u
#define GENERATOR_VERSION 0x00020000u
#define SHELL_VERSION 0x0A000001u

static int g_fail = 0;
#define CHECK(cond) do { if (!(cond)) { \
    fprintf(stderr, "CHECK failed at %s:%d: %s\n", __FILE__, __LINE__, #cond); \
    g_fail = 1; } } while (0)

/* zlib/binascii-compatible CRC-32 (must match the runtime + Python). */
static uint32_t crc32_of(const uint8_t *buf, size_t len)
{
    static uint32_t table[256];
    static int init = 0;
    if (!init) {
        for (uint32_t n = 0; n < 256; n++) {
            uint32_t c = n;
            for (int k = 0; k < 8; k++)
                c = (c & 1u) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
            table[n] = c;
        }
        init = 1;
    }
    uint32_t c = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; i++)
        c = table[(c ^ buf[i]) & 0xFFu] ^ (c >> 8);
    return c ^ 0xFFFFFFFFu;
}

static void put32(uint8_t *p, uint32_t v)
{
    p[0] = v & 0xFF; p[1] = (v >> 8) & 0xFF;
    p[2] = (v >> 16) & 0xFF; p[3] = (v >> 24) & 0xFF;
}

/* Build the artifact for a 2-state automaton matching literal byte 'A'
 * (state 0 --'A'--> state 1 accept).  Returns malloc'd payload; *len set. */
static uint8_t *build_artifact(const uint8_t hash[16], uint32_t circ_flags,
                               size_t *len)
{
    size_t body = 1 * 40;                 /* one byte edge, 40 bytes */
    size_t total = 76 + body + 4;         /* header + body + crc trailer */
    uint8_t *p = calloc(1, total);
    memcpy(p, "PYROART1", 8);
    put32(p + 8, 1);                      /* format_version */
    memcpy(p + 12, hash, 16);
    put32(p + 28, circ_flags);
    put32(p + 32, 0);                     /* encoding = BYTES */
    put32(p + 36, 0);                     /* effective_flags */
    put32(p + 40, GENERATOR_VERSION);
    put32(p + 44, HARNESS_VERSION);
    put32(p + 48, SHELL_VERSION);
    put32(p + 52, 2);                     /* n_states */
    put32(p + 56, 0);                     /* start */
    put32(p + 60, 1);                     /* accept */
    put32(p + 64, 1);                     /* n_byte_edges */
    put32(p + 68, 0);                     /* n_eps */
    put32(p + 72, 0);                     /* n_assert */
    /* byte edge: src=0 dst=1 bitmap{'A'} */
    uint8_t *e = p + 76;
    put32(e, 0);
    put32(e + 4, 1);
    e[8 + ('A' >> 3)] |= (1u << ('A' & 7));
    put32(p + total - 4, crc32_of(p, total - 4));
    *len = total;
    return p;
}

static void hex16(const uint8_t h[16], char out[33])
{
    static const char *d = "0123456789abcdef";
    for (int i = 0; i < 16; i++) { out[i*2] = d[h[i]>>4]; out[i*2+1] = d[h[i]&0xF]; }
    out[32] = 0;
}

static int write_file(const char *dir, const char *name, const void *buf, size_t n)
{
    char path[4096];
    snprintf(path, sizeof path, "%s/%s", dir, name);
    FILE *f = fopen(path, "wb");
    if (!f) return -1;
    size_t w = fwrite(buf, 1, n, f);
    fclose(f);
    return (w == n) ? 0 : -1;
}

static void rm_file(const char *dir, const char *name)
{
    char path[4096];
    snprintf(path, sizeof path, "%s/%s", dir, name);
    unlink(path);
}

/* Build a descriptor blob for pyro_generate. */
static uint8_t *make_desc(const uint8_t hash[16], uint32_t circ_flags,
                          uint32_t enc, const char *dir, size_t *len)
{
    size_t plen = strlen(dir);
    size_t total = 36 + plen;
    uint8_t *d = calloc(1, total);
    memcpy(d, "PYRODSC1", 8);
    memcpy(d + 8, hash, 16);
    put32(d + 24, circ_flags);
    put32(d + 28, enc);
    put32(d + 32, (uint32_t)plen);
    memcpy(d + 36, dir, plen);
    *len = total;
    return d;
}

int main(void)
{
    CHECK(pyro_abi_version() == 0x00020000u);

    char tmpl[] = "/tmp/pyro_abi_XXXXXX";
    char *dir = mkdtemp(tmpl);
    CHECK(dir != NULL);
    if (!dir) return 1;

    uint8_t hash[16];
    for (int i = 0; i < 16; i++) hash[i] = (uint8_t)(i + 1);
    uint32_t circ_flags = 0;

    /* Write a valid artifact + manifest. */
    size_t alen = 0;
    uint8_t *art = build_artifact(hash, circ_flags, &alen);
    char hexh[33];
    hex16(hash, hexh);
    char manifest[512];
    int mlen = snprintf(manifest, sizeof manifest,
        "{\"integrity_hash\":\"%08x\",\"pattern_hash\":\"%s\",\"payload_len\":%zu}",
        crc32_of(art, alen), hexh, alen);
    CHECK(write_file(dir, "artifact.bin", art, alen) == 0);
    CHECK(write_file(dir, "manifest.json", manifest, (size_t)mlen) == 0);

    /* --- lifecycle happy path --- */
    pyro_ctx *ctx = NULL;
    CHECK(pyro_ctx_open(&ctx, "model://") == PYRO_OK && ctx != NULL);

    pyro_caps caps;
    CHECK(pyro_caps_get(ctx, &caps) == PYRO_OK);
    CHECK(caps.max_states >= 1024 && caps.max_patterns >= 256 && caps.max_repeat >= 255);
    CHECK(caps.harness_version == HARNESS_VERSION && caps.pr_partitions == 1);

    size_t dlen = 0;
    uint8_t *desc = make_desc(hash, circ_flags, 0, dir, &dlen);
    pyro_circuit *c = NULL;
    CHECK(pyro_generate(ctx, desc, dlen, 0, PYRO_ENC_BYTES, &c) == PYRO_OK && c != NULL);

    /* scan before load must refuse (R41). */
    pyro_match mout[8];
    size_t n = 123;
    CHECK(pyro_scan(ctx, c, (const uint8_t *)"AAB", 3, 0, mout, 8, &n) == PYRO_E_NOT_RESIDENT);
    CHECK(n == 0);

    pyro_circ_status st = PYRO_CIRC_FALLBACK;
    CHECK(pyro_synth_request(ctx, c) == PYRO_OK);
    CHECK(pyro_circuit_status(ctx, c, &st) == PYRO_OK && st == PYRO_CIRC_WARM);

    CHECK(pyro_circuit_load(ctx, c) == PYRO_OK);
    CHECK(pyro_circuit_status(ctx, c, &st) == PYRO_OK && st == PYRO_CIRC_RESIDENT);
    CHECK(pyro_ctx_debug_csr_read(ctx, 0x0000) == 0x5059524Fu);   /* ID magic */
    CHECK(pyro_ctx_debug_csr_read(ctx, 0x0018) == 0x04030201u);   /* CIRC_ID0 */

    /* scan "AAB" -> windows [0,1) and [1,2). */
    CHECK(pyro_scan(ctx, c, (const uint8_t *)"AAB", 3, 0, mout, 8, &n) == PYRO_OK);
    CHECK(n == 2);
    CHECK(mout[0].start == 0 && mout[0].end == 1);
    CHECK(mout[1].start == 1 && mout[1].end == 2);

    /* overflow/resume (out_cap=1). */
    CHECK(pyro_scan(ctx, c, (const uint8_t *)"AAB", 3, 0, mout, 1, &n) == PYRO_OK);
    CHECK(n == 1 && mout[0].start == 0);
    CHECK(pyro_scan(ctx, c, (const uint8_t *)"AAB", 3, mout[0].start + 1, mout, 8, &n) == PYRO_OK);
    CHECK(n == 1 && mout[0].start == 1);

    /* R49 alignment assertion fires as a defined error. */
    pyro_ctx_debug_force_misalign(ctx, 1);
    CHECK(pyro_scan(ctx, c, (const uint8_t *)"AAB", 3, 0, mout, 8, &n) == PYRO_E_INVALID);
    CHECK(n == 0);

    /* --- identity mismatch (R47a): wrong demanded hash -> load refused. --- */
    uint8_t bad_hash[16];
    memset(bad_hash, 0xEE, 16);
    size_t bdlen = 0;
    uint8_t *bdesc = make_desc(bad_hash, circ_flags, 0, dir, &bdlen);
    pyro_circuit *bc = NULL;
    CHECK(pyro_generate(ctx, bdesc, bdlen, 0, PYRO_ENC_BYTES, &bc) == PYRO_OK);
    CHECK(pyro_circuit_load(ctx, bc) == PYRO_E_NOT_RESIDENT);
    pyro_circuit_free(bc);
    free(bdesc);

    /* --- synthesis failure (R65): FAILED.json -> E_SYNTH, tier FALLBACK. --- */
    rm_file(dir, "artifact.bin");
    rm_file(dir, "manifest.json");
    const char *fail = "{\"reason\":\"mock\"}";
    CHECK(write_file(dir, "FAILED.json", fail, strlen(fail)) == 0);
    size_t fdlen = 0;
    uint8_t *fdesc = make_desc(hash, circ_flags, 0, dir, &fdlen);
    pyro_circuit *fc = NULL;
    CHECK(pyro_generate(ctx, fdesc, fdlen, 0, PYRO_ENC_BYTES, &fc) == PYRO_OK);
    CHECK(pyro_circuit_status(ctx, fc, &st) == PYRO_OK && st == PYRO_CIRC_FALLBACK);
    CHECK(pyro_circuit_load(ctx, fc) == PYRO_E_SYNTH);
    pyro_circuit_free(fc);
    free(fdesc);

    /* --- device bindings parse but report no device on this host (F5). --- */
    pyro_ctx *dctx = (pyro_ctx *)0x1;
    CHECK(pyro_ctx_open(&dctx, "qdma://af:00.0/q0") == PYRO_E_DEVICE && dctx == NULL);
    CHECK(pyro_ctx_open(&dctx, "eth://enp175s0f0") == PYRO_E_DEVICE && dctx == NULL);
    CHECK(pyro_ctx_open(&dctx, "bogus://x") == PYRO_E_INVALID && dctx == NULL);

    pyro_circuit_free(c);
    pyro_ctx_close(ctx);
    free(desc);
    free(art);

    /* cleanup temp files/dir */
    rm_file(dir, "artifact.bin");
    rm_file(dir, "manifest.json");
    rm_file(dir, "FAILED.json");
    rmdir(dir);

    if (g_fail) { fprintf(stderr, "FAIL\n"); return 1; }
    printf("abi_conformance: OK\n");
    return 0;
}
