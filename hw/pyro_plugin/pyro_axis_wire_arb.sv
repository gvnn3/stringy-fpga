// *************************************************************************
// OQ-2 wire-rate spike: 2:1 packet-atomic AXIS arbiter.
//
// Merges two 512-bit AXI-Stream sources (port 0 = QDMA H2C, port 1 =
// CMAC RX via the 250 MHz adapter) onto the single pyro_rp ingress.
// Frames keep their tuser, so the RP distinguishes host (src 0x0001)
// from wire (src 0x0040) inside the frozen R80 boundary.
//
// Properties:
//   * Packet-atomic: once a packet's first beat is accepted, the
//     grant is locked until its tlast beat is accepted.  Beats of
//     the two ports never interleave.
//   * Round-robin: at each idle-to-busy transition, the port after
//     the last-granted one wins ties.
//   * Combinational pass-through (no store-and-forward, no beat
//     buffering): tready of the granted port is the master's tready;
//     the non-granted port sees tready low and stalls.  Backpressure
//     from the RP therefore reaches both sources unmodified; CMAC RX
//     loss under sustained stall happens (counted) in the adapter RX
//     FIFO, not silently here.
// *************************************************************************
`timescale 1ns/1ps

module pyro_axis_wire_arb (
  input             clk,
  input             rstn,

  input             s0_tvalid,
  input     [511:0] s0_tdata,
  input      [63:0] s0_tkeep,
  input             s0_tlast,
  input      [47:0] s0_tuser,
  output            s0_tready,

  input             s1_tvalid,
  input     [511:0] s1_tdata,
  input      [63:0] s1_tkeep,
  input             s1_tlast,
  input      [47:0] s1_tuser,
  output            s1_tready,

  output            m_tvalid,
  output    [511:0] m_tdata,
  output     [63:0] m_tkeep,
  output            m_tlast,
  output     [47:0] m_tuser,
  input             m_tready
);

  // AXI-Stream stability: the grant freezes the moment a beat is
  // PRESENTED (m_tvalid high), not merely accepted — otherwise the
  // other port asserting tvalid during a stall would swap m_tdata
  // under an asserted m_tvalid, which AXIS forbids.  The grant is
  // released only when the packet's tlast beat is accepted.
  reg  locked;      // grant frozen (presentation or mid-packet)
  reg  grant;       // 0 = s0, 1 = s1
  reg  last_grant;  // round-robin state

  wire idle_pick =
      (last_grant == 1'b0) ? (s1_tvalid ? 1'b1 : 1'b0)
                           : (s0_tvalid ? 1'b0 : 1'b1);

  wire cur = locked ? grant : idle_pick;

  wire cur_tvalid = cur ? s1_tvalid : s0_tvalid;
  wire cur_tlast  = cur ? s1_tlast  : s0_tlast;

  wire beat = cur_tvalid && m_tready;

  always @(posedge clk) begin
    if (!rstn) begin
      locked     <= 1'b0;
      grant      <= 1'b0;
      last_grant <= 1'b1;   // so s0 wins the first tie
    end else if (beat && cur_tlast) begin
      locked     <= 1'b0;   // packet done: re-arbitrate next cycle
      last_grant <= cur;
    end else if (cur_tvalid) begin
      locked <= 1'b1;       // freeze on presentation
      grant  <= cur;
    end
  end

  assign m_tvalid = cur_tvalid;
  assign m_tdata  = cur ? s1_tdata : s0_tdata;
  assign m_tkeep  = cur ? s1_tkeep : s0_tkeep;
  assign m_tlast  = cur_tlast;
  assign m_tuser  = cur ? s1_tuser : s0_tuser;

  assign s0_tready = (cur == 1'b0) && m_tready;
  assign s1_tready = (cur == 1'b1) && m_tready;

endmodule: pyro_axis_wire_arb
