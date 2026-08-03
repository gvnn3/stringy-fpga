"""AC-2b-1/2b-2/2b-3 fault paths driven by the R86.6 spec-sanctioned
DeviceConfig
test seams (cap_check / transport_factory / load_runner).

R86.6 (v2.2.2) blesses three defaulted `DeviceConfig` seam fields as
**normative** test injection points that need no PYRO_ENABLE_TEST_HOOKS gate:
  * `cap_check: Callable[[], bool]`     — overrides CAP_NET_RAW detection
  (R86.4);
  * `transport_factory: Callable[[DeviceConfig], Transport]` — a fake frame
    transport (drives timeout / SPEC16-mismatch / stray-frame / valid-reply,
    R86.4);
  * `load_runner: Callable[[list, str, float], (int, str)]` — a fake JTAG runner
    (drives PyroLoadError / timeout without hw_server, R86.5).

These make the no-privilege / timeout / SPEC16-mismatch / load-failure paths of
AC-2b-1..2b-3 testable WITHOUT hardware and deterministically
(host-independent).

Also covers R68 (v2.2.2): the PYRO_DEVICE_IFACE / PYRO_HW_SERVER env knobs
seed the
DeviceConfig `iface` / `hw_server` defaults from the R35a-sampled snapshot,
while
the remaining device parameters are spec constants (not env knobs).

Coverage: AC-2b-2, AC-2b-3.  Requirements: R68, R78, R81, R83, R84, R86.1,
R86.4,
R86.5, R86.6.

--- Spec note (Transport / load_runner callable contracts)
----------------------
R86.6 names the seams and their Python signatures but the spec text does not
spell
out the fake Transport's method contract.  Bound here through the sanctioned
seam's
observable behavior (black-box via the public injection point, no source
read): a
Transport is `send(frame: bytes) -> None`, `recv(timeout: float) -> bytes |
None`
(None = no frame within the per-attempt deadline), `close() -> None`; frames are
FULL Ethernet frames (14B L2 header + PYRO header + payload).  load_runner is
`(args: list, bitstream_path: str, timeout: float) -> (returncode: int,
output: str)`; a nonzero rc, a raised exception, or a subprocess timeout each
map to
PyroLoadError.  Surfaced for the spec-writer in case an explicit protocol is
wanted.
"""
import subprocess

import pytest

import pyro
import pyro.device as pdev
import phase2_support


# --------------------------------------------------------------------------
# Canonical R83/R86.4 reason literals.
# --------------------------------------------------------------------------
CANON_PROBE = (
    "device_usable=false — probe: no valid ID_REPLY (no reply within "
    "PYRO_PROBE_TIMEOUT, or static_shell_id SPEC16 mismatch)")
CANON_TWO_CONDITION = CANON_PROBE + ", transport: CAP_NET_RAW absent"
# v2.5.0 (A4/R68/R83): condition 1 of the renumbered canonical enumeration —
# emitted first, fail-closed, when no netdev is configured.
CANON_IFACE_UNSET = ("device_usable=false — transport:"
                     " PYRO_DEVICE_IFACE not configured")


# --------------------------------------------------------------------------
# Fake frame transport (bound to the R86.6 seam's observable contract).
# --------------------------------------------------------------------------
# R78.10 example locally-administered MACs (frame validity is MAC-independent).
_ETH = b"\x02\x00\x00\x00\x00\x01" + b"\x02\x00\x00\x00\x00\x02" + b"\x88\xb5"


def _id_reply_frame(spec16, seq, low16=0x1234, harness=0x00010000, rp_child=0):
    """A full-Ethernet ID_REPLY frame echoing `seq`, static_shell_id high half =
    spec16 (R78.5/R81)."""
    ssid = ((spec16 & 0xFFFF) << 16) | (low16 & 0xFFFF)
    payload = (ssid.to_bytes(4, "big") + harness.to_bytes(4, "big")
               + rp_child.to_bytes(4, "big"))
    return _ETH + pdev.encode_frame(pdev.KIND_ID_REPLY, 0, seq, payload)


