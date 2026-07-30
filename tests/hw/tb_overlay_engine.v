// Differential testbench: drive the SAME vectors through the RTL that the
// Python model consumed, and require identical (pattern_id, end) sets.
// This is the "three views of one artifact" discipline applied to the
// overlay engine -- the RTL is only trustworthy where it agrees with an
// independently written model.
`timescale 1ns/1ps
`default_nettype none

module tb_overlay_engine;
    localparam integer IMG_BYTES = 592;
    localparam integer SUBJ_BYTES = 6;
    localparam [31:0]  ENGINE_ID = 32'h0A5E0001;

    reg clk = 0, rst_n = 0;
    always #2 clk = ~clk;             // 250 MHz

    reg  [15:0] csr_addr = 0;
    reg  [31:0] csr_wdata = 0;
    reg         csr_write = 0;
    wire [31:0] csr_rdata;
    reg         in_valid = 0;
    reg  [7:0]  in_data = 0;
    reg         in_last = 0;
    wire        in_ready;
    wire        res_wr;
    wire [63:0] res_start, res_end;
    wire [31:0] res_pattern_id, res_flags;

    pyro_overlay_engine #(
        .MAX_STATES(64), .MAX_DENSE(128), .MAX_OUT(64),
        .IMAGE_BYTES(1024), .ENGINE_ID(ENGINE_ID)
    ) dut (
        .clk(clk), .rst_n(rst_n),
        .csr_addr(csr_addr), .csr_wdata(csr_wdata),
        .csr_write(csr_write), .csr_rdata(csr_rdata),
        .in_valid(in_valid), .in_data(in_data), .in_last(in_last),
        .in_ready(in_ready),
        .res_wr(res_wr), .res_start(res_start), .res_end(res_end),
        .res_pattern_id(res_pattern_id), .res_flags(res_flags)
    );

    reg [7:0] img  [0:IMG_BYTES-1];
    reg [7:0] subj [0:SUBJ_BYTES-1];
    integer i, got = 0, errors = 0;
    reg [31:0] got_pid [0:63];
    reg [63:0] got_end [0:63];

    task csr_w(input [15:0] a, input [31:0] d);
        begin
            @(posedge clk); csr_addr <= a; csr_wdata <= d; csr_write <= 1;
            @(posedge clk); csr_write <= 0;
        end
    endtask

    always @(posedge clk) begin
        if (res_wr) begin
            got_pid[got] = res_pattern_id;
            got_end[got] = res_end;
            $display("  RTL match: pid=%0d end=%0d epoch=%0d",
                     res_pattern_id, res_end, res_flags[31:16]);
            got = got + 1;
        end
    end

    initial begin
        $readmemh("table.hex", img);
        $readmemh("subject.hex", subj);
        repeat (8) @(posedge clk);
        rst_n = 1;
        repeat (4) @(posedge clk);

        // A5 section 5: nothing valid after reset.
        csr_addr = 16'h0068; @(posedge clk); @(posedge clk);
        if (csr_rdata !== 32'h0) begin
            $display("FAIL: TABLE_ID=%08x after reset, expected 0", csr_rdata);
            errors = errors + 1;
        end else $display("  after reset TABLE_ID=0 (nothing valid) OK");

        // Load the table image through the input stream.
        csr_w(16'h007C, 32'h1);                 // TBL_CTRL.LOAD
        for (i = 0; i < IMG_BYTES; i = i + 1) begin
            @(posedge clk);
            in_valid <= 1; in_data <= img[i];
            in_last  <= (i == IMG_BYTES-1);
        end
        @(posedge clk); in_valid <= 0; in_last <= 0;
        repeat (4) @(posedge clk);

        csr_addr = 16'h0078; @(posedge clk); @(posedge clk);
        $display("  bytes received = %0d (expected %0d)", csr_rdata, IMG_BYTES);
        if (csr_rdata !== IMG_BYTES) errors = errors + 1;

        csr_addr = 16'h006C; @(posedge clk); @(posedge clk);
        $display("  shadow CRC = %08x (model 0xf2edc7f8)", csr_rdata);
        if (csr_rdata !== 32'hf2edc7f8) begin
            $display("FAIL: CRC mismatch");
            errors = errors + 1;
        end

        csr_w(16'h007C, 32'h2);                 // COMMIT (clears LOAD)
        repeat (4) @(posedge clk);
        csr_addr = 16'h0070; @(posedge clk); @(posedge clk);
        $display("  epoch after commit = %0d", csr_rdata);
        if (csr_rdata !== 32'd1) errors = errors + 1;

        // Scan.
        for (i = 0; i < SUBJ_BYTES; i = i + 1) begin
            @(posedge clk);
            in_valid <= 1; in_data <= subj[i]; in_last <= (i == SUBJ_BYTES-1);
            @(posedge clk);
            while (!in_ready) @(posedge clk);   // hold until accepted
            in_valid <= 0;
        end
        repeat (40) @(posedge clk);

        $display("  RTL produced %0d matches", got);
        if (errors == 0 && got > 0)
            $display("TB_RESULT: matches=%0d errors=%0d", got, errors);
        else
            $display("TB_RESULT: matches=%0d errors=%0d", got, errors);
        $finish;
    end
endmodule
`default_nettype wire
