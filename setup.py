"""Packaging for PYRO.

The only thing here that matters beyond the pure-Python package is
``pyro._fast`` — the native routing hot path (R3b/R3c).  It is declared
``optional=True``, which is load-bearing: on a box with no C toolchain the
extension silently fails to build and the pure-Python package installs and
works.  With no extension, ``PyroPattern`` is the pure-Python class, routing is
correct but ~4.5x stock, R3a (absolute <= 2 us) governs and R3b records
SKIP-with-reason.
"""
from __future__ import annotations

import os
import sys

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext as _build_ext

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "scripts"))


class build_ext(_build_ext):
    """Generate build/include/pyro_thresholds.h before compiling.

    The thresholds have exactly one source of truth (pyro/_thresholds.py); the
    C reads a header generated from it, so a threshold edit can never leave the
    native router routing on a stale S_min.
    """

    def run(self):
        import gen_route_config

        gen_route_config.main([
            "--src", os.path.join(HERE, "pyro", "_thresholds.py"),
            "--out", os.path.join(HERE, "build", "include", "pyro_thresholds.h"),
        ])
        super().run()


setup(
    name="pyro",
    version="0.0.1",
    packages=["pyro", "pyro.hdl", "pyro.synth", "pyro.snort"],
    cmdclass={"build_ext": build_ext},
    ext_modules=[
        Extension(
            "pyro._fast",
            sources=["src/pyro_ext.c", "src/pyro_route.c",
                     "src/pyro_dataplane.c"],
            include_dirs=["include", "build/include"],
            extra_compile_args=["-O2", "-std=c11", "-pthread"],
            extra_link_args=["-pthread"],
            optional=True,   # no compiler => pure-Python install, still correct
        ),
    ],
)