class _FakeTransport:
    """send/recv/close per the R86.6 seam contract.  `responder(seq, recv_i) ->
    bytes|None` builds the reply for the recv-call at index recv_i since the
    last
    send, given the echoed request seq."""

    def __init__(self, responder):
        self._responder = responder
        self.sends = []
        self._recv_i = 0
        self.closes = 0

    @property
    def closed(self):
        return self.closes > 0

    def send(self, frame):
        self.sends.append(bytes(frame))
        self._recv_i = 0

    def recv(self, timeout):
        seq = self._last_seq()
        out = self._responder(seq, self._recv_i)
        self._recv_i += 1
        return out

    def _last_seq(self):
        raw = self.sends[-1]
        try:
            return pdev.decode_frame(raw).seq       # header-onward slice?
        except pdev.PyroFrameError:
            return pdev.decode_frame(raw[14:]).seq  # strip 14B Ethernet header

    def close(self):  # R86.7: idempotent
        self.closes += 1


def _probe(
    responder,
    *,
    cap=True,
    attempts=3,
    timeout=0.02,
     expected_spec16=0x0202):
    """Run probe_device with a fake transport + cap_check; return (result,
    transport)."""
    box = {}

    def factory(cfg):
        box["t"] = _FakeTransport(responder)
        return box["t"]

    cfg = pdev.DeviceConfig(
        expected_spec16=expected_spec16,
        probe_timeout_s=timeout,
        probe_attempts=attempts,
        cap_check=(lambda: cap),
        transport_factory=factory,
    )
    return pdev.probe_device(cfg), box.get("t")


# ==========================================================================
# probe_device fault paths (R86.4) via transport_factory + cap_check.
# ==========================================================================
def test_probe_timeout_after_three_attempts():  # AC-2b-2 (R84/R86.4)
    """Transport that never replies (recv -> None): after probe_attempts (3,
    R84)
    the probe concludes not-usable with the single probe-condition canonical
    reason
    (transport privilege present via cap_check)."""
    (usable, reason), t = _probe(lambda seq, i: None, cap=True, attempts=3)
    assert usable is False
    assert reason == CANON_PROBE, f"{reason!r}"
    assert len(t.sends) == 3, (
        f"probe must make exactly probe_attempts=3 ID_REQUEST attempts (R84), "
        f"made {len(t.sends)}")


def test_probe_spec16_mismatch_is_not_usable():  # AC-2b-2 (R81/R86.4)
    """A valid ID_REPLY whose static_shell_id SPEC16 != expected (0x0303 vs
    0x0202)
    is NOT a valid reply (R81 SPEC16 check) -> (False, probe-condition
    reason)."""
    (usable, reason), _t = _probe(
        lambda seq, i: _id_reply_frame(0x0303, seq), cap=True)
    assert usable is False
    assert reason == CANON_PROBE, f"{reason!r}"


def test_probe_valid_reply_is_usable():  # AC-2b-2 (R81/R86.4 positive control)
    """A SPEC16-valid ID_REPLY (0x0202) with CAP_NET_RAW present -> (True,
    reason);
    the reason records the observed full static_shell_id (R86.4 MAY)."""
    (usable, reason), _t = _probe(
        lambda seq, i: _id_reply_frame(0x0202, seq, low16=0xABCD), cap=True)
    assert usable is True, reason
    assert "0x0202abcd" in reason.lower(), (
        f"reason should record the observed static_shell_id (R86.4): "
        f"{reason!r}")


@pytest.mark.parametrize("stray_kind",
                         ["bad_magic", "wrong_kind", "truncated"])
# AC-2b-2 (R78.1/R86.4)
def test_probe_tolerates_stray_frame_before_valid_reply(stray_kind):
    """A malformed / non-matching 0x88B5 frame arriving BEFORE the valid
    ID_REPLY
    must NOT abort the probe (R86.4 permits raising only on a genuinely
    malformed
    REPLY); the subsequent valid reply is accepted -> (True, ...)."""
    def responder(seq, i):
        if i == 0:
            if stray_kind == "bad_magic":
                b = bytearray(_id_reply_frame(0x0202, seq))
                b[14] = 0x51                      # corrupt PYRO magic
                return bytes(b)
            if stray_kind == "wrong_kind":
                return _ETH + pdev.encode_frame(pdev.KIND_MATCH_REPLY, 1, seq,
                                                b"\x00" * 8)
            return _ETH + b"\x50\x01\x02"         # truncated PYRO header
        return _id_reply_frame(0x0202, seq)

    (usable, reason), _t = _probe(responder, cap=True)
    assert usable is True, f"stray {stray_kind} aborted the probe: {reason!r}"


