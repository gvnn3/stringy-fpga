"""ctypes glue for the native host runtime (L3, ABI 2.0.0) — ``libpyro_rt.so``.

This is a thin loader + wrapper over the C ABI defined in ``include/pyro_rt.h``
and implemented in ``src/pyro_rt.c``.  It exists so later phases (Phase 2/3
integration) can route real dispatch through the native library.  **Nothing here
is wired into any dispatch path yet:** :mod:`pyro._route` still serves every
top-level call, and on the model/HW-eligible path dispatch goes through the
Phase-0 software model (:mod:`pyro._model`).  The v2.0.0 circuit model
(:mod:`pyro._circuit_model`) and this native binding are **not** on the dispatch
path in Phase 1 — both are exercised directly by the ABI-conformance /
harness-contract unit tests, not by ``re.search``/``finditer`` calls.

The library is not built by importing PYRO; it is produced by ``make lib``
(``build/libpyro_rt.so``).  :func:`load` raises :class:`NativeUnavailable`
with a
recorded reason if the ``.so`` is absent, so a test fixture can *skip with
reason*
rather than fail when a C toolchain is unavailable.

model:// binding descriptor (Task-7 artifact-format extension).  Because a C
library performs no regex classification, ``pyro_generate`` in the model binding
takes a **circuit descriptor** built by :func:`build_descriptor` from a
:class:`pyro.hdl.GeneratedCircuit` and its on-disk artifact directory, rather
than
raw regex text.  See ``src/pyro_rt.c`` for the wire layout.
"""

from __future__ import annotations

import ctypes
import os
import struct
from pathlib import Path
from typing import Optional

# --- pyro_status (R38) -----------------------------------------------------
PYRO_OK = 0
PYRO_E_UNSUPPORTED = 1
PYRO_E_CAPACITY = 2
PYRO_E_DEVICE = 3
PYRO_E_INVALID = 4
PYRO_E_NOMEM = 5
PYRO_E_TIMEOUT = 6
PYRO_E_NOT_RESIDENT = 7
PYRO_E_SYNTH = 8

# --- pyro_encoding (R38) ---------------------------------------------------
PYRO_ENC_BYTES = 0
PYRO_ENC_UTF8 = 1

# --- pyro_circ_status (R38) ------------------------------------------------
PYRO_CIRC_COLD = 0
PYRO_CIRC_SYNTHESIZING = 1
PYRO_CIRC_WARM = 2
PYRO_CIRC_RESIDENT = 3
PYRO_CIRC_FALLBACK = 4

# --- pyro_match.flags bits (R38/R47) ---------------------------------------
PYRO_MATCH_VERIFIED = 0x1
PYRO_MATCH_ZERO_WIDTH = 0x2

_DESC_MAGIC = b"PYRODSC1"


class NativeUnavailable(RuntimeError):
    """The native runtime could not be loaded (with a human-readable reason)."""


