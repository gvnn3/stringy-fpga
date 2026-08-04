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

  // Register isolation: a 2-deep skid on every face (both slaves and
  // the master).  The first wiretap static build showed why — with a
  // purely combinational arbiter, the QDMA-slice -> RP ingress path
  // and the tready fan-back crossed an SLR boundary at 7 LUT levels
  // (WNS -0.150 / -0.105 on axis_aclk_0).  With skids, every signal
  // leaving this module is register-sourced in both directions.
  wire         a0_tvalid, a1_tvalid;
  wire [511:0] a0_tdata,  a1_tdata;
  wire  [63:0] a0_tkeep,  a1_tkeep;
  wire         a0_tlast,  a1_tlast;
  wire  [47:0] a0_tuser,  a1_tuser;
  wire         a0_tready, a1_tready;

  pyro_axis_skid skid_s0 (
    .clk (clk), .rstn (rstn),
    .s_tvalid (s0_tvalid), .s_tdata (s0_tdata), .s_tkeep (s0_tkeep),
    .s_tlast (s0_tlast), .s_tuser (s0_tuser), .s_tready (s0_tready),
    .m_tvalid (a0_tvalid), .m_tdata (a0_tdata), .m_tkeep (a0_tkeep),
    .m_tlast (a0_tlast), .m_tuser (a0_tuser), .m_tready (a0_tready)
  );

  pyro_axis_skid skid_s1 (
    .clk (clk), .rstn (rstn),
    .s_tvalid (s1_tvalid), .s_tdata (s1_tdata), .s_tkeep (s1_tkeep),
    .s_tlast (s1_tlast), .s_tuser (s1_tuser), .s_tready (s1_tready),
    .m_tvalid (a1_tvalid), .m_tdata (a1_tdata), .m_tkeep (a1_tkeep),
    .m_tlast (a1_tlast), .m_tuser (a1_tuser), .m_tready (a1_tready)
  );

  wire         c_tvalid;
  wire [511:0] c_tdata;
  wire  [63:0] c_tkeep;
  wire         c_tlast;
  wire  [47:0] c_tuser;
  wire         c_tready;

  // AXI-Stream stability: the grant freezes the moment a beat is
  // PRESENTED (c_tvalid high), not merely accepted — otherwise the
  // other port asserting tvalid during a stall would swap c_tdata
  // under an asserted c_tvalid, which AXIS forbids.  The grant is
  // released only when the packet's tlast beat is accepted.
  reg  locked;      // grant frozen (presentation or mid-packet)
  reg  grant;       // 0 = s0, 1 = s1
  reg  last_grant;  // round-robin state

  wire idle_pick =
      (last_grant == 1'b0) ? (a1_tvalid ? 1'b1 : 1'b0)
                           : (a0_tvalid ? 1'b0 : 1'b1);

  wire cur = locked ? grant : idle_pick;

  wire cur_tvalid = cur ? a1_tvalid : a0_tvalid;
  wire cur_tlast  = cur ? a1_tlast  : a0_tlast;

  wire beat = cur_tvalid && c_tready;

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

  assign c_tvalid = cur_tvalid;
  assign c_tdata  = cur ? a1_tdata : a0_tdata;
  assign c_tkeep  = cur ? a1_tkeep : a0_tkeep;
  assign c_tlast  = cur_tlast;
  assign c_tuser  = cur ? a1_tuser : a0_tuser;

  assign a0_tready = (cur == 1'b0) && c_tready;
  assign a1_tready = (cur == 1'b1) && c_tready;

  pyro_axis_skid skid_m (
    .clk (clk), .rstn (rstn),
    .s_tvalid (c_tvalid), .s_tdata (c_tdata), .s_tkeep (c_tkeep),
    .s_tlast (c_tlast), .s_tuser (c_tuser), .s_tready (c_tready),
    .m_tvalid (m_tvalid), .m_tdata (m_tdata), .m_tkeep (m_tkeep),
    .m_tlast (m_tlast), .m_tuser (m_tuser), .m_tready (m_tready)
  );

endmodule: pyro_axis_wire_arb
