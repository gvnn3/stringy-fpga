// PYRO MAC engine top — pyro_circuit-ABI shim around pyro_mac_engine.
//
// Multi-program MAC contract, P1 program.  Presents EXACTLY the generated
// pyro_circuit port list (the repo's one-ABI rule), so the multi-program
// wrapper can host it beside the A5 overlay engine with no new ports and
// no R80 boundary change.  Result mapping per the contract:
//
//   one res_wr pulse per DIGESTED frame:
//     res_start      = digest[63:0]
//     res_end[15:0]  = pkt_len (upper bits 0)
//     res_pattern_id = record flags (bit0 ipv4 .. bit6 truncated)
//     res_flags      = key_id
//   skipped frames produce NO res_wr.
//
// CSR map (engine-internal wrapper<->engine space; the existing map ends
// at 0x0084, this engine starts at 0x0090):
//   0x0090..0x009C KEY0-3   key words, little-endian word order (KEY0 =
//                           key bytes 0-3); WRITE-ONLY — key material is
//                           never readable back, reads return 0
//   0x00A0 KEYCTRL          write bit0 = commit, bit1 = clear/invalidate
//                           (bit1 wins if both are set)
//   0x00A4 KEY_ID           r/w, echoed in res_flags of every record
//   0x00A8 KEYCHECK_LO      SipHash-2-4 of "PYROMACKEYCHECK1" under the
//   0x00AC KEYCHECK_HI      just-committed key; valid once MACSTAT bit0
//   0x00B0 MACSTAT          bit0 key_valid, bit1 commit-in-progress
//   0x00B4 SEEN             zero-slack counters:
//   0x00B8 DIGESTED           SEEN == DIGESTED + SKIP_NONIP + SKIP_NOKEY
//   0x00BC SKIP_NONIP        EXACTLY (every frame lands in one bucket)
//   0x00C0 SKIP_NOKEY
//
// A commit is a SEQUENCE, not an edge: the key latches into both SipHash
// cores, then the 16-byte keycheck string streams through a dedicated
// second core (~30 cycles).  MACSTAT bit0 rises only when KEYCHECK_LO/HI
// are real; poll it before reading them.  The second core exists so a
// commit never has to arbitrate against a frame in flight on the frame
// core — the price is ~350 FFs, the alternative was a shared-core
// arbiter with a mid-frame corner.  Fail-closed: before the first
// commit completes, every frame counts skip_nokey and nothing digests.
//
// The wrapper drives every engine through RESET -> OUT_CAP -> START ->
// wait BUSY -> feed -> wait DONE (the overlay engine learned this the
// hard way: sharing a port list is not the same as sharing a protocol),
// so a minimal A_CTRL/A_STATUS/A_OUT_COUNT shim answers that handshake:
// START raises BUSY, the per-frame event raises DONE, OUT_COUNT counts
// results since RESET (0 for a skipped frame, 1 for a digested one).
// CTRL.RESET deliberately does NOT touch the MAC counters or the key:
// those are program-lifetime state read by MAC_STAT_REQUEST, and the
// wrapper pulses RESET before every scan (W4).
//
// CSR read timing: csr_rdata is REGISTERED from the current csr_addr
// (the overlay engine's N+2 pattern); the wrapper holds addresses two
// cycles, which tolerates exactly this.

`timescale 1ns/1ps
`default_nettype none

