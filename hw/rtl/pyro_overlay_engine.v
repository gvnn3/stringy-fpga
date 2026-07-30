// PYRO overlay engine — Aho-Corasick matcher with a run-time writable table.
//
// Amendment A5 (approved 2026-07-30).  Presents the SAME interface as the
// generated `pyro_circuit`, so it drops into the existing `rp_wrapper` with
// no change to the R80 boundary, and takes its table through the SAME input
// stream a corpus arrives on, selected by a CSR mode bit.  No new ports, no
// new datapath, no static rebuild — the only reason this fits the shell.
//
// Why it exists: JTAG partial reconfiguration costs a measured 13.6 s, which
// the frontier study put outside every timescale below a minute.  Writing a
// 2.10 MB table over the existing path costs 0.85 ms.
//
// Identity (A5 §2), enforced in hardware:
//   CIRC_ID  — which engine (baked)
//   TABLE_ID — CRC-32C over the bytes actually received
//   EPOCH    — incremented on commit, emitted with every match
// After reset TABLE_ID is 0 and the engine matches NOTHING.  An overlay that
// came up holding stale content reading as valid would be the worst failure
// available, because it would look correct.
//
// ---------------------------------------------------------------------
// MEASURED STATUS (OOC synth + P&R, xcu250, 2026-07-30)
// ---------------------------------------------------------------------
//   BRAM inference : ALL SIX arrays -> Block RAM, zero LUTRAM fallback
//                    ("LUT as Memory" = 0).  53 BRAM tiles, 1.97% of the
//                    device.
//   Area           : 1,609 LUTs / 1,068 FFs -- about 1/6 the LUTs of a
//                    253-slot generated group circuit (~10,300), because
//                    the patterns now live in BRAM instead of fabric.
//   Functional     : agrees with pyro/overlay/model.py on identical
//                    vectors (same CRC, same match set, same epoch).
//   TIMING         : **MEETS 250 MHz.  WNS = +0.176 ns.**
//
//                    v1  -1.297 ns, 19 levels: bram -> a_dense_reg[12]/D
//                        (CARRY8 x4 popcount tree + the 256-bit mask/shift)
//                    v2  +0.046 ns, 12 levels: bram -> state_q_reg[0]/CE
//                        after splitting rank across S_RANK/S_RANK2 and
//                        dropping the 256-bit barrel shifter for eight
//                        fixed 32-bit popcounts + a lane-selected prefix sum
//                    v3  +0.176 ns, 10 levels: bram -> r_partial_reg[2]/D
//                        after registering hit/fail so the 256:1 bitmap mux
//                        no longer reaches the FSM or state_q's clock enable
//
//                    1.473 ns recovered in total, and LUTs went DOWN over
//                    the same span (1,609 -> 1,225): the barrel shifter was
//                    pure cost.  Registers rose only 1,068 -> 1,121.
//
//                    The remaining path is bram -> lane mux -> 32-bit mask
//                    -> popcount -> r_partial.  If more margin is ever
//                    wanted, register the lane select next; that is another
//                    FSM state, and 176 ps did not seem worth it.
//
//                    STILL OUT-OF-CONTEXT.  OF-1 measured RM<->static
//                    boundary paths costing 5-120 ps in-context on six of
//                    21 group builds; 176 ps now covers that envelope with
//                    room, but the PR link is the only real test and has
//                    not been run.
//
// ---------------------------------------------------------------------
// EVERY memory read is REGISTERED, and that shapes the whole datapath.
// ---------------------------------------------------------------------
// Block RAM has no asynchronous read port.  A first version read four of the
// six arrays combinationally and Vivado silently demoted them to LUTRAM
// (measured: bitmap/dense/fail/oidx fell back; only base/oflat inferred),
// which at MAX_STATES=4096 would be ruinous.  Two consequences, both
// deliberate:
//
//  1. The LOAD path accumulates a whole word in a shift register and writes
//     it once.  The obvious byte-at-a-time read-modify-write needs an async
//     read and costs the array its BRAM.
//  2. The SCAN path is a 10-state FSM with an explicit WAIT state per
//     dependent read (a registered read issued in cycle N is valid in N+2,
//     not N+1).  It processes a byte every ~7 cycles rather than
//     1 B/cycle, so this engine is ~7x slower per byte than the
//     generated group circuits.  That is the honest cost of holding every
//     anchor at once and swapping in 0.85 ms instead of 13.6 s; throughput
//     is recoverable later by overlapping independent bytes, which the
//     loop-carried state dependency makes real work rather than a tweak.

