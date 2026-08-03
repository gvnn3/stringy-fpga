"""Self-tests for the SHARED Phase-3 test infrastructure.

These tests prove the infrastructure itself is non-vacuous BEFORE any AC-3-*
test is written on top of it:

  * the oracle booby-trap fix — expectations come from STOCK re even while the
    stdlib ``re`` module's functions are patched (by pyro.install() or by a
    deliberately broken stand-in), and a broken patched build now FAILS the
    oracle instead of vacuously passing;
  * the corpus worker machinery — a corpus program run under stock re and
    under pyro.install() in isolated out-of-process workers produces identical
    per-call sequences, AND a genuine mid-run tier transition is positively
    observed (cold at call 0, warm/resident later, synth_launched > 0) from
    TEST code;
  * the comparators themselves — each assertion helper is fed a doctored
    input and must RAISE (a comparator that cannot fail proves nothing).

Everything synthesis-shaped runs out of process against isolated
PYRO_CACHE_DIRs on the MOCK toolchain (the default; these tests exercise the
dispatch/stats machinery, not Vivado).
"""
import copy
import re as stdre

import pytest

import oracle
import phase3_support as p3
from phase3_workers import PROGRAMS  # registry only; importing runs nothing


# ==========================================================================
# 1. Oracle booby-trap regression (trap 1).
# ==========================================================================
class TestOracleStockCapture:
    def test_broken_patched_re_now_fails_the_oracle(self, monkeypatch):
        """THE booby-trap regression.  Patch re.search to a broken function —
        standing in for a broken interposed build, exactly what install()
        does mechanically (rebinding the module attribute).  Before the fix,
        assert_equivalent looked expectations up on ``re`` at call time, so
        expected == actual == garbage and the oracle passed VACUOUSLY.  With
        stock callables captured at import, the oracle must now FAIL.

        The patch is scoped with monkeypatch.context() and the message check
        uses `in`, NOT pytest.raises(match=...): the match= path itself calls
        re.search — the very function this test breaks."""
        with monkeypatch.context() as m:
            m.setattr(stdre, "search", lambda p, s, f=0: None)
            with pytest.raises(AssertionError) as ei:
                oracle.assert_equivalent(stdre, r"abc", "xxabcxx",
                                         label="booby-trap")
        assert "search mismatch" in str(ei.value)

    def test_broken_patched_findall_now_fails_the_oracle(self, monkeypatch):
        """Same regression for a non-Match-returning API (findall)."""
        with monkeypatch.context() as m:
            m.setattr(stdre, "findall", lambda p, s, f=0: ["bogus"])
            with pytest.raises(AssertionError) as ei:
                oracle.assert_equivalent(stdre, r"a(b)c", "zabcz",
                                         label="booby-trap")
        assert "findall mismatch" in str(ei.value)

    def test_stock_table_is_stock_while_pyro_installed(self):
        """Positive capture proof under the REAL shim: while pyro.install() is
        active, re.search/... ARE pyro's functions, yet the oracle's captured
        table still holds the distinct, stock-``re``-module callables."""
        import pyro
        pyro.install()
        try:
            assert pyro.is_installed()
            for name in ("search", "match", "fullmatch", "findall",
                         "finditer", "sub", "subn", "split"):
                patched = getattr(stdre, name)
                captured = oracle.stock(name)
                # The patched attribute is pyro's...
                assert getattr(patched, "__module__", "").startswith("pyro"), (
                    f"re.{name} not patched by install(): {patched!r}")
                # ...and the oracle's captured callable is NOT it, and is stock.
                assert captured is not patched
                assert captured.__module__ == "re", (
                    f"oracle captured a non-stock re.{name}: {captured!r}")
        finally:
            pyro.uninstall()

    def test_oracle_passes_against_real_installed_pyro(self):
        """The compatibility half: with expectations now provably stock, the
        REAL installed pyro still satisfies the oracle (R16 byte-identical),
        including a hybrid (grouped) pattern and an eligible plain one."""
        import pyro
        pyro.install()
        try:
            oracle.assert_equivalent(stdre, r"(a)(b)|c", "zzabzc",
                                     label="installed-hybrid")
            oracle.assert_equivalent(stdre, r"cat|dog", "a dog and a cat",
                                     label="installed-alt")
        finally:
            pyro.uninstall()


