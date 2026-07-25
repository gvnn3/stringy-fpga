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

/* Retransmit watchdog (P2e): the EQDMA5.0 soft IP err-flags ~1/1500 H2C
 * packets under multi-queue load and the shell drops them silently
 * (qdma_subsystem_h2c.sv), while write() still returns success.  MATCH is
 * seq-idempotent, so when reply progress stalls the watchdog re-sends every
 * missing seq below the send watermark.  Dormant on a loss-free run; a
 * duplicate reply from a late original lands in n_dup.  Worst-case in-flight
 * latency at W=64 is ~350 us, so 25 ms of no progress means loss, not lag. */
#define RETX_POLL_US 100
#define RETX_STALL_MS 10
#define RETX_TAIL_STALL_US 2000
#define RETX_MAX_ROUNDS 400

struct dp_retx {
    struct dp_shared *sh;
    const int *fds;                      /* all TX queues; rotate resends so
                                          * a queue mid-episode (loss comes
                                          * in ~1 s per-queue bursts) doesn't
                                          * eat every retransmit too */
    Py_ssize_t nfds;
    Py_ssize_t rr;
    unsigned char *frame;                /* private template */
    unsigned char *got;                  /* reader-owned; racy read is safe */
    unsigned long long nretx;            /* frames retransmitted */
};

static int dp_retx_send(struct dp_retx *r, unsigned long long seq)
{
    struct dp_shared *sh = r->sh;

    r->frame[20] = (unsigned char)((seq >> 24) & 0xff);
    r->frame[21] = (unsigned char)((seq >> 16) & 0xff);
    r->frame[22] = (unsigned char)((seq >> 8) & 0xff);
    r->frame[23] = (unsigned char)(seq & 0xff);
    r->rr = (r->rr + 1) % r->nfds;
    if (write(r->fds[r->rr], r->frame, sh->wlen) != (ssize_t)sh->wlen) {
        atomic_store(&sh->stop, 1);
        return -1;
    }
    r->nretx++;
    return 0;
}

static void *dp_retx_run(void *arg)
{
    struct dp_retx *r = arg;
    struct dp_shared *sh = r->sh;
    unsigned long long last = 0, oldest = 1, resent_upto = 0;
    unsigned int stalled_us = 0, rounds = 0;

    while (!atomic_load(&sh->stop)) {
        unsigned long long cur = atomic_load(&sh->recvd);
        if (cur >= sh->nframes)
            return NULL;
        if (cur != last) {
            last = cur;
            stalled_us = 0;
        } else {
            stalled_us += RETX_POLL_US;
        }
        while (oldest <= sh->nframes && r->got[oldest])
            oldest++;

        /* Positional detection: a missing seq more than two windows behind
         * the send watermark can no longer be legitimately in flight —
         * resend just the holes (once each; the stall fallback below
         * catches a lost retransmit).  Detection latency ~2W frame times,
         * so the amortized penalty at the observed ~1/1500 loss rate is
         * sub-microsecond per frame, vs ~17 us/frame for a pure 25 ms
         * stall watchdog. */
        unsigned long long mark = atomic_load(&sh->next) - 1;
        if (mark > sh->nframes)
            mark = sh->nframes;
        /* Slack: 2 windows of legitimate flight + MAX_TXQ seqs that a
         * writer may have claimed but not yet written (the credit gate
         * sits between claim and write). */
        unsigned long long slack = 2ULL * sh->window + MAX_TXQ;
        unsigned long long horizon = (mark > slack) ? mark - slack : 0;
        if (mark >= sh->nframes && stalled_us >= RETX_TAIL_STALL_US)
            horizon = sh->nframes;      /* tail: nothing left in flight */
        if (horizon > resent_upto) {
            for (unsigned long long seq =
                     (oldest > resent_upto + 1 ? oldest : resent_upto + 1);
                 seq <= horizon; seq++) {
                if (r->got[seq])
                    continue;
                if (atomic_load(&sh->stop) || dp_retx_send(r, seq))
                    return NULL;
            }
            resent_upto = horizon;
        }

        /* Full-stall fallback: no reply progress at all — blanket-resend
         * every hole (covers lost retransmits and burst loss). */
        if (stalled_us >= RETX_STALL_MS * 1000u) {
            stalled_us = 0;
            if (++rounds > RETX_MAX_ROUNDS) {   /* dead card: give up */
                atomic_store(&sh->stop, 1);
                return NULL;
            }
            for (unsigned long long seq = oldest; seq <= mark; seq++) {
                if (r->got[seq])
                    continue;
                if (atomic_load(&sh->stop) || dp_retx_send(r, seq))
                    return NULL;
            }
        }
        usleep(RETX_POLL_US);
    }
    return NULL;
}

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
    unsigned char *templates = malloc(wlen * ((size_t)npaths + 1));
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

    struct dp_retx retx;
    pthread_t retx_tid;
    int retx_started = 0;

    retx.sh = &sh;
    retx.fds = fds;        /* own H2C writes; reader only reads fds[0] */
    retx.nfds = npaths;
    retx.rr = 0;
    retx.frame = templates + wlen * (size_t)npaths;
    retx.got = got;
    retx.nretx = 0;
    build_template(retx.frame, wlen, slot, chunk);

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
    if (!thr_err)
        retx_started = !pthread_create(&retx_tid, NULL, dp_retx_run, &retx);
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
    if (retx_started)
        pthread_join(retx_tid, NULL);
    t1 = mono_s();
    Py_END_ALLOW_THREADS

    for (Py_ssize_t i = 0; i < npaths; i++)
        close(fds[i]);
    free(rbuf); free(got); free(templates);

    if (thr_err)
        return PyErr_Format(PyExc_OSError, "pthread_create failed");
    if (t_last == 0.0)
        t_last = t1;
    return Py_BuildValue("(KKddKKKK)", recvd, nframes, t1 - t0, t_last - t0,
                         n_status, n_dup, n_other, retx.nretx);
}
