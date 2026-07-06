"""AC-2b-1/2b-2/2b-3 fault paths driven by the R86.6 spec-sanctioned DeviceConfig
test seams (cap_check / transport_factory / load_runner).

R86.6 (v2.2.2) blesses three defaulted `DeviceConfig` seam fields as
**normative** test injection points that need no PYRO_ENABLE_TEST_HOOKS gate:
  * `cap_check: Callable[[], bool]`     — overrides CAP_NET_RAW detection (R86.4);
  * `transport_factory: Callable[[DeviceConfig], Transport]` — a fake frame
    transport (drives timeout / SPEC16-mismatch / stray-frame / valid-reply, R86.4);
  * `load_runner: Callable[[list, str, float], (int, str)]` — a fake JTAG runner
    (drives PyroLoadError / timeout without hw_server, R86.5).

These make the no-privilege / timeout / SPEC16-mismatch / load-failure paths of
AC-2b-1..2b-3 testable WITHOUT hardware and deterministically (host-independent).

Also covers R68 (v2.2.2): the PYRO_DEVICE_IFACE / PYRO_HW_SERVER env knobs seed the
DeviceConfig `iface` / `hw_server` defaults from the R35a-sampled snapshot, while
the remaining device parameters are spec constants (not env knobs).

Coverage: AC-2b-2, AC-2b-3.  Requirements: R68, R78, R81, R83, R84, R86.1, R86.4,
R86.5, R86.6.

--- Spec note (Transport / load_runner callable contracts) ----------------------
R86.6 names the seams and their Python signatures but the spec text does not spell
out the fake Transport's method contract.  Bound here through the sanctioned seam's
observable behavior (black-box via the public injection point, no source read): a
Transport is `send(frame: bytes) -> None`, `recv(timeout: float) -> bytes | None`
(None = no frame within the per-attempt deadline), `close() -> None`; frames are
FULL Ethernet frames (14B L2 header + PYRO header + payload).  load_runner is
`(args: list, bitstream_path: str, timeout: float) -> (returncode: int,
output: str)`; a nonzero rc, a raised exception, or a subprocess timeout each map to
PyroLoadError.  Surfaced for the spec-writer in case an explicit protocol is wanted.
"""
import subprocess

import pytest

import pyro
import pyro.device as pdev
import phase2_support


# --------------------------------------------------------------------------
# Canonical R83/R86.4 reason literals.
# --------------------------------------------------------------------------
CANON_PROBE = ("device_usable=false — probe: no valid ID_REPLY (no reply within "
               "PYRO_PROBE_TIMEOUT, or static_shell_id SPEC16 mismatch)")
