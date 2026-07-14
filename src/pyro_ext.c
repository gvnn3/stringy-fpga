/* pyro_ext.c — `pyro._fast`: the native routing hot path (R3b/R3c).
 *
 * =====================================================================
 *  WHY THIS FILE EXISTS — the four costs that were removed.  Do not
 *  re-introduce ANY of them on the hot path.  (Ablation on the reference
 *  host; the four sum to the 917.6 ns of the pure-Python router, and the
 *  ENTIRE R3b budget is 26-39 ns.)
 *
 *      threading.Lock in _record()      435 ns   -> per-thread relaxed
 *                                                   counters (no RMW, no lock)
 *      5 Python frames on the path      208 ns   -> one C method, zero frames
 *      Python decision arithmetic       175 ns   -> pyro_route_decide_inline
 *      getattr(patt._stock, op)          99 ns   -> bound methods cached in m[]
 *
 *  The R51 DECISION itself costs 3.5-9 ns in C.  The decision was never the
 *  cost.  A single stray `atomic_fetch_add` (6.6-12.7 ns), an uncached
 *  getattr, or one extra Python frame is enough to fail AC-3-3: the shipped
 *  ratio is ~1.10x against a 1.15x bound.
 * =====================================================================
 *
 * Structure:
 *   - `pyro._fast.Pattern`: a C extension type mirroring _match.PyPattern's
 *     __slots__, so every existing Python reader (_route, HybridMatch, the
 *     tests) keeps working unchanged.
 *   - search/match/fullmatch/finditer/findall run the S0..S6 decision natively
 *     and, on a fallback verdict, vectorcall a CACHED BOUND stock method.
 *   - A "model" verdict, an exotic argument shape, and sub/subn/split all hand
 *     off to the SAME Python functions the pure-Python router uses, so there is
 *     exactly one implementation of the model/cold path.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <structmember.h>

#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "pyro_route.h"

#define PYRO_ROUTE_ABI 1

/* ---------------------------------------------------------------------
 * Cached env snapshot (R35a/R35d).
 *
 * One atomic word, pushed by _route.sample_env().  The hot path NEVER touches
 * os.environ (that costs ~0.85 us per get) and never reads a Python global.
 * A single relaxed atomic load cannot split a call across an old and a new
 * snapshot -- the same argument as the one-tuple rebind at _route.py:171.
 * ------------------------------------------------------------------- */
#define ENV_DISABLED 0x1u
#define ENV_FORCE    0x2u
static _Atomic uint32_t g_env = 0;

/* ---------------------------------------------------------------------
 * Per-thread dispatch counters (R66.1).
 *
 * Index order is the exact insertion order of _route._STATS, which is the
 * order pyro.re.stats() must present (R52/R66 shape is frozen).
 *
 * Each counter has exactly ONE writer: its owning thread.  `bump()` is
 * therefore a relaxed load/add/store (mov/add/mov on x86-64), NOT a
 * lock-prefixed xadd.  The relaxed atomics exist only so the aggregator's
 * cross-thread reads are not UB; they cost nothing.
 *
 * The mutex is taken on thread birth, thread death, stats() and reset_stats()
 * -- never on the measured path.
 * ------------------------------------------------------------------- */
enum { CNT_HW = 0, CNT_MODEL, CNT_FALLBACK, CNT_FB_ERR, CNT_DEVERR, CNT_TOTAL,
       CNT_N };

static const char *const CNT_KEYS[CNT_N] = {
    "hardware", "model", "fallback", "fallback_after_error",
    "device_errors", "total",
};

typedef struct blk {
    _Atomic uint64_t c[CNT_N];
    struct blk *next;
} blk;

/* initial-exec TLS model: a single %fs-relative load, NOT a __tls_get_addr
 * function call (which the default general-dynamic model emits under -fPIC and
 * which measured ~15-20 ns on the hot path).  A dlopened extension gets its
 * 8-byte TLS slot from glibc's static surplus, so IE is safe here. */
static _Thread_local blk *tls_blk __attribute__((tls_model("initial-exec")));
static pthread_key_t   g_key;               /* ONLY for a thread-exit destructor */
static int             g_key_ok;
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static blk            *g_head;              /* intrusive list of live blocks */
static uint64_t        g_dead[CNT_N];       /* drained from exited threads */

/* Thread death: drain into g_dead so counts from exited threads are never lost
 * (R66 monotonicity across a churny thread pool).  Must not touch tls_blk,
 * which may already be torn down. */
static void blk_destroy(void *p)
{
    blk *b = (blk *)p;
    if (b == NULL)
        return;
    pthread_mutex_lock(&g_lock);
    for (int i = 0; i < CNT_N; i++)
        g_dead[i] += atomic_load_explicit(&b->c[i], memory_order_relaxed);
    for (blk **pp = &g_head; *pp != NULL; pp = &(*pp)->next) {
        if (*pp == b) { *pp = b->next; break; }
    }
    pthread_mutex_unlock(&g_lock);
    free(b);
}

