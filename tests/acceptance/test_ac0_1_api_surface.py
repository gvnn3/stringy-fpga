"""AC-0-1: `import pyro.re as re` provides every R26 symbol with signatures
matching stock re; pyro.re.error is re.error.  (R26, R30)
"""

import inspect
import re as stdre

import pytest

import pyro.re as pre

MODULE_FUNCS = [
    "compile", "search", "match", "fullmatch", "findall", "finditer",
    "sub", "subn", "split", "escape", "purge",
]

FLAG_NAMES = [
    "A", "ASCII", "I", "IGNORECASE", "M", "MULTILINE",
    "S", "DOTALL", "X", "VERBOSE", "U", "UNICODE",
]


@pytest.mark.parametrize("name", MODULE_FUNCS)
def test_module_function_present(name):
    """R26: pyro.re exposes each documented module-level function."""
    assert hasattr(pre, name), f"pyro.re is missing {name}"
    assert callable(getattr(pre, name)), f"pyro.re.{name} is not callable"


@pytest.mark.parametrize("name", MODULE_FUNCS)
def test_module_function_signature_matches_stock(name):
    """R26: signatures match CPython re (Python 3.10+).

    Compares the introspectable signature of pyro.re.<fn> to stock re.<fn>.
    Spec R26 enumerates the exact signatures; stock re is the oracle.
    """
    exp = str(inspect.signature(getattr(stdre, name)))
    act = str(inspect.signature(getattr(pre, name)))
    assert act == exp, f"pyro.re.{name}{act} != re.{name}{exp}"


@pytest.mark.parametrize("name", FLAG_NAMES)
def test_flag_constants_alias_stock(name):
    """R26: flag constants A/I/M/S/X and long forms (+U/UNICODE no-op) are
    aliases of re's values."""
    assert hasattr(pre, name), f"pyro.re is missing flag {name}"
    assert getattr(pre, name) == getattr(stdre, name), (
        f"pyro.re.{name}={getattr(pre, name)!r} != re.{name}={getattr(stdre, name)!r}"
    )


def test_flag_short_long_equal():
    """R26: short and long flag aliases denote the same value."""
    assert pre.I == pre.IGNORECASE == stdre.IGNORECASE
    assert pre.M == pre.MULTILINE == stdre.MULTILINE
    assert pre.S == pre.DOTALL == stdre.DOTALL
    assert pre.A == pre.ASCII == stdre.ASCII
    assert pre.X == pre.VERBOSE == stdre.VERBOSE


def test_error_is_re_error():
    """AC-0-1 / R26: pyro.re.error IS re.error (same exception type)."""
    assert pre.error is stdre.error


def test_unicode_flag_is_noop_for_str():
    """R26: U/UNICODE accepted as a no-op for str patterns."""
    assert pre.search(r"\w+", "abc", pre.U).group(0) == "abc"
    assert pre.findall(r"\w+", "a b c", pre.UNICODE) == ["a", "b", "c"]


def test_escape_matches_stock():
    """R26: escape() behaves like re.escape."""
    for s in ["a.b*c", "1+1=2", "(x)[y]{z}", "plain"]:
        assert pre.escape(s) == stdre.escape(s)


def test_purge_callable():
    """R26: purge() exists and is callable without raising."""
    pre.purge()


def test_compile_raises_re_error_like_stock():
    """R30: pyro.re.compile raises re.error iff re.compile would."""
    bad_patterns = ["(", "[", "a{2,1}", "*abc", "(?P<>x)", r"\1"]
    for pat in bad_patterns:
        stock_err = None
        try:
            stdre.compile(pat)
        except stdre.error as e:
            stock_err = e
        if stock_err is not None:
            with pytest.raises(stdre.error):
                pre.compile(pat)
        else:
            # stock accepted it; pyro must not reject it
            pre.compile(pat)
