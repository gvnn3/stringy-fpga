"""Public host-side device API — ``pyro.device`` (spec §10, R86).

This module is the PYRO-specific surface the coder and the test-developer each
derive independently from §10.1/§10.2 of the spec.  It provides four functions:

  * :func:`encode_frame` / :func:`decode_frame` — the byte-exact PYRO control
    header + payload codec of **R78** (R86.2/R86.3).  ``encode_frame`` builds the
    14-byte big-endian PYRO header (R78.3) plus payload — i.e. the Ethernet
    *payload*, frame offset 14 onward; the 14-byte Ethernet L2 header
    (``dst``/``src``/``0x88B5``), the FCS, and any 60-byte-minimum zero-padding
    are the transport/NIC's responsibility (R78.9) and are deliberately not this
    codec's concern (frame validity is MAC-independent, R78.10).
  * :func:`probe_device` — the R83/R84 device-usability probe (R86.4): an
    ``ID_REQUEST``/``ID_REPLY`` handshake on the ``onic`` netdev with the
    ``static_shell_id`` ``SPEC16`` check (R81), the ``CAP_NET_RAW`` gate (R83),
    and the canonical ``device_usable=false — …`` reason string.  It is
    **privilege-free**: when ``CAP_NET_RAW`` is absent it returns
    ``(False, reason)`` **without** attempting any privileged ``AF_PACKET``
    operation and never raises ``PermissionError`` (R86.4).
  * :func:`load_partial` — the R85 JTAG partial-bitstream load via Vivado
    ``hw_server`` batch Tcl (R86.5), raising :class:`PyroLoadError` on failure.

**Exception taxonomy (R86.1).** The only exceptions these functions raise are
:class:`PyroDeviceError` and its subclasses :class:`PyroFrameError` (bad frame)
and :class:`PyroLoadError` (JTAG/PR-load failure).  ``PermissionError``/
``OSError``/transport internals are wrapped or swallowed and MUST NOT leak.

**Config-in, no ad-hoc env reads (R5/R35a, R86).** Every environment-derived
value (netdev, expected ``PYRO_SHELL_SPEC16``, probe-timeout, Vivado/``hw_server``
locations) is carried on the passed :class:`DeviceConfig`; the functions never
read ``os.environ`` themselves.  **Spec-sanctioned private test seams (R86.6,
v2.2.2)** on the config
(``cap_check``, ``transport_factory``, ``load_runner``) let the no-privilege /
timeout / SPEC16-mismatch / load-failure paths be exercised without hardware;
each defaults to the real implementation, so ``DeviceConfig()`` is byte-identical
to production.  R86.6 blesses these as the specced (test-developer-usable)
injection points; they are explicit config injection, not ``pyro.testing``
ambient hooks, so they need no ``PYRO_ENABLE_TEST_HOOKS`` gate.

This namespace is PYRO-specific and is NOT patched onto the standard ``re``
module by :func:`pyro.install` (§7.2), mirroring ``explain``/``stats``/``testing``.
"""

from __future__ import annotations

import operator
import os
import shutil
import struct
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

__all__ = [
    "PyroDeviceError",
    "PyroFrameError",
    "PyroLoadError",
    "DeviceConfig",
    "DecodedFrame",
    "encode_frame",
    "decode_frame",
    "probe_device",
    "load_partial",
    "read_perf_counters",
    # message-kind constants (R78.4)
    "KIND_ID_REQUEST",
    "KIND_ID_REPLY",
    "KIND_MATCH_REQUEST",
    "KIND_MATCH_REPLY",
    "KIND_STATUS",
    "KIND_PERF_REQUEST",
    "KIND_PERF_REPLY",
]

# ---------------------------------------------------------------------------
# Protocol constants (R78)
# ---------------------------------------------------------------------------

#: EtherType for PYRO control frames (R78.1), big-endian on the wire (``88 B5``).
ETHERTYPE = 0x88B5
#: PYRO control-header sanity byte, ``'P'`` (R78.3).
MAGIC = 0x50
#: Protocol version this spec defines (R78.3).
VERSION = 0x01
#: Fixed PYRO control-header length in bytes (R78.3).
PYRO_HEADER_LEN = 14
#: Max PYRO payload length (R78.9): total frame ≤ 1518, header + payload ≤ 1500.
MAX_PAYLOAD = 1486

# Message kinds (R78.4).
KIND_RESERVED = 0x00
KIND_ID_REQUEST = 0x01
KIND_ID_REPLY = 0x02
KIND_MATCH_REQUEST = 0x03
KIND_MATCH_REPLY = 0x04
KIND_STATUS = 0x05  # STATUS/ERROR
KIND_PERF_REQUEST = 0x06  # R45a counter read-out (R78.11, v2.4.0)
KIND_PERF_REPLY = 0x07

#: Sendable/receivable message kinds (R78.4); ``0x00`` reserved is not a kind.
VALID_KINDS = frozenset({
    KIND_ID_REQUEST, KIND_ID_REPLY, KIND_MATCH_REQUEST,
    KIND_MATCH_REPLY, KIND_STATUS, KIND_PERF_REQUEST, KIND_PERF_REPLY,
})

