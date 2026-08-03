// *************************************************************************
// PYRO Phase 2b - default reconfigurable-partition child: the ID stub
// (R80/R81).
//
// This is the `pyro_rp` child baked into the FULL static image at flash time.
// It is what lets `device_usable` flip true from a freshly flashed board,
// before
// any pattern has ever been synthesized (R83): the probe only needs a valid
// ID_REPLY, not a resident circuit.
//
// It presents the SAME frozen R80 boundary as the generated pattern child
// (pyro.hdl.rp_wrapper), so every per-pattern partial links against the same
// locked static DCP (R82b) and can replace this stub in-place over JTAG.
//
// Behaviour (deliberately a strict subset of the pattern child):
// * ID_REQUEST    -> ID_REPLY, rp_child_id == 0 ("no pattern resident",
// R78.5a)
//                      + static_shell_id = (SPEC16 << 16) | BUILD16   (R81)
// * MATCH_REQUEST -> STATUS/ERROR, code = PYRO_E_NOT_RESIDENT (7)
// (R78.8/R80)
// * PERF_REQUEST  -> STATUS/ERROR, code = PYRO_E_NOT_RESIDENT (7)   (no
// engine)
//   * anything else -> silently drained, no reply
//
// It carries NO engine, so it never needs the corpus: only the first beat (the
// 14-byte Ethernet header + 14-byte PYRO control header = 28 bytes) is parsed;
// any remaining beats of a MATCH_REQUEST are drained and discarded. Every reply
// this stub emits is <= 40 bytes, hence exactly ONE 64-byte beat, zero-padded
// to
// the 60-byte L2 minimum (R78.9).
//
// Byte k of a frame lives in tdata[8*k +: 8]. The PYRO control header is
// big-endian (R78.3).
// *************************************************************************
`timescale 1ns/1ps

module pyro_rp #(
  parameter [15:0] SPEC16          = 16'h0202,     // R81: spec 2.2
  parameter [15:0] BUILD16         = 16'h0000,     // R81: build discriminator
  // R78.5b wire/harness version
  parameter [31:0] HARNESS_VERSION = 32'h0000_0000,
  parameter [15:0] SLOT            = 16'd1,        // R87 (echoed, unused here)
  parameter [31:0] RP_CHILD_ID     = 32'h0000_0000 // R78.5a: 0 == ID stub
) (
  input                clk,
  input                rstn,

  // AXI-Stream slave: host -> device (QDMA H2C) frames into the RP (R80)
  input                s_axis_tvalid,
  input        [511:0] s_axis_tdata,
  input         [63:0] s_axis_tkeep,
  input                s_axis_tlast,
  input         [47:0] s_axis_tuser,   // {dst[15:0], src[15:0], size[15:0]}
  output               s_axis_tready,

  // AXI-Stream master: device -> host (QDMA C2H) frames out of the RP (R80)
  output               m_axis_tvalid,
  output       [511:0] m_axis_tdata,
  output        [63:0] m_axis_tkeep,
  output               m_axis_tlast,
  output        [47:0] m_axis_tuser,
  input                m_axis_tready
);

  // ---- R78 wire constants (mirrored from pyro/device.py) -----------------
  localparam [7:0]  KIND_ID_REQ     = 8'h01;
  localparam [7:0]  KIND_ID_REPLY   = 8'h02;
  localparam [7:0]  KIND_MATCH_REQ  = 8'h03;
  localparam [7:0]  KIND_STATUS_ERR = 8'h05;
  localparam [7:0]  KIND_PERF_REQ   = 8'h06;
  localparam [7:0]  PYRO_MAGIC      = 8'h50;   // 'P'
  localparam [7:0]  PYRO_VER        = 8'h01;
  localparam [7:0]  ETH_HI          = 8'h88;   // EtherType 0x88B5, big-endian
  localparam [7:0]  ETH_LO          = 8'hB5;
  // PYRO_E_NOT_RESIDENT (R38/R78.8)
  localparam [31:0] E_NOT_RESIDENT  = 32'd7;

  // Every reply is one beat, zero-padded to the 60-byte L2 minimum (R78.9).
  localparam [15:0] EFF_LEN  = 16'd60;
  localparam [63:0] KEEP_60  = 64'h0FFF_FFFF_FFFF_FFFF;  // low 60 lanes valid

  localparam [1:0] ST_IDLE  = 2'd0,
                   ST_DRAIN = 2'd1,
                   ST_BHDR  = 2'd2,
                   ST_TX    = 2'd3;

  reg  [1:0]   state;
  reg  [223:0] hdr;        // first 28 bytes: Ethernet(14) + PYRO control(14)
  reg          rk_status;  // reply kind: 0 = ID_REPLY, 1 = STATUS/ERROR
  reg  [511:0] tx_word;

  // ---- header decode (combinational, on the captured first beat) ---------
  wire [223:0] h        = (state == ST_IDLE) ? s_axis_tdata[223:0] : hdr;
  wire         eth_ok   = (h[8*12 +: 8] == ETH_HI) && (h[8*13 +: 8] == ETH_LO);
  wire         pyro_ok  = (h[8*14 +: 8] == PYRO_MAGIC)
                       && (h[8*15 +: 8] == PYRO_VER);
  wire [7:0]   kind     = h[8*16 +: 8];
  wire         is_idreq = (kind == KIND_ID_REQ);
  wire         is_err   = (kind == KIND_MATCH_REQ) || (kind == KIND_PERF_REQ);
  wire         accept   = eth_ok && pyro_ok && (is_idreq || is_err);

  // Accept input in IDLE and DRAIN; stall the source only while replying.
  assign s_axis_tready = (state == ST_IDLE) || (state == ST_DRAIN);

  assign m_axis_tvalid = (state == ST_TX);
  assign m_axis_tdata  = tx_word;
  assign m_axis_tkeep  = KEEP_60;
  assign m_axis_tlast  = (state == ST_TX);
  assign m_axis_tuser  = {16'h0000, 16'h0000, EFF_LEN};  // {dst, src, size}

  integer k;
  reg [511:0] wacc;

  always @(posedge clk) begin
    if (!rstn) begin
      state     <= ST_IDLE;
      hdr       <= 224'b0;
      rk_status <= 1'b0;
      tx_word   <= 512'b0;
    end else begin
      case (state)
        // ---- capture the header beat -----------------------------------
        ST_IDLE: begin
          if (s_axis_tvalid) begin
            hdr       <= s_axis_tdata[223:0];
            rk_status <= is_err;
            if (!accept)
              // Not ours (or malformed): drain the rest, emit nothing.
              state <= s_axis_tlast ? ST_IDLE : ST_DRAIN;
            else
              state <= s_axis_tlast ? ST_BHDR : ST_DRAIN;
          end
        end

        // ---- discard the remainder of a multi-beat request --------------
        // The stub has no engine, so a MATCH_REQUEST's corpus is not needed.
        ST_DRAIN: begin
          if (s_axis_tvalid && s_axis_tlast)
            state <= accept ? ST_BHDR : ST_IDLE;
        end

        // ---- build the single reply beat --------------------------------
        ST_BHDR: begin
          wacc = 512'b0;
          // Ethernet: MAC swap (dst <- request src, src <- request dst).
          for (k = 0; k < 6; k = k + 1) begin
            wacc[8*k +: 8]       = hdr[8*(6 + k) +: 8];
            wacc[8*(6 + k) +: 8] = hdr[8*k +: 8];
          end
          wacc[8*12 +: 8] = ETH_HI;
          wacc[8*13 +: 8] = ETH_LO;

          // PYRO control header (R78.3): magic ver kind flags slot seq len resv
          wacc[8*14 +: 8] = PYRO_MAGIC;
          wacc[8*15 +: 8] = PYRO_VER;
          wacc[8*16 +: 8] = rk_status ? KIND_STATUS_ERR : KIND_ID_REPLY;
          wacc[8*17 +: 8] = 8'h00;                  // flags
          wacc[8*18 +: 8] = hdr[8*18 +: 8];         // slot echo (BE)
          wacc[8*19 +: 8] = hdr[8*19 +: 8];
          wacc[8*20 +: 8] = hdr[8*20 +: 8];         // seq echo (BE)
          wacc[8*21 +: 8] = hdr[8*21 +: 8];
          wacc[8*22 +: 8] = hdr[8*22 +: 8];
          wacc[8*23 +: 8] = hdr[8*23 +: 8];
          wacc[8*26 +: 8] = 8'h00;                  // reserved
          wacc[8*27 +: 8] = 8'h00;

          if (!rk_status) begin
            // ID_REPLY payload (R78.5/R78.5a/R78.5b), length 12:
            // static_shell_id (4B BE) | harness_version (4B BE) | rp_child_id
            // (4B BE)
            // static_shell_id = (SPEC16 << 16) | BUILD16  (R81)
            wacc[8*24 +: 8] = 8'h00; wacc[8*25 +: 8] = 8'h0C;
            wacc[8*28 +: 8] = SPEC16[15:8];
            wacc[8*29 +: 8] = SPEC16[7:0];
            wacc[8*30 +: 8] = BUILD16[15:8];
            wacc[8*31 +: 8] = BUILD16[7:0];
            wacc[8*32 +: 8] = HARNESS_VERSION[31:24];
            wacc[8*33 +: 8] = HARNESS_VERSION[23:16];
            wacc[8*34 +: 8] = HARNESS_VERSION[15:8];
            wacc[8*35 +: 8] = HARNESS_VERSION[7:0];
            wacc[8*36 +: 8] = RP_CHILD_ID[31:24];
            wacc[8*37 +: 8] = RP_CHILD_ID[23:16];
            wacc[8*38 +: 8] = RP_CHILD_ID[15:8];
            wacc[8*39 +: 8] = RP_CHILD_ID[7:0];
          end else begin
            // STATUS/ERROR payload (R78.8), length 4: code (4B BE)
            wacc[8*24 +: 8] = 8'h00; wacc[8*25 +: 8] = 8'h04;
            wacc[8*28 +: 8] = E_NOT_RESIDENT[31:24];
            wacc[8*29 +: 8] = E_NOT_RESIDENT[23:16];
            wacc[8*30 +: 8] = E_NOT_RESIDENT[15:8];
            wacc[8*31 +: 8] = E_NOT_RESIDENT[7:0];
          end

          tx_word <= wacc;   // bytes past the payload stay 0 => R78.9 zero-pad
          state   <= ST_TX;
        end

        // ---- present the single reply beat ------------------------------
        ST_TX: begin
          if (m_axis_tready)
            state <= ST_IDLE;
        end

        default: state <= ST_IDLE;
      endcase
    end
  end

endmodule
