"""Phase-2 shared availability probes (R71) + the vivado-synthesis corpus fixture.

This is the SINGLE source of truth for the three R71 availability predicates so
that every gated Phase-2 assertion cites the same probe (R71: "A single shared
availability probe SHOULD expose these three predicates"):

    toolchain_present() -> (bool, reason)
    pr_flow_present()   -> (bool, reason)
    device_usable()     -> (bool, reason)

SKIP discipline (R71, normative): a clause that requires an ABSENT predicate MUST
record a SKIP whose reason names the missing prerequisite; a SKIP is NEVER a PASS,
and a PASS comes ONLY from real execution of the clause with its predicate
satisfied.  Tests call `pytest.skip(reason)` using the returned reason string.

This module drives ONLY the public PYRO surface, the filesystem, and (read-only,
non-perturbing) device probes.  It never reads pyro/ implementation source.
Manifest fields are read back from the on-disk bitstream-cache JSON; the field
names asserted (payload_kind, luts, ffs, fmax_mhz, met_timing, toolchain_version,
shell_version) are NORMATIVE in the spec (R47b / R72 / R73 / R75), not scraped
from the implementation.
"""
import json
import os
import subprocess
import sys

import pytest

# --------------------------------------------------------------------------
# Spec constants (cited, not derived from the implementation).
# --------------------------------------------------------------------------
# This host's pinned toolchain (R70a-pin / R71 / §11 P1, v2.2.1: Vivado 2025.2
# present at /usr/local/cad/2025.2/Vivado, synthesizes the target part with no
# license error).  The spec forbids *library code* from scanning the filesystem/
# PATH for Vivado (R70); the test harness MAY locate this known install to pin the
# env for the acceptance run.
KNOWN_VIVADO_INSTALL = "/usr/local/cad/2025.2/Vivado"
VIVADO_PART = "xcu250-figd2104-2L-e"          # physical U250 part (R70a / R71)
PROXY_CLOCK_MHZ = 250                          # R73 proxy clock (F4 250 MHz user box)
PROXY_CLOCK_PERIOD_NS = 4.000                  # R73 4.000 ns constraint
MOCK_TOOLCHAIN_VERSION = 0x00000100            # R72a / R75 mock sentinel
# R75 pinned toolchain_version for Vivado 2025.2 (YY=25=0x19, RR=2, build=0).
VIVADO_TOOLCHAIN_VERSION = 0x19020000          # R70a-pin / R75 (v2.2.1 normative pin)
# 2023.1's 0x17010000 is retained by the spec only as a historical example and is
# NOT valid 2025.2 evidence (R74a); kept here to assert artifacts do NOT carry it.
VIVADO_2023_1_TOOLCHAIN_VERSION = 0x17010000   # historical (R74a/R75 stale-pin)
SHELL_VERSION_MODEL = 0x0A000001               # R75 model-harness shell version
PYRO_SHELL_SPEC16 = 0x0202                      # R81: (spec_MAJOR<<8)|spec_MINOR for 2.2
ESTIMATOR_CALIBRATION_MARGIN = 10              # R74 pre-registered constant (10x ceiling)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PHASE2_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phase2_workers.py")

# A real Vivado OOC synth + P&R is minutes.  Poll deadline inside the worker and
# the parent subprocess wall-clock deadline, both generous (R63/R77; AC-2-3 note
# suggests a ~20 min ceiling).
SYNTH_POLL_TIMEOUT_S = 1200        # worker-side poll deadline
WORKER_SUBPROC_TIMEOUT_S = 1320    # parent-side subprocess deadline (poll + margin)


# --------------------------------------------------------------------------
# Availability predicates (R71).  Each returns (present: bool, reason: str).
# reason is "" when present, else names the missing prerequisite.
# --------------------------------------------------------------------------
def _vivado_install_dir():
    return os.environ.get("PYRO_VIVADO") or KNOWN_VIVADO_INSTALL


def _vivado_executable_ok(install_dir):
    """R71 note: probe for <dir>/bin/vivado existence + executability only; do NOT
    launch it (the adapter handles its own libtinfo.so.5 shim on this host)."""
    exe = os.path.join(install_dir, "bin", "vivado")
    return os.path.isfile(exe) and os.access(exe, os.X_OK)


def toolchain_present():
    """R71: PYRO_TOOLCHAIN=vivado selected AND PYRO_VIVADO resolves to an
    executable Vivado for the target part.

    For the acceptance session: honor a pre-set PYRO_VIVADO; otherwise probe the
    known install and, when a working Vivado is found, pin PYRO_TOOLCHAIN=vivado
    and PYRO_VIVADO into os.environ so the out-of-process synthesis worker
    (R63/R70a) selects the real OOC adapter.  Never launches Vivado.
    """
    tc = os.environ.get("PYRO_TOOLCHAIN")
    if tc is not None and tc != "vivado":
        return (False,
                f"toolchain_present=false — PYRO_TOOLCHAIN={tc!r} selects a "
                f"non-vivado toolchain (operator opt-out) (R70/R71)")
    install_dir = _vivado_install_dir()
    if not _vivado_executable_ok(install_dir):
        return (False,
                f"toolchain_present=false — no executable Vivado at "
                f"{install_dir}/bin/vivado; PYRO_VIVADO unresolved (R70/R71)")
    # Pin the resolved selection for the acceptance run (sampled at worker import,
    # R35a/R70).  conftest _ENV_KEYS saves/restores these so they never leak.
    os.environ["PYRO_VIVADO"] = install_dir
    os.environ["PYRO_TOOLCHAIN"] = "vivado"
    return (True, "")


