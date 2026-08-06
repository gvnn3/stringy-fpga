// PYRO S4 ROM engine — the A5 Aho-Corasick scanner with its table BAKED.
//
// SNORT-PF AC-S4-1: the shared anchor trie, ROM-initialized at synthesis,
// no runtime table writes.  This is deliberately NOT the A5 overlay engine
// with a flag — it is a separate module with the load path REMOVED, because
// PYRO's v2.0.0 ruling took loadable-data engines out of the product and the
// honest way to respect that here is for the write port not to exist.  The
// scan datapath — memories, FSM, popcount rank, skid, R45/R45a surface — is
// the A5 engine's verbatim (see pyro_overlay_engine.v for the measured
// timing history; every structural comment there still applies).
//
// What replaces the load path:
//   * The six arrays initialize from $readmemh files emitted by
//     pyro.hdl.rom_child from the SAME serialized image the A5 protocol
//     would have transferred — the serializer stays the one source of
//     layout truth, and word order matches the load accumulators exactly
//     (little-endian words; first byte received = low byte).
//   * Identity is baked: TABLE_ID is the host-computed CRC-32C of the
//     image (a parameter, not a computation — the fabric has nothing to
//     CRC because nothing is ever transferred), TABLE_EPOCH is a nonzero
//     constant.  Epoch never increments: the table cannot change without
//     a re-synthesis, and THAT rolls the child identity instead.
//   * A stray A5 load sequence is consumed and REFUSED: load-mode bytes
//     are accepted (in_ready high) and discarded, never scanned, and any
//     COMMIT sets commit_err with the active table untouched — the same
//     fail-closed degradation A5 §5 requires, reached by construction.
//
// CASE FOLD IN FABRIC (SNORT-PF SR4 `case_fold`): every input byte is
// ASCII-folded (A-Z -> a-z, exactly pyro.overlay.table.ascii_fold) before
// it enters the skid, and the baked trie holds folded patterns.  Folding
// only ever ENLARGES the recognized language (SR3), so nominations stay
// sound, case-permutation fuzz passes by construction, and the price is
// over-nomination on case-sensitive anchors — declared in the manifest,
// re-verified by Snort (SR5).  One 6-input compare on a path with a
// 5-cycle-per-byte budget; it registers into the skid immediately.
//
// URAM CANNOT BE INITIALIZED ON THIS DEVICE — measured, not assumed.
// The first OOC synth of this engine (2026-08-06) warned:
//   [Synth 8-10226] ram_style = ultra ... can not be honored for this
//   device.  The URAM primitives on this device do not support
//   initializations to any non 0 values.  This ROM will be
//   implemented using BRAMs
// and re-inferred BRAM for both big arrays (URAM count 0; the build
// guard refused the link).  URAM init is a Versal feature; on
// UltraScale+ the primitives configure to all-ZEROS and that is all.
//
// So the table is baked in two forms:
//   * base/fail/dense/oflat/oidx — BRAM ROMs ($readmemh; BRAM init IS
//     honored).  oidx moves from URAM to BRAM for this reason (38
//     BRAM36 at cap-16 sizing; it fits).
//   * bitmap_mem — the only array that must stay URAM (256 b wide; in
//     BRAM it alone would blow the budget).  Its content is fully
//     DERIVABLE from data already resident: base_mem bounds each
//     state's transition run in dense order, and a small tbyte ROM
//     (8 b per dense entry) holds each transition's byte value.  A
//     one-shot BOOT FSM walks the states out of reset and expands
//     bitmap_mem[s] = OR(1 << tbyte[j]) over the state's run —
//     roughly 3 cycles per transition plus 6 per state, ~0.3 ms at
//     250 MHz, once per configuration.  Every state is written,
//     including the transitionless ones (zeros): hardware URAM powers
//     up zeroed but xsim reads X, and the differential must see the
//     same table silicon will.
//
// The engine holds in_ready low until boot_done, so a frame arriving
// during expansion stalls in the wrapper's credit handshake and is
// scanned correctly ~0.3 ms later; nothing is dropped, nothing scans
// against a half-built bitmap.  This is still "ROM-initialized at
// synthesis — no runtime table writes" in AC-S4-1's sense: content is
// fixed when the bitstream is written and no host-reachable write
// path exists.

