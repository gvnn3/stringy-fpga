// Thin alias so the overlay engine drops into the existing PR flow.
//
// `rp_wrapper` instantiates a module literally named `pyro_circuit`
// (pyro/hdl/rp_wrapper.py: ENGINE_MODULE).  The overlay engine already has
// that exact port list by construction, so no adaptation logic is needed —
// only the name.  Keeping the alias separate leaves the engine reusable
// outside the PR flow and makes the substitution auditable in one place.
`timescale 1ns/1ps
`default_nettype none

module pyro_circuit (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] csr_addr,
    input  wire [31:0] csr_wdata,
    input  wire        csr_write,
    output wire [31:0] csr_rdata,
    input  wire        in_valid,
    input  wire [7:0]  in_data,
    input  wire        in_last,
    output wire        in_ready,
    output wire        res_wr,
    output wire [63:0] res_start,
    output wire [63:0] res_end,
    output wire [31:0] res_pattern_id,
    output wire [31:0] res_flags
);
    // Sized to the SF2 RP envelope: 4096 states costs 53 BRAM36 of the 160
    // available.  The FULL 39,647-state corpus table would need ~530 BRAM
    // tiles, well over budget, so at full scale the 32 B/state bitmap must
    // move to URAM (64 URAM = 2.25 MB) with the narrow arrays left in BRAM.
    // That is a separate change; this size answers the in-context TIMING
    // question first.
    pyro_overlay_engine #(
        .MAX_STATES(4096), .MAX_DENSE(8192), .MAX_OUT(4096),
        .IMAGE_BYTES(262144), .ENGINE_ID(32'h0A5E0001)
    ) u_engine (
        .clk(clk), .rst_n(rst_n),
        .csr_addr(csr_addr), .csr_wdata(csr_wdata),
        .csr_write(csr_write), .csr_rdata(csr_rdata),
        .in_valid(in_valid), .in_data(in_data), .in_last(in_last),
        .in_ready(in_ready),
        .res_wr(res_wr), .res_start(res_start), .res_end(res_end),
        .res_pattern_id(res_pattern_id), .res_flags(res_flags)
    );
endmodule

`default_nettype wire
