// Beat-player for pyro_rp WITH A WATCHDOG.
//
// tb_pyro_rp.v holds each beat with `while (!trdy) @(posedge clk);`.  If the
// wrapper's FSM parks in a state that never returns to ST_RX, that loop spins
// forever: the run burns CPU, writes nothing, and reports nothing.  A hang is
// then indistinguishable from a slow simulation, which cost a 25-minute
// timeout before this file existed.
//
// So: bound the wait, and when it expires say WHERE the FSM stopped rather
// than just that it stopped.
`timescale 1ns/1ps
module tb_pyro_rp_wd;
  parameter integer NBEATS  = 0;
  parameter integer RUNCYC  = 20000;
  parameter integer MAXB    = 4096;
  parameter integer STALLCYC = 20000;   // per-beat patience

  reg clk = 0, rstn = 0;
  reg          tv = 0;
  reg  [511:0] td = 0;
  reg  [63:0]  tk = 0;
  reg          tl = 0;
  wire         trdy;
  wire         mv;
  wire [511:0] md;
  wire [63:0]  mk;
  wire         ml;
  wire [47:0]  mu;

  pyro_rp dut (
    .clk(clk), .rstn(rstn),
    .s_axis_tvalid(tv), .s_axis_tdata(td), .s_axis_tkeep(tk),
    .s_axis_tlast(tl), .s_axis_tuser(48'd0), .s_axis_tready(trdy),
    .m_axis_tvalid(mv), .m_axis_tdata(md), .m_axis_tkeep(mk),
    .m_axis_tlast(ml), .m_axis_tuser(mu), .m_axis_tready(1'b1));

  always #2 clk = ~clk;   // 250 MHz

  integer fo;
  always @(posedge clk) if (rstn && mv)
    $fdisplay(fo, "%h %h %b", md, mk, ml);

  reg [511:0] beats_d [0:MAXB-1];
  reg [63:0]  beats_k [0:MAXB-1];
  reg         beats_l [0:MAXB-1];

  integer i, c, w;
  initial begin
    fo = $fopen("out_beats.txt", "w");
    $readmemh("stim_d.memh", beats_d);
    $readmemh("stim_k.memh", beats_k);
    $readmemb("stim_l.memb", beats_l);
    repeat (5) @(negedge clk);
    rstn = 1;
    repeat (2) @(negedge clk);
    for (i = 0; i < NBEATS; i = i + 1) begin
      tv = 1; td = beats_d[i]; tk = beats_k[i]; tl = beats_l[i];
      @(posedge clk);
      w = 0;
      while (!trdy) begin
        @(posedge clk);
        w = w + 1;
        if (w > STALLCYC) begin
          $display("TB_HANG: beat %0d not accepted after %0d cycles; dut.state=%0d",
                   i, w, dut.state);
          $fclose(fo);
          $finish;
        end
      end
      #0;
    end
    @(posedge clk);
    tv = 0; tl = 0; tk = 0;
    for (c = 0; c < RUNCYC; c = c + 1) @(posedge clk);
    $display("TB_DONE: drove %0d beats", NBEATS);
    $fclose(fo);
    $finish;
  end
endmodule
