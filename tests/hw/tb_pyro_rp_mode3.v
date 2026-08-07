// Self-checking testbench for SCHED mode 3 (broadcast) on the
// multi-program pyro_rp top (PYRO MAC contract, spec v0.2.0 MR4/MR4a/
// MR5/MR20).  The DUT is the REAL emission of
// pyro.hdl.rp_wrapper.generate_rp_child(mac_program=True) elaborated
// with the REAL hw/rtl pyro_mac_engine_top/pyro_mac_engine/
// pyro_siphash P1 program and a REAL generated single-pattern P0
// engine (pyro.hdl.generator.generate) — no stubs anywhere.
//
// Proves, in one run:
//   (a) SCHED_SET 3 -> SCHED_ACK mode 3, quantum 1, status 0; modes 7
//       and 0xFF (the telemetry refusal probe) and quantum 0 are all
//       REFUSED (status 1) with the live schedule unchanged;
//   (b) mode 3: ONE IPv4/TCP wire frame (key committed) -> P0
//       wire_seen +1 AND P1 seen +1 AND a MAC_REPORT record whose
//       digest equals pyro.macwire.digest_packet of the frame under
//       the committed key, EXACTLY (wire_seq 0, pkt_len 74, flags
//       ipv4|tcp, report key_id = the committed key_id);
//   (c) six MIXED wire frames (IPv4 x3, IPv6 x1, ARP x1, 150-byte
//       non-IP multi-beat x1) in mode 3 -> P0.wire_seen and P1.seen
//       both +6 (equal counters, NOT a partition), and both
//       zero-slack invariants hold with absolute exactness:
//       P0 wire_seen == wire_scanned + wire_drops, and P1
//       seen == digested + skip_nonip + skip_nokey;
//   (d) back to mode 2 quantum 1: four wire frames -> an exact 2/2
//       partition (P0 +2, P1 +2);
//   (e) a SCHED_SET whose application lands MID-FRAME (mode 0 -> 3
//       applied by P1 while a 3-beat wire frame is in flight through
//       the dispatcher) neither tears the frame nor dual-targets it:
//       the in-flight frame stays wholly with its locked target
//       (P0 +1, P1 +0), and the NEXT wire frame broadcasts (+1/+1).
//       A whitebox monitor additionally asserts, on EVERY packet all
//       run long, that the dispatch target set never changes between
//       a packet's first beat and its tlast, that a dual-target beat
//       only ever occurs under the broadcast latch, and that no beat
//       fires targetless.
//
// EXP_DIG_A / EXP_KEYCHECK defaults were computed by
// pyro.macwire.digest_packet / key_check (the independent Python
// SipHash-2-4 + masking model) for wire frame A under the SipHash
// paper key 000102..0f; the sim driver recomputes both live and
// overrides via -generic_top, so the check is against the model, not
// against a stale constant.
//
// Compile (xvlog -sv) with, in order: the generated P0 engine
// (pyro_circuit), the generated wrapper (pyro_rp_core +
// pyro_rp_mac_prog + pyro_rp), hw/rtl/pyro_mac_engine_top.v,
// hw/rtl/pyro_mac_engine.v, hw/rtl/pyro_siphash.v, then this file.
// Driven end-to-end by the mode-3 sim driver (xvlog/xelab/xsim,
// pinned Vivado 2025.2 per pyro.synth.toolchain).

`timescale 1ns/1ps
`default_nettype none

