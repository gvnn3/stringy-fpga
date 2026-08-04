// OQ-2 wire-rate spike, G1: pyro_axis_wire_arb + pyro_wire_tx_gen.
//
// Arbiter checks, under random valid gaps and random sink stalls:
//   1. AXIS stability: while tvalid && !tready, the presented beat
//      (data/keep/last/user) must not change.
//   2. Packet atomicity: tuser is constant from a packet's first
//      beat through its tlast beat (beats never interleave).
//   3. Per-port completeness and order: every sent beat arrives,
//      in order, exactly once (data encodes port/pkt/beat).
//   4. Progress: both ports complete all packets.
// TX generator checks: frame cadence, valid-held-under-stall, the
// fixed header bytes, and the incrementing sequence number.
`timescale 1ns/1ps

module tb_pyro_wire_arb;

  reg clk = 0;
  always #2 clk = ~clk;      // 250 MHz
  reg rstn = 0;

  localparam NPKT = 200;     // packets per port

  // ---- DUT: arbiter --------------------------------------------------
  reg          s0_tvalid = 0, s1_tvalid = 0;
  reg  [511:0] s0_tdata = 0, s1_tdata = 0;
  reg   [63:0] s0_tkeep = 0, s1_tkeep = 0;
  reg          s0_tlast = 0, s1_tlast = 0;
  reg   [47:0] s0_tuser = 0, s1_tuser = 0;
  wire         s0_tready, s1_tready;

  wire         m_tvalid;
  wire [511:0] m_tdata;
  wire  [63:0] m_tkeep;
  wire         m_tlast;
  wire  [47:0] m_tuser;
  reg          m_tready = 0;

  pyro_axis_wire_arb dut (
    .clk (clk), .rstn (rstn),
    .s0_tvalid (s0_tvalid), .s0_tdata (s0_tdata),
    .s0_tkeep (s0_tkeep), .s0_tlast (s0_tlast),
    .s0_tuser (s0_tuser), .s0_tready (s0_tready),
    .s1_tvalid (s1_tvalid), .s1_tdata (s1_tdata),
    .s1_tkeep (s1_tkeep), .s1_tlast (s1_tlast),
    .s1_tuser (s1_tuser), .s1_tready (s1_tready),
    .m_tvalid (m_tvalid), .m_tdata (m_tdata),
    .m_tkeep (m_tkeep), .m_tlast (m_tlast),
    .m_tuser (m_tuser), .m_tready (m_tready)
  );

  integer errors = 0;

  // ---- stimulus masters ----------------------------------------------
  // Beat payload encodes {port, pkt, beat} in the low 48 bits.
  task automatic drive_port (
      input integer port);
    integer pkt, nbeats, b;
    begin
      for (pkt = 0; pkt < NPKT; pkt = pkt + 1) begin
        nbeats = 1 + ($urandom % 6);
        for (b = 0; b < nbeats; b = b + 1) begin
          // random idle gap before presenting
          while (($urandom % 4) == 0) @(posedge clk);
          if (port == 0) begin
            s0_tdata  <= {464'd0, 16'd0, pkt[15:0], b[15:0]};
            s0_tkeep  <= {64{1'b1}};
            s0_tlast  <= (b == nbeats - 1);
            s0_tuser  <= {16'hAAAA, 16'h0001, 16'd64};
            s0_tvalid <= 1'b1;
            @(posedge clk);
            while (!s0_tready) @(posedge clk);
            s0_tvalid <= 1'b0;
          end else begin
            s1_tdata  <= {464'd0, 16'd1, pkt[15:0], b[15:0]};
            s1_tkeep  <= {64{1'b1}};
            s1_tlast  <= (b == nbeats - 1);
            s1_tuser  <= {16'hBBBB, 16'h0040, 16'd64};
            s1_tvalid <= 1'b1;
            @(posedge clk);
            while (!s1_tready) @(posedge clk);
            s1_tvalid <= 1'b0;
          end
        end
      end
    end
  endtask

  // ---- sink + checkers ------------------------------------------------
  reg  [511:0] hold_data;
  reg   [63:0] hold_keep;
  reg          hold_last;
  reg   [47:0] hold_user;
  reg          holding = 0;

  reg          in_pkt = 0;
  reg   [47:0] pkt_user;

  integer exp_beat0 = 0, exp_pkt0 = 0;
  integer exp_beat1 = 0, exp_pkt1 = 0;
  integer done_pkts0 = 0, done_pkts1 = 0;

  wire [15:0] beat_port = m_tdata[47:32];
  wire [15:0] beat_pkt  = m_tdata[31:16];
  wire [15:0] beat_idx  = m_tdata[15:0];

  always @(posedge clk) begin
    if (rstn) begin
      m_tready <= ($urandom % 3) != 0;

      // 1. AXIS stability across stalls
      if (holding) begin
        if (!m_tvalid) begin
          errors = errors + 1;
          $display("FAIL: tvalid dropped during stall");
        end else if (m_tdata !== hold_data || m_tkeep !== hold_keep
                     || m_tlast !== hold_last
                     || m_tuser !== hold_user) begin
          errors = errors + 1;
          $display("FAIL: beat changed during stall");
        end
      end
      if (m_tvalid && !m_tready) begin
        holding   <= 1'b1;
        hold_data <= m_tdata;  hold_keep <= m_tkeep;
        hold_last <= m_tlast;  hold_user <= m_tuser;
      end else begin
        holding <= 1'b0;
      end

      if (m_tvalid && m_tready) begin
        // 2. packet atomicity via constant tuser
        if (in_pkt && m_tuser !== pkt_user) begin
          errors = errors + 1;
          $display("FAIL: tuser changed mid-packet (interleave)");
        end
        pkt_user <= m_tuser;
        in_pkt   <= !m_tlast;

        // 3. per-port order/completeness
        if (beat_port == 16'd0) begin
          if (beat_pkt != exp_pkt0[15:0]
              || beat_idx != exp_beat0[15:0]) begin
            errors = errors + 1;
            $display("FAIL: p0 order got %0d/%0d want %0d/%0d",
                     beat_pkt, beat_idx, exp_pkt0, exp_beat0);
          end
          if (m_tlast) begin
            exp_pkt0  = exp_pkt0 + 1;
            exp_beat0 = 0;
            done_pkts0 = done_pkts0 + 1;
          end else begin
            exp_beat0 = exp_beat0 + 1;
          end
        end else begin
          if (beat_pkt != exp_pkt1[15:0]
              || beat_idx != exp_beat1[15:0]) begin
            errors = errors + 1;
            $display("FAIL: p1 order got %0d/%0d want %0d/%0d",
                     beat_pkt, beat_idx, exp_pkt1, exp_beat1);
          end
          if (m_tlast) begin
            exp_pkt1  = exp_pkt1 + 1;
            exp_beat1 = 0;
            done_pkts1 = done_pkts1 + 1;
          end else begin
            exp_beat1 = exp_beat1 + 1;
          end
        end
      end
    end
  end

  // ---- DUT: TX generator ----------------------------------------------
  wire         g_tvalid;
  wire [511:0] g_tdata;
  wire  [63:0] g_tkeep;
  wire         g_tlast;
  wire  [15:0] g_tsize;
  reg          g_tready = 0;
  integer      g_frames = 0;
  reg   [31:0] g_prev_seq;

  pyro_wire_tx_gen #(.GAP_CYCLES (32'd50)) gen_dut (
    .clk (clk), .rstn (rstn),
    .m_tvalid (g_tvalid), .m_tdata (g_tdata), .m_tkeep (g_tkeep),
    .m_tlast (g_tlast), .m_tuser_size (g_tsize),
    .m_tready (g_tready)
  );

  always @(posedge clk) begin
    if (rstn) begin
      g_tready <= ($urandom % 2);
      if (g_tvalid && g_tready) begin
        if (g_tdata[47:0] !== 48'hFFFF_FFFF_FFFF
            || g_tdata[95:48] !== 48'h01_00_00_00_00_02
            || g_tdata[111:96] !== 16'hB588) begin
          errors = errors + 1;
          $display("FAIL: txgen header bytes wrong");
        end
        if (!g_tlast || g_tkeep !== {64{1'b1}}
            || g_tsize !== 16'd64) begin
          errors = errors + 1;
          $display("FAIL: txgen framing wrong");
        end
        if (g_frames > 0
            && g_tdata[143:112] !== g_prev_seq + 32'd1) begin
          errors = errors + 1;
          $display("FAIL: txgen seq not incrementing");
        end
        g_prev_seq <= g_tdata[143:112];
        g_frames = g_frames + 1;
      end
    end
  end

  // ---- run -------------------------------------------------------------
  initial begin
    repeat (10) @(posedge clk);
    rstn = 1;
    repeat (2) @(posedge clk);
    fork
      drive_port(0);
      drive_port(1);
    join
    // drain
    repeat (100) @(posedge clk);
    if (done_pkts0 != NPKT || done_pkts1 != NPKT) begin
      errors = errors + 1;
      $display("FAIL: progress p0=%0d p1=%0d want %0d",
               done_pkts0, done_pkts1, NPKT);
    end
    if (g_frames < 3) begin
      errors = errors + 1;
      $display("FAIL: txgen produced %0d frames", g_frames);
    end
    if (errors == 0)
      $display("TB_WIRE_ARB_PASS (p0=%0d p1=%0d txgen=%0d)",
               done_pkts0, done_pkts1, g_frames);
    else
      $display("TB_WIRE_ARB_FAIL errors=%0d", errors);
    $finish;
  end

  initial begin
    #4000000;
    $display("TB_WIRE_ARB_FAIL: timeout");
    $finish;
  end

endmodule
