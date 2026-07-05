"""Bitstream-cache keying and tier persistence (R4/R4b/R47b/R63d/R65).

TDD focus: the persistent PR-artifact cache is keyed EXACTLY per R4 and its
canonicalization scope is R4b (two raw-distinct-but-canonically-equal patterns
share one key -> one circuit).  Warm artifacts survive a simulated process
restart (a fresh BitstreamCache over the same directory).
"""

import re

import pytest

import pyro.hdl as hdl
from pyro.hdl import identity
from pyro.synth import (
    BitstreamCache, Manifest, make_key, key_digest, payload_crc32,
)
from pyro.synth.toolchain import TOOLCHAIN_VERSION, SHELL_VERSION


def _key(pattern, flags=0):
    dk = identity.descriptor_key(pattern, flags, hdl.GENERATOR_VERSION)
    return make_key(dk, TOOLCHAIN_VERSION, SHELL_VERSION)


def _manifest(payload, pattern="abc", flags=0):
    dk = identity.descriptor_key(pattern, flags, hdl.GENERATOR_VERSION)
    pb, enc, eff, gen = dk
    return Manifest(
        pattern_hash=identity.pattern_hash(
            pattern, eff, hdl.GENERATOR_VERSION, hdl.HARNESS_VERSION).hex(),
        encoding=enc, effective_flags=eff, circ_flags=0,
        generator_version=gen, harness_version=hdl.HARNESS_VERSION,
        toolchain_version=TOOLCHAIN_VERSION, shell_version=SHELL_VERSION,
        luts=1, ffs=1, bram_kb=1, dsps=0, fmax_mhz=300.0, met_timing=True,
        integrity_hash=payload_crc32(payload), payload_len=len(payload),
    )


# --- R4/R4b canonical keying ----------------------------------------------
def test_key_components_are_full_r4_tuple():
    k = _key("abc", 0)
    assert len(k) == 6  # (pattern_bytes, enc, eff_flags, gen, toolchain, shell)
    assert k[4] == TOOLCHAIN_VERSION and k[5] == SHELL_VERSION


def test_canonically_equal_patterns_share_one_key():
    # R4b: "(?i)abc" and ("abc", re.I) canonicalize identically -> one circuit key.
    assert _key("(?i)abc", 0) == _key("abc", re.I)
    assert key_digest(_key("(?i)abc", 0)) == key_digest(_key("abc", re.I))


def test_scoped_inline_flag_stays_distinct():
    # Scoped (?i:abc) is semantic, not a global flag -> distinct key from plain.
    assert _key("(?i:abc)", 0) != _key("abc", 0)


def test_bytes_vs_str_same_text_distinct_key():
    # Encoding tag is part of the key (R47a): same text, different automaton.
    assert _key("abc", 0) != _key(b"abc", 0)


def test_generator_version_participates():
    dk = identity.descriptor_key("abc", 0, hdl.GENERATOR_VERSION + 1)
    other = make_key(dk, TOOLCHAIN_VERSION, SHELL_VERSION)
    assert other != _key("abc", 0)


# --- tiers: cold / warm + negative entry (R65) -----------------------------
def test_cold_miss(tmp_path):
    cache = BitstreamCache(tmp_path)
    assert cache.get(_key("abc")) is None
    assert cache.has(_key("abc")) is False
    assert cache.is_failed(_key("abc")) is False


def test_put_then_warm_hit(tmp_path):
    cache = BitstreamCache(tmp_path)
    k = _key("abc")
    payload = b"PYROSTUB" + b"\x00" * 16
    cache.put(k, payload, _manifest(payload))
    entry = cache.get(k)
    assert entry is not None
    assert entry.read_payload() == payload
    assert entry.manifest.integrity_ok(payload)


def test_negative_entry_is_permanent_fallback(tmp_path):
    cache = BitstreamCache(tmp_path)
    k = _key("abc")
    cache.put_failure(k, "does not fit")
    assert cache.is_failed(k) is True
    assert cache.has(k) is False           # not warm
    assert cache.failure_reason(k) == "does not fit"


def test_success_supersedes_prior_failure(tmp_path):
    cache = BitstreamCache(tmp_path)
    k = _key("abc")
    cache.put_failure(k, "transient")
    payload = b"PYROSTUB" + b"\x01" * 16
    cache.put(k, payload, _manifest(payload))
    assert cache.is_failed(k) is False
    assert cache.has(k) is True


# --- R4 invariant: warm survives a simulated process restart (R63d) --------
def test_warm_survives_restart(tmp_path):
    k = _key("needle[0-9]+")
    payload = b"PYROSTUB" + b"\x02" * 16
    BitstreamCache(tmp_path).put(k, payload, _manifest(payload, "needle[0-9]+"))
    # "restart": a brand-new cache object over the same on-disk directory.
    reopened = BitstreamCache(tmp_path)
    entry = reopened.get(k)
    assert entry is not None
    assert entry.read_payload() == payload


# --- R47b integrity + compatibility checks ---------------------------------
def test_integrity_hash_detects_corruption(tmp_path):
    payload = b"PYROSTUB" + b"\x03" * 16
    m = _manifest(payload)
    assert m.integrity_ok(payload) is True
    assert m.integrity_ok(payload + b"x") is False
    assert m.integrity_ok(b"PYROSTUB" + b"\xff" * 16) is False


def test_shell_incompatibility_refused():
    payload = b"PYROSTUB"
    m = _manifest(payload)
    assert m.compatible_with(SHELL_VERSION, hdl.HARNESS_VERSION) is True
    assert m.compatible_with(SHELL_VERSION + 1, hdl.HARNESS_VERSION) is False
    assert m.compatible_with(SHELL_VERSION, hdl.HARNESS_VERSION + 1) is False


def test_manifest_json_roundtrip():
    payload = b"PYROSTUB" + b"\x04" * 8
    m = _manifest(payload)
    assert Manifest.from_json(m.to_json()) == m
