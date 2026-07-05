"""AC-0-5: install()/uninstall() patch and restore stock re with identical
observable behavior; PYRO_DISABLE at a sampling point forces fallback; env flags
are sampled only at import / install / uninstall / refresh_env; a variable
mutated mid-process takes effect only after the next sampling point.
(R33-R36, R35a-R35d, R60)

Routing observability lever (spec-airtight): the ONLY publicly guaranteed signal
that a call took the fallback path is R29 — on the fallback path the returned
object IS a genuine re.Match, so isinstance(m, re.Match) is True.  The model path
MAY return non-isinstance wrappers (R36a) but is not required to, so we only ever
assert isinstance==True to prove "fell back"; we never assert ==False.  A
>= S_min HW-eligible subject would otherwise take the model path (R51 step 6, no
device in Phase 0), so observing it fall back proves a disable/routing effect.
stats() corroborates where available but its exact keys are not spec-pinned.
"""

import os
import subprocess
import sys
import textwrap

import pytest

import pyro
import pyro.re as pre
import oracle

LARGE = "x" * (oracle.S_MIN + 32)  # >= S_min (R2)

RESTORE_NAMES = [
    "search", "match", "fullmatch", "findall", "finditer",
    "sub", "subn", "split", "compile", "escape", "purge", "error",
    "Pattern", "Match",
]


def _large_subject(needle):
    return LARGE + needle


# --------------------------------------------------------------------------
# install()/uninstall() round-trip (R34).
# --------------------------------------------------------------------------
def test_install_uninstall_roundtrip_restores_re():
    """R34: install() patches re; uninstall() fully restores every documented
    attribute to its original object identity."""
    import re as stdre

    orig = {n: getattr(stdre, n) for n in RESTORE_NAMES}
    assert not hasattr(stdre, "explain"), "stock re must not expose explain pre-install"

    pyro.install()
    try:
        # error type MUST be unchanged so callers' except clauses still work (R26).
        assert stdre.error is orig["error"]
        # R31: explain MUST NOT appear on the standard re namespace when interposing.
        assert not hasattr(stdre, "explain"), "patched re must not expose explain (R31)"
        # R34: install patches the module functions so calls route through PYRO.
        # (Interpretation of "patch ... so subsequent re.search route through PYRO"
        # as function replacement.)
        assert stdre.search is not orig["search"], "install() did not patch re.search"
    finally:
        pyro.uninstall()

    for n in RESTORE_NAMES:
        assert getattr(stdre, n) is orig[n], f"uninstall() did not restore re.{n}"
    assert not hasattr(stdre, "explain"), "uninstall() left explain on re (R31)"


def test_installed_re_transparent_results():
    """R33/R36: with install(), documented re behavior is unchanged (results and
    exceptions identical to stock)."""
    import re as stdre

    # Compute oracle values from stock re BEFORE patching.
    cases = [
        lambda re: re.findall(r"\d+", "a1 b22 c333"),
        lambda re: re.sub(r"\s+", "_", "a  b   c"),
        lambda re: re.subn(r"o", "0", "foobar"),
        lambda re: re.split(r"[,;]", "a,b;c,d"),
        lambda re: re.match(r"(\w+)@(\w+)", "u@h").groups(),
        lambda re: re.search(r"(?P<n>\d+)", "id42").groupdict(),
    ]
    expected = [c(stdre) for c in cases]

    pyro.install()
    try:
        got = [c(stdre) for c in cases]
    finally:
        pyro.uninstall()
    assert got == expected


# --------------------------------------------------------------------------
# R60 transparency regression on a corpus of re-using snippets.
# --------------------------------------------------------------------------
def test_transparency_regression_snippets():
    """R60/R36: a corpus of stock-re snippets produces identical outputs and
    exceptions under pyro.install() vs stock re.  (Avoids isinstance, R36a.)"""
    import re as stdre

    def snip_error(re):
        try:
            re.compile("(")
            return "no-error"
        except re.error as e:
            return ("re.error", type(e).__name__)

    snippets = [
        lambda re: re.findall(r"\w+", "the quick brown fox"),
        lambda re: [m.span() for m in re.finditer(r"\d", "a1b2c3")],
        lambda re: re.sub(r"(\w)(\w)", r"\2\1", "abcdef"),
        lambda re: re.subn(r"a", "A", "banana"),
        lambda re: re.split(r"\W+", "a, b; c.d"),
        lambda re: bool(re.fullmatch(r"[a-z]+", "hello")),
        lambda re: re.compile(r"x+").findall("xxaxxx"),
        snip_error,
    ]
    expected = [s(stdre) for s in snippets]

    pyro.install()
    try:
        got = [s(stdre) for s in snippets]
    finally:
        pyro.uninstall()
    assert got == expected


