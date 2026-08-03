"""HDL generator (L2, spec §4/§7.4, R9/R11/R45-R50; AC-1-3 generator clause).

Lowers a HW-eligible pattern's byte automaton (:mod:`pyro.hdl.automaton`) into a
**synthesizable per-pattern Verilog-2001 circuit** that implements the fixed
harness contract of §7.4: the normative CSR register block (R45), the on-chip
performance counters (R45a), the baked circuit-identity block (R47a),
result-ring semantics (R47), and START/DONE/OVF single-issue control (R48).
The datapath is a generic one-hot NFA recognizer; no vendor primitives and no
timing-closure effort are attempted (Task-5 brief) —
that is Phase-2 work.  The circuit software model (:mod:`pyro._circuit_model`)
provides the behavioral reference; this RTL is the structural lowering that a
real synthesis flow (Phase 2) would consume.

Determinism (AC-1-3).  For a fixed ``(pattern, flags, generator_version)`` the
emitted text is **byte-identical**: state numbering, edge ordering and byte-set
ranges are all canonical (see :mod:`pyro.hdl.automaton`), and the emitter uses
no
timestamps, hashes-of-object-ids, or dict-iteration order.
"""

from __future__ import annotations

import re._constants as _c
from typing import List, NamedTuple, Tuple

from . import automaton as _auto
from . import identity as _identity
from . import estimator as _estimator

# Generator / harness contract versions (packed MAJOR<<16|MINOR<<8|PATCH).
# 2.1.0: R45a on-chip perf counters (CYCLES/BYTES CSRs, spec §14 2.3.0 entry).
# Both bump together: the counters change the emitted RTL *and* the harness
# register map, so identity hashes (R47a) and cache keys (R4) roll over.
# 2.2.0: R78.11 in-band PERF read-out (spec §14 2.4.0 entry).  The RP-child
# wrapper's wire protocol gains PERF_REQUEST/PERF_REPLY and the emitted engine
# RTL rolls its HARNESS_VER localparam; both bump together again so pre-2.2.0
# artifacts (whose children drop PERF_REQUEST, R78.4) are stale (R47b).
# 2.3.0: the eps/assert closure loop runs closure_passes(au) rounds instead of
# NSTATES.  Emitted text changes (loop bound only; semantics identical — the
# extra rounds were provably no-ops), so identities/caches roll over per R47b.
# Without it Vivado elaborates NSTATES x edges redundant read-modify-writes and
# a 151-state datapath_bytes=8 circuit does not converge in 2 h; with it the
# same circuit synthesizes in 2 min 3 s to 1,358 LUTs.
GENERATOR_VERSION = 0x00020300
HARNESS_VERSION = 0x00020300
DATAPATH_BYTES = 1  # default bytes/cycle (R42 datapath_bytes)

# P2b (specs/p2-dataplane.md): supported widened datapaths.  N > 1 emits the
# cascaded-stage engine (see _emit_rtl_wide); the N == 1 emission is the
# original single-byte engine, byte-identical to pre-P2b output so existing
# identities, caches, and the flashed child stay valid.
SUPPORTED_DATAPATH_BYTES = (1, 2, 4, 8, 16)

ID_MAGIC = 0x5059524F  # "PYRO" (R45 offset 0x0000)

# Zero-width assertion -> Verilog condition wire (see the emitted anchor block).
# The (scoped) MULTILINE flag has already been baked into each ^/$ edge at
# lowering time (AT_BEGINNING/AT_END vs. their *_LINE variants, see
# automaton._resolve_at), so this map is a straight, flag-independent lookup.
_AT_EXPR = {
    _c.AT_BEGINNING: "at_sob",
    _c.AT_BEGINNING_STRING: "at_sob",
    _c.AT_BEGINNING_LINE: "at_bol",
    _c.AT_END: "at_eob",
    _c.AT_END_STRING: "at_eob",
    _c.AT_END_LINE: "at_eol",
    _c.AT_BOUNDARY: "word_boundary",
    _c.AT_UNI_BOUNDARY: "word_boundary",
    _c.AT_NON_BOUNDARY: "(~word_boundary)",
    _c.AT_UNI_NON_BOUNDARY: "(~word_boundary)",
}


class GeneratedCircuit(NamedTuple):
    """The artifact produced by :func:`generate` for one HW-eligible pattern."""

    pattern: object
    flags: int
    enc: int
    generator_version: int
    harness_version: int
    datapath_bytes: int
    automaton: _auto.Automaton
    circ_id: Tuple[int, int, int, int]  # CIRC_ID0..3 (R47a identity block)
    circ_flags: int                     # CIRC_FLAGS (R45 0x0028)
    num_patterns: int
    resources: dict
    rtl: str                            # synthesizable Verilog-2001 text
    # R19c over-approximation classes (sorted)
    over_approx: Tuple[str, ...]
    # R19c coarse false-positive-rate estimate
    estimated_fp_rate: float

    # -- convenience accessors for the harness identity block (R47a) --------
    @property
    def pattern_hash16(self) -> bytes:
        return b"".join(w.to_bytes(4, "little") for w in self.circ_id)


def _ranges_expr(ranges: Tuple[Tuple[int, int], ...]) -> str:
    """Verilog boolean for ``in_data`` falling in any of ``ranges``."""
    terms = []
    for lo, hi in ranges:
        if lo == hi:
            terms.append(f"(in_data == 8'd{lo})")
        else:
            terms.append(f"(in_data >= 8'd{lo} && in_data <= 8'd{hi})")
    if not terms:
        return "1'b0"
    return " || ".join(terms)