# AC-2b-2 (R86.4 deterministic)
def test_probe_no_privilege_via_cap_check_false():
    """Deterministic, host-independent no-privilege path: cap_check -> False
    means
    the probe cannot send ID_REQUEST, so BOTH conditions are unmet and the exact
    non-elided two-condition literal is returned WITHOUT raising."""
    called = {"transport": False}

    def factory(cfg):  # must NOT be invoked when privilege is absent
        called["transport"] = True
        return _FakeTransport(lambda seq, i: None)

    cfg = pdev.DeviceConfig(
    cap_check=(
        lambda: False),
         transport_factory=factory)
    usable, reason = pdev.probe_device(cfg)  # must not raise
    assert usable is False
    assert reason == CANON_TWO_CONDITION, f"{reason!r}"
    assert called["transport"] is False, (
        "R86.4: no privileged transport operation before the CAP_NET_RAW check")


# AC-2b-2 (R83/R86.4)
def test_probe_cap_present_no_reply_enumerates_only_probe():
    """When transport privilege IS present (cap_check True) but no valid reply
    arrives, ONLY the probe condition is enumerated (transport condition
    met)."""
    (usable, reason), _t = _probe(lambda seq, i: None, cap=True, attempts=1)
    assert usable is False
    assert reason == CANON_PROBE
    assert "CAP_NET_RAW absent" not in reason


def test_probe_never_leaks_os_permission_error():  # AC-2b-2 (R86.1/R86.4)
    """No transport/privilege condition may surface as
    OSError/PermissionError."""
    for cap in (True, False):
        try:
            pdev.probe_device(
    pdev.DeviceConfig(
        cap_check=(
            lambda c=cap: c),
            transport_factory=(
                lambda cfg: _FakeTransport(
                    lambda seq,
                    i: None)),
                    probe_timeout_s=0.01,
                     probe_attempts=1))
        # pragma: no cover - spec violation
        except (OSError, PermissionError) as exc:
            pytest.fail(
    f"probe_device leaked {
        type(exc).__name__} (R86.1/R86.4)")


# ==========================================================================
# R86.7 Transport-protocol conformance (v2.2.3): close() on every path,
# None sentinel, short-frame handling.
# ==========================================================================
@pytest.mark.parametrize("scenario", ["timeout", "mismatch", "valid"])
def test_probe_closes_transport_on_every_path(scenario):  # AC-2b-2 (R86.7)
    """R86.7: `close()` releases the transport; the probe MUST call it on
    success
    AND on failure paths (no leaked transport)."""
    responders = {
        "timeout": lambda seq, i: None,
        "mismatch": lambda seq, i: _id_reply_frame(0x0303, seq),
        "valid": lambda seq, i: _id_reply_frame(0x0202, seq),
    }
    (_usable, _reason), t = _probe(responders[scenario], cap=True, attempts=2)
    assert t.closes >= 1, (
        f"probe must close() the transport on the {scenario} path (R86.7)")


def test_transport_close_is_idempotent():  # AC-2b-2 (R86.7)
    """R86.7: close() is idempotent — a second call is a harmless no-op."""
    t = _FakeTransport(lambda seq, i: None)
    t.close()
    t.close()  # must not raise
    assert t.closes == 2


# AC-2b-2 (R86.7/R84)
def test_probe_recv_none_sentinel_is_no_frame_not_empty():
    """R86.7: recv()==None is the *no-frame-within-timeout* sentinel; a None
    at each
    of probe_attempts attempts drives the `probe: no valid ID_REPLY` disposition
    (distinct from an empty/short frame, which is also handled without
    crashing)."""
    (usable, reason), t = _probe(lambda seq, i: None, cap=True, attempts=3)
    assert usable is False and reason == CANON_PROBE
    assert len(t.sends) == 3


@pytest.mark.parametrize("frag",
    [b"",
    b"\x50",
    b"\x50\x01\x02",
     b"\x02\x00\x00"])