class PyroMatch(ctypes.Structure):
    """R38/R47 result-ring entry — 24 bytes, little-endian."""

    _fields_ = [
        ("start", ctypes.c_uint64),
        ("end", ctypes.c_uint64),
        ("pattern_id", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
    ]


class PyroCaps(ctypes.Structure):
    """R42 capability block."""

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


def library_path() -> Path:
    """Path where ``make lib`` writes the shared library."""
    override = os.environ.get("PYRO_NATIVE_LIB")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "build" / "libpyro_rt.so"


def _bind(lib: ctypes.CDLL) -> ctypes.CDLL:
    """Attach argtypes/restypes to the ABI entry points."""
    vp = ctypes.c_void_p
    u32 = ctypes.c_uint32
    u64 = ctypes.c_uint64
    sz = ctypes.c_size_t

    lib.pyro_abi_version.restype = u32
    lib.pyro_abi_version.argtypes = []

    lib.pyro_ctx_open.restype = ctypes.c_int
    lib.pyro_ctx_open.argtypes = [ctypes.POINTER(vp), ctypes.c_char_p]

    lib.pyro_ctx_close.restype = None
    lib.pyro_ctx_close.argtypes = [vp]

    lib.pyro_generate.restype = ctypes.c_int
    lib.pyro_generate.argtypes = [vp, ctypes.c_char_p, sz, u32, ctypes.c_int,
                                  ctypes.POINTER(vp)]

    lib.pyro_synth_request.restype = ctypes.c_int
    lib.pyro_synth_request.argtypes = [vp, vp]

    lib.pyro_circuit_status.restype = ctypes.c_int
    lib.pyro_circuit_status.argtypes = [vp, vp, ctypes.POINTER(ctypes.c_int)]

    lib.pyro_circuit_load.restype = ctypes.c_int
    lib.pyro_circuit_load.argtypes = [vp, vp]

    lib.pyro_circuit_free.restype = None
    lib.pyro_circuit_free.argtypes = [vp]

    lib.pyro_scan.restype = ctypes.c_int
    lib.pyro_scan.argtypes = [vp, vp, ctypes.c_char_p, sz, u64,
                              ctypes.POINTER(PyroMatch), sz,
                              ctypes.POINTER(sz)]

    lib.pyro_caps_get.restype = ctypes.c_int
    lib.pyro_caps_get.argtypes = [vp, ctypes.POINTER(PyroCaps)]

    lib.pyro_ctx_debug_force_misalign.restype = None
    lib.pyro_ctx_debug_force_misalign.argtypes = [vp, ctypes.c_int]

    lib.pyro_ctx_debug_csr_read.restype = u32
    lib.pyro_ctx_debug_csr_read.argtypes = [vp, u32]

    return lib


_CACHE: Optional[ctypes.CDLL] = None


def load() -> ctypes.CDLL:
    """Load ``libpyro_rt.so`` (cached), or raise :class:`NativeUnavailable`."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    path = library_path()
    if not path.is_file():
        raise NativeUnavailable(
            f"native library not built at {path}; run `make lib`")
    try:
        lib = ctypes.CDLL(str(path))
    except OSError as exc:  # pragma: no cover - platform loader failure
        raise NativeUnavailable(f"cannot dlopen {path}: {exc}") from exc
    _CACHE = _bind(lib)
    return _CACHE


def available() -> bool:
    try:
        load()
        return True
    except NativeUnavailable:
        return False


def build_descriptor(pattern_hash16: bytes, circ_flags: int, enc: int,
                     art_dir: os.PathLike) -> bytes:
    """Build the model:// circuit descriptor consumed by ``pyro_generate``.

    ``pattern_hash16`` is the 16-byte R47a identity the host will demand of the
    resident circuit (``GeneratedCircuit.pattern_hash16``); ``circ_flags`` is
    the
    packed ``CIRC_FLAGS`` word; ``art_dir`` is the directory holding the
    ``artifact.bin`` + ``manifest.json`` produced by the toolchain.
    """
    if len(pattern_hash16) != 16:
        raise ValueError("pattern_hash16 must be exactly 16 bytes")
    path = os.fspath(art_dir)
    pb = path.encode("utf-8") if isinstance(path, str) else bytes(path)
    return (_DESC_MAGIC + bytes(pattern_hash16)
            + struct.pack("<III", int(circ_flags) & 0xFFFFFFFF,
                          int(enc) & 0xFFFFFFFF, len(pb))
            + pb)


# --------------------------------------------------------------------------
# Thin object wrappers (convenience for callers / tests)
# --------------------------------------------------------------------------
class NativeCircuit:
    """Owns a ``pyro_circuit*`` handle (freed on :meth:`close`/GC)."""

    def __init__(self, ctx: "NativeCtx", handle: ctypes.c_void_p):
        self._ctx = ctx
        self._h = handle

    @property
    def handle(self) -> ctypes.c_void_p:
        return self._h

    def synth_request(self) -> int:
        return self._ctx.lib.pyro_synth_request(self._ctx.handle, self._h)

    def status(self) -> int:
        out = ctypes.c_int(PYRO_CIRC_FALLBACK)
        self._ctx.lib.pyro_circuit_status(self._ctx.handle, self._h,
                                          ctypes.byref(out))
        return out.value

    def load(self) -> int:
        return self._ctx.lib.pyro_circuit_load(self._ctx.handle, self._h)

    def scan(self, buf: bytes, start_off: int = 0, out_cap: int = 64):
        """Return ``(status, matches, overflowed)`` — a Python view of a
        scan."""
        arr = (PyroMatch * out_cap)()
        count = ctypes.c_size_t(0)
        rc = self._ctx.lib.pyro_scan(
            self._ctx.handle, self._h, bytes(buf), len(buf), int(start_off),
            arr, out_cap, ctypes.byref(count))
        n = count.value
        matches = [(arr[i].start, arr[i].end, arr[i].pattern_id, arr[i].flags)
                   for i in range(n)]
        return rc, matches

    def close(self) -> None:
        if self._h:
            self._ctx.lib.pyro_circuit_free(self._h)
            self._h = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class NativeCtx:
    """Owns a ``pyro_ctx*`` handle for a transport binding."""

    def __init__(self, transport_uri: str = "model://"):
        self.lib = load()
        handle = ctypes.c_void_p()
        rc = self.lib.pyro_ctx_open(ctypes.byref(handle),
                                    transport_uri.encode("utf-8"))
        if rc != PYRO_OK:
            raise OSError(f"pyro_ctx_open({transport_uri!r}) -> {rc}")
        self._h = handle

    @property
    def handle(self) -> ctypes.c_void_p:
        return self._h

    def generate(self, descriptor: bytes, flags: int = 0,
                 enc: int = PYRO_ENC_BYTES) -> NativeCircuit:
        out = ctypes.c_void_p()
        rc = self.lib.pyro_generate(
    self._h,
    bytes(descriptor),
    len(descriptor),
    int(flags),
    int(enc),
     ctypes.byref(out))
        if rc != PYRO_OK:
            raise OSError(f"pyro_generate -> {rc}")
        return NativeCircuit(self, out)

    def caps(self) -> PyroCaps:
        c = PyroCaps()
        self.lib.pyro_caps_get(self._h, ctypes.byref(c))
        return c

    def force_misalign(self, on: bool = True) -> None:
        self.lib.pyro_ctx_debug_force_misalign(self._h, 1 if on else 0)

    def csr_read(self, offset: int) -> int:
        return int(self.lib.pyro_ctx_debug_csr_read(self._h, int(offset)))

    def close(self) -> None:
        if self._h:
            self.lib.pyro_ctx_close(self._h)
            self._h = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
