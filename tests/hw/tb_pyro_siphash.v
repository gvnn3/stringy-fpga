// Self-checking testbench for pyro_siphash — the 64 SipHash paper vectors.
//
// Key 000102030405060708090a0b0c0d0e0f, message n = bytes 00..n-1 for
// n = 0..63, expected tags from the reference implementation printout in
// the SipHash paper (vectors are printed there as byte arrays; the
// constants below are those arrays read little-endian into a u64, the
// same convention the core's dig output uses).  The identical table is
// generated and anchor-checked by the Python model, so RTL and model
// share one source of truth.
//
// Stimulus is driven on the falling edge: msg_ready sampled at a negedge
// can only change at the following posedge, so a byte presented under
// ready is accepted exactly once — the backpressure handshake is
// exercised for real on every 8th byte (2 unready compression cycles).
// Messages run back-to-back under one key_set, covering stream reuse.
// Prints PASS/FAIL per vector and a final summary.

`timescale 1ns/1ps
`default_nettype none

module tb_pyro_siphash;

    reg         clk;
    reg         rst_n;
    reg  [63:0] k0, k1;
    reg         key_set;
    reg         msg_start;
    reg         msg_valid;
    reg  [7:0]  msg_byte;
    reg         msg_end;
    wire        msg_ready;
    wire        dig_valid;
    wire [63:0] dig;

    pyro_siphash dut (
        .clk       (clk),
        .rst_n     (rst_n),
        .k0        (k0),
        .k1        (k1),
        .key_set   (key_set),
        .msg_start (msg_start),
        .msg_valid (msg_valid),
        .msg_byte  (msg_byte),
        .msg_end   (msg_end),
        .msg_ready (msg_ready),
        .dig_valid (dig_valid),
        .dig       (dig)
    );

    // 250 MHz, matching the repo's other benches.
    initial clk = 1'b0;
    always #2 clk = ~clk;

    reg [63:0] exp [0:63];
    integer    errors;
    integer    n;
    integer    i;
    integer    guard;

    // Run one vector: message = bytes 00..len-1, compare against exp.
    task run_vec;
        input integer len;
        begin
            @(negedge clk);
            msg_start = 1'b1;
            @(negedge clk);
            msg_start = 1'b0;

            i = 0;
            while (i < len) begin
                if (msg_ready) begin
                    msg_valid = 1'b1;
                    msg_byte  = i[7:0];
                    msg_end   = (i == len - 1);
                    i = i + 1;
                end else begin
                    msg_valid = 1'b0;
                    msg_end   = 1'b0;
                end
                @(negedge clk);
            end
            msg_valid = 1'b0;

            if (len == 0) begin
                // Empty message: bare msg_end once ready.
                while (!msg_ready) @(negedge clk);
                msg_end = 1'b1;
                @(negedge clk);
            end
            msg_end = 1'b0;

            guard = 0;
            while (!dig_valid && guard < 100) begin
                @(negedge clk);
                guard = guard + 1;
            end
            if (!dig_valid) begin
                $display("FAIL len=%0d: no dig_valid within %0d cycles",
                         len, guard);
                errors = errors + 1;
            end else if (dig !== exp[len]) begin
                $display("FAIL len=%0d: dig=%016h expected=%016h",
                         len, dig, exp[len]);
                errors = errors + 1;
            end else begin
                $display("PASS len=%0d dig=%016h", len, dig);
            end
        end
    endtask

    initial begin
        exp[ 0] = 64'h726fdb47dd0e0e31;
        exp[ 1] = 64'h74f839c593dc67fd;
        exp[ 2] = 64'h0d6c8009d9a94f5a;
        exp[ 3] = 64'h85676696d7fb7e2d;
        exp[ 4] = 64'hcf2794e0277187b7;
        exp[ 5] = 64'h18765564cd99a68d;
        exp[ 6] = 64'hcbc9466e58fee3ce;
        exp[ 7] = 64'hab0200f58b01d137;
        exp[ 8] = 64'h93f5f5799a932462;
        exp[ 9] = 64'h9e0082df0ba9e4b0;
        exp[10] = 64'h7a5dbbc594ddb9f3;
        exp[11] = 64'hf4b32f46226bada7;
        exp[12] = 64'h751e8fbc860ee5fb;
        exp[13] = 64'h14ea5627c0843d90;
        exp[14] = 64'hf723ca908e7af2ee;
        exp[15] = 64'ha129ca6149be45e5;
        exp[16] = 64'h3f2acc7f57c29bdb;
        exp[17] = 64'h699ae9f52cbe4794;
        exp[18] = 64'h4bc1b3f0968dd39c;
        exp[19] = 64'hbb6dc91da77961bd;
        exp[20] = 64'hbed65cf21aa2ee98;
        exp[21] = 64'hd0f2cbb02e3b67c7;
        exp[22] = 64'h93536795e3a33e88;
        exp[23] = 64'ha80c038ccd5ccec8;
        exp[24] = 64'hb8ad50c6f649af94;
        exp[25] = 64'hbce192de8a85b8ea;
        exp[26] = 64'h17d835b85bbb15f3;
        exp[27] = 64'h2f2e6163076bcfad;
        exp[28] = 64'hde4daaaca71dc9a5;
        exp[29] = 64'ha6a2506687956571;
        exp[30] = 64'had87a3535c49ef28;
        exp[31] = 64'h32d892fad841c342;
        exp[32] = 64'h7127512f72f27cce;
        exp[33] = 64'ha7f32346f95978e3;
        exp[34] = 64'h12e0b01abb051238;
        exp[35] = 64'h15e034d40fa197ae;
        exp[36] = 64'h314dffbe0815a3b4;
        exp[37] = 64'h027990f029623981;
        exp[38] = 64'hcadcd4e59ef40c4d;
        exp[39] = 64'h9abfd8766a33735c;
        exp[40] = 64'h0e3ea96b5304a7d0;
        exp[41] = 64'had0c42d6fc585992;
        exp[42] = 64'h187306c89bc215a9;
        exp[43] = 64'hd4a60abcf3792b95;
        exp[44] = 64'hf935451de4f21df2;
        exp[45] = 64'ha9538f0419755787;
        exp[46] = 64'hdb9acddff56ca510;
        exp[47] = 64'hd06c98cd5c0975eb;
        exp[48] = 64'he612a3cb9ecba951;
        exp[49] = 64'hc766e62cfcadaf96;
        exp[50] = 64'hee64435a9752fe72;
        exp[51] = 64'ha192d576b245165a;
        exp[52] = 64'h0a8787bf8ecb74b2;
        exp[53] = 64'h81b3e73d20b49b6f;
        exp[54] = 64'h7fa8220ba3b2ecea;
        exp[55] = 64'h245731c13ca42499;
        exp[56] = 64'hb78dbfaf3a8d83bd;
        exp[57] = 64'hea1ad565322a1a0b;
        exp[58] = 64'h60e61c23a3795013;
        exp[59] = 64'h6606d7e446282b93;
        exp[60] = 64'h6ca4ecb15c5f91e1;
        exp[61] = 64'h9f626da15c9625f3;
        exp[62] = 64'he51b38608ef25f57;
        exp[63] = 64'h958a324ceb064572;

        errors    = 0;
        rst_n     = 1'b0;
        key_set   = 1'b0;
        msg_start = 1'b0;
        msg_valid = 1'b0;
        msg_byte  = 8'd0;
        msg_end   = 1'b0;
        k0        = 64'h0706050403020100;   // key bytes 0-7, LE
        k1        = 64'h0f0e0d0c0b0a0908;   // key bytes 8-15, LE
        repeat (4) @(negedge clk);
        rst_n = 1'b1;
        repeat (2) @(negedge clk);

        key_set = 1'b1;
        @(negedge clk);
        key_set = 1'b0;
        @(negedge clk);

        for (n = 0; n < 64; n = n + 1)
            run_vec(n);

        if (errors == 0)
            $display("ALL 64 VECTORS PASS");
        else
            $display("%0d VECTOR(S) FAILED", errors);
        $finish;
    end

    // Watchdog: 64 vectors at worst ~100 cycles each is well under this.
    initial begin
        #200000;
        $display("FAIL: watchdog timeout");
        $finish;
    end

endmodule

`default_nettype wire