def pr_flow_present():
    """R71/R82d/R83: the conjunction of (i) a host-validated locked static DCP with
    pyro_rp as HD.RECONFIGURABLE (R82b), (ii) a pyro_rp Pblock/HD.RECONFIGURABLE
    floorplan (R80 boundary), and (iii) a PR-generation flow that emits a genuine
    loadable partial bitstream with a passing pr_verify (R82c).  Established FALSE
    on this host — none of the R82d artifacts exist (R71/§11 P1).

    R83 canonical reason: `pr_flow_present=false — <first missing R82d artifact>`.
    The first missing artifact here is the locked static DCP (nothing is built yet).

    Probed honestly: a real PR flow would advertise itself via a PYRO_PR_FLOW /
    OpenNIC PR floorplan pointer; absent that, this is the documented
    always-false-with-reason placeholder mandated by R71/R83.
    """
    flow = os.environ.get("PYRO_PR_FLOW")
    if flow and os.path.isdir(flow):
        # Reserved for a future host where the PR flow exists; never true here.
        return (True, "")
    return (False,
            "pr_flow_present=false — locked static DCP (pyro_rp "
            "HD.RECONFIGURABLE) not built/validated on this host (R82d)")


# R83 canonical unmet-condition phrases (fixed order, comma-separated).
_R83_PROBE_UNMET = ("probe: no valid ID_REPLY (no reply within "
                    "PYRO_PROBE_TIMEOUT, or static_shell_id SPEC16 mismatch)")
_R83_TRANSPORT_UNMET = "transport: CAP_NET_RAW absent"


