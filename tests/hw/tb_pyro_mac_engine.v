// Self-checking testbench for pyro_mac_engine_top (multi-program MAC
// contract, P1 program).  Every expected digest below was computed by an
// independently written Python SipHash-2-4 + masking model (the same
// reference that asserts the SipHash paper test vector, key 000102..0f,
// before emitting a single constant), then hardcoded — the RTL is only
// trustworthy where it agrees with a model it has never seen.
//
// Tests:
//   (f) frame before any key commit        -> skip_nokey, NO result
//   key commit                             -> MACSTAT bit0 rises,
//                                             KEYCHECK == SipHash-2-4 of
//                                             "PYROMACKEYCHECK1"
//   (a) IPv4/TCP frame with known bytes    -> exact digest of the
//                                             hand-masked byte sequence
//   (b) same frame, TTL/DSCP/frag/checksums changed -> SAME digest
//   (c) one payload byte changed           -> DIFFERENT (exact) digest
//   (d) VLAN-tagged variant of (a)         -> same digest, longer len
//   (e) ARP frame                          -> skip_nonip, NO result
//   (g) IPv6/UDP exact digest; (g2) TC/flow/hop/cksum changed -> same
//   (h) IPv4 options (IHL=7)/UDP exact digest, flag bit5;
//       (h2) options/TTL/cksums changed    -> same digest
//   (i) IPv4/ICMP: checksum masked, NO L4 flag bit (MR11)
//   (j) IPv4 total-length claims 256, 60 L3 bytes delivered
//                                          -> record flag bit6
//   (k) RE-key: MACSTAT bit0 stays high across a commit, so completion
//       is bit0 && !bit1; keycheck must be the NEW key's, and the next
//       digest runs under the new key with the new key_id
//   zero-slack: SEEN == DIGESTED + SKIP_NONIP + SKIP_NOKEY, exactly.
//
// Compile with hw/rtl/pyro_mac_engine.v, hw/rtl/pyro_mac_engine_top.v
// and hw/rtl/pyro_siphash.v (byte-serial SipHash-2-4 core).
//
// The feed handshake here mirrors rp_wrapper's ST_FEED: in_ready is a
// CREDIT, so the master waits for ready and then pulses in_valid for
// exactly one cycle per byte.

`timescale 1ns/1ps
`default_nettype none

