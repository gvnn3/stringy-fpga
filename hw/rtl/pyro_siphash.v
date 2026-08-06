// PYRO SipHash-2-4 core — byte-serial keyed digest for the P1 MAC engine.
//
// Multi-program MAC contract (P1).  This is the hash primitive under
// pyro_mac_engine_top: one 64-bit SipHash-2-4 tag per wire frame, keyed
// by the 128-bit key delivered over MAC_KEY_LOAD.  It implements the
// reference algorithm exactly (v0..v3 initialized from k0/k1 XOR the
// "somepseudorandomlygeneratedbytes" constants, 8-byte little-endian
// message blocks, length byte in bits [63:56] of the final block, c = 2
// compression rounds per block, d = 4 finalization rounds, tag =
// v0^v1^v2^v3).
//
// Byte-serial by design.  One message byte is accepted per cycle while a
// block is filling; each completed block then costs 2 unready cycles for
// the two compression rounds (one full SipRound per cycle — four 64-bit
// adds and six rotates is comfortable combinational depth at 250 MHz).
// Worst case is 10 cycles per 8 bytes = 200 MB/s at axis_aclk, far above
// the ~30.5 MB/s wire ingest the contract sizes against.
//
// Handshake (all pulses are one cycle, sampled on posedge clk):
//   key_set                latches k0/k1.  The key registers are read
//                          only at msg_start (v-state initialization),
//                          so an in-flight message is untouched: it
//                          completes, and digests, under the key it
//                          started with.
//   msg_start              begins a new message under the latched key.
//   msg_valid/msg_byte     one message byte; accepted only when
//                          msg_ready is high (holds off during
//                          compression rounds).
//   msg_end                finalize; assert when msg_ready is high.  May
//                          coincide with msg_valid to mark the final
//                          byte, or arrive alone — msg_start followed by
//                          a bare msg_end digests the empty message.
//   dig_valid/dig          one-cycle result pulse; dig holds until the
//                          next message completes.
//
// Verified against the 64 official test vectors from the SipHash paper
// (key 000102..0f, message bytes 00..len-1, lengths 0..63) by
// tests/hw/tb_pyro_siphash.v; the same vectors gate the Python model,
// so the two cannot drift apart silently.
//
// Async active-low reset, matching the engine files in this directory
// (the rp_wrapper template is sync-reset; the wrapper tolerates either
// on its engine side).

`timescale 1ns/1ps
`default_nettype none