/* Thread birth (lazy, once per thread, off the measured path). */
static blk *blk_init(void)
{
    void *mem = NULL;
    if (posix_memalign(&mem, 64, sizeof(blk)) != 0 || mem == NULL)
        return NULL;
    memset(mem, 0, sizeof(blk));
    blk *b = (blk *)mem;
    pthread_mutex_lock(&g_lock);
    b->next = g_head;
    g_head = b;
    pthread_mutex_unlock(&g_lock);
    if (g_key_ok)
        pthread_setspecific(g_key, b);
    tls_blk = b;
    return b;
}

static inline void bump(blk *b, int i)
{
    /* single-writer relaxed increment -- NOT atomic_fetch_add */
    atomic_store_explicit(&b->c[i],
        atomic_load_explicit(&b->c[i], memory_order_relaxed) + 1u,
        memory_order_relaxed);
}

static inline blk *cnt_blk(void)
{
    blk *b = tls_blk;
    if (__builtin_expect(b == NULL, 0))
        b = blk_init();
    return b;
}

/* One dispatch on `path` (+ total), exactly like _route._record. */
static inline void count_path(int idx)
{
    blk *b = cnt_blk();
    if (__builtin_expect(b == NULL, 0))
        return;                              /* OOM: lose a count, never crash */
    bump(b, idx);
    bump(b, CNT_TOTAL);
}

/* Exactly _route._record_error_fallback (R52). */
static void count_error_fallback(void)
{
    blk *b = cnt_blk();
    if (b == NULL)
        return;
    bump(b, CNT_DEVERR);
    bump(b, CNT_FB_ERR);
    bump(b, CNT_TOTAL);
}

/* ---------------------------------------------------------------------
 * Python callables configured by _route at import (strong refs, C statics).
 * No import machinery, no attribute lookup on any path.
 * ------------------------------------------------------------------- */
static PyObject *g_serve_single;     /* _route._serve_model_single   */
static PyObject *g_serve_finditer;   /* _route._serve_model_finditer */
static PyObject *g_run_single;       /* _route.run_single   (bail-out) */
static PyObject *g_run_finditer;     /* _route.run_finditer (bail-out) */
static PyObject *g_run_findall;      /* _route.run_findall  (bail-out) */
static PyObject *g_run_sub;          /* _route.run_sub      (cold)     */
static PyObject *g_run_subn;         /* _route.run_subn     (cold)     */
static PyObject *g_run_split;        /* _route.run_split    (cold)     */

/* Op identity.  Order is fixed: it indexes both Pattern.m[] and OP_NAMES. */
enum { OP_SEARCH = 0, OP_MATCH, OP_FULLMATCH, OP_FINDITER, OP_FINDALL, OP_N };
static const char *const OP_ATTRS[OP_N] = {
    "search", "match", "fullmatch", "finditer", "findall",
};
static PyObject *g_op_name[OP_N];    /* interned str, handed to the Python side */
static PyObject *g_zero;             /* the small-int 0 (default pos) */

/* ---------------------------------------------------------------------
 * pyro._fast.Pattern
 *
 * Layout mirrors _match.PyPattern.__slots__ so every Python reader keeps
 * working: _stock, _classification, _calls, _prog, __weakref__.
 * ------------------------------------------------------------------- */
typedef struct {
    PyObject_HEAD
    PyObject *stock;             /* compiled stdlib re.Pattern  (READONLY) */
    PyObject *classification;    /* Classification              (READONLY) */
    PyObject *prog;              /* lazily-compiled model program (rw; None) */
    unsigned long long calls;    /* per-pattern reuse counter (rw, S0)      */
    PyObject *m[OP_N];           /* cached BOUND stock methods (99 ns saved) */
    unsigned char eligible;      /* classification.eligible, cached at ctor  */
    PyObject *weakreflist;
} PatternObj;

static PyObject *
Pattern_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    (void)args; (void)kwds;
    PatternObj *self = (PatternObj *)type->tp_alloc(type, 0);
    if (self == NULL)
        return NULL;
    self->stock = NULL;
    self->classification = NULL;
    self->prog = Py_NewRef(Py_None);
    self->calls = 0;
    for (int i = 0; i < OP_N; i++)
        self->m[i] = NULL;
    self->eligible = 0;
    self->weakreflist = NULL;
    return (PyObject *)self;
}

