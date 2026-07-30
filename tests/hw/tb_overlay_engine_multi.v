// WIDE differential: real corpus anchors, five subjects, 85 expected
// matches, all compared against pyro/overlay/model.py.
//
// The single-vector testbench (tb_overlay_engine.v) was thin enough that a
// deliberate handshake sabotage went uncaught.  This one drives matches at
// the head and tail of the buffer, dense overlaps, random binary, and a
// zero-match subject, and fails on ANY set difference in either direction.
`timescale 1ns/1ps
`default_nettype none

module tb_overlay_engine_multi;
    localparam integer IMG_BYTES  = 2564;
    localparam integer SUBJ_BYTES = 390;
    localparam integer N_SUBJ     = 5;
    localparam integer N_EXP      = 85;
    localparam [31:0]  ENGINE_ID  = 32'h0A5E0001;
    localparam [31:0]  EXP_CRC    = 32'h9c8f7ce9;

    reg clk = 0, rst_n = 0;
    always #2 clk = ~clk;

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
        .MAX_STATES(256), .MAX_DENSE(512), .MAX_OUT(256),
        .IMAGE_BYTES(4096), .ENGINE_ID(ENGINE_ID)
    ) dut (
        .clk(clk), .rst_n(rst_n),
        .csr_addr(csr_addr), .csr_wdata(csr_wdata),
        .csr_write(csr_write), .csr_rdata(csr_rdata),
        .in_valid(in_valid), .in_data(in_data), .in_last(in_last),
        .in_ready(in_ready),
        .res_wr(res_wr), .res_start(res_start), .res_end(res_end),
        .res_pattern_id(res_pattern_id), .res_flags(res_flags)
    );

    reg [7:0]  img  [0:IMG_BYTES-1];
    reg [7:0]  subj [0:SUBJ_BYTES-1];
    integer    exp_pid [0:N_EXP-1];
    integer    exp_end [0:N_EXP-1];
    integer    idx_off [0:N_SUBJ-1];
    integer    idx_len [0:N_SUBJ-1];
    integer    idx_cnt [0:N_SUBJ-1];

    integer got_pid [0:255];
    integer got_end [0:255];
    integer got = 0, errors = 0, i, j, k, s, base_exp, fd, rc;
    reg found;

    always @(posedge clk) if (res_wr) begin
        got_pid[got] = res_pattern_id;
        got_end[got] = res_end;
        got = got + 1;
    end

    task csr_w(input [15:0] a, input [31:0] d);
        begin
            @(posedge clk); csr_addr <= a; csr_wdata <= d; csr_write <= 1;
            @(posedge clk); csr_write <= 0;
        end
    endtask

    initial begin
        $readmemh("table.hex", img);
        $readmemh("subjects.hex", subj);
        fd = $fopen("expected.txt", "r");
        for (i = 0; i < N_EXP; i = i + 1)
            rc = $fscanf(fd, "%d %d\n", exp_pid[i], exp_end[i]);
        $fclose(fd);
        fd = $fopen("index.txt", "r");
        for (i = 0; i < N_SUBJ; i = i + 1)
            rc = $fscanf(fd, "%d %d %d\n", idx_off[i], idx_len[i], idx_cnt[i]);
        $fclose(fd);

        repeat (8) @(posedge clk); rst_n = 1; repeat (4) @(posedge clk);

        // Load once; every subject scans against the same committed table.
        csr_w(16'h007C, 32'h1);
        for (i = 0; i < IMG_BYTES; i = i + 1) begin
            @(posedge clk); in_valid <= 1; in_data <= img[i];
        end
        @(posedge clk); in_valid <= 0;
        repeat (4) @(posedge clk);
        csr_addr = 16'h006C; @(posedge clk); @(posedge clk);
        if (csr_rdata !== EXP_CRC) begin
            $display("FAIL: CRC %08x != model %08x", csr_rdata, EXP_CRC);
            errors = errors + 1;
        end else $display("  CRC %08x matches the model", csr_rdata);
        // A5 §5: declare the expected CRC before committing.  Without it
        // the engine now refuses (expect_crc resets to 0), which is the
        // point -- a commit that cannot be checked must not happen.
        csr_w(16'h0084, EXP_CRC);
        csr_w(16'h007C, 32'h2);
        repeat (4) @(posedge clk);

        base_exp = 0;
        for (s = 0; s < N_SUBJ; s = s + 1) begin
            got = 0;
            for (i = 0; i < idx_len[s]; i = i + 1) begin
                // in_ready is a CREDIT, not "taking it now" (see the
                // handshake note in pyro_overlay_engine.v): every cycle
                // in_valid is high with room available enqueues a byte.  So
                // wait for credit FIRST, then pulse in_valid for exactly one
                // cycle.  The old hold-until-ready form enqueued the same
                // byte two or three times over, which corrupted the stream.
                while (!in_ready) @(posedge clk);
                in_valid <= 1; in_data <= subj[idx_off[s] + i];
                in_last  <= (i == idx_len[s] - 1);   // end of THIS request
                @(posedge clk);
                in_valid <= 0; in_last <= 0;
                @(posedge clk);
            end
            repeat (60) @(posedge clk);

            if (got !== idx_cnt[s]) begin
                $display("FAIL subj[%0d]: RTL %0d matches, model %0d",
                         s, got, idx_cnt[s]);
                errors = errors + 1;
            end
            for (i = 0; i < idx_cnt[s]; i = i + 1) begin
                found = 0;
                for (j = 0; j < got; j = j + 1)
                    if (got_pid[j] === exp_pid[base_exp+i] &&
                        got_end[j] === exp_end[base_exp+i]) found = 1;
                if (!found) begin
                    $display("FAIL subj[%0d]: model (pid=%0d,end=%0d) MISSING",
                             s, exp_pid[base_exp+i], exp_end[base_exp+i]);
                    errors = errors + 1;
                end
            end
            $display("  subj[%0d] len=%0d: RTL %0d / model %0d %s",
                     s, idx_len[s], got, idx_cnt[s],
                     (got === idx_cnt[s]) ? "OK" : "<-- MISMATCH");
            base_exp = base_exp + idx_cnt[s];
        end

        if (errors == 0) $display("TB_RESULT: PASS (%0d subjects, %0d matches)",
                                  N_SUBJ, N_EXP);
        else             $display("TB_RESULT: FAIL errors=%0d", errors);
        $finish;
    end
endmodule
`default_nettype wire
