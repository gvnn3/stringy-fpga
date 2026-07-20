/* pyro_dataplane.c — native pipelined MATCH credit loop for the P2d
 * QDMA char-dev transport (v2.7.0, amendment B2 measurement shape).
 *
 * The interpreted credit loop costs ~15-20 us/frame of Python/GIL work —
 * above the whole per-frame hardware budget.  This is the same argument that
 * produced the R3c native router (R3b.3: binds against the compiled build).
 *
 * Multi-queue TX (5 GiB/s target): the per-write syscall + bounce cost
 * serializes ONE queue at ~3 GB/s, under the 5.37 GB/s target.  N writer
 * threads each own a TX queue fd (separate descq locks in the driver);
 * every C2H reply steers to queue 0 (the shell's all-zero RSS indirection
 * table), which the main thread reads.  Credit accounting is a pair of
 * C11 atomics; the GIL is released for the whole run.
 *
 * Wire knowledge duplicated here (R78/R78.12, mirrored from pyro.device):
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
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
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
#define MAX_TXQ 8

static double mono_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

struct dp_shared {
    _Atomic unsigned long long next;     /* next seq to claim (1-based) */
    _Atomic unsigned long long recvd;    /* replies accepted */
    _Atomic int stop;                    /* reader aborted / io error */
    unsigned long long nframes;
    unsigned int window;
    size_t wlen;
};

struct dp_writer {
    struct dp_shared *sh;
    int fd;
    unsigned char *frame;                /* private template */
};

static void *dp_writer_run(void *arg)
{
    struct dp_writer *w = arg;
    struct dp_shared *sh = w->sh;

    for (;;) {
        unsigned long long seq =
            atomic_fetch_add(&sh->next, 1ULL);
        if (seq > sh->nframes)
            return NULL;
        /* credit gate: at most `window` frames in flight */
        while (seq - atomic_load(&sh->recvd) > sh->window) {
            if (atomic_load(&sh->stop))
                return NULL;
            sched_yield();
        }
        if (atomic_load(&sh->stop))
            return NULL;
        w->frame[20] = (unsigned char)((seq >> 24) & 0xff);
        w->frame[21] = (unsigned char)((seq >> 16) & 0xff);
        w->frame[22] = (unsigned char)((seq >> 8) & 0xff);
        w->frame[23] = (unsigned char)(seq & 0xff);
        if (write(w->fd, w->frame, sh->wlen) != (ssize_t)sh->wlen) {
            atomic_store(&sh->stop, 1);
            return NULL;
        }
    }
}

static void build_template(unsigned char *frame, size_t wlen, int slot,
                           Py_ssize_t chunk)
{
    size_t plen = PREFIX_LEN + (size_t)chunk;

    memset(frame, 0, wlen);
    memset(frame, 0xff, 6);                    /* eth dst: broadcast */
    frame[6] = 0x02; frame[11] = 0x01;         /* eth src: 02:...:01 */
    frame[12] = 0x88; frame[13] = 0xb5;        /* ethertype */
    frame[14] = 0x50; frame[15] = 0x01;        /* magic, version */
    frame[16] = KIND_MATCH_REQUEST;
    frame[18] = (unsigned char)((slot >> 8) & 0xff);
    frame[19] = (unsigned char)(slot & 0xff);
    frame[24] = (unsigned char)((plen >> 8) & 0xff);
    frame[25] = (unsigned char)(plen & 0xff);
    frame[HDR_TOTAL + 8] = 0; frame[HDR_TOTAL + 9] = 8;  /* out_cap = 8 */
    memset(frame + HDR_TOTAL + PREFIX_LEN, 0x78, (size_t)chunk);
}

