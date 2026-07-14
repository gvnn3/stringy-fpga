"""AC-3-1 — the R60 transparency-regression suite over the real-program corpus.

    "With pyro.install(), the transparency-regression suite (R60) passes on a
     corpus of real re-using programs: identical outputs and exceptions vs.
     stock re, including patterns that transition tiers mid-run." (R36, R60)

The corpus itself is defined in ``programs.py`` (R60 does not enumerate it;
that module IS the corpus definition).  For EVERY corpus program this file
runs the program twice in isolated out-of-process workers — once under stock
``re`` (pyro never imported) and once under ``pyro.install()`` — and compares
the FULL per-call output sequence (values AND exceptions; both sides jsonify'd
because findall's tuples cross the worker JSON boundary as lists; exceptions
compare by type AND str(e) AND, for re.error, msg/pos/pattern).

The NON-VACUOUS core: a corpus whose every call stays pinned to fallback
(subjects < 64 KiB and reuse < 32, R51 step 4) would compare equal trivially.
``r60_log_scan`` is therefore driven across a GENUINE mid-run tier transition
(PYRO_N_SYNTH=2 via worker env — R4a knob; >= 64 KiB subjects; mock toolchain,
the default — trap 7: these tests certify the dispatch/stats machinery, not
Vivado) and this file POSITIVELY asserts, from test code and never from inside
the corpus program, that circuit_status moved cold -> warm/resident mid-run,
that stats() shows synth_launched > 0, and that per-call outputs match BOTH
before and after the transition point.

Also covered here: R31 (install() must NOT add explain/stats/prewarm/... to
the ``re`` namespace) and uninstall-restores-by-identity (R34).

Synthesis NEVER runs inside this pytest process (R63e): every corpus run is a
subprocess bound to an isolated PYRO_CACHE_DIR (tmp_path); the in-process
tests below make zero regex dispatches while installed.  No new env knobs are
introduced (PYRO_N_SYNTH and PYRO_TEST_SYNTH_TIMEOUT are already registered in
conftest._ENV_KEYS and popped by the worker-env builders; both reach workers
via extra_env only).
"""
import re as stdre

import pytest

import phase3_support as p3
import programs
from phase1_support import jsonify
from phase3_workers import _canon_exc, _json_safe

# The transition vehicle rides the R4a launch knob: launch background
# synthesis on the 2nd eligible dispatch (worker env only, never os.environ).
_TRANSITION_ENV = {"PYRO_N_SYNTH": 2}

NON_TRANSITION = tuple(sorted(n for n in programs.REGISTRY
                              if n not in programs.TRANSITION_PROGRAMS))

# Every program shape AC-3-1 demands of the corpus, by registry name.
_REQUIRED_PROGRAMS = {
    "r60_log_scan",           # compile-once log loop, >= 64 KiB (transition)
    "r60_tokenizer",          # tokenizer
    "r60_config_parser",      # config parser
    "r60_sub_callable",       # re.sub with callable replacement
    "r60_walrus_scan",        # if m := ... / while m := ...
    "r60_finditer_dispatch",  # finditer driving groupdict dispatch
    "r60_error_flow",         # except re.error program flow
    "r60_ac17_shapes",        # AC-1-7 bug-derived shapes
}


# --------------------------------------------------------------------------
# Worker-run fixtures (module-scoped: results are pure JSON data; no pyro
# state crosses tests).
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def corpus_pairs(tmp_path_factory):
    """(stock, installed) worker runs for every NON-transition program,
    default env: small subjects / low reuse keep these pinned to fallback,
    which is exactly the R3/R29 'indistinguishable' regime R60 also covers."""
    root = tmp_path_factory.mktemp("ac31_corpus")
    pairs = {}
    for name in NON_TRANSITION:
        pairs[name] = (
            p3.run_corpus(name, "stock", cache_dir=root / name / "stock"),
            p3.run_corpus(name, "installed", cache_dir=root / name / "pyro"),
        )
    return pairs


@pytest.fixture(scope="module")
def transition_pair(tmp_path_factory):
    """(stock, installed) runs of the tier-transition vehicle, installed side
    under PYRO_N_SYNTH=2 so the mock-toolchain synthesis launches mid-run."""
    root = tmp_path_factory.mktemp("ac31_transition")
    stock = p3.run_corpus("r60_log_scan", "stock", cache_dir=root / "stock")
    installed = p3.run_corpus("r60_log_scan", "installed",
                              cache_dir=root / "pyro",
                              extra_env=_TRANSITION_ENV)
    return stock, installed