def has_cap_net_raw():
    """Read-only, non-perturbing check for CAP_NET_RAW (bit 13) in this process's
    effective capability set (R83 gate (ii) / P2/P3).  False on the current host."""
    try:
        with open("/proc/self/status", "r", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("CapEff:"):
                    return bool(int(line.split()[1], 16) & (1 << 13))
    except Exception:
        pass
    return False


def device_usable():
    """R71/R83: `device_usable` flips true iff BOTH (i) a live probe receives a
    valid ID_REPLY whose static_shell_id passes the R81 SPEC16 check within
    PYRO_PROBE_TIMEOUT (R84) AND (ii) transport privilege CAP_NET_RAW is present
    (R83).  Established FALSE on this host: the PR shell is not yet flashed (so no
    valid ID_REPLY) and CAP_NET_RAW is not granted.

    R83 canonical reason (supersedes the v2.1.3 literal): `device_usable=false — `
    followed by a comma-separated enumeration, in fixed order, of EXACTLY the unmet
    conditions among the probe (i) and transport (ii) phrases.

    Probed READ-ONLY and non-perturbing: we never open or touch the device; the
    probe condition (i) is unmet here because no PR shell answers (pr_flow absent).
    """
    unmet = []
    # (i) probe: no flashed PR shell on this host => no valid ID_REPLY.
    unmet.append(_R83_PROBE_UNMET)
    # (ii) transport privilege.
    if not has_cap_net_raw():
        unmet.append(_R83_TRANSPORT_UNMET)
    return (False, "device_usable=false — " + ", ".join(unmet))


# --------------------------------------------------------------------------
# Manifest read-back from the on-disk bitstream cache (R47b/R72/R73/R75).
# We identify manifests structurally by their normative field set so the cache
# layout is not hard-coded.  Parsing JSON data files is NOT reading the impl.
# --------------------------------------------------------------------------
_MANIFEST_KEYS = ("toolchain_version", "luts", "ffs")


def find_manifests(cache_dir):
    """Return [(path, dict), ...] for every bitstream-cache manifest under
    cache_dir, identified by the normative R47b field set."""
    out = []
    for root, _dirs, files in os.walk(cache_dir):
        for fn in files:
            if not fn.endswith(".json"):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                continue
            if isinstance(data, dict) and all(k in data for k in _MANIFEST_KEYS):
                out.append((path, data))
    return out


def ooc_manifests(cache_dir):
    """The genuine-post-route ('ooc_metrics', R72a) manifests under cache_dir."""
    return [d for _p, d in find_manifests(cache_dir)
            if d.get("payload_kind") == "ooc_metrics"]


# --------------------------------------------------------------------------
# Out-of-process worker runner (mirrors phase1_support.run_worker but targets
# phase2_workers.py and allows a long vivado deadline).  Subprocess isolation
# keeps the synthesis service's spawn/forkserver workers (R63) short-lived.
# --------------------------------------------------------------------------
def run_phase2_worker(command, *args, cache_dir, extra_env=None,
                      timeout=WORKER_SUBPROC_TIMEOUT_S):
    env = dict(os.environ)
    env["PYRO_CACHE_DIR"] = str(cache_dir)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    # Clean routing/test-hook config; caller opts in via extra_env.  We do NOT
    # pop PYRO_TOOLCHAIN/PYRO_VIVADO here — the vivado selection must survive.
    for k in ("PYRO_DISABLE", "PYRO_FORCE_MODEL", "PYRO_ENABLE_TEST_HOOKS"):
        env.pop(k, None)
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items()})
    proc = subprocess.run(
        [sys.executable, PHASE2_WORKER, command, *[str(a) for a in args]],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=timeout,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"phase2 worker {command} failed rc={proc.returncode}\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    try:
        return json.loads(proc.stdout), proc.stdout, proc.stderr
    except json.JSONDecodeError:
        raise AssertionError(
            f"phase2 worker {command} did not emit JSON:\n{proc.stdout}\n{proc.stderr}")


# --------------------------------------------------------------------------
# Estimator resources via the public introspection hook (R31 explain()).
# --------------------------------------------------------------------------
def estimate_resources(pattern, flags=0):
    """L2 resource estimate for an HW-eligible pattern (R11/R12/R31), as a dict
    with at least 'luts' and 'ffs'.  Returns None for a fallback-only pattern."""
    import pyro.re as pre
    info = pre.explain(pattern, flags)
    return info.get("est_resources")


# --------------------------------------------------------------------------
# Shared session-scoped calibration corpus (R74 / AC-2-1 / AC-2-4).  Real Vivado
# is minutes/pattern; synthesize each calibration pattern ONCE into its own cache
# dir and reuse across tests.  Kept to 4 patterns to stay within the suite's
# ~25-min Vivado budget.  Each pattern uses only §5.1 constructs (R9).
# --------------------------------------------------------------------------
CALIBRATION_PATTERNS = [
    (r"error|warn|fatal", 0, "an error and a warn then fatal"),  # alternation of literals
    (r"[A-Za-z_]\w*", 0, "identifier_42 x9"),                    # class + \w* concat
    (r"\d{1,6}", 0, "id 000123 tail"),                           # bounded repeat of a class
    (r"(GET|POST) /\S*", 0, "GET /index.html POST /a"),          # group + class + concat
]


@pytest.fixture(scope="session")
def vivado_corpus(tmp_path_factory):
    """Synthesize the calibration corpus once via the REAL vivado adapter and
    expose {pattern: {cache, worker_json, manifest, est}} to AC-2-1/AC-2-3/AC-2-4.

    SKIPs the whole set when toolchain_present is false (R71) so the LIVE clauses
    that depend on it record a SKIP naming the missing prerequisite, never a PASS.
    """
    present, reason = toolchain_present()
    if not present:
        pytest.skip(reason)
    root = tmp_path_factory.mktemp("pyro_vivado_corpus")
    extra = {
        "PYRO_TOOLCHAIN": "vivado",
        "PYRO_VIVADO": os.environ["PYRO_VIVADO"],
        "PYRO_TEST_SYNTH_TIMEOUT": str(SYNTH_POLL_TIMEOUT_S),
    }
    entries = {}
    for i, (pat, flags, subject) in enumerate(CALIBRATION_PATTERNS):
        cdir = str(root / f"p{i}")
        os.makedirs(cdir, exist_ok=True)
        # Per-pattern isolation: a worker CRASH (nonzero rc / timeout) on one
        # pattern MUST NOT turn every dependent LIVE test into an ERROR.  Capture
        # it into the entry so dependent tests fail/skip individually per pattern
        # and consumers can pick the first pattern whose worker actually ran.
        # A GRACEFUL synth failure (rc==0, synth_failed>0) is NOT a crash and is
        # recorded normally as worker_error=None with manifest=None.
        res, worker_error = None, None
        try:
            res, _o, _e = run_phase2_worker(
                "vivado_synth", pat, subject, cache_dir=cdir, extra_env=extra)
        except Exception as exc:  # noqa: BLE001 — AssertionError (rc!=0) / TimeoutExpired
            worker_error = f"{type(exc).__name__}: {exc}"
        oocs = ooc_manifests(cdir)
        entries[pat] = {
            "cache": cdir,
            "flags": flags,
            "subject": subject,
            "worker": res,
            "worker_error": worker_error,
            "manifest": (oocs[0] if oocs else None),
            "all_manifests": oocs,
            "est": estimate_resources(pat, flags),
        }
    return entries


def first_ran_pattern(corpus):
    """The first calibration pattern whose vivado worker actually RAN (no crash),
    regardless of whether synthesis then succeeded or gracefully failed.  Consumers
    that need at least one good pattern use this; returns None if every worker
    crashed (a genuine failure the caller should surface, not silently pass)."""
    for pat, _flags, _subject in CALIBRATION_PATTERNS:
        entry = corpus.get(pat)
        if entry and entry.get("worker_error") is None and entry.get("worker"):
            return pat
    return None