#: Expected shell SPEC16 the host runtime is compiled with (R81): spec 2.2.
PYRO_SHELL_SPEC16 = 0x0202
#: R84 probe timeout per attempt (500 ms) and attempt count (3).
PYRO_PROBE_TIMEOUT = 0.5
PYRO_PROBE_ATTEMPTS = 3
#: R84 (v2.2.2) normative JTAG PR-load timeout (10 min) for the R85 load step.
#: A JTAG-load timeout is **transient** (maps to PYRO_E_NOT_RESIDENT / fallback,
#: R65), not a permanent-fallback synthesis failure.
PYRO_JTAG_LOAD_TIMEOUT = 600.0

#: R85a (v2.5.1) in-band recovery command run by ``load_partial`` after a
#: successful program step.  A raw JTAG reconfig leaves the child unreachable
#: (box-side AXIS desync + stuck QDMA C2H stream state); the recovery script
#: performs the mandated driver-detach -> user-box reset -> QDMA soft reset ->
#: driver re-attach sequence.  ``sudo -n`` so an absent sudoers grant fails
#: fast and loud instead of prompting inside a library call.  The script path
#: is a pinned location relative to this package (not filesystem scanning, R70).
PYRO_RECOVER_CMD = (
    "sudo", "-n",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "scripts", "pyro_wedge_recover.sh"),
)

# Linux capability bit for AF_PACKET raw sockets (R83 transport gate).
_CAP_NET_RAW = 13

# ``struct`` format for the 14-byte PYRO control header, all big-endian (R78.2/
# R78.3): magic(B) version(B) kind(B) flags(B) slot(H) seq(I) length(H) resv(H).
_HDR_FMT = ">BBBBHIHH"
assert struct.calcsize(_HDR_FMT) == PYRO_HEADER_LEN


# ---------------------------------------------------------------------------
# Exception taxonomy (R86.1)
# ---------------------------------------------------------------------------


class PyroDeviceError(Exception):
    """Base class for all ``pyro.device`` errors (R86.1)."""


class PyroFrameError(PyroDeviceError):
    """A malformed PYRO frame in :func:`encode_frame`/:func:`decode_frame` (R86.1)."""


class PyroLoadError(PyroDeviceError):
    """A JTAG / PR partial-bitstream load failure in :func:`load_partial` (R86.1)."""


# ---------------------------------------------------------------------------
# Frame codec (R78, R86.2/R86.3)
# ---------------------------------------------------------------------------


def encode_frame(kind: int, slot: int, seq: int, payload: bytes,
                 flags: int = 0) -> bytes:
    """Build the PYRO control header + payload (R78 offset 14 onward) (R86.2).

    Returns ``bytes`` = 14-byte big-endian PYRO header followed by ``payload``.
    The Ethernet L2 header, FCS, and 60-byte-minimum zero-padding are the
    transport's concern (R78.9) and are not produced here.

    Raises :class:`PyroFrameError` if ``len(payload) > 1486`` (R78.9), if ``kind``
    is not an R78.4 message kind, if ``flags != 0`` (reserved, MUST be 0 in
    version 1), or if ``slot``/``seq`` are out of their unsigned field ranges.
    """
    # B1/M2: reject a non-bytes-like payload BEFORE any conversion.  ``bytes(7)``
    # would silently fabricate a 7-byte zero payload, and ``bytes(2**33)`` would
    # allocate an attacker-sized buffer — both must be a taxonomy error, not a
    # valid frame or a MemoryError.
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise PyroFrameError(
            f"payload must be bytes-like, not {type(payload).__name__} (R86.2)")
    # B2: coerce the integer header fields via operator.index so any non-integer
    # (str, float, None) becomes a PyroFrameError rather than leaking a bare
    # TypeError from a comparison or a struct.error from struct.pack (R86.1).
    kind = _index_field("kind", kind)
    slot = _index_field("slot", slot)
    seq = _index_field("seq", seq)
    flags = _index_field("flags", flags)
    if flags != 0:
        raise PyroFrameError(
            f"flags must be 0 in protocol version 1 (got {flags!r}) (R78.3/R86.2)")
    if kind not in VALID_KINDS:
        raise PyroFrameError(f"invalid message kind 0x{_as_byte(kind):02x} (R78.4)")
    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD:
        raise PyroFrameError(
            f"payload length {len(payload)} exceeds MTU bound {MAX_PAYLOAD} (R78.9)")
    if not (0 <= slot <= 0xFFFF):
        raise PyroFrameError(f"slot {slot!r} out of 16-bit range (R78.3)")
    if not (0 <= seq <= 0xFFFFFFFF):
        raise PyroFrameError(f"seq {seq!r} out of 32-bit range (R78.3)")
    header = struct.pack(_HDR_FMT, MAGIC, VERSION, kind, flags,
                         slot, seq, len(payload), 0)
    return header + payload


def _index_field(name: str, value) -> int:
    """Coerce a header field to ``int`` via ``operator.index`` (``bool`` is an
    ``int`` subclass and passes), mapping any non-integer to
    :class:`PyroFrameError` so the R86.1 taxonomy is never bypassed by a bare
    ``TypeError``/``struct.error``."""
    try:
        return operator.index(value)
    except TypeError as exc:
        raise PyroFrameError(
            f"{name} must be an integer, not {type(value).__name__} (R86.2)") from exc


