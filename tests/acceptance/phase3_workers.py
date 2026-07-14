"""Out-of-process corpus workers for the Phase-3 transparency regression
(AC-3-1 / R60) and stats (AC-3-4) tests.

TEST SUPPORT, not a test module: it runs one NAMED corpus program — an
ordinary ``re``-using Python program — either under stock ``re`` (mode
``stock``: pyro is never imported) or under ``pyro.install()`` (mode
``installed``), and streams the full per-call output sequence plus
explain()/stats() snapshots to stdout as JSON lines for the parent test to
compare and assert on.  It drives ONLY the public surface: the stdlib ``re``
module (patched or not), ``pyro.install()``/``uninstall()``,
``pyro.re.explain()``/``stats()`` (R31/R66) and deadline polling — no R67
seams beyond what exists, no implementation imports.

Record stream (one JSON object per line):
  {"type": "snap", "at": K, "circuit_status": S, "stats": {...}}
      installed mode only; "at" = number of corpus calls completed when the
      snapshot was taken (at=0 precedes the first call).
  {"type": "call", "i": K, "label": L, "value": V | "exc": E}
      per-call canonical output.  V is JSON-safe (Match objects canonicalised
      to span/groups/spans/lastindex/lastgroup/groupdict; bytes tagged).  E
      compares exceptions by canonical type AND str(e) AND, for re.error,
      msg/pos/pattern (never just the type name).
  {"type": "done", "mode": ..., "n_calls": N, "pyro_in_sys_modules": bool,
   "transition_reached": bool|None, "stats": {...}|None,
   "explain_key": {...}|None, "native_router": {...}|None}

Corpus-program ground rules (why some things are ABSENT):
  * Programs assert NOTHING about tiers or stats — the transition assertion
    is made by the parent TEST over the snapshot stream (otherwise AC-3-1 is
    vacuous, or worse, self-confirming).
  * A program that must transition mid-run yields AWAIT_TIER once; in
    installed mode the harness deadline-POLLS explain()/stats() there (mock
    toolchain synthesis, seconds); in stock mode it is a no-op.  Calls
    continue after it, so the transition is genuinely mid-run.
  * KNOWN, DELIBERATE non-defects are kept OUT of the corpus: the concrete
    match type name (``HybridMatch``), ``m.re`` identity, ``repr(m)``, and
    pickling of Match/Pattern objects intentionally differ from stock re and
    are accepted by the spec as-is — changing them would require a spec
    amendment, not a code fix.  Comparing them here would fail the suite on
    non-defects, so corpus programs never observe them.

Env (PYRO_CACHE_DIR / PYRO_N_SYNTH / PYRO_NO_NATIVE / ...) is inherited from
the parent and must be set BEFORE this process starts (R35a sampling at pyro
import); phase3_support._worker_env owns that contract.
"""
import itertools
import json
import os
import re  # the corpus programs' module-under-test; patched in installed mode
import sys

# Repo root importable when run as a script or re-imported by a spawn /
# forkserver worker of the synthesis service (R63).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import phase3_support  # shared deadline-poll helper (poll_tier)

S_MIN = 64 * 1024  # R2 / R51 step 4 (spec constant, mirrors oracle.S_MIN)

# Sentinel step: wait (installed mode only) for the program's key pattern to
# reach warm/resident before continuing.  NOT an assertion.
AWAIT_TIER = ("await_tier",)


# --------------------------------------------------------------------------
# JSON-safe canonicalisation.
# --------------------------------------------------------------------------
def _json_safe(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (bytes, bytearray)):
        return {"__bytes__": bytes(v).decode("latin-1")}
    # Match detection is STRUCTURAL, not isinstance: R36a explicitly permits
    # isinstance(m, re.Match) to be False on the hybrid path (HybridMatch is
    # not a re.Match subclass — a documented permanent limitation, not a
    # defect), so an isinstance check here would silently canonicalise stock
    # matches but raise for pyro's, poisoning the comparison.
    if (hasattr(v, "span") and hasattr(v, "group") and hasattr(v, "groups")
            and hasattr(v, "groupdict")):
        return _canon_match(v)
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    raise TypeError(
        f"corpus program produced a non-canonicalisable value {v!r} "
        f"({type(v).__name__}); return JSON-safe values or Match objects "
        "(Pattern objects / m.re / repr are deliberately out of scope)")