# ==========================================================================
# 2. Corpus worker machinery (traps 2, 3, 4, 6, 7).
# ==========================================================================
# Worker runs are pure data (JSON); module-scoped so several tests share one
# pair of runs without re-synthesizing.  No pyro state crosses tests.
@pytest.fixture(scope="module")
def log_hunter_pair(tmp_path_factory):
    root = tmp_path_factory.mktemp("p3_log_hunter")
    stock = p3.run_corpus("log_hunter", "stock", cache_dir=root / "stock")
    installed = p3.run_corpus(
    "log_hunter",
    "installed",
    cache_dir=root / "pyro",
    extra_env={
        "PYRO_N_SYNTH": 2}) # R4a knob: launch on the 2nd eligible dispatch
    return stock, installed


@pytest.fixture(scope="module")
def error_paths_pair(tmp_path_factory):
    root = tmp_path_factory.mktemp("p3_error_paths")
    stock = p3.run_corpus("error_paths", "stock", cache_dir=root / "stock")
    installed = p3.run_corpus(
    "error_paths",
    "installed",
     cache_dir=root / "pyro")
    return stock, installed


class TestCorpusMachinery:
    def test_program_registry_documents_key_patterns(self):
        for name, meta in PROGRAMS.items():
            assert callable(meta["fn"]), name
            assert "key_pattern" in meta and "key_flags" in meta, name

    def test_stock_worker_never_imports_pyro(self, log_hunter_pair):
        stock, _ = log_hunter_pair
        assert stock["done"]["pyro_in_sys_modules"] is False
        # no explain()/stats() surface in stock mode
        assert stock["snaps"] == []

    def test_log_hunter_sequences_identical(self, log_hunter_pair):
        stock, installed = log_hunter_pair
        p3.assert_call_sequences_identical(
    stock, installed, label="log_hunter")

    def test_log_hunter_mid_run_tier_transition(self, log_hunter_pair):
        """The AC-3-1 non-vacuity core, asserted from TEST code (never from the
        corpus program): cold at call 0, warm/resident later, calls on both
        sides of the transition, synth launched AND completed (mock)."""
        _, installed = log_hunter_pair
        first_hot_at = p3.assert_tier_transition(installed, label="log_hunter")
        # The transition happened after the >=64 KiB pre-await calls...
        assert first_hot_at >= 2, (
            "transition before the R4a threshold could fire")
        # ...and the worker's own await-poll agreed it completed.
        assert installed["done"]["transition_reached"] is True

    def test_log_hunter_exercises_multigroup_findall(self, log_hunter_pair):
        """Trap 3 evidence: the corpus really produces multi-group findall
        results (tuples -> JSON lists) and they matched across modes."""
        stock, installed = log_hunter_pair
        for res in (stock, installed):
            rec = next(c for c in res["calls"]
                       if c["label"] == "findall.groups.big.pre")
            assert rec["value"], "findall found nothing — corpus subject broken"
            first = rec["value"][0]
            assert isinstance(first, list) and len(first) == 2, (
                f"expected 2-group tuple-as-list, got {first!r}")

    def test_field_extractor_reuse_arm_transitions(self, tmp_path):
        """The REUSE (>=32 calls, small subjects) arm of R51 step 4, end to
        end: identical sequences plus a positively-asserted transition."""
        stock = p3.run_corpus("field_extractor", "stock",
                              cache_dir=tmp_path / "stock")
        installed = p3.run_corpus("field_extractor", "installed",
                                  cache_dir=tmp_path / "pyro",
                                  extra_env={"PYRO_N_SYNTH": 2})
        p3.assert_call_sequences_identical(stock, installed,
                                           label="field_extractor")
        first_hot_at = p3.assert_tier_transition(installed,
                                                 label="field_extractor")
        # Reuse arm: nothing can launch until the 33rd call crosses N_reuse.
        assert first_hot_at >= 33, (
            f"transition at call {first_hot_at} — before the reuse gate "
            "(N_reuse=32) could possibly have been crossed")

    def test_error_paths_sequences_identical(self, error_paths_pair):
        stock, installed = error_paths_pair
        p3.assert_call_sequences_identical(
    stock, installed, label="error_paths")

    def test_error_paths_actually_raised_and_captured_fields(
        self, error_paths_pair):
        """Non-vacuity of the exception comparator: the corpus really raised,
        and re.error was captured with msg/pos/pattern (trap 10), in BOTH
        modes, with a genuine matched pair on at least one compile error."""
        stock, installed = error_paths_pair
        for res, mode in ((stock, "stock"), (installed, "installed")):
            excs = [c["exc"] for c in res["calls"] if "exc" in c]
            assert len(excs) >= 5, f"{mode}: corpus raised too few exceptions"
            re_errors = [
    e for e in excs if e["type"].endswith(
        (".error", ".PatternError"))]
            assert re_errors, f"{mode}: no re.error captured"
            for e in re_errors:
                assert e["str"], f"{mode}: empty str(e): {e}"
                assert e["msg"], f"{mode}: re.error captured without .msg: {e}"
                assert e["pos"] is not None, (
                    f"{mode}: re.error without .pos: {e}")
                assert e["pattern"] is not None, (
                    f"{mode}: re.error without .pattern: {e}")
            type_errors = [
    e for e in excs if e["type"] == "builtins.TypeError"]
            assert type_errors, f"{mode}: no TypeError captured"