@dataclass(frozen=True)
class DecodedFrame:
    """A decoded PYRO control header + payload (R86.3).

    Field names mirror the R78.3 header exactly.  ``payload`` is the raw
    kind-specific body ``bytes``; typed sub-parsing of ``ID_REPLY``/``MATCH_REPLY``
    is offered separately (not required of :func:`decode_frame`, R86.3).
    """

    magic: int
    version: int
    kind: int
    flags: int
    slot: int
    seq: int
    length: int
    payload: bytes


def decode_frame(data: bytes) -> DecodedFrame:
    """Parse the PYRO control header + payload (R78) into a :class:`DecodedFrame`.

    ``data`` is the same slice :func:`encode_frame` returns (frame offset 14
    onward), optionally followed by Ethernet zero-padding, which is ignored
    (R78.9).

    Raises :class:`PyroFrameError` if the header is truncated, if ``magic`` !=
    ``0x50``, if ``version`` != ``0x01``, if ``flags`` != 0, if ``length``
    exceeds the R78.9 bound (1486), or if fewer than ``length`` payload bytes are
    available (R86.3).
    """
    # B2/M2: reject non-bytes-like input BEFORE any conversion.  ``bytes(None)`` /
    # ``bytes("xyz")`` raise TypeError (taxonomy leak) and ``bytes(2**33)`` would
    # allocate an 8 GiB buffer before any bounds check — guard first.
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise PyroFrameError(
            f"frame data must be bytes-like, not {type(data).__name__} (R86.3)")
    data = bytes(data)
    if len(data) < PYRO_HEADER_LEN:
        raise PyroFrameError(
            f"truncated PYRO header: {len(data)} < {PYRO_HEADER_LEN} bytes (R78.3)")
    magic, version, kind, flags, slot, seq, length, reserved = struct.unpack(
        _HDR_FMT, data[:PYRO_HEADER_LEN])
    if magic != MAGIC:
        raise PyroFrameError(f"bad magic 0x{magic:02x} (expected 0x50) (R78.3)")
    if version != VERSION:
        raise PyroFrameError(
            f"unsupported protocol version 0x{version:02x} (expected 0x01) (R78.3)")
    if flags != 0:
        raise PyroFrameError(
            f"reserved flags must be 0 in version 1 (got 0x{flags:02x}) (R78.3)")
    if length > MAX_PAYLOAD:
        raise PyroFrameError(
            f"length {length} exceeds MTU bound {MAX_PAYLOAD} (R78.9)")
    avail = len(data) - PYRO_HEADER_LEN
    if avail < length:
        raise PyroFrameError(
            f"length {length} inconsistent with {avail} available payload bytes "
            f"(R78.9/R86.3)")
    payload = data[PYRO_HEADER_LEN:PYRO_HEADER_LEN + length]  # trailing pad ignored
    return DecodedFrame(magic=magic, version=version, kind=kind, flags=flags,
                        slot=slot, seq=seq, length=length, payload=payload)


def _as_byte(value: int) -> int:
    """Best-effort clamp of a kind value for diagnostics (never raises)."""
    try:
        return int(value) & 0xFF
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Device configuration (R86: config-in, no ad-hoc env reads)
# ---------------------------------------------------------------------------


def _sampled_iface() -> Optional[str]:
    """The sampled PYRO_DEVICE_IFACE (R68), else ``None`` — **no spec default,
    fail-closed** (F3/R68 no-default rule, v2.5.0): the netdev name is host
    configuration, not derivable from any spec fact, so nothing is guessed and
    nothing is scanned (R70).  Reads :mod:`pyro._route`'s cached R35a snapshot,
    NOT ``os.environ`` (R86/R5)."""
    try:
        from . import _route
        return _route.device_iface()
    except Exception:
        return None


def _sampled_hw_server() -> str:
    """The sampled PYRO_HW_SERVER (R68), else the spec default.  Reads
    :mod:`pyro._route`'s cached R35a snapshot, NOT ``os.environ`` (R86/R5)."""
    try:
        from . import _route
        return _route.hw_server_url()
    except Exception:
        return "TCP:localhost:3121"


def _default_vivado_dir() -> str:
    """The R70a-pin PINNED_VIVADO_DIR default for locating the pinned
    ``hw_server``/``vivado`` used by :func:`load_partial` (R86.6)."""
    from .synth.toolchain import PINNED_VIVADO_DIR
    return PINNED_VIVADO_DIR


