// *************************************************************************
// OQ-2 wire-rate spike: self-paced CMAC TX frame generator.
//
// Emits one 64-byte single-beat frame every GAP_CYCLES cycles, AXIS-
// compliant (tvalid held until tready).  The frame is a minimal
// Ethernet frame: broadcast dst, locally-administered src, ethertype
// 0x88B5 (the PYRO local-experiments ethertype), then an incrementing
// 32-bit sequence number and zero padding.  With CMAC near-end
// loopback these frames return on RX and exercise the wire-tap path
// with no cable and no host involvement.
//
// Deliberately free-running and decoupled from H2C: the wire side
// must never be able to stall host control.
// *************************************************************************
`timescale 1ns/1ps

module pyro_wire_tx_gen #(
  parameter [31:0] GAP_CYCLES = 32'd1024
) (
  input          clk,
  input          rstn,

  output         m_tvalid,
  output [511:0] m_tdata,
  output  [63:0] m_tkeep,
  output         m_tlast,
  output  [15:0] m_tuser_size,
  input          m_tready
);

  reg [31:0] gap_q;
  reg [31:0] seq_q;
  reg        pend_q;

  // dst ff:ff:ff:ff:ff:ff, src 02:00:00:00:00:01, ethertype 0x88B5,
  // then the sequence number.  Byte 0 of the frame is tdata[7:0]
  // (AXIS byte order), so fields are assembled low-byte-first.
  wire [511:0] frame = {
      {368{1'b0}},                       // pad to 64 B
      seq_q,                             // bytes 14..17
      16'hB588,                          // ethertype 0x88B5 (LE bytes)
      48'h01_00_00_00_00_02,             // src 02:00:00:00:00:01
      48'hFF_FF_FF_FF_FF_FF              // dst broadcast
  };

  always @(posedge clk) begin
    if (!rstn) begin
      gap_q  <= 32'd0;
      seq_q  <= 32'd0;
      pend_q <= 1'b0;
    end else if (pend_q) begin
      if (m_tready) begin
        pend_q <= 1'b0;
        seq_q  <= seq_q + 32'd1;
        gap_q  <= 32'd0;
      end
    end else if (gap_q >= GAP_CYCLES) begin
      pend_q <= 1'b1;
    end else begin
      gap_q <= gap_q + 32'd1;
    end
  end

  assign m_tvalid     = pend_q;
  assign m_tdata      = frame;
  assign m_tkeep      = 64'hFFFF_FFFF_FFFF_FFFF;   // full 64-byte beat
  assign m_tlast      = 1'b1;
  assign m_tuser_size = 16'd64;

endmodule: pyro_wire_tx_gen