module tb_pyro_mac_engine;

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

    pyro_mac_engine_top dut (
        .clk(clk), .rst_n(rst_n),
        .csr_addr(csr_addr), .csr_wdata(csr_wdata),
        .csr_write(csr_write), .csr_rdata(csr_rdata),
        .in_valid(in_valid), .in_data(in_data), .in_last(in_last),
        .in_ready(in_ready),
        .res_wr(res_wr), .res_start(res_start), .res_end(res_end),
        .res_pattern_id(res_pattern_id), .res_flags(res_flags)
    );

    // Contract CSR addresses (pyro_mac_engine_top).
    localparam [15:0] A_KEY0        = 16'h0090;
    localparam [15:0] A_KEY1        = 16'h0094;
    localparam [15:0] A_KEY2        = 16'h0098;
    localparam [15:0] A_KEY3        = 16'h009C;
    localparam [15:0] A_KEYCTRL     = 16'h00A0;
    localparam [15:0] A_KEY_ID      = 16'h00A4;
    localparam [15:0] A_KEYCHECK_LO = 16'h00A8;
    localparam [15:0] A_KEYCHECK_HI = 16'h00AC;
    localparam [15:0] A_MACSTAT     = 16'h00B0;
    localparam [15:0] A_SEEN        = 16'h00B4;
    localparam [15:0] A_DIGESTED    = 16'h00B8;
    localparam [15:0] A_SKIP_NONIP  = 16'h00BC;
    localparam [15:0] A_SKIP_NOKEY  = 16'h00C0;

    // SipHash paper key 000102...0f, little-endian word order
    // (KEY0 = key bytes 0-3); second key 101112...1f for the re-key.
    localparam [31:0] KW0 = 32'h03020100, KW1 = 32'h07060504,
                      KW2 = 32'h0B0A0908, KW3 = 32'h0F0E0D0C;
    localparam [31:0] KW0B = 32'h13121110, KW1B = 32'h17161514,
                      KW2B = 32'h1B1A1918, KW3B = 32'h1F1E1D1C;
    localparam [31:0] KEY_ID  = 32'h11223344;
    localparam [31:0] KEY_ID2 = 32'h55667788;

    // Expected values from the Python reference model (macref.py).
    localparam [63:0] EXP_KEYCHECK  = 64'hB8296382E220BA8C;
    localparam [63:0] EXP_KEYCHECK2 = 64'hD010E9F2897C829E;
    localparam [63:0] DIG_A = 64'h1C5020F84F8238AA;   // also B and D
    localparam [63:0] DIG_C = 64'h31CA96F0AAF64A54;
    localparam [63:0] DIG_G = 64'h8B46D08F709FE755;   // also G2
    localparam [63:0] DIG_H = 64'h5DF2B98E3699469F;   // also H2
    localparam [63:0] DIG_I = 64'h9AEEC834FD7A7DF0;   // IPv4/ICMP
    localparam [63:0] DIG_J = 64'h289CD06E6DCF931D;   // trunc claim
    localparam [63:0] DIG_AB = 64'h524AEED609CB8794;  // A under key B
    localparam [6:0]  FLG_TCP4  = 7'h05;  // ipv4|tcp
    localparam [6:0]  FLG_UDP6  = 7'h0A;  // ipv6|udp
    localparam [6:0]  FLG_OPT4  = 7'h29;  // ipv4|udp|options-zeroed
    localparam [6:0]  FLG_ICMP4 = 7'h01;  // ipv4 only: ICMP, no L4 bit
    localparam [6:0]  FLG_TRC4  = 7'h45;  // ipv4|tcp|truncated

    // ---------------- stimulus frames (generated by macref.py) ----------
    reg [7:0]   fb [0:127];
    integer     flen;

    // frame A: 74 bytes
    localparam [591:0] FR_A = {
        64'h0200000000010200, 64'h0000000208004500, 64'h003C123440004006,
        64'hB1B20A0000010A00, 64'h00021F9000500000, 64'h0064000000C85018,
        64'h2000CAFE00005059, 64'h524F204D41432044, 64'h4947455354205445,
        16'h5354 };
    task load_a;
        integer i;
        begin
            flen = 74;
            for (i = 0; i < 74; i = i + 1)
                fb[i] = FR_A[8 * (73 - i) +: 8];
        end
    endtask
    // frame B: A with DSCP/ECN 0xB8, frag 0x2000, TTL 7, both checksums
    // changed — every mutable IPv4/TCP byte differs from A.
    localparam [591:0] FR_B = {
        64'h0200000000010200, 64'h00000002080045B8, 64'h003C123420000706,
        64'hDEAD0A0000010A00, 64'h00021F9000500000, 64'h0064000000C85018,
        64'h2000111100005059, 64'h524F204D41432044, 64'h4947455354205445,
        16'h5354 };
    task load_b;
        integer i;
        begin
            flen = 74;
            for (i = 0; i < 74; i = i + 1)
                fb[i] = FR_B[8 * (73 - i) +: 8];
        end
    endtask
    // frame C: A with the last payload byte 'T' -> 't'.
    localparam [591:0] FR_C = {
        64'h0200000000010200, 64'h0000000208004500, 64'h003C123440004006,
        64'hB1B20A0000010A00, 64'h00021F9000500000, 64'h0064000000C85018,
        64'h2000CAFE00005059, 64'h524F204D41432044, 64'h4947455354205445,
        16'h5374 };
    task load_c;
        integer i;
        begin
            flen = 74;
            for (i = 0; i < 74; i = i + 1)
                fb[i] = FR_C[8 * (73 - i) +: 8];
        end
    endtask
    // frame D: A with one 802.1Q tag (TPID 0x8100, TCI 0x0064): 78 bytes.
    localparam [623:0] FR_D = {
        64'h0200000000010200, 64'h0000000281000064, 64'h08004500003C1234,
        64'h40004006B1B20A00, 64'h00010A0000021F90, 64'h0050000000640000,
        64'h00C850182000CAFE, 64'h00005059524F204D, 64'h4143204449474553,
        48'h542054455354 };
    task load_d;
        integer i;
        begin
            flen = 78;
            for (i = 0; i < 78; i = i + 1)
                fb[i] = FR_D[8 * (77 - i) +: 8];
        end
    endtask
    // frame E: ARP request, padded to 60 bytes.
    localparam [479:0] FR_E = {
        64'h0200000000010200, 64'h0000000208060001, 64'h0800060400010200,
        64'h000000020A000002, 64'h0000000000000A00, 64'h0001000000000000,
        64'h0000000000000000, 32'h00000000 };
    task load_e;
        integer i;
        begin
            flen = 60;
            for (i = 0; i < 60; i = i + 1)
                fb[i] = FR_E[8 * (59 - i) +: 8];
        end
    endtask
    // frame G: IPv6/UDP, nonzero traffic class and flow label: 82 bytes.
    localparam [655:0] FR_G = {
        64'h0200000000010200, 64'h0000000286DD6ABC, 64'hDEF0001C1140FE80,
        64'h0000000000000000, 64'h000000000001FE80, 64'h0000000000000000,
        64'h0000000000021389, 64'h1F91001CBEEF4950, 64'h563620554450204D,
        64'h4143205041594C4F, 16'h4144 };
    task load_g;
        integer i;
        begin
            flen = 82;
            for (i = 0; i < 82; i = i + 1)
                fb[i] = FR_G[8 * (81 - i) +: 8];
        end
    endtask
    // frame G2: G with TC/flow zeroed, hop limit 1, UDP checksum 0.
    localparam [655:0] FR_G2 = {
        64'h0200000000010200, 64'h0000000286DD6000, 64'h0000001C1101FE80,
        64'h0000000000000000, 64'h000000000001FE80, 64'h0000000000000000,
        64'h0000000000021389, 64'h1F91001C00004950, 64'h563620554450204D,
        64'h4143205041594C4F, 16'h4144 };
    task load_g2;
        integer i;
        begin
            flen = 82;
            for (i = 0; i < 82; i = i + 1)
                fb[i] = FR_G2[8 * (81 - i) +: 8];
        end
    endtask
    // frame H: IPv4 IHL=7 (8 options bytes)/UDP: 70 bytes.
    localparam [559:0] FR_H = {
        64'h0200000000010200, 64'h0000000208004700, 64'h0038123440004011,
        64'hB1B20A0000010A00, 64'h0002072704000000, 64'h000013891F91001C,
        64'hBEEF495056362055, 64'h4450204D41432050, 48'h41594C4F4144 };
    task load_h;
        integer i;
        begin
            flen = 70;
            for (i = 0; i < 70; i = i + 1)
                fb[i] = FR_H[8 * (69 - i) +: 8];
        end
    endtask
    // frame H2: H with different options bytes, TTL 9, checksums changed.
    localparam [559:0] FR_H2 = {
        64'h0200000000010200, 64'h0000000208004700, 64'h0038123440000911,
        64'hB1B20A0000010A00, 64'h0002830704000A00, 64'h00FE13891F91001C,
        64'h2222495056362055, 64'h4450204D41432050, 48'h41594C4F4144 };
    task load_h2;
        integer i;
        begin
            flen = 70;
            for (i = 0; i < 70; i = i + 1)
                fb[i] = FR_H2[8 * (69 - i) +: 8];
        end
    endtask
    // frame I: IPv4/ICMP echo request, padded to 60 bytes.
    localparam [479:0] FR_I = {
        64'h0200000000010200, 64'h0000000208004500, 64'h002A432140004001,
        64'hBEEF0A0000010A00, 64'h00020800F00D1234, 64'h00015059524F2049,
        64'h434D502050494E47, 32'h00000000 };
    task load_i;
        integer i;
        begin
            flen = 60;
            for (i = 0; i < 60; i = i + 1)
                fb[i] = FR_I[8 * (59 - i) +: 8];
        end
    endtask
    // frame J: A with the IPv4 total length rewritten to 0x0100 (claims
    // 256 L3 bytes, the frame delivers 60) -> record flag bit6.
    localparam [591:0] FR_J = {
        64'h0200000000010200, 64'h0000000208004500, 64'h0100123440004006,
        64'hB1B20A0000010A00, 64'h00021F9000500000, 64'h0064000000C85018,
        64'h2000CAFE00005059, 64'h524F204D41432044, 64'h4947455354205445,
        16'h5354 };
    task load_j;
        integer i;
        begin
            flen = 74;
            for (i = 0; i < 74; i = i + 1)
                fb[i] = FR_J[8 * (73 - i) +: 8];
        end
    endtask

    // ---------------- result capture ------------------------------------
    integer got = 0, errors = 0;
    reg [63:0] r_dig;
    reg [63:0] r_len;
    reg [31:0] r_flags, r_kid;

    always @(posedge clk) begin
        if (res_wr) begin
            r_dig   = res_start;
            r_len   = res_end;
            r_flags = res_pattern_id;
            r_kid   = res_flags;
            got = got + 1;
        end
    end

    // ---------------- driver tasks --------------------------------------
    task csr_w(input [15:0] a, input [31:0] d);
        begin
            @(posedge clk); csr_addr <= a; csr_wdata <= d; csr_write <= 1;
            @(posedge clk); csr_write <= 0;
        end
    endtask

    task csr_r(input [15:0] a, output [31:0] d);
        begin
            @(posedge clk); csr_addr <= a;
            @(posedge clk); @(posedge clk);
            d = csr_rdata;
        end
    endtask

    // Credit handshake: wait for ready, then pulse valid one cycle.
    task send_frame;
        integer i;
        begin
            for (i = 0; i < flen; i = i + 1) begin
                @(posedge clk);
                while (!in_ready) @(posedge clk);
                in_valid <= 1; in_data <= fb[i];
                in_last  <= (i == flen - 1);
                @(posedge clk);
                in_valid <= 0; in_last <= 0;
            end
        end
    endtask

    // Expected key_id of the next results (advanced by the re-key).
    reg [31:0] exp_kid;

    // Expect exactly one new result with the given fields.
    task chk_res(input [63:0] xd, input [15:0] xl, input [6:0] xf);
        integer t, prev;
        begin
            prev = got; t = 0;
            while (got == prev && t < 2000) begin
                @(posedge clk); t = t + 1;
            end
            if (got != prev + 1) begin
                $display("FAIL: expected 1 result, got %0d", got - prev);
                errors = errors + 1;
            end else begin
                if (r_dig !== xd) begin
                    $display("FAIL: digest %016x != expected %016x",
                             r_dig, xd);
                    errors = errors + 1;
                end
                if (r_len !== {48'd0, xl}) begin
                    $display("FAIL: pkt_len %0d != expected %0d",
                             r_len, xl);
                    errors = errors + 1;
                end
                if (r_flags !== {25'd0, xf}) begin
                    $display("FAIL: flags %02x != expected %02x",
                             r_flags, xf);
                    errors = errors + 1;
                end
                if (r_kid !== exp_kid) begin
                    $display("FAIL: key_id %08x != expected %08x",
                             r_kid, exp_kid);
                    errors = errors + 1;
                end
            end
        end
    endtask

    // Expect NO result for a skipped frame.
    task chk_none;
        integer prev;
        begin
            prev = got;
            repeat (200) @(posedge clk);
            if (got != prev) begin
                $display("FAIL: skipped frame produced a result");
                errors = errors + 1;
            end
        end
    endtask

    task chk_ctr(input [15:0] a, input [31:0] exp,
                 input [8*10-1:0] name);
        reg [31:0] v;
        begin
            csr_r(a, v);
            if (v !== exp) begin
                $display("FAIL: %0s = %0d, expected %0d", name, v, exp);
                errors = errors + 1;
            end
        end
    endtask

    // ---------------- test sequence -------------------------------------
    integer t;
    reg [31:0] v, vlo, vhi;

    initial begin
        repeat (8) @(posedge clk);
        rst_n = 1;
        repeat (4) @(posedge clk);

        // Fail-closed after reset: no key, keycheck reads zero.
        csr_r(A_MACSTAT, v);
        if (v !== 32'd0) begin
            $display("FAIL: MACSTAT=%08x after reset, expected 0", v);
            errors = errors + 1;
        end

        // (f) frame before any commit -> skip_nokey, no result.
        $display("test f: frame with no committed key");
        load_a; send_frame; chk_none;
        chk_ctr(A_SEEN, 32'd1, "SEEN");
        chk_ctr(A_SKIP_NOKEY, 32'd1, "SKIP_NOKEY");
        chk_ctr(A_DIGESTED, 32'd0, "DIGESTED");

        // Commit the SipHash paper key; wait for MACSTAT.key_valid.
        $display("key commit + keycheck");
        csr_w(A_KEY0, KW0); csr_w(A_KEY1, KW1);
        csr_w(A_KEY2, KW2); csr_w(A_KEY3, KW3);
        csr_w(A_KEY_ID, KEY_ID);
        csr_w(A_KEYCTRL, 32'h1);
        v = 0; t = 0;
        while (!v[0] && t < 100) begin
            csr_r(A_MACSTAT, v); t = t + 1;
        end
        if (!v[0]) begin
            $display("FAIL: key_valid never rose after commit");
            errors = errors + 1;
        end
        csr_r(A_KEYCHECK_LO, vlo);
        csr_r(A_KEYCHECK_HI, vhi);
        if ({vhi, vlo} !== EXP_KEYCHECK) begin
            $display("FAIL: keycheck %08x%08x != expected %016x",
                     vhi, vlo, EXP_KEYCHECK);
            errors = errors + 1;
        end else
            $display("  keycheck %08x%08x OK", vhi, vlo);
        exp_kid = KEY_ID;

        // (a) exact digest of the hand-masked IPv4/TCP frame.
        $display("test a: IPv4/TCP exact digest");
        load_a; send_frame; chk_res(DIG_A, 16'd74, FLG_TCP4);

        // (b) mutable bytes changed -> same digest.
        $display("test b: TTL/DSCP/frag/checksums changed, same digest");
        load_b; send_frame; chk_res(DIG_A, 16'd74, FLG_TCP4);

        // (c) covered payload byte changed -> different digest.
        $display("test c: payload byte changed, different digest");
        load_c; send_frame; chk_res(DIG_C, 16'd74, FLG_TCP4);
        if (DIG_C === DIG_A) begin
            $display("FAIL: model constants collide");
            errors = errors + 1;
        end

        // (d) VLAN tag is L2, excluded -> same digest as (a).
        $display("test d: VLAN-tagged variant, same digest");
        load_d; send_frame; chk_res(DIG_A, 16'd78, FLG_TCP4);

        // (e) ARP -> skip_nonip, no result.
        $display("test e: ARP frame skipped");
        load_e; send_frame; chk_none;
        chk_ctr(A_SKIP_NONIP, 32'd1, "SKIP_NONIP");

        // (g) IPv6/UDP exact digest; (g2) mutable-v6 invariance.
        $display("test g: IPv6/UDP exact digest");
        load_g; send_frame; chk_res(DIG_G, 16'd82, FLG_UDP6);
        $display("test g2: TC/flow/hop/checksum changed, same digest");
        load_g2; send_frame; chk_res(DIG_G, 16'd82, FLG_UDP6);

        // (h) IPv4 options zeroed (flag bit5); (h2) invariance.
        $display("test h: IPv4 options/UDP exact digest");
        load_h; send_frame; chk_res(DIG_H, 16'd70, FLG_OPT4);
        $display("test h2: options/TTL/checksums changed, same digest");
        load_h2; send_frame; chk_res(DIG_H, 16'd70, FLG_OPT4);

        // (i) ICMP: checksum masked but NO L4 flag bit (MR11 reserves
        //     bit4 for protocols covered whole).
        $display("test i: IPv4/ICMP digest, no L4 flag");
        load_i; send_frame; chk_res(DIG_I, 16'd60, FLG_ICMP4);

        // (j) total length claims 256, the frame delivers 60 L3 bytes
        //     -> record flag bit6, digest covers what arrived (MR12).
        $display("test j: length-claim truncation flag");
        load_j; send_frame; chk_res(DIG_J, 16'd74, FLG_TRC4);

        // (k) RE-key.  MACSTAT bit0 (key_valid) stays HIGH across a
        //     re-key, so commit completion is bit0 && !bit1 — polling
        //     bit0 alone reads the PREVIOUS key's keycheck (the
        //     wrapper bug this test pins down).
        $display("test k: re-key keycheck + digest under the new key");
        csr_w(A_KEY0, KW0B); csr_w(A_KEY1, KW1B);
        csr_w(A_KEY2, KW2B); csr_w(A_KEY3, KW3B);
        csr_w(A_KEY_ID, KEY_ID2);
        csr_w(A_KEYCTRL, 32'h1);
        v = 32'h2; t = 0;
        while (!(v[0] && !v[1]) && t < 100) begin
            csr_r(A_MACSTAT, v); t = t + 1;
        end
        if (!(v[0] && !v[1])) begin
            $display("FAIL: re-key commit never completed");
            errors = errors + 1;
        end
        csr_r(A_KEYCHECK_LO, vlo);
        csr_r(A_KEYCHECK_HI, vhi);
        if ({vhi, vlo} !== EXP_KEYCHECK2) begin
            $display("FAIL: re-key keycheck %08x%08x != expected %016x",
                     vhi, vlo, EXP_KEYCHECK2);
            errors = errors + 1;
        end else
            $display("  re-key keycheck %08x%08x OK", vhi, vlo);
        exp_kid = KEY_ID2;
        load_a; send_frame; chk_res(DIG_AB, 16'd74, FLG_TCP4);

        // Zero-slack invariant, exact counts: 13 frames total.
        $display("counter check");
        chk_ctr(A_SEEN, 32'd13, "SEEN");
        chk_ctr(A_DIGESTED, 32'd11, "DIGESTED");
        chk_ctr(A_SKIP_NONIP, 32'd1, "SKIP_NONIP");
        chk_ctr(A_SKIP_NOKEY, 32'd1, "SKIP_NOKEY");

        if (errors == 0)
            $display("TB_RESULT: PASS digests=%0d", got);
        else
            $display("TB_RESULT: FAIL errors=%0d", errors);
        $finish;
    end

    // Watchdog: a wedged handshake must fail loudly, not hang the sim.
    initial begin
        #2000000;
        $display("TB_RESULT: FAIL watchdog timeout");
        $finish;
    end

endmodule
`default_nettype wire