def _emit_rtl(au: _auto.Automaton, circ_id, circ_flags, num_patterns) -> str:
    N = au.n_states
    _kpasses = closure_passes(au)
    L: List[str] = []
    add = L.append

    caps0 = (DATAPATH_BYTES & 0xFFFF) | (
        (_estimator.PR_PARTITIONS & 0xFFFF) << 16)

    add("// Auto-generated by pyro.hdl.generator — do not edit.")
    add("// Per-pattern PYRO recognizer implementing the §7.4 harness"
        " contract.")
    add(f"// generator_version=0x{GENERATOR_VERSION:08X} "
        f"harness_version=0x{HARNESS_VERSION:08X}")
    add("`timescale 1ns/1ps")
    add("module pyro_circuit (")
    add("    input  wire        clk,")
    add("    input  wire        rst_n,")
    add("    // AXI-Lite-style CSR window (§7.4 R45)")
    add("    input  wire [15:0] csr_addr,")
    add("    input  wire [31:0] csr_wdata,")
    add("    input  wire        csr_write,")
    add("    output reg  [31:0] csr_rdata,")
    add("    // input byte stream (DMA abstracted behind the harness, R48)")
    add("    input  wire        in_valid,")
    add("    input  wire [7:0]  in_data,")
    add("    input  wire        in_last,")
    add("    // result-ring write port (24-byte entries, R47)")
    add("    output reg         res_wr,")
    add("    output reg  [63:0] res_start,")
    add("    output reg  [63:0] res_end,")
    add("    output reg  [31:0] res_pattern_id,")
    add("    output reg  [31:0] res_flags")
    add(");")
    add("")
    add("    // --- normative harness constants (R45) ---")
    add(f"    localparam [31:0] MAGIC       = 32'h{ID_MAGIC:08X};")
    add(f"    localparam [31:0] HARNESS_VER = 32'h{HARNESS_VERSION:08X};")
    add(f"    localparam [31:0] GEN_VER     = 32'h{GENERATOR_VERSION:08X};")
    add(f"    localparam [31:0] CAPS0       = 32'h{caps0:08X};")
    add(f"    localparam [31:0] CIRC_ID0    = 32'h{circ_id[0]:08X};")
    add(f"    localparam [31:0] CIRC_ID1    = 32'h{circ_id[1]:08X};")
    add(f"    localparam [31:0] CIRC_ID2    = 32'h{circ_id[2]:08X};")
    add(f"    localparam [31:0] CIRC_ID3    = 32'h{circ_id[3]:08X};")
    add(f"    localparam [31:0] CIRC_FLAGS  = 32'h{circ_flags:08X};")
    add(f"    localparam integer NSTATES    = {N};")
    add(f"    localparam [31:0] START_STATE = 32'd{au.start};")
    add(f"    localparam [31:0] ACCEPT_STATE= 32'd{au.accept};")
    add("")
    add("    // --- CSR registers (R45) ---")
    add("    reg  [31:0] ctrl, status;")
    add("    reg  [31:0] in_addr_lo, in_addr_hi, in_len;")
    add("    reg  [31:0] out_addr_lo, out_addr_hi, out_cap, out_count;")
    add("    reg  [31:0] irq_enable, irq_status;")
    add("    wire        ctrl_start = ctrl[0];")
    add("    wire        ctrl_reset = ctrl[1];")
    add("    reg         ctrl_start_d;  // 2.2.0: START is EDGE-triggered "
        "(see below)")
    add("")
    add("    // --- automaton state (one-hot NFA, R9 lowering) ---")
    add("    reg  [NSTATES-1:0] state_reg;   // registered active set")
    add("    reg  [NSTATES-1:0] active;      // eps/assert closure (comb)")
    add("    reg  [NSTATES-1:0] moved;       // byte-move result (comb)")
    add("    reg  [63:0] byte_index;")
    add("    reg  [63:0] cycle_count;  // R45a CYCLES: core-clock cycles "
        "while BUSY")
    add("    reg  [7:0]  prev_byte;")
    add("    reg         have_prev;")
    add("    reg         running;")
    add("    reg         finishing;   // 2.2.0: one-cycle final-accept "
        "harvest (see below)")
    add("    integer     it;")
    add("")
    add("    // --- anchor/position condition wires (§6.5) ---")
    add("    // NOTE (Phase-2 synthesis TODO): the end-of-buffer anchors are")
    add("    // structural placeholders — at_eol/at_eob are driven by in_last")
    add("    // (last cycle) rather than by exact '$'/\\Z lookahead, so the")
    add("    // model (pyro._circuit_model) is authoritative for end-anchor")
    add("    // semantics until a streaming end-lookahead datapath is built.")
    add("    wire at_sob = (byte_index == 64'd0);")
    add("    wire at_bol = at_sob || (have_prev && prev_byte == 8'h0A);")
    add("    wire at_eob = in_last;")
    add("    wire at_eol = in_last;  // Phase-2: exact $ handled by the model")
    add("    wire cur_word  = (in_data >= 8'd48 && in_data <= 8'd57) ||")
    add("                     (in_data >= 8'd65 && in_data <= 8'd90) ||")
    add("                     (in_data >= 8'd97 && in_data <= 8'd122) ||")
    add("                     (in_data == 8'd95);")
    add("    wire prev_word = have_prev && ((prev_byte >= 8'd48 && prev_byte "
        "<= 8'd57) ||")
    add("                     (prev_byte >= 8'd65 && prev_byte <= 8'd90) ||")
    add("                     (prev_byte >= 8'd97 && prev_byte <= 8'd122) ||")
    add("                     (prev_byte == 8'd95));")
    add("    wire word_boundary = cur_word ^ prev_word;")
    add("")
    add("    // --- epsilon/assertion closure (combinational relaxation) ---")
    add("    always @(*) begin")
    add("        active = state_reg;")
    # CLOSURE_PASSES, not NSTATES: the body is replicated by the loop and the
    # fixpoint is reached in closure_passes(au) rounds (see its docstring).
    add(f"        for (it = 0; it < {_kpasses}; it = it + 1) begin")
    eps_lines = _emit_closure_body(au)
    for ln in eps_lines:
        add("            " + ln)
    add("        end")
    add("    end")
    add("")
    add("    // --- byte-move: consume in_data, seed a fresh start thread ---")
    add("    always @(*) begin")
    add("        moved = {NSTATES{1'b0}};")
    add("        moved[START_STATE] = 1'b1;  // continuous search seeding")
    move_lines = _emit_move_body(au)
    for ln in move_lines:
        add("        " + ln)
    add("    end")
    add("")
    add("    // accept_hit samples the CLOSURE of the registered state, i.e. "
        "the")
    add("    // accept produced by the byte consumed in the PREVIOUS cycle "
        "(this is")
    add("    // deliberate: closure-reached accepts are caught too).  "
        "Consequently a")
    add("    // window is harvested one cycle after its final byte, when "
        "byte_index")
    add("    // already equals that byte's position + 1 == the R47 exclusive "
        "`end`;")
    add("    // and the LAST byte's accept needs the dedicated `finishing` "
        "harvest")
    add("    // cycle below (2.2.0 fix: it used to be dropped, and `end` was "
        "+1).")
    add("    wire accept_hit = active[ACCEPT_STATE];")
    add("")
    add("    // --- sequential control / datapath (R48 single-issue) ---")
    add("    always @(posedge clk) begin")
    add("        if (!rst_n) begin")
    add("            ctrl <= 32'd0; status <= 32'd0; running <= 1'b0;")
    add("            in_addr_lo <= 32'd0; in_addr_hi <= 32'd0;"
        " in_len <= 32'd0;")
    add("            out_addr_lo <= 32'd0; out_addr_hi <= 32'd0;")
    add("            out_cap <= 32'd0; out_count <= 32'd0;")
    add("            irq_enable <= 32'd0; irq_status <= 32'd0;")
    add("            state_reg <= {NSTATES{1'b0}};")
    add("            byte_index <= 64'd0; cycle_count <= 64'd0;")
    add("            prev_byte <= 8'd0; have_prev <= 1'b0;")
    add("            finishing <= 1'b0; ctrl_start_d <= 1'b0;")
    add("            res_wr <= 1'b0; res_start <= 64'd0; res_end <= 64'd0;")
    add("            res_pattern_id <= 32'd0; res_flags <= 32'd0;")
    add("        end else begin")
    add("            res_wr <= 1'b0;")
    add("            ctrl_start_d <= ctrl_start;"
        "   // START edge detect (2.2.0)")
    add("            // R45a CYCLES: count every BUSY cycle up to (not "
        "including)")
    add("            // the finishing harvest cycle — 'from the cycle after "
        "START")
    add("            // is accepted until the final input byte is"
        " consumed'.  A")
    add("            // START/RESET assignment later in this block overrides "
        "(last")
    add("            // nonblocking write wins), so clears take precedence.")
    add("            if (running && !finishing) cycle_count <= cycle_count + "
        "64'd1;")
    add("            // CSR writes (RW registers only; RO ignored)")
    add("            if (csr_write) begin")
    add("                case (csr_addr)")
    add("                    16'h0010: ctrl <= csr_wdata;")
    add("                    16'h0030: in_addr_lo <= csr_wdata;")
    add("                    16'h0034: in_addr_hi <= csr_wdata;")
    add("                    16'h0038: in_len <= csr_wdata;")
    add("                    16'h0040: out_addr_lo <= csr_wdata;")
    add("                    16'h0044: out_addr_hi <= csr_wdata;")
    add("                    16'h0048: out_cap <= csr_wdata;")
    add("                    16'h0050: irq_enable <= csr_wdata;")
    add("                    16'h0054: irq_status <= irq_status & ~csr_wdata;")
    add("                    default: ;")
    add("                endcase")
    add("            end")
    add("            if (ctrl_reset) begin")
    add("                state_reg <= {NSTATES{1'b0}};")
    add("                byte_index <= 64'd0; cycle_count <= 64'd0; have_prev "
        "<= 1'b0;")
    add("                out_count <= 32'd0; running <= 1'b0; finishing <= "
        "1'b0;")
    add("                status <= 32'd0;")
    add("            end else if (ctrl_start && !ctrl_start_d && !running) "
        "begin")
    add("                // 2.2.0: START fires on the CTRL bit0 RISING EDGE "
        "only.  As a")
    add("                // level it made the engine restart itself the cycle "
        "after DONE")
    add("                // (ctrl.START is never cleared), wiping the R45a "
        "counters and")
    add("                // reducing DONE to a single-cycle pulse — 'hold "
        "their values")
    add("                // after STATUS.DONE' (R45a) was unimplementable.  "
        "Edge-firing")
    add("                // keeps DONE and both counters stable until the "
        "next START/")
    add("                // RESET, exactly as R45a requires.")
    add("                running <= 1'b1;")
    add("                status <= 32'h0000_0001;  // BUSY")
    add("                state_reg <= {NSTATES{1'b0}};")
    add("                state_reg[START_STATE] <= 1'b1;")
    add("                byte_index <= 64'd0; cycle_count <= 64'd0;")
    add("                have_prev <= 1'b0; out_count <= 32'd0; finishing <= "
        "1'b0;")
    add("            end else if (finishing) begin")
    add("                // 2.2.0: one-cycle harvest of the FINAL byte's "
        "accept (its")
    add("                // closure is only visible in `active` the cycle "
        "after the")
    add("                // last in_valid; it used to be dropped => missed "
        "matches).")
    add("                finishing <= 1'b0;")
    add("                running <= 1'b0;")
    add("                status <= (status & ~32'h0000_0001) | 32'h0000_0002")
    add("                          | ((accept_hit && !(out_count < out_cap))")
    add("                             ? 32'h0000_0008 : 32'h0000_0000);")
    add("                irq_status <= irq_status | 32'h0000_0001;")
    add("                if (accept_hit && (out_count < out_cap)) begin")
    add("                    res_wr <= 1'b1;")
    add("                    res_start <= 64'd0;")
    add("                    res_end <= byte_index;")
    add("                    res_pattern_id <= 32'd0;")
    add("                    res_flags <= 32'd1;  // bit0 verified (advisory)")
    add("                    out_count <= out_count + 32'd1;")
    add("                end")
    add("            end else if (running && in_valid) begin")
    add("                state_reg <= moved;")
    add("                prev_byte <= in_data; have_prev <= 1'b1;")
    add("                byte_index <= byte_index + 64'd1;")
    add("                if (accept_hit) begin")
    add("                    if (out_count < out_cap) begin")
    add("                        res_wr <= 1'b1;")
    add("                        // Phase-2 synthesis TODO: bake per-thread "
        "start-offset")
    add("                        // tracking; res_start=0 here and the "
        "host/model recovers")
    add("                        // the exact start via the R18/R19 hybrid "
        "re-run.")
    add("                        res_start <= 64'd0;")
    add("                        // byte_index already == accepting-byte "
        "position + 1")
    add("                        // == the R47 exclusive end (2.2.0 fix: was "
        "+1 too big).")
    add("                        res_end <= byte_index;")
    add("                        res_pattern_id <= 32'd0;")
    add("                        res_flags <= 32'd1;  // bit0 verified "
        "(advisory)")
    add("                        out_count <= out_count + 32'd1;")
    add("                    end else begin")
    add("                        status <= status | 32'h0000_0008;  // OVF")
    add("                    end")
    add("                end")
    add("                if (in_last) finishing <= 1'b1;  // harvest + DONE "
        "next cycle")
    add("            end")
    add("        end")
    add("    end")
    add("")
    add("    // --- CSR read mux (R45 offsets; all RO/RW registers) ---")
    add("    always @(*) begin")
    add("        case (csr_addr)")
    add("            16'h0000: csr_rdata = MAGIC;")
    add("            16'h0004: csr_rdata = HARNESS_VER;")
    add("            16'h0008: csr_rdata = CAPS0;")
    add("            16'h000C: csr_rdata = GEN_VER;")
    add("            16'h0010: csr_rdata = ctrl;")
    add("            16'h0014: csr_rdata = status;")
    add("            16'h0018: csr_rdata = CIRC_ID0;")
    add("            16'h001C: csr_rdata = CIRC_ID1;")
    add("            16'h0020: csr_rdata = CIRC_ID2;")
    add("            16'h0024: csr_rdata = CIRC_ID3;")
    add("            16'h0028: csr_rdata = CIRC_FLAGS;")
    add("            16'h002C: csr_rdata = 32'd0;")
    add("            16'h0030: csr_rdata = in_addr_lo;")
    add("            16'h0034: csr_rdata = in_addr_hi;")
    add("            16'h0038: csr_rdata = in_len;")
    add("            16'h0040: csr_rdata = out_addr_lo;")
    add("            16'h0044: csr_rdata = out_addr_hi;")
    add("            16'h0048: csr_rdata = out_cap;")
    add("            16'h004C: csr_rdata = out_count;")
    add("            16'h0050: csr_rdata = irq_enable;")
    add("            16'h0054: csr_rdata = irq_status;")
    add("            // R45a perf counters; BYTES is the byte_index feed "
        "counter.")
    add("            16'h0058: csr_rdata = cycle_count[31:0];")
    add("            16'h005C: csr_rdata = cycle_count[63:32];")
    add("            16'h0060: csr_rdata = byte_index[31:0];")
    add("            16'h0064: csr_rdata = byte_index[63:32];")
    add("            default:  csr_rdata = 32'd0;")
    add("        endcase")
    add("    end")
    add("endmodule")
    add("")
    return "\n".join(L)


