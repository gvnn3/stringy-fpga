# PYRO Phase 1c — C ABI 2.0.0 native runtime (L3, model transport binding).
#
# Targets:
#   make lib        -> build/libpyro_rt.so   (the shared library the Python
#                      ctypes glue pyro/_native.py loads)
#   make abi-check  -> compile + run a tiny program asserting
#                      pyro_abi_version() == 0x00020000 (R37/R38, ABI 2.0.0)
#   make valgrind   -> run the C ABI conformance harness under valgrind, which
#                      drives the full open/generate/synth/status/load/scan/free
#                      lifecycle and must be leak-/error-clean (R43/R44)
#   make clean

CC       ?= cc
CFLAGS   ?= -Wall -Werror -std=c11 -Iinclude
LDLIBS   ?= -lpthread
BUILD    := build

# Test-only compile flag: exposes the debug seams (pyro_ctx_debug_*) that the
# conformance / harness-contract tests drive.  It is applied to the test library
# and the valgrind harness, but NOT to the frozen-ABI `abi-check` build, so the
# stable/production ABI surface never exports the test mutators (R37/R38).
TESTFLAGS := -DPYRO_TESTING

LIB      := $(BUILD)/libpyro_rt.so
SRC      := src/pyro_rt.c

# --- native routing extension (Phase 3, R3b/R3c) --------------------------
# `pyro._fast` is the CPython interposition extension that serves the §8 R51
# routing decision in compiled code, reached by a DIRECT C call (no ctypes: one
# ctypes hop costs 175.9 ns, 4.5x the whole R3b budget).  It is OPTIONAL: with
# no extension present, pyro falls back to the pure-Python router and everything
# still works (R3a governs; R3b honestly SKIPs).  Built IN-TREE because the test
# suite imports `pyro` straight from the source directory.
PYTHON     ?= python3
EXT_SUFFIX := $(shell $(PYTHON) -c 'import sysconfig;print(sysconfig.get_config_var("EXT_SUFFIX"))')
# sysconfig rather than python3-config: a venv interpreter has no python3-config.
PYINC      := $(shell $(PYTHON) -c 'import sysconfig;print("-I"+sysconfig.get_paths()["include"]+" -I"+sysconfig.get_paths()["platinclude"])')
EXT        := pyro/_fast$(EXT_SUFFIX)
EXT_SRC    := src/pyro_ext.c src/pyro_route.c
GEN_HDR    := $(BUILD)/include/pyro_thresholds.h

.PHONY: all lib abi-check valgrind ext clean

all: lib abi-check ext

# --- shared library (position-independent, C11, warnings-as-errors) --------
# Built with $(TESTFLAGS) because its only consumers today are the ABI /
# harness-contract conformance tests, which need the debug seams.  A production
# build would omit $(TESTFLAGS); `abi-check` compiles the runtime without it to
# prove the frozen ABI is self-contained and excludes the test mutators.
lib: $(LIB)

$(LIB): $(SRC) include/pyro_rt.h | $(BUILD)
	$(CC) $(CFLAGS) $(TESTFLAGS) -fPIC -shared $(SRC) -o $@ $(LDLIBS)

# --- ABI version conformance (AC-0-8 lineage; now asserts 2.0.0) -----------
# The check program is generated so the only committed C sources are the
# header and the runtime.  It links the real runtime (not a stub).
abi-check: $(BUILD)/abi_check
	@$(BUILD)/abi_check

$(BUILD)/abi_check: $(SRC) include/pyro_rt.h | $(BUILD)
	@printf '%s\n' \
	  '#include <stdio.h>' \
	  '#include <stdlib.h>' \
	  '#include "pyro_rt.h"' \
	  'int main(void) {' \
	  '    uint32_t v = pyro_abi_version();' \
	  '    printf("pyro_abi_version = 0x%08X\n", v);' \
	  '    if (v != 0x00020000u) {' \
	  '        fprintf(stderr, "FAIL: expected 0x00020000\n");' \
	  '        return EXIT_FAILURE;' \
	  '    }' \
	  '    printf("ABI OK (2.0.0)\n");' \
	  '    return EXIT_SUCCESS;' \
	  '}' > $(BUILD)/abi_check.c
	$(CC) $(CFLAGS) $(SRC) $(BUILD)/abi_check.c -o $@ $(LDLIBS)

# --- valgrind-clean full-lifecycle conformance harness (R43/R44) -----------
valgrind: $(BUILD)/abi_conformance
	valgrind --error-exitcode=1 --leak-check=full --errors-for-leak-kinds=all \
	  $(BUILD)/abi_conformance

$(BUILD)/abi_conformance: tests/c/abi_conformance.c $(SRC) include/pyro_rt.h | $(BUILD)
	$(CC) $(CFLAGS) $(TESTFLAGS) tests/c/abi_conformance.c $(SRC) -o $@ $(LDLIBS)

# --- pyro._fast: the native routing hot path (R3b/R3c, AC-3-3) ------------
# -O2 is load-bearing: the shipped ratio is ~1.10x against a 1.15x bound.
# -Werror stays here (and in CI) but NOT in setup.py -- a user's compiler must
# never fail an install on a warning.
ext: $(EXT)

$(EXT): $(EXT_SRC) include/pyro_route.h $(GEN_HDR)
	$(CC) -O2 -std=c11 -Wall -Werror -fPIC -shared \
	  -Iinclude -I$(BUILD)/include $(PYINC) $(EXT_SRC) -o $@

# The ONE literal for S_min / N_reuse lives in pyro/_thresholds.py; the C never
# carries its own copy (anti-drift).
$(GEN_HDR): pyro/_thresholds.py scripts/gen_route_config.py | $(BUILD)
	@mkdir -p $(BUILD)/include
	$(PYTHON) scripts/gen_route_config.py --out $@

$(BUILD):
	@mkdir -p $(BUILD)

clean:
	@rm -rf $(BUILD)
	@rm -f pyro/_fast*.so
