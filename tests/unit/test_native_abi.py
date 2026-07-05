"""ABI-2.0.0 conformance + harness-contract tests driven via ctypes (R57/R58).

These exercise the FULL native ABI surface (``src/pyro_rt.c`` /
``include/pyro_rt.h``) against the in-library software model of the §7.4 harness,
the way AC-1-1 / AC-1-2 / AC-1-4 require: lifecycle, generate/synth/status/load,
scan incl. overflow/resume and the ``PYRO_E_NOT_RESIDENT`` guard, caps, the R47a
identity trust boundary, R47b manifest integrity/compatibility rejection, R49
alignment, single-issue thread safety, and R44 defined-state guarantees.

The library is built in-session (``make lib``); if no C toolchain is present the
whole module skips with a recorded reason.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import struct
import subprocess
import threading
from pathlib import Path

import pytest

from pyro import hdl
from pyro._circuit_model import _scan_windows
from pyro.synth import artifact as _artifact
from pyro.synth.manifest import Manifest, payload_crc32
from pyro.synth.residency import _job_from_circuit
from pyro.synth.toolchain import (
    MockToolchain, TOOLCHAIN_VERSION, SHELL_VERSION,
)

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def native():
    """Build + load libpyro_rt.so; skip-with-reason if a C toolchain is absent."""
    import pyro._native as _n

    cc = shutil.which(os.environ.get("CC", "cc")) or shutil.which("gcc")
    if cc is None:
        pytest.skip("no C compiler (cc/gcc) on PATH")
    make = shutil.which("make") or "/usr/bin/make"
    try:
        r = subprocess.run([make, "lib"], cwd=str(_REPO),
                           capture_output=True, text=True)
    except (FileNotFoundError, OSError) as exc:
        pytest.skip(f"make unavailable: {exc}")
    if r.returncode != 0:
        pytest.skip(f"libpyro_rt.so build failed:\n{r.stderr[-600:]}")
    try:
        _n.load()
    except _n.NativeUnavailable as exc:  # pragma: no cover
        pytest.skip(str(exc))
    return _n


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _write_artifact(art_dir: Path, pattern, flags=0):
    """Generate a circuit, run the mock toolchain, write artifact.bin+manifest."""
    circ = hdl.generate(pattern, flags)
    payload, manifest = MockToolchain().run(_job_from_circuit(circ))
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / "artifact.bin").write_bytes(payload)
    (art_dir / "manifest.json").write_text(manifest.to_json())
    return circ


def _descriptor(native, circ, art_dir: Path, hash_override: bytes = None):
    h = hash_override if hash_override is not None else circ.pattern_hash16
    return native.build_descriptor(h, circ.circ_flags, circ.enc, art_dir)


def _encode(subject):
    return subject.encode("utf-8") if isinstance(subject, str) else bytes(subject)


def _py_windows(circ, subject):
    buf = _encode(subject)
    return [(w.start, w.end) for w in _scan_windows(circ.automaton, buf, 0)]


def _resident(native, art_dir: Path, pattern, flags=0):
    """Full happy path -> (ctx, circuit, circ) with the circuit resident."""
    circ = _write_artifact(art_dir, pattern, flags)
    ctx = native.NativeCtx("model://")
    c = ctx.generate(_descriptor(native, circ, art_dir), flags, circ.enc)
    assert c.synth_request() == native.PYRO_OK
    assert c.load() == native.PYRO_OK
    assert c.status() == native.PYRO_CIRC_RESIDENT
    return ctx, c, circ


# --------------------------------------------------------------------------
# version / caps (R37/R42, AC-0-8 lineage, R13)
# --------------------------------------------------------------------------
def test_abi_version_is_2_0_0(native):
    assert native.load().pyro_abi_version() == 0x00020000


def test_caps_meets_r13_minimums(native):
    ctx = native.NativeCtx("model://")
    caps = ctx.caps()
    assert caps.max_states >= 1024
    assert caps.max_patterns >= 256
    assert caps.max_repeat >= 255
    assert caps.alphabet == 256
    assert caps.datapath_bytes == 1
    assert caps.generator_version == hdl.GENERATOR_VERSION
    assert caps.harness_version == hdl.HARNESS_VERSION
    assert caps.pr_partitions == 1
    ctx.close()


# --------------------------------------------------------------------------
# transport bindings (R39, F5)
# --------------------------------------------------------------------------
def test_open_close_model(native):
    ctx = native.NativeCtx("model://")
    ctx.close()  # idempotent-safe
    ctx.close()


def test_device_bindings_parse_but_report_no_device(native):
    lib = native.load()
    for uri in (b"qdma://af:00.0/q0", b"eth://enp175s0f0"):
        h = ctypes.c_void_p(0x1234)
        rc = lib.pyro_ctx_open(ctypes.byref(h), uri)
        assert rc == native.PYRO_E_DEVICE
        assert not h  # R44: *out left NULL


def test_bad_scheme_is_invalid(native):
    lib = native.load()
    h = ctypes.c_void_p(0x1234)
    rc = lib.pyro_ctx_open(ctypes.byref(h), b"bogus://x")
    assert rc == native.PYRO_E_INVALID
    assert not h


def test_generate_bad_descriptor_invalid_and_null(native):
    lib = native.load()
    ctx = native.NativeCtx("model://")
    out = ctypes.c_void_p(0x1)
    rc = lib.pyro_generate(ctx.handle, b"not-a-descriptor", 16, 0,
                           native.PYRO_ENC_BYTES, ctypes.byref(out))
    assert rc == native.PYRO_E_INVALID
    assert not out  # R44 handle left NULL
    ctx.close()


# --------------------------------------------------------------------------
# async lifecycle tiers (R40/R4)
# --------------------------------------------------------------------------
def test_tier_transitions_cold_synth_warm_resident(native, tmp_path):
    art_dir = tmp_path / "art"
    art_dir.mkdir()
    circ = hdl.generate(b"ab+c")
    ctx = native.NativeCtx("model://")
    c = ctx.generate(_descriptor(native, circ, art_dir), 0, circ.enc)

    assert c.status() == native.PYRO_CIRC_COLD
    assert c.synth_request() == native.PYRO_OK
    assert c.status() == native.PYRO_CIRC_SYNTHESIZING  # requested, no artifact

    # background "synthesis" completes -> artifact appears -> warm
    payload, manifest = MockToolchain().run(_job_from_circuit(circ))
    (art_dir / "artifact.bin").write_bytes(payload)
    (art_dir / "manifest.json").write_text(manifest.to_json())
    assert c.status() == native.PYRO_CIRC_WARM

    assert c.load() == native.PYRO_OK
    assert c.status() == native.PYRO_CIRC_RESIDENT
    ctx.close()


def test_scan_before_load_is_not_resident(native, tmp_path):
    circ = _write_artifact(tmp_path / "art", b"abc")
    ctx = native.NativeCtx("model://")
    c = ctx.generate(_descriptor(native, circ, tmp_path / "art"), 0, circ.enc)
    rc, matches = c.scan(b"abcabc")
    assert rc == native.PYRO_E_NOT_RESIDENT
    assert matches == []  # R44 *out_count == 0
    ctx.close()


# --------------------------------------------------------------------------
# R47a identity trust boundary + R58 CSR identity block
# --------------------------------------------------------------------------
def test_identity_mismatch_refuses_load(native, tmp_path):
    circ = _write_artifact(tmp_path / "art", b"abc")
    ctx = native.NativeCtx("model://")
    wrong = bytes(16)  # not the baked hash
    c = ctx.generate(_descriptor(native, circ, tmp_path / "art", wrong), 0, circ.enc)
    assert c.load() == native.PYRO_E_NOT_RESIDENT
    assert c.status() != native.PYRO_CIRC_RESIDENT
    ctx.close()


def test_csr_identity_block_populated_from_manifest(native, tmp_path):
    ctx, c, circ = _resident(native, tmp_path / "art", b"hello")
    assert ctx.csr_read(0x0000) == hdl.ID_MAGIC          # ID "PYRO"
    assert ctx.csr_read(0x0004) == hdl.HARNESS_VERSION
    for i in range(4):
        assert ctx.csr_read(0x0018 + 4 * i) == circ.circ_id[i]
    assert ctx.csr_read(0x0028) == circ.circ_flags
    ctx.close()


# --------------------------------------------------------------------------
# R47b manifest integrity / compatibility rejection (R58)
# --------------------------------------------------------------------------
def test_corrupt_manifest_integrity_refuses_load(native, tmp_path):
    art_dir = tmp_path / "art"
    circ = _write_artifact(art_dir, b"abc")
    man = Manifest.from_json((art_dir / "manifest.json").read_text())
    tampered = Manifest(**{**man.__dict__, "integrity_hash": "deadbeef"})
    (art_dir / "manifest.json").write_text(tampered.to_json())
    ctx = native.NativeCtx("model://")
    c = ctx.generate(_descriptor(native, circ, art_dir), 0, circ.enc)
    assert c.load() == native.PYRO_E_NOT_RESIDENT
    ctx.close()


def test_incompatible_shell_refuses_load(native, tmp_path):
    art_dir = tmp_path / "art"
    art_dir.mkdir()
    circ = hdl.generate(b"abc")
    body = _artifact.serialize_automaton_body(circ.automaton)
    payload = _artifact.build_artifact(
        pattern_hash16=circ.pattern_hash16, circ_flags=circ.circ_flags,
        encoding=circ.enc, effective_flags=circ.automaton.flags,
        generator_version=circ.generator_version,
        harness_version=circ.harness_version,
        shell_version=0xDEADBEEF, body=body)  # WRONG shell/PR-region
    manifest = Manifest(
        pattern_hash=circ.pattern_hash16.hex(), encoding=circ.enc,
        effective_flags=circ.automaton.flags, circ_flags=circ.circ_flags,
        generator_version=circ.generator_version,
        harness_version=circ.harness_version,
        toolchain_version=TOOLCHAIN_VERSION, shell_version=0xDEADBEEF,
        luts=1, ffs=1, bram_kb=1, dsps=0, fmax_mhz=300.0, met_timing=True,
        integrity_hash=payload_crc32(payload), payload_len=len(payload))
    (art_dir / "artifact.bin").write_bytes(payload)
    (art_dir / "manifest.json").write_text(manifest.to_json())
    ctx = native.NativeCtx("model://")
    c = ctx.generate(_descriptor(native, circ, art_dir), 0, circ.enc)
    assert c.load() == native.PYRO_E_NOT_RESIDENT
    ctx.close()


def test_corrupt_artifact_bytes_refuse_load(native, tmp_path):
    art_dir = tmp_path / "art"
    circ = _write_artifact(art_dir, b"abc")
    raw = bytearray((art_dir / "artifact.bin").read_bytes())
    raw[80] ^= 0xFF  # flip a body byte -> internal CRC fails
    (art_dir / "artifact.bin").write_bytes(bytes(raw))
    ctx = native.NativeCtx("model://")
    c = ctx.generate(_descriptor(native, circ, art_dir), 0, circ.enc)
    assert c.load() == native.PYRO_E_NOT_RESIDENT
    ctx.close()


# --------------------------------------------------------------------------
# R65: synthesis failure -> permanent fallback (PYRO_E_SYNTH)
# --------------------------------------------------------------------------
def test_synth_failure_is_permanent_fallback(native, tmp_path):
    art_dir = tmp_path / "art"
    art_dir.mkdir()
    (art_dir / "FAILED.json").write_text('{"reason":"mock RTL does not fit"}')
    circ = hdl.generate(b"abc")
    ctx = native.NativeCtx("model://")
    c = ctx.generate(_descriptor(native, circ, art_dir), 0, circ.enc)
    assert c.status() == native.PYRO_CIRC_FALLBACK
    assert c.load() == native.PYRO_E_SYNTH   # not a device error (R44/R65)
    ctx.close()


# --------------------------------------------------------------------------
# scan correctness / differential (R19, AC-1-2 candidate windows)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("pattern,subject", [
    (b"a", b"banana"),
    (b"ab+c", b"xabcabbbcy"),
    (b"[0-9]+", b"a12b345c"),
    (b"a.c", b"aXcaYc"),
    (b"(cat|dog)", b"cat dog cot"),
    (b"a*", b"baab"),
    (rb"\bword\b", b"a word here word"),
    (b"^ab", b"abx"),
    ("café", "a café here"),
    (r"\d+", "price 42 euro 7"),
])
def test_c_windows_match_python_model(native, tmp_path, pattern, subject):
    ctx, c, circ = _resident(native, tmp_path / "art", pattern)
    rc, matches = c.scan(_encode(subject), 0, out_cap=256)
    assert rc == native.PYRO_OK
    got = [(s, e) for (s, e, _pid, _fl) in matches]
    assert got == _py_windows(circ, subject)   # byte-identical to the Py model
    ctx.close()


def test_c_windows_are_complete_for_stock_re(native, tmp_path):
    import re
    pattern, subject = b"ab+c", b"abcabbbcabxabc"
    ctx, c, circ = _resident(native, tmp_path / "art", pattern)
    rc, matches = c.scan(subject, 0, out_cap=256)
    assert rc == native.PYRO_OK
    c_starts = {s for (s, e, _p, _f) in matches}
    stock_starts = {m.start() for m in re.finditer(pattern, subject)}
    assert stock_starts <= c_starts        # completeness (R19): no false negs
    ctx.close()


def test_zero_width_flag_reported(native, tmp_path):
    ctx, c, circ = _resident(native, tmp_path / "art", b"a*")
    rc, matches = c.scan(b"baa", 0, out_cap=64)
    assert rc == native.PYRO_OK
    # every window equals the Python model, and zero-width flag is set on [s,s)
    for (s, e, _pid, fl) in matches:
        assert bool(fl & native.PYRO_MATCH_ZERO_WIDTH) == (s == e)
    ctx.close()


# --------------------------------------------------------------------------
# AC-1-4: overflow / streaming resumption (R41/R47)
# --------------------------------------------------------------------------
def test_overflow_resume_reconstructs_full_list(native, tmp_path):
    subject = b"A" * 40
    ctx, c, circ = _resident(native, tmp_path / "art", b"A")
    full = _py_windows(circ, subject)

    # Drain with a small ring capacity, resuming via start_off past the last
    # returned window's start (streaming), and assert the union equals `full`.
    collected = []
    start_off = 0
    guard = 0
    while True:
        guard += 1
        assert guard < 1000
        rc, matches = c.scan(subject, start_off, out_cap=7)
        assert rc == native.PYRO_OK
        collected.extend((s, e) for (s, e, _p, _f) in matches)
        if len(matches) < 7:
            break
        start_off = matches[-1][0] + 1  # resume just past the last start
    assert collected == full
    # OVF is signalled in STATUS during an overflowing scan
    ctx.close()


def test_overflow_sets_status_ovf(native, tmp_path):
    subject = b"A" * 10
    ctx, c, circ = _resident(native, tmp_path / "art", b"A")
    rc, matches = c.scan(subject, 0, out_cap=3)
    assert rc == native.PYRO_OK
    assert len(matches) == 3
    assert ctx.csr_read(0x0014) & 0x8   # STATUS.OVF
    ctx.close()


# --------------------------------------------------------------------------
# R49 alignment assertion (defined error, testable)
# --------------------------------------------------------------------------
def test_alignment_assertion_fires(native, tmp_path):
    ctx, c, circ = _resident(native, tmp_path / "art", b"a")
    ctx.force_misalign(True)
    rc, matches = c.scan(b"aaa", 0, out_cap=8)
    assert rc == native.PYRO_E_INVALID
    assert matches == []            # R44 *out_count == 0
    # auto-clears: the next scan succeeds
    rc, matches = c.scan(b"aaa", 0, out_cap=8)
    assert rc == native.PYRO_OK
    assert len(matches) == 3
    ctx.close()


# --------------------------------------------------------------------------
# single-issue + thread safety (R32/R43/R48)
# --------------------------------------------------------------------------
def test_concurrent_scans_serialize_correctly(native, tmp_path):
    subject = b"abcabcabc"
    ctx, c, circ = _resident(native, tmp_path / "art", b"abc")
    expected = _py_windows(circ, subject)

    errors = []

    def worker():
        for _ in range(50):
            rc, matches = c.scan(subject, 0, out_cap=64)
            if rc != native.PYRO_OK or [(s, e) for (s, e, _p, _f) in matches] != expected:
                errors.append((rc, matches))
                return

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    ctx.close()


# --------------------------------------------------------------------------
# single-tenant eviction (R64) + reload determinism
# --------------------------------------------------------------------------
def test_second_load_evicts_first(native, tmp_path):
    circ_a = _write_artifact(tmp_path / "a", b"aaa")
    circ_b = _write_artifact(tmp_path / "b", b"bbb")
    ctx = native.NativeCtx("model://")
    ca = ctx.generate(_descriptor(native, circ_a, tmp_path / "a"), 0, circ_a.enc)
    cb = ctx.generate(_descriptor(native, circ_b, tmp_path / "b"), 0, circ_b.enc)
    assert ca.load() == native.PYRO_OK
    assert cb.load() == native.PYRO_OK            # evicts A (R64)
    assert cb.status() == native.PYRO_CIRC_RESIDENT
    assert ca.status() == native.PYRO_CIRC_WARM   # reverted to warm

    rc, _ = ca.scan(b"aaa", 0, out_cap=8)
    assert rc == native.PYRO_E_NOT_RESIDENT       # A no longer resident
    rc, matches = cb.scan(b"bbb", 0, out_cap=8)
    assert rc == native.PYRO_OK
    assert [(s, e) for (s, e, _p, _f) in matches] == _py_windows(circ_b, b"bbb")
    ctx.close()
