// *************************************************************************
// PYRO Phase 2b - `pyro_rp` BLACK BOX stub (the frozen R80 boundary).
//
// An EMPTY module carrying exactly the R80 port list. Vivado elaborates an
// empty
// module definition as a black box, which is what the DFX static synthesis
// needs:
// the static shell is built with pyro_rp unpopulated, then
// hw/dfx/build_static.tcl marks the cell HD.RECONFIGURABLE and links a real
// child
// in with `read_checkpoint -cell`.
//
// THIS FILE DEFINES THE CONTRACT. Every child -- the ID stub
// (hw/src/pyro_id_stub.sv) and every per-pattern partial emitted by
// pyro.hdl.rp_wrapper -- MUST present this exact port list, or it will not link
// against the locked static DCP (R82b). Changing a port here is a static-shell
// change: new build, new flash, new locked DCP, and every existing partial is
// invalidated (R79/R80).
//
// Ports (R80):
//   clk, rstn
//   s_axis_*  512-bit AXI-Stream slave  -- host -> device (QDMA H2C) frames in
//   m_axis_*  512-bit AXI-Stream master -- device -> host (QDMA C2H) frames out
//   tuser is 48 bits: {dst[15:0], src[15:0], size[15:0]}
// and nothing else -- no AXI-Lite (deliberately tied off in Phase 2b, R80).
// *************************************************************************
`timescale 1ns/1ps

(* black_box *)
module pyro_rp #(
  parameter [15:0] SPEC16          = 16'h0202,
  parameter [15:0] BUILD16         = 16'h0000,
  parameter [31:0] HARNESS_VERSION = 32'h0000_0000,
  parameter [15:0] SLOT            = 16'd1,
  parameter [31:0] RP_CHILD_ID     = 32'h0000_0000
) (
  input                clk,
  input                rstn,

  input                s_axis_tvalid,
  input        [511:0] s_axis_tdata,
  input         [63:0] s_axis_tkeep,
  input                s_axis_tlast,
  input         [47:0] s_axis_tuser,
  output               s_axis_tready,

  output               m_axis_tvalid,
  output       [511:0] m_axis_tdata,
  output        [63:0] m_axis_tkeep,
  output               m_axis_tlast,
  output       [47:0]  m_axis_tuser,
  input                m_axis_tready
);
endmodule
