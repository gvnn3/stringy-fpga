// Beat-player for pyro_rp with the watchdog AND a beat-exact latency
// probe (OQ-2 wire-rate work).
//
// Same player as tb_pyro_rp_wd.v -- bounded per-beat patience, named
// hang state -- plus a free-running cycle counter and $display probes
// timestamping the events a latency measurement needs:
//
//   LAT_IN <c>         last input beat of a frame ACCEPTED
//                      (tvalid && tready && tlast at posedge c)
//   LAT_OUT_FIRST <c>  m_axis_tvalid rise: first beat of a reply
//   LAT_OUT_LAST <c>   accepted m_axis tlast (m_tready is tied 1)
//   LAT_READY <c>      s_axis_tready rising back
//   LAT_UNREADY <c>    s_axis_tready falling (lets the driver tell
//                      "never went busy" from "no rise seen")
//
// All probes read `cyc` in the active region of the same posedge, and
// every sampled signal is NBA-driven by the DUT, so a read at an edge
// sees the value valid AT that edge and `cyc` reads pre-increment.
// The timestamps therefore share one timebase: a difference of two
// printed values is an exact cycle count at 250 MHz (4 ns/cycle).
//
// The driver writes ALL stimulus with non-blocking assignments.  A
// zero-time blocking write after `@(posedge clk)` races the DUT's own
// always block at that same edge: xsim is free to run the driver
// first, and in this snapshot it did -- a blocking `tv = 0` after the
// accept edge retracted tlast before the DUT ever sampled it, so no
// frame was ever seen and no reply ever came.  NBAs commit after
// every active-region read of the edge, closing the race for any
// process ordering.
//
// GAPCYC idles the input for that many cycles after each accepted
// tlast so one frame's whole transaction (scan + reply + ready) can
// finish before the next frame starts: the driver may then pair the
// events by simple ordering.
`timescale 1ns/1ps
module tb_pyro_rp_lat;
  parameter integer NBEATS   = 0;
  parameter integer RUNCYC   = 20000;
  parameter integer MAXB     = 4096;
  parameter integer STALLCYC = 20000;   // per-beat patience
  parameter integer GAPCYC   = 0;      // idle cycles after each tlast

  reg clk = 0, rstn = 0;
  reg          tv = 0;
  reg  [511:0] td = 0;
  reg  [63:0]  tk = 0;
  reg          tl = 0;
  reg  [47:0]  tu = 0;    // per-beat tuser (OQ-2: src 0x0040 = wire)
  wire         trdy;
  wire         mv;
  wire [511:0] md;
  wire [63:0]  mk;
  wire         ml;
  wire [47:0]  mu;

  pyro_rp dut (
    .clk(clk), .rstn(rstn),
    .s_axis_tvalid(tv), .s_axis_tdata(td), .s_axis_tkeep(tk),
    .s_axis_tlast(tl), .s_axis_tuser(tu), .s_axis_tready(trdy),
    .m_axis_tvalid(mv), .m_axis_tdata(md), .m_axis_tkeep(mk),
    .m_axis_tlast(ml), .m_axis_tuser(mu), .m_axis_tready(1'b1));

  always #2 clk = ~clk;   // 250 MHz

  integer fo;
  always @(posedge clk) if (rstn && mv)
    $fdisplay(fo, "%h %h %b", md, mk, ml);

  // Latency probes.  m_axis_tready is tied 1, so mv && ml is an
  // accepted output tlast.
  reg [63:0] cyc = 0;
  reg mv_q = 0, trdy_q = 0;
  always @(posedge clk) begin
    cyc    <= cyc + 1;
    mv_q   <= mv;
    trdy_q <= trdy;
    if (rstn) begin
      if (mv && !mv_q)     $display("LAT_OUT_FIRST %0d", cyc);
      if (mv && ml)        $display("LAT_OUT_LAST %0d", cyc);
      if (trdy && !trdy_q) $display("LAT_READY %0d", cyc);
      if (!trdy && trdy_q) $display("LAT_UNREADY %0d", cyc);
    end
  end

  reg [511:0] beats_d [0:MAXB-1];
  reg [63:0]  beats_k [0:MAXB-1];
  reg         beats_l [0:MAXB-1];
  reg [47:0]  beats_u [0:MAXB-1];

  integer i, c, w;
  initial begin
    fo = $fopen("out_beats.txt", "w");
    $readmemh("stim_d.memh", beats_d);
    $readmemh("stim_k.memh", beats_k);
    $readmemb("stim_l.memb", beats_l);
    $readmemh("stim_u.memh", beats_u);
    repeat (5) @(negedge clk);
    rstn = 1;
    repeat (2) @(negedge clk);
    for (i = 0; i < NBEATS; i = i + 1) begin
      tv <= 1; td <= beats_d[i]; tk <= beats_k[i]; tl <= beats_l[i];
      tu <= beats_u[i];
      @(posedge clk);
      w = 0;
      while (!trdy) begin
        @(posedge clk);
        w = w + 1;
        if (w > STALLCYC) begin
          $display("TB_HANG: beat %0d not accepted after %0d cycles;",
                   i, w);
          $display("TB_HANG: dut.state=%0d", dut.state);
          $fclose(fo);
          $finish;
        end
      end
      // The beat was accepted at the posedge just waited on; cyc
      // still holds that edge's pre-increment value here (NBA).
      if (beats_l[i]) begin
        $display("LAT_IN %0d", cyc);
        tv <= 0; tl <= 0; tk <= 0;
        repeat (GAPCYC) @(posedge clk);
      end
    end
    @(posedge clk);
    tv <= 0; tl <= 0; tk <= 0;
    for (c = 0; c < RUNCYC; c = c + 1) @(posedge clk);
    $display("TB_DONE: drove %0d beats", NBEATS);
    $fclose(fo);
    $finish;
  end
endmodule