static int
Pattern_init(PyObject *op, PyObject *args, PyObject *kwds)
{
    static char *kwlist[] = { "stock", "classification", NULL };
    PatternObj *self = (PatternObj *)op;
    PyObject *stock = NULL, *classi = NULL;
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "OO:Pattern", kwlist,
                                     &stock, &classi))
        return -1;

    Py_XSETREF(self->stock, Py_NewRef(stock));
    Py_XSETREF(self->classification, Py_NewRef(classi));
    self->calls = 0;
    Py_XSETREF(self->prog, Py_NewRef(Py_None));

    /* Cache eligibility ONCE.  The hot path must never walk
     * patt._classification.eligible (a Python attribute chain). */
    PyObject *el = PyObject_GetAttrString(classi, "eligible");
    if (el == NULL)
        return -1;
    int truth = PyObject_IsTrue(el);
    Py_DECREF(el);
    if (truth < 0)
        return -1;
    self->eligible = (unsigned char)truth;

    /* Cache the five BOUND stock methods.  A non-re.Pattern mock may not have
     * them all: store NULL and re-fetch lazily at call time. */
    for (int i = 0; i < OP_N; i++) {
        PyObject *meth = PyObject_GetAttrString(stock, OP_ATTRS[i]);
        if (meth == NULL) {
            if (!PyErr_ExceptionMatches(PyExc_AttributeError))
                return -1;
            PyErr_Clear();
        }
        Py_XSETREF(self->m[i], meth);   /* may be NULL */
    }
    /* Track only now that the object is fully populated (PyType_GenericAlloc
     * does not track GC objects for us).  Idempotent-guard against a re-init. */
    if (!PyObject_GC_IsTracked((PyObject *)self))
        PyObject_GC_Track(self);
    return 0;
}

static int
Pattern_traverse(PyObject *op, visitproc visit, void *arg)
{
    PatternObj *self = (PatternObj *)op;
    Py_VISIT(self->stock);
    Py_VISIT(self->classification);
    Py_VISIT(self->prog);
    for (int i = 0; i < OP_N; i++)
        Py_VISIT(self->m[i]);
    return 0;
}

static int
Pattern_clear(PyObject *op)
{
    PatternObj *self = (PatternObj *)op;
    Py_CLEAR(self->stock);
    Py_CLEAR(self->classification);
    Py_CLEAR(self->prog);
    for (int i = 0; i < OP_N; i++)
        Py_CLEAR(self->m[i]);
    return 0;
}

static void
Pattern_dealloc(PyObject *op)
{
    PatternObj *self = (PatternObj *)op;
    PyObject_GC_UnTrack(op);
    if (self->weakreflist != NULL)
        PyObject_ClearWeakRefs(op);
    (void)Pattern_clear(op);
    Py_TYPE(op)->tp_free(op);   /* static type: do NOT decref tp */
}

/* --- the fallback delegation (transcribed from _route._stock_op) ---------
 *
 *   meth = getattr(patt._stock, op)
 *   if pos == 0 and endpos is None:
 *       return meth(string)
 *   return meth(string, pos, len(string) if endpos is None else endpos)
 *
 * The `len(string)` substitution for a None endpos when pos != 0 is the fix for
 * the spike's bug: stock re.Pattern.search(s, 0, None) raises TypeError, while
 * PyroPattern.search(s, 0, None) returns a Match.  PyroPattern is the oracle.
 */
static PyObject *
stock_op(PatternObj *self, int op, PyObject *string,
         PyObject *pos_obj, int pos_is_zero, PyObject *endpos_obj)
{
    PyObject *m = self->m[op];
    if (__builtin_expect(m == NULL, 0)) {           /* mock without that method */
        m = PyObject_GetAttrString(self->stock, OP_ATTRS[op]);
        if (m == NULL)
            return NULL;
        Py_XSETREF(self->m[op], m);                 /* keeps the ref we own */
    }

    if (pos_is_zero && endpos_obj == Py_None) {
        PyObject *argv[1] = { string };
        return PyObject_Vectorcall(m, argv, 1, NULL);
    }

    PyObject *ep = endpos_obj;
    PyObject *tmp = NULL;
    if (ep == Py_None) {
        /* NOT PyUnicode_GET_LENGTH: on this path the subject may be a
         * bytearray/memoryview (the S3 type gate routes those here).  A
         * TypeError out of PyObject_Size is exactly what len() would raise. */
        Py_ssize_t n = PyObject_Size(string);
        if (n < 0)
            return NULL;
        tmp = PyLong_FromSsize_t(n);
        if (tmp == NULL)
            return NULL;
        ep = tmp;
    }
    PyObject *argv[3] = { string, pos_obj, ep };
    PyObject *r = PyObject_Vectorcall(m, argv, 3, NULL);
    Py_XDECREF(tmp);
    return r;
}

/* Hand the ENTIRE call (args + kwnames, untouched) to a Python router entry
 * point.  Used for exotic argument shapes, BEFORE the S0 counter increment, so
 * Python's own rich-compare / __index__ semantics are reproduced by
 * construction rather than re-implemented in C.  The Python entry point runs
 * _decide itself, so _calls is incremented exactly once. */
