// *************************************************************************
// OQ-2 wiretap spike - box_250mhz user-plugin instantiation
// (WIRE_TAP=1). Production plugin: hw/pyro_plugin.
//
// Included by open_nic_shell.sv (via the plugin include_dirs). Instantiates
// `pyro_250mhz` in place of the stock `p2p_250mhz`.
//
// OpenNIC requires NUM_PHYS_FUNC == NUM_CMAC_PORT; PYRO builds with pf=cmac=1,
// giving exactly one pyro_rp partition.
// *************************************************************************
initial begin
  if (USE_PHYS_FUNC == 0) begin
    $fatal(1, "PYRO: no implementation for USE_PHYS_FUNC = %d", 0);
  end
  if (NUM_PHYS_FUNC != NUM_CMAC_PORT) begin
    $fatal(1,
      "PYRO: no implementation for NUM_PHYS_FUNC (%d) != NUM_CMAC_PORT (%d)",
      NUM_PHYS_FUNC, NUM_CMAC_PORT);
  end
end

localparam C_NUM_USER_BLOCK = 1;

// Tie off "mod_rst_done" bits for every unused reset pair.
assign mod_rst_done[15:C_NUM_USER_BLOCK] = {(16-C_NUM_USER_BLOCK){1'b1}};

pyro_250mhz #(
  .NUM_QDMA    (NUM_QDMA),
  .NUM_INTF    (NUM_PHYS_FUNC),
  .WIRE_TAP    (1'b1)   // OQ-2 wire-rate spike
) pyro_250mhz_inst (
  .s_axil_awvalid                   (axil_p2p_awvalid),
  .s_axil_awaddr                    (axil_p2p_awaddr),
  .s_axil_awready                   (axil_p2p_awready),
  .s_axil_wvalid                    (axil_p2p_wvalid),
  .s_axil_wdata                     (axil_p2p_wdata),
  .s_axil_wready                    (axil_p2p_wready),
  .s_axil_bvalid                    (axil_p2p_bvalid),
  .s_axil_bresp                     (axil_p2p_bresp),
  .s_axil_bready                    (axil_p2p_bready),
  .s_axil_arvalid                   (axil_p2p_arvalid),
  .s_axil_araddr                    (axil_p2p_araddr),
  .s_axil_arready                   (axil_p2p_arready),
  .s_axil_rvalid                    (axil_p2p_rvalid),
  .s_axil_rdata                     (axil_p2p_rdata),
  .s_axil_rresp                     (axil_p2p_rresp),
  .s_axil_rready                    (axil_p2p_rready),

  .s_axis_qdma_h2c_tvalid           (s_axis_qdma_h2c_tvalid),
  .s_axis_qdma_h2c_tdata            (s_axis_qdma_h2c_tdata),
  .s_axis_qdma_h2c_tkeep            (s_axis_qdma_h2c_tkeep),
  .s_axis_qdma_h2c_tlast            (s_axis_qdma_h2c_tlast),
  .s_axis_qdma_h2c_tuser_size       (s_axis_qdma_h2c_tuser_size),
  .s_axis_qdma_h2c_tuser_src        (s_axis_qdma_h2c_tuser_src),
  .s_axis_qdma_h2c_tuser_dst        (s_axis_qdma_h2c_tuser_dst),
  .s_axis_qdma_h2c_tready           (s_axis_qdma_h2c_tready),

  .m_axis_qdma_c2h_tvalid           (m_axis_qdma_c2h_tvalid),
  .m_axis_qdma_c2h_tdata            (m_axis_qdma_c2h_tdata),
  .m_axis_qdma_c2h_tkeep            (m_axis_qdma_c2h_tkeep),
  .m_axis_qdma_c2h_tlast            (m_axis_qdma_c2h_tlast),
  .m_axis_qdma_c2h_tuser_size       (m_axis_qdma_c2h_tuser_size),
  .m_axis_qdma_c2h_tuser_src        (m_axis_qdma_c2h_tuser_src),
  .m_axis_qdma_c2h_tuser_dst        (m_axis_qdma_c2h_tuser_dst),
  .m_axis_qdma_c2h_tready           (m_axis_qdma_c2h_tready),

  .m_axis_adap_tx_250mhz_tvalid     (m_axis_adap_tx_250mhz_tvalid),
  .m_axis_adap_tx_250mhz_tdata      (m_axis_adap_tx_250mhz_tdata),
  .m_axis_adap_tx_250mhz_tkeep      (m_axis_adap_tx_250mhz_tkeep),
  .m_axis_adap_tx_250mhz_tlast      (m_axis_adap_tx_250mhz_tlast),
  .m_axis_adap_tx_250mhz_tuser_size (m_axis_adap_tx_250mhz_tuser_size),
  .m_axis_adap_tx_250mhz_tuser_src  (m_axis_adap_tx_250mhz_tuser_src),
  .m_axis_adap_tx_250mhz_tuser_dst  (m_axis_adap_tx_250mhz_tuser_dst),
  .m_axis_adap_tx_250mhz_tready     (m_axis_adap_tx_250mhz_tready),

  .s_axis_adap_rx_250mhz_tvalid     (s_axis_adap_rx_250mhz_tvalid),
  .s_axis_adap_rx_250mhz_tdata      (s_axis_adap_rx_250mhz_tdata),
  .s_axis_adap_rx_250mhz_tkeep      (s_axis_adap_rx_250mhz_tkeep),
  .s_axis_adap_rx_250mhz_tlast      (s_axis_adap_rx_250mhz_tlast),
  .s_axis_adap_rx_250mhz_tuser_size (s_axis_adap_rx_250mhz_tuser_size),
  .s_axis_adap_rx_250mhz_tuser_src  (s_axis_adap_rx_250mhz_tuser_src),
  .s_axis_adap_rx_250mhz_tuser_dst  (s_axis_adap_rx_250mhz_tuser_dst),
  .s_axis_adap_rx_250mhz_tready     (s_axis_adap_rx_250mhz_tready),

  .mod_rstn                         (mod_rstn[0]),
  .mod_rst_done                     (mod_rst_done[0]),

  .axil_aclk                        (axil_aclk),
  .axis_aclk                        (axis_aclk)
);