module pyro_siphash (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [63:0] k0,
    input  wire [63:0] k1,
    input  wire        key_set,
    input  wire        msg_start,
    input  wire        msg_valid,
    input  wire [7:0]  msg_byte,
    input  wire        msg_end,
    output wire        msg_ready,
    output reg         dig_valid,
    output reg  [63:0] dig
);

    // SipHash initialization constants ("somepseudo..." from the paper).
    localparam [63:0] C0 = 64'h736f6d6570736575;
    localparam [63:0] C1 = 64'h646f72616e646f6d;
    localparam [63:0] C2 = 64'h6c7967656e657261;
    localparam [63:0] C3 = 64'h7465646279746573;

    localparam [1:0] S_IDLE   = 2'd0;  // waiting for msg_start
    localparam [1:0] S_ABSORB = 2'd1;  // accepting bytes / msg_end
    localparam [1:0] S_COMP   = 2'd2;  // c=2 rounds for one block
    localparam [1:0] S_FIN    = 2'd3;  // d=4 finalization rounds

    reg [1:0]  state;
    reg [2:0]  rounds_left;   // rounds remaining in S_COMP / S_FIN
    reg        final_blk;     // block in S_COMP is the length block
    reg        end_pend;      // msg_end arrived with the block-filling
                              // byte; finalize once compression returns

    reg [63:0] key0_q, key1_q;
    reg [63:0] v0, v1, v2, v3;
    reg [63:0] m_hold;        // block being compressed (for v0 ^= m)
    reg [63:0] mbuf;          // little-endian block accumulator; kept
                              // zero above blen so the final-block pad
                              // needs no masking
    reg [2:0]  blen;          // bytes held in mbuf, 0..7
    reg [7:0]  tlen;          // total message length mod 256 (the
                              // length byte of the final block)

    // One full SipRound, combinational from the current v0..v3.
    reg [63:0] rv0, rv1, rv2, rv3;
    reg [63:0] sr_a0, sr_a1, sr_a2, sr_a3, sr_b0, sr_c2;
    always @* begin
        sr_a0 = v0 + v1;
        sr_a1 = {v1[50:0], v1[63:51]} ^ sr_a0;      // rotl(v1, 13)
        sr_b0 = {sr_a0[31:0], sr_a0[63:32]};        // rotl(v0, 32)
        sr_a2 = v2 + v3;
        sr_a3 = {v3[47:0], v3[63:48]} ^ sr_a2;      // rotl(v3, 16)
        rv0   = sr_b0 + sr_a3;
        rv3   = {sr_a3[42:0], sr_a3[63:43]} ^ rv0;  // rotl(v3, 21)
        sr_c2 = sr_a2 + sr_a1;
        rv1   = {sr_a1[46:0], sr_a1[63:47]} ^ sr_c2; // rotl(v1, 17)
        rv2   = {sr_c2[31:0], sr_c2[63:32]};        // rotl(v2, 32)
    end

    // If key_set coincides with msg_start, the new key wins.
    wire [63:0] ek0 = key_set ? k0 : key0_q;
    wire [63:0] ek1 = key_set ? k1 : key1_q;

    // The block completed by the byte being accepted this cycle.
    wire [63:0] m_full = {msg_byte, mbuf[55:0]};

    // Final block: pad zeros are already in mbuf; length byte on top.
    wire [63:0] m_last = mbuf | {tlen, 56'd0};

    assign msg_ready = (state == S_ABSORB) && !end_pend;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state       <= S_IDLE;
            rounds_left <= 3'd0;
            final_blk   <= 1'b0;
            end_pend    <= 1'b0;
            key0_q      <= 64'd0;
            key1_q      <= 64'd0;
            v0 <= 64'd0;  v1 <= 64'd0;  v2 <= 64'd0;  v3 <= 64'd0;
            m_hold      <= 64'd0;
            mbuf        <= 64'd0;
            blen        <= 3'd0;
            tlen        <= 8'd0;
            dig_valid   <= 1'b0;
            dig         <= 64'd0;
        end else begin
            dig_valid <= 1'b0;

            case (state)
              S_IDLE: begin
                if (msg_start) begin
                    v0    <= ek0 ^ C0;
                    v1    <= ek1 ^ C1;
                    v2    <= ek0 ^ C2;
                    v3    <= ek1 ^ C3;
                    mbuf  <= 64'd0;
                    blen  <= 3'd0;
                    tlen  <= 8'd0;
                    state <= S_ABSORB;
                end
              end

              S_ABSORB: begin
                if (end_pend) begin
                    // msg_end rode in with the byte that completed the
                    // previous block; compression is done, finalize now.
                    end_pend    <= 1'b0;
                    v3          <= v3 ^ m_last;
                    m_hold      <= m_last;
                    final_blk   <= 1'b1;
                    rounds_left <= 3'd2;
                    state       <= S_COMP;
                end else if (msg_valid) begin
                    tlen <= tlen + 8'd1;
                    if (blen == 3'd7) begin
                        // Eighth byte: compress the completed block.
                        v3          <= v3 ^ m_full;
                        m_hold      <= m_full;
                        mbuf        <= 64'd0;
                        blen        <= 3'd0;
                        final_blk   <= 1'b0;
                        rounds_left <= 3'd2;
                        state       <= S_COMP;
                        if (msg_end)
                            end_pend <= 1'b1;
                    end else begin
                        mbuf[8*blen +: 8] <= msg_byte;
                        blen              <= blen + 3'd1;
                        if (msg_end)
                            end_pend <= 1'b1;
                    end
                end else if (msg_end) begin
                    v3          <= v3 ^ m_last;
                    m_hold      <= m_last;
                    final_blk   <= 1'b1;
                    rounds_left <= 3'd2;
                    state       <= S_COMP;
                end
              end

              S_COMP: begin
                rounds_left <= rounds_left - 3'd1;
                v1 <= rv1;
                v3 <= rv3;
                if (rounds_left == 3'd1) begin
                    // Last compression round: fold in v0 ^= m, and for
                    // the length block also v2 ^= 0xff, saving a cycle.
                    v0 <= rv0 ^ m_hold;
                    if (final_blk) begin
                        v2          <= rv2 ^ 64'h00000000000000ff;
                        rounds_left <= 3'd4;
                        state       <= S_FIN;
                    end else begin
                        v2    <= rv2;
                        state <= S_ABSORB;
                    end
                end else begin
                    v0 <= rv0;
                    v2 <= rv2;
                end
              end

              S_FIN: begin
                rounds_left <= rounds_left - 3'd1;
                v0 <= rv0;
                v1 <= rv1;
                v2 <= rv2;
                v3 <= rv3;
                if (rounds_left == 3'd1) begin
                    dig       <= rv0 ^ rv1 ^ rv2 ^ rv3;
                    dig_valid <= 1'b1;
                    state     <= S_IDLE;
                end
              end

              default: state <= S_IDLE;
            endcase

            // The key registers are consumed ONLY at msg_start (the
            // v0..v3 initialization), so latching here never disturbs
            // an in-flight message: it completes under the key it
            // started with — no digest can span two keys, and a
            // mid-frame commit cannot wedge the feed handshake (an
            // abort-to-idle here would kill msg_ready with the engine
            // FSM still waiting on it).  key_set + msg_start in the
            // same cycle starts under the new key (ek0/ek1 above).
            if (key_set) begin
                key0_q <= k0;
                key1_q <= k1;
            end
        end
    end

endmodule

`default_nettype wire