static PyObject *
bail_to_python(PyObject *fn, PyObject *self, PyObject *op_name,
               PyObject *const *args, Py_ssize_t nargs, PyObject *kwnames)
{
    Py_ssize_t nkw = (kwnames == NULL) ? 0 : PyTuple_GET_SIZE(kwnames);
    Py_ssize_t extra = (op_name == NULL) ? 1 : 2;
    Py_ssize_t total = extra + nargs + nkw;
    PyObject **stack = (PyObject **)PyMem_Malloc(
        (size_t)(total ? total : 1) * sizeof(PyObject *));
    if (stack == NULL)
        return PyErr_NoMemory();
    stack[0] = self;
    if (op_name != NULL)
        stack[1] = op_name;
    for (Py_ssize_t i = 0; i < nargs + nkw; i++)
        stack[extra + i] = args[i];
    PyObject *r = PyObject_Vectorcall(fn, stack,
                                      (size_t)(extra + nargs), kwnames);
    PyMem_Free(stack);
    return r;
}

/* ---------------------------------------------------------------------
 * THE HOT PATH.
 * ------------------------------------------------------------------- */
static inline PyObject *
gate(PatternObj *self, int op, PyObject *const *args, Py_ssize_t nargs,
     PyObject *kwnames)
{
    /* --- BAIL-OUT (evaluated BEFORE S0, so semantics are untouched) ------
     * Anything but a plain positional (string[, pos[, endpos]]) with exact-int
     * pos / (None|exact-int) endpos is handed whole to the Python router. */
    PyObject *string, *pos_obj = g_zero, *endpos_obj = Py_None;
    if (__builtin_expect(kwnames != NULL || nargs < 1 || nargs > 3, 0))
        goto bail;
    string = args[0];
    if (nargs >= 2) {
        pos_obj = args[1];
        if (!PyLong_CheckExact(pos_obj))       /* bool, numpy.int64, __index__ */
            goto bail;
    }
    if (nargs == 3) {
        endpos_obj = args[2];
        if (endpos_obj != Py_None && !PyLong_CheckExact(endpos_obj))
            goto bail;
    }

    /* --- S0: the reuse counter side effect happens FIRST, before every gate,
     * including PYRO_DISABLE (_route.py:286-287). --------------------------*/
    uint64_t reuse = (uint64_t)self->calls;
    self->calls = (unsigned long long)(reuse + 1);

    /* --- gather the decision inputs (nothing else may be read) ------------*/
    uint32_t env = atomic_load_explicit(&g_env, memory_order_relaxed);
    pyro_route_in in;
    in.reuse      = reuse;
    in.disabled   = (uint8_t)(env & ENV_DISABLED);
    in.force      = (uint8_t)((env & ENV_FORCE) != 0);
    in.eligible   = self->eligible;
    in.subj_len   = 0;
    in.full_span  = 0;

    /* S3: type(string) is str / bytes, exactly.  len() is computed ONLY after
     * this gate passes -- Python does not call len() before it either. */
    Py_ssize_t slen = 0;
    if (PyUnicode_CheckExact(string)) {
        slen = PyUnicode_GET_LENGTH(string);     /* code points, like len() */
        in.exact_type = 1;
    } else if (PyBytes_CheckExact(string)) {
        slen = PyBytes_GET_SIZE(string);
        in.exact_type = 1;
    } else {
        in.exact_type = 0;
    }

    /* pos == 0?  (Python: `pos != 0` -> not full span.)  A huge/negative pos is
     * simply != 0, so an overflow is not an error here. */
    int pos_is_zero;
    if (pos_obj == g_zero) {
        pos_is_zero = 1;
    } else {
        int ovf = 0;
        long long pv = PyLong_AsLongLongAndOverflow(pos_obj, &ovf);
        if (ovf != 0) {
            pos_is_zero = 0;
        } else if (pv == -1 && PyErr_Occurred()) {
            PyErr_Clear();
            pos_is_zero = 0;
        } else {
            pos_is_zero = (pv == 0);
        }
    }

    if (in.exact_type) {
        in.subj_len = (uint64_t)slen;
        /* S4: _is_full_span -- pos == 0 and (endpos is None or endpos >= len).
         * An OverflowError must never leak out of the decision: a huge positive
         * endpos is >= len (full span), a huge negative one is < len. */
        int ep_ge_len;
        if (endpos_obj == Py_None) {
            ep_ge_len = 1;
        } else {
            int ovf = 0;
            long long ev = PyLong_AsLongLongAndOverflow(endpos_obj, &ovf);
            if (ovf > 0)        ep_ge_len = 1;
            else if (ovf < 0)   ep_ge_len = 0;
            else if (ev == -1 && PyErr_Occurred()) { PyErr_Clear(); ep_ge_len = 1; }
            else                ep_ge_len = (ev >= (long long)slen);
        }
        in.full_span = (uint8_t)(pos_is_zero && ep_ge_len);
    }

    /* --- S1..S6: a DIRECT C call (inlined), no PLT hop, no ctypes ---------*/
    if (__builtin_expect(pyro_route_decide_inline(&in) == PYRO_ROUTE_FALLBACK, 1)) {
        count_path(CNT_FALLBACK);
        return stock_op(self, op, string, pos_obj, pos_is_zero, endpos_obj);
    }

    /* --- model verdict: hand off to the SAME Python function the pure-Python
     * router uses post-decision.  It must NOT touch _calls (already done at S0)
     * and it owns the UTF-8 transportability probe (R51a(d)), the residency
     * consultation, and the model/error accounting. ------------------------*/
    {
        PyObject *fn = (op == OP_FINDITER) ? g_serve_finditer : g_serve_single;
        if (fn == NULL) {
            /* _route.configure() has not run (e.g. the native Pattern type was
             * imported directly while PYRO_NO_NATIVE=1 skipped configure()).
             * A model verdict has nowhere to go; raise instead of vectorcalling
             * NULL and segfaulting the interpreter. */
            PyErr_SetString(PyExc_RuntimeError,
                            "pyro._fast: model handoff unconfigured "
                            "(pyro._route.configure() has not run; do not use "
                            "pyro._fast.Pattern directly)");
            return NULL;
        }
        if (op == OP_FINDITER) {
            PyObject *argv[4] = { (PyObject *)self, string, pos_obj, endpos_obj };
            return PyObject_Vectorcall(fn, argv, 4, NULL);
        }
        PyObject *argv[5] = { (PyObject *)self, g_op_name[op], string,
                              pos_obj, endpos_obj };
        return PyObject_Vectorcall(fn, argv, 5, NULL);
    }

bail:
    switch (op) {
    case OP_FINDITER:
        return bail_to_python(g_run_finditer, (PyObject *)self, NULL,
                              args, nargs, kwnames);
    case OP_FINDALL:
        return bail_to_python(g_run_findall, (PyObject *)self, NULL,
                              args, nargs, kwnames);
    default:
        return bail_to_python(g_run_single, (PyObject *)self, g_op_name[op],
                              args, nargs, kwnames);
    }
}

