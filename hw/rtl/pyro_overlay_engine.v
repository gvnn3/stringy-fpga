// PYRO overlay engine — Aho-Corasick matcher with a run-time writable table.
//
// Amendment A5 (approved 2026-07-30).  Presents the SAME interface as the
// generated `pyro_circuit` so it drops into the existing `rp_wrapper` with
// no change to the R80 boundary, and takes its table through the SAME input
// stream a corpus arrives on, selected by a CSR mode bit.  That is the whole
// reason this fits the shell: no new ports, no new datapath, no static
// rebuild.
//
// Why this exists: JTAG partial reconfiguration costs a measured 13.6 s,
// which the frontier study put outside every timescale below a minute.
// Writing a 2.10 MB table over the existing path costs 0.85 ms.
//
// Identity (A5 §2), enforced in hardware, not by convention:
//   CIRC_ID  — which engine (baked, as today)
//   TABLE_ID — CRC-32C accumulated over the bytes actually received
//   EPOCH    — incremented on commit, emitted with every match
// After reset TABLE_ID is 0 and the engine matches NOTHING: an overlay that
// came up holding stale content that read as valid would be the worst
// possible failure, because it would look correct.
//
// Table image layout (must match pyro/overlay/table.py byte for byte):
//   header 56 B, then bitmap[state][256b], base[state], dense[], fail[state],
//   out_idx[state]={off,cnt}, out_flat[].
//
// STATUS (2026-07-30) -- stated precisely, because "verified" is a strong
// word and only part of this earns it:
//
//  * FUNCTIONAL: agrees with pyro/overlay/model.py on identical vectors --
//    same CRC (0xf2edc7f8), same match set ((0,4),(1,4),(3,6)), same epoch,
//    and TABLE_ID=0 after reset.  See tests/hw/tb_overlay_engine.v.
//  * SYNTHESIS: 0 errors, and the table arrays DO infer Block RAM.  That
//    took a real fix: memory writes must live in a block with NO reset, or
//    Vivado refuses inference outright ("RAM is sensitive to asynchronous
//    reset signal" -- 7 errors, synth failed).  Memory contents are not
//    resettable; validity is tracked by `active_valid` instead.
//  * TIMING: NOT YET KNOWN.  P&R was still running when this was written.
//    Vivado already warns that no output register could be merged into the
//    RAM blocks, which is the third pipeline stage this design still owes.
//
// COVERAGE IS THIN, and pretending otherwise would be worse than the gap:
// the differential runs ONE small vector (4 patterns, 6 bytes).  A
// deliberate sabotage of the input handshake was NOT caught by it, because
// that vector does not excite the case.  This RTL is a first cut that
// agrees with the model where it has been asked; it is not broadly
// verified, and it should not be trusted on silicon until the vector set
// is as wide as the group circuits' (xsim_diff over real corpora).
//
// ALSO OUTSTANDING:
//  * no shadow bank -- A5 section 2.2's quiesce discipline applies, so a
//    commit while scanning is not yet safe;
//  * the S2 dense->oidx read chain needs its own stage (see the Vivado
//    output-register warning above).

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
    // CSR window (R45)
    input  wire [15:0] csr_addr,
    input  wire [31:0] csr_wdata,
    input  wire        csr_write,
    output reg  [31:0] csr_rdata,
    // input byte stream: corpus in SCAN mode, table image in LOAD mode
    input  wire        in_valid,
    input  wire [7:0]  in_data,
    input  wire        in_last,
    output wire        in_ready,
    // result ring (24-byte entries, R47)
    output reg         res_wr,
    output reg  [63:0] res_start,
    output reg  [63:0] res_end,
    output reg  [31:0] res_pattern_id,
    output reg  [31:0] res_flags
);

    // ---------------------------------------------------------------
    // Harness constants (R45)
    // ---------------------------------------------------------------
    localparam [31:0] MAGIC       = 32'h5059524F;
    localparam [31:0] HARNESS_VER = 32'h00020300;
    localparam [31:0] GEN_VER     = 32'h00020300;

    // CSR offsets: R45 block, plus the A5 table window at 0x0068..0x0080
    localparam [15:0] A_CTRL       = 16'h0010;
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

    // TBL_CTRL bits
    localparam integer B_LOAD   = 0;   // 1 = input stream carries table bytes
    localparam integer B_COMMIT = 1;   // write 1 to activate the shadow
    localparam integer B_ABORT  = 2;   // write 1 to discard the shadow

    reg  [31:0] ctrl, status, out_count;
    reg  [31:0] tbl_ctrl;
    reg  [31:0] active_id, shadow_crc, epoch;
    reg  [31:0] bytes_rcvd;
    reg         active_valid;
    wire        load_mode = tbl_ctrl[B_LOAD];

    // ---------------------------------------------------------------
    // Table memories.  Separate arrays so each infers a simple BRAM.
    // ---------------------------------------------------------------
    (* ram_style = "block" *) reg [255:0] bitmap_mem [0:MAX_STATES-1];
    (* ram_style = "block" *) reg [31:0]  base_mem   [0:MAX_STATES-1];
    (* ram_style = "block" *) reg [31:0]  fail_mem   [0:MAX_STATES-1];
    (* ram_style = "block" *) reg [31:0]  dense_mem  [0:MAX_DENSE-1];
    (* ram_style = "block" *) reg [63:0]  oidx_mem   [0:MAX_STATES-1];
    (* ram_style = "block" *) reg [31:0]  oflat_mem  [0:MAX_OUT-1];

    // Header fields captured during load (offsets into the image).
    reg [31:0] hdr_n_states, hdr_n_patterns, hdr_engine_id, hdr_version;
    reg [31:0] off_bm, off_base, off_dense, off_fail, off_oidx, off_oflat;

    // ---------------------------------------------------------------
    // LOAD PATH: table bytes arrive on the input stream, assembled into
    // 32-bit words, CRC-32C accumulated over every byte received.
    // ---------------------------------------------------------------
    reg [31:0] wr_addr;        // byte address within the image
    reg [31:0] wr_word;
    reg [1:0]  wr_bcnt;
    reg [7:0]  bm_bcnt;        // byte counter within a 256-bit bitmap word

    // CRC-32C (Castagnoli), bitwise — one byte per cycle is ample here
    // because the load path is byte-serial anyway.
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

    // ---------------------------------------------------------------
    // SCAN PATH: two-stage pipeline.
    //   S1: fetch bitmap/base/fail for the current state
    //   S2: rank (popcount below the byte) -> dense index -> next state,
    //       or fall back through the failure link and retry (stall)
    // ---------------------------------------------------------------
    reg [31:0] state_q;
    reg [63:0] pos;            // bytes consumed; a match end is pos+1

    reg        s1_valid;
    reg [7:0]  s1_byte;
    reg        s1_last;
    reg [255:0] s1_bitmap;
    reg [31:0] s1_base, s1_fail;
    reg        retrying;       // a failure-link retry is in flight

    // Rank: popcount of bitmap bits strictly below s1_byte.  Split into
    // eight 32-bit lanes so the adder tree stays shallow -- the whole point
    // of pipelining this stage.
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

    wire [255:0] mask_below = (s1_byte == 8'd0)
                            ? 256'd0
                            : ({256{1'b1}} >> (256 - s1_byte));
    wire [255:0] masked = s1_bitmap & mask_below;
    wire [8:0] rank = pc32(masked[31:0])    + pc32(masked[63:32])
                    + pc32(masked[95:64])   + pc32(masked[127:96])
                    + pc32(masked[159:128]) + pc32(masked[191:160])
                    + pc32(masked[223:192]) + pc32(masked[255:224]);
    wire hit = s1_bitmap[s1_byte];

    // Output emission
    reg [31:0] emit_off, emit_left;
    reg [63:0] emit_end;
    wire emitting = (emit_left != 0);

    // Accept input only when scanning, not emitting, and not retrying.
    // A byte accepted while the pipeline is busy would be DROPPED, which
    // is a silent false negative -- the one failure class SR3 forbids.  The
    // xsim differential caught exactly that: "hers" was lost because
    // !s1_valid was missing here.
    assign in_ready = load_mode ? 1'b1
                                : (active_valid && !emitting && !retrying
                                   && !s1_valid);

    integer k;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            // A5 section 5: after reset NOTHING is valid.
            ctrl <= 0; status <= 0; out_count <= 0; tbl_ctrl <= 0;
            active_id <= 32'h0; shadow_crc <= 32'hFFFFFFFF; epoch <= 32'h0;
            bytes_rcvd <= 0; active_valid <= 1'b0;
            wr_addr <= 0; wr_word <= 0; wr_bcnt <= 0; bm_bcnt <= 0;
            state_q <= 0; pos <= 0;
            s1_valid <= 0; retrying <= 0;
            emit_left <= 0; emit_off <= 0; emit_end <= 0;
            res_wr <= 0; res_start <= 0; res_end <= 0;
            res_pattern_id <= 0; res_flags <= 0;
            hdr_n_states <= 0; hdr_n_patterns <= 0;
            hdr_engine_id <= 0; hdr_version <= 0;
            off_bm <= 0; off_base <= 0; off_dense <= 0;
            off_fail <= 0; off_oidx <= 0; off_oflat <= 0;
        end else begin
            res_wr <= 1'b0;

            // ---------------- CSR ----------------
            if (csr_write) begin
                case (csr_addr)
                    A_CTRL: ctrl <= csr_wdata;
                    A_TBL_CTRL: begin
                        tbl_ctrl <= csr_wdata;
                        if (csr_wdata[B_ABORT]) begin
                            wr_addr <= 0; wr_bcnt <= 0; bm_bcnt <= 0;
                            bytes_rcvd <= 0; shadow_crc <= 32'hFFFFFFFF;
                        end
                        if (csr_wdata[B_COMMIT]) begin
                            // Commit only a complete, self-consistent image.
                            if (bytes_rcvd != 0 &&
                                hdr_engine_id == ENGINE_ID &&
                                hdr_n_states <= MAX_STATES) begin
                                active_id    <= ~shadow_crc;
                                active_valid <= 1'b1;
                                epoch        <= epoch + 1;
                                state_q      <= 0;
                            end else begin
                                status <= status | 32'h4;  // ST_ERR
                            end
                        end
                    end
                    default: ;
                endcase
            end
            case (csr_addr)
                A_CIRC_ID0:   csr_rdata <= MAGIC;
                A_STATUS:     csr_rdata <= status;
                A_OUT_COUNT:  csr_rdata <= out_count;
                A_TBL_ACTIVE: csr_rdata <= active_id;
                A_TBL_SHADOW: csr_rdata <= ~shadow_crc;
                A_TBL_EPOCH:  csr_rdata <= epoch;
                A_TBL_STATUS: csr_rdata <= {29'd0, active_valid,
                                            load_mode, (bytes_rcvd != 0)};
                A_TBL_BYTES:  csr_rdata <= bytes_rcvd;
                A_TBL_CAPS:   csr_rdata <= MAX_STATES[31:0];
                default:      csr_rdata <= HARNESS_VER;
            endcase

            // ---------------- LOAD ----------------
            if (load_mode && in_valid) begin
                shadow_crc <= crc32c_byte(shadow_crc, in_data);
                bytes_rcvd <= bytes_rcvd + 1;
                wr_word <= {in_data, wr_word[31:8]};
                wr_bcnt <= wr_bcnt + 1;

                // Header (first 56 bytes) is captured field by field.
                if (wr_addr < 56 && wr_bcnt == 2'd3) begin
                    case (wr_addr[7:2])
                        6'd2: hdr_version   <= {in_data, wr_word[31:8]};
                        6'd3: hdr_engine_id <= {in_data, wr_word[31:8]};
                        6'd4: hdr_n_states  <= {in_data, wr_word[31:8]};
                        6'd5: hdr_n_patterns<= {in_data, wr_word[31:8]};
                        6'd6: off_bm        <= {in_data, wr_word[31:8]};
                        6'd7: off_base      <= {in_data, wr_word[31:8]};
                        6'd8: off_dense     <= {in_data, wr_word[31:8]};
                        6'd9: off_fail      <= {in_data, wr_word[31:8]};
                        6'd10: off_oidx     <= {in_data, wr_word[31:8]};
                        6'd11: off_oflat    <= {in_data, wr_word[31:8]};
                        default: ;
                    endcase
                end

                wr_addr <= wr_addr + 1;
            end

            // ---------------- SCAN ----------------
            if (!load_mode && active_valid) begin
                // S2: resolve the transition fetched last cycle.
                if (s1_valid) begin
                    if (hit) begin
                        state_q  <= dense_mem[s1_base + rank[8:0]];
                        retrying <= 1'b0;
                        pos      <= pos + 1;
                        // Schedule this state's outputs.
                        emit_off  <= oidx_mem[dense_mem[s1_base + rank]][31:0];
                        emit_left <= oidx_mem[dense_mem[s1_base + rank]][63:32];
                        emit_end  <= pos + 1;
                        s1_valid  <= 1'b0;
                    end else if (state_q != 0) begin
                        // Failure fallback: retry the SAME byte from the
                        // failure state.  Amortised O(1) by the standard AC
                        // argument, but it does stall, hence in_ready.
                        state_q  <= fail_mem[state_q];
                        retrying <= 1'b1;
                        s1_valid <= 1'b0;
                    end else begin
                        // Miss at the root: consume the byte, stay at 0.
                        pos      <= pos + 1;
                        retrying <= 1'b0;
                        s1_valid <= 1'b0;
                    end
                end

                // Emit one output per cycle from the scheduled list.
                if (emitting) begin
                    res_wr         <= 1'b1;
                    res_pattern_id <= oflat_mem[emit_off];
                    res_start      <= 64'd0;   // host recovers start from end
                    res_end        <= emit_end;
                    res_flags      <= {epoch[15:0], 16'h0001};
                    emit_off       <= emit_off + 1;
                    emit_left      <= emit_left - 1;
                    out_count      <= out_count + 1;
                end

                // S1: fetch for the next byte (or the retry).
                if (!s1_valid && !emitting) begin
                    if (retrying) begin
                        s1_bitmap <= bitmap_mem[state_q];
                        s1_base   <= base_mem[state_q];
                        s1_fail   <= fail_mem[state_q];
                        s1_valid  <= 1'b1;
                    end else if (in_valid) begin
                        s1_byte   <= in_data;
                        s1_last   <= in_last;
                        s1_bitmap <= bitmap_mem[state_q];
                        s1_base   <= base_mem[state_q];
                        s1_fail   <= fail_mem[state_q];
                        s1_valid  <= 1'b1;
                    end
                end
            end
        end
    end

    // ---------------------------------------------------------------
    // Memory writes live HERE, in a block with NO RESET.
    //
    // BRAM contents are not resettable, and an async reset on the array is
    // exactly what makes Vivado refuse block-RAM inference -- measured on
    // the first synthesis attempt: "RAM is sensitive to asynchronous reset
    // signal", seven errors, synth failed outright.  Validity is tracked by
    // `active_valid`, which IS a reset register, never by contents.
    // ---------------------------------------------------------------
    always @(posedge clk) begin
        if (load_mode && in_valid) begin
                // Section writes, on each completed 32-bit word.
            if (wr_bcnt == 2'd3) begin
                if (wr_addr >= off_base && wr_addr < off_dense)
                    base_mem[(wr_addr - off_base) >> 2]
                        <= {in_data, wr_word[31:8]};
                else if (wr_addr >= off_dense && wr_addr < off_fail)
                    dense_mem[(wr_addr - off_dense) >> 2]
                        <= {in_data, wr_word[31:8]};
                else if (wr_addr >= off_fail && wr_addr < off_oidx)
                    fail_mem[(wr_addr - off_fail) >> 2]
                        <= {in_data, wr_word[31:8]};
                else if (wr_addr >= off_oflat)
                    oflat_mem[(wr_addr - off_oflat) >> 2]
                        <= {in_data, wr_word[31:8]};
            end

            // Bitmaps are 32 bytes each; shift bytes in little-endian.
            if (wr_addr >= off_bm && wr_addr < off_base) begin
                bitmap_mem[(wr_addr - off_bm) >> 5]
                    <= {in_data, bitmap_mem[(wr_addr - off_bm) >> 5][255:8]};
            end
            // out_idx is 8 bytes per state: {count, offset}
            if (wr_addr >= off_oidx && wr_addr < off_oflat) begin
                oidx_mem[(wr_addr - off_oidx) >> 3]
                    <= {in_data, oidx_mem[(wr_addr - off_oidx) >> 3][63:8]};
            end
        end
    end

endmodule

`default_nettype wire