# ==========================================================================
# 3. The comparators must be able to FAIL (negative controls).
# ==========================================================================
class TestComparatorsCanFail:
    def test_sequence_comparator_catches_value_divergence(
        self, log_hunter_pair):
        stock, installed = log_hunter_pair
        doctored = copy.deepcopy(installed)
        victim = doctored["calls"][0]
        victim["value"] = {"__match__": True, "span": [999, 1000],
                           "group0": "WRONG", "groups": [],
                           "spans": [[999, 1000]], "lastindex": None,
                           "lastgroup": None, "groupdict": {}}
        with pytest.raises(AssertionError, match="diverged under install"):
            p3.assert_call_sequences_identical(
                stock, doctored, label="doctored")

    def test_sequence_comparator_catches_exception_str_divergence(
        self, error_paths_pair):
        """Trap 10: same exception TYPE but different str(e)/.msg must fail."""
        stock, installed = error_paths_pair
        doctored = copy.deepcopy(installed)
        victim = next(c for c in doctored["calls"] if "exc" in c)
        victim["exc"]["str"] = victim["exc"]["str"] + " (subtly different)"
        with pytest.raises(AssertionError, match="diverged under install"):
            p3.assert_call_sequences_identical(
                stock, doctored, label="doctored")

    def test_transition_assertion_catches_pinned_fallback_corpus(
        self, log_hunter_pair):
        """Trap 2's exact failure mode, synthesized: strip every warm/resident
        snapshot (a corpus that never left cold) — assert_tier_transition must
        REFUSE it, not pass it."""
        _, installed = log_hunter_pair
        doctored = copy.deepcopy(installed)
        doctored["snaps"] = [s for s in doctored["snaps"]
                             if s["circuit_status"] not in p3.HOT_TIERS]
        for s in doctored["snaps"]:
            s["circuit_status"] = "cold"
        with pytest.raises(AssertionError, match="tier never reached"):
            p3.assert_tier_transition(doctored, label="doctored")

    def test_transition_assertion_requires_calls_after_transition(
        self, log_hunter_pair):
        """Mid-run means calls AFTER the flip too: truncate the call list at
        the transition point and the assertion must fail."""
        _, installed = log_hunter_pair
        doctored = copy.deepcopy(installed)
        first_hot_at = next(s["at"] for s in doctored["snaps"]
                            if s["circuit_status"] in p3.HOT_TIERS)
        doctored["calls"] = [c for c in doctored["calls"]
                             if c["i"] < first_hot_at]
        with pytest.raises(AssertionError, match="AFTER the tier transition"):
            p3.assert_tier_transition(doctored, label="doctored")

    def test_transition_assertion_requires_synth_counters(
        self, log_hunter_pair):
        _, installed = log_hunter_pair
        doctored = copy.deepcopy(installed)
        doctored["done"]["stats"]["synth_launched"] = 0
        with pytest.raises(AssertionError, match="synth_launched==0"):
            p3.assert_tier_transition(doctored, label="doctored")
