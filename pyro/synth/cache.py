"""Persistent PR-bitstream (artifact) cache — the R4 bitstream cache.

This is the second of PYRO's two caches (the first being the µs-scale host
classification cache in :mod:`pyro.re`).  It is **persistent on disk** and keyed
*exactly* per R4/R47b:

    (pattern_bytes, encoding, effective_flags,
     generator_version, toolchain_version, shell/PR-region_version)

``pattern_bytes`` and ``effective_flags`` are the **canonicalized** values from
:func:`pyro.hdl.identity.descriptor_key` (R4b: canonicalization applies wherever
circuit reuse is decided).  A cache hit means the artifact is **warm** — a PR
load (~O(100 ms)) suffices instead of a minutes-long synthesis (R4 invariant);
this holds **across process restarts** (R63d).

The cache also records **negative entries** (R65): a key whose synthesis failed
is permanently fallback-only for the current ``(generator, toolchain, shell)``
tuple and MUST NOT be retried indefinitely.

Layout on disk (one directory per key hash)::

    <root>/<keyhash>/artifact.bin      # stub PR bitstream payload
    <root>/<keyhash>/manifest.json     # R47b manifest
    <root>/<keyhash>/FAILED.json       # negative entry (diagnostic), if failed

The default root lives under the user cache area (``$XDG_CACHE_HOME`` or
``~/.cache``), never inside the git worktree; a test overrides it with a temp
directory (``PYRO_CACHE_DIR`` or the constructor argument).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from .manifest import Manifest

# R4/R47b key: descriptor_key prefix + toolchain/shell versions.
BitstreamKey = Tuple[bytes, int, int, int, int, int]
# (pattern_bytes, enc, eff_flags, generator_ver, toolchain_ver, shell_ver)


def make_key(descriptor_key: tuple, toolchain_version: int,
             shell_version: int) -> BitstreamKey:
    """Extend a :func:`pyro.hdl.identity.descriptor_key` with tool/shell
    versions.

    ``descriptor_key`` is ``(pattern_bytes, encoding, effective_flags,
    generator_version)``; appending ``toolchain_version`` and ``shell_version``
    yields the full R4/R47b bitstream-cache key.
    """
    pb, enc, eff, gen = descriptor_key
    return (bytes(pb), int(enc), int(eff), int(gen),
            int(toolchain_version), int(shell_version))


def key_digest(key: BitstreamKey) -> str:
    """Filesystem-safe, collision-resistant digest of a bitstream key.

    Deterministic and length-prefixed so distinct keys never collide via
    concatenation ambiguity.
    """
    pb, enc, eff, gen, tool, shell = key
    h = hashlib.sha256()
    h.update(b"PYRO-BSKEY\x00")
    h.update(len(pb).to_bytes(8, "little"))
    h.update(pb)
    for v in (enc, eff, gen, tool, shell):
        h.update(int(v).to_bytes(8, "little", signed=False))
    return h.hexdigest()


def default_root() -> Path:
    """Default persistent cache root — under the user cache area (never the
    repo)."""
    override = os.environ.get("PYRO_CACHE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")
    return Path(base) / "pyro" / "bitstreams"


@dataclass(frozen=True)
class CacheEntry:
    """A warm artifact: its manifest and the path to the bitstream payload."""

    manifest: Manifest
    artifact_path: Path

    def read_payload(self) -> bytes:
        return self.artifact_path.read_bytes()


class BitstreamCache:
    """Persistent on-disk PR-artifact cache (R4/R63d/R65)."""

    _ARTIFACT = "artifact.bin"
    _MANIFEST = "manifest.json"
    _FAILED = "FAILED.json"

    def __init__(self, root: Optional[os.PathLike] = None):
        self.root = Path(root) if root is not None else default_root()

    # -- key -> directory --------------------------------------------------
    def _dir(self, key: BitstreamKey) -> Path:
        return self.root / key_digest(key)

    # -- lookup ------------------------------------------------------------
    def get(self, key: BitstreamKey) -> Optional[CacheEntry]:
        """Return the warm :class:`CacheEntry` for ``key`` or ``None`` (cold).

        A key with a negative entry (R65) returns ``None`` here; callers use
        :meth:`is_failed` to distinguish permanent-fallback from plain cold.
        """
        d = self._dir(key)
        man_path = d / self._MANIFEST
        art_path = d / self._ARTIFACT
        if not (man_path.is_file() and art_path.is_file()):
            return None
        try:
            manifest = Manifest.from_json(man_path.read_text())
        except (OSError, ValueError, TypeError):
            return None
        return CacheEntry(manifest=manifest, artifact_path=art_path)

    def has(self, key: BitstreamKey) -> bool:
        return self.get(key) is not None

    def is_failed(self, key: BitstreamKey) -> bool:
        """R65: does ``key`` have a negative (permanent-fallback) entry?"""
        return (self._dir(key) / self._FAILED).is_file()

    def failure_reason(self, key: BitstreamKey) -> Optional[str]:
        p = self._dir(key) / self._FAILED
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text()).get("reason")
        except (OSError, ValueError):
            return None

    # -- population (atomic writes, restart-safe) --------------------------
    def put(
    self,
    key: BitstreamKey,
    payload: bytes,
     manifest: Manifest) -> CacheEntry:
        """Write a successful artifact + manifest (R63d).  Overwrites any prior
        negative entry for the key (a later success supersedes a failure)."""
        d = self._dir(key)
        d.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(d / self._ARTIFACT, payload)
        _atomic_write_text(d / self._MANIFEST, manifest.to_json())
        # Clear any stale negative entry.
        try:
            (d / self._FAILED).unlink()
        except FileNotFoundError:
            pass
        return CacheEntry(manifest=manifest, artifact_path=d / self._ARTIFACT)

    def put_failure(self, key: BitstreamKey, reason: str) -> None:
        """Record a permanent-fallback negative entry (R65)."""
        d = self._dir(key)
        d.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(d / self._FAILED,
                           json.dumps({"reason": str(reason)}, sort_keys=True))


# --- atomic file writes (same-dir temp + rename; restart-safe) ------------
def _atomic_write_bytes(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))
