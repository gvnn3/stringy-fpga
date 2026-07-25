// *************************************************************************
//
// Copyright 2020 Xilinx, Inc.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//
// *************************************************************************
// [TODO]
// - Cross-check packet size
// - Parity check
// - Drop error packets
`include "open_nic_shell_macros.vh"
`timescale 1ns/1ps
module qdma_subsystem_h2c #(
  parameter int NUM_PHYS_FUNC = 1
) (
  input                          s_axis_qdma_h2c_tvalid,
  input                  [511:0] s_axis_qdma_h2c_tdata,
  input                   [31:0] s_axis_qdma_h2c_tcrc,
  input                          s_axis_qdma_h2c_tlast,
  input                   [10:0] s_axis_qdma_h2c_tuser_qid,
  input                    [2:0] s_axis_qdma_h2c_tuser_port_id,
  input                          s_axis_qdma_h2c_tuser_err,
  input                   [31:0] s_axis_qdma_h2c_tuser_mdata,
  input                    [5:0] s_axis_qdma_h2c_tuser_mty,
  input                          s_axis_qdma_h2c_tuser_zero_byte,
  output                         s_axis_qdma_h2c_tready,

  output     [NUM_PHYS_FUNC-1:0] m_axis_h2c_tvalid,
  output [512*NUM_PHYS_FUNC-1:0] m_axis_h2c_tdata,
  output     [NUM_PHYS_FUNC-1:0] m_axis_h2c_tlast,
  output  [16*NUM_PHYS_FUNC-1:0] m_axis_h2c_tuser_size,
  output  [11*NUM_PHYS_FUNC-1:0] m_axis_h2c_tuser_qid,
  input      [NUM_PHYS_FUNC-1:0] m_axis_h2c_tready,

  output                         h2c_status_valid,
  output                  [15:0] h2c_status_bytes,
  output reg               [1:0] h2c_status_func_id,

  // Debug counters (axis_aclk domain; see EQDMA multi-queue loss,
  // stringy-fpga notebook 2026-07-25).  tuser_err is otherwise IGNORED by
  // this module (the header's "drop error packets" is an unimplemented
  // TODO): err-flagged packets pass through with corrupt payload and die in
  // the consumer's frame resync.  These counters make that visible.
  output reg              [31:0] h2c_pkt_count,
  output reg              [31:0] h2c_err_count,

  input                          axis_aclk,
  input                          axil_aresetn
);

  wire         axis_h2c_tvalid;
  wire [511:0] axis_h2c_tdata;
  wire         axis_h2c_tlast;
  wire  [15:0] axis_h2c_tuser_size;
  wire   [5:0] axis_h2c_tuser_mty;
  wire  [10:0] axis_h2c_tuser_qid;
  wire         axis_h2c_tready;

  wire         size_valid;
  wire  [15:0] size;

  // Count at the IP-facing handshake so packets are counted even when a
  // downstream stall means they never reach the consumer.  tuser_err can
  // assert on any beat; latch it across the packet.
  reg err_seen;
  always @(posedge axis_aclk) begin
    if (~axil_aresetn) begin
      h2c_pkt_count <= 0;
      h2c_err_count <= 0;
      err_seen      <= 1'b0;
    end
    else if (s_axis_qdma_h2c_tvalid && s_axis_qdma_h2c_tready) begin
      if (s_axis_qdma_h2c_tlast) begin
        h2c_pkt_count <= h2c_pkt_count + 1;
        if (err_seen || s_axis_qdma_h2c_tuser_err)
          h2c_err_count <= h2c_err_count + 1;
        err_seen <= 1'b0;
      end
      else if (s_axis_qdma_h2c_tuser_err) begin
        err_seen <= 1'b1;
      end
    end
  end

  axi_stream_register_slice #(
    .TDATA_W (512),
    .TUSER_W (33),
    .MODE    ("forward")
  ) slice_inst (
    .s_axis_tvalid (s_axis_qdma_h2c_tvalid),
    .s_axis_tdata  (s_axis_qdma_h2c_tdata),
    .s_axis_tkeep  ({64{1'b1}}),
    .s_axis_tlast  (s_axis_qdma_h2c_tlast),
    .s_axis_tuser  ({s_axis_qdma_h2c_tuser_mdata[15:0],
                     s_axis_qdma_h2c_tuser_mty,
                     s_axis_qdma_h2c_tuser_qid}),
    .s_axis_tid    (0),
    .s_axis_tdest  (0),
    .s_axis_tready (s_axis_qdma_h2c_tready),

    .m_axis_tvalid (axis_h2c_tvalid),
    .m_axis_tdata  (axis_h2c_tdata),
    .m_axis_tkeep  (),
    .m_axis_tlast  (axis_h2c_tlast),
    .m_axis_tuser  ({axis_h2c_tuser_size, axis_h2c_tuser_mty, axis_h2c_tuser_qid}),
    .m_axis_tid    (),
    .m_axis_tdest  (),
    .m_axis_tready (axis_h2c_tready),

    .aclk          (axis_aclk),
    .aresetn       (axil_aresetn)
  );

  axi_stream_size_counter #(
    .TDATA_W (512)
  ) size_cnt_inst (
    .p_axis_tvalid    (axis_h2c_tvalid),
    .p_axis_tkeep     ({64{1'b1}}),
    .p_axis_tlast     (axis_h2c_tlast),
    .p_axis_tuser_mty (axis_h2c_tuser_mty),
    .p_axis_tready    (axis_h2c_tready),

    .size_valid       (size_valid),
    .size             (size),

    .aclk             (axis_aclk),
    .aresetn          (axil_aresetn)
  );

  generate for (genvar i = 0; i < NUM_PHYS_FUNC; i++) begin
    assign m_axis_h2c_tvalid[i]                  = axis_h2c_tvalid;
    assign m_axis_h2c_tdata[`getvec(512, i)]     = axis_h2c_tdata;
    assign m_axis_h2c_tlast[i]                   = axis_h2c_tlast;
    assign m_axis_h2c_tuser_size[`getvec(16, i)] = axis_h2c_tuser_size;
    assign m_axis_h2c_tuser_qid[`getvec(11, i)]  = axis_h2c_tuser_qid;
  end
  endgenerate

  assign axis_h2c_tready = (| m_axis_h2c_tready);

  // H2C status update
  assign h2c_status_valid = size_valid;
  assign h2c_status_bytes = size;
  always @(*) begin
    h2c_status_func_id = 2'd0;
    for (int i = 0; i < NUM_PHYS_FUNC; i++) begin
      if (m_axis_h2c_tready[i]) begin
        h2c_status_func_id = i;
        break;
      end
    end
  end

endmodule: qdma_subsystem_h2c
