// *************************************************************************
// PYRO Phase 2b - OpenNIC box_250mhz user plugin.
//
// Replaces the stock `p2p_250mhz` passthrough box. Instead of splicing QDMA H2C
// out to the CMAC and CMAC RX back to QDMA C2H, this box routes:
//
//     QDMA H2C (host -> card)  ->  pyro_rp  ->  QDMA C2H (card -> host)
//
// i.e. a host<->RP loopback *through the card*, never touching a MAC. That is
// what lets the R78 in-band control protocol ride raw Ethernet frames on the
// `onic` netdev with NO 100G transceiver and NO link partner: the frames the
// host AF_PACKET-sends on enpXsYf0 arrive at pyro_rp over H2C, and pyro_rp's
// replies return over C2H to the same netdev.
//
// `pyro_rp` is the DFX reconfigurable partition (R80): a black box here, marked
// HD.RECONFIGURABLE by hw/dfx/build_static.tcl. Its frozen boundary is clk/rstn
// + one 512-bit AXIS slave + one 512-bit AXIS master (tkeep/tlast/tuser), and
// nothing else -- the AXI-Lite MMIO window is terminated in this box and is
// deliberately NOT routed into the RP (R80).
//
// The CMAC datapath is tied off in Phase 2b: adap_tx is held idle and adap_rx
// is
// sunk. Adding a signal across the RP boundary, or un-tying the CMAC path, is a
// static-shell change (new flash, new locked DCP); changing what happens
// *inside*
// pyro_rp is a partial-bitstream-only change (R79).
// *************************************************************************
`include "open_nic_shell_macros.vh"
`timescale 1ns/1ps

