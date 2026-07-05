# PYRO Phase 0 — C ABI conformance harness (AC-0-8).
#
# `make abi-check` compiles the frozen header + stub with -Wall -Werror and
# runs a tiny program that prints pyro_abi_version(), asserting it equals
# 0x00010000 (R37/R38).

CC      ?= cc
CFLAGS  ?= -Wall -Werror -std=c11 -Iinclude
BUILD   := build

.PHONY: abi-check clean

abi-check: $(BUILD)/abi_check
	@$(BUILD)/abi_check

# The check program is generated here so the only committed C sources are the
# frozen header and the version stub.
$(BUILD)/abi_check: include/pyro_rt.h src/pyro_rt_stub.c | $(BUILD)
	@printf '%s\n' \
	  '#include <stdio.h>' \
	  '#include <stdlib.h>' \
	  '#include "pyro_rt.h"' \
	  'int main(void) {' \
	  '    uint32_t v = pyro_abi_version();' \
	  '    printf("pyro_abi_version = 0x%08X\n", v);' \
	  '    if (v != 0x00010000u) {' \
	  '        fprintf(stderr, "FAIL: expected 0x00010000\n");' \
	  '        return EXIT_FAILURE;' \
	  '    }' \
	  '    printf("ABI OK (1.0.0)\n");' \
	  '    return EXIT_SUCCESS;' \
	  '}' > $(BUILD)/abi_check.c
	$(CC) $(CFLAGS) src/pyro_rt_stub.c $(BUILD)/abi_check.c -o $@

$(BUILD):
	@mkdir -p $(BUILD)

clean:
	@rm -rf $(BUILD)