def test_probe_handles_short_frame_without_crash(frag):  # AC-2b-2 (R86.7)
    """R86.7: a transport returning a short/partial frame (fewer bytes than a
    valid
    reply) is handled per protocol — treated as not-a-valid-reply, never a
    crash;
    the probe concludes not-usable and closes the transport."""
    (usable, reason), t = _probe(lambda seq, i,
     f=frag: (_ETH + f), cap=True, attempts=2)
    assert usable is False, f"short frame {
    frag!r} must not yield usable (R86.7)"
    assert reason == CANON_PROBE
    assert t.closes >= 1


# ==========================================================================
# load_partial fault paths (R86.5) via the load_runner seam.
# ==========================================================================
def _bitfile(tmp_path):
    p = tmp_path / "pyro_rp.bit"
    p.write_bytes(b"\x00" * 64)
    return str(p)


def test_load_runner_nonzero_rc_raises_load_error(tmp_path):  # AC-2b-3 (R86.5)
    with pytest.raises(pdev.PyroLoadError):
        pdev.load_partial(
    pdev.DeviceConfig(
        load_runner=lambda a, p, t: (
            1, "JTAG chain mismatch")), _bitfile(tmp_path))


# AC-2b-3 (R86.1/R86.5)
def test_load_runner_exception_wrapped_as_load_error(tmp_path):
    def runner(args, path, timeout):
        raise RuntimeError("hw_server unreachable")
    with pytest.raises(pdev.PyroLoadError):
        pdev.load_partial(
    pdev.DeviceConfig(
        load_runner=runner),
         _bitfile(tmp_path))


# AC-2b-3 (R84/R86.5)
def test_load_runner_timeout_raises_load_error(tmp_path):
    """A JTAG-load timeout (PYRO_JTAG_LOAD_TIMEOUT expiry, R84) surfaces as
    PyroLoadError, not a crash."""
    def runner(args, path, timeout):
        raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
    with pytest.raises(pdev.PyroLoadError):
        pdev.load_partial(
    pdev.DeviceConfig(
        load_runner=runner),
         _bitfile(tmp_path))


def test_load_missing_bitstream_raises_load_error(tmp_path):  # AC-2b-3 (R86.5)
    with pytest.raises(pdev.PyroLoadError):
        pdev.load_partial(
            pdev.DeviceConfig(load_runner=lambda a, p, t: (0, "ok")),
            str(tmp_path / "does_not_exist.bit"))


# AC-2b-3 (R86.5 positive control)
def test_load_success_returns_none(tmp_path):
    """A successful runner (rc==0) with an existing bitstream -> None
    (R86.5)."""
    result = pdev.load_partial(
        pdev.DeviceConfig(load_runner=lambda a, p, t: (0, "programmed")),
        _bitfile(tmp_path))
    assert result is None


# AC-2b-3 (R84/R86.5)
def test_load_runner_receives_jtag_load_timeout_constant(tmp_path):
    """R84/R86.5: load_partial bounds the programming step by
    PYRO_JTAG_LOAD_TIMEOUT (default 600.0 s) and passes it to the runner; a
    config
    override is honored."""
    seen = {}

    def runner(args, path, timeout):
        seen["timeout"] = timeout
        return (0, "ok")

    pdev.load_partial(
    pdev.DeviceConfig(
        load_runner=runner),
         _bitfile(tmp_path))
    assert seen["timeout"] == 600.0, (
        f"default JTAG-load timeout must be PYRO_JTAG_LOAD_TIMEOUT=600s (R84), "
        f"got {seen['timeout']}")

    seen.clear()
    pdev.load_partial(
        pdev.DeviceConfig(load_runner=runner, jtag_load_timeout_s=42.0),
        _bitfile(tmp_path))
    assert seen["timeout"] == 42.0, (
        "config override of jtag_load_timeout_s ignored")


# AC-2b-3 (R86.1)
def test_load_partial_never_leaks_os_permission_error(tmp_path):
    def runner(args, path, timeout):
        raise PermissionError("simulated privileged failure")
    try:
        pdev.load_partial(
    pdev.DeviceConfig(
        load_runner=runner),
         _bitfile(tmp_path))
    except pdev.PyroLoadError:
        pass  # correctly wrapped
    # pragma: no cover - spec violation
    except (OSError, PermissionError) as exc:
        pytest.fail(f"load_partial leaked {type(exc).__name__} (R86.1/R86.5)")
    else:  # pragma: no cover - spec violation
        pytest.fail("load_partial must raise PyroLoadError on a runner failure")