def _canon_match(m):
    """Canonical Match form: span/groups/spans/lastindex/lastgroup/groupdict.

    Identity-ish surfaces (type name, m.re, repr, pickle) are deliberately
    excluded — spec-known non-defects (see module docstring)."""
    groups = m.groups()
    ng = len(groups)
    return {
        "__match__": True,
        "span": list(m.span()),
        "group0": _json_safe(m.group(0)),
        "groups": _json_safe(groups),
        "spans": [list(m.span(i)) for i in range(ng + 1)],
        "lastindex": m.lastindex,
        "lastgroup": m.lastgroup,
        "groupdict": {k: _json_safe(x) for k, x in sorted(m.groupdict().items())},
    }


def _canon_exc(e):
    """Exception canonical form: type AND str AND (re.error) msg/pos/pattern."""
    d = {
        "type": f"{type(e).__module__}.{type(e).__qualname__}",
        "str": str(e),
    }
    if isinstance(e, re.error):
        d["msg"] = getattr(e, "msg", None)
        d["pos"] = getattr(e, "pos", None)
        pat = getattr(e, "pattern", None)
        d["pattern"] = _json_safe(pat) if isinstance(
            pat, (str, bytes, bytearray, type(None))) else repr(pat)
    return d


# --------------------------------------------------------------------------
# Corpus programs (R60: real re-using programs).  Each is a generator yielding
# ("label", thunk) steps and optionally AWAIT_TIER; each has metadata naming
# its key pattern for explain() snapshots.  Deterministic by construction.
# --------------------------------------------------------------------------

# ~86 KiB synthetic access log (>= S_min, so EVERY key-pattern call over it
# crosses R51 step 4 by size and counts toward the R4a launch policy).
_LOG_STANZA = ("GET /index.html 200 12ms\n"
               "POST /api/v1/items 500 ERROR upstream timeout\n"
               "GET /static/app.js 304 2ms\n"
               "POST /login 302 55ms\n")
BIG_LOG = _LOG_STANZA * 800
assert len(BIG_LOG) >= S_MIN, "BIG_LOG must be >= 64 KiB (R51 step 4 by size)"

LOG_KEY_PATTERN = r"(GET|POST) (/\S*)"          # 2 groups -> findall yields tuples
FIELD_KEY_PATTERN = r"(?P<key>\w+)=(?P<val>\d+)"  # named groups
FIELD_SUBJECT = "a=1 bb=22 ccc=333 dd=4 tail x=9 y=not z=77"


def prog_log_hunter():
    """Access-log scanner: >=64 KiB subjects drive the size arm of R51 step 4.

    With PYRO_N_SYNTH=2 (parent-chosen), the second big-subject call launches
    background synthesis; AWAIT_TIER lets it complete; the tail calls then run
    with the circuit warm/resident — same outputs required throughout (R36/R53).
    """
    kp = LOG_KEY_PATTERN
    yield "search.big.pre", lambda: re.search(kp, BIG_LOG)
    yield "findall.groups.big.pre", lambda: re.findall(kp, BIG_LOG)
    yield "finditer.head5.big.pre", lambda: list(
        itertools.islice(re.finditer(kp, BIG_LOG), 5))
    yield "match.big.pre", lambda: re.match(kp, BIG_LOG)
    yield "fullmatch.small.pre", lambda: re.fullmatch(kp, "GET /x")
    yield AWAIT_TIER
    yield "search.big.post", lambda: re.search(kp, BIG_LOG)
    yield "findall.groups.big.post", lambda: re.findall(kp, BIG_LOG)
    yield "finditer.head5.big.post", lambda: list(
        itertools.islice(re.finditer(kp, BIG_LOG), 5))
    yield "sub.big.post", lambda: re.sub(kp, r"\1 [\2]", BIG_LOG)
    yield "subn.big.post", lambda: re.subn(kp, r"\1", BIG_LOG)
    yield "split.small.post", lambda: re.split(kp, _LOG_STANZA)
    yield "search.nomatch.post", lambda: re.search(kp, "no verbs here")