def closure_passes(au: _auto.Automaton) -> int:
    """Relaxation passes the emitted closure body needs to reach its fixpoint.

    ``_emit_closure_body`` applies every eps/assert edge once, in a fixed
    order.  Propagation therefore advances one edge per pass along a chain
    only when the chain follows that order; a chain that runs *against* it
    needs another pass.  The emitted loop must run at least this many times.

    The relaxation is monotone and additive — each rule fires on the presence
    of one source bit, independently — so the closure of a union of start sets
    is the union of the closures, and the worst case over all initial ``active``
    values is attained at some single start state.  Assertion conditions are
    treated as always-true: a disabled edge can only shrink the reachable set,
    never lengthen a chain, so this is an upper bound over all runtime
    condition valuations.

    Emitting ``NSTATES`` passes (as pre-2.3.0 did) is always sufficient but
    replicates the body ``NSTATES`` times; Vivado unrolls it, elaborates
    ``NSTATES * edges`` blocking read-modify-writes of one ``NSTATES``-bit
    vector, and the redundancy proof is superlinear — a 151-state 45-edge
    circuit at ``datapath_bytes=8`` did not converge in two hours.  The real
    depth over the Snort community corpus is <= 4.
    """
    seq: List[Tuple[int, int]] = []
    for s in range(au.n_states):
        for e in au.sorted_edges(s):
            if e.kind in (_auto.E_EPS, _auto.E_ASSERT):
                seq.append((s, e.target))
    if not seq:
        return 1
    worst = 1
    for start in range(au.n_states):
        active = bytearray(au.n_states)
        active[start] = 1
        changed_passes = 0
        while True:
            before = bytes(active)
            for src, dst in seq:
                if active[src]:
                    active[dst] = 1
            if bytes(active) == before:
                break
            changed_passes += 1
        worst = max(worst, changed_passes)
    return worst


def _emit_closure_body(au: _auto.Automaton) -> List[str]:
    """Relaxation body: propagate eps/assert edges into ``active`` once."""
    lines: List[str] = []
    for s in range(au.n_states):
        for e in au.sorted_edges(s):
            if e.kind == _auto.E_EPS:
                lines.append(
                    f"if (active[{s}]) active[{e.target}] = 1'b1;")
            elif e.kind == _auto.E_ASSERT:
                cond = _AT_EXPR.get(int(e.payload))
                if cond is None:
                    # OVER-APPROX: unknown assertion treated as always-true.
                    lines.append(
                        f"if (active[{s}]) active[{e.target}] = 1'b1;")
                else:
                    lines.append(
    f"if (active[{s}] && {cond}) active[{
        e.target}] = 1'b1;")
    if not lines:
        lines.append("active = active;  // no epsilon/assert edges")
    return lines


def _emit_move_body(au: _auto.Automaton) -> List[str]:
    """Byte-move body: per byte edge, gate on source active + byte
    match."""
    lines: List[str] = []
    for src, ranges, dst in au.byte_edges():
        expr = _ranges_expr(ranges)
        lines.append(f"if (active[{src}] && ({expr})) moved[{dst}] = 1'b1;")
    if not lines:
        lines.append("moved = moved;  // no byte edges")
    return lines


def _rename(lines: List[str], subs: List[Tuple[str, str]]) -> List[str]:
    """Ordered token substitution over emitted body lines (P2b staging)."""
    out = []
    for ln in lines:
        for a, b in subs:
            ln = ln.replace(a, b)
        out.append(ln)
    return out