@dataclass(frozen=True)
class DeviceConfig:
    """Config carried into the R86 device functions (R86.6, config-in discipline).

    Every field is defaulted from a spec constant so ``DeviceConfig()`` is fully
    usable with no introspection; the functions never read ``os.environ``.  Only
    ``iface``/``hw_server`` derive from env knobs (PYRO_DEVICE_IFACE /
    PYRO_HW_SERVER, R68) — via :mod:`pyro._route`'s R35a-sampled snapshot at
    construction, not an ad-hoc environment read.  From v2.5.0 ``iface`` is the
    deliberate exception to full usability (F3/R68 no-default rule): it has **no
    spec constant to default from** and is ``None`` when PYRO_DEVICE_IFACE is
    unset — fail-closed, :func:`probe_device` then returns ``(False, <R83
    canonical reason>)`` naming ``transport: PYRO_DEVICE_IFACE not configured``
    (never raises, never guesses, never scans — R70).  Every other field is a
    spec-fixed constant (SPEC16 per R81, R84 timeouts, R70a-pin Vivado) a test
    MAY override on the config.

    **Spec-sanctioned private test seams (R86.6, v2.2.2).** The three
    ``Optional[Callable]`` seam fields are **blessed by R86.6** as the specced way
    to drive the no-privilege / timeout / SPEC16-mismatch / load-failure paths of
    AC-2b-1..2b-3 without hardware.  Each defaults to ``None`` => the real
    production implementation, so a ``DeviceConfig()`` with no overrides is
    byte-identical to production behavior.  They need no ``PYRO_ENABLE_TEST_HOOKS``
    gate: passing a non-default value is explicit config injection, not an ambient
    process-wide hook, so there is no production foot-gun (R86.6).  The seam
    callables' contracts are normative in **R86.7** (v2.2.3): ``transport_factory``
    returns an R86.7 ``Transport`` (full-Ethernet-frame ``send``/``recv``-``None``-
    sentinel/idempotent ``close``); ``load_runner`` returns ``(rc: int, output:
    str)`` with nonzero ``rc`` → :class:`PyroLoadError`.
    """

    # -- probe (R83/R84); iface from PYRO_DEVICE_IFACE (R68) -----------------
    # onic netdev (F3); None = unconfigured => fail-closed (R68 v2.5.0)
    iface: Optional[str] = field(default_factory=_sampled_iface)
    expected_spec16: int = PYRO_SHELL_SPEC16             # expected SPEC16 (R81)
    probe_timeout_s: float = PYRO_PROBE_TIMEOUT          # per attempt (R84)
    probe_attempts: int = PYRO_PROBE_ATTEMPTS            # attempt count (R84)
    src_mac: bytes = b"\x02\x00\x00\x00\x00\x01"         # example LA host MAC (R78.10)
    dst_mac: bytes = b"\xff\xff\xff\xff\xff\xff"          # broadcast (MAC-independent)
    # -- JTAG load (R85/R86.5); hw_server from PYRO_HW_SERVER (R68) ----------
    hw_server: str = field(default_factory=_sampled_hw_server)  # Vivado hw_server URL
    vivado_dir: str = field(default_factory=_default_vivado_dir)  # PINNED_VIVADO_DIR
    jtag_load_timeout_s: float = PYRO_JTAG_LOAD_TIMEOUT   # R84 (v2.2.2), transient
    # R85a (v2.5.1) post-program in-band recovery; None disables (R86.5/R86.6)
    recover_cmd: Optional[Tuple[str, ...]] = PYRO_RECOVER_CMD
    # -- spec-sanctioned private test seams (R86.6; None => real impl) -------
    cap_check: Optional[Callable[[], bool]] = None
    transport_factory: Optional[Callable[["DeviceConfig"], "_Transport"]] = None
    load_runner: Optional[Callable[[list, str, float], Tuple[int, str]]] = None


# ---------------------------------------------------------------------------
# probe_device (R86.4 / R83 / R84 / R81)
# ---------------------------------------------------------------------------

# R83 canonical unmet-condition clauses, in the fixed enumeration order
# (v2.5.0/A4: the unconfigured-interface condition is condition 1 — you cannot
# probe an interface you do not have — renumbering the previous two to 2 and 3).
_REASON_IFACE = "transport: PYRO_DEVICE_IFACE not configured"
_REASON_PROBE = (
    "probe: no valid ID_REPLY (no reply within PYRO_PROBE_TIMEOUT, "
    "or static_shell_id SPEC16 mismatch)")
_REASON_TRANSPORT = "transport: CAP_NET_RAW absent"