# --------------------------------------------------------------------------
# PYRO_DISABLE forces fallback at a sampling point (R35/R35a/R35c, R51 step 1).
# --------------------------------------------------------------------------
def test_pyro_disable_forces_fallback_via_refresh():
    """R35/R35c/R51-step-1: PYRO_DISABLE=1 sampled via refresh_env() forces even
    a >= S_min HW-eligible call (which would otherwise use the model) to the
    fallback path — observable as a genuine re.Match (R29)."""
    import re as stdre

    os.environ["PYRO_DISABLE"] = "1"
    pyro.refresh_env()
    m = pre.search("DISNEEDLE", _large_subject("DISNEEDLE"))
    assert m is not None
    assert isinstance(m, stdre.Match), "PYRO_DISABLE=1 must route to genuine fallback (R29)"


def test_pyro_disable_sampled_at_install():
    """R35a case 2: PYRO_DISABLE set before install() is sampled at install; the
    patched re then forces fallback (genuine object)."""
    import re as stdre

    os.environ["PYRO_DISABLE"] = "1"
    pyro.install()
    try:
        m = stdre.search("INSTNEEDLE", _large_subject("INSTNEEDLE"))
        assert m is not None
        assert isinstance(m, stdre.Match)
    finally:
        pyro.uninstall()


def test_import_time_env_sampling_subprocess():
    """R35a case 1 / R35b: PYRO_DISABLE=1 present before process start is sampled
    at package import and behaves as if read per-call (a >= S_min HW-eligible
    call falls back -> genuine re.Match).  Hermetic subprocess."""
    prog = textwrap.dedent(
        """
        import re, pyro.re as pre
        subj = "x" * (64 * 1024 + 32) + "SUBNEEDLE"
        m = pre.search("SUBNEEDLE", subj)
        print(m is not None and isinstance(m, re.Match))
        """
    )
    env = dict(os.environ)
    env["PYRO_DISABLE"] = "1"
    env.pop("PYRO_FORCE_MODEL", None)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out = subprocess.run(
        [sys.executable, "-c", prog], capture_output=True, text=True, env=env, cwd=root,
    )
    assert out.returncode == 0, f"subprocess failed: {out.stderr}"
    assert out.stdout.strip() == "True", f"import-time PYRO_DISABLE not honored: {out.stdout!r}"


# --------------------------------------------------------------------------
# Deferred sampling: mid-process os.environ mutation does not change routing
# until the next sampling point (R35a/R35b).
# --------------------------------------------------------------------------
def test_env_mutation_deferred_until_sampling_point():
    """R35b: with PYRO_DISABLE=1 sampled (disabled), unsetting os.environ
    WITHOUT reaching a sampling point must NOT change routing — the call still
    falls back (genuine re.Match).  If the implementation illegally read env
    per-call, a >= S_min call would take the model path and (for a non-isinstance
    wrapper) fail this assertion."""
    import re as stdre

    os.environ["PYRO_DISABLE"] = "1"
    pyro.refresh_env()  # sampling point: disabled
    m1 = pre.search("DEFN1", _large_subject("DEFN1"))
    assert isinstance(m1, stdre.Match)

    # Mutate env WITHOUT any sampling point.
    os.environ.pop("PYRO_DISABLE", None)
    m2 = pre.search("DEFN2", _large_subject("DEFN2"))
    assert isinstance(m2, stdre.Match), \
        "mid-process env unset must not take effect before next sampling point (R35b)"


def test_refresh_env_applies_new_disable_value():
    """R35d: refresh_env() re-samples os.environ and applies the new value —
    setting PYRO_DISABLE=1 then calling refresh_env() forces subsequent calls to
    fallback (genuine re.Match)."""
    import re as stdre

    os.environ.pop("PYRO_DISABLE", None)
    pyro.refresh_env()  # enabled baseline

    os.environ["PYRO_DISABLE"] = "1"
    pyro.refresh_env()  # apply disable
    m = pre.search("REFNEEDLE", _large_subject("REFNEEDLE"))
    assert isinstance(m, stdre.Match), "refresh_env() must apply newly-set PYRO_DISABLE (R35d)"


def test_refresh_env_idempotent_and_returns_none():
    """R35d: refresh_env() is idempotent and returns None."""
    assert pyro.refresh_env() is None
    assert pyro.refresh_env() is None


# --------------------------------------------------------------------------
# Determinism of results regardless of routing/env (R53).
# --------------------------------------------------------------------------
def test_result_identical_disable_vs_enabled():
    """R53: the observable result is independent of the routing path; only
    isinstance/timing/stats may differ.  Compare values (not identity)."""
    import re as stdre

    pattern, subject = r"(\w+)-(\d+)", _large_subject("tok-42")
    exp = oracle.canon_match(stdre.search(pattern, subject))

    os.environ["PYRO_DISABLE"] = "1"
    pyro.refresh_env()
    got_disabled = oracle.canon_match(pre.search(pattern, subject))

    os.environ.pop("PYRO_DISABLE", None)
    pyro.refresh_env()
    got_enabled = oracle.canon_match(pre.search(pattern, subject))

    assert got_disabled == exp
    assert got_enabled == exp