def prog_field_extractor():
    """key=value extractor: small subjects, >=32 reuses drive the REUSE arm of
    R51 step 4 (the per-pattern reuse counter crosses N_reuse=32 at call 33;
    with PYRO_N_SYNTH=2 the second gate-crossing dispatch launches synthesis).

    The bulk of the loop uses search/finditer deliberately: only the single-
    match ops (search/match/fullmatch/finditer) reach the residency
    consultation that ticks the R4a launch policy — the aggregate ops
    (findall/sub/subn/split) are Phase-0-delegated whole-op fallbacks that
    tick the reuse counter but never the launch policy.  A findall-only loop
    would cross the reuse gate yet never launch, and the transition assertion
    in the parent test would (rightly) fail."""
    kp = FIELD_KEY_PATTERN
    subj = FIELD_SUBJECT
    for k in range(40):
        if k % 4 == 3:
            yield f"findall.{k:02d}", (lambda: re.findall(kp, subj))
        else:
            yield f"search.{k:02d}", (lambda: re.search(kp, subj))
        if k % 10 == 0:
            yield f"groupdicts.{k:02d}", (lambda: [
                m.groupdict() for m in re.finditer(kp, subj)])
    yield AWAIT_TIER
    for k in range(3):
        yield f"findall.post.{k}", (lambda: re.findall(kp, subj))
    yield "search.post", lambda: re.search(kp, subj)
    yield "sub.post", lambda: re.sub(kp, r"\g<key>:\g<val>", subj)
    yield "split.post", lambda: re.split(r"\s+", subj)


def _compile_ok(pattern):
    re.compile(pattern)  # Pattern object deliberately not returned (identity)
    return "compiled-ok"


def prog_error_paths():
    """Exception fidelity: re.error (with msg/pos/pattern) and TypeError must
    be identical under install() — type AND str AND re.error fields."""
    yield "compile.unbalanced", lambda: _compile_ok(r"(unclosed")
    yield "compile.badrange", lambda: _compile_ok(r"[z-a]")
    yield "compile.badrepeat", lambda: _compile_ok(r"*nothing")
    yield "compile.badgroupname", lambda: _compile_ok(r"(?P<1bad>x)")
    yield "sub.badgroupref", lambda: re.sub(r"(a)", r"\2", "aa")
    yield "search.bytes_pat_str_subj", lambda: re.search(rb"a", "aaa")
    yield "findall.int_subject", lambda: re.findall(r"a", 123)
    yield "search.ok.between", lambda: re.search(r"(a)+b", "aab")
    yield "fullmatch.none", lambda: re.fullmatch(r"\d+", "12x")
    yield "compile.bytes.unbalanced", lambda: _compile_ok(rb"(oops")


PROGRAMS = {
    "log_hunter": {"fn": prog_log_hunter,
                   "key_pattern": LOG_KEY_PATTERN, "key_flags": 0},
    "field_extractor": {"fn": prog_field_extractor,
                        "key_pattern": FIELD_KEY_PATTERN, "key_flags": 0},
    "error_paths": {"fn": prog_error_paths,
                    "key_pattern": None, "key_flags": 0},
}