`timescale 1ns/1ps
`default_nettype none

module pyro_ac_rom_engine #(
    parameter integer MAX_STATES  = 21332,
    parameter integer MAX_DENSE   = 21331,
    parameter integer MAX_OUT     = 10256,
    parameter integer IMAGE_BYTES = 1150340,
    parameter [31:0]  ENGINE_ID   = 32'h53340001,  // "S4"
    parameter [31:0]  TABLE_ID    = 32'h00000000,  // host CRC-32C, baked
    parameter [31:0]  TABLE_EPOCH = 32'h00000001,  // nonzero, constant
    parameter BASE_MEMH   = "s4_base.memh",
    parameter DENSE_MEMH  = "s4_dense.memh",
    parameter FAIL_MEMH   = "s4_fail.memh",
    parameter OIDX_MEMH   = "s4_oidx.memh",
    parameter OFLAT_MEMH  = "s4_oflat.memh",
    parameter TBYTE_MEMH  = "s4_tbyte.memh"
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] csr_addr,
    input  wire [31:0] csr_wdata,
    input  wire        csr_write,
    output reg  [31:0] csr_rdata,
    input  wire        in_valid,
    input  wire [7:0]  in_data,
    input  wire        in_last,
    output wire        in_ready,
    output reg         res_wr,
    output reg  [63:0] res_start,
    output reg  [63:0] res_end,
    output reg  [31:0] res_pattern_id,
    output reg  [31:0] res_flags
);

    localparam [31:0] MAGIC       = 32'h5059524F;
    localparam [31:0] HARNESS_VER = 32'h00020300;

    // R45 harness contract — same registers, same reason as the A5
    // engine: rp_wrapper drives RESET -> OUT_CAP -> START -> wait BUSY
    // -> feed -> wait DONE, and a streaming scanner must answer it.
    localparam [15:0] A_CTRL       = 16'h0010;   // bit0 START, bit1 RESET
    localparam [15:0] A_STATUS     = 16'h0014;   // 0 BUSY 1 DONE 3 OVF
    localparam [15:0] A_CIRC_ID0   = 16'h0018;
    localparam [15:0] A_OUT_CAP    = 16'h0048;
    localparam [15:0] A_OUT_COUNT  = 16'h004C;
    localparam [15:0] A_CYCLES_LO  = 16'h0058;
    localparam [15:0] A_CYCLES_HI  = 16'h005C;
    localparam [15:0] A_BYTES_LO   = 16'h0060;
    localparam [15:0] A_BYTES_HI   = 16'h0064;
    localparam [15:0] A_TBL_ACTIVE = 16'h0068;
    localparam [15:0] A_TBL_SHADOW = 16'h006C;
    localparam [15:0] A_TBL_EPOCH  = 16'h0070;
    localparam [15:0] A_TBL_STATUS = 16'h0074;
    localparam [15:0] A_TBL_BYTES  = 16'h0078;
    localparam [15:0] A_TBL_CTRL   = 16'h007C;
    localparam [15:0] A_TBL_CAPS   = 16'h0080;

    localparam integer B_LOAD   = 0;
    localparam integer B_COMMIT = 1;
    localparam integer B_ABORT  = 2;

    reg [31:0] status, out_count, tbl_ctrl;
    reg [31:0] out_cap;          // R45 OUT_CAP; 0 = uncapped
    reg        commit_err;       // a commit was attempted (always refused)
    reg        busy, done_f, ovf_f;
    reg [63:0] perf_cycles, perf_bytes;
    wire       load_mode = tbl_ctrl[B_LOAD];

    // ---------------- table memories -------------------------------------
    // bitmap: URAM, boot-expanded (see header — URAM has no init on
    // this device).  Everything else: BRAM ROMs, $readmemh honored.
    // Cap-16 sizing: 24 URAM + ~109 BRAM36 against SF2's 64/160.
    (* ram_style = "ultra", cascade_height = 2 *)
    reg [255:0] bitmap_mem [0:MAX_STATES-1];
    (* ram_style = "block" *) reg [31:0]  base_mem   [0:MAX_STATES-1];
    (* ram_style = "block" *) reg [31:0]  fail_mem   [0:MAX_STATES-1];
    (* ram_style = "block" *) reg [31:0]  dense_mem  [0:MAX_DENSE-1];
    (* ram_style = "block" *) reg [63:0]  oidx_mem   [0:MAX_STATES-1];
    (* ram_style = "block" *) reg [31:0]  oflat_mem  [0:MAX_OUT-1];
    (* ram_style = "block" *) reg [7:0]   tbyte_mem  [0:MAX_DENSE-1];

    initial begin
        $readmemh(BASE_MEMH,  base_mem);
        $readmemh(DENSE_MEMH, dense_mem);
        $readmemh(FAIL_MEMH,  fail_mem);
        $readmemh(OIDX_MEMH,  oidx_mem);
        $readmemh(OFLAT_MEMH, oflat_mem);
        $readmemh(TBYTE_MEMH, tbyte_mem);
    end

    // ---------------- boot expansion: base + tbyte -> bitmap -------------
    // Non-pipelined on purpose: boot runs once per configuration and
    // 0.3 ms is invisible next to configuration itself, so every read
    // gets the same addr -> wait -> latch discipline the scan FSM uses
    // (registered BRAM reads are valid at N+2, and guessing one cycle
    // is the off-by-one that produced zero matches in the A5 bring-up).
    localparam [2:0] BS_BASE0 = 3'd0,  // issue base[s]
                     BS_BASE1 = 3'd1,  // issue base[s+1]
                     BS_BASE2 = 3'd2,  // latch run start
                     BS_BASE3 = 3'd3,  // latch run end
                     BS_TB0   = 3'd4,  // issue tbyte[j] / done -> write
                     BS_TB1   = 3'd5,  // wait
                     BS_TB2   = 3'd6,  // latch: acc |= 1 << tbyte
                     BS_DONE  = 3'd7;
    reg [2:0]   bst;
    reg [31:0]  b_s, b_j, b_end;
    (* max_fanout = 16 *) reg [255:0] b_acc;
    reg         b_wr;
    // The write executes one cycle after b_wr is raised, by which time
    // b_s has already advanced to the next state — writing bitmap_mem
    // [b_s] here put every state's bitmap at s+1 (caught by the xsim
    // differential the cycle it was written).  b_ws latches the OLD
    // b_s in the same nonblocking assignment group, so the write lands
    // where the accumulation happened.
    reg [31:0]  b_ws;
    reg         boot_done;
    reg [31:0]  a_bbase, a_tb;
    reg [31:0]  d_bbase;
    reg [7:0]   d_tb;

    always @(posedge clk) begin
        d_bbase <= base_mem[a_bbase];
        d_tb    <= tbyte_mem[a_tb];
        if (b_wr) bitmap_mem[b_ws] <= b_acc;
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            bst <= BS_BASE0; b_s <= 32'd0; b_j <= 32'd0; b_end <= 32'd0;
            b_acc <= 256'd0; b_wr <= 1'b0; b_ws <= 32'd0;
            boot_done <= 1'b0;
            a_bbase <= 32'd0; a_tb <= 32'd0;
        end else begin
            b_wr <= 1'b0;
            b_ws <= b_s;
            case (bst)
                BS_BASE0: if (!boot_done) begin
                    a_bbase <= b_s;
                    bst     <= BS_BASE1;
                end
                BS_BASE1: begin
                    // Clamp the s+1 read at the top state; its run
                    // ends at MAX_DENSE, not at a base entry.
                    a_bbase <= (b_s + 32'd1 < MAX_STATES)
                             ? (b_s + 32'd1) : 32'd0;
                    bst <= BS_BASE2;
                end
                BS_BASE2: begin
                    b_j <= d_bbase;
                    bst <= BS_BASE3;
                end
                BS_BASE3: begin
                    b_end <= (b_s + 32'd1 < MAX_STATES)
                           ? d_bbase : MAX_DENSE;
                    b_acc <= 256'd0;
                    bst   <= BS_TB0;
                end
                BS_TB0: if (b_j == b_end) begin
                    // Run complete (possibly empty): write the bitmap
                    // — EVERY state gets one, zeros included, so xsim
                    // and silicon hold the same table.
                    b_wr <= 1'b1;
                    if (b_s + 32'd1 == MAX_STATES) begin
                        boot_done <= 1'b1;
                        bst       <= BS_DONE;
                    end else begin
                        b_s <= b_s + 32'd1;
                        bst <= BS_BASE0;
                    end
                end else begin
                    a_tb <= b_j;
                    bst  <= BS_TB1;
                end
                BS_TB1: bst <= BS_TB2;
                BS_TB2: begin
                    b_acc <= b_acc | (256'd1 << d_tb);
                    b_j   <= b_j + 32'd1;
                    bst   <= BS_TB0;
                end
                BS_DONE: ;   // parked; only reset restarts the walk
                default: bst <= BS_DONE;
            endcase
        end
    end

    // ---------------- scan pipeline (verbatim from the A5 engine) -------
    localparam [3:0] S_IDLE  = 4'd0,
                     S_FETCH = 4'd1,   // wait 1: bitmap (URAM) in flight
                     S_FETCH2= 4'd10,  // wait 2: URAM cascade output reg
                     S_RANK  = 4'd2,   // valid; latch per-lane popcounts
                     S_RANK2 = 4'd9,   // prefix-sum the rank; issue dense
                     S_DWAIT = 4'd3,   // wait: dense in flight
                     S_DENSE = 4'd4,   // valid; issue oidx read
                     S_OWAIT = 4'd5,   // wait 1: oidx (URAM) in flight
                     S_OWAIT2= 4'd11,  // wait 2: URAM cascade output reg
                     S_OIDX  = 4'd6,   // valid; maybe start emitting
                     S_FWAIT = 4'd7,   // wait: oflat in flight
                     S_EMIT  = 4'd8;   // valid; emit one result
    reg [3:0]  st;
    reg [31:0] state_q;
    reg [63:0] pos;
    reg [7:0]  cur_byte;
    reg        req_done;

    (* max_fanout = 16 *) reg [31:0] a_state;
    reg [31:0]  a_dense, a_oidx, a_oflat;
    reg [255:0] d_bitmap, d_bitmap_q;
    reg [31:0]  d_base, d_fail, d_dense, d_oflat;
    reg [63:0]  d_oidx, d_oidx_q;

    always @(posedge clk) begin
        d_bitmap   <= bitmap_mem[a_state];
        d_bitmap_q <= d_bitmap;      // isolate the URAM cascade ripple
        d_base     <= base_mem  [a_state];
        d_fail     <= fail_mem  [a_state];
        d_dense    <= dense_mem [a_dense];
        d_oidx     <= oidx_mem  [a_oidx];
        d_oidx_q   <= d_oidx;        // ditto
        d_oflat    <= oflat_mem [a_oflat];
    end

    function [5:0] pc32;
        input [31:0] w;
        integer i;
        reg [5:0] n;
        begin
            n = 0;
            for (i = 0; i < 32; i = i + 1) n = n + w[i];
            pc32 = n;
        end
    endfunction

    wire [2:0] sel_lane = cur_byte[7:5];
    wire [4:0] sub_bit  = cur_byte[4:0];
    wire [31:0] lane0 = d_bitmap_q[31:0];
    wire [31:0] lane1 = d_bitmap_q[63:32];
    wire [31:0] lane2 = d_bitmap_q[95:64];
    wire [31:0] lane3 = d_bitmap_q[127:96];
    wire [31:0] lane4 = d_bitmap_q[159:128];
    wire [31:0] lane5 = d_bitmap_q[191:160];
    wire [31:0] lane6 = d_bitmap_q[223:192];
    wire [31:0] lane7 = d_bitmap_q[255:224];
    wire [31:0] sel_lane_bits =
        (sel_lane == 3'd0) ? lane0 : (sel_lane == 3'd1) ? lane1 :
        (sel_lane == 3'd2) ? lane2 : (sel_lane == 3'd3) ? lane3 :
        (sel_lane == 3'd4) ? lane4 : (sel_lane == 3'd5) ? lane5 :
        (sel_lane == 3'd6) ? lane6 : lane7;
    wire [31:0] sub_mask = (sub_bit == 5'd0) ? 32'd0
                         : (32'hFFFFFFFF >> (6'd32 - {1'b0, sub_bit}));

    reg [5:0] r_pc0, r_pc1, r_pc2, r_pc3, r_pc4, r_pc5, r_pc6, r_pc7;
    reg [5:0] r_partial;
    reg [2:0] r_lane;
    reg [31:0] r_base, r_fail;
    reg        r_hit;

    wire [8:0] pfx =
        ((r_lane > 3'd0) ? {3'd0, r_pc0} : 9'd0) +
        ((r_lane > 3'd1) ? {3'd0, r_pc1} : 9'd0) +
        ((r_lane > 3'd2) ? {3'd0, r_pc2} : 9'd0) +
        ((r_lane > 3'd3) ? {3'd0, r_pc3} : 9'd0) +
        ((r_lane > 3'd4) ? {3'd0, r_pc4} : 9'd0) +
        ((r_lane > 3'd5) ? {3'd0, r_pc5} : 9'd0) +
        ((r_lane > 3'd6) ? {3'd0, r_pc6} : 9'd0);
    wire [8:0] rank2 = pfx + {3'd0, r_partial};
    wire hit = d_bitmap_q[cur_byte];

    reg [31:0] emit_off, emit_left;
    reg [63:0] emit_end;

    // 2-deep skid — the rp_wrapper credit contract, verbatim from the
    // A5 engine (see its comment block for the two measured failure
    // modes that shaped it).
    reg [7:0] sk0_d, sk1_d;
    reg       sk0_l, sk1_l;
    reg [1:0] sk_n;

    // The SR4 case_fold, in fabric: A-Z (0x41-0x5A) -> a-z, nothing
    // else — the exact ascii_fold the trie was built with.
    wire [7:0] fold_b = (in_data >= 8'h41 && in_data <= 8'h5A)
                      ? (in_data | 8'h20) : in_data;

    wire push = in_valid && !load_mode && boot_done && (sk_n < 2'd2);
    wire pop  = (st == S_IDLE) && (sk_n != 2'd0);
    wire       have_byte = (sk_n != 2'd0);
    wire [7:0] byte_in   = sk0_d;
    wire       byte_last = sk0_l;

    // Ready once the boot expansion has written every bitmap (there is
    // no stale-content hazard — content cannot change — but scanning
    // against a HALF-EXPANDED bitmap would be silently wrong, so
    // in_ready holds the wrapper's credit handshake until boot_done).
    // Load-mode bytes are absorbed and dropped so a stray A5 transfer
    // terminates cleanly.
    assign in_ready = load_mode ? 1'b1 : (boot_done && sk_n == 2'd0);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            status <= 0; out_count <= 0; tbl_ctrl <= 0;
            out_cap <= 0; busy <= 1'b0; done_f <= 1'b0; ovf_f <= 1'b0;
            perf_cycles <= 64'd0; perf_bytes <= 64'd0;
            commit_err <= 1'b0;
            sk_n <= 2'd0; sk0_d <= 8'd0; sk1_d <= 8'd0;
            sk0_l <= 1'b0; sk1_l <= 1'b0;
            st <= S_IDLE; state_q <= 0; pos <= 0; cur_byte <= 0;
            req_done <= 1'b0;
            a_state <= 0; a_dense <= 0; a_oidx <= 0; a_oflat <= 0;
            emit_off <= 0; emit_left <= 0; emit_end <= 0;
            res_wr <= 0; res_start <= 0; res_end <= 0;
            res_pattern_id <= 0; res_flags <= 0;
            csr_rdata <= 0;
            // d_* read registers deliberately NOT reset — same
            // multi-driver hazard the A5 engine documents (a second
            // driver here killed the URAMs entirely at synthesis).
        end else begin
            res_wr <= 1'b0;
            if (busy) perf_cycles <= perf_cycles + 64'd1;

            // ---- CSR ----
            if (csr_write && csr_addr == A_CTRL) begin
                if (csr_wdata[1]) begin              // RESET
                    sk_n      <= 2'd0;
                    perf_cycles <= 64'd0;    // per-scan counters (R45a)
                    perf_bytes  <= 64'd0;
                    busy      <= 1'b0;
                    done_f    <= 1'b0;
                    ovf_f     <= 1'b0;
                    out_count <= 32'd0;
                    st        <= S_IDLE;
                    state_q   <= 32'd0;
                    a_state   <= 32'd0;
                    pos       <= 64'd0;
                    req_done  <= 1'b0;
                end
                if (csr_wdata[0]) begin              // START
                    busy   <= 1'b1;
                    done_f <= 1'b0;
                end
            end
            if (csr_write && csr_addr == A_OUT_CAP)
                out_cap <= csr_wdata;

            if (csr_write && csr_addr == A_TBL_CTRL) begin
                tbl_ctrl <= csr_wdata;
                if (csr_wdata[B_ABORT])
                    commit_err <= 1'b0;
                if (csr_wdata[B_COMMIT]) begin
                    // ALWAYS refused: this engine has no shadow to
                    // commit.  Refusal is total (A5 §5): the baked
                    // table is untouched and the error is visible.
                    status     <= status | 32'h4;
                    commit_err <= 1'b1;
                end
            end
            case (csr_addr)
                A_CIRC_ID0:   csr_rdata <= MAGIC;
                A_STATUS:     csr_rdata <= {28'd0, ovf_f, status[2],
                                            done_f, busy};
                A_OUT_COUNT:  csr_rdata <= out_count;
                A_OUT_CAP:    csr_rdata <= out_cap;
                A_CYCLES_LO:  csr_rdata <= perf_cycles[31:0];
                A_CYCLES_HI:  csr_rdata <= perf_cycles[63:32];
                A_BYTES_LO:   csr_rdata <= perf_bytes[31:0];
                A_BYTES_HI:   csr_rdata <= perf_bytes[63:32];
                A_TBL_ACTIVE: csr_rdata <= TABLE_ID;
                A_TBL_SHADOW: csr_rdata <= 32'd0;   // nothing in flight
                A_TBL_EPOCH:  csr_rdata <= TABLE_EPOCH;
                // active_valid = boot_done: the identity is baked but
                // the table is not scannable until expansion finishes.
                A_TBL_STATUS: csr_rdata <= {28'd0, commit_err, boot_done,
                                            load_mode, 1'b0};
                A_TBL_BYTES:  csr_rdata <= IMAGE_BYTES[31:0];
                A_TBL_CAPS:   csr_rdata <= MAX_STATES[31:0];
                default:      csr_rdata <= HARNESS_VER;
            endcase

            // ---- SCAN ----
            if (!load_mode) begin
                if (pop) perf_bytes <= perf_bytes + 64'd1;
                case ({push, pop})
                    2'b10: begin
                        if (sk_n == 2'd0) begin
                            sk0_d <= fold_b; sk0_l <= in_last;
                        end else begin
                            sk1_d <= fold_b; sk1_l <= in_last;
                        end
                        sk_n <= sk_n + 2'd1;
                    end
                    2'b01: begin
                        sk0_d <= sk1_d; sk0_l <= sk1_l;
                        sk_n  <= sk_n - 2'd1;
                    end
                    2'b11: begin
                        if (sk_n == 2'd1) begin
                            sk0_d <= fold_b; sk0_l <= in_last;
                        end else begin
                            sk0_d <= sk1_d;   sk0_l <= sk1_l;
                            sk1_d <= fold_b;  sk1_l <= in_last;
                        end
                        // depth unchanged: one in, one out
                    end
                    default: ;
                endcase
                case (st)
                    S_IDLE: if (have_byte) begin
                        cur_byte <= byte_in;
                        if (req_done) begin
                            pos      <= 64'd0;
                            state_q  <= 32'd0;
                            a_state  <= 32'd0;
                            req_done <= 1'b0;
                        end else begin
                            a_state <= state_q;
                        end
                        if (byte_last) req_done <= 1'b1;
                        st <= S_FETCH;
                    end
                    S_FETCH:  st <= S_FETCH2;
                    S_FETCH2: st <= S_RANK;
                    S_RANK: begin
                        r_pc0 <= pc32(lane0); r_pc1 <= pc32(lane1);
                        r_pc2 <= pc32(lane2); r_pc3 <= pc32(lane3);
                        r_pc4 <= pc32(lane4); r_pc5 <= pc32(lane5);
                        r_pc6 <= pc32(lane6); r_pc7 <= pc32(lane7);
                        r_partial <= pc32(sel_lane_bits & sub_mask);
                        r_lane    <= sel_lane;
                        r_base    <= d_base;
                        r_fail    <= d_fail;
                        r_hit     <= hit;
                        st        <= S_RANK2;
                    end
                    S_RANK2: begin
                        if (r_hit) begin
                            a_dense <= r_base + {23'd0, rank2};
                            st      <= S_DWAIT;
                        end else if (state_q != 0) begin
                            state_q <= r_fail;
                            a_state <= r_fail;
                            st      <= S_FETCH;  // retry the SAME byte
                        end else begin
                            pos <= pos + 1;      // miss at the root
                            st  <= S_IDLE;
                        end
                    end
                    S_DWAIT: st <= S_DENSE;
                    S_DENSE: begin
                        state_q <= d_dense;
                        a_oidx  <= d_dense;
                        pos     <= pos + 1;
                        st      <= S_OWAIT;
                    end
                    S_OWAIT:  st <= S_OWAIT2;
                    S_OWAIT2: st <= S_OIDX;
                    S_OIDX: begin
                        if (d_oidx_q[63:32] != 32'd0) begin
                            emit_off  <= d_oidx_q[31:0];
                            emit_left <= d_oidx_q[63:32];
                            emit_end  <= pos;
                            a_oflat   <= d_oidx_q[31:0];
                            st        <= S_FWAIT;
                        end else st <= S_IDLE;
                    end
                    S_FWAIT: st <= S_EMIT;
                    S_EMIT: if (out_cap != 32'd0
                                && out_count >= out_cap) begin
                        ovf_f <= 1'b1;
                        st    <= S_IDLE;
                    end else begin
                        res_wr         <= 1'b1;
                        res_pattern_id <= d_oflat;
                        res_start      <= 64'd0;  // host derives from end
                        res_end        <= emit_end;
                        res_flags      <= {TABLE_EPOCH[15:0], 16'h0001};
                        out_count      <= out_count + 1;
                        emit_off       <= emit_off + 1;
                        a_oflat        <= emit_off + 1;
                        emit_left      <= emit_left - 1;
                        st <= (emit_left == 32'd1) ? S_IDLE : S_FWAIT;
                    end
                    default: st <= S_IDLE;
                endcase

                if (busy && req_done && st == S_IDLE) begin
                    busy   <= 1'b0;
                    done_f <= 1'b1;
                end
            end
        end
    end

endmodule

`default_nettype wire
