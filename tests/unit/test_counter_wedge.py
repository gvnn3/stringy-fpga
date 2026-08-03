"""AC-3-1 counter-wedge regression (R4a launch policy survives install-time
lazy construction).

The wedge: in a fresh interpreter where stdlib ``dataclasses``/``pickle`` have
not yet been imported (true for bare ``import pyro``; NOT true under pytest,
hence the subprocess below), the sequence

    pyro.install()  ->  pyro.re.explain(<eligible>)  ->  eligible dispatches

initiates the process's FIRST import of ``pyro.synth`` from explain(), while
the stdlib ``re`` surface is patched.  Mid-import, ``dataclasses`` (first
imported by pyro.synth.manifest) compiles/matches through the *patched* re;
at the 33rd match the reuse gate routes a nested dispatch into
``_route._consult_residency``, which used to cache the partially initialized
``pyro.synth.residency`` module.  That nested import attempt then failed
(circular import) and importlib evicted the module object — pinning a corpse
without ``get_manager`` in ``_route._residency`` forever.  Every subsequent
eligible dispatch died in a swallowed AttributeError: ``note_eligible_dispatch``
never ran, stats()['synth_launched'] stayed 0, explain()['circuit_status']
stayed 'cold' for the whole process.

The fix caches ``_route._residency`` only once the module is fully initialized
(``get_manager`` present), so a partial/evicted module can never be pinned.
This test drives ONLY the public surface (install/uninstall, stdlib re,
pyro.re.explain/stats) in the exact trigger order and asserts the launch
policy still ticks; it FAILS on the unfixed code.
"""
import os
import subprocess
import sys
import textwrap

_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.abspath(__file__))))


def test_launch_policy_survives_post_install_explain_trigger(tmp_path):
    prog = textwrap.dedent(
        """
        import os, sys
        os.environ["PYRO_CACHE_DIR"] = %r
        os.environ["PYRO_N_SYNTH"] = "5"
        os.environ["PYRO_TOOLCHAIN"] = "mock"   # hermetic vs leaked vivado env
        os.environ.pop("PYRO_VIVADO", None)

        import pyro
        import pyro.re as pre
        import re

        # The trigger is only armed while the stdlib detonators are NOT yet
        # imported (bare `import pyro` must keep it that way): if this fires,
        # the test has gone vacuous and must be re-plumbed, not skipped.
        assert "dataclasses" not in sys.modules, (
            "trigger disarmed: dataclasses pre-imported")

        pyro.install()
        try:
            # THE trigger ordering: first import of pyro.synth is initiated by
            # explain(), post-install, before the first eligible dispatch.
            assert pre.explain("abc[0-9]+x")["eligible"] is True
            # >= S_MIN so the model path runs on every call
            subj = "y" * (64 * 1024) + "abc123x"
            for _ in range(12):
                assert re.search("abc[0-9]+x", subj).group(0) == "abc123x"

            from pyro.synth import residency
            mgr = residency.get_manager()
            mgr.drain(10.0)
            mgr.poll()

            s = pre.stats()
            status = pre.explain("abc[0-9]+x")["circuit_status"]
            import pyro._route as rt
            corpse = (rt._residency is not None
                      and not hasattr(rt._residency, "get_manager"))
            print("SYNTH_LAUNCHED", s["synth_launched"])
            print("CIRCUIT_STATUS", status)
            print("CORPSE", corpse)
        finally:
            pyro.uninstall()
        """
    ) % str(tmp_path / "cache")
    out = subprocess.run( [sys.executable, "-c", prog],
                         capture_output=True, text=True, cwd=_ROOT, )
    assert out.returncode == 0, f"trigger subprocess failed:\n{out.stderr}"
    got = dict(line.split(" ", 1) for line in out.stdout.splitlines() if line)
    # Unfixed: SYNTH_LAUNCHED 0 / CIRCUIT_STATUS cold / CORPSE True.
    assert got["CORPSE"] == "False", (
        "dead partial residency module pinned in _route")
    assert got["SYNTH_LAUNCHED"] == "1", (
        "R4a launch policy never ticked (wedged)")
    assert got["CIRCUIT_STATUS"] in ("warm", "resident"), got["CIRCUIT_STATUS"]