/* findall: _route.run_findall runs the decision purely for the S0 reuse side
 * effect, DISCARDS the verdict, always records `fallback`, and delegates the
 * whole op to stock re (aggregate-op offload is a later phase). */
static PyObject *
gate_findall(PatternObj *self, PyObject *const *args, Py_ssize_t nargs,
             PyObject *kwnames)
{
    PyObject *string, *pos_obj = g_zero, *endpos_obj = Py_None;
    if (__builtin_expect(kwnames != NULL || nargs < 1 || nargs > 3, 0))
        return bail_to_python(g_run_findall, (PyObject *)self, NULL,
                              args, nargs, kwnames);
    string = args[0];
    if (nargs >= 2) {
        pos_obj = args[1];
        if (!PyLong_CheckExact(pos_obj))
            return bail_to_python(g_run_findall, (PyObject *)self, NULL,
                                  args, nargs, kwnames);
    }
    if (nargs == 3) {
        endpos_obj = args[2];
        if (endpos_obj != Py_None && !PyLong_CheckExact(endpos_obj))
            return bail_to_python(g_run_findall, (PyObject *)self, NULL,
                                  args, nargs, kwnames);
    }
    self->calls++;                                   /* S0 (verdict discarded) */

    int pos_is_zero;
    if (pos_obj == g_zero) {
        pos_is_zero = 1;
    } else {
        int ovf = 0;
        long long pv = PyLong_AsLongLongAndOverflow(pos_obj, &ovf);
        if (ovf != 0 || (pv == -1 && PyErr_Occurred())) {
            PyErr_Clear();
            pos_is_zero = 0;
        } else {
            pos_is_zero = (pv == 0);
        }
    }
    count_path(CNT_FALLBACK);
    return stock_op(self, OP_FINDALL, string, pos_obj, pos_is_zero, endpos_obj);
}

#define GATE_METHOD(NAME, OP)                                                 \
static PyObject *                                                             \
Pattern_##NAME(PyObject *self, PyObject *const *args, Py_ssize_t nargs,       \
               PyObject *kwnames)                                             \
{                                                                             \
    return gate((PatternObj *)self, OP, args, nargs, kwnames);                \
}

GATE_METHOD(search,    OP_SEARCH)
GATE_METHOD(match,     OP_MATCH)
GATE_METHOD(fullmatch, OP_FULLMATCH)
GATE_METHOD(finditer,  OP_FINDITER)

