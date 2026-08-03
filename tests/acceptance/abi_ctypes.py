"""ctypes binding to the public ABI 2.0.0 library (build/libpyro_rt.so) for the
Phase 1 C-ABI conformance tests (AC-1-1/AC-1-2, R57/R58).

Signatures and enum values are transcribed from the public ABI contract
(spec §7.3, R37–R44, and include/pyro_rt.h). Nothing here reads the runtime's
C source.  If the library is not built and cannot be built (no cc), callers
skip.
"""
import ctypes
import os
import subprocess

REPO_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.abspath(__file__))))
LIB_PATH = os.path.join(REPO_ROOT, "build", "libpyro_rt.so")
HEADER_PATH = os.path.join(REPO_ROOT, "include", "pyro_rt.h")

ABI_2_0_0 = 0x00020000
# Phase-0 historical checkpoint (R37 version-history note)
ABI_1_0_0 = 0x00010000

# pyro_status (R38) — frozen, additive-only from ABI 2.0.0 (R37).
PYRO_OK = 0
PYRO_E_UNSUPPORTED = 1
PYRO_E_CAPACITY = 2
PYRO_E_DEVICE = 3
PYRO_E_INVALID = 4
PYRO_E_NOMEM = 5
PYRO_E_TIMEOUT = 6
PYRO_E_NOT_RESIDENT = 7
PYRO_E_SYNTH = 8

# pyro_encoding (R38)
PYRO_ENC_BYTES = 0
PYRO_ENC_UTF8 = 1

# pyro_circ_status (R38)
PYRO_CIRC_COLD = 0
PYRO_CIRC_SYNTHESIZING = 1
PYRO_CIRC_WARM = 2
PYRO_CIRC_RESIDENT = 3
PYRO_CIRC_FALLBACK = 4


class PyroMatch(ctypes.Structure):
    """R38/R47 result-ring entry: start(8) end(8) pattern_id(4) flags(4)."""
    _fields_ = [
        ("start", ctypes.c_uint64),
        ("end", ctypes.c_uint64),
        ("pattern_id", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
    ]


class PyroCaps(ctypes.Structure):
    """R42 capability struct."""
    _fields_ = [
        ("max_states", ctypes.c_uint32),
        ("max_patterns", ctypes.c_uint32),
        ("max_repeat", ctypes.c_uint32),
        ("alphabet", ctypes.c_uint32),
        ("generator_version", ctypes.c_uint32),
        ("harness_version", ctypes.c_uint32),
        ("datapath_bytes", ctypes.c_uint32),
        ("pr_luts", ctypes.c_uint32),
        ("pr_ffs", ctypes.c_uint32),
        ("pr_bram_kb", ctypes.c_uint32),
        ("pr_dsps", ctypes.c_uint32),
        ("pr_partitions", ctypes.c_uint32),
    ]


def _ensure_lib():
    if os.path.isfile(LIB_PATH):
        return None
    make = None
    for cand in ("make", "/usr/bin/make"):
        try:
            subprocess.run([cand, "lib"], cwd=REPO_ROOT, capture_output=True,
                           timeout=120, check=False)
            make = cand
            break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return "cannot build libpyro_rt.so (no make/cc)" if not os.path.isfile(
        LIB_PATH) else None


def load():
    """Return a configured CDLL, or raise OSError if unavailable (caller
    skips)."""
    reason = _ensure_lib()
    if reason:
        raise OSError(reason)
    lib = ctypes.CDLL(LIB_PATH)
    P = ctypes.c_void_p
    c_int = ctypes.c_int
    sz = ctypes.c_size_t

    lib.pyro_abi_version.restype = ctypes.c_uint32
    lib.pyro_abi_version.argtypes = []

    lib.pyro_ctx_open.restype = c_int
    lib.pyro_ctx_open.argtypes = [ctypes.POINTER(P), ctypes.c_char_p]
    lib.pyro_ctx_close.restype = None
    lib.pyro_ctx_close.argtypes = [P]

    lib.pyro_generate.restype = c_int
    lib.pyro_generate.argtypes = [P, ctypes.c_char_p, sz, ctypes.c_uint32,
                                  c_int, ctypes.POINTER(P)]
    lib.pyro_synth_request.restype = c_int
    lib.pyro_synth_request.argtypes = [P, P]
    lib.pyro_circuit_status.restype = c_int
    lib.pyro_circuit_status.argtypes = [P, P, ctypes.POINTER(c_int)]
    lib.pyro_circuit_load.restype = c_int
    lib.pyro_circuit_load.argtypes = [P, P]
    lib.pyro_circuit_free.restype = None
    lib.pyro_circuit_free.argtypes = [P]

    lib.pyro_scan.restype = c_int
    lib.pyro_scan.argtypes = [
    P,
    P,
    ctypes.c_char_p,
    sz,
    ctypes.c_uint64,
    ctypes.POINTER(PyroMatch),
    sz,
     ctypes.POINTER(sz)]

    lib.pyro_caps_get.restype = c_int
    lib.pyro_caps_get.argtypes = [P, ctypes.POINTER(PyroCaps)]

    lib.pyro_ctx_debug_csr_read.restype = ctypes.c_uint32
    lib.pyro_ctx_debug_csr_read.argtypes = [P, ctypes.c_uint32]
    lib.pyro_ctx_debug_force_misalign.restype = None
    lib.pyro_ctx_debug_force_misalign.argtypes = [P, c_int]
    return lib