# ==========================================================================
# 0. The corpus definition itself.
# ==========================================================================
class TestCorpusDefinition:
    def test_registry_covers_every_required_program_shape(self):
        assert _REQUIRED_PROGRAMS <= set(programs.REGISTRY), (
            f"corpus lost required programs: "
            f"{sorted(_REQUIRED_PROGRAMS - set(programs.REGISTRY))}")
        for name, meta in programs.REGISTRY.items():
            assert callable(meta["fn"]), name

    def test_transition_vehicle_is_registered_and_big(self):
        assert programs.TRANSITION_PROGRAMS, "no tier-transition vehicle"
        assert "r60_log_scan" in programs.TRANSITION_PROGRAMS
        assert len(programs.BIG_LOG) >= programs.S_MIN, (
            "transition vehicle's subject fell below the R51 step-4 size arm")
        assert programs.REGISTRY["r60_log_scan"]["key_pattern"], (
            "transition vehicle must name its key pattern for explain() snaps")

    def test_programs_are_deterministic_callables_on_stock_re(self):
        """Each program is a deterministic callable from the re module to the
        FULL per-call output sequence (run twice in-process on STOCK re; pyro
        is not involved, so nothing synthesis-shaped touches this process)."""
        for name, meta in programs.REGISTRY.items():
            a = programs.run_program(meta["fn"], stdre,
                                     canon=_json_safe, canon_exc=_canon_exc)
            b = programs.run_program(meta["fn"], stdre,
                                     canon=_json_safe, canon_exc=_canon_exc)
            assert a == b, f"{name} is nondeterministic"
            assert len(a) >= 5, f"{name} is trivially small ({len(a)} calls)"

    def test_corpus_really_exercises_exceptions(self):
        """Non-vacuity of the exception surface: the error-flow program both
        CATCHES re.error itself (recording str/msg/pos/pattern) and lets
        exceptions propagate to the harness."""
        seq = programs.run_program(programs.REGISTRY["r60_error_flow"]["fn"],
                                   stdre, canon=_json_safe,
                                   canon_exc=_canon_exc)
        caught = [r for r in seq
                  if "value" in r and isinstance(r["value"], dict)
                  and r["value"].get("ok") is False]
        assert len(caught) >= 3, "except-re.error flow caught too few errors"
        for rec in caught:
            v = rec["value"]
            assert v["msg"] and v["pos"] is not None and v["pattern"], v
        propagated = [r for r in seq if "exc" in r]
        assert len(propagated) >= 2, "no exceptions propagated to the harness"
        assert any(e["exc"]["type"].endswith((".error", ".PatternError"))
                   for e in propagated)


# ==========================================================================
# 1. R60 core: identical FULL per-call sequences for every corpus program.
# ==========================================================================
class TestTransparencyRegression:
    @pytest.mark.parametrize("name", NON_TRANSITION)
    def test_full_call_sequence_identical(self, corpus_pairs, name):
        stock, installed = corpus_pairs[name]
        # The stock side really is stock: pyro was never even imported.
        assert stock["done"]["pyro_in_sys_modules"] is False
        # The installed side really ran the shim (worker installs + reports).
        assert installed["done"]["stats"] is not None, (
            f"{name}: installed worker reported no stats — install() not run?")
        assert stock["done"]["n_calls"] == installed["done"]["n_calls"] > 0
        p3.assert_call_sequences_identical(stock, installed, label=name)

    def test_installed_side_actually_dispatched_through_pyro(self, corpus_pairs):
        """Positive evidence the equality above is not comparing stock against
        stock: every installed run's R52 dispatch counters ticked."""
        for name, (_, installed) in corpus_pairs.items():
            stats = installed["done"]["stats"]
            assert stats["total"] > 0, (
                f"{name}: installed run made no pyro dispatches: {stats}")

    def test_multigroup_findall_crossed_json_boundary_as_lists(self, corpus_pairs):
        """Trap 3 evidence: the corpus produces multi-group findall tuples and
        they were compared as lists on BOTH sides."""
        stock, installed = corpus_pairs["r60_finditer_dispatch"]
        for res in (stock, installed):
            rec = next(c for c in res["calls"] if c["label"] == "last.markers")
            assert rec["value"] and isinstance(rec["value"][0], list)


