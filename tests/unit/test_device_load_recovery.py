"""R85a (v2.5.1): in-band recovery folded into pyro.device.load_partial.

LIVE (no hardware): drives load_partial through the R86.6 load_runner seam,
asserting the R86.5 (v2.5.1) two-step contract:

  * after a successful program step the R85a recovery command runs through the
    SAME runner seam, bounded by the same jtag_load_timeout_s;
  * the default recover_cmd is the spec constant PYRO_RECOVER_CMD
    (``sudo -n <repo>/scripts/pyro_wedge_recover.sh`` — pinned, existing);
  * recover_cmd=None disables the step (explicit config injection);
  * recovery nonzero rc / exception / timeout => PyroLoadError (never OSError),
    with the R85a-transient diagnostic;
  * a FAILED program step never attempts recovery.
"""
import os

import pytest

import pyro.device as pdev


def _bitfile(tmp_path):
    p = tmp_path / "pattern_partial.bit"
    p.write_bytes(b"\x00" * 64)
    return str(p)


class _RecordingRunner:
    def __init__(self, results):
        self.calls = []
        self._results = list(results)

    def __call__(self, cmd, cwd, timeout):
        self.calls.append({"cmd": list(cmd), "cwd": cwd, "timeout": timeout})
        r = self._results.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


def test_recovery_runs_after_successful_program(tmp_path):
    runner = _RecordingRunner([(0, "programmed"), (0, "WEDGE_RECOVER_DONE")])
    cfg = pdev.DeviceConfig(load_runner=runner,
                            recover_cmd=("recover", "--now"))
    assert pdev.load_partial(cfg, _bitfile(tmp_path)) is None
    assert len(runner.calls) == 2, "program step then R85a recovery step"
    assert runner.calls[1]["cmd"] == ["recover", "--now"]
    # Same R84 bound on both steps (R86.5 v2.5.1).
    assert runner.calls[0]["timeout"] == runner.calls[1]["timeout"]


def test_default_recover_cmd_is_spec_constant_and_pinned_script_exists():
    cfg = pdev.DeviceConfig()
    assert cfg.recover_cmd == pdev.PYRO_RECOVER_CMD
    assert pdev.PYRO_RECOVER_CMD[:2] == ("sudo", "-n"), (
        "non-interactive sudo so an absent sudoers grant fails fast (R85a)")
    script = pdev.PYRO_RECOVER_CMD[2]
    assert os.path.basename(script) == "pyro_wedge_recover.sh"
    assert os.path.isfile(script), (
        f"pinned R85a recovery script missing: {script!r}")


def test_recover_cmd_none_disables_recovery(tmp_path):
    runner = _RecordingRunner([(0, "programmed")])
    cfg = pdev.DeviceConfig(load_runner=runner, recover_cmd=None)
    assert pdev.load_partial(cfg, _bitfile(tmp_path)) is None
    assert len(runner.calls) == 1, "recover_cmd=None must skip the R85a step"


def test_recovery_nonzero_rc_raises_load_error(tmp_path):
    runner = _RecordingRunner([(0, "programmed"), (1, "sudo: a password is required")])
    cfg = pdev.DeviceConfig(load_runner=runner, recover_cmd=("r",))
    with pytest.raises(pdev.PyroLoadError) as ei:
        pdev.load_partial(cfg, _bitfile(tmp_path))
    msg = str(ei.value)
    assert "R85a" in msg and "recovery" in msg
    assert "password is required" in msg, "runner output tail in diagnostic"


def test_recovery_exception_wrapped_never_leaks(tmp_path):
    runner = _RecordingRunner([(0, "programmed"), OSError("sudo missing")])
    cfg = pdev.DeviceConfig(load_runner=runner, recover_cmd=("r",))
    try:
        pdev.load_partial(cfg, _bitfile(tmp_path))
    except pdev.PyroLoadError:
        pass
    except Exception as exc:  # pragma: no cover - the failure being asserted
        pytest.fail(f"load_partial leaked {type(exc).__name__} (R86.1)")
    else:  # pragma: no cover - the failure being asserted
        pytest.fail("recovery failure must not be silent (R85a/R86.5)")


def test_recovery_timeout_raises_transient_load_error(tmp_path):
    runner = _RecordingRunner([(0, "programmed"), pdev._LoadTimeout()])
    cfg = pdev.DeviceConfig(load_runner=runner, recover_cmd=("r",),
                            jtag_load_timeout_s=42.0)
    with pytest.raises(pdev.PyroLoadError) as ei:
        pdev.load_partial(cfg, _bitfile(tmp_path))
    assert "recovery" in str(ei.value) and "42" in str(ei.value)


def test_failed_program_step_never_attempts_recovery(tmp_path):
    runner = _RecordingRunner([(1, "JTAG chain mismatch")])
    cfg = pdev.DeviceConfig(load_runner=runner, recover_cmd=("r",))
    with pytest.raises(pdev.PyroLoadError):
        pdev.load_partial(cfg, _bitfile(tmp_path))
    assert len(runner.calls) == 1, (
        "recovery must not run when the program step failed (R86.5 v2.5.1)")