CANON_TWO_CONDITION = CANON_PROBE + ", transport: CAP_NET_RAW absent"


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
    bytes|None` builds the reply for the recv-call at index recv_i since the last
    send, given the echoed request seq."""

    def __init__(self, responder):
        self._responder = responder
        self.sends = []
        self._recv_i = 0
        self.closed = False

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

    def close(self):
        self.closed = True


def _probe(responder, *, cap=True, attempts=3, timeout=0.02, expected_spec16=0x0202):
    """Run probe_device with a fake transport + cap_check; return (result, transport)."""
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
    """Transport that never replies (recv -> None): after probe_attempts (3, R84)
    the probe concludes not-usable with the single probe-condition canonical reason
    (transport privilege present via cap_check)."""
    (usable, reason), t = _probe(lambda seq, i: None, cap=True, attempts=3)
    assert usable is False
    assert reason == CANON_PROBE, f"{reason!r}"
    assert len(t.sends) == 3, (
        f"probe must make exactly probe_attempts=3 ID_REQUEST attempts (R84), "
        f"made {len(t.sends)}")


def test_probe_spec16_mismatch_is_not_usable():  # AC-2b-2 (R81/R86.4)
    """A valid ID_REPLY whose static_shell_id SPEC16 != expected (0x0303 vs 0x0202)
    is NOT a valid reply (R81 SPEC16 check) -> (False, probe-condition reason)."""
    (usable, reason), _t = _probe(
        lambda seq, i: _id_reply_frame(0x0303, seq), cap=True)
    assert usable is False
    assert reason == CANON_PROBE, f"{reason!r}"


def test_probe_valid_reply_is_usable():  # AC-2b-2 (R81/R86.4 positive control)
    """A SPEC16-valid ID_REPLY (0x0202) with CAP_NET_RAW present -> (True, reason);
    the reason records the observed full static_shell_id (R86.4 MAY)."""
    (usable, reason), _t = _probe(
        lambda seq, i: _id_reply_frame(0x0202, seq, low16=0xABCD), cap=True)
    assert usable is True, reason
    assert "0x0202abcd" in reason.lower(), (
        f"reason should record the observed static_shell_id (R86.4): {reason!r}")


@pytest.mark.parametrize("stray_kind", ["bad_magic", "wrong_kind", "truncated"])
def test_probe_tolerates_stray_frame_before_valid_reply(stray_kind):  # AC-2b-2 (R78.1/R86.4)
    """A malformed / non-matching 0x88B5 frame arriving BEFORE the valid ID_REPLY
    must NOT abort the probe (R86.4 permits raising only on a genuinely malformed
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


def test_probe_no_privilege_via_cap_check_false():  # AC-2b-2 (R86.4 deterministic)
    """Deterministic, host-independent no-privilege path: cap_check -> False means
    the probe cannot send ID_REQUEST, so BOTH conditions are unmet and the exact
    non-elided two-condition literal is returned WITHOUT raising."""
    called = {"transport": False}

    def factory(cfg):  # must NOT be invoked when privilege is absent
        called["transport"] = True
        return _FakeTransport(lambda seq, i: None)

    cfg = pdev.DeviceConfig(cap_check=(lambda: False), transport_factory=factory)
    usable, reason = pdev.probe_device(cfg)  # must not raise
    assert usable is False
    assert reason == CANON_TWO_CONDITION, f"{reason!r}"
    assert called["transport"] is False, (
        "R86.4: no privileged transport operation before the CAP_NET_RAW check")


def test_probe_cap_present_no_reply_enumerates_only_probe():  # AC-2b-2 (R83/R86.4)
    """When transport privilege IS present (cap_check True) but no valid reply
    arrives, ONLY the probe condition is enumerated (transport condition met)."""
    (usable, reason), _t = _probe(lambda seq, i: None, cap=True, attempts=1)
    assert usable is False
    assert reason == CANON_PROBE
    assert "CAP_NET_RAW absent" not in reason


def test_probe_never_leaks_os_permission_error():  # AC-2b-2 (R86.1/R86.4)
    """No transport/privilege condition may surface as OSError/PermissionError."""
    for cap in (True, False):
        try:
            pdev.probe_device(pdev.DeviceConfig(
                cap_check=(lambda c=cap: c),
                transport_factory=(lambda cfg: _FakeTransport(lambda seq, i: None)),
                probe_timeout_s=0.01, probe_attempts=1))
        except (OSError, PermissionError) as exc:  # pragma: no cover - spec violation
            pytest.fail(f"probe_device leaked {type(exc).__name__} (R86.1/R86.4)")


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
            pdev.DeviceConfig(load_runner=lambda a, p, t: (1, "JTAG chain mismatch")),
            _bitfile(tmp_path))


def test_load_runner_exception_wrapped_as_load_error(tmp_path):  # AC-2b-3 (R86.1/R86.5)
    def runner(args, path, timeout):
        raise RuntimeError("hw_server unreachable")
    with pytest.raises(pdev.PyroLoadError):
        pdev.load_partial(pdev.DeviceConfig(load_runner=runner), _bitfile(tmp_path))


def test_load_runner_timeout_raises_load_error(tmp_path):  # AC-2b-3 (R84/R86.5)
    """A JTAG-load timeout (PYRO_JTAG_LOAD_TIMEOUT expiry, R84) surfaces as
    PyroLoadError, not a crash."""
    def runner(args, path, timeout):
        raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
    with pytest.raises(pdev.PyroLoadError):
        pdev.load_partial(pdev.DeviceConfig(load_runner=runner), _bitfile(tmp_path))


def test_load_missing_bitstream_raises_load_error(tmp_path):  # AC-2b-3 (R86.5)
    with pytest.raises(pdev.PyroLoadError):
        pdev.load_partial(
            pdev.DeviceConfig(load_runner=lambda a, p, t: (0, "ok")),
            str(tmp_path / "does_not_exist.bit"))


def test_load_success_returns_none(tmp_path):  # AC-2b-3 (R86.5 positive control)
    """A successful runner (rc==0) with an existing bitstream -> None (R86.5)."""
    result = pdev.load_partial(
        pdev.DeviceConfig(load_runner=lambda a, p, t: (0, "programmed")),
        _bitfile(tmp_path))
    assert result is None


def test_load_runner_receives_jtag_load_timeout_constant(tmp_path):  # AC-2b-3 (R84/R86.5)
    """R84/R86.5: load_partial bounds the programming step by
    PYRO_JTAG_LOAD_TIMEOUT (default 600.0 s) and passes it to the runner; a config
    override is honored."""
    seen = {}

    def runner(args, path, timeout):
        seen["timeout"] = timeout
        return (0, "ok")

    pdev.load_partial(pdev.DeviceConfig(load_runner=runner), _bitfile(tmp_path))
    assert seen["timeout"] == 600.0, (
        f"default JTAG-load timeout must be PYRO_JTAG_LOAD_TIMEOUT=600s (R84), "
        f"got {seen['timeout']}")

    seen.clear()
    pdev.load_partial(
        pdev.DeviceConfig(load_runner=runner, jtag_load_timeout_s=42.0),
        _bitfile(tmp_path))
    assert seen["timeout"] == 42.0, "config override of jtag_load_timeout_s ignored"


def test_load_partial_never_leaks_os_permission_error(tmp_path):  # AC-2b-3 (R86.1)
    def runner(args, path, timeout):
        raise PermissionError("simulated privileged failure")
    try:
        pdev.load_partial(pdev.DeviceConfig(load_runner=runner), _bitfile(tmp_path))
    except pdev.PyroLoadError:
        pass  # correctly wrapped
    except (OSError, PermissionError) as exc:  # pragma: no cover - spec violation
        pytest.fail(f"load_partial leaked {type(exc).__name__} (R86.1/R86.5)")
    else:  # pragma: no cover - spec violation
        pytest.fail("load_partial must raise PyroLoadError on a runner failure")


# ==========================================================================
# R68 device env knobs -> DeviceConfig defaults (v2.2.2), sampled at R35a points.
# ==========================================================================
def test_device_iface_default_is_spec_default():  # AC-2b (R68/R86.6)
    """Unset PYRO_DEVICE_IFACE -> DeviceConfig().iface default 'enp175s0f0' (F3).
    (conftest clears the knob and refresh_env()s before each test.)"""
    assert pdev.DeviceConfig().iface == "enp175s0f0"


def test_hw_server_default_is_spec_default():  # AC-2b (R68/R86.6)
    """Unset PYRO_HW_SERVER -> DeviceConfig().hw_server default 'TCP:localhost:3121'."""
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
    jtag_load_timeout_s are spec constants (NOT env knobs) with fixed defaults."""
    c = pdev.DeviceConfig()
    assert c.expected_spec16 == phase2_support.PYRO_SHELL_SPEC16 == 0x0202  # R81
    assert c.probe_timeout_s == 0.5                                         # R84
    assert c.probe_attempts == 3                                           # R84
    assert c.jtag_load_timeout_s == 600.0                                  # R84