def _emit_rtl_wide(au: _auto.Automaton, circ_id, circ_flags, num_patterns,
                   n: int) -> str:
    """P2b widened engine: ``n`` bytes/cycle via ``n`` cascaded closure+move
    stages per clock (specs/p2-dataplane.md §2 P2b).

    Same §7.4 harness contract as the 1-byte engine with these deltas:

    * ``in_data`` is ``8*n`` bits (lane ``i`` = byte ``in_data[8*i +: 8]``);
      ``in_keep`` marks valid lanes.  Contract: ``in_keep`` is contiguous from
      lane 0 and may be partial ONLY on the ``in_last`` beat (the harness
      feeds full beats otherwise).  Trailing-stage state after a short final
      beat is dead (the harness RESETs before every scan), so the cascade
      always registers ``mov{n-1}``.
    * Accepts are detected IN-BEAT (closure of each stage), gated by
      ``in_keep``, and COALESCED to the highest-position accept of the beat:
      one result entry per beat, ``end`` = position of the beat's last
      accepting byte + 1.  Sound for R19 nomination: ``start`` is always 0,
      so the emitted window [0, end_max) contains every dropped window
      [0, end_i<max).  ``out_count`` therefore counts accepting BEATS on a
      wide engine (a documented v3 semantic, not comparable across widths).
    * End-of-buffer anchors keep their 1-byte-engine placeholder status (the
      model is authoritative, R7): ``$``-edges relax on the closure following
      the last kept byte of the ``in_last`` beat.
    * R45a: CYCLES counts BUSY cycles exactly as before (so on-chip CYCLES ≈
      ceil(len/n) + overhead — the R42/R45a idealized-model relation); BYTES
      advances by the beat's kept-lane count.
    """
    N = au.n_states
    _kpasses = closure_passes(au)
    L: List[str] = []
    add = L.append

    caps0 = (n & 0xFFFF) | ((_estimator.PR_PARTITIONS & 0xFFFF) << 16)

    def word_expr(b: str) -> str:
        return (f"(({b} >= 8'd48 && {b} <= 8'd57) || "
                f"({b} >= 8'd65 && {b} <= 8'd90) || "
                f"({b} >= 8'd97 && {b} <= 8'd122) || ({b} == 8'd95))")

    add("// Auto-generated by pyro.hdl.generator — do not edit.")
    add(
    f"// Per-pattern PYRO recognizer, WIDENED datapath: {n} bytes/cycle (P2b).")
    add(f"// generator_version=0x{GENERATOR_VERSION:08X} "
        f"harness_version=0x{HARNESS_VERSION:08X} datapath_bytes={n}")
    add("`timescale 1ns/1ps")
    add("module pyro_circuit (")
    add("    input  wire        clk,")
    add("    input  wire        rst_n,")
    add("    input  wire [15:0] csr_addr,")
    add("    input  wire [31:0] csr_wdata,")
    add("    input  wire        csr_write,")
    add("    output reg  [31:0] csr_rdata,")
    add(f"    input  wire        in_valid,")
    add(f"    input  wire [{8*n-1}:0] in_data,")
    add(f"    input  wire [{n-1}:0]  in_keep,")
    add("    input  wire        in_last,")
    add("    output reg         res_wr,")
    add("    output reg  [63:0] res_start,")
    add("    output reg  [63:0] res_end,")
    add("    output reg  [31:0] res_pattern_id,")
    add("    output reg  [31:0] res_flags")
    add(");")
    add("")
    add(f"    localparam [31:0] MAGIC       = 32'h{ID_MAGIC:08X};")
    add(f"    localparam [31:0] HARNESS_VER = 32'h{HARNESS_VERSION:08X};")
    add(f"    localparam [31:0] GEN_VER     = 32'h{GENERATOR_VERSION:08X};")
    add(f"    localparam [31:0] CAPS0       = 32'h{caps0:08X};")
    add(f"    localparam [31:0] CIRC_ID0    = 32'h{circ_id[0]:08X};")
    add(f"    localparam [31:0] CIRC_ID1    = 32'h{circ_id[1]:08X};")
    add(f"    localparam [31:0] CIRC_ID2    = 32'h{circ_id[2]:08X};")
    add(f"    localparam [31:0] CIRC_ID3    = 32'h{circ_id[3]:08X};")
    add(f"    localparam [31:0] CIRC_FLAGS  = 32'h{circ_flags:08X};")
    add(f"    localparam integer NSTATES    = {N};")
    add(f"    localparam [31:0] START_STATE = 32'd{au.start};")
    add(f"    localparam [31:0] ACCEPT_STATE= 32'd{au.accept};")
    add("")
    add("    reg  [31:0] ctrl, status;")
    add("    reg  [31:0] in_addr_lo, in_addr_hi, in_len;")
    add("    reg  [31:0] out_addr_lo, out_addr_hi, out_cap, out_count;")
    add("    reg  [31:0] irq_enable, irq_status;")
    add("    wire        ctrl_start = ctrl[0];")
    add("    wire        ctrl_reset = ctrl[1];")
    add("    reg         ctrl_start_d;")
    add("")
    add("    reg  [NSTATES-1:0] state_reg;")
    add("    reg  [63:0] byte_index;")
    add("    reg  [63:0] cycle_count;  // R45a CYCLES: core-clock cycles "
        "while BUSY")
    add("    reg  [7:0]  prev_byte;")
    add("    reg         have_prev;")
    add("    reg         running;")
    add("    reg         finishing;")
    add("")
    add("    // ---- input lanes ----")
    for i in range(n):
        add(f"    wire [7:0] b{i} = in_data[{8*i} +: 8];")
    add("")
    add("    // ---- per-stage anchor/position wires (§6.5; end anchors"
        " are the")
    add("    // documented 1-byte-engine placeholders — model authoritative, "
        "R7) ----")
    # last-kept lane markers
    for i in range(n):
        if i == n - 1:
            add(f"    wire lastk{i} = in_keep[{i}];")
        else:
            add(f"    wire lastk{i} = in_keep[{i}]"
                f" && ~(|in_keep[{n-1}:{i+1}]);")
    # per-lane word-char wires
    for i in range(n):
        add(f"    wire w{i} = {word_expr(f'b{i}')};")
    add("    wire wprev = have_prev && "
        + word_expr("prev_byte").replace("((", "((") + ";")
    # closure-stage anchors, j = 0..n (stage j = position byte_index + j)
    for j in range(n + 1):
        if j == 0:
            add("    wire at_sob_0 = (byte_index == 64'd0);")
            add("    wire at_bol_0 = at_sob_0 || (have_prev && prev_byte == "
                "8'h0A);")
            add("    wire at_eob_0 = 1'b0;")
            add("    wire at_eol_0 = 1'b0;")
            add("    wire wb_0 = w0 ^ wprev;")
        else:
            cur_w = f"w{j}" if j < n else "1'b0"
            add(f"    wire at_sob_{j} = 1'b0;")
            add(f"    wire at_bol_{j} = (b{j-1} == 8'h0A);")
            add(f"    wire at_eob_{j} = in_last && lastk{j-1};")
            add(f"    wire at_eol_{j} = in_last && lastk{j-1};")
            add(f"    wire wb_{j} = {cur_w} ^ w{j-1};")
    add("")
    # stage registers (combinational)
    for j in range(n + 1):
        add(f"    reg [NSTATES-1:0] act{j};")
    for i in range(n):
        add(f"    reg [NSTATES-1:0] mov{i};")
    add("")
    # closure blocks
    for j in range(n + 1):
        src = "state_reg" if j == 0 else f"mov{j-1}"
        add(f"    // ---- closure stage {j} (position byte_index+{j}) ----")
        add(f"    integer it{j};")
        add("    always @(*) begin")
        add(f"        act{j} = {src};")
        add(f"        for (it{j} = 0; it{j} < {_kpasses};"
            f" it{j} = it{j} + 1) begin")
        subs = [("active[", f"act{j}["), ("word_boundary", f"wb_{j}"),
                ("at_sob", f"at_sob_{j}"), ("at_bol", f"at_bol_{j}"),
                ("at_eob", f"at_eob_{j}"), ("at_eol", f"at_eol_{j}"),
                ("active = active;", f"act{j} = act{j};")]
        for ln in _rename(_emit_closure_body(au), subs):
            add("            " + ln)
        add("        end")
        add("    end")
    add("")
    # move blocks (keep-gated passthrough)
    for i in range(n):
        add(f"    // ---- move stage {i} (consume lane {i}) ----")
        add("    always @(*) begin")
        add(f"        mov{i} = {{NSTATES{{1'b0}}}};")
        add(f"        mov{i}[START_STATE] = 1'b1;"
            "  // continuous search seeding")
        subs = [("active[", f"act{i}["), ("moved[", f"mov{i}["),
                ("in_data", f"b{i}"), ("moved = moved;", f"mov{i} = mov{i};")]
        for ln in _rename(_emit_move_body(au), subs):
            add("        " + ln)
        add(f"        if (!in_keep[{i}]) mov{i} = act{i};"
            "  // short-beat passthrough")
        add("    end")
    add("")
    # accepts / kept-count / last-byte (combinational)
    add("    // ---- in-beat accepts, coalesced to the highest end (see "
        "header) ----")
    add(f"    wire [{n-1}:0] accv = {{")
    for i in range(n - 1, -1, -1):
        sep = "," if i > 0 else ""
        add(f"        (act{i+1}[ACCEPT_STATE] & in_keep[{i}]){sep}")
    add("    };")
    add("    reg [7:0] acc_off; reg acc_any;"
        " reg [7:0] kept_n; reg [7:0] lastb;")
    add("    integer ja;")
    add("    always @(*) begin")
    add("        acc_any = 1'b0; acc_off = 8'd0; kept_n = 8'd0; lastb = "
        "prev_byte;")
    add(f"        for (ja = 0; ja < {n}; ja = ja + 1) begin")
    add("            if (accv[ja]) begin acc_any = 1'b1;"
        " acc_off = ja[7:0]; end")
    add("            if (in_keep[ja]) kept_n = kept_n + 8'd1;")
    add("        end")
    for i in range(n):
        add(f"        if (in_keep[{i}]) lastb = b{i};")
    add("    end")
    add("")
    add("    // ---- sequential control / datapath (R48 single-issue) ----")
    add("    always @(posedge clk) begin")
    add("        if (!rst_n) begin")
    add("            ctrl <= 32'd0; status <= 32'd0; running <= 1'b0;")
    add("            in_addr_lo <= 32'd0; in_addr_hi <= 32'd0;"
        " in_len <= 32'd0;")
    add("            out_addr_lo <= 32'd0; out_addr_hi <= 32'd0;")
    add("            out_cap <= 32'd0; out_count <= 32'd0;")
    add("            irq_enable <= 32'd0; irq_status <= 32'd0;")
    add("            state_reg <= {NSTATES{1'b0}};")
    add("            byte_index <= 64'd0; cycle_count <= 64'd0;")
    add("            prev_byte <= 8'd0; have_prev <= 1'b0;")
    add("            finishing <= 1'b0; ctrl_start_d <= 1'b0;")
    add("            res_wr <= 1'b0; res_start <= 64'd0; res_end <= 64'd0;")
    add("            res_pattern_id <= 32'd0; res_flags <= 32'd0;")
    add("        end else begin")
    add("            res_wr <= 1'b0;")
    add("            ctrl_start_d <= ctrl_start;")
    add("            if (running && !finishing) cycle_count <= cycle_count + "
        "64'd1;")
    add("            if (csr_write) begin")
    add("                case (csr_addr)")
    add("                    16'h0010: ctrl <= csr_wdata;")
    add("                    16'h0030: in_addr_lo <= csr_wdata;")
    add("                    16'h0034: in_addr_hi <= csr_wdata;")
    add("                    16'h0038: in_len <= csr_wdata;")
    add("                    16'h0040: out_addr_lo <= csr_wdata;")
    add("                    16'h0044: out_addr_hi <= csr_wdata;")
    add("                    16'h0048: out_cap <= csr_wdata;")
    add("                    16'h0050: irq_enable <= csr_wdata;")
    add("                    16'h0054: irq_status <= irq_status & ~csr_wdata;")
    add("                    default: ;")
    add("                endcase")
    add("            end")
    add("            if (ctrl_reset) begin")
    add("                state_reg <= {NSTATES{1'b0}};")
    add("                byte_index <= 64'd0; cycle_count <= 64'd0; have_prev "
        "<= 1'b0;")
    add("                out_count <= 32'd0; running <= 1'b0; finishing <= "
        "1'b0;")
    add("                status <= 32'd0;")
    add("            end else if (ctrl_start && !ctrl_start_d && !running) "
        "begin")
    add("                running <= 1'b1;")
    add("                status <= 32'h0000_0001;  // BUSY")
    add("                state_reg <= {NSTATES{1'b0}};")
    add("                state_reg[START_STATE] <= 1'b1;")
    add("                byte_index <= 64'd0; cycle_count <= 64'd0;")
    add("                have_prev <= 1'b0; out_count <= 32'd0; finishing <= "
        "1'b0;")
    add("            end else if (finishing) begin")
    add("                // Wide engine: accepts were harvested IN-BEAT "
        "(incl. the")
    add("                // final byte's, via the stage closures), so this "
        "cycle")
    add("                // only completes the scan: DONE + IRQ (R48/R45a).")
    add("                finishing <= 1'b0;")
    add("                running <= 1'b0;")
    add("                status <= (status & ~32'h0000_0001) | 32'h0000_0002;")
    add("                irq_status <= irq_status | 32'h0000_0001;")
    add("            end else if (running && in_valid) begin")
    add(f"                state_reg <= mov{n-1};")
    add("                prev_byte <= lastb; have_prev <= have_prev | "
        "(|in_keep);")
    add("                byte_index <= byte_index + {56'd0, kept_n};")
    add("                if (acc_any) begin")
    add("                    if (out_count < out_cap) begin")
    add("                        res_wr <= 1'b1;")
    add("                        res_start <= 64'd0;")
    add("                        // end = position of the beat's LAST"
        " accepting")
    add("                        // byte + 1 (R47 exclusive end; coalesced "
        "window")
    add("                        // contains every narrower one — see header).")
    add(
        "                        res_end <= byte_index + {56'd0, acc_off} + "
        "64'd1;")
    add("                        res_pattern_id <= 32'd0;")
    add("                        res_flags <= 32'd1;")
    add("                        out_count <= out_count + 32'd1;")
    add("                    end else begin")
    add("                        status <= status | 32'h0000_0008;  // OVF")
    add("                    end")
    add("                end")
    add("                if (in_last) finishing <= 1'b1;")
    add("            end")
    add("        end")
    add("    end")
    add("")
    add("    // ---- CSR read mux (R45 offsets) ----")
    add("    always @(*) begin")
    add("        case (csr_addr)")
    add("            16'h0000: csr_rdata = MAGIC;")
    add("            16'h0004: csr_rdata = HARNESS_VER;")
    add("            16'h0008: csr_rdata = CAPS0;")
    add("            16'h000C: csr_rdata = GEN_VER;")
    add("            16'h0010: csr_rdata = ctrl;")
    add("            16'h0014: csr_rdata = status;")
    add("            16'h0018: csr_rdata = CIRC_ID0;")
    add("            16'h001C: csr_rdata = CIRC_ID1;")
    add("            16'h0020: csr_rdata = CIRC_ID2;")
    add("            16'h0024: csr_rdata = CIRC_ID3;")
    add("            16'h0028: csr_rdata = CIRC_FLAGS;")
    add("            16'h002C: csr_rdata = 32'd0;")
    add("            16'h0030: csr_rdata = in_addr_lo;")
    add("            16'h0034: csr_rdata = in_addr_hi;")
    add("            16'h0038: csr_rdata = in_len;")
    add("            16'h0040: csr_rdata = out_addr_lo;")
    add("            16'h0044: csr_rdata = out_addr_hi;")
    add("            16'h0048: csr_rdata = out_cap;")
    add("            16'h004C: csr_rdata = out_count;")
    add("            16'h0050: csr_rdata = irq_enable;")
    add("            16'h0054: csr_rdata = irq_status;")
    add("            16'h0058: csr_rdata = cycle_count[31:0];")
    add("            16'h005C: csr_rdata = cycle_count[63:32];")
    add("            16'h0060: csr_rdata = byte_index[31:0];")
    add("            16'h0064: csr_rdata = byte_index[63:32];")
    add("            default:  csr_rdata = 32'd0;")
    add("        endcase")
    add("    end")
    add("endmodule")
    add("")
    return "\n".join(L)