def probe_device(config: DeviceConfig) -> Tuple[bool, str]:
    """Probe device usability (R86.4) — returns ``(usable, reason)``, never raises
    for any "not usable" condition.

    Order (R86.4 privilege-free guarantee): the ``CAP_NET_RAW`` gate (R83) is
    checked **first**, privilege-free, so that when the capability is absent this
    returns ``(False, "device_usable=false — …transport: CAP_NET_RAW absent")``
    **without** attempting any privileged ``AF_PACKET`` operation and never raises
    ``PermissionError``.  When the capability is present, a live ``ID_REQUEST`` /
    ``ID_REPLY`` handshake (up to ``probe_attempts`` × ``probe_timeout_s``, R84)
    validates the ``static_shell_id`` ``SPEC16`` (R81).

    **Fail-closed on an unconfigured netdev (F3/R68 no-default rule, v2.5.0).**
    When ``config.iface`` is ``None`` (PYRO_DEVICE_IFACE unset and no caller-
    supplied value) and no ``transport_factory`` seam supplies the transport,
    the ``AF_PACKET`` binding has no interface: no probe is attempted (a probe
    that never ran is not enumerated as a probe failure) and the reason names
    ``transport: PYRO_DEVICE_IFACE not configured`` as the **first** R83
    condition.  Never raises, never guesses a name, never scans the system for
    candidate interfaces (R70).  An injected ``transport_factory`` (R86.6
    explicit config injection) needs no netdev, so the seam paths are unaffected.

    Returns ``(True, reason)`` only when **all** conditions hold; otherwise
    ``(False, <R83 canonical enumeration>)``.  May raise :class:`PyroFrameError`
    only on a genuinely malformed reply frame (R86.4), which the caller treats as
    not-usable.
    """
    cap = config.cap_check() if config.cap_check is not None else _has_cap_net_raw()
    # R68 (v2.5.0) fail-closed gate: the real AF_PACKET transport needs a
    # configured netdev; an injected transport (R86.6 seam) does not.
    iface_missing = config.iface is None and config.transport_factory is None

    static_shell_id: Optional[int] = None
    if cap and not iface_missing:
        # CAP_NET_RAW present: attempt the live probe.  OSError from the raw
        # socket is contained (R86.1 — no OSError leakage); a malformed reply
        # surfaces as PyroFrameError (R86.4, permitted).
        try:
            static_shell_id = _live_probe(config)
        except PyroFrameError:
            raise
        except OSError:
            static_shell_id = None

    probe_ok = (static_shell_id is not None
                and (static_shell_id >> 16) == (config.expected_spec16 & 0xFFFF))

    if cap and probe_ok:
        return (True,
                f"device_usable=true — static_shell_id=0x{static_shell_id:08x}, "
                f"transport: CAP_NET_RAW present")

    # R83 canonical enumeration of exactly the unmet conditions, in fixed order
    # (1. iface not configured, 2. no valid ID_REPLY, 3. CAP_NET_RAW absent).
    # When the iface is unconfigured no probe ran, so condition 2 — which would
    # report a probe failure for a probe that never happened — is not claimed.
    unmet = []
    if iface_missing:
        unmet.append(_REASON_IFACE)
    elif not probe_ok:
        unmet.append(_REASON_PROBE)
    if not cap:
        unmet.append(_REASON_TRANSPORT)
    return (False, "device_usable=false — " + ", ".join(unmet))


def _has_cap_net_raw() -> bool:
    """Return whether the current process holds ``CAP_NET_RAW`` in its effective
    set — privilege-free, never raises (R86.4).

    Reads the effective-capability bitmask from ``/proc/self/status`` (which a
    process may always read about itself; this correctly reports ``True`` for
    root, whose ``CapEff`` is full).  Any read/parse failure is treated as the
    capability being absent (fail-safe: not usable, but never a raise).
    """
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("CapEff:"):
                    caps = int(line.split()[1], 16)
                    return bool(caps & (1 << _CAP_NET_RAW))
    except (OSError, ValueError, IndexError):
        return False
    return False


def _live_probe(config: DeviceConfig) -> Optional[int]:
    """Send ``ID_REQUEST`` and return the reply's ``static_shell_id`` (int), or
    ``None`` if no valid ``ID_REPLY`` arrives within the R84 budget.

    Precondition: ``CAP_NET_RAW`` is present (probe_device gates on it).  Up to
    ``probe_attempts`` attempts, each with a fresh ``seq`` and a
    ``probe_timeout_s`` receive window (R84).  Malformed reply => PyroFrameError.
    """
    transport = (config.transport_factory(config)
                 if config.transport_factory is not None
                 else _EthTransport(config))
    try:
        eth_hdr = (bytes(config.dst_mac) + bytes(config.src_mac)
                   + struct.pack(">H", ETHERTYPE))
        for attempt in range(max(1, int(config.probe_attempts))):
            seq = attempt + 1
            frame = eth_hdr + encode_frame(KIND_ID_REQUEST, 0, seq, b"")
            transport.send(frame)
            deadline = time.monotonic() + float(config.probe_timeout_s)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break  # this attempt timed out; try the next seq
                reply = transport.recv(remaining)
                if reply is None:
                    break
                try:
                    sid = _parse_id_reply(reply, seq)
                except PyroFrameError:
                    # N1/R86.4: a stray or garbled 0x88B5 broadcast is *not* our
                    # reply — keep listening within this attempt's R84 window
                    # rather than unwinding the whole probe.  (R86.4 permits
                    # raising only on a genuinely malformed reply; the safer,
                    # explicitly-blessed "return (False, reason)" disposition is
                    # preferred here, so a malformed reply-to-our-seq also just
                    # ends the attempt.)
                    continue
                if sid is not None:
                    return sid
        return None
    finally:
        transport.close()