# ==========================================================================
# 2. The NON-VACUOUS core: a genuine mid-run tier transition (trap 2).
# ==========================================================================
class TestMidRunTierTransition:
    def test_sequences_identical_across_the_whole_run(self, transition_pair):
        stock, installed = transition_pair
        assert stock["done"]["pyro_in_sys_modules"] is False
        p3.assert_call_sequences_identical(stock, installed,
                                           label="r60_log_scan")

    def test_transition_positively_observed_from_test_code(self, transition_pair):
        """cold at call 0 -> warm/resident strictly mid-run, synthesis
        launched AND completed (mock toolchain), calls on both sides of the
        flip — all asserted HERE, never from inside the corpus program."""
        _, installed = transition_pair
        first_hot_at = p3.assert_tier_transition(installed,
                                                 label="r60_log_scan")
        # PYRO_N_SYNTH=2: the launch cannot have fired before the 2nd
        # eligible (>= 64 KiB single-match) dispatch.
        assert first_hot_at >= 2, (
            f"transition at call {first_hot_at}, before the R4a threshold "
            "(PYRO_N_SYNTH=2) could possibly have been crossed")
        # The worker's own await-poll agreed the mock synthesis completed.
        assert installed["done"]["transition_reached"] is True
        # And the final public surfaces concur (R31/R52/R66).
        assert installed["done"]["explain_key"]["circuit_status"] in p3.HOT_TIERS
        stats = installed["done"]["stats"]
        assert stats["synth_launched"] > 0
        assert stats["synth_succeeded"] > 0

    def test_outputs_identical_before_AND_after_the_transition(self, transition_pair):
        """AC-3-1's mid-run clause, made explicit: split both call sequences
        at the first warm/resident snapshot and require non-empty, identical
        halves on each side of it."""
        stock, installed = transition_pair
        first_hot_at = next(s["at"] for s in installed["snaps"]
                            if s["circuit_status"] in p3.HOT_TIERS)
        for phase, keep in (("pre", lambda c: c["i"] < first_hot_at),
                            ("post", lambda c: c["i"] >= first_hot_at)):
            sub_stock = [c for c in stock["calls"] if keep(c)]
            sub_inst = [c for c in installed["calls"] if keep(c)]
            assert sub_stock and sub_inst, (
                f"no {phase}-transition calls — transition not mid-run")
            p3.assert_call_sequences_identical(
                {"calls": sub_stock}, {"calls": sub_inst},
                label=f"r60_log_scan.{phase}-transition")

    def test_post_transition_engine_actually_left_fallback(self, transition_pair):
        """The transition was consequential, not cosmetic: dispatches were
        served by the model/hardware engine, not 100% fallback (R51/R52)."""
        _, installed = transition_pair
        stats = installed["done"]["stats"]
        served_accelerated = stats.get("hardware", 0) + stats.get("model", 0)
        assert served_accelerated > 0, (
            f"every dispatch fell back — the tier flip changed nothing: {stats}")

    def test_multigroup_findall_over_big_subject_matches(self, transition_pair):
        """Trap 3 on the transition vehicle itself: the 3-group findall over
        the >= 64 KiB log matched, pre AND post transition."""
        stock, installed = transition_pair
        for label in ("findall.groups.pre", "findall.groups.post"):
            s = next(c for c in stock["calls"] if c["label"] == label)
            i = next(c for c in installed["calls"] if c["label"] == label)
            assert s["value"], f"{label}: corpus found nothing — subject broken"
            assert len(s["value"][0]) == 3, "expected 3-group tuples-as-lists"
            assert jsonify(s["value"]) == jsonify(i["value"])


# ==========================================================================
# 3. Interposition contract: R31 namespace purity + identity restore (R34).
#    In-process, but makes ZERO regex dispatches while installed — nothing
#    can tick a reuse counter, so nothing synthesis-shaped runs here (R63e).
# ==========================================================================
_PATCHED_NAMES = ("compile", "search", "match", "fullmatch", "findall",
                  "finditer", "sub", "subn", "split", "purge")
_PYRO_ONLY_NAMES = ("explain", "stats", "prewarm", "native_router")


class TestInterpositionContract:
    def test_install_does_not_pollute_re_namespace_R31(self):
        import pyro
        for n in _PYRO_ONLY_NAMES:
            assert not hasattr(stdre, n), f"re.{n} exists before install()"
        pyro.install()
        try:
            assert pyro.is_installed()
            # Non-vacuity: install really rebound the module attributes...
            for n in _PATCHED_NAMES:
                mod = getattr(getattr(stdre, n), "__module__", "")
                assert mod.startswith("pyro"), (
                    f"re.{n} not patched by install(): module {mod!r}")
            # ...yet added none of the PYRO-specific surface (R31).
            for n in _PYRO_ONLY_NAMES:
                assert not hasattr(stdre, n), (
                    f"R31 violation: install() added re.{n}")
        finally:
            pyro.uninstall()

    def test_uninstall_restores_stock_functions_by_identity(self):
        import pyro
        originals = {n: getattr(stdre, n) for n in _PATCHED_NAMES}
        for n, fn in originals.items():
            assert not getattr(fn, "__module__", "").startswith("pyro"), (
                f"re.{n} already patched at test start — isolation broken")
        pyro.install()
        try:
            changed = [n for n in _PATCHED_NAMES
                       if getattr(stdre, n) is not originals[n]]
            assert set(changed) == set(_PATCHED_NAMES), (
                f"install() left stock bindings for: "
                f"{sorted(set(_PATCHED_NAMES) - set(changed))}")
        finally:
            pyro.uninstall()
        for n, fn in originals.items():
            assert getattr(stdre, n) is fn, (
                f"uninstall() did not restore re.{n} BY IDENTITY (R34)")
        assert not pyro.is_installed()