`timescale 1ns/1ps
`default_nettype none

module pyro_overlay_engine #(
    parameter integer MAX_STATES  = 4096,
    parameter integer MAX_DENSE   = 8192,
    parameter integer MAX_OUT     = 4096,
    parameter integer IMAGE_BYTES = 262144,
    parameter [31:0]  ENGINE_ID   = 32'h0A5E0001
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

    localparam [15:0] A_STATUS     = 16'h0014;
    localparam [15:0] A_CIRC_ID0   = 16'h0018;
    localparam [15:0] A_OUT_COUNT  = 16'h004C;
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
    reg [31:0] active_id, shadow_crc, epoch, bytes_rcvd;
    reg        active_valid;
    wire       load_mode = tbl_ctrl[B_LOAD];

    // ---------------- table memories (no reset, registered reads) -------
    (* ram_style = "block" *) reg [255:0] bitmap_mem [0:MAX_STATES-1];
    (* ram_style = "block" *) reg [31:0]  base_mem   [0:MAX_STATES-1];
    (* ram_style = "block" *) reg [31:0]  fail_mem   [0:MAX_STATES-1];
    (* ram_style = "block" *) reg [31:0]  dense_mem  [0:MAX_DENSE-1];
    (* ram_style = "block" *) reg [63:0]  oidx_mem   [0:MAX_STATES-1];
    (* ram_style = "block" *) reg [31:0]  oflat_mem  [0:MAX_OUT-1];

    reg [31:0] hdr_n_states, hdr_n_patterns, hdr_engine_id, hdr_version;
    reg [31:0] off_bm, off_base, off_dense, off_fail, off_oidx, off_oflat;

    // ---------------- load path -----------------------------------------
    reg [31:0]  wr_addr;
    reg [31:0]  word_sr;      // 4-byte accumulator (base/dense/fail/oflat)
    reg [255:0] bm_sr;        // 32-byte accumulator (bitmap)
    reg [63:0]  oidx_sr;      // 8-byte accumulator (out_idx)
    reg [1:0]   wr_bcnt;

    function [31:0] crc32c_byte;
        input [31:0] crc;
        input [7:0]  data;
        integer i;
        reg [31:0] c;
        begin
            c = crc ^ {24'h0, data};
            for (i = 0; i < 8; i = i + 1)
                c = c[0] ? ((c >> 1) ^ 32'h82F63B78) : (c >> 1);
            crc32c_byte = c;
        end
    endfunction

    wire accepting = load_mode && in_valid;
    wire [31:0]  nxt_word = {in_data, word_sr[31:8]};
    wire [255:0] nxt_bm   = {in_data, bm_sr[255:8]};
    wire [63:0]  nxt_oidx = {in_data, oidx_sr[63:8]};
    wire in_bm    = (wr_addr >= off_bm)    && (wr_addr < off_base);
    wire in_base  = (wr_addr >= off_base)  && (wr_addr < off_dense);
    wire in_dense = (wr_addr >= off_dense) && (wr_addr < off_fail);
    wire in_fail  = (wr_addr >= off_fail)  && (wr_addr < off_oidx);
    wire in_oidx  = (wr_addr >= off_oidx)  && (wr_addr < off_oflat);
    wire in_oflat = (wr_addr >= off_oflat) && (off_oflat != 0);
    wire word_last = (wr_bcnt == 2'd3);
    wire bm_last   = (((wr_addr - off_bm)   & 32'd31) == 32'd31);
    wire oidx_last = (((wr_addr - off_oidx) & 32'd7)  == 32'd7);

    // Whole-word writes only: no read-modify-write, so BRAM survives.
    always @(posedge clk) begin
        if (accepting) begin
            if (in_bm    && bm_last)
                bitmap_mem[(wr_addr - off_bm) >> 5] <= nxt_bm;
            if (in_base  && word_last)
                base_mem [(wr_addr - off_base)  >> 2] <= nxt_word;
            if (in_dense && word_last)
                dense_mem[(wr_addr - off_dense) >> 2] <= nxt_word;
            if (in_fail  && word_last)
                fail_mem [(wr_addr - off_fail)  >> 2] <= nxt_word;
            if (in_oidx  && oidx_last)
                oidx_mem [(wr_addr - off_oidx)  >> 3] <= nxt_oidx;
            if (in_oflat && word_last)
                oflat_mem[(wr_addr - off_oflat) >> 2] <= nxt_word;
        end
    end

    // ---------------- scan pipeline -------------------------------------
    // A registered read set up in cycle N is not valid until cycle N+2:
    // the address registers at the end of N, the BRAM reads during N+1, and
    // the data registers at the end of N+1.  Every dependent read therefore
    // needs its own WAIT state.  Getting this off by one produced ZERO
    // matches on the first run of the pipelined version -- caught only
    // because the testbench had been fixed to fail on a wrong match set.
    localparam [3:0] S_IDLE  = 4'd0,
                     S_FETCH = 4'd1,   // wait: bitmap/base/fail in flight
                     S_RANK  = 4'd2,   // valid; latch per-lane popcounts
                     S_RANK2 = 4'd9,   // prefix-sum the rank; issue dense
                     S_DWAIT = 4'd3,   // wait: dense in flight
                     S_DENSE = 4'd4,   // valid; issue oidx read
                     S_OWAIT = 4'd5,   // wait: oidx in flight
                     S_OIDX  = 4'd6,   // valid; maybe start emitting
                     S_FWAIT = 4'd7,   // wait: oflat in flight
                     S_EMIT  = 4'd8;   // valid; emit one result
    reg [3:0]  st;
    reg [31:0] state_q;
    reg [63:0] pos;
    reg [7:0]  cur_byte;

    // Registered BRAM ports: address regs in, data regs out.
    reg [31:0]  a_state, a_dense, a_oidx, a_oflat;
    reg [255:0] d_bitmap;
    reg [31:0]  d_base, d_fail, d_dense, d_oflat;
    reg [63:0]  d_oidx;

    always @(posedge clk) begin
        d_bitmap <= bitmap_mem[a_state];
        d_base   <= base_mem  [a_state];
        d_fail   <= fail_mem  [a_state];
        d_dense  <= dense_mem [a_dense];
        d_oidx   <= oidx_mem  [a_oidx];
        d_oflat  <= oflat_mem [a_oflat];
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

    // Rank = popcount of transition bits strictly below cur_byte.
    //
    // The obvious form -- build a 256-bit mask by variable-shifting, AND it
    // with the bitmap, then popcount 256 bits -- puts a 256-bit barrel
    // shifter AND a 256-input adder tree in one combinational path.  That
    // was the whole timing gap.
    //
    // Instead: eight FIXED 32-bit popcounts (no mask, no shifter), plus ONE
    // 32-bit partial for the lane the byte falls in, then a prefix sum
    // selected by the lane index.  The only variable shift left is 32 bits
    // wide.  Stage 1 registers the nine counts; stage 2 does the prefix sum
    // and the add to base.
    wire [2:0] sel_lane = cur_byte[7:5];
    wire [4:0] sub_bit  = cur_byte[4:0];
    wire [31:0] lane0 = d_bitmap[31:0];    wire [31:0] lane1 = d_bitmap[63:32];
    wire [31:0] lane2 = d_bitmap[95:64];   wire [31:0] lane3 = d_bitmap[127:96];
    wire [31:0] lane4 = d_bitmap[159:128]; wire [31:0] lane5 = d_bitmap[191:160];
    wire [31:0] lane6 = d_bitmap[223:192]; wire [31:0] lane7 = d_bitmap[255:224];
    wire [31:0] sel_lane_bits =
        (sel_lane == 3'd0) ? lane0 : (sel_lane == 3'd1) ? lane1 :
        (sel_lane == 3'd2) ? lane2 : (sel_lane == 3'd3) ? lane3 :
        (sel_lane == 3'd4) ? lane4 : (sel_lane == 3'd5) ? lane5 :
        (sel_lane == 3'd6) ? lane6 : lane7;
    wire [31:0] sub_mask = (sub_bit == 5'd0) ? 32'd0
                         : (32'hFFFFFFFF >> (6'd32 - {1'b0, sub_bit}));

    // Stage-1 registers (filled in S_RANK1, consumed in S_RANK2).
    reg [5:0] r_pc0, r_pc1, r_pc2, r_pc3, r_pc4, r_pc5, r_pc6, r_pc7;
    reg [5:0] r_partial;
    reg [2:0] r_lane;
    reg [31:0] r_base, r_fail;
    reg        r_hit;

    // Prefix sum over the registered lane counts: shallow, and every term
    // is only 6 bits wide.
    wire [8:0] pfx =
        ((r_lane > 3'd0) ? {3'd0, r_pc0} : 9'd0) +
        ((r_lane > 3'd1) ? {3'd0, r_pc1} : 9'd0) +
        ((r_lane > 3'd2) ? {3'd0, r_pc2} : 9'd0) +
        ((r_lane > 3'd3) ? {3'd0, r_pc3} : 9'd0) +
        ((r_lane > 3'd4) ? {3'd0, r_pc4} : 9'd0) +
        ((r_lane > 3'd5) ? {3'd0, r_pc5} : 9'd0) +
        ((r_lane > 3'd6) ? {3'd0, r_pc6} : 9'd0);
    wire [8:0] rank2 = pfx + {3'd0, r_partial};
    wire hit = d_bitmap[cur_byte];

    reg [31:0] emit_off, emit_left;
    reg [63:0] emit_end;

    assign in_ready = load_mode ? 1'b1 : (active_valid && (st == S_IDLE));

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            status <= 0; out_count <= 0; tbl_ctrl <= 0;
            active_id <= 0; shadow_crc <= 32'hFFFFFFFF; epoch <= 0;
            bytes_rcvd <= 0; active_valid <= 0;
            wr_addr <= 0; wr_bcnt <= 0; word_sr <= 0; bm_sr <= 0; oidx_sr <= 0;
            hdr_n_states <= 0; hdr_n_patterns <= 0;
            hdr_engine_id <= 0; hdr_version <= 0;
            off_bm <= 0; off_base <= 0; off_dense <= 0;
            off_fail <= 0; off_oidx <= 0; off_oflat <= 0;
            st <= S_IDLE; state_q <= 0; pos <= 0; cur_byte <= 0;
            a_state <= 0; a_dense <= 0; a_oidx <= 0; a_oflat <= 0;
            emit_off <= 0; emit_left <= 0; emit_end <= 0;
            res_wr <= 0; res_start <= 0; res_end <= 0;
            res_pattern_id <= 0; res_flags <= 0;
            csr_rdata <= 0;
        end else begin
            res_wr <= 1'b0;

            // ---- CSR ----
            if (csr_write && csr_addr == A_TBL_CTRL) begin
                tbl_ctrl <= csr_wdata;
                if (csr_wdata[B_ABORT]) begin
                    wr_addr <= 0; wr_bcnt <= 0; bytes_rcvd <= 0;
                    shadow_crc <= 32'hFFFFFFFF;
                end
                if (csr_wdata[B_COMMIT]) begin
                    if (bytes_rcvd != 0 && hdr_engine_id == ENGINE_ID
                        && hdr_n_states <= MAX_STATES) begin
                        active_id    <= ~shadow_crc;
                        active_valid <= 1'b1;
                        epoch        <= epoch + 1;
                        state_q      <= 0;
                        a_state      <= 0;
                        st           <= S_IDLE;
                    end else status <= status | 32'h4;
                end
            end
            case (csr_addr)
                A_CIRC_ID0:   csr_rdata <= MAGIC;
                A_STATUS:     csr_rdata <= status;
                A_OUT_COUNT:  csr_rdata <= out_count;
                A_TBL_ACTIVE: csr_rdata <= active_id;
                A_TBL_SHADOW: csr_rdata <= ~shadow_crc;
                A_TBL_EPOCH:  csr_rdata <= epoch;
                A_TBL_STATUS: csr_rdata <= {29'd0, active_valid, load_mode,
                                            (bytes_rcvd != 0)};
                A_TBL_BYTES:  csr_rdata <= bytes_rcvd;
                A_TBL_CAPS:   csr_rdata <= MAX_STATES[31:0];
                default:      csr_rdata <= HARNESS_VER;
            endcase

            // ---- LOAD ----
            if (accepting) begin
                shadow_crc <= crc32c_byte(shadow_crc, in_data);
                bytes_rcvd <= bytes_rcvd + 1;
                word_sr <= nxt_word;
                bm_sr   <= nxt_bm;
                oidx_sr <= nxt_oidx;
                wr_bcnt <= wr_bcnt + 1;
                if (wr_addr < 56 && word_last) begin
                    case (wr_addr[7:2])
                        6'd2:  hdr_version    <= nxt_word;
                        6'd3:  hdr_engine_id  <= nxt_word;
                        6'd4:  hdr_n_states   <= nxt_word;
                        6'd5:  hdr_n_patterns <= nxt_word;
                        6'd6:  off_bm         <= nxt_word;
                        6'd7:  off_base       <= nxt_word;
                        6'd8:  off_dense      <= nxt_word;
                        6'd9:  off_fail       <= nxt_word;
                        6'd10: off_oidx       <= nxt_word;
                        6'd11: off_oflat      <= nxt_word;
                        default: ;
                    endcase
                end
                wr_addr <= wr_addr + 1;
            end

            // ---- SCAN ----
            if (!load_mode && active_valid) begin
                case (st)
                    S_IDLE: if (in_valid) begin
                        cur_byte <= in_data;
                        a_state  <= state_q;     // issue the fetch
                        st       <= S_FETCH;
                    end
                    S_FETCH: st <= S_RANK;       // BRAM read latency
                    S_RANK: begin
                        // CAPTURE ONLY -- deliberately no branch here.
                        //
                        // `hit` is d_bitmap[cur_byte], a 256:1 multiplexer
                        // over the BRAM output.  Branching on it in this
                        // state put that mux (MUXF7/MUXF8 pairs) in front of
                        // the FSM logic and hence state_q's clock enable,
                        // which measured as the critical path at +0.046 ns:
                        //   bitmap_mem_reg_bram_24 -> state_q_reg[0]/CE
                        // Registering it here cuts the path at the earliest
                        // possible point -- right at the BRAM output -- and
                        // every consumer downstream sees a plain flop.
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
                            state_q <= r_fail;   // registered, not the mux
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
                    S_OWAIT: st <= S_OIDX;
                    S_OIDX: begin
                        if (d_oidx[63:32] != 32'd0) begin
                            emit_off  <= d_oidx[31:0];
                            emit_left <= d_oidx[63:32];
                            emit_end  <= pos;
                            a_oflat   <= d_oidx[31:0];
                            st        <= S_FWAIT;
                        end else st <= S_IDLE;
                    end
                    S_FWAIT: st <= S_EMIT;
                    S_EMIT: begin
                        res_wr         <= 1'b1;
                        res_pattern_id <= d_oflat;
                        res_start      <= 64'd0;  // host derives from end
                        res_end        <= emit_end;
                        res_flags      <= {epoch[15:0], 16'h0001};
                        out_count      <= out_count + 1;
                        emit_off       <= emit_off + 1;
                        a_oflat        <= emit_off + 1;
                        emit_left      <= emit_left - 1;
                        // Each further output is another registered read.
                        st <= (emit_left == 32'd1) ? S_IDLE : S_FWAIT;
                    end
                    default: st <= S_IDLE;
                endcase
            end
        end
    end

endmodule

`default_nettype wire