def _parse_id_reply(frame: bytes, expect_seq: int) -> Optional[int]:
    """Extract ``static_shell_id`` from a received Ethernet frame if it is a
    matching PYRO ``ID_REPLY``; else ``None`` (ignore).  Malformed ID_REPLY
    payloads raise :class:`PyroFrameError` (R86.4)."""
    frame = bytes(frame)
    if len(frame) < 14 + PYRO_HEADER_LEN:
        return None
    ethertype = struct.unpack(">H", frame[12:14])[0]
    if ethertype != ETHERTYPE:
        return None  # not a PYRO frame (R78.1) — ignore
    dec = decode_frame(frame[14:])  # PyroFrameError on genuinely malformed header
    if dec.kind != KIND_ID_REPLY or dec.seq != expect_seq:
        return None  # not the reply we're waiting for — keep listening
    if dec.length < 12:
        raise PyroFrameError(
            f"malformed ID_REPLY: payload length {dec.length} < 12 (R78.5)")
    return struct.unpack(">I", dec.payload[0:4])[0]  # static_shell_id (R78.5)


def read_perf_counters(config: DeviceConfig,
                       slot: int = 1) -> Optional[Tuple[int, int]]:
    """Read the resident circuit's R45a counters via ``PERF_REQUEST`` (R78.11).

    Sends ``PERF_REQUEST`` for ``slot`` (up to ``probe_attempts`` attempts,
    ``probe_timeout_s`` each — the R84 budget, same shape as the probe) and
    returns ``(cycles, bytes)`` from the ``PERF_REPLY``.

    Returns ``None`` when no ``PERF_REPLY`` arrives — the R78.11 **"counters
    unavailable"** disposition, which is the *expected* outcome against a child
    built before v2.4.0 (including the flashed default ID stub), which drops the
    unknown kind per R78.4.  A ``STATUS``/``ERROR`` reply (non-resident slot,
    R78.11) also yields ``None``.  Never a device fault: callers MUST NOT route
    the scan path to fallback because of a ``None`` here.

    Precondition: ``CAP_NET_RAW`` (as for the live probe); ``OSError`` is
    contained to ``None`` (R86.1).  Raises :class:`PyroFrameError` only on a
    genuinely malformed ``PERF_REPLY`` to our ``seq`` (short payload).

    Fail-closed on an unconfigured netdev (F3/R68 no-default rule, v2.5.0):
    with ``config.iface is None`` and no ``transport_factory`` seam there is no
    interface to bind, so this returns ``None`` (counters unavailable) — never
    a raise, a guess, or a scan (R70).
    """
    if config.transport_factory is None and config.iface is None:
        return None  # R68 fail-closed: no netdev configured (v2.5.0)
    try:
        transport = (config.transport_factory(config)
                     if config.transport_factory is not None
                     else _EthTransport(config))
    except OSError:
        return None
    try:
        eth_hdr = (bytes(config.dst_mac) + bytes(config.src_mac)
                   + struct.pack(">H", ETHERTYPE))
        for attempt in range(max(1, int(config.probe_attempts))):
            seq = attempt + 1
            frame = eth_hdr + encode_frame(KIND_PERF_REQUEST, slot, seq, b"")
            try:
                transport.send(frame)
            except OSError:
                return None
            deadline = time.monotonic() + float(config.probe_timeout_s)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break  # this attempt timed out; try the next seq
                try:
                    reply = transport.recv(remaining)
                except OSError:
                    return None
                if reply is None:
                    break
                reply = bytes(reply)
                if (len(reply) < 14 + PYRO_HEADER_LEN
                        or struct.unpack(">H", reply[12:14])[0] != ETHERTYPE):
                    continue  # not a PYRO frame (R78.1) — ignore
                try:
                    dec = decode_frame(reply[14:])
                except PyroFrameError:
                    continue  # stray/garbled 0x88B5 frame — keep listening (N1)
                if dec.seq != seq:
                    continue  # not the reply we're waiting for
                if dec.kind == KIND_STATUS:
                    return None  # non-resident slot (R78.11) — unavailable
                if dec.kind != KIND_PERF_REPLY:
                    continue
                if dec.length < 16:
                    raise PyroFrameError(
                        f"malformed PERF_REPLY: payload length {dec.length} "
                        f"< 16 (R78.11)")
                cycles, nbytes = struct.unpack(">QQ", dec.payload[0:16])
                return (cycles, nbytes)
        return None
    finally:
        transport.close()