static PyObject *
Pattern_findall(PyObject *self, PyObject *const *args, Py_ssize_t nargs,
                PyObject *kwnames)
{
    return gate_findall((PatternObj *)self, args, nargs, kwnames);
}

/* --- cold trampolines: sub / subn / split -------------------------------
 * These vectorcall the existing _route.run_* functions and touch NOTHING else.
 * Those functions run _decide themselves, so the trampoline MUST NOT touch
 * _calls (that would double-increment). */
#define TRAMPOLINE(NAME, FN)                                                  \
static PyObject *                                                             \
Pattern_##NAME(PyObject *self, PyObject *const *args, Py_ssize_t nargs,       \
               PyObject *kwnames)                                             \
{                                                                             \
    return bail_to_python(FN, self, NULL, args, nargs, kwnames);              \
}

TRAMPOLINE(sub,   g_run_sub)
TRAMPOLINE(subn,  g_run_subn)
TRAMPOLINE(split, g_run_split)

static PyObject *
Pattern_repr(PyObject *op)
{
    PatternObj *self = (PatternObj *)op;
    PyObject *pat = PyObject_GetAttrString(self->stock, "pattern");
    if (pat == NULL)
        return NULL;
    PyObject *r = PyUnicode_FromFormat("<pyro.Pattern %R>", pat);
    Py_DECREF(pat);
    return r;
}

/* --- delegated attributes (R27) ---------------------------------------- */
static PyObject *deleg(PyObject *op, void *closure)
{
    PatternObj *self = (PatternObj *)op;
    return PyObject_GetAttrString(self->stock, (const char *)closure);
}

static PyGetSetDef Pattern_getset[] = {
    {"pattern",    deleg, NULL, "the source pattern (R27)",  (void *)"pattern"},
    {"flags",      deleg, NULL, "the effective flags (R27)", (void *)"flags"},
    {"groups",     deleg, NULL, "capture-group count (R27)", (void *)"groups"},
    {"groupindex", deleg, NULL, "named-group map (R27)",     (void *)"groupindex"},
    {NULL}
};

static PyMemberDef Pattern_members[] = {
    {"_stock",          T_OBJECT_EX, offsetof(PatternObj, stock),          READONLY},
    {"_classification", T_OBJECT_EX, offsetof(PatternObj, classification), READONLY},
    {"_calls",          T_ULONGLONG, offsetof(PatternObj, calls),          0},
    {"_prog",           T_OBJECT_EX, offsetof(PatternObj, prog),           0},
    {NULL}
};

static PyMethodDef Pattern_methods[] = {
    {"search",    (PyCFunction)(void (*)(void))Pattern_search,
     METH_FASTCALL | METH_KEYWORDS, "search(string, pos=0, endpos=None)"},
    {"match",     (PyCFunction)(void (*)(void))Pattern_match,
     METH_FASTCALL | METH_KEYWORDS, "match(string, pos=0, endpos=None)"},
    {"fullmatch", (PyCFunction)(void (*)(void))Pattern_fullmatch,
     METH_FASTCALL | METH_KEYWORDS, "fullmatch(string, pos=0, endpos=None)"},
    {"finditer",  (PyCFunction)(void (*)(void))Pattern_finditer,
     METH_FASTCALL | METH_KEYWORDS, "finditer(string, pos=0, endpos=None)"},
    {"findall",   (PyCFunction)(void (*)(void))Pattern_findall,
     METH_FASTCALL | METH_KEYWORDS, "findall(string, pos=0, endpos=None)"},
    {"sub",       (PyCFunction)(void (*)(void))Pattern_sub,
     METH_FASTCALL | METH_KEYWORDS, "sub(repl, string, count=0)"},
    {"subn",      (PyCFunction)(void (*)(void))Pattern_subn,
     METH_FASTCALL | METH_KEYWORDS, "subn(repl, string, count=0)"},
    {"split",     (PyCFunction)(void (*)(void))Pattern_split,
     METH_FASTCALL | METH_KEYWORDS, "split(string, maxsplit=0)"},
    {NULL}
};

static PyTypeObject PatternType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name      = "pyro._fast.Pattern",
    .tp_basicsize = sizeof(PatternObj),
    .tp_dealloc   = Pattern_dealloc,
    .tp_repr      = Pattern_repr,
    .tp_flags     = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE | Py_TPFLAGS_HAVE_GC,
    .tp_doc       = "Native drop-in for re.Pattern (R27) with the R51 routing "
                    "decision in compiled code (R3b/R3c).",
    .tp_traverse  = Pattern_traverse,
    .tp_clear     = Pattern_clear,
    .tp_weaklistoffset = offsetof(PatternObj, weakreflist),
    .tp_methods   = Pattern_methods,
    .tp_members   = Pattern_members,
    .tp_getset    = Pattern_getset,
    .tp_init      = Pattern_init,
    .tp_new       = Pattern_new,
};

