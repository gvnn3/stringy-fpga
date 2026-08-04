// *************************************************************************
// OQ-2 wire-rate spike: 2-deep AXIS skid buffer (register slice).
//
// Full register isolation in both directions: m_* outputs come from
// registers, and s_tready is a function of the occupancy count only.
// No combinational path crosses the module in either direction, at
// full throughput (1 beat/cycle sustained).
//
// Added after the first wiretap static build: the combinational
// arbiter stretched the QDMA-slice -> RP ingress and tready fan-back
// paths across an SLR crossing (WNS -0.150 / -0.105, axis_aclk_0).
// A skid on each arbiter face returns those to register-to-register
// hops.
// *************************************************************************
`timescale 1ns/1ps

module pyro_axis_skid (
  input          clk,
  input          rstn,

  input          s_tvalid,
  input  [511:0] s_tdata,
  input   [63:0] s_tkeep,
  input          s_tlast,
  input   [47:0] s_tuser,
  output         s_tready,

  output         m_tvalid,
  output [511:0] m_tdata,
  output  [63:0] m_tkeep,
  output         m_tlast,
  output  [47:0] m_tuser,
  input          m_tready
);

  localparam W = 512 + 64 + 1 + 48;

  reg [W-1:0] q0, q1;
  reg   [1:0] cnt;

  wire [W-1:0] s_flat = {s_tdata, s_tkeep, s_tlast, s_tuser};

  wire push = s_tvalid && (cnt < 2'd2);
  wire pop  = (cnt != 2'd0) && m_tready;

  always @(posedge clk) begin
    if (!rstn) begin
      cnt <= 2'd0;
    end else begin
      case ({push, pop})
        2'b10: cnt <= cnt + 2'd1;
        2'b01: cnt <= cnt - 2'd1;
        default: ;
      endcase
      if (push) begin
        if (cnt == 2'd0 || (cnt == 2'd1 && pop))
          q0 <= s_flat;
        else
          q1 <= s_flat;
      end
      if (pop && cnt == 2'd2)
        q0 <= q1;
    end
  end

  assign s_tready = (cnt < 2'd2);
  assign m_tvalid = (cnt != 2'd0);
  assign {m_tdata, m_tkeep, m_tlast, m_tuser} = q0;

endmodule: pyro_axis_skid