def _clog2(x: int) -> int:
    """``ceil(log2(x))`` for ``x >= 1`` (0 for ``x <= 1``)."""
    return max(0, (int(x) - 1).bit_length())


def _emit_rtl_group(
    automata,
    circ_id,
    circ_flags,
    n_slots: int,
     n: int) -> str:
    """SR7 pattern-SET engine: ``n_slots`` independent automata, ONE harness.

    Structure (spec ``snort-rule-offload`` §4.4 SR7).  Each slot gets its own
    one-hot state vector, its own closure/move logic and its own accept bit;
    ``pattern_id`` IS the slot index.  Everything else — the R45 CSR block, the
    R45a counters, ``byte_index``, the anchor/position wires, the input feed and
    the R47 result-ring writer — exists exactly once and is shared.  There is
    deliberately **no** merged NFA: a union automaton would make attribution
    (which rule fired?) a subset-reconstruction problem, and the measured area
    of the independent form already fits SF2 (80,000 LUT).

    Deliberately NOT a merged NFA, and deliberately not the single-pattern
    engine's harvest schedule either: this engine detects accepts **in beat**
    (from the per-lane closure stages, like ``_emit_rtl_wide``), so ``n == 1``
    is just the degenerate one-lane cascade rather than a second code path.
    One consequence worth stating: unlike ``_emit_rtl_wide`` there is **no
    per-beat coalescing** — every (lane, slot) accept becomes its own ring
    entry, so the wire result of a ``datapath_bytes=8`` group is byte-identical
    to the same group at ``datapath_bytes=1``.  Width is then purely a
    throughput/area knob and never a semantic one (``tests/hw/xsim_diff.py``
    diffs both widths against ONE reference).

    Accept arbitration (the reason this engine has backpressure at all)
    -------------------------------------------------------------------
    Several slots can accept on the same byte, and at ``n > 1`` several lanes
    of one beat can accept as well; the result ring writes ONE 24-byte entry
    per cycle (R47).  So a consumed beat latches its whole accept matrix into
    ``pend`` (a ``n * 2**clog2(n_slots)`` bit vector, index = lane*STRIDE +
    slot) plus ``pend_base`` = the ``byte_index`` at which the beat started, and
    a priority encoder drains the LOWEST set index one per cycle.  Lowest-index
    first means ascending ``(lane, slot)``, i.e. ascending ``(end,
    pattern_id)`` — the ring order ``pyro._circuit_model.GroupCircuitModel``
    pins — and OVF truncation therefore keeps the earliest-ENDING entries,
    which is what the R41 ``start_off`` resume needs.  ``in_ready`` is low
    while entries are pending, so the feed stalls instead of overrunning the
    ring.

    HAZARD (a) — the in-flight beat.  ``in_ready`` is a function of REGISTERED
    state, so a feeder can only react to it one cycle late; the wrapper's
    ST_FEED (``pyro/hdl/rp_wrapper.py``) asserts ``eng_in_valid`` and advances
    ``feed_idx`` in the SAME cycle, so a beat is already committed when ready
    drops.  Dropping it would be a SILENT FALSE NEGATIVE (a missed byte = a
    missed match, with nothing on the wire to show for it).  This engine
    therefore ACCEPTS AND HOLDS: a 1-deep skid buffer captures that beat and it
    is consumed before any new input.  One slot is provably enough — a feeder
    that samples ``in_ready`` in cycle T cannot present a new beat later than
    T+1, and ``in_ready`` low in T implies it was low in T for every subsequent
    cycle until the drain finishes.

    HAZARD (b) — overflow must still drain.  When ``out_count == out_cap`` the
    entry is DROPPED and ``STATUS.OVF`` is set, but its ``pend`` bit is cleared
    all the same (the clear is unconditional, outside the cap test).  If OVF
    skipped the clear, ``pend`` would never empty, ``in_ready`` would never
    rise, the feed would never resume and the finish drain would deadlock —
    a hang instead of a truncated reply.

    ``res_start`` is 0, as on every PYRO engine: the harness emits on accept and
    never tracked where the run began (see ``GroupCircuitModel``'s docstring —
    the host recovers the true start from ``end`` and the sidecar's anchor
    length; only ``end`` is exact on hardware).
    """
    L: List[str] = []
    add = L.append
    caps0 = (n & 0xFFFF) | ((_estimator.PR_PARTITIONS & 0xFFFF) << 16)

    slots = list(automata)
    slot_bits = max(1, _clog2(n_slots))
    stride = 1 << slot_bits
    npend = n * stride
    pw = max(1, _clog2(npend))
    lane_bits = pw - slot_bits

    def word_expr(b: str) -> str:
        return (f"(({b} >= 8'd48 && {b} <= 8'd57) || "
                f"({b} >= 8'd65 && {b} <= 8'd90) || "
                f"({b} >= 8'd97 && {b} <= 8'd122) || ({b} == 8'd95))")

    add("// Auto-generated by pyro.hdl.generator — do not edit.")
    add(
    f"// PYRO pattern-SET recognizer (SNORT-PF SR7): {n_slots} independent")
    add(
    f"// automata sharing ONE harness; {n} byte(s)/cycle; pattern_id = slot.")
    add(f"// generator_version=0x{GENERATOR_VERSION:08X} "
        f"harness_version=0x{HARNESS_VERSION:08X} datapath_bytes={n}")
    add("//")
    add("// Accept arbitration: a consumed beat latches every (lane, slot) "
        "accept")
    add("// into `pend`; a priority encoder drains ONE result-ring entry per "
        "cycle,")
    add("// lowest index first == ascending (end, pattern_id).  `in_ready` is "
        "low")
    add("// while entries are pending, so the feed stalls rather than "
        "overrunning")
    add("// the ring.  Two hazards are handled explicitly below: (a) the "
        "in-flight")
    add("// beat is captured by a 1-deep skid (a dropped byte is a"
        " silent false")
    add("// negative), and (b) an overflowing entry still CLEARS its pend bit "
        "(or")
    add("// the drain, and with it the whole scan, deadlocks).")
    add("`timescale 1ns/1ps")
    add("module pyro_circuit (")
    add("    input  wire        clk,")
    add("    input  wire        rst_n,")
    add("    // AXI-Lite-style CSR window (§7.4 R45)")
    add("    input  wire [15:0] csr_addr,")
    add("    input  wire [31:0] csr_wdata,")
    add("    input  wire        csr_write,")
    add("    output reg  [31:0] csr_rdata,")
    add("    // input byte stream (DMA abstracted behind the harness, R48)")
    add("    input  wire        in_valid,")
    if n == 1:
        add("    input  wire [7:0]  in_data,")
    else:
        add(f"    input  wire [{8*n-1}:0] in_data,")
        add(f"    input  wire [{n-1}:0]  in_keep,")
    add("    input  wire        in_last,")
    add("    // engine backpressure (SR7): low while the result ring is "
        "draining.")
    add("    // A feeder MUST gate on it; the skid below covers the one"
        " beat it")
    add("    // cannot retract (see HAZARD (a) in the generator docstring).")
    add("    output wire        in_ready,")
    add("    // result-ring write port (24-byte entries, R47)")
    add("    output reg         res_wr,")
    add("    output reg  [63:0] res_start,")
    add("    output reg  [63:0] res_end,")
    add("    output reg  [31:0] res_pattern_id,")
    add("    output reg  [31:0] res_flags")
    add(");")
    add("")
    add("    // --- normative harness constants (R45) ---")
    add(f"    localparam [31:0] MAGIC       = 32'h{ID_MAGIC:08X};")
    add(f"    localparam [31:0] HARNESS_VER = 32'h{HARNESS_VERSION:08X};")
    add(f"    localparam [31:0] GEN_VER     = 32'h{GENERATOR_VERSION:08X};")
    add(f"    localparam [31:0] CAPS0       = 32'h{caps0:08X};")
    add(f"    localparam [31:0] CIRC_ID0    = 32'h{circ_id[0]:08X};")
    add(f"    localparam [31:0] CIRC_ID1    = 32'h{circ_id[1]:08X};")
    add(f"    localparam [31:0] CIRC_ID2    = 32'h{circ_id[2]:08X};")
    add(f"    localparam [31:0] CIRC_ID3    = 32'h{circ_id[3]:08X};")
    add(f"    localparam [31:0] CIRC_FLAGS  = 32'h{circ_flags:08X};")
    add("    // NSLOTS is the SLOT count including tombstones (SR6): "
        "pattern_id is")
    add("    // an index into slots, never into the live subset, so"
        " every bound")
    add("    // check uses this and not the number of live automata.")
    add(f"    localparam integer NSLOTS     = {n_slots};")
    add(f"    localparam integer NPEND      = {npend};  "
        f"// {n} lane(s) x {stride} slot ids")
    add("")
    add("    // --- CSR registers (R45) ---")
    add("    reg  [31:0] ctrl, status;")
    add("    reg  [31:0] in_addr_lo, in_addr_hi, in_len;")
    add("    reg  [31:0] out_addr_lo, out_addr_hi, out_cap, out_count;")
    add("    reg  [31:0] irq_enable, irq_status;")
    add("    wire        ctrl_start = ctrl[0];")
    add("    wire        ctrl_reset = ctrl[1];")
    add("    reg         ctrl_start_d;  // START is EDGE-triggered (2.2.0)")
    add("")
    add("    // --- shared harness datapath ---")
    add("    reg  [63:0] byte_index;")
    add("    reg  [63:0] cycle_count;  // R45a CYCLES: core-clock cycles "
        "while BUSY")
    add("    reg  [7:0]  prev_byte;")
    add("    reg         have_prev;")
    add("    reg         running;")
    add("    reg         finishing;")
    add("    reg  [NPEND-1:0] pend;"
        "      // pending accepts: lane*%d + slot" % stride)
    add("    reg  [63:0] pend_base;      // byte_index of the beat that set "
        "`pend`")
    add("")
    add("    // --- HAZARD (a): 1-deep input skid (accept-and-hold) ---")
    add("    // in_ready is combinational from REGISTERED state, so a feeder "
        "reacts")
    add("    // one cycle late and exactly one beat can be in flight when it "
        "drops.")
    add("    // Capturing that beat here (instead of dropping it) is what "
        "keeps a")
    add("    // stall from turning into a silent missed match.")
    add("    reg         skid_valid;")
    if n == 1:
        add("    reg  [7:0]  skid_data;")
    else:
        add(f"    reg  [{8*n-1}:0] skid_data;")
        add(f"    reg  [{n-1}:0]  skid_keep;")
    add("    reg         skid_last;")
    add("    wire        pend_any = |pend;")
    add("    assign in_ready = !pend_any && !skid_valid;")
    add("")
    add("    // fed_*: the beat actually presented to the datapath this"
        " cycle —")
    add("    // the held one first, then live input.")
    add("    wire        fed_valid = skid_valid || in_valid;")
    if n == 1:
        add("    wire [7:0]  fed_data  = skid_valid ? skid_data : in_data;")
        add("    wire [0:0]  fed_keep  = 1'b1;")
    else:
        add(f"    wire [{8*n-1}:0] fed_data ="
            " skid_valid ? skid_data : in_data;")
        add(f"    wire [{n-1}:0]  fed_keep = skid_valid ? skid_keep : in_keep;")
    add("    wire        fed_last  = skid_valid ? skid_last : in_last;")
    add("")
    add("    // ---- input lanes ----")
    for i in range(n):
        add(f"    wire [7:0] b{i} = fed_data[{8*i} +: 8];")
    add("")
    add("    // ---- shared anchor/position wires (§6.5).  End-of-buffer "
        "anchors keep")
    add("    // the documented 1-byte-engine placeholder status (the model is")
    add("    // authoritative, R7).  Snort anchors are pure literals and "
        "lower to")
    add("    // automata with ZERO eps/assert edges, so on the SNORT-PF "
        "workload")
    add("    // these wires drive nothing at all. ----")
    for i in range(n):
        if i == n - 1:
            add(f"    wire lastk{i} = fed_keep[{i}];")
        else:
            add(f"    wire lastk{i} = fed_keep[{i}]"
                f" && ~(|fed_keep[{n-1}:{i+1}]);")
    for i in range(n):
        add(f"    wire w{i} = {word_expr(f'b{i}')};")
    add("    wire wprev = have_prev && " + word_expr("prev_byte") + ";")
    for j in range(n + 1):
        if j == 0:
            add("    wire at_sob_0 = (byte_index == 64'd0);")
            add("    wire at_bol_0 = at_sob_0 || (have_prev && prev_byte == "
                "8'h0A);")
            add("    wire at_eob_0 = 1'b0;")
            add("    wire at_eol_0 = 1'b0;")
            add("    wire wb_0 = w0 ^ wprev;")
        else:
            cur_w = f"w{j}" if j < n else "1'b0"
            add(f"    wire at_sob_{j} = 1'b0;")
            add(f"    wire at_bol_{j} = (b{j-1} == 8'h0A);")
            add(f"    wire at_eob_{j} = fed_last && lastk{j-1};")
            add(f"    wire at_eol_{j} = fed_last && lastk{j-1};")
            add(f"    wire wb_{j} = {cur_w} ^ w{j-1};")
    add("")
    # ---- per-slot automata ------------------------------------------------
    for i, au in enumerate(slots):
        add(
    f"    // ================= slot {i} (pattern_id {i}) =================")
        if au is None:
            add("    // TOMBSTONED (SR6): the index is retained and can never "
                "match,")
            add("    // so no state, no logic — only a tied-off accept vector.")
            add(f"    wire [{n-1}:0] accv{i} = {{{n}{{1'b0}}}};")
            add("")
            continue
        ns = au.n_states
        kp = closure_passes(au)
        add(f"    localparam integer NS{i}    = {ns};")
        add(f"    localparam [31:0] START{i}  = 32'd{au.start};")
        add(f"    localparam [31:0] ACCEPT{i} = 32'd{au.accept};")
        add(f"    reg [NS{i}-1:0] st{i};"
            "        // registered one-hot state set")
        for j in range(n + 1):
            add(f"    reg [NS{i}-1:0] a{i}_{j};")
        for j in range(n):
            add(f"    reg [NS{i}-1:0] m{i}_{j};")
        for j in range(n + 1):
            src = f"st{i}" if j == 0 else f"m{i}_{j-1}"
            add(f"    // closure stage {j} (position byte_index+{j}), "
                f"{kp} relaxation pass(es)")
            # One loop variable per (slot, stage): two always blocks sharing one
            # `integer` would be a multi-driven variable, which the tools are
            # entitled to reject even though each block runs to completion.
            add(f"    integer it{i}_{j};")
            add("    always @(*) begin")
            add(f"        a{i}_{j} = {src};")
            add(f"        for (it{i}_{j} = 0; it{i}_{j} < {kp}; "
                f"it{i}_{j} = it{i}_{j} + 1) begin")
            subs = [("active[", f"a{i}_{j}["), ("word_boundary", f"wb_{j}"),
                    ("at_sob", f"at_sob_{j}"), ("at_bol", f"at_bol_{j}"),
                    ("at_eob", f"at_eob_{j}"), ("at_eol", f"at_eol_{j}"),
                    ("active = active;", f"a{i}_{j} = a{i}_{j};")]
            for ln in _rename(_emit_closure_body(au), subs):
                add("            " + ln)
            add("        end")
            add("    end")
        for j in range(n):
            add(f"    // move stage {j} (consume lane {j})")
            add("    always @(*) begin")
            add(f"        m{i}_{j} = {{NS{i}{{1'b0}}}};")
            add(f"        m{i}_{j}[START{i}] = 1'b1;"
                "  // continuous search seeding")
            subs = [("active[",
    f"a{i}_{j}["),
    ("moved[",
    f"m{i}_{j}["),
    ("in_data",
    f"b{j}"),
    ("moved = moved;",
     f"m{i}_{j} = m{i}_{j};")]
            for ln in _rename(_emit_move_body(au), subs):
                add("        " + ln)
            if n > 1:
                add(f"        if (!fed_keep[{j}]) m{i}_{j} = a{i}_{j};"
                    "  // short-beat passthrough")
            add("    end")
        acc_terms = ", ".join(
            f"(a{i}_{j+1}[ACCEPT{i}] & fed_keep[{j}])"
            for j in range(n - 1, -1, -1))
        add(f"    wire [{n-1}:0] accv{i} = {{{acc_terms}}};")
        add("")
    # ---- accept matrix -> pending vector ----------------------------------
    add("    // ---- accept matrix, flattened to lane*%d + slot ----" % stride)
    add("    // Index order IS the drain order: ascending index == ascending")
    add("    // (lane, slot) == ascending (end, pattern_id) — the ring order "
        "the")
    add("    // group circuit model pins.")
    terms = []
    for k in range(npend - 1, -1, -1):
        j, i = divmod(k, stride)
        if i < n_slots:
            terms.append(f"accv{i}[{j}]")
        else:
            # slot-id padding to a power of two
            terms.append("1'b0")
    add(f"    wire [NPEND-1:0] acc_flat = {{")
    for idx, t in enumerate(terms):
        add(f"        {t}{',' if idx < len(terms) - 1 else ''}")
    add("    };")
    add("")
    add("    // ---- priority encoder: LOWEST pending index first ----")
    add(f"    reg [{pw-1}:0] pend_idx;")
    add("    integer pe;")
    add("    always @(*) begin")
    add(f"        pend_idx = {pw}'d0;")
    add("        for (pe = NPEND-1; pe >= 0; pe = pe - 1)")
    add(
    f"            if (pend[pe]) pend_idx = pe[{
        pw-1}:0];  // last write wins")
    add("    end")
    add(f"    wire [{slot_bits-1}:0] pend_slot = pend_idx[{slot_bits-1}:0];")
    if lane_bits > 0:
        add(f"    wire [{lane_bits-
    1}:0] pend_lane = pend_idx[{pw-
    1}:{slot_bits}];")
    add("")
    # ---- kept-lane count / last byte --------------------------------------
    if n == 1:
        add("    wire [7:0] kept_n = 8'd1;")
        add("    wire [7:0] lastb  = b0;")
    else:
        add("    reg [7:0] kept_n; reg [7:0] lastb;")
        add("    integer jk;")
        add("    always @(*) begin")
        add("        kept_n = 8'd0; lastb = prev_byte;")
        add(f"        for (jk = 0; jk < {n}; jk = jk + 1)")
        add("            if (fed_keep[jk]) kept_n = kept_n + 8'd1;")
        for i in range(n):
            add(f"        if (fed_keep[{i}]) lastb = b{i};")
        add("    end")
    add("")
    # ---- sequential control ------------------------------------------------
    add("    // --- sequential control / datapath (R48 single-issue) ---")
    add("    always @(posedge clk) begin")
    add("        if (!rst_n) begin")
    add("            ctrl <= 32'd0; status <= 32'd0; running <= 1'b0;")
    add("            in_addr_lo <= 32'd0; in_addr_hi <= 32'd0;"
        " in_len <= 32'd0;")
    add("            out_addr_lo <= 32'd0; out_addr_hi <= 32'd0;")
    add("            out_cap <= 32'd0; out_count <= 32'd0;")
    add("            irq_enable <= 32'd0; irq_status <= 32'd0;")
    add("            byte_index <= 64'd0; cycle_count <= 64'd0;")
    add("            prev_byte <= 8'd0; have_prev <= 1'b0;")
    add("            finishing <= 1'b0; ctrl_start_d <= 1'b0;")
    add("            pend <= {NPEND{1'b0}}; pend_base <= 64'd0;")
    add("            skid_valid <= 1'b0; skid_last <= 1'b0;")
    if n == 1:
        add("            skid_data <= 8'd0;")
    else:
        add(f"            skid_data <= {{{8*n}{{1'b0}}}}; "
            f"skid_keep <= {{{n}{{1'b0}}}};")
    for i, au in enumerate(slots):
        if au is not None:
            add(f"            st{i} <= {{NS{i}{{1'b0}}}};")
    add("            res_wr <= 1'b0; res_start <= 64'd0; res_end <= 64'd0;")
    add("            res_pattern_id <= 32'd0; res_flags <= 32'd0;")
    add("        end else begin")
    add("            res_wr <= 1'b0;")
    add("            ctrl_start_d <= ctrl_start;"
        "   // START edge detect (2.2.0)")
    add("            if (running && !finishing) cycle_count <= cycle_count + "
        "64'd1;")
    add("            // CSR writes (RW registers only; RO ignored)")
    add("            if (csr_write) begin")
    add("                case (csr_addr)")
    add("                    16'h0010: ctrl <= csr_wdata;")
    add("                    16'h0030: in_addr_lo <= csr_wdata;")
    add("                    16'h0034: in_addr_hi <= csr_wdata;")
    add("                    16'h0038: in_len <= csr_wdata;")
    add("                    16'h0040: out_addr_lo <= csr_wdata;")
    add("                    16'h0044: out_addr_hi <= csr_wdata;")
    add("                    16'h0048: out_cap <= csr_wdata;")
    add("                    16'h0050: irq_enable <= csr_wdata;")
    add("                    16'h0054: irq_status <= irq_status & ~csr_wdata;")
    add("                    default: ;")
    add("                endcase")
    add("            end")
    add("            // HAZARD (a): hold the beat the feeder could not "
        "retract.  The")
    add("            // guard (!skid_valid) and the consume branch's clear "
        "(guarded by")
    add("            // skid_valid) are mutually exclusive by construction, "
        "so the two")
    add("            // nonblocking writes below can never race for this "
        "register.")
    add("            if (in_valid && !in_ready && !skid_valid) begin")
    add("                skid_valid <= 1'b1;")
    add("                skid_data  <= in_data;")
    if n > 1:
        add("                skid_keep  <= in_keep;")
    add("                skid_last  <= in_last;")
    add("            end")
    add("            if (ctrl_reset) begin")
    add("                byte_index <= 64'd0; cycle_count <= 64'd0; have_prev "
        "<= 1'b0;")
    add("                out_count <= 32'd0; running <= 1'b0; finishing <= "
        "1'b0;")
    add("                status <= 32'd0;")
    add("                pend <= {NPEND{1'b0}}; skid_valid <= 1'b0;")
    for i, au in enumerate(slots):
        if au is not None:
            add(f"                st{i} <= {{NS{i}{{1'b0}}}};")
    add("            end else if (ctrl_start && !ctrl_start_d && !running) "
        "begin")
    add("                running <= 1'b1;")
    add("                status <= 32'h0000_0001;  // BUSY")
    add("                byte_index <= 64'd0; cycle_count <= 64'd0;")
    add("                have_prev <= 1'b0; out_count <= 32'd0; finishing <= "
        "1'b0;")
    add("                pend <= {NPEND{1'b0}}; skid_valid <= 1'b0;")
    for i, au in enumerate(slots):
        if au is not None:
            add(
    f"                st{i} <= {{NS{i}{{1'b0}}}}; st{i}[START{i}] <= 1'b1;")
    add("            end else if (pend_any) begin")
    add("                // ---- drain: ONE result-ring entry per cycle ----")
    add("                // HAZARD (b): the pend bit is cleared "
        "UNCONDITIONALLY, out-")
    add("                // side the cap test.  An overflowing entry is "
        "dropped (OVF)")
    add("                // but must still retire, or pend never empties, "
        "in_ready")
    add("                // never rises, and both the feed and the finish "
        "drain hang.")
    add("                pend[pend_idx] <= 1'b0;")
    add("                if (out_count < out_cap) begin")
    add("                    res_wr <= 1'b1;")
    add("                    // res_start = 0: the harness emits on accept "
        "and never")
    add("                    // tracked the run's origin; the host recovers "
        "the true")
    add("                    // start from `end` + the slot's anchor length "
        "(SR7).")
    add("                    res_start <= 64'd0;")
    if lane_bits > 0:
        add(f"                    res_end <= pend_base + {{{64-lane_bits}'d0, "
            f"pend_lane}} + 64'd1;")
    else:
        add("                    res_end <= pend_base + 64'd1;")
    add(
    f"                    res_pattern_id <= {{{
        32-slot_bits}'d0, pend_slot}};")
    add("                    res_flags <= 32'd1;  // bit0 verified (advisory)")
    add("                    out_count <= out_count + 32'd1;")
    add("                end else begin")
    add("                    status <= status | 32'h0000_0008;  // OVF")
    add("                end")
    add("            end else if (finishing) begin")
    add("                // DONE only once the ring is complete: the drain "
        "branch has")
    add("                // higher priority, so every accept of the final "
        "beat is")
    add("                // already written.  Asserting DONE earlier would "
        "let the")
    add("                // wrapper compose the reply while entries were in "
        "flight.")
    add("                finishing <= 1'b0;")
    add("                running <= 1'b0;")
    add("                status <= (status & ~32'h0000_0001) | 32'h0000_0002;")
    add("                irq_status <= irq_status | 32'h0000_0001;")
    add("            end else if (running && fed_valid) begin")
    for i, au in enumerate(slots):
        if au is not None:
            add(f"                st{i} <= m{i}_{n-1};")
    add("                pend      <= acc_flat;   // whole accept matrix of "
        "this beat")
    add("                pend_base <= byte_index;")
    add("                prev_byte <= lastb;")
    if n == 1:
        add("                have_prev <= 1'b1;")
        add("                byte_index <= byte_index + 64'd1;")
    else:
        add("                have_prev <= have_prev | (|fed_keep);")
        add("                byte_index <= byte_index + {56'd0, kept_n};")
    add("                if (skid_valid) skid_valid <= 1'b0;  // held beat "
        "retired")
    add("                if (fed_last) finishing <= 1'b1;")
    add("            end")
    add("        end")
    add("    end")
    add("")
    add("    // --- CSR read mux (R45 offsets; all RO/RW registers) ---")
    add("    always @(*) begin")
    add("        case (csr_addr)")
    add("            16'h0000: csr_rdata = MAGIC;")
    add("            16'h0004: csr_rdata = HARNESS_VER;")
    add("            16'h0008: csr_rdata = CAPS0;")
    add("            16'h000C: csr_rdata = GEN_VER;")
    add("            16'h0010: csr_rdata = ctrl;")
    add("            16'h0014: csr_rdata = status;")
    add("            16'h0018: csr_rdata = CIRC_ID0;")
    add("            16'h001C: csr_rdata = CIRC_ID1;")
    add("            16'h0020: csr_rdata = CIRC_ID2;")
    add("            16'h0024: csr_rdata = CIRC_ID3;")
    add("            16'h0028: csr_rdata = CIRC_FLAGS;")
    add("            16'h002C: csr_rdata = 32'd0;")
    add("            16'h0030: csr_rdata = in_addr_lo;")
    add("            16'h0034: csr_rdata = in_addr_hi;")
    add("            16'h0038: csr_rdata = in_len;")
    add("            16'h0040: csr_rdata = out_addr_lo;")
    add("            16'h0044: csr_rdata = out_addr_hi;")
    add("            16'h0048: csr_rdata = out_cap;")
    add("            16'h004C: csr_rdata = out_count;")
    add("            16'h0050: csr_rdata = irq_enable;")
    add("            16'h0054: csr_rdata = irq_status;")
    add("            // R45a perf counters; BYTES is the byte_index feed "
        "counter.")
    add("            16'h0058: csr_rdata = cycle_count[31:0];")
    add("            16'h005C: csr_rdata = cycle_count[63:32];")
    add("            16'h0060: csr_rdata = byte_index[31:0];")
    add("            16'h0064: csr_rdata = byte_index[63:32];")
    add("            default:  csr_rdata = 32'd0;")
    add("        endcase")
    add("    end")
    add("endmodule")
    add("")
    return "\n".join(L)