class _Transport:
    """The R86.7 ``Transport`` protocol (v2.2.3) — what ``transport_factory``
    returns.  It carries **full Ethernet frames including the 14-byte L2 header**
    (``dst/src/0x88B5``), so the transport is the layer that prepends/strips the
    L2 header around the R86.2/R86.3 PYRO portion and owns the 60-byte-minimum
    zero-padding (R78.9).

      * ``send(frame: bytes) -> None`` — transmit one complete Ethernet frame.
      * ``recv(timeout: float) -> bytes | None`` — the next complete Ethernet
        frame, or ``None`` if none arrives within ``timeout`` seconds (the
        ``None`` sentinel means *no-frame-within-timeout*, distinct from an empty
        frame; a ``None`` at each attempt drives the ``probe: no valid ID_REPLY``
        disposition, R84/R86.4).
      * ``close() -> None`` — release the transport; **idempotent**.
    """

    def send(self, frame: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def recv(self, timeout: float) -> Optional[bytes]:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class _EthTransport(_Transport):
    """Real ``AF_PACKET`` raw-socket transport on the ``onic`` netdev (R78/R83),
    conforming to the R86.7 ``Transport`` protocol.

    Constructed only after the ``CAP_NET_RAW`` gate passes; still fully contained
    so no ``OSError`` leaks (probe_device swallows OSError into "no reply").
    """

    def __init__(self, config: DeviceConfig):
        import socket  # local import: keeps package import off the socket module
        if config.iface is None:
            # R68 no-default rule (v2.5.0): no configured netdev to bind.
            # OSError keeps the R86.1 containment contract at every call site
            # (probe/perf swallow OSError into their (False, reason)/None paths)
            # instead of leaking a bind() TypeError.
            raise OSError("PYRO_DEVICE_IFACE not configured (R68, fail-closed)")
        self._socket_mod = socket
        self._sock = socket.socket(
            socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETHERTYPE))
        try:
            self._sock.bind((config.iface, ETHERTYPE))
        except OSError:
            self._sock.close()
            raise

    def send(self, frame: bytes) -> None:
        # R78.9: the SENDER zero-pads to the 60-byte Ethernet L2 minimum
        # (excluding FCS).  The PYRO `length` field alone delimits the real
        # payload, so the receiver ignores the trailing pad.
        frame = bytes(frame)
        if len(frame) < 60:
            frame = frame + b"\x00" * (60 - len(frame))
        self._sock.send(frame)

    def recv(self, timeout: float) -> Optional[bytes]:
        # R86.7: return the next complete Ethernet frame, or None if none arrives
        # within `timeout` (the None sentinel = no-frame-within-timeout).
        import select
        r, _, _ = select.select([self._sock], [], [], max(0.0, float(timeout)))
        if not r:
            return None
        return self._sock.recv(2048)

    def close(self) -> None:
        # R86.7: idempotent — socket.close() on an already-closed socket is a
        # no-op, so repeated calls neither raise nor double-free.
        try:
            self._sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# load_partial (R86.5 / R85)
# ---------------------------------------------------------------------------

# R85 JTAG partial-bitstream program via Vivado hw_server batch Tcl
# (open_hw_manager / connect_hw_server / program the partial bitstream).  Braces
# are literal Tcl, so substitute with str.replace (NOT str.format), matching the
# convention in pyro.synth.toolchain._FLOW_TCL.
_JTAG_LOAD_TCL = (
    "open_hw_manager\n"
    "connect_hw_server -url @URL@\n"
    "set _t [lindex [get_hw_targets] 0]\n"
    "current_hw_target $_t\n"
    "open_hw_target\n"
    "set _d [lindex [get_hw_devices] 0]\n"
    "current_hw_device $_d\n"
    "set_property PROGRAM.FILE {@BIT@} $_d\n"
    "program_hw_devices $_d\n"
    "close_hw_target\n"
    "disconnect_hw_server\n"
    "close_hw_manager\n"
)


class _LoadTimeout(Exception):
    """Internal: the JTAG program subprocess overran its deadline."""