/* ---------------------------------------------------------------------
 * Module-level functions.
 * ------------------------------------------------------------------- */
static PyObject *
fast_configure(PyObject *mod, PyObject *args)
{
    (void)mod;
    PyObject *ss, *sf, *rs, *rf, *rfa, *rsub, *rsubn, *rsplit;
    if (!PyArg_ParseTuple(args, "OOOOOOOO:configure",
                          &ss, &sf, &rs, &rf, &rfa, &rsub, &rsubn, &rsplit))
        return NULL;
    Py_XSETREF(g_serve_single,   Py_NewRef(ss));
    Py_XSETREF(g_serve_finditer, Py_NewRef(sf));
    Py_XSETREF(g_run_single,     Py_NewRef(rs));
    Py_XSETREF(g_run_finditer,   Py_NewRef(rf));
    Py_XSETREF(g_run_findall,    Py_NewRef(rfa));
    Py_XSETREF(g_run_sub,        Py_NewRef(rsub));
    Py_XSETREF(g_run_subn,       Py_NewRef(rsubn));
    Py_XSETREF(g_run_split,      Py_NewRef(rsplit));
    Py_RETURN_NONE;
}

static PyObject *
fast_set_env(PyObject *mod, PyObject *args)
{
    (void)mod;
    int disabled = 0, force = 0;
    if (!PyArg_ParseTuple(args, "pp:set_env", &disabled, &force))
        return NULL;
    uint32_t w = (disabled ? ENV_DISABLED : 0u) | (force ? ENV_FORCE : 0u);
    atomic_store_explicit(&g_env, w, memory_order_relaxed);
    Py_RETURN_NONE;
}

static PyObject *
fast_stats(PyObject *mod, PyObject *unused)
{
    (void)mod; (void)unused;
    uint64_t out[CNT_N];
    pthread_mutex_lock(&g_lock);
    for (int i = 0; i < CNT_N; i++)
        out[i] = g_dead[i];
    for (blk *b = g_head; b != NULL; b = b->next)
        for (int i = 0; i < CNT_N; i++)
            out[i] += atomic_load_explicit(&b->c[i], memory_order_relaxed);
    pthread_mutex_unlock(&g_lock);

    PyObject *d = PyDict_New();
    if (d == NULL)
        return NULL;
    for (int i = 0; i < CNT_N; i++) {          /* exact _STATS key order */
        PyObject *v = PyLong_FromUnsignedLongLong(out[i]);
        if (v == NULL || PyDict_SetItemString(d, CNT_KEYS[i], v) < 0) {
            Py_XDECREF(v);
            Py_DECREF(d);
            return NULL;
        }
        Py_DECREF(v);
    }
    return d;
}

static PyObject *
fast_reset_stats(PyObject *mod, PyObject *unused)
{
    (void)mod; (void)unused;
    pthread_mutex_lock(&g_lock);
    for (int i = 0; i < CNT_N; i++)
        g_dead[i] = 0;
    for (blk *b = g_head; b != NULL; b = b->next)
        for (int i = 0; i < CNT_N; i++)
            atomic_store_explicit(&b->c[i], 0u, memory_order_relaxed);
    pthread_mutex_unlock(&g_lock);
    Py_RETURN_NONE;
}

static PyObject *
fast_count(PyObject *mod, PyObject *arg)
{
    (void)mod;
    long idx = PyLong_AsLong(arg);
    if (idx == -1 && PyErr_Occurred())
        return NULL;
    if (idx < 0 || idx >= CNT_TOTAL) {
        PyErr_SetString(PyExc_ValueError, "counter index out of range");
        return NULL;
    }
    count_path((int)idx);
    Py_RETURN_NONE;
}

static PyObject *
fast_count_error(PyObject *mod, PyObject *unused)
{
    (void)mod; (void)unused;
    count_error_fallback();
    Py_RETURN_NONE;
}

/* Test-only seam: drive pyro_route_decide directly over the full input domain
 * (the 288-point exhaustive equivalence test).  Never used at runtime. */
