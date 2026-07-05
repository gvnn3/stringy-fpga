"""AC-0-8: the frozen pyro_rt.h compiles and pyro_abi_version() returns
0x00010000.  (R37, R38)

This test compiles include/pyro_rt.h + src/pyro_rt_stub.c with `cc -Wall -Werror`
(subprocess), links a tiny main derived from the §7.3 signatures, runs it, and
asserts the packed ABI version.  It also textually verifies the header declares
the §7.3 symbols (symbol list derived from the spec, not from reading the impl).
Skips with a reason if no C compiler is available.
"""

import os
import shutil
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HEADER = os.path.join(REPO_ROOT, "include", "pyro_rt.h")
STUB = os.path.join(REPO_ROOT, "src", "pyro_rt_stub.c")
INCLUDE_DIR = os.path.join(REPO_ROOT, "include")

ABI_EXPECTED = 0x00010000  # R37: ABI 1.0.0 == MAJOR<<16 | MINOR<<8 | PATCH

# §7.3 symbols the header MUST declare (R38, R37, R39-R42).
REQUIRED_SYMBOLS = [
    # functions
    "pyro_abi_version", "pyro_ctx_open", "pyro_ctx_close", "pyro_compile",
    "pyro_prog_free", "pyro_prog_load", "pyro_scan", "pyro_caps_get",
    # opaque / struct / enum type names
    "pyro_ctx", "pyro_prog", "pyro_status", "pyro_encoding", "pyro_match",
    "pyro_caps",
    # enum constants
    "PYRO_OK", "PYRO_E_UNSUPPORTED", "PYRO_E_CAPACITY", "PYRO_E_DEVICE",
    "PYRO_E_INVALID", "PYRO_E_NOMEM", "PYRO_E_TIMEOUT",
    "PYRO_ENC_BYTES", "PYRO_ENC_UTF8",
]

CC = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")


def test_header_present():
    """R38: the frozen header exists at include/pyro_rt.h."""
    assert os.path.isfile(HEADER), f"missing header {HEADER}"


@pytest.mark.parametrize("symbol", REQUIRED_SYMBOLS)
def test_header_declares_symbol(symbol):
    """R38/R37: header text declares each §7.3 symbol (textual presence check)."""
    with open(HEADER, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    assert symbol in text, f"header does not declare §7.3 symbol {symbol!r}"


@pytest.mark.skipif(CC is None, reason="no C compiler (cc/gcc/clang) on PATH")
def test_header_and_stub_compile_wall_werror(tmp_path):
    """R37/R38/AC-0-8: header + stub compile clean under -Wall -Werror, link, and
    the program reports pyro_abi_version() == 0x00010000."""
    assert os.path.isfile(STUB), f"missing stub {STUB}"

    main_c = tmp_path / "main.c"
    main_c.write_text(
        "#include <stdint.h>\n"
        "#include <stdio.h>\n"
        '#include "pyro_rt.h"\n'
        "int main(void) {\n"
        "    uint32_t v = pyro_abi_version();\n"
        '    printf("%u\\n", (unsigned)v);\n'
        "    return v == 0x00010000u ? 0 : 2;\n"
        "}\n"
    )

    exe = tmp_path / "pyro_abi_probe"
    compile_cmd = [
        CC, "-Wall", "-Werror", "-std=c11",
        "-I", INCLUDE_DIR,
        str(STUB), str(main_c),
        "-o", str(exe),
    ]
    cproc = subprocess.run(compile_cmd, capture_output=True, text=True)
    assert cproc.returncode == 0, (
        f"compilation failed under -Wall -Werror:\n{cproc.stderr}"
    )

    rproc = subprocess.run([str(exe)], capture_output=True, text=True)
    assert rproc.returncode == 0, (
        f"pyro_abi_version() != 0x00010000 (stdout={rproc.stdout!r})"
    )
    assert rproc.stdout.strip() == str(ABI_EXPECTED), (
        f"expected {ABI_EXPECTED} got {rproc.stdout.strip()!r}"
    )