def load_partial(config: DeviceConfig, partial_bitstream_path) -> None:
    """Load a partial bitstream into ``pyro_rp`` over JTAG via ``hw_server`` (R86.5).

    Runs a Vivado batch Tcl (``open_hw_manager`` / ``connect_hw_server`` /
    ``program_hw_devices``, R85).  Returns ``None`` on success; on any failure
    (``hw_server`` unreachable, JTAG chain mismatch, incompatible/corrupt
    bitstream, load error, timeout) raises :class:`PyroLoadError` with a
    diagnostic (R86.5).  ``PermissionError``/``OSError`` never leak (R86.1).

    R85: this uses **JTAG only** and does **not** disturb the live PCIe link — the
    static shell owns PCIe and is bit-identical across configurations.  R85a
    (v2.5.1): a raw JTAG program leaves the child **unreachable** (no decoupler;
    stuck QDMA C2H stream state), so after a successful program step this runs the
    in-band recovery command (``config.recover_cmd``, default ``PYRO_RECOVER_CMD``
    = ``sudo -n scripts/pyro_wedge_recover.sh``) through the same ``load_runner``
    seam; the load succeeds only if both steps do.  Recovery transiently detaches
    the ``onic`` driver, so the netdev's MAC changes across a load.  Artifact
    admissibility (``payload_kind == "pr_bitstream"``, same-release, integrity —
    R72b/R82) is enforced by ``pyro_circuit_load`` upstream (R40); this function
    performs the JTAG mechanism and surfaces mechanism failures as PyroLoadError.
    """
    try:
        path = os.fspath(partial_bitstream_path)
    except TypeError as exc:
        raise PyroLoadError(f"invalid bitstream path: {exc}") from exc
    if not os.path.isfile(path):
        raise PyroLoadError(f"partial bitstream not found: {path!r} (R86.5)")

    vivado = _resolve_vivado(config)
    tcl = (_JTAG_LOAD_TCL
           .replace("@URL@", str(config.hw_server))
           .replace("@BIT@", os.path.abspath(path)))

    # B3: the workdir create + Tcl write are filesystem ops that can raise
    # OSError; map them to PyroLoadError so no raw OSError leaks (R86.1).
    try:
        workdir = tempfile.mkdtemp(prefix="pyro_jtag_")
    except OSError as exc:
        raise PyroLoadError(
            f"JTAG load could not create work dir: {exc} (R86.5)") from exc
    try:
        script = os.path.join(workdir, "load.tcl")
        try:
            with open(script, "w") as f:
                f.write(tcl)
        except OSError as exc:
            raise PyroLoadError(
                f"JTAG load could not write Tcl script: {exc} (R86.5)") from exc
        cmd = [vivado, "-mode", "batch", "-source", script,
               "-nojournal", "-log", os.path.join(workdir, "jtag.log")]
        runner = (config.load_runner if config.load_runner is not None
                  else _default_load_runner)
        try:
            rc, out = runner(cmd, workdir, float(config.jtag_load_timeout_s))
        except _LoadTimeout:
            # R84 (v2.2.2): a JTAG-load timeout is TRANSIENT — it maps to
            # PYRO_E_NOT_RESIDENT/fallback (R65) upstream, not permanent fallback.
            raise PyroLoadError(
                f"JTAG hw_server load exceeded {config.jtag_load_timeout_s:g}s; "
                f"process tree killed (R84/R85/R86.5)")
        except PyroLoadError:
            raise
        except OSError as exc:
            raise PyroLoadError(
                f"JTAG hw_server load could not launch vivado: {exc} (R86.5)"
            ) from exc
        except Exception as exc:
            # N2: KeyboardInterrupt / SystemExit are BaseException (not Exception)
            # and deliberately propagate untouched so an operator interrupt is
            # never masked as a load failure (mirrors toolchain.py's convention).
            raise PyroLoadError(
                f"JTAG hw_server load failed: {exc!r} (R86.5)") from exc
        if rc != 0:
            tail = _tail(out)
            raise PyroLoadError(
                f"JTAG hw_server load failed (vivado exit {rc}) (R86.5)"
                + (f": {tail}" if tail else ""))

        # R85a (v2.5.1): a raw JTAG program leaves the child unreachable (AXIS
        # desync + stuck QDMA C2H state) — the load is complete only after the
        # in-band recovery sequence.  Same runner seam, same timeout bound; a
        # failure here is TRANSIENT per R85a (the fabric holds the new child;
        # only reachability failed).  recover_cmd=None disables (R86.5/R86.6).
        if config.recover_cmd is not None:
            try:
                rc, out = runner(list(config.recover_cmd), workdir,
                                 float(config.jtag_load_timeout_s))
            except _LoadTimeout:
                raise PyroLoadError(
                    f"R85a post-load recovery exceeded "
                    f"{config.jtag_load_timeout_s:g}s; process tree killed "
                    f"(R85a/R86.5)")
            except PyroLoadError:
                raise
            except Exception as exc:
                raise PyroLoadError(
                    f"R85a post-load recovery failed: {exc!r} (R85a/R86.5)"
                ) from exc
            if rc != 0:
                tail = _tail(out)
                raise PyroLoadError(
                    f"R85a post-load recovery failed (exit {rc}) — the fabric "
                    f"holds the new child but it is unreachable until recovery "
                    f"succeeds (R85a/R86.5)"
                    + (f": {tail}" if tail else ""))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return None


def _resolve_vivado(config: DeviceConfig) -> str:
    """Resolve the ``vivado`` executable for the JTAG batch flow, else raise
    :class:`PyroLoadError`.  Falls back to the R70a-pin PINNED_VIVADO_DIR when the
    config does not name an install dir (the pinned location is not filesystem
    scanning, R70)."""
    from .synth.toolchain import PINNED_VIVADO_DIR
    d = config.vivado_dir or PINNED_VIVADO_DIR
    exe = os.path.join(d, "bin", "vivado")
    if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
        raise PyroLoadError(
            f"no runnable vivado for JTAG load at {exe!r} (R86.5)")
    return exe


def _default_load_runner(cmd: list, cwd: str, timeout: float) -> Tuple[int, str]:
    """The real R86.7 ``load_runner``: run the JTAG batch command with an R77-style
    process-tree kill on timeout.

    Returns ``(rc: int, output: str)`` — the process exit code and combined tool
    log (R86.7).  ``rc != 0`` is mapped to :class:`PyroLoadError` by the caller
    (``load_partial``), with ``output`` in the diagnostic.  Raises
    :class:`_LoadTimeout` on deadline expiry (after killing the whole process
    group; the timeout need not surface as an ``rc``, R86.7), or ``OSError`` if the
    process cannot be launched (mapped to PyroLoadError by the caller)."""
    import signal
    import subprocess

    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc, signal)   # R77-style: kill the whole tree
        try:
            proc.communicate(timeout=30)
        except (subprocess.SubprocessError, OSError):
            pass
        raise _LoadTimeout()
    return proc.returncode, out or ""


def _kill_process_tree(proc, signal) -> None:
    """SIGKILL the process group led by ``proc`` (start_new_session made it a
    group leader), matching the R77 kill discipline.  Best-effort."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _tail(text: str, limit: int = 400) -> str:
    """Last ``limit`` chars of ``text`` (diagnostic context), single-lined."""
    if not text:
        return ""
    t = text.strip().replace("\n", " ")
    return t[-limit:]
