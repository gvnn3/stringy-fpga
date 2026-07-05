"""AC-1-1: the C ABI 2.0.0 passes ABI-conformance tests against the model
(`model://`), via ctypes.  (R57; R37, R39, R42, R44, R13, R43/R32)

Scope note (§13): `pyro_generate` takes an already-eligible L2-produced circuit
DESCRIPTOR (serialized automaton + locator), not raw regex (R40/R40a); that
descriptor's byte format is an internal L2↔L3 contract not defined in the public
§7.3.  Consequently the resident-circuit happy path (synth→load→resident scan),
the `PYRO_E_NOT_RESIDENT` scan guard on a real circuit, and the R49 alignment
check via the debug seam are not constructible from public information here; they
are exercised through the Python surface (AC-1-3/1-4/1-7) and recorded as a
public-surface limitation.  This module covers everything reachable in pure
ctypes: version, lifecycle open/close, caps, and the R44 defined-state-on-error
contract for every error-returning entry point, plus a thread-safety smoke.
"""
import ctypes
import threading

import pytest

import abi_ctypes as abi

lib = None
_load_error = None
try:
    lib = abi.load()
except OSError as e:
    _load_error = str(e)

pytestmark = pytest.mark.skipif(lib is None, reason=f"libpyro_rt.so unavailable: {_load_error}")


def _open(uri=b"model://"):
    ctx = ctypes.c_void_p()
    rc = lib.pyro_ctx_open(ctypes.byref(ctx), uri)
    return rc, ctx


def test_abi_version_is_2_0_0():
    """R37: pyro_abi_version() returns packed ABI 2.0.0 == 0x00020000."""
    assert lib.pyro_abi_version() == abi.ABI_2_0_0


def test_ctx_open_model_ok_nonnull():
    """R39: pyro_ctx_open('model://') returns PYRO_OK with a non-NULL ctx."""
    rc, ctx = _open()
    try:
        assert rc == abi.PYRO_OK
        assert ctx.value not in (None, 0)
    finally:
        if ctx:
            lib.pyro_ctx_close(ctx)


def test_ctx_open_bad_uri_defined_state():
    """R39/R44: a failing open returns non-PYRO_OK and leaves *out == NULL."""
    rc, ctx = _open(b"bogus://not-a-binding")
    assert rc != abi.PYRO_OK
    assert not ctx  # NULL


def test_caps_get_minimums():
    """R42/R13/R11: caps advertise at least the mandated complexity minimums and
    a sane PR-region budget/datapath."""
    rc, ctx = _open()
    assert rc == abi.PYRO_OK
    try:
        caps = abi.PyroCaps()
        assert lib.pyro_caps_get(ctx, ctypes.byref(caps)) == abi.PYRO_OK
        assert caps.max_states >= 1024, caps.max_states
        assert caps.max_patterns >= 256, caps.max_patterns
        assert caps.max_repeat >= 255, caps.max_repeat
        assert caps.alphabet == 256, caps.alphabet
        assert caps.datapath_bytes >= 1
        assert caps.pr_partitions >= 1
        assert caps.generator_version >= 1
        assert caps.harness_version >= 1
    finally:
        lib.pyro_ctx_close(ctx)


def test_generate_rejects_non_descriptor_defined_state():
    """R44/R40a: on a non-OK `pyro_generate` (here: raw non-descriptor bytes) the
    out handle MUST be left NULL.  The device-free model does not classify
    (R40a); it returns a defined error, never UB."""
    rc, ctx = _open()
    assert rc == abi.PYRO_OK
    try:
        circ = ctypes.c_void_p(0xDEADBEEF)  # poison; must be overwritten to NULL
        rc_gen = lib.pyro_generate(ctx, b"\x00\x01rawnotadescriptor", 18, 0,
                                   abi.PYRO_ENC_UTF8, ctypes.byref(circ))
        assert rc_gen != abi.PYRO_OK
        assert not circ, "R44: *out must be NULL on generate error"
    finally:
        lib.pyro_ctx_close(ctx)


def test_scan_null_circuit_defined_state():
    """R44: pyro_scan with no valid circuit returns non-OK and sets
    *out_count == 0 (no UB)."""
    rc, ctx = _open()
    assert rc == abi.PYRO_OK
    try:
        out = (abi.PyroMatch * 8)()
        n = ctypes.c_size_t(999)
        rc_scan = lib.pyro_scan(ctx, None, b"abc", 3, 0, out, 8, ctypes.byref(n))
        assert rc_scan != abi.PYRO_OK
        assert n.value == 0, "R44: *out_count must be 0 on scan error"
    finally:
        lib.pyro_ctx_close(ctx)


def test_status_and_load_null_circuit_defined():
    """R44: status/load/synth_request/free on a NULL circuit return a defined
    error and do not crash."""
    rc, ctx = _open()
    assert rc == abi.PYRO_OK
    try:
        st = ctypes.c_int(-1)
        assert lib.pyro_circuit_status(ctx, None, ctypes.byref(st)) != abi.PYRO_OK
        assert lib.pyro_circuit_load(ctx, None) != abi.PYRO_OK
        assert lib.pyro_synth_request(ctx, None) != abi.PYRO_OK
        lib.pyro_circuit_free(None)  # must not crash
    finally:
        lib.pyro_ctx_close(ctx)


def test_status_codes_within_frozen_enum_range():
    """R37/R38: every status returned by the reachable surface is within the
    frozen ABI-2.0.0 enum range [PYRO_OK .. PYRO_E_SYNTH]."""
    rc, ctx = _open()
    try:
        assert abi.PYRO_OK <= rc <= abi.PYRO_E_SYNTH
        out = (abi.PyroMatch * 1)()
        n = ctypes.c_size_t(0)
        rc_scan = lib.pyro_scan(ctx, None, b"x", 1, 0, out, 1, ctypes.byref(n))
        assert abi.PYRO_OK <= rc_scan <= abi.PYRO_E_SYNTH
    finally:
        if ctx:
            lib.pyro_ctx_close(ctx)


def test_thread_safety_smoke_distinct_contexts():
    """R43/R32: opening/using/closing distinct pyro_ctx concurrently from many
    threads is safe (no crash, all return defined states)."""
    errors = []

    def worker():
        try:
            rc, ctx = _open()
            if rc != abi.PYRO_OK:
                errors.append(("open", rc))
                return
            caps = abi.PyroCaps()
            for _ in range(50):
                if lib.pyro_caps_get(ctx, ctypes.byref(caps)) != abi.PYRO_OK:
                    errors.append(("caps", -1))
                    break
            lib.pyro_ctx_close(ctx)
        except BaseException as e:  # noqa
            errors.append(repr(e))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


def test_shared_ctx_concurrent_caps_internally_synchronized():
    """R43/R32: a single pyro_ctx is internally synchronized — concurrent
    caps_get from many threads never corrupts the result."""
    rc, ctx = _open()
    assert rc == abi.PYRO_OK
    errors = []

    def worker():
        caps = abi.PyroCaps()
        for _ in range(100):
            if lib.pyro_caps_get(ctx, ctypes.byref(caps)) != abi.PYRO_OK:
                errors.append("caps failed")
                return
            if caps.alphabet != 256:
                errors.append(("corrupt", caps.alphabet))
                return

    try:
        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
    finally:
        lib.pyro_ctx_close(ctx)