module tb_pyro_rp_mode3 #(
    parameter [63:0] EXP_DIG_A    = 64'h1C5020F84F8238AA,
    parameter [63:0] EXP_KEYCHECK = 64'hB8296382E220BA8C
);

    reg clk = 0, rstn = 0;
    always #2 clk = ~clk;             // 250 MHz

    // tuser = {dst[15:0], src[15:0], size[15:0]}; src bit 6 (0x0040)
    // marks a raw wire frame (CMAC-0 via the 250 MHz adapter).
    localparam [15:0] SRC_HOST = 16'h0001;
    localparam [15:0] SRC_WIRE = 16'h0040;

    localparam [31:0] KEY_ID = 32'h11223344;

    reg          s_tvalid = 0;
    reg  [511:0] s_tdata = 0;
    reg   [63:0] s_tkeep = 0;
    reg          s_tlast = 0;
    reg   [47:0] s_tuser = 0;
    wire         s_axis_tready;
    wire         m_axis_tvalid;
    wire [511:0] m_axis_tdata;
    wire  [63:0] m_axis_tkeep;
    wire         m_axis_tlast;
    wire  [47:0] m_axis_tuser;

    pyro_rp dut (
        .clk           (clk),
        .rstn          (rstn),
        .s_axis_tvalid (s_tvalid),
        .s_axis_tdata  (s_tdata),
        .s_axis_tkeep  (s_tkeep),
        .s_axis_tlast  (s_tlast),
        .s_axis_tuser  (s_tuser),
        .s_axis_tready (s_axis_tready),
        .m_axis_tvalid (m_axis_tvalid),
        .m_axis_tdata  (m_axis_tdata),
        .m_axis_tkeep  (m_axis_tkeep),
        .m_axis_tlast  (m_axis_tlast),
        .m_axis_tuser  (m_axis_tuser),
        .m_axis_tready (1'b1)
    );

    integer errors = 0;

    // ---------------- stimulus frame buffer -----------------------------
    reg [7:0]  fb [0:511];
    reg [15:0] flen;

    task clear_fb;
        integer i;
        begin
            for (i = 0; i < 512; i = i + 1) fb[i] = 8'h00;
        end
    endtask

    // frame A: 74-byte IPv4/TCP (tb_pyro_mac_engine's proven vector).
    localparam [591:0] FR_A = {
        64'h0200000000010200, 64'h0000000208004500, 64'h003C123440004006,
        64'hB1B20A0000010A00, 64'h00021F9000500000, 64'h0064000000C85018,
        64'h2000CAFE00005059, 64'h524F204D41432044, 64'h4947455354205445,
        16'h5354 };
    task load_a;
        integer i;
        begin
            clear_fb;
            flen = 74;
            for (i = 0; i < 74; i = i + 1)
                fb[i] = FR_A[8 * (73 - i) +: 8];
        end
    endtask
    // frame B: A with every mutable IPv4/TCP byte changed (digests
    // the same; here it is just one more digested IPv4 frame).
    localparam [591:0] FR_B = {
        64'h0200000000010200, 64'h00000002080045B8, 64'h003C123420000706,
        64'hDEAD0A0000010A00, 64'h00021F9000500000, 64'h0064000000C85018,
        64'h2000111100005059, 64'h524F204D41432044, 64'h4947455354205445,
        16'h5354 };
    task load_b;
        integer i;
        begin
            clear_fb;
            flen = 74;
            for (i = 0; i < 74; i = i + 1)
                fb[i] = FR_B[8 * (73 - i) +: 8];
        end
    endtask
    // frame C: A with the last payload byte changed (digested, IPv4).
    localparam [591:0] FR_C = {
        64'h0200000000010200, 64'h0000000208004500, 64'h003C123440004006,
        64'hB1B20A0000010A00, 64'h00021F9000500000, 64'h0064000000C85018,
        64'h2000CAFE00005059, 64'h524F204D41432044, 64'h4947455354205445,
        16'h5374 };
    task load_c;
        integer i;
        begin
            clear_fb;
            flen = 74;
            for (i = 0; i < 74; i = i + 1)
                fb[i] = FR_C[8 * (73 - i) +: 8];
        end
    endtask
    // frame E: ARP request, 60 bytes (P1 skip_nonip).
    localparam [479:0] FR_E = {
        64'h0200000000010200, 64'h0000000208060001, 64'h0800060400010200,
        64'h000000020A000002, 64'h0000000000000A00, 64'h0001000000000000,
        64'h0000000000000000, 32'h00000000 };
    task load_e;
        integer i;
        begin
            clear_fb;
            flen = 60;
            for (i = 0; i < 60; i = i + 1)
                fb[i] = FR_E[8 * (59 - i) +: 8];
        end
    endtask
    // frame G: IPv6/UDP, 82 bytes (digested, two beats).
    localparam [655:0] FR_G = {
        64'h0200000000010200, 64'h0000000286DD6ABC, 64'hDEF0001C1140FE80,
        64'h0000000000000000, 64'h000000000001FE80, 64'h0000000000000000,
        64'h0000000000021389, 64'h1F91001CBEEF4950, 64'h563620554450204D,
        64'h4143205041594C4F, 16'h4144 };
    task load_g;
        integer i;
        begin
            clear_fb;
            flen = 82;
            for (i = 0; i < 82; i = i + 1)
                fb[i] = FR_G[8 * (81 - i) +: 8];
        end
    endtask
    // frame L: 150-byte non-IP (EtherType 0x1234) — a MULTI-BEAT
    // broadcast frame that P1 counts skip_nonip.
    task load_l;
        integer i;
        begin
            clear_fb;
            fb[0]  = 8'h02; fb[5]  = 8'h02;
            fb[6]  = 8'h02; fb[11] = 8'h01;
            fb[12] = 8'h12; fb[13] = 8'h34;
            for (i = 14; i < 150; i = i + 1) fb[i] = i[7:0];
            flen = 150;
        end
    endtask
    // frame W3: frame A zero-padded to 160 bytes — THREE beats, used
    // by test (e) so a schedule change lands strictly mid-frame.
    task load_w3;
        begin
            load_a;
            flen = 160;               // pad bytes already zeroed
        end
    endtask

    // ---------------- host control frames --------------------------------
    integer hseq;
    task make_hdr(input [7:0] kind, input [15:0] plen);
        begin
            clear_fb;
            fb[0]  = 8'h02; fb[5]  = 8'h02;   // dst 02:00:00:00:00:02
            fb[6]  = 8'h02; fb[11] = 8'h01;   // src 02:00:00:00:00:01
            fb[12] = 8'h88; fb[13] = 8'hB5;   // EtherType 0x88B5
            fb[14] = 8'h50; fb[15] = 8'h01;   // magic 'P', version 1
            fb[16] = kind;  fb[17] = 8'h00;   // kind, flags
            fb[18] = 8'h00; fb[19] = 8'h01;   // slot 1 (R87)
            fb[20] = hseq[31:24]; fb[21] = hseq[23:16];
            fb[22] = hseq[15:8];  fb[23] = hseq[7:0];
            fb[24] = plen[15:8];  fb[25] = plen[7:0];
            hseq = hseq + 1;
            flen = ((28 + plen) < 16'd60) ? 16'd60 : (28 + plen);
        end
    endtask

    // ---------------- beat driver (back-to-back capable) -----------------
    task send_frame(input [15:0] src);
        integer b, i, nb;
        reg [511:0] d;
        reg [63:0]  kp;
        reg         ok;
        begin
            nb = (flen + 63) / 64;
            b  = 0;
            while (b < nb) begin
                d  = 512'b0;
                kp = 64'b0;
                for (i = 0; i < 64; i = i + 1)
                    if ((b * 64 + i) < flen) begin
                        d[8*i +: 8] = fb[b*64 + i];
                        kp[i]       = 1'b1;
                    end
                s_tvalid <= 1'b1;
                s_tdata  <= d;
                s_tkeep  <= kp;
                s_tlast  <= (b == nb - 1);
                s_tuser  <= {16'h0000, src, flen};
                @(negedge clk);
                ok = s_axis_tready;   // settled registered tready
                @(posedge clk);
                if (ok) b = b + 1;
            end
            s_tvalid <= 1'b0;
            s_tlast  <= 1'b0;
        end
    endtask

    task wait_cycles(input integer n);
        begin
            repeat (n) @(posedge clk);
        end
    endtask

    // ---------------- reply capture --------------------------------------
    // Reassembles every C2H frame.  MAC_REPORT (0x0E) frames are
    // UNSOLICITED (flush timer / key flush), so their records go to a
    // side accumulator; every other frame joins the reply queue in
    // arrival order (requests here are strictly serialized, so the
    // queue order is deterministic).
    reg [7:0]  cap [0:2047];
    integer    cap_off = 0;
    reg [7:0]  rq [0:31][0:63];
    integer    rq_n = 0, rq_rd = 0;
    integer    rec_n = 0, rep_n = 0;
    reg [31:0] rec_seq [0:255];
    reg [15:0] rec_len [0:255];
    reg [15:0] rec_flg [0:255];
    reg [63:0] rec_dig [0:255];
    reg [31:0] rec_kid [0:255];

    integer    ci, cj, cb;
    reg [15:0] rcount;
    reg [31:0] rkid;

    always @(posedge clk) begin
        if (!rstn) begin
            cap_off = 0;
        end else if (m_axis_tvalid) begin        // tready tied high
            for (ci = 0; ci < 64; ci = ci + 1)
                if (m_axis_tkeep[ci]) begin
                    cap[cap_off] = m_axis_tdata[8*ci +: 8];
                    cap_off = cap_off + 1;
                end
            if (m_axis_tlast) begin
                if ((cap[12] == 8'h88) && (cap[13] == 8'hB5) &&
                    (cap[14] == 8'h50) && (cap[15] == 8'h01) &&
                    (cap[16] == 8'h0E)) begin
                    rcount = {cap[28], cap[29]};
                    rkid   = {cap[32], cap[33], cap[34], cap[35]};
                    for (cj = 0; cj < rcount; cj = cj + 1) begin
                        cb = 36 + 16 * cj;       // "<IHHQ" records
                        rec_seq[rec_n] = {cap[cb+3], cap[cb+2],
                                          cap[cb+1], cap[cb+0]};
                        rec_len[rec_n] = {cap[cb+5], cap[cb+4]};
                        rec_flg[rec_n] = {cap[cb+7], cap[cb+6]};
                        rec_dig[rec_n] = {cap[cb+15], cap[cb+14],
                                          cap[cb+13], cap[cb+12],
                                          cap[cb+11], cap[cb+10],
                                          cap[cb+9],  cap[cb+8]};
                        rec_kid[rec_n] = rkid;
                        rec_n = rec_n + 1;
                    end
                    rep_n = rep_n + 1;
                end else begin
                    for (ci = 0; ci < 64; ci = ci + 1)
                        rq[rq_n % 32][ci] = cap[ci];
                    rq_n = rq_n + 1;
                end
                cap_off = 0;
            end
        end
    end

    // ---------------- dispatch-atomicity monitor (whitebox) --------------
    // Runs the WHOLE simulation: per packet, the dispatch target set
    // (p0, p1) latched on the first beat must hold to tlast; a dual
    // target is legal only under the broadcast latch; a targetless
    // dispatched beat is never legal.  This is the direct statement
    // of "a SCHED_SET landing mid-frame neither tears nor
    // dual-targets the in-flight frame".
    reg  mon_inpkt = 0, mon_p0 = 0, mon_p1 = 0;
    wire mon_fire = dut.si_v0 && dut.d_tready;
    always @(posedge clk) begin
        if (!rstn) begin
            mon_inpkt <= 1'b0;
        end else if (mon_fire) begin
            if (!mon_inpkt) begin
                mon_p0 <= dut.p0_s_tvalid;
                mon_p1 <= dut.p1_s_tvalid;
                if (dut.p0_s_tvalid && dut.p1_s_tvalid &&
                    !dut.rx_bc) begin
                    $display("FAIL: dual target without bcast latch");
                    errors = errors + 1;
                end
                if (!dut.p0_s_tvalid && !dut.p1_s_tvalid) begin
                    $display("FAIL: dispatched beat with no target");
                    errors = errors + 1;
                end
            end else begin
                if ((dut.p0_s_tvalid !== mon_p0) ||
                    (dut.p1_s_tvalid !== mon_p1)) begin
                    $display("FAIL: target changed mid-frame (tear)");
                    errors = errors + 1;
                end
            end
            mon_inpkt <= !dut.si_l0;
        end
    end

    // Mid-frame-switch witness for test (e): fires ONLY when a
    // non-broadcast frame is still in flight (rx_inpkt, broadcast
    // latch clear) while the live schedule already reads mode 3 —
    // i.e. the SCHED_SET application really landed inside the frame
    // and the packet-atomic latch is what held the target.  No other
    // phase can set it: single-beat host frames never set rx_inpkt,
    // and every multi-beat wire frame sent while mode 3 is live has
    // the broadcast latch set.
    reg saw_mid_switch = 0;
    always @(posedge clk) begin
        if (rstn && dut.rx_inpkt && (dut.sched_mode == 2'd3) &&
            !dut.rx_bcast_q)
            saw_mid_switch <= 1'b1;
    end

    // ---------------- reply pop + field checks ---------------------------
    reg [7:0] rp [0:63];

    function [31:0] be32(input integer off);
        begin
            be32 = {rp[off], rp[off+1], rp[off+2], rp[off+3]};
        end
    endfunction

    task wait_reply(input [7:0] kind);
        integer t, i;
        begin
            t = 0;
            while ((rq_rd == rq_n) && (t < 300000)) begin
                @(posedge clk);
                t = t + 1;
            end
            if (rq_rd == rq_n) begin
                $display("FAIL: timeout waiting for reply kind %02x",
                         kind);
                errors = errors + 1;
            end else begin
                for (i = 0; i < 64; i = i + 1)
                    rp[i] = rq[rq_rd % 32][i];
                rq_rd = rq_rd + 1;
                if (rp[16] !== kind) begin
                    $display("FAIL: reply kind %02x, expected %02x",
                             rp[16], kind);
                    errors = errors + 1;
                end
            end
        end
    endtask

    task wait_records(input integer n);
        integer t;
        begin
            t = 0;
            while ((rec_n < n) && (t < 300000)) begin
                @(posedge clk);
                t = t + 1;
            end
            if (rec_n < n) begin
                $display("FAIL: timeout waiting for %0d record(s)", n);
                errors = errors + 1;
            end
        end
    endtask

    task chk32(input [31:0] got, input [31:0] exp,
               input [8*24-1:0] name);
        begin
            if (got !== exp) begin
                $display("FAIL: %0s = %0d, expected %0d",
                         name, got, exp);
                errors = errors + 1;
            end
        end
    endtask

    task chk64(input [63:0] got, input [63:0] exp,
               input [8*24-1:0] name);
        begin
            if (got !== exp) begin
                $display("FAIL: %0s = %016x, expected %016x",
                         name, got, exp);
                errors = errors + 1;
            end
        end
    endtask

    // ---------------- request round-trips --------------------------------
    // SCHED_SET -> SCHED_ACK (mode/quantum echo the POST-op schedule).
    task do_sched(input [7:0] mode, input [15:0] quant,
                  input [7:0] xm, input [15:0] xq, input [31:0] xs);
        begin
            make_hdr(8'h11, 16'd4);
            fb[28] = mode;
            fb[29] = 8'h00;
            fb[30] = quant[15:8];
            fb[31] = quant[7:0];
            send_frame(SRC_HOST);
            wait_reply(8'h12);
            chk32({24'd0, rp[28]}, {24'd0, xm}, "sched_ack.mode");
            chk32({16'd0, rp[30], rp[31]}, {16'd0, xq},
                  "sched_ack.quantum");
            chk32(be32(32), xs, "sched_ack.status");
        end
    endtask

    // MAC_KEY_LOAD (SipHash paper key 000102..0f) -> MAC_KEY_ACK with
    // the model keycheck.  Re-issuing it also FLUSHES pending records
    // (MR15), which test (b) uses to get its MAC_REPORT promptly.
    task do_key;
        integer i;
        begin
            make_hdr(8'h0F, 16'd20);
            fb[28] = KEY_ID[31:24]; fb[29] = KEY_ID[23:16];
            fb[30] = KEY_ID[15:8];  fb[31] = KEY_ID[7:0];
            for (i = 0; i < 16; i = i + 1)
                fb[32 + i] = i[7:0];
            send_frame(SRC_HOST);
            wait_reply(8'h10);
            chk32(be32(28), KEY_ID, "key_ack.key_id");
            chk64({rp[32], rp[33], rp[34], rp[35],
                   rp[36], rp[37], rp[38], rp[39]},
                  EXP_KEYCHECK, "key_ack.keycheck");
        end
    endtask

    // PERF_REQUEST -> P0's PERF_REPLY wire counters (payload 16-27).
    task read_p0(output [31:0] wseen, output [31:0] wscan,
                 output [31:0] wdrop);
        begin
            make_hdr(8'h06, 16'd0);
            send_frame(SRC_HOST);
            wait_reply(8'h07);
            wseen = be32(44);
            wscan = be32(48);
            wdrop = be32(52);
        end
    endtask

    // MAC_STAT_REQ -> P1's MAC_STAT_REPLY engine counters.
    task read_p1(output [31:0] seen, output [31:0] dig,
                 output [31:0] nonip, output [31:0] nokey);
        begin
            make_hdr(8'h13, 16'd0);
            send_frame(SRC_HOST);
            wait_reply(8'h14);
            seen  = be32(28);
            dig   = be32(32);
            nonip = be32(36);
            nokey = be32(40);
        end
    endtask

    // ---------------- test sequence --------------------------------------
    reg [31:0] a0, a1, a2;          // P0 baseline: seen/scanned/drops
    reg [31:0] b0, b1, b2, b3;      // P1 baseline: seen/dig/nonip/nokey
    reg [31:0] x0, x1, x2;          // P0 current
    reg [31:0] y0, y1, y2, y3;      // P1 current

    initial begin
        hseq = 1;
        $display("EXP_DIG_A=%016x EXP_KEYCHECK=%016x",
                 EXP_DIG_A, EXP_KEYCHECK);
        repeat (10) @(posedge clk);
        rstn = 1;
        repeat (100) @(posedge clk);   // P0 boot table-CSR sweep

        // ---- (a) SCHED_SET validation ------------------------------
        $display("test a: SCHED_SET 3 accepted; 7/0xFF/q0 refused");
        do_sched(8'd3,  16'd1, 8'd3, 16'd1, 32'd0);
        do_sched(8'd7,  16'd1, 8'd3, 16'd1, 32'd1);
        do_sched(8'hFF, 16'd0, 8'd3, 16'd1, 32'd1);   // refusal probe
        do_sched(8'd2,  16'd0, 8'd3, 16'd1, 32'd1);   // quantum >= 1

        // ---- key commit --------------------------------------------
        $display("key commit + keycheck");
        do_key;

        read_p0(a0, a1, a2);
        read_p1(b0, b1, b2, b3);
        chk32(a0, 32'd0, "p0.wire_seen@start");
        chk32(b0, 32'd0, "p1.seen@start");

        // ---- (b) one IPv4 wire frame, broadcast --------------------
        $display("test b: one IPv4 wire frame -> both programs + "
                 , "exact MAC_REPORT digest");
        load_a;
        send_frame(SRC_WIRE);
        wait_cycles(2000);
        read_p0(x0, x1, x2);
        read_p1(y0, y1, y2, y3);
        chk32(x0, a0 + 32'd1, "b.p0.wire_seen");
        chk32(y0, b0 + 32'd1, "b.p1.seen");
        chk32(y1, b1 + 32'd1, "b.p1.digested");
        do_key;                        // MR15 flush -> prompt report
        wait_records(1);
        chk32(rec_seq[0], 32'd0,      "b.record.wire_seq");
        chk32({16'd0, rec_len[0]}, 32'd74, "b.record.pkt_len");
        chk32({16'd0, rec_flg[0]}, 32'h0005, "b.record.flags");
        chk64(rec_dig[0], EXP_DIG_A,  "b.record.digest");
        chk32(rec_kid[0], KEY_ID,     "b.report.key_id");

        // ---- (c) six mixed wire frames, broadcast ------------------
        $display("test c: six mixed wire frames, equal counters + "
                 , "zero slack");
        read_p0(a0, a1, a2);
        read_p1(b0, b1, b2, b3);
        load_a; send_frame(SRC_WIRE); wait_cycles(1500);
        load_g; send_frame(SRC_WIRE); wait_cycles(1500);
        load_e; send_frame(SRC_WIRE); wait_cycles(1500);
        load_c; send_frame(SRC_WIRE); wait_cycles(1500);
        load_l; send_frame(SRC_WIRE); wait_cycles(2000);
        load_b; send_frame(SRC_WIRE); wait_cycles(2500);
        read_p0(x0, x1, x2);
        read_p1(y0, y1, y2, y3);
        chk32(x0, a0 + 32'd6, "c.p0.wire_seen");
        chk32(y0, b0 + 32'd6, "c.p1.seen");
        chk32(y1, b1 + 32'd4, "c.p1.digested");
        chk32(y2, b2 + 32'd2, "c.p1.skip_nonip");
        chk32(y3, b3,         "c.p1.skip_nokey");
        chk32(x0, x1 + x2,    "c.p0.zero_slack");
        chk32(y0, y1 + y2 + y3, "c.p1.zero_slack");

        // ---- (d) mode 2 quantum 1: exact 2/2 partition -------------
        $display("test d: mode 2 quantum 1, four frames -> 2/2");
        do_sched(8'd2, 16'd1, 8'd2, 16'd1, 32'd0);
        read_p0(a0, a1, a2);
        read_p1(b0, b1, b2, b3);
        load_a; send_frame(SRC_WIRE); wait_cycles(1500);
        load_a; send_frame(SRC_WIRE); wait_cycles(1500);
        load_a; send_frame(SRC_WIRE); wait_cycles(1500);
        load_a; send_frame(SRC_WIRE); wait_cycles(2000);
        read_p0(x0, x1, x2);
        read_p1(y0, y1, y2, y3);
        chk32(x0, a0 + 32'd2, "d.p0.wire_seen");
        chk32(y0, b0 + 32'd2, "d.p1.seen");
        chk32(y1, b1 + 32'd2, "d.p1.digested");

        // ---- (e) SCHED_SET landing mid-frame -----------------------
        // Mode 0 (P0-only), then a single-beat SCHED_SET(3) followed
        // BACK-TO-BACK by a 3-beat wire frame: P1 applies the new
        // mode 2-3 cycles after taking the SCHED_SET, i.e. strictly
        // inside the wire frame's dispatch window, so the broadcast
        // switch lands mid-frame.  The frame must stay wholly with
        // its locked unicast target (P0), and only the NEXT wire
        // frame may broadcast.  The whitebox monitor above holds the
        // per-beat no-tear/no-dual invariant throughout.
        $display("test e: SCHED_SET mid-frame is packet-atomic");
        do_sched(8'd0, 16'd1, 8'd0, 16'd1, 32'd0);
        read_p0(a0, a1, a2);
        read_p1(b0, b1, b2, b3);
        make_hdr(8'h11, 16'd4);        // SCHED_SET mode 3 quantum 1
        fb[28] = 8'd3;
        fb[29] = 8'h00;
        fb[30] = 8'h00;
        fb[31] = 8'h01;
        send_frame(SRC_HOST);
        load_w3;
        send_frame(SRC_WIRE);          // back-to-back: no idle beat
        wait_reply(8'h12);
        chk32({24'd0, rp[28]}, 32'd3, "e.sched_ack.mode");
        chk32(be32(32), 32'd0,        "e.sched_ack.status");
        wait_cycles(2500);
        read_p0(x0, x1, x2);
        read_p1(y0, y1, y2, y3);
        chk32(x0, a0 + 32'd1, "e.p0.wire_seen (atomic unicast)");
        chk32(y0, b0,         "e.p1.seen (no dual target)");
        chk32({31'd0, saw_mid_switch}, 32'd1,
              "e.switch landed mid-frame");
        load_a;                        // next frame DOES broadcast
        send_frame(SRC_WIRE);
        wait_cycles(2000);
        read_p0(x0, x1, x2);
        read_p1(y0, y1, y2, y3);
        chk32(x0, a0 + 32'd2, "e.p0.wire_seen (post-switch)");
        chk32(y0, b0 + 32'd1, "e.p1.seen (post-switch)");
        chk32(y1, b1 + 32'd1, "e.p1.digested (post-switch)");
        chk32(x0, x1 + x2,      "e.p0.zero_slack");
        chk32(y0, y1 + y2 + y3, "e.p1.zero_slack");

        if (errors == 0)
            $display("TB_RESULT: PASS reports=%0d records=%0d",
                     rep_n, rec_n);
        else
            $display("TB_RESULT: FAIL errors=%0d", errors);
        $finish;
    end

    // Watchdog: a wedged handshake must fail loudly, not hang xsim.
    initial begin
        #40000000;
        $display("TB_RESULT: FAIL watchdog timeout");
        $finish;
    end

endmodule
`default_nettype wire
