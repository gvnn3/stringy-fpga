/* pyro_dataplane.c — native pipelined MATCH credit loop for the P2d
 * QDMA char-dev transport (v2.7.0, amendment B2 measurement shape).
 *
 * The interpreted credit loop costs ~15-20 us/frame of Python/GIL work —
 * above the whole per-frame hardware budget.  This is the same argument that
 * produced the R3c native router (R3b.3: binds against the compiled build):
 * the loop below is a single thread alternating async writes (the cdev posts
 * the descriptor and returns) with blocking packet-boundary reads, GIL
 * released for the duration.
 *
 * Wire knowledge duplicated here (R78, mirrored from pyro.device):
 *   frame = eth(14: dst ff.., src 02:...:01, ethertype 0x88B5)
 *         + pyro hdr(14: magic 0x50, ver 1, kind, flags, slot u16, seq u32,
 *                    length u16, resv u16 -- all BE)
 *         + MATCH prefix(12: start u64, out_cap u16, flags u16) + corpus.
 * Replies are re-framed from the byte stream by the length field, skipping
 * inter-frame zero padding (same discipline as _CharDevTransport).
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define ETH_HLEN_P 14
#define PYRO_HLEN 14
#define PREFIX_LEN 12
#define HDR_TOTAL (ETH_HLEN_P + PYRO_HLEN)
#define KIND_MATCH_REQUEST 0x03
#define KIND_MATCH_REPLY 0x04
#define RBUF_SZ 65536
#define READ_CHUNK 12288

static double mono_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

PyObject *
pyro_dataplane_pipeline(PyObject *self, PyObject *args)
{
    const char *path;
    int slot;
    Py_ssize_t chunk;
    unsigned long long total;
    unsigned int window;

    (void)self;
    if (!PyArg_ParseTuple(args, "sinKI", &path, &slot, &chunk, &total,
                          &window))
        return NULL;
    if (chunk <= 0 || chunk > 16384 || window == 0 || total == 0)
        return PyErr_Format(PyExc_ValueError, "bad chunk/total/window");

    unsigned long long nframes = total / (unsigned long long)chunk;
    if (nframes == 0 || nframes > (1ULL << 24))
        return PyErr_Format(PyExc_ValueError, "bad frame count");

    size_t flen = (size_t)HDR_TOTAL + PREFIX_LEN + (size_t)chunk;
    unsigned char *frame = malloc(flen < 60 ? 60 : flen);
    unsigned char *rbuf = malloc(RBUF_SZ);
    unsigned char *got = calloc(nframes + 1, 1);
    if (!frame || !rbuf || !got) {
        free(frame); free(rbuf); free(got);
        return PyErr_NoMemory();
    }
    size_t wlen = flen < 60 ? 60 : flen;
    memset(frame, 0, wlen);
    /* eth */
    memset(frame, 0xff, 6);
    frame[6] = 0x02; frame[11] = 0x01;
    frame[12] = 0x88; frame[13] = 0xb5;
    /* pyro hdr */
    frame[14] = 0x50; frame[15] = 0x01; frame[16] = KIND_MATCH_REQUEST;
    frame[18] = (unsigned char)((slot >> 8) & 0xff);
    frame[19] = (unsigned char)(slot & 0xff);
    size_t plen = PREFIX_LEN + (size_t)chunk;
    frame[24] = (unsigned char)((plen >> 8) & 0xff);
    frame[25] = (unsigned char)(plen & 0xff);
    /* MATCH prefix: start=0, out_cap=8, flags=0 */
    frame[HDR_TOTAL + 8] = 0; frame[HDR_TOTAL + 9] = 8;
    memset(frame + HDR_TOTAL + PREFIX_LEN, 0x78, (size_t)chunk);

    int fd = open(path, O_RDWR);
    if (fd < 0) {
        free(frame); free(rbuf); free(got);
        return PyErr_SetFromErrnoWithFilename(PyExc_OSError, path);
    }

    unsigned long long sent = 0, recvd = 0, inflight = 0;
    unsigned long long n_status = 0, n_dup = 0, n_other = 0;
    int io_error = 0;
    size_t roff = 0;
    double t0, t1, t_last = 0.0;

    Py_BEGIN_ALLOW_THREADS
    t0 = mono_s();
    while (recvd < nframes) {
        while (sent < nframes && inflight < window) {
            unsigned long long seq = sent + 1;
            frame[20] = (unsigned char)((seq >> 24) & 0xff);
            frame[21] = (unsigned char)((seq >> 16) & 0xff);
            frame[22] = (unsigned char)((seq >> 8) & 0xff);
            frame[23] = (unsigned char)(seq & 0xff);
            if (write(fd, frame, wlen) != (ssize_t)wlen) {
                io_error = 1;
                break;
            }
            sent++; inflight++;
        }
        if (io_error)
            break;
        ssize_t n = read(fd, rbuf + roff,
                         (size_t)READ_CHUNK < RBUF_SZ - roff
                             ? (size_t)READ_CHUNK : RBUF_SZ - roff);
        if (n <= 0)
            break;              /* driver timeout (~10 s) or error: abort */
        roff += (size_t)n;
        size_t off = 0;
        while (roff - off >= HDR_TOTAL) {
            if (rbuf[off] == 0) { off++; continue; }
            if (rbuf[off + 12] != 0x88 || rbuf[off + 13] != 0xb5 ||
                rbuf[off + 14] != 0x50 || rbuf[off + 15] != 0x01) {
                off++;          /* resync scan */
                continue;
            }
            size_t rplen = ((size_t)rbuf[off + 24] << 8) | rbuf[off + 25];
            size_t tot = HDR_TOTAL + rplen;
            if (roff - off < tot)
                break;
            if (rbuf[off + 16] == KIND_MATCH_REPLY) {
                unsigned long long seq =
                    ((unsigned long long)rbuf[off + 20] << 24) |
                    ((unsigned long long)rbuf[off + 21] << 16) |
                    ((unsigned long long)rbuf[off + 22] << 8) |
                    (unsigned long long)rbuf[off + 23];
                if (seq >= 1 && seq <= nframes && !got[seq]) {
                    got[seq] = 1;
                    recvd++;
                    t_last = mono_s();
                    if (inflight)
                        inflight--;
                } else {
                    n_dup++;
                }
            } else if (rbuf[off + 16] == 0x05) {
                n_status++;
            } else {
                n_other++;
            }
            off += tot;
        }
        if (off) {
            memmove(rbuf, rbuf + off, roff - off);
            roff -= off;
        }
    }
    t1 = mono_s();
    Py_END_ALLOW_THREADS

    close(fd);
    free(frame); free(rbuf); free(got);

    if (t_last == 0.0)
        t_last = t1;
    return Py_BuildValue("(KKddKKK)", recvd, nframes, t1 - t0, t_last - t0,
                         n_status, n_dup, n_other);
}