static PyObject *
fast_decide_raw(PyObject *mod, PyObject *const *args, Py_ssize_t nargs)
{
    (void)mod;
    if (nargs != 7) {
        PyErr_SetString(PyExc_TypeError,
                        "decide_raw(subj_len, reuse, disabled, force, eligible, "
                        "exact_type, full_span)");
        return NULL;
    }
    unsigned long long v[7];
    for (Py_ssize_t i = 0; i < 7; i++) {
        v[i] = PyLong_AsUnsignedLongLong(args[i]);
        if (v[i] == (unsigned long long)-1 && PyErr_Occurred())
            return NULL;
    }
    pyro_route_in in = {
        .subj_len   = (uint64_t)v[0],
        .reuse      = (uint64_t)v[1],
        .disabled   = (uint8_t)(v[2] != 0),
        .force      = (uint8_t)(v[3] != 0),
        .eligible   = (uint8_t)(v[4] != 0),
        .exact_type = (uint8_t)(v[5] != 0),
        .full_span  = (uint8_t)(v[6] != 0),
    };
    return PyLong_FromLong(pyro_route_decide_inline(&in));
}

static PyMethodDef fast_methods[] = {
    {"configure",   fast_configure,   METH_VARARGS,
     "configure(serve_single, serve_finditer, run_single, run_finditer, "
     "run_findall, run_sub, run_subn, run_split)"},
    {"set_env",     fast_set_env,     METH_VARARGS,
     "set_env(disabled, force) -- push the R35a env snapshot (one atomic word)"},
    {"stats",       fast_stats,       METH_NOARGS,
     "aggregate the per-thread dispatch counters (R66.1)"},
    {"reset_stats", fast_reset_stats, METH_NOARGS, "zero all dispatch counters"},
    {"count",       fast_count,       METH_O,
     "count(idx) -- cold-path counter shim for _route._record"},
    {"count_error", fast_count_error, METH_NOARGS,
     "cold-path shim for _route._record_error_fallback (R52)"},
    {"decide_raw",  (PyCFunction)(void (*)(void))fast_decide_raw, METH_FASTCALL,
     "test-only: run the native R51 decision core on raw inputs"},
    {NULL}
};

static int
fast_exec(PyObject *mod)
{
    if (g_zero == NULL) {
        g_zero = PyLong_FromLong(0);
        if (g_zero == NULL)
            return -1;
    }
    for (int i = 0; i < OP_N; i++) {
        if (g_op_name[i] == NULL) {
            g_op_name[i] = PyUnicode_InternFromString(OP_ATTRS[i]);
            if (g_op_name[i] == NULL)
                return -1;
        }
    }
    if (!g_key_ok) {
        if (pthread_key_create(&g_key, blk_destroy) == 0)
            g_key_ok = 1;
    }
    if (PyType_Ready(&PatternType) < 0)
        return -1;
    if (PyModule_AddObjectRef(mod, "Pattern", (PyObject *)&PatternType) < 0)
        return -1;
    if (PyModule_AddIntConstant(mod, "ROUTE_ABI", PYRO_ROUTE_ABI) < 0)
        return -1;
    if (PyModule_AddIntConstant(mod, "S_MIN", (long)PYRO_S_MIN) < 0)
        return -1;
    if (PyModule_AddIntConstant(mod, "N_REUSE", (long)PYRO_N_REUSE) < 0)
        return -1;
    /* Counter index order == _route._STATS insertion order (R52/R66 shape). */
    if (PyModule_AddIntConstant(mod, "CNT_HW", CNT_HW) < 0 ||
        PyModule_AddIntConstant(mod, "CNT_MODEL", CNT_MODEL) < 0 ||
        PyModule_AddIntConstant(mod, "CNT_FALLBACK", CNT_FALLBACK) < 0 ||
        PyModule_AddIntConstant(mod, "CNT_FB_ERR", CNT_FB_ERR) < 0 ||
        PyModule_AddIntConstant(mod, "CNT_DEVERR", CNT_DEVERR) < 0)
        return -1;
    return 0;
}

static PyModuleDef_Slot fast_slots[] = {
    {Py_mod_exec, (void *)fast_exec},
#if PY_VERSION_HEX >= 0x030C0000
    /* The TLS counter blocks and the env word are process-global C statics,
     * whereas _route._STATS is a per-subinterpreter module global.  Fail loudly
     * rather than silently share counters across subinterpreters. */
    {Py_mod_multiple_interpreters, Py_MOD_MULTIPLE_INTERPRETERS_NOT_SUPPORTED},
#endif
    /* NOTE: Py_mod_gil is deliberately NOT declared as Py_MOD_GIL_NOT_USED.
     * The S0 `self->calls++` RMW and the indivisibility of the (fallback,total)
     * pair w.r.t. a stats() snapshot both lean on the GIL.  On a free-threaded
     * build CPython therefore re-enables the GIL for this module, which is the
     * conservative, correct outcome until that audit is done. */
    {0, NULL}
};

static struct PyModuleDef fast_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "pyro._fast",
    .m_doc  = "Native R51 routing hot path (R3b/R3c).",
    .m_size = 0,
    .m_methods = fast_methods,
    .m_slots = fast_slots,
};

PyMODINIT_FUNC
PyInit__fast(void)
{
    return PyModuleDef_Init(&fast_module);
}