# --------------------------------------------------------------------------
# THE R60 corpus (AC-3-1) lives in programs.py — that module IS the corpus
# definition; the three programs above are infra self-test subjects only.
# programs.py programs take the ``re`` MODULE as a parameter (stock, or the
# same module patched in place by pyro.install()); the legacy programs above
# close over this module's global ``re`` instead.  Adapt and merge.
# --------------------------------------------------------------------------
import programs as _r60_corpus


def _bind_re(fn):
    return lambda: fn(re)


PROGRAMS.update({
    _name: {"fn": _bind_re(_meta["fn"]),
            "key_pattern": _meta["key_pattern"],
            "key_flags": _meta["key_flags"]}
    for _name, _meta in _r60_corpus.REGISTRY.items()
})


# --------------------------------------------------------------------------
# Harness.
# --------------------------------------------------------------------------
def _emit(rec):
    sys.stdout.write(json.dumps(rec) + "\n")


def cmd_corpus(program, mode):
    prog = PROGRAMS[program]
    installed = (mode == "installed")
    key_pat, key_flags = prog["key_pattern"], prog["key_flags"]
    pre = None
    if installed:
        import pyro
        import pyro.re as _pre
        pre = _pre
        # Prime the diagnostics surface BEFORE installing.  explain()/stats()
        # lazily construct the residency manager and import the HDL estimator
        # on first use; if that first use happens while install() is active,
        # pyro's own internal `re` traffic routes through the patched module
        # and re-enters the construction path — empirically the R4a launch
        # policy then never ticks for the whole process (dispatches stop being
        # counted; see the phase3 infra report).  Priming here keeps the lazy
        # construction on genuine stock `re`, exactly as in any process that
        # touches pyro.re diagnostics before installing.  It makes NO
        # dispatches and does not tick the reuse counter (explain is R66
        # non-perturbing), so the corpus still starts cold at call 0.
        if key_pat is not None:
            pre.explain(key_pat, key_flags)
        pre.stats()
        pyro.install()

    def snap(at):
        if pre is None or key_pat is None:
            return
        e = pre.explain(key_pat, key_flags)
        s = pre.stats()
        _emit({"type": "snap", "at": at,
               "circuit_status": e["circuit_status"], "stats": s})

    transition_reached = None
    i = 0
    snap(0)  # pre-first-call snapshot: an isolated cache starts cold
    for step in prog["fn"]():
        if step is AWAIT_TIER or step[0] == "await_tier":
            if pre is not None and key_pat is not None:
                # Stream a snapshot on every STATUS CHANGE while polling, so
                # the parent sees the transition without a per-iteration flood.
                last_status = [None]

                def _on(sn, _i=i):
                    if sn["circuit_status"] != last_status[0]:
                        last_status[0] = sn["circuit_status"]
                        _emit({"type": "snap", "at": _i,
                               "circuit_status": sn["circuit_status"],
                               "stats": sn["stats"]})

                transition_reached, _last = phase3_support.poll_tier(
                    pre, key_pat, key_flags, on_snapshot=_on)
            continue
        label, thunk = step
        rec = {"type": "call", "i": i, "label": label}
        try:
            rec["value"] = _json_safe(thunk())
        except Exception as e:  # per-call exception IS the output (R60)
            rec["exc"] = _canon_exc(e)
        _emit(rec)
        i += 1
        snap(i)

    done = {
        "type": "done",
        "mode": mode,
        "program": program,
        "n_calls": i,
        # Positive stock-mode evidence: pyro was never even imported.
        "pyro_in_sys_modules": "pyro" in sys.modules,
        "transition_reached": transition_reached,
        "stats": None,
        "explain_key": None,
        "native_router": None,
    }
    if installed:
        import pyro
        done["stats"] = pre.stats()
        if key_pat is not None:
            done["explain_key"] = pre.explain(key_pat, key_flags)
        done["native_router"] = pyro.native_router()
        pyro.uninstall()
    _emit(done)
    sys.stdout.flush()
    return 0


_COMMANDS = {"corpus": cmd_corpus}


def main(argv):
    return _COMMANDS[argv[1]](*argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