class GeneratedGroup(NamedTuple):
    """The artifact produced by :func:`generate_group` for one pattern SET.

    Field-compatible with :class:`GeneratedCircuit` except that ``automaton``
    becomes ``automata`` (ordered; index = slot = ``pattern_id``; ``None`` for
    a tombstoned slot) and ``pattern`` becomes ``patterns`` — exactly the duck
    type :class:`pyro._circuit_model.GroupCircuitModel` consumes, so the model
    and the RTL are two views of ONE artifact.
    """

    patterns: Tuple[object, ...]
    flags_per_pattern: Tuple[int, ...]
    enc: int
    generator_version: int
    harness_version: int
    datapath_bytes: int
    automata: Tuple[object, ...]
    circ_id: Tuple[int, int, int, int]
    circ_flags: int
    num_patterns: int                   # == n_slots (tombstones included)
    resources: dict
    rtl: str
    over_approx: Tuple[str, ...]
    estimated_fp_rate: float

    @property
    def n_slots(self) -> int:
        return len(self.automata)

    @property
    def pattern_hash16(self) -> bytes:
        return b"".join(w.to_bytes(4, "little") for w in self.circ_id)


def generate_group(patterns, flags_per_pattern=None,
                   datapath_bytes: int = DATAPATH_BYTES,
                   group_hash: bytes = None,
                   enc: int = _auto.ENC_BYTES) -> GeneratedGroup:
    """Generate the SR7 pattern-SET circuit for ``patterns`` (N slots, 1
    harness).

    ``patterns`` is slot-ordered: slot *i* is ``pattern_id`` *i* (R47), and a
    ``None`` entry is a **tombstoned** slot (SR6) — it keeps its index, emits no
    logic, and can never match.  ``flags_per_pattern`` defaults to 0 for every
    slot.  ``enc`` defaults to ENC_BYTES because SNORT-PF content is bytes
    (``pyro.snort.groups.slot_pattern`` — a str-mode ``nocase`` lowering
    silently over-approximates to "any code point", the S1 silicon defect).

    ``group_hash`` is the 16-byte SR7 group identity baked into CIRC_ID0..3 —
    pass ``RuleGroup.group_hash(...)`` on the SNORT-PF path, so the identity
    the daemon verifies (SR14) covers the sidecar and the port class, which
    this layer cannot see.  When omitted, a domain-separated hash of the
    pattern set itself is used (:func:`pyro.hdl.identity.pattern_set_hash`),
    which is stable and cannot alias either a single-pattern or a real group
    identity.

    ``generate`` is untouched by this function: an N=1 single-pattern circuit
    is still emitted byte-identically by :func:`generate`, so every existing
    cache entry, artifact and the flashed S1 child stay valid (R47b).
    """
    if datapath_bytes not in SUPPORTED_DATAPATH_BYTES:
        raise ValueError(
            f"unsupported datapath_bytes={datapath_bytes!r} "
            f"(supported: {SUPPORTED_DATAPATH_BYTES})")
    pats = list(patterns)
    if not pats:
        raise ValueError("a group needs at least one slot")
    if flags_per_pattern is None:
        flags = [0] * len(pats)
    else:
        flags = [int(f) for f in flags_per_pattern]
        if len(flags) != len(pats):
            raise ValueError(
                f"flags_per_pattern has {len(flags)} entries for "
                f"{len(pats)} slots")
    n_slots = len(pats)
    if n_slots > _estimator._classify.MAX_PATTERNS:
        raise ValueError(
            f"group has {n_slots} slots, exceeds MAX_PATTERNS "
            f"({_estimator._classify.MAX_PATTERNS})")

    automata = []
    over_approx = set()
    for pat, fl in zip(pats, flags):
        if pat is None:
            automata.append(None)            # tombstone (SR6)
            continue
        est = _estimator.estimate(pat, fl, enc)
        if not est.eligible:
            raise ValueError(f"slot pattern not HW-eligible: {est.reason}")
        au = _auto.build(pat, fl, enc)
        automata.append(au)
        over_approx |= set(au.over_approx)
    automata = tuple(automata)

    if group_hash is None:
        digest = _identity.pattern_set_hash(
            pats, flags, GENERATOR_VERSION, HARNESS_VERSION,
            datapath_bytes=datapath_bytes, enc=enc)
    else:
        digest = bytes(group_hash)
        if len(digest) != 16:
            raise ValueError("group_hash must be 16 bytes (128-bit, SR7)")
    circ_id = _identity.circ_id_words(digest)
    # CIRC_FLAGS low16: there are no group-level regex flags (a slot's nocase
    # lives inside its own automaton), so the flags word is the OR of the
    # per-slot effective flags, which is 0 for a pure-bytes Snort group.
    eff_flags = 0
    for au in automata:
        if au is not None:
            eff_flags |= int(au.flags)
    circ_flags = _identity.circ_flags_word(eff_flags, n_slots)
    resources = _estimator.estimate_group(automata, datapath_bytes)
    rtl = _emit_rtl_group(automata, circ_id, circ_flags, n_slots,
                          int(datapath_bytes))
    oa = tuple(sorted(over_approx))
    return GeneratedGroup(
        patterns=tuple(pats),
        flags_per_pattern=tuple(flags),
        enc=int(enc),
        generator_version=GENERATOR_VERSION,
        harness_version=HARNESS_VERSION,
        datapath_bytes=int(datapath_bytes),
        automata=automata,
        circ_id=circ_id,
        circ_flags=circ_flags,
        num_patterns=n_slots,
        resources=resources,
        rtl=rtl,
        over_approx=oa,
        estimated_fp_rate=estimate_fp_rate(oa),
    )


