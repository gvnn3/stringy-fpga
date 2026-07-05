"""AC-0-8 reconciliation (R37 version-history note).

Phase 0 froze a shape-only ABI **1.0.0** stub and validated it on branch
`phase0-pyro` (`pyro_abi_version() == 0x00010000`).  The v2.0.0 architecture
supersedes that stub with the circuit-oriented ABI **2.0.0**; a fresh integrated
build reports `0x00020000` (R37).  Per R37's version-history note the 1.0.0
assertion is a **historical Phase-0 checkpoint against the stub, not an invariant
of the shipped system**, so this module asserts the *current* tree reports ABI
2.0.0 and records the 1.0.0 checkpoint as historical.  The full §7.3 ABI-2.0.0
symbol set is checked in test_ac1_2_harness_contract.py.
"""
import ctypes
import os
import shutil
import subprocess

import pytest

import abi_ctypes as abi

REPO_ROOT = abi.REPO_ROOT
STUB_1_0_0 = os.path.join(REPO_ROOT, "src", "pyro_rt_stub.c")

_lib = None
_load_error = None
try:
    _lib = abi.load()
except OSError as e:
    _load_error = str(e)

CC = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
MAKE = shutil.which("make")


@pytest.mark.skipif(_lib is None, reason=f"libpyro_rt.so unavailable: {_load_error}")
def test_current_lib_reports_abi_2_0_0():
    """R37/AC-1-1: the shipped runtime reports ABI 2.0.0 == 0x00020000."""
    assert _lib.pyro_abi_version() == abi.ABI_2_0_0


@pytest.mark.skipif(not (CC and MAKE), reason="need make + cc to compile the ABI check")
def test_make_abi_check_reports_2_0_0():
    """R37/R38: the Makefile `abi-check` target compiles the frozen header against
    the real runtime and asserts pyro_abi_version() == 0x00020000."""
    proc = subprocess.run([MAKE, "abi-check"], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"make abi-check failed:\n{proc.stdout}\n{proc.stderr}"
    combined = proc.stdout + proc.stderr
    assert "0x00020000" in combined or "2.0.0" in combined, combined


def test_abi_1_0_0_is_phase0_historical_checkpoint():
    """R37: the ABI 1.0.0 stub is a Phase-0 historical checkpoint, superseded on
    this branch — the 1.0.0 value is no longer the shipped ABI, and the old
    Phase-0 stub source is absent here (its checkpoint lives on `phase0-pyro`)."""
    assert abi.ABI_1_0_0 != abi.ABI_2_0_0
    assert (abi.ABI_2_0_0 >> 16) == 2, "shipped ABI MAJOR must be 2 (R37)"
    # The Phase-0 shape-only stub is not part of the integrated v2.0.0 tree.
    assert not os.path.isfile(STUB_1_0_0), (
        "src/pyro_rt_stub.c (the 1.0.0 stub) should not exist on the integrated "
        "branch; its AC-0-8 checkpoint is preserved on phase0-pyro (R37 note)"
    )


def test_header_declares_2_0_0_and_records_supersession():
    """R37: the ABI 2.0.0 header self-identifies as 2.0.0 and records that it
    supersedes the Phase-0 1.0.0 stub."""
    with open(abi.HEADER_PATH, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    assert "2.0.0" in text
    assert "0x00020000" in text
    assert "1.0.0" in text, "header should record the superseded 1.0.0 checkpoint (R37)"
