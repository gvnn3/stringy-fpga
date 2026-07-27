"""HDL generator (L2, spec §4/§7.4, R9/R11/R45-R50; AC-1-3 generator clause).

Lowers a HW-eligible pattern's byte automaton (:mod:`pyro.hdl.automaton`) into a
**synthesizable per-pattern Verilog-2001 circuit** that implements the fixed
harness contract of §7.4: the normative CSR register block (R45), the on-chip
performance counters (R45a), the baked circuit-identity block (R47a),
result-ring semantics (R47), and START/DONE/OVF single-issue control (R48).
The datapath is a generic one-hot NFA recognizer; no vendor primitives and no timing-closure effort are attempted (Task-5 brief) —
that is Phase-2 work.  The circuit software model (:mod:`pyro._circuit_model`)
provides the behavioral reference; this RTL is the structural lowering that a
real synthesis flow (Phase 2) would consume.

Determinism (AC-1-3).  For a fixed ``(pattern, flags, generator_version)`` the
emitted text is **byte-identical**: state numbering, edge ordering and byte-set
ranges are all canonical (see :mod:`pyro.hdl.automaton`), and the emitter uses no
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
    over_approx: Tuple[str, ...]        # R19c over-approximation classes (sorted)
    estimated_fp_rate: float           # R19c coarse false-positive-rate estimate

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

    caps0 = (DATAPATH_BYTES & 0xFFFF) | ((_estimator.PR_PARTITIONS & 0xFFFF) << 16)

    add("// Auto-generated by pyro.hdl.generator — do not edit.")
    add("// Per-pattern PYRO recognizer implementing the §7.4 harness contract.")
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
    add("    reg         ctrl_start_d;  // 2.2.0: START is EDGE-triggered (see below)")
    add("")
    add("    // --- automaton state (one-hot NFA, R9 lowering) ---")
    add("    reg  [NSTATES-1:0] state_reg;   // registered active set")
    add("    reg  [NSTATES-1:0] active;      // eps/assert closure (comb)")
    add("    reg  [NSTATES-1:0] moved;       // byte-move result (comb)")
    add("    reg  [63:0] byte_index;")
    add("    reg  [63:0] cycle_count;  // R45a CYCLES: core-clock cycles while BUSY")
    add("    reg  [7:0]  prev_byte;")
    add("    reg         have_prev;")
    add("    reg         running;")
    add("    reg         finishing;   // 2.2.0: one-cycle final-accept harvest (see below)")
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
    add("    wire prev_word = have_prev && ((prev_byte >= 8'd48 && prev_byte <= 8'd57) ||")
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
    add("    // accept_hit samples the CLOSURE of the registered state, i.e. the")
    add("    // accept produced by the byte consumed in the PREVIOUS cycle (this is")
    add("    // deliberate: closure-reached accepts are caught too).  Consequently a")
    add("    // window is harvested one cycle after its final byte, when byte_index")
    add("    // already equals that byte's position + 1 == the R47 exclusive `end`;")
    add("    // and the LAST byte's accept needs the dedicated `finishing` harvest")
    add("    // cycle below (2.2.0 fix: it used to be dropped, and `end` was +1).")
    add("    wire accept_hit = active[ACCEPT_STATE];")
    add("")
    add("    // --- sequential control / datapath (R48 single-issue) ---")
    add("    always @(posedge clk) begin")
    add("        if (!rst_n) begin")
    add("            ctrl <= 32'd0; status <= 32'd0; running <= 1'b0;")
    add("            in_addr_lo <= 32'd0; in_addr_hi <= 32'd0; in_len <= 32'd0;")
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
    add("            ctrl_start_d <= ctrl_start;   // START edge detect (2.2.0)")
    add("            // R45a CYCLES: count every BUSY cycle up to (not including)")
    add("            // the finishing harvest cycle — 'from the cycle after START")
    add("            // is accepted until the final input byte is consumed'.  A")
    add("            // START/RESET assignment later in this block overrides (last")
    add("            // nonblocking write wins), so clears take precedence.")
    add("            if (running && !finishing) cycle_count <= cycle_count + 64'd1;")
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
    add("                byte_index <= 64'd0; cycle_count <= 64'd0; have_prev <= 1'b0;")
    add("                out_count <= 32'd0; running <= 1'b0; finishing <= 1'b0;")
    add("                status <= 32'd0;")
    add("            end else if (ctrl_start && !ctrl_start_d && !running) begin")
    add("                // 2.2.0: START fires on the CTRL bit0 RISING EDGE only.  As a")
    add("                // level it made the engine restart itself the cycle after DONE")
    add("                // (ctrl.START is never cleared), wiping the R45a counters and")
    add("                // reducing DONE to a single-cycle pulse — 'hold their values")
    add("                // after STATUS.DONE' (R45a) was unimplementable.  Edge-firing")
    add("                // keeps DONE and both counters stable until the next START/")
    add("                // RESET, exactly as R45a requires.")
    add("                running <= 1'b1;")
    add("                status <= 32'h0000_0001;  // BUSY")
    add("                state_reg <= {NSTATES{1'b0}};")
    add("                state_reg[START_STATE] <= 1'b1;")
    add("                byte_index <= 64'd0; cycle_count <= 64'd0;")
    add("                have_prev <= 1'b0; out_count <= 32'd0; finishing <= 1'b0;")
    add("            end else if (finishing) begin")
    add("                // 2.2.0: one-cycle harvest of the FINAL byte's accept (its")
    add("                // closure is only visible in `active` the cycle after the")
    add("                // last in_valid; it used to be dropped => missed matches).")
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
    add("                        // Phase-2 synthesis TODO: bake per-thread start-offset")
    add("                        // tracking; res_start=0 here and the host/model recovers")
    add("                        // the exact start via the R18/R19 hybrid re-run.")
    add("                        res_start <= 64'd0;")
    add("                        // byte_index already == accepting-byte position + 1")
    add("                        // == the R47 exclusive end (2.2.0 fix: was +1 too big).")
    add("                        res_end <= byte_index;")
    add("                        res_pattern_id <= 32'd0;")
    add("                        res_flags <= 32'd1;  // bit0 verified (advisory)")
    add("                        out_count <= out_count + 32'd1;")
    add("                    end else begin")
    add("                        status <= status | 32'h0000_0008;  // OVF")
    add("                    end")
    add("                end")
    add("                if (in_last) finishing <= 1'b1;  // harvest + DONE next cycle")
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
    add("            // R45a perf counters; BYTES is the byte_index feed counter.")
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
                        f"if (active[{s}] && {cond}) active[{e.target}] = 1'b1;")
    if not lines:
        lines.append("active = active;  // no epsilon/assert edges")
    return lines


def _emit_move_body(au: _auto.Automaton) -> List[str]:
    """Byte-move body: for each byte edge, gate on source active + byte match."""
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
    add(f"// Per-pattern PYRO recognizer, WIDENED datapath: {n} bytes/cycle (P2b).")
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
    add("    reg  [63:0] cycle_count;  // R45a CYCLES: core-clock cycles while BUSY")
    add("    reg  [7:0]  prev_byte;")
    add("    reg         have_prev;")
    add("    reg         running;")
    add("    reg         finishing;")
    add("")
    add("    // ---- input lanes ----")
    for i in range(n):
        add(f"    wire [7:0] b{i} = in_data[{8*i} +: 8];")
    add("")
    add("    // ---- per-stage anchor/position wires (§6.5; end anchors are the")
    add("    // documented 1-byte-engine placeholders — model authoritative, R7) ----")
    # last-kept lane markers
    for i in range(n):
        if i == n - 1:
            add(f"    wire lastk{i} = in_keep[{i}];")
        else:
            add(f"    wire lastk{i} = in_keep[{i}] && ~(|in_keep[{n-1}:{i+1}]);")
    # per-lane word-char wires
    for i in range(n):
        add(f"    wire w{i} = {word_expr(f'b{i}')};")
    add("    wire wprev = have_prev && "
        + word_expr("prev_byte").replace("((", "((") + ";")
    # closure-stage anchors, j = 0..n (stage j = position byte_index + j)
    for j in range(n + 1):
        if j == 0:
            add("    wire at_sob_0 = (byte_index == 64'd0);")
            add("    wire at_bol_0 = at_sob_0 || (have_prev && prev_byte == 8'h0A);")
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
        add(f"        for (it{j} = 0; it{j} < {_kpasses}; it{j} = it{j} + 1) begin")
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
        add(f"        mov{i}[START_STATE] = 1'b1;  // continuous search seeding")
        subs = [("active[", f"act{i}["), ("moved[", f"mov{i}["),
                ("in_data", f"b{i}"), ("moved = moved;", f"mov{i} = mov{i};")]
        for ln in _rename(_emit_move_body(au), subs):
            add("        " + ln)
        add(f"        if (!in_keep[{i}]) mov{i} = act{i};  // short-beat passthrough")
        add("    end")
    add("")
    # accepts / kept-count / last-byte (combinational)
    add("    // ---- in-beat accepts, coalesced to the highest end (see header) ----")
    add(f"    wire [{n-1}:0] accv = {{")
    for i in range(n - 1, -1, -1):
        sep = "," if i > 0 else ""
        add(f"        (act{i+1}[ACCEPT_STATE] & in_keep[{i}]){sep}")
    add("    };")
    add(f"    reg [7:0] acc_off; reg acc_any; reg [7:0] kept_n; reg [7:0] lastb;")
    add("    integer ja;")
    add("    always @(*) begin")
    add("        acc_any = 1'b0; acc_off = 8'd0; kept_n = 8'd0; lastb = prev_byte;")
    add(f"        for (ja = 0; ja < {n}; ja = ja + 1) begin")
    add("            if (accv[ja]) begin acc_any = 1'b1; acc_off = ja[7:0]; end")
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
    add("            in_addr_lo <= 32'd0; in_addr_hi <= 32'd0; in_len <= 32'd0;")
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
    add("            if (running && !finishing) cycle_count <= cycle_count + 64'd1;")
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
    add("                byte_index <= 64'd0; cycle_count <= 64'd0; have_prev <= 1'b0;")
    add("                out_count <= 32'd0; running <= 1'b0; finishing <= 1'b0;")
    add("                status <= 32'd0;")
    add("            end else if (ctrl_start && !ctrl_start_d && !running) begin")
    add("                running <= 1'b1;")
    add("                status <= 32'h0000_0001;  // BUSY")
    add("                state_reg <= {NSTATES{1'b0}};")
    add("                state_reg[START_STATE] <= 1'b1;")
    add("                byte_index <= 64'd0; cycle_count <= 64'd0;")
    add("                have_prev <= 1'b0; out_count <= 32'd0; finishing <= 1'b0;")
    add("            end else if (finishing) begin")
    add("                // Wide engine: accepts were harvested IN-BEAT (incl. the")
    add("                // final byte's, via the stage closures), so this cycle")
    add("                // only completes the scan: DONE + IRQ (R48/R45a).")
    add("                finishing <= 1'b0;")
    add("                running <= 1'b0;")
    add("                status <= (status & ~32'h0000_0001) | 32'h0000_0002;")
    add("                irq_status <= irq_status | 32'h0000_0001;")
    add("            end else if (running && in_valid) begin")
    add(f"                state_reg <= mov{n-1};")
    add("                prev_byte <= lastb; have_prev <= have_prev | (|in_keep);")
    add("                byte_index <= byte_index + {56'd0, kept_n};")
    add("                if (acc_any) begin")
    add("                    if (out_count < out_cap) begin")
    add("                        res_wr <= 1'b1;")
    add("                        res_start <= 64'd0;")
    add("                        // end = position of the beat's LAST accepting")
    add("                        // byte + 1 (R47 exclusive end; coalesced window")
    add("                        // contains every narrower one — see header).")
    add("                        res_end <= byte_index + {56'd0, acc_off} + 64'd1;")
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
# deliberately conservative order-of-magnitude estimates used only to (a) let the
# benchmark suite attribute re-verification cost and (b) let R19b flag a ruinous
# over-approximation; they are NOT measured rates.  An exact circuit reports 0.0.
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
