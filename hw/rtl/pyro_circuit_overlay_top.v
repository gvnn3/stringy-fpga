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
    // FULL-CORPUS sizing.  The 39,647-state table needs 40 URAM288 for the
    // 256 b/state bitmap and 10 more for out_idx, leaving base/fail/dense/
    // out_flat in BRAM at 108 of 160 BRAM36 -- measured, not estimated.
    //
    // This was 4096 for one release, from when the bitmap was still headed
    // for BRAM and ~530 tiles looked unavoidable.  That number outlived its
    // reason: once the bitmap moved to URAM the constraint went away, but
    // the override stayed and quietly capped every in-context build at a
    // tenth of the corpus.  A parameter that encodes a constraint should die
    // with the constraint.
    //
    // PYRO_OVERLAY_STATES exists only so simulation can instantiate a small
    // engine -- xsim walks these arrays and full size makes a table-load
    // differential needlessly slow.  It is NOT a synthesis knob: builds take
    // the default, which is the size that ships.
`ifndef PYRO_OVERLAY_STATES
  `define PYRO_OVERLAY_STATES 40960
`endif
    pyro_overlay_engine #(
        .MAX_STATES(`PYRO_OVERLAY_STATES),
        .MAX_DENSE(`PYRO_OVERLAY_STATES),
        .MAX_OUT(16384),
        .IMAGE_BYTES(2621440), .ENGINE_ID(32'h0A5E0001)
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