def generate(pattern, flags: int = 0, enc: int = None,
             datapath_bytes: int = DATAPATH_BYTES) -> GeneratedCircuit:
    """Generate the per-pattern circuit for a HW-eligible ``(pattern, flags)``.

    Raises ``ValueError`` if the pattern is not HW-eligible or does not fit the
    advertised budget (callers gate with the estimator / classifier first; this
    guard keeps a bad pattern from ever producing RTL, R12).

    ``datapath_bytes`` (P2b): 1 emits the original single-byte engine
    (byte-identical to pre-P2b output); N in SUPPORTED_DATAPATH_BYTES emits
    the cascaded N-byte/cycle engine (specs/p2-dataplane.md).  N > 1 changes
    the circuit identity (R47a digest mixes N) so caches never conflate
    widths of the same pattern.
    """
    if datapath_bytes not in SUPPORTED_DATAPATH_BYTES:
        raise ValueError(
            f"unsupported datapath_bytes={datapath_bytes!r} "
            f"(supported: {SUPPORTED_DATAPATH_BYTES})")
    est = _estimator.estimate(pattern, flags, enc)
    if not est.eligible:
        raise ValueError(f"pattern not HW-eligible: {est.reason}")

    au = _auto.build(pattern, flags, enc)
    digest = _identity.pattern_hash(
        pattern, au.flags, GENERATOR_VERSION, HARNESS_VERSION,
        datapath_bytes=datapath_bytes)
    circ_id = _identity.circ_id_words(digest)
    num_patterns = est.resources["num_patterns"]
    circ_flags = _identity.circ_flags_word(au.flags, num_patterns)

    if datapath_bytes == 1:
        rtl = _emit_rtl(au, circ_id, circ_flags, num_patterns)
    else:
        rtl = _emit_rtl_wide(au, circ_id, circ_flags, num_patterns,
                             datapath_bytes)
    over_approx = tuple(sorted(au.over_approx))
    return GeneratedCircuit(
        pattern=pattern,
        flags=int(flags),
        enc=au.enc,
        generator_version=GENERATOR_VERSION,
        harness_version=HARNESS_VERSION,
        datapath_bytes=int(datapath_bytes),
        automaton=au,
        circ_id=circ_id,
        circ_flags=circ_flags,
        num_patterns=num_patterns,
        resources=est.resources,
        rtl=rtl,
        over_approx=over_approx,
        estimated_fp_rate=estimate_fp_rate(over_approx),
    )


# Coarse per-class false-positive-rate contributions (R19c/R19b).  These are
# deliberately conservative order-of-magnitude estimates used only to (a) let
# the
# benchmark suite attribute re-verification cost and (b) let R19b flag a ruinous
# over-approximation; they are NOT measured rates.  An exact circuit
# reports 0.0.
_FP_CONTRIB = {
    _auto.OA_UNICODE_CATEGORY: 0.05,
    _auto.OA_CROSS_LENGTH_CASEFOLD: 0.02,
    _auto.OA_WORD_BOUNDARY_UTF8: 0.01,
}


def estimate_fp_rate(over_approx) -> float:
    """A coarse estimated false-positive rate for a set of OA_* classes (R19c).

    Combined as independent probabilities (1 - prod(1 - p_i)); 0.0 for an exact
    circuit.  Deterministic, so it is stable in the manifest across runs.
    """
    keep = 1.0
    for cls in over_approx:
        keep *= (1.0 - _FP_CONTRIB.get(cls, 0.0))
    return round(1.0 - keep, 6)