module pyro_250mhz #(
  parameter int NUM_QDMA = 1,
  parameter int NUM_INTF = 1,

  // R81 identity, driven into the default ID-stub child baked into the static
  // image. BUILD16 is overridden per build by hw/dfx/build_static.tcl.
  parameter [15:0] PYRO_SPEC16          = 16'h0202,
  parameter [15:0] PYRO_BUILD16         = 16'h0000,
  parameter [31:0] PYRO_HARNESS_VERSION = 32'h0000_0000,
  parameter [15:0] PYRO_SLOT            = 16'd1
) (
  input        [NUM_INTF*2-1:0] s_axil_awvalid,
  input     [32*NUM_INTF*2-1:0] s_axil_awaddr,
  output       [NUM_INTF*2-1:0] s_axil_awready,
  input        [NUM_INTF*2-1:0] s_axil_wvalid,
  input     [32*NUM_INTF*2-1:0] s_axil_wdata,
  output       [NUM_INTF*2-1:0] s_axil_wready,
  output       [NUM_INTF*2-1:0] s_axil_bvalid,
  output     [2*NUM_INTF*2-1:0] s_axil_bresp,
  input        [NUM_INTF*2-1:0] s_axil_bready,
  input        [NUM_INTF*2-1:0] s_axil_arvalid,
  input     [32*NUM_INTF*2-1:0] s_axil_araddr,
  output       [NUM_INTF*2-1:0] s_axil_arready,
  output       [NUM_INTF*2-1:0] s_axil_rvalid,
  output    [32*NUM_INTF*2-1:0] s_axil_rdata,
  output     [2*NUM_INTF*2-1:0] s_axil_rresp,
  input        [NUM_INTF*2-1:0] s_axil_rready,

  input      [NUM_INTF*NUM_QDMA-1:0] s_axis_qdma_h2c_tvalid,
  input  [512*NUM_INTF*NUM_QDMA-1:0] s_axis_qdma_h2c_tdata,
  input   [64*NUM_INTF*NUM_QDMA-1:0] s_axis_qdma_h2c_tkeep,
  input      [NUM_INTF*NUM_QDMA-1:0] s_axis_qdma_h2c_tlast,
  input   [16*NUM_INTF*NUM_QDMA-1:0] s_axis_qdma_h2c_tuser_size,
  input   [16*NUM_INTF*NUM_QDMA-1:0] s_axis_qdma_h2c_tuser_src,
  input   [16*NUM_INTF*NUM_QDMA-1:0] s_axis_qdma_h2c_tuser_dst,
  output     [NUM_INTF*NUM_QDMA-1:0] s_axis_qdma_h2c_tready,

  output     [NUM_INTF*NUM_QDMA-1:0] m_axis_qdma_c2h_tvalid,
  output [512*NUM_INTF*NUM_QDMA-1:0] m_axis_qdma_c2h_tdata,
  output  [64*NUM_INTF*NUM_QDMA-1:0] m_axis_qdma_c2h_tkeep,
  output     [NUM_INTF*NUM_QDMA-1:0] m_axis_qdma_c2h_tlast,
  output  [16*NUM_INTF*NUM_QDMA-1:0] m_axis_qdma_c2h_tuser_size,
  output  [16*NUM_INTF*NUM_QDMA-1:0] m_axis_qdma_c2h_tuser_src,
  output  [16*NUM_INTF*NUM_QDMA-1:0] m_axis_qdma_c2h_tuser_dst,
  input      [NUM_INTF*NUM_QDMA-1:0] m_axis_qdma_c2h_tready,

  output     [NUM_INTF-1:0] m_axis_adap_tx_250mhz_tvalid,
  output [512*NUM_INTF-1:0] m_axis_adap_tx_250mhz_tdata,
  output  [64*NUM_INTF-1:0] m_axis_adap_tx_250mhz_tkeep,
  output     [NUM_INTF-1:0] m_axis_adap_tx_250mhz_tlast,
  output  [16*NUM_INTF-1:0] m_axis_adap_tx_250mhz_tuser_size,
  output  [16*NUM_INTF-1:0] m_axis_adap_tx_250mhz_tuser_src,
  output  [16*NUM_INTF-1:0] m_axis_adap_tx_250mhz_tuser_dst,
  input      [NUM_INTF-1:0] m_axis_adap_tx_250mhz_tready,

  input      [NUM_INTF-1:0] s_axis_adap_rx_250mhz_tvalid,
  input  [512*NUM_INTF-1:0] s_axis_adap_rx_250mhz_tdata,
  input   [64*NUM_INTF-1:0] s_axis_adap_rx_250mhz_tkeep,
  input      [NUM_INTF-1:0] s_axis_adap_rx_250mhz_tlast,
  input   [16*NUM_INTF-1:0] s_axis_adap_rx_250mhz_tuser_size,
  input   [16*NUM_INTF-1:0] s_axis_adap_rx_250mhz_tuser_src,
  input   [16*NUM_INTF-1:0] s_axis_adap_rx_250mhz_tuser_dst,
  output     [NUM_INTF-1:0] s_axis_adap_rx_250mhz_tready,

  input                     mod_rstn,
  output                    mod_rst_done,

  input                     axil_aclk,
  input                     axis_aclk
);

  wire axil_aresetn;

  // Reset is clocked by the 125MHz AXI-Lite clock (same as the stock p2p box).
  generic_reset #(
    .NUM_INPUT_CLK  (1),
    .RESET_DURATION (100)
  ) reset_inst (
    .mod_rstn     (mod_rstn),
    .mod_rst_done (mod_rst_done),
    .clk          (axil_aclk),
    .rstn         (axil_aresetn)
  );

  // AXI-Lite is TERMINATED here, not routed into the RP (R80). The slave must
  // still exist: without it, any MMIO read/write into this box's address window
  // would hang the shell's AXI-Lite crossbar rather than return a response.
  axi_lite_slave #(
    .REG_ADDR_W (12),
    .REG_PREFIX (16'hB000)
  ) reg_inst (
    .s_axil_awvalid (s_axil_awvalid),
    .s_axil_awaddr  (s_axil_awaddr),
    .s_axil_awready (s_axil_awready),
    .s_axil_wvalid  (s_axil_wvalid),
    .s_axil_wdata   (s_axil_wdata),
    .s_axil_wready  (s_axil_wready),
    .s_axil_bvalid  (s_axil_bvalid),
    .s_axil_bresp   (s_axil_bresp),
    .s_axil_bready  (s_axil_bready),
    .s_axil_arvalid (s_axil_arvalid),
    .s_axil_araddr  (s_axil_araddr),
    .s_axil_arready (s_axil_arready),
    .s_axil_rvalid  (s_axil_rvalid),
    .s_axil_rdata   (s_axil_rdata),
    .s_axil_rresp   (s_axil_rresp),
    .s_axil_rready  (s_axil_rready),

    .aclk           (axil_aclk),
    .aresetn        (axil_aresetn)
  );

  generate for (genvar i = 0; i < NUM_INTF; i++) begin : g_intf
    // H2C tuser, packed as pyro_rp expects: {dst[15:0], src[15:0], size[15:0]}
    wire [47:0] h2c_tuser;
    assign h2c_tuser[0+:16]  = s_axis_qdma_h2c_tuser_size[`getvec(16, i)];
    assign h2c_tuser[16+:16] = s_axis_qdma_h2c_tuser_src[`getvec(16, i)];
    assign h2c_tuser[32+:16] = s_axis_qdma_h2c_tuser_dst[`getvec(16, i)];

    wire [47:0] c2h_tuser;

    // ---- the reconfigurable partition (R80) -----------------------------
    // Black box during static synthesis; marked HD.RECONFIGURABLE in
    // hw/dfx/build_static.tcl. dont_touch keeps the cell from being optimized
    // away or absorbed while it is empty.
    (* dont_touch = "true" *)
    pyro_rp #(
      .SPEC16          (PYRO_SPEC16),
      .BUILD16         (PYRO_BUILD16),
      .HARNESS_VERSION (PYRO_HARNESS_VERSION),
      .SLOT            (PYRO_SLOT),
      .RP_CHILD_ID     (32'h0000_0000)   // ID stub default child (R80/R78.5a)
    ) pyro_rp_inst (
      .clk           (axis_aclk),
      .rstn          (axil_aresetn),

      .s_axis_tvalid (s_axis_qdma_h2c_tvalid[i]),
      .s_axis_tdata  (s_axis_qdma_h2c_tdata[`getvec(512, i)]),
      .s_axis_tkeep  (s_axis_qdma_h2c_tkeep[`getvec(64, i)]),
      .s_axis_tlast  (s_axis_qdma_h2c_tlast[i]),
      .s_axis_tuser  (h2c_tuser),
      .s_axis_tready (s_axis_qdma_h2c_tready[i]),

      .m_axis_tvalid (m_axis_qdma_c2h_tvalid[i]),
      .m_axis_tdata  (m_axis_qdma_c2h_tdata[`getvec(512, i)]),
      .m_axis_tkeep  (m_axis_qdma_c2h_tkeep[`getvec(64, i)]),
      .m_axis_tlast  (m_axis_qdma_c2h_tlast[i]),
      .m_axis_tuser  (c2h_tuser),
      .m_axis_tready (m_axis_qdma_c2h_tready[i])
    );

    // C2H tuser back out. `dst` is forced to the PF bitmask exactly as the
    // stock
    // p2p box does -- the QDMA adapter uses it to select the destination queue,
    // so it is the shell's to set, not the RP's.
    assign m_axis_qdma_c2h_tuser_size[`getvec(16, i)] = c2h_tuser[0+:16];
    assign m_axis_qdma_c2h_tuser_src[`getvec(16, i)]  = c2h_tuser[16+:16];
    assign m_axis_qdma_c2h_tuser_dst[`getvec(16, i)]  = 16'h1 << i;

    // ---- CMAC datapath tied off (Phase 2b) ------------------------------
    // No traffic is sent to the network, and anything arriving from it is sunk.
    // This is why bring-up needs no transceiver or link partner.
    assign m_axis_adap_tx_250mhz_tvalid[i]              = 1'b0;
    assign m_axis_adap_tx_250mhz_tdata[`getvec(512, i)] = 512'b0;
    assign m_axis_adap_tx_250mhz_tkeep[`getvec(64, i)]  = 64'b0;
    assign m_axis_adap_tx_250mhz_tlast[i]               = 1'b0;
    assign m_axis_adap_tx_250mhz_tuser_size[`getvec(16, i)] = 16'b0;
    assign m_axis_adap_tx_250mhz_tuser_src[`getvec(16, i)]  = 16'b0;
    assign m_axis_adap_tx_250mhz_tuser_dst[`getvec(16, i)]  = 16'h1 << (6 + i);

    assign s_axis_adap_rx_250mhz_tready[i]              = 1'b1;   // sink
  end
  endgenerate

endmodule: pyro_250mhz