module pyro_mac_engine_top (
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

    localparam [31:0] MAGIC = 32'h504D4143;          // "PMAC"

    // R45 harness shim.
    localparam [15:0] A_CTRL         = 16'h0010;  // bit0 START, bit1 RESET
    localparam [15:0] A_STATUS       = 16'h0014;  // bit0 BUSY, bit1 DONE
    localparam [15:0] A_CIRC_ID0     = 16'h0018;
    localparam [15:0] A_OUT_CAP      = 16'h0048;
    localparam [15:0] A_OUT_COUNT    = 16'h004C;
    // MAC program CSRs (contract).
    localparam [15:0] A_KEY0         = 16'h0090;
    localparam [15:0] A_KEY1         = 16'h0094;
    localparam [15:0] A_KEY2         = 16'h0098;
    localparam [15:0] A_KEY3         = 16'h009C;
    localparam [15:0] A_KEYCTRL      = 16'h00A0;
    localparam [15:0] A_KEY_ID       = 16'h00A4;
    localparam [15:0] A_KEYCHECK_LO  = 16'h00A8;
    localparam [15:0] A_KEYCHECK_HI  = 16'h00AC;
    localparam [15:0] A_MACSTAT      = 16'h00B0;
    localparam [15:0] A_SEEN         = 16'h00B4;
    localparam [15:0] A_DIGESTED     = 16'h00B8;
    localparam [15:0] A_SKIP_NONIP   = 16'h00BC;
    localparam [15:0] A_SKIP_NOKEY   = 16'h00C0;

    // Keep in step with pyro_mac_engine.
    localparam [1:0] EV_DIGESTED = 2'd0,
                     EV_NONIP    = 2'd1,
                     EV_NOKEY    = 2'd2;

    // The keycheck plaintext (contract kind 0x10): the exact 16-byte
    // ASCII string, fed first character first.
    localparam [127:0] KC_STR = "PYROMACKEYCHECK1";

    localparam [2:0] KC_IDLE  = 3'd0,
                     KC_SET   = 3'd1,   // key_set pulse to both cores
                     KC_START = 3'd2,   // msg_start pulse
                     KC_FEED  = 3'd3,   // stream the 16 bytes
                     KC_END   = 3'd4,   // msg_end once core is ready
                     KC_WAIT  = 3'd5;   // wait dig_valid, publish

    reg [31:0] key_w0, key_w1, key_w2, key_w3;
    reg [31:0] key_id;
    reg [31:0] key_id_active;   // key_id as of the last COMPLETED commit
    reg        key_valid;
    reg [63:0] keycheck;
    reg [2:0]  kcst;
    reg [3:0]  kc_idx;
    reg        key_set_p, kc_start_p, kc_end_p;

    // Key bytes 0-7 = k0 little-endian, 8-15 = k1 (SipHash reference
    // convention); KEY0 holds key bytes 0-3, so k0 = {KEY1, KEY0}.
    wire [63:0] k0 = {key_w1, key_w0};
    wire [63:0] k1 = {key_w3, key_w2};
    wire        kc_busy = (kcst != KC_IDLE);

    // Harness shim state.
    reg        busy, done_f;
    reg [31:0] out_cap, out_count;

    // Zero-slack counters.
    reg [31:0] seen, digested, skip_nonip, skip_nokey;

    // ---------------- frame datapath ------------------------------------
    wire        evt_done;
    wire [1:0]  evt_kind;
    wire [63:0] evt_digest;
    wire [15:0] evt_len;
    wire [6:0]  evt_flags;

    pyro_mac_engine engine (
        .clk(clk), .rst_n(rst_n),
        .k0(k0), .k1(k1), .key_set(key_set_p), .key_valid(key_valid),
        .in_valid(in_valid), .in_data(in_data), .in_last(in_last),
        .in_ready(in_ready),
        .evt_done(evt_done), .evt_kind(evt_kind),
        .evt_digest(evt_digest), .evt_len(evt_len),
        .evt_flags(evt_flags)
    );

    // ---------------- keycheck core -------------------------------------
    wire        kc_ready;
    wire        kc_dvld;
    wire [63:0] kc_dig;
    wire        kc_msg_valid = (kcst == KC_FEED);
    wire [7:0]  kc_byte = KC_STR[8 * (4'd15 - kc_idx) +: 8];

    pyro_siphash kc_sip (
        .clk(clk), .rst_n(rst_n),
        .k0(k0), .k1(k1), .key_set(key_set_p),
        .msg_start(kc_start_p),
        .msg_valid(kc_msg_valid),
        .msg_byte(kc_byte),
        .msg_ready(kc_ready),
        .msg_end(kc_end_p),
        .dig_valid(kc_dvld),
        .dig(kc_dig)
    );

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            key_w0 <= 32'd0; key_w1 <= 32'd0;
            key_w2 <= 32'd0; key_w3 <= 32'd0;
            key_id <= 32'd0; key_id_active <= 32'd0;
            key_valid <= 1'b0; keycheck <= 64'd0;
            kcst <= KC_IDLE; kc_idx <= 4'd0;
            key_set_p <= 1'b0; kc_start_p <= 1'b0; kc_end_p <= 1'b0;
            busy <= 1'b0; done_f <= 1'b0;
            out_cap <= 32'd0; out_count <= 32'd0;
            seen <= 32'd0; digested <= 32'd0;
            skip_nonip <= 32'd0; skip_nokey <= 32'd0;
            res_wr <= 1'b0; res_start <= 64'd0; res_end <= 64'd0;
            res_pattern_id <= 32'd0; res_flags <= 32'd0;
            csr_rdata <= 32'd0;
        end else begin
            res_wr     <= 1'b0;
            key_set_p  <= 1'b0;
            kc_start_p <= 1'b0;
            kc_end_p   <= 1'b0;

            // ---- CSR writes ----
            if (csr_write) begin
                case (csr_addr)
                A_CTRL: begin
                    if (csr_wdata[1]) begin          // RESET (per scan)
                        busy <= 1'b0; done_f <= 1'b0;
                        out_count <= 32'd0;
                        // MAC counters and key state deliberately kept.
                    end
                    if (csr_wdata[0]) begin          // START
                        busy <= 1'b1; done_f <= 1'b0;
                    end
                end
                A_OUT_CAP: out_cap <= csr_wdata;
                A_KEY0:    key_w0  <= csr_wdata;
                A_KEY1:    key_w1  <= csr_wdata;
                A_KEY2:    key_w2  <= csr_wdata;
                A_KEY3:    key_w3  <= csr_wdata;
                A_KEY_ID:  key_id  <= csr_wdata;
                A_KEYCTRL: begin
                    if (csr_wdata[1]) begin          // clear/invalidate
                        key_valid <= 1'b0;
                        keycheck  <= 64'd0;
                        key_w0 <= 32'd0; key_w1 <= 32'd0;
                        key_w2 <= 32'd0; key_w3 <= 32'd0;
                        kcst <= KC_IDLE;             // abort any commit
                    end else if (csr_wdata[0] && !kc_busy) begin
                        // Commit: latch the key into both cores, then
                        // run the keycheck stream.  A second commit
                        // while one is in flight is ignored.
                        key_set_p <= 1'b1;
                        kcst <= KC_SET;
                    end
                end
                default: ;
                endcase
            end

            // ---- keycheck sequencer ----
            case (kcst)
            KC_SET: begin
                kc_start_p <= 1'b1;
                kcst <= KC_START;
            end
            KC_START: begin
                kc_idx <= 4'd0;
                kcst <= KC_FEED;
            end
            KC_FEED: begin
                if (kc_ready) begin
                    if (kc_idx == 4'd15) kcst <= KC_END;
                    kc_idx <= kc_idx + 4'd1;
                end
            end
            KC_END: begin
                // Byte 15 completed a word; wait out the compression.
                if (kc_ready) begin
                    kc_end_p <= 1'b1;
                    kcst <= KC_WAIT;
                end
            end
            KC_WAIT: begin
                if (kc_dvld) begin
                    keycheck      <= kc_dig;
                    key_valid     <= 1'b1;
                    key_id_active <= key_id;
                    kcst <= KC_IDLE;
                end
            end
            default: ;
            endcase

            // ---- per-frame event ----
            if (evt_done) begin
                seen   <= seen + 32'd1;
                done_f <= 1'b1;
                busy   <= 1'b0;
                case (evt_kind)
                EV_DIGESTED: begin
                    digested  <= digested + 32'd1;
                    out_count <= out_count + 32'd1;
                    res_wr         <= 1'b1;
                    res_start      <= evt_digest;
                    res_end        <= {48'd0, evt_len};
                    res_pattern_id <= {25'd0, evt_flags};
                    res_flags      <= key_id_active;
                end
                EV_NONIP: skip_nonip <= skip_nonip + 32'd1;
                EV_NOKEY: skip_nokey <= skip_nokey + 32'd1;
                default: ;
                endcase
            end

            // ---- registered CSR read mux (N+2, overlay pattern) ----
            case (csr_addr)
            A_CIRC_ID0:    csr_rdata <= MAGIC;
            A_STATUS:      csr_rdata <= {30'd0, done_f, busy};
            A_OUT_CAP:     csr_rdata <= out_cap;
            A_OUT_COUNT:   csr_rdata <= out_count;
            A_KEY_ID:      csr_rdata <= key_id;
            A_KEYCHECK_LO: csr_rdata <= keycheck[31:0];
            A_KEYCHECK_HI: csr_rdata <= keycheck[63:32];
            A_MACSTAT:     csr_rdata <= {30'd0, kc_busy, key_valid};
            A_SEEN:        csr_rdata <= seen;
            A_DIGESTED:    csr_rdata <= digested;
            A_SKIP_NONIP:  csr_rdata <= skip_nonip;
            A_SKIP_NOKEY:  csr_rdata <= skip_nokey;
            default:       csr_rdata <= 32'd0;   // incl. KEY0-3: no
                                                 // key readback
            endcase
        end
    end

endmodule
`default_nettype wire