PyObject *
pyro_dataplane_pipeline(PyObject *self, PyObject *args)
{
    PyObject *paths_obj;
    int slot;
    Py_ssize_t chunk;
    unsigned long long total;
    unsigned int window;
    const char *paths[MAX_TXQ];
    Py_ssize_t npaths = 0;

    (void)self;
    if (!PyArg_ParseTuple(args, "OinKI", &paths_obj, &slot, &chunk, &total,
                          &window))
        return NULL;
    if (PyUnicode_Check(paths_obj)) {
        paths[0] = PyUnicode_AsUTF8(paths_obj);
        if (!paths[0])
            return NULL;
        npaths = 1;
    } else if (PyTuple_Check(paths_obj) || PyList_Check(paths_obj)) {
        PyObject *fast = PySequence_Fast(paths_obj, "paths");
        if (!fast)
            return NULL;
        npaths = PySequence_Fast_GET_SIZE(fast);
        if (npaths < 1 || npaths > MAX_TXQ) {
            Py_DECREF(fast);
            return PyErr_Format(PyExc_ValueError,
                                "1..%d queue paths", MAX_TXQ);
        }
        for (Py_ssize_t i = 0; i < npaths; i++) {
            paths[i] = PyUnicode_AsUTF8(
                PySequence_Fast_GET_ITEM(fast, i));
            if (!paths[i]) {
                Py_DECREF(fast);
                return NULL;
            }
        }
        Py_DECREF(fast);   /* borrowed UTF8 stays valid via paths_obj */
    } else {
        return PyErr_Format(PyExc_TypeError, "paths: str or sequence");
    }
    if (chunk <= 0 || chunk > 16384 || window == 0 || total == 0)
        return PyErr_Format(PyExc_ValueError, "bad chunk/total/window");

    unsigned long long nframes = total / (unsigned long long)chunk;
    if (nframes == 0 || nframes > (1ULL << 24))
        return PyErr_Format(PyExc_ValueError, "bad frame count");

    size_t flen = (size_t)HDR_TOTAL + PREFIX_LEN + (size_t)chunk;
    size_t wlen = flen < 60 ? 60 : flen;
    unsigned char *rbuf = malloc(RBUF_SZ);
    unsigned char *got = calloc(nframes + 1, 1);
    unsigned char *templates = malloc(wlen * (size_t)npaths);
    int fds[MAX_TXQ];
    Py_ssize_t nopen = 0;

    if (!rbuf || !got || !templates) {
        free(rbuf); free(got); free(templates);
        return PyErr_NoMemory();
    }
    for (nopen = 0; nopen < npaths; nopen++) {
        fds[nopen] = open(paths[nopen], O_RDWR);
        if (fds[nopen] < 0) {
            PyErr_SetFromErrnoWithFilename(PyExc_OSError, paths[nopen]);
            for (Py_ssize_t i = 0; i < nopen; i++)
                close(fds[i]);
            free(rbuf); free(got); free(templates);
            return NULL;
        }
    }

    struct dp_shared sh;
    struct dp_writer writers[MAX_TXQ];
    pthread_t tids[MAX_TXQ];

    atomic_init(&sh.next, 1ULL);
    atomic_init(&sh.recvd, 0ULL);
    atomic_init(&sh.stop, 0);
    sh.nframes = nframes;
    sh.window = window;
    sh.wlen = wlen;
    for (Py_ssize_t i = 0; i < npaths; i++) {
        writers[i].sh = &sh;
        writers[i].fd = fds[i];
        writers[i].frame = templates + wlen * (size_t)i;
        build_template(writers[i].frame, wlen, slot, chunk);
    }

    unsigned long long recvd = 0;
    unsigned long long n_status = 0, n_dup = 0, n_other = 0;
    size_t roff = 0;
    double t0, t1, t_last = 0.0;
    int thr_err = 0;

    Py_BEGIN_ALLOW_THREADS
    t0 = mono_s();
    for (Py_ssize_t i = 0; i < npaths; i++) {
        if (pthread_create(&tids[i], NULL, dp_writer_run, &writers[i])) {
            thr_err = 1;
            atomic_store(&sh.stop, 1);
            npaths = i;   /* join only the started ones */
            break;
        }
    }
    while (!thr_err && recvd < nframes) {
        ssize_t n = read(fds[0], rbuf + roff,
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
                    atomic_store(&sh.recvd, recvd);
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
        if (atomic_load(&sh.stop))
            break;              /* a writer hit an I/O error */
    }
    atomic_store(&sh.stop, 1);
    for (Py_ssize_t i = 0; i < npaths; i++)
        pthread_join(tids[i], NULL);
    t1 = mono_s();
    Py_END_ALLOW_THREADS

    for (Py_ssize_t i = 0; i < npaths; i++)
        close(fds[i]);
    free(rbuf); free(got); free(templates);

    if (thr_err)
        return PyErr_Format(PyExc_OSError, "pthread_create failed");
    if (t_last == 0.0)
        t_last = t1;
    return Py_BuildValue("(KKddKKK)", recvd, nframes, t1 - t0, t_last - t0,
                         n_status, n_dup, n_other);
}