# ==========================================================================
# R68 device env knobs -> DeviceConfig defaults (v2.2.2), sampled at R35a
# points.
# ==========================================================================
# AC-2b (R68/R86.6, v2.5.0)
def test_device_iface_default_is_none_fail_closed():
    """Unset PYRO_DEVICE_IFACE -> DeviceConfig().iface is None: **no spec
    default, fail-closed** (F3/R68 no-default rule, v2.5.0 — the netdev name is
    host configuration, not a spec fact, so nothing is guessed and nothing is
    scanned).  (conftest clears the knob and refresh_env()s before each
    test.)"""
    assert pdev.DeviceConfig().iface is None


# AC-2b-2 (R68/R83, v2.5.0)
def test_probe_unconfigured_iface_fails_closed_canonical():
    """No iface configured (and no transport seam): probe_device returns
    (False, <R83 canonical reason>) whose FIRST condition is
    `transport: PYRO_DEVICE_IFACE not configured` — it never raises, never
    guesses, never scans (R68/R70).  No probe ran, so the probe condition
    (no valid ID_REPLY) is NOT enumerated; the CAP_NET_RAW condition follows
    when the capability is also absent (fixed R83 order: 1 then 3)."""
    # Privilege present (cap_check seam): the unconfigured iface is the ONLY
    # unmet condition.  chardev is pinned None (P2d v2.7.0): a sampled
    # PYRO_QDMA_CHARDEV is a VALIDLY CONFIGURED transport, and this test
    # isolates the unconfigured-iface clause.
    usable, reason = pdev.probe_device(
        pdev.DeviceConfig(cap_check=lambda: True, chardev=None))
    assert usable is False
    assert reason == CANON_IFACE_UNSET, f"{reason!r}"
    # Privilege absent: conditions 1 and 3, in the fixed canonical order.
    usable, reason = pdev.probe_device(
        pdev.DeviceConfig(cap_check=lambda: False, chardev=None))
    assert usable is False
    assert reason == CANON_IFACE_UNSET + \
        ", transport: CAP_NET_RAW absent", f"{reason!r}"


# R78.11/R68 (v2.5.0)
def test_perf_readout_unconfigured_iface_is_unavailable():
    """read_perf_counters with no iface and no transport seam fails closed to
    None (counters unavailable) — never a raise, a guess, or a scan (R70).
    chardev pinned None (P2d v2.7.0): with PYRO_QDMA_CHARDEV configured
    the transport exists and the counters are genuinely readable — this test
    is about the fully-unconfigured case."""
    assert pdev.read_perf_counters(
        pdev.DeviceConfig(iface=None, chardev=None)) is None


def test_hw_server_default_is_spec_default():  # AC-2b (R68/R86.6)
    """Unset PYRO_HW_SERVER -> DeviceConfig().hw_server default
    'TCP:localhost:3121'."""
    assert pdev.DeviceConfig().hw_server == "TCP:localhost:3121"


def test_device_iface_sampled_at_r35a(monkeypatch):  # AC-2b (R68/R35a/R86.6)
    """Setting PYRO_DEVICE_IFACE then reaching an R35a sampling point
    (refresh_env) makes DeviceConfig() default `iface` reflect the new value."""
    monkeypatch.setenv("PYRO_DEVICE_IFACE", "testif0")
    pyro.refresh_env()
    assert pdev.DeviceConfig().iface == "testif0"


def test_hw_server_sampled_at_r35a(monkeypatch):  # AC-2b (R68/R35a/R86.6)
    monkeypatch.setenv("PYRO_HW_SERVER", "TCP:example.internal:9999")
    pyro.refresh_env()
    assert pdev.DeviceConfig().hw_server == "TCP:example.internal:9999"


def test_spec_fixed_params_are_not_env_knobs():  # AC-2b (R68/R86.6)
    """R68/R86.6: expected_spec16, probe_timeout_s, probe_attempts,
    jtag_load_timeout_s are spec constants (NOT env knobs) with fixed
    defaults."""
    c = pdev.DeviceConfig()
    # R81
    assert c.expected_spec16 == phase2_support.PYRO_SHELL_SPEC16 == 0x0202
    # R84
    assert c.probe_timeout_s == 0.5
    assert c.probe_attempts == 3                                           # R84
    assert c.jtag_load_timeout_s == 600.0                                  # R84
