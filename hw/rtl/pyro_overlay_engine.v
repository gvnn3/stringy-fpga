// PYRO overlay engine — Aho-Corasick matcher with a run-time writable table.
//
// Amendment A5 (approved 2026-07-30).  Presents the SAME interface as the
// generated `pyro_circuit`, so it drops into the existing `rp_wrapper` with
// no change to the R80 boundary, and takes its table through the SAME input
// stream a corpus arrives on, selected by a CSR mode bit.  No new ports, no
// new datapath, no static rebuild — the only reason this fits the shell.
//
// Why it exists: JTAG partial reconfiguration costs a measured 13.6 s, which
// the frontier study put outside every timescale below a minute.  Writing a
// 2.10 MB table over the existing path costs 0.85 ms.
//
// Identity (A5 §2), enforced in hardware:
//   CIRC_ID  — which engine (baked)
//   TABLE_ID — CRC-32C over the bytes actually received
//   EPOCH    — incremented on commit, emitted with every match
// After reset TABLE_ID is 0 and the engine matches NOTHING.  An overlay that
// came up holding stale content reading as valid would be the worst failure
// available, because it would look correct.
//
// ---------------------------------------------------------------------
// MEASURED STATUS (OOC synth + P&R, xcu250, 2026-07-30)
// ---------------------------------------------------------------------
//   BRAM inference : ALL SIX arrays -> Block RAM, zero LUTRAM fallback
//                    ("LUT as Memory" = 0).  53 BRAM tiles, 1.97% of the
//                    device.
//   Area           : 1,609 LUTs / 1,068 FFs -- about 1/6 the LUTs of a
//                    253-slot generated group circuit (~10,300), because
//                    the patterns now live in BRAM instead of fabric.
//   Functional     : agrees with pyro/overlay/model.py on identical
//                    vectors (same CRC, same match set, same epoch).
//   TIMING         : **MEETS 250 MHz.  WNS = +0.176 ns.**
//
//                    v1  -1.297 ns, 19 levels: bram -> a_dense_reg[12]/D
//                        (CARRY8 x4 popcount tree + the 256-bit mask/shift)
//                    v2  +0.046 ns, 12 levels: bram -> state_q_reg[0]/CE
//                        after splitting rank across S_RANK/S_RANK2 and
//                        dropping the 256-bit barrel shifter for eight
//                        fixed 32-bit popcounts + a lane-selected prefix sum
//                    v3  +0.176 ns, 10 levels: bram -> r_partial_reg[2]/D
//                        after registering hit/fail so the 256:1 bitmap mux
//                        no longer reaches the FSM or state_q's clock enable
//
//                    1.473 ns recovered in total, and LUTs went DOWN over
//                    the same span (1,609 -> 1,225): the barrel shifter was
//                    pure cost.  Registers rose only 1,068 -> 1,121.
//
//                    The remaining path is bram -> lane mux -> 32-bit mask
//                    -> popcount -> r_partial.  If more margin is ever
//                    wanted, register the lane select next; that is another
//                    FSM state, and 176 ps did not seem worth it.
//
//                    STILL OUT-OF-CONTEXT.  OF-1 measured RM<->static
//                    boundary paths costing 5-120 ps in-context on six of
//                    21 group builds; 176 ps now covers that envelope with
//                    room, but the PR link is the only real test and has
//                    not been run.
//
// ---------------------------------------------------------------------
// EVERY memory read is REGISTERED, and that shapes the whole datapath.
// ---------------------------------------------------------------------
// Block RAM has no asynchronous read port.  A first version read four of the
// six arrays combinationally and Vivado silently demoted them to LUTRAM
// (measured: bitmap/dense/fail/oidx fell back; only base/oflat inferred),
// which at MAX_STATES=4096 would be ruinous.  Two consequences, both
// deliberate:
//
//  1. The LOAD path accumulates a whole word in a shift register and writes
//     it once.  The obvious byte-at-a-time read-modify-write needs an async
//     read and costs the array its BRAM.
//  2. The SCAN path is a 10-state FSM with an explicit WAIT state per
//     dependent read (a registered read issued in cycle N is valid in N+2,
//     not N+1).  It processes a byte every ~7 cycles rather than
//     1 B/cycle, so this engine is ~7x slower per byte than the
//     generated group circuits.  That is the honest cost of holding every
//     anchor at once and swapping in 0.85 ms instead of 13.6 s; throughput
//     is recoverable later by overlapping independent bytes, which the
//     loop-carried state dependency makes real work rather than a tweak.

`timescale 1ns/1ps
`default_nettype none

module pyro_overlay_engine #(
    parameter integer MAX_STATES  = 40960,   // 10 URAM banks of 4096
    parameter integer MAX_DENSE   = 40960,
    parameter integer MAX_OUT     = 16384,
    parameter integer IMAGE_BYTES = 262144,
    parameter [31:0]  ENGINE_ID   = 32'h0A5E0001
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] csr_addr,
    input  wire [31:0] csr_wdata,
    input  wire        csr_write,
    output reg  [31:0] csr_rdata,
    input  wire        in_valid,
    input  wire [7:0]  in_data,
    input  wire        in_last,
    output wire        in_ready,
    output reg         res_wr,
    output reg  [63:0] res_start,
    output reg  [63:0] res_end,
    output reg  [31:0] res_pattern_id,
    output reg  [31:0] res_flags
);

    localparam [31:0] MAGIC       = 32'h5059524F;
    localparam [31:0] HARNESS_VER = 32'h00020300;

    // R45 harness contract.  This engine is natively a STREAMING scanner --
    // it matches whatever arrives on in_valid -- but rp_wrapper drives every
    // engine through RESET -> OUT_CAP -> START -> wait BUSY -> feed -> wait
    // DONE.  Sharing a port list is not the same as sharing a protocol: the
    // wrapper parked forever in ST_WAIT_BUSY because nothing here ever
    // asserted BUSY, so the table would load and then never match anything.
    // These three registers make the streaming engine answer the handshake.
    localparam [15:0] A_CTRL       = 16'h0010;   // bit0 START, bit1 RESET
    // A_STATUS bits: 0 BUSY, 1 DONE, 3 OVF
    localparam [15:0] A_STATUS     = 16'h0014;
    localparam [15:0] A_CIRC_ID0   = 16'h0018;
    localparam [15:0] A_OUT_CAP    = 16'h0048;
    localparam [15:0] A_OUT_COUNT  = 16'h004C;
    // R45a performance counters (added 2026-07-31).  The telemetry audit
    // found these unmapped: reads of 0x0058-0x0064 fell to the default
    // (HARNESS_VER), so a PERF_REQUEST against this child returned constant
    // garbage that looked plausible.  Same per-scan semantics as the
    // generated engines: the wrapper's CTRL.RESET before each scan (W4)
    // zeroes them, so they cover exactly the most recent scan.
    localparam [15:0] A_CYCLES_LO  = 16'h0058;
    localparam [15:0] A_CYCLES_HI  = 16'h005C;
    localparam [15:0] A_BYTES_LO   = 16'h0060;
    localparam [15:0] A_BYTES_HI   = 16'h0064;
    localparam [15:0] A_TBL_ACTIVE = 16'h0068;
    localparam [15:0] A_TBL_SHADOW = 16'h006C;
    localparam [15:0] A_TBL_EPOCH  = 16'h0070;
    localparam [15:0] A_TBL_STATUS = 16'h0074;
    localparam [15:0] A_TBL_BYTES  = 16'h0078;
    localparam [15:0] A_TBL_CTRL   = 16'h007C;
    localparam [15:0] A_TBL_CAPS   = 16'h0080;
    // The CRC the HOST declared at TABLE_BEGIN.  Without it the engine can
    // only report the CRC of whatever it happened to receive, which is
    // self-consistent and therefore useless as a check -- measured on
    // silicon: a transfer corrupted in flight committed cleanly and
    // DESTROYED the working table, which is precisely what A5 §5 forbids.
    localparam [15:0] A_TBL_EXPECT = 16'h0084;

    localparam integer B_LOAD   = 0;
    localparam integer B_COMMIT = 1;
    localparam integer B_ABORT  = 2;

    reg [31:0] status, out_count, tbl_ctrl;
    reg [31:0] out_cap;          // R45 OUT_CAP; 0 = uncapped
    reg [31:0] expect_crc;       // host's declared TABLE_ID (A5 §5)
    reg        commit_err;       // last commit was refused
    reg        busy, done_f, ovf_f;
    // CYCLES counts every cycle busy is high -- START to completion,
    // INCLUDING feed stalls, because that is the honest scan latency the
    // host experiences.  BYTES counts bytes consumed from the input queue.
    // At ~8 cycles/byte the ratio is the engine's signature; a wire read
    // that does not show it is reading the wrong thing.
    reg [63:0] perf_cycles, perf_bytes;
    reg [31:0] active_id, shadow_crc, epoch, bytes_rcvd;
    reg        active_valid;
    wire       load_mode = tbl_ctrl[B_LOAD];

    // ---------------- table memories (no reset, registered reads) -------
    // bitmap and out_idx live in URAM; the narrow arrays stay in BRAM.
    // Measured sizing for the full 39,647-state corpus against SF2's RP
    // budget (160 BRAM36 + 64 URAM):
    //     bitmap 256b x 39,647 = 1.21 MB -> 40 URAM  (276 BRAM36 if block!)
    //     oidx    64b x 39,647 = 0.30 MB -> 10 URAM  (69 BRAM36 if block)
    //     base/fail/dense/oflat            -> 114 BRAM36
    //   totals: 50 of 64 URAM, 114 of 160 BRAM36 -- both fit.
    // Moving ONLY the bitmap is not enough: the remainder still needs 183
    // BRAM36, over the 160 budget. oidx is the next largest and is 64 bits
    // wide, so it costs a single URAM column.
    // cascade_height caps how deep Vivado chains URAM primitives.  Left
    // unbounded it built a SEVEN-deep cascade whose ripple is COMBINATIONAL:
    // measured 14 logic levels of which URAM288 x7, 3.25 ns of logic, and
    // WNS back to -1.297 ns.  Capping the chain trades a little output
    // muxing for a far shorter path; the URAM COUNT is unchanged, since
    // that is set by capacity, not by how the primitives are wired.
    (* ram_style = "ultra", cascade_height = 2 *)
    reg [255:0] bitmap_mem [0:MAX_STATES-1];
    (* ram_style = "block" *) reg [31:0]  base_mem   [0:MAX_STATES-1];
    (* ram_style = "block" *) reg [31:0]  fail_mem   [0:MAX_STATES-1];
    (* ram_style = "block" *) reg [31:0]  dense_mem  [0:MAX_DENSE-1];
    (* ram_style = "ultra", cascade_height = 2 *)
    reg [63:0]  oidx_mem   [0:MAX_STATES-1];
    (* ram_style = "block" *) reg [31:0]  oflat_mem  [0:MAX_OUT-1];

    reg [31:0] hdr_n_states, hdr_n_patterns, hdr_engine_id, hdr_version;
    reg [31:0] off_bm, off_base, off_dense, off_fail, off_oidx, off_oflat;

    // ---------------- load path -----------------------------------------
    reg [31:0]  wr_addr;
    // The load-path accumulators have the same shape of problem as a_state:
    // one register feeding write-data pins across every bank of an array, so
    // the path is route-bound (91% route, 1 logic level).  Replicating them
    // is a physical optimization only.
    (* max_fanout = 16 *) reg [31:0]  word_sr;   // 4-byte accumulator
                                                 // (base/dense/fail/oflat)
    (* max_fanout = 16 *) reg [255:0] bm_sr;     // 32-byte accum (bitmap)
    (* max_fanout = 16 *) reg [63:0]  oidx_sr;   // 8-byte accum (out_idx)
    reg [1:0]   wr_bcnt;

    function [31:0] crc32c_byte;
        input [31:0] crc;
        input [7:0]  data;
        integer i;
        reg [31:0] c;
        begin
            c = crc ^ {24'h0, data};
            for (i = 0; i < 8; i = i + 1)
                c = c[0] ? ((c >> 1) ^ 32'h82F63B78) : (c >> 1);
            crc32c_byte = c;
        end
    endfunction

    wire accepting = load_mode && in_valid;
    wire [31:0]  nxt_word = {in_data, word_sr[31:8]};
    wire [255:0] nxt_bm   = {in_data, bm_sr[255:8]};
    wire [63:0]  nxt_oidx = {in_data, oidx_sr[63:8]};
    wire in_bm    = (wr_addr >= off_bm)    && (wr_addr < off_base);
    wire in_base  = (wr_addr >= off_base)  && (wr_addr < off_dense);
    wire in_dense = (wr_addr >= off_dense) && (wr_addr < off_fail);
    wire in_fail  = (wr_addr >= off_fail)  && (wr_addr < off_oidx);
    wire in_oidx  = (wr_addr >= off_oidx)  && (wr_addr < off_oflat);
    wire in_oflat = (wr_addr >= off_oflat) && (off_oflat != 0);
    wire word_last = (wr_bcnt == 2'd3);
    wire bm_last   = (((wr_addr - off_bm)   & 32'd31) == 32'd31);
    wire oidx_last = (((wr_addr - off_oidx) & 32'd7)  == 32'd7);

    // Whole-word writes only: no read-modify-write, so BRAM survives.
    always @(posedge clk) begin
        if (accepting) begin
            if (in_bm    && bm_last)
                bitmap_mem[(wr_addr - off_bm) >> 5] <= nxt_bm;
            if (in_base  && word_last)
                base_mem [(wr_addr - off_base)  >> 2] <= nxt_word;
            if (in_dense && word_last)
                dense_mem[(wr_addr - off_dense) >> 2] <= nxt_word;
            if (in_fail  && word_last)
                fail_mem [(wr_addr - off_fail)  >> 2] <= nxt_word;
            if (in_oidx  && oidx_last)
                oidx_mem [(wr_addr - off_oidx)  >> 3] <= nxt_oidx;
            if (in_oflat && word_last)
                oflat_mem[(wr_addr - off_oflat) >> 2] <= nxt_word;
        end
    end

    // ---------------- scan pipeline -------------------------------------
    // A registered read set up in cycle N is not valid until cycle N+2:
    // the address registers at the end of N, the BRAM reads during N+1, and
    // the data registers at the end of N+1.  Every dependent read therefore
    // needs its own WAIT state.  Getting this off by one produced ZERO
    // matches on the first run of the pipelined version -- caught only
    // because the testbench had been fixed to fail on a wrong match set.
    localparam [3:0] S_IDLE  = 4'd0,
                     S_FETCH = 4'd1,   // wait 1: bitmap (URAM) in flight
                     S_FETCH2= 4'd10,  // wait 2: URAM cascade output reg
                     S_RANK  = 4'd2,   // valid; latch per-lane popcounts
                     S_RANK2 = 4'd9,   // prefix-sum the rank; issue dense
                     S_DWAIT = 4'd3,   // wait: dense in flight
                     S_DENSE = 4'd4,   // valid; issue oidx read
                     S_OWAIT = 4'd5,   // wait 1: oidx (URAM) in flight
                     S_OWAIT2= 4'd11,  // wait 2: URAM cascade output reg
                     S_OIDX  = 4'd6,   // valid; maybe start emitting
                     S_FWAIT = 4'd7,   // wait: oflat in flight
                     S_EMIT  = 4'd8;   // valid; emit one result
    reg [3:0]  st;
    reg [31:0] state_q;
    reg [63:0] pos;
    reg [7:0]  cur_byte;
    // A MATCH_REQUEST is a FRESH buffer: offsets are relative to its first
    // byte and matching restarts at the root.  Without this the second
    // request's ends are biased by the first request's length -- the RTL
    // reports the right NUMBER of matches at the WRONG offsets, which a
    // single-subject testbench cannot see and the wide one caught at once.
    reg        req_done;

    // Registered BRAM ports: address regs in, data regs out.
    //
    // a_state addresses four arrays at once -- bitmap (40 URAM288), oidx (10
    // URAM288), base and fail (BRAM) -- so one register drives address pins
    // spread right across the region.  Measured, that route was 91% of a
    // 3.411 ns critical path with a single logic level: the delay is
    // distance, not computation.  max_fanout lets synthesis replicate the
    // register so each cluster is driven by a nearby copy.  This is a
    // physical optimization only; the replicas are functionally identical
    // and the netlist semantics do not change.
    (* max_fanout = 16 *) reg [31:0] a_state;
    reg [31:0]  a_dense, a_oidx, a_oflat;
    reg [255:0] d_bitmap, d_bitmap_q;
    reg [31:0]  d_base, d_fail, d_dense, d_oflat;
    reg [63:0]  d_oidx, d_oidx_q;

    always @(posedge clk) begin
        d_bitmap   <= bitmap_mem[a_state];
        d_bitmap_q <= d_bitmap;      // isolate the URAM cascade ripple
        d_base     <= base_mem  [a_state];
        d_fail     <= fail_mem  [a_state];
        d_dense    <= dense_mem [a_dense];
        d_oidx     <= oidx_mem  [a_oidx];
        d_oidx_q   <= d_oidx;        // ditto
        d_oflat    <= oflat_mem [a_oflat];
    end

    function [5:0] pc32;
        input [31:0] w;
        integer i;
        reg [5:0] n;
        begin
            n = 0;
            for (i = 0; i < 32; i = i + 1) n = n + w[i];
            pc32 = n;
        end
    endfunction

    // Rank = popcount of transition bits strictly below cur_byte.
    //
    // The obvious form -- build a 256-bit mask by variable-shifting, AND it
    // with the bitmap, then popcount 256 bits -- puts a 256-bit barrel
    // shifter AND a 256-input adder tree in one combinational path.  That
    // was the whole timing gap.
    //
    // Instead: eight FIXED 32-bit popcounts (no mask, no shifter), plus ONE
    // 32-bit partial for the lane the byte falls in, then a prefix sum
    // selected by the lane index.  The only variable shift left is 32 bits
    // wide.  Stage 1 registers the nine counts; stage 2 does the prefix sum
    // and the add to base.
    wire [2:0] sel_lane = cur_byte[7:5];
    wire [4:0] sub_bit  = cur_byte[4:0];
    wire [31:0] lane0 = d_bitmap_q[31:0];
    wire [31:0] lane1 = d_bitmap_q[63:32];
    wire [31:0] lane2 = d_bitmap_q[95:64];
    wire [31:0] lane3 = d_bitmap_q[127:96];
    wire [31:0] lane4 = d_bitmap_q[159:128];
    wire [31:0] lane5 = d_bitmap_q[191:160];
    wire [31:0] lane6 = d_bitmap_q[223:192];
    wire [31:0] lane7 = d_bitmap_q[255:224];
    wire [31:0] sel_lane_bits =
        (sel_lane == 3'd0) ? lane0 : (sel_lane == 3'd1) ? lane1 :
        (sel_lane == 3'd2) ? lane2 : (sel_lane == 3'd3) ? lane3 :
        (sel_lane == 3'd4) ? lane4 : (sel_lane == 3'd5) ? lane5 :
        (sel_lane == 3'd6) ? lane6 : lane7;
    wire [31:0] sub_mask = (sub_bit == 5'd0) ? 32'd0
                         : (32'hFFFFFFFF >> (6'd32 - {1'b0, sub_bit}));

    // Stage-1 registers (filled in S_RANK1, consumed in S_RANK2).
    reg [5:0] r_pc0, r_pc1, r_pc2, r_pc3, r_pc4, r_pc5, r_pc6, r_pc7;
    reg [5:0] r_partial;
    reg [2:0] r_lane;
    reg [31:0] r_base, r_fail;
    reg        r_hit;

    // Prefix sum over the registered lane counts: shallow, and every term
    // is only 6 bits wide.
    wire [8:0] pfx =
        ((r_lane > 3'd0) ? {3'd0, r_pc0} : 9'd0) +
        ((r_lane > 3'd1) ? {3'd0, r_pc1} : 9'd0) +
        ((r_lane > 3'd2) ? {3'd0, r_pc2} : 9'd0) +
        ((r_lane > 3'd3) ? {3'd0, r_pc3} : 9'd0) +
        ((r_lane > 3'd4) ? {3'd0, r_pc4} : 9'd0) +
        ((r_lane > 3'd5) ? {3'd0, r_pc5} : 9'd0) +
        ((r_lane > 3'd6) ? {3'd0, r_pc6} : 9'd0);
    wire [8:0] rank2 = pfx + {3'd0, r_partial};
    wire hit = d_bitmap_q[cur_byte];

    reg [31:0] emit_off, emit_left;
    reg [63:0] emit_end;

    // 1-deep skid buffer -- the second half of the rp_wrapper backpressure
    // contract (pyro.hdl.rp_wrapper._engine_backpressure, HAZARD (a)).
    //
    // The wrapper asserts eng_in_valid and advances feed_idx in the SAME
    // cycle, so it cannot see in_ready fall in time to hold the beat it just
    // committed.  An engine that simply ignores bytes while busy therefore
    // loses them, with nothing on the wire to show for it.  This scanner is
    // busy ~8 cycles per byte, so it dropped roughly every byte after the
    // first and reported zero matches on a subject full of them -- the table
    // loaded, committed, and attested correctly, and then matched nothing.
    //
    // So: accept whenever the skid is empty and park the byte until the FSM
    // is back at the root.  in_ready is "the skid has room", not "I am idle",
    // which is what makes the wrapper's early commit safe.
    // DEPTH TWO, and the second slot is the whole point.  With a 1-deep skid
    // and in_ready = "skid empty", the wrapper still loses a byte: it samples
    // in_ready in cycle N and commits the NEXT beat in the same cycle, so
    // when the skid fills at N+1 there is already a beat in flight with
    // nowhere to land.  Measured exactly that way -- the load, commit and
    // attestation were all correct and the scan returned zero matches.
    //
    // So in_ready deasserts at depth 1, leaving slot 1 reserved for the beat
    // the wrapper has already committed.  Two entries is sufficient and
    // necessary: the wrapper can never be more than one beat ahead of the
    // ready it observed.
    reg [7:0] sk0_d, sk1_d;
    reg       sk0_l, sk1_l;
    reg [1:0] sk_n;

    // NOTE the handshake this implies, because it is NOT plain AXI-Stream.
    // in_ready is a CREDIT ("there is room for one more"), not "I am taking
    // this beat now", and every cycle in_valid is high with room available is
    // a DISTINCT beat.  A master must therefore pulse in_valid once per byte
    // rather than hold it until ready -- holding it would enqueue the same
    // byte repeatedly.
    //
    // That is exactly how rp_wrapper drives it (its ST_FEED asserts
    // eng_in_valid for one cycle per byte and a per-cycle default clears it),
    // and it is what lets the wrapper commit a beat one cycle past the ready
    // it observed without losing it.  A conventional hold-until-ready master
    // needs adapting; tb_overlay_engine_multi was.
    wire push = in_valid && !load_mode && active_valid && (sk_n < 2'd2);
    wire pop  = (st == S_IDLE) && (sk_n != 2'd0);
    // Bytes reach the FSM only through the queue, never straight off the
    // wire; one path in means one place for an off-by-one to hide.
    wire       have_byte = (sk_n != 2'd0);
    wire [7:0] byte_in   = sk0_d;
    wire       byte_last = sk0_l;

    assign in_ready = load_mode ? 1'b1 : (active_valid && (sk_n == 2'd0));

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            status <= 0; out_count <= 0; tbl_ctrl <= 0;
            out_cap <= 0; busy <= 1'b0; done_f <= 1'b0; ovf_f <= 1'b0;
            perf_cycles <= 64'd0; perf_bytes <= 64'd0;
            expect_crc <= 32'd0; commit_err <= 1'b0;
            sk_n <= 2'd0; sk0_d <= 8'd0; sk1_d <= 8'd0;
            sk0_l <= 1'b0; sk1_l <= 1'b0;
            active_id <= 0; shadow_crc <= 32'hFFFFFFFF; epoch <= 0;
            bytes_rcvd <= 0; active_valid <= 0;
            wr_addr <= 0; wr_bcnt <= 0; word_sr <= 0; bm_sr <= 0; oidx_sr <= 0;
            hdr_n_states <= 0; hdr_n_patterns <= 0;
            hdr_engine_id <= 0; hdr_version <= 0;
            off_bm <= 0; off_base <= 0; off_dense <= 0;
            off_fail <= 0; off_oidx <= 0; off_oflat <= 0;
            st <= S_IDLE; state_q <= 0; pos <= 0; cur_byte <= 0;
            req_done <= 1'b0;
            a_state <= 0; a_dense <= 0; a_oidx <= 0; a_oflat <= 0;
            emit_off <= 0; emit_left <= 0; emit_end <= 0;
            res_wr <= 0; res_start <= 0; res_end <= 0;
            res_pattern_id <= 0; res_flags <= 0;
            csr_rdata <= 0;
            // d_bitmap_q/d_oidx_q are deliberately NOT reset here.  They are
            // driven by the memory-read block above, and a second driver in
            // this block makes them multi-driven: synthesis keeps the
            // constant 0 and discards the real one, so the bitmap read path
            // goes dead and the URAMs are optimized away entirely.
            // Simulation does not catch it -- the reset branch only fires
            // during reset, so xsim sees one driver and passes.  They need no
            // reset in any case: nothing consumes them until the FSM has
            // walked S_FETCH -> S_RANK, which loads them first.
        end else begin
            res_wr <= 1'b0;
            if (busy) perf_cycles <= perf_cycles + 64'd1;

            // ---- CSR ----
            // R45 scan handshake (see A_CTRL).  RESET clears the pipeline so
            // scan N+1 cannot inherit state from scan N; START raises BUSY,
            // which is the edge the wrapper is waiting on before it feeds.
            if (csr_write && csr_addr == A_CTRL) begin
                if (csr_wdata[1]) begin              // RESET
                    sk_n      <= 2'd0;   // flush queued bytes with the pipeline
                    perf_cycles <= 64'd0;    // per-scan counters (W4/R45a)
                    perf_bytes  <= 64'd0;
                    busy      <= 1'b0;
                    done_f    <= 1'b0;
                    ovf_f     <= 1'b0;
                    out_count <= 32'd0;
                    st        <= S_IDLE;
                    state_q   <= 32'd0;
                    a_state   <= 32'd0;
                    pos       <= 64'd0;
                    req_done  <= 1'b0;
                end
                if (csr_wdata[0]) begin              // START
                    busy   <= 1'b1;
                    done_f <= 1'b0;
                end
            end
            if (csr_write && csr_addr == A_OUT_CAP)
                out_cap <= csr_wdata;
            if (csr_write && csr_addr == A_TBL_EXPECT)
                expect_crc <= csr_wdata;

            if (csr_write && csr_addr == A_TBL_CTRL) begin
                tbl_ctrl <= csr_wdata;
                if (csr_wdata[B_ABORT]) begin
                    wr_addr <= 0; wr_bcnt <= 0; bytes_rcvd <= 0;
                    shadow_crc <= 32'hFFFFFFFF;
                end
                if (csr_wdata[B_COMMIT]) begin
                    // The CRC gate is the whole of A5 §5.  Refusing must be
                    // total: active_id, epoch and active_valid are all left
                    // exactly as they were, so a bad load degrades to "no
                    // change" rather than to a silently wrong resident table.
                    if (bytes_rcvd != 0 && hdr_engine_id == ENGINE_ID
                        && hdr_n_states <= MAX_STATES
                        && (~shadow_crc) == expect_crc) begin
                        active_id    <= ~shadow_crc;
                        active_valid <= 1'b1;
                        epoch        <= epoch + 1;
                        state_q      <= 0;
                        a_state      <= 0;
                        pos          <= 0;
                        req_done     <= 1'b0;
                        st           <= S_IDLE;
                        commit_err   <= 1'b0;
                    end else begin
                        status     <= status | 32'h4;
                        commit_err <= 1'b1;
                    end
                end
            end
            case (csr_addr)
                A_CIRC_ID0:   csr_rdata <= MAGIC;
                // bit2 stays the commit-refused flag this engine already
                // raised; bits 0/1/3 are the R45 BUSY/DONE/OVF the wrapper
                // polls.  Composed on read so the two uses cannot drift.
                A_STATUS:     csr_rdata <= {28'd0, ovf_f, status[2],
                                            done_f, busy};
                A_OUT_COUNT:  csr_rdata <= out_count;
                A_OUT_CAP:    csr_rdata <= out_cap;
                A_CYCLES_LO:  csr_rdata <= perf_cycles[31:0];
                A_CYCLES_HI:  csr_rdata <= perf_cycles[63:32];
                A_BYTES_LO:   csr_rdata <= perf_bytes[31:0];
                A_BYTES_HI:   csr_rdata <= perf_bytes[63:32];
                A_TBL_ACTIVE: csr_rdata <= active_id;
                A_TBL_SHADOW: csr_rdata <= ~shadow_crc;
                A_TBL_EPOCH:  csr_rdata <= epoch;
                A_TBL_STATUS: csr_rdata <= {28'd0, commit_err, active_valid,
                                            load_mode, (bytes_rcvd != 0)};
                A_TBL_BYTES:  csr_rdata <= bytes_rcvd;
                A_TBL_CAPS:   csr_rdata <= MAX_STATES[31:0];
                default:      csr_rdata <= HARNESS_VER;
            endcase

            // ---- LOAD ----
            if (accepting) begin
                shadow_crc <= crc32c_byte(shadow_crc, in_data);
                bytes_rcvd <= bytes_rcvd + 1;
                word_sr <= nxt_word;
                bm_sr   <= nxt_bm;
                oidx_sr <= nxt_oidx;
                wr_bcnt <= wr_bcnt + 1;
                if (wr_addr < 56 && word_last) begin
                    case (wr_addr[7:2])
                        6'd2:  hdr_version    <= nxt_word;
                        6'd3:  hdr_engine_id  <= nxt_word;
                        6'd4:  hdr_n_states   <= nxt_word;
                        6'd5:  hdr_n_patterns <= nxt_word;
                        6'd6:  off_bm         <= nxt_word;
                        6'd7:  off_base       <= nxt_word;
                        6'd8:  off_dense      <= nxt_word;
                        6'd9:  off_fail       <= nxt_word;
                        6'd10: off_oidx       <= nxt_word;
                        6'd11: off_oflat      <= nxt_word;
                        default: ;
                    endcase
                end
                wr_addr <= wr_addr + 1;
            end

            // ---- SCAN ----
            if (!load_mode && active_valid) begin
                // Input queue.  Push and pop can happen in the same cycle, so
                // the three combinations are spelt out rather than layered as
                // two independent updates that would fight over sk0.
                if (pop) perf_bytes <= perf_bytes + 64'd1;
                case ({push, pop})
                    2'b10: begin
                        if (sk_n == 2'd0) begin
                            sk0_d <= in_data; sk0_l <= in_last;
                        end else begin
                            sk1_d <= in_data; sk1_l <= in_last;
                        end
                        sk_n <= sk_n + 2'd1;
                    end
                    2'b01: begin
                        sk0_d <= sk1_d; sk0_l <= sk1_l;
                        sk_n  <= sk_n - 2'd1;
                    end
                    2'b11: begin
                        if (sk_n == 2'd1) begin
                            sk0_d <= in_data; sk0_l <= in_last;
                        end else begin
                            sk0_d <= sk1_d;   sk0_l <= sk1_l;
                            sk1_d <= in_data; sk1_l <= in_last;
                        end
                        // depth unchanged: one in, one out
                    end
                    default: ;
                endcase
                case (st)
                    S_IDLE: if (have_byte) begin
                        cur_byte <= byte_in;
                        if (req_done) begin
                            // First byte of a NEW request: fresh offsets,
                            // matching restarts at the root.
                            pos      <= 64'd0;
                            state_q  <= 32'd0;
                            a_state  <= 32'd0;
                            req_done <= 1'b0;
                        end else begin
                            a_state <= state_q;
                        end
                        // byte_last, NOT in_last: when the byte came from the
                        // skid its end-of-request marker came with it, and
                        // the wire's in_last has long since gone away.
                        if (byte_last) req_done <= 1'b1;
                        st <= S_FETCH;
                    end
                    // Two waits, not one: a URAM cascade this deep needs an
                    // output register, and guessing one cycle would repeat
                    // the off-by-one that produced zero matches earlier.
                    // Harmless when the array falls back to BRAM (small
                    // configs) -- just a cycle slower.
                    S_FETCH:  st <= S_FETCH2;
                    S_FETCH2: st <= S_RANK;
                    S_RANK: begin
                        // CAPTURE ONLY -- deliberately no branch here.
                        //
                        // `hit` is d_bitmap[cur_byte], a 256:1 multiplexer
                        // over the BRAM output.  Branching on it in this
                        // state put that mux (MUXF7/MUXF8 pairs) in front of
                        // the FSM logic and hence state_q's clock enable,
                        // which measured as the critical path at +0.046 ns:
                        //   bitmap_mem_reg_bram_24 -> state_q_reg[0]/CE
                        // Registering it here cuts the path at the earliest
                        // possible point -- right at the BRAM output -- and
                        // every consumer downstream sees a plain flop.
                        r_pc0 <= pc32(lane0); r_pc1 <= pc32(lane1);
                        r_pc2 <= pc32(lane2); r_pc3 <= pc32(lane3);
                        r_pc4 <= pc32(lane4); r_pc5 <= pc32(lane5);
                        r_pc6 <= pc32(lane6); r_pc7 <= pc32(lane7);
                        r_partial <= pc32(sel_lane_bits & sub_mask);
                        r_lane    <= sel_lane;
                        r_base    <= d_base;
                        r_fail    <= d_fail;
                        r_hit     <= hit;
                        st        <= S_RANK2;
                    end
                    S_RANK2: begin
                        if (r_hit) begin
                            a_dense <= r_base + {23'd0, rank2};
                            st      <= S_DWAIT;
                        end else if (state_q != 0) begin
                            state_q <= r_fail;   // registered, not the mux
                            a_state <= r_fail;
                            st      <= S_FETCH;  // retry the SAME byte
                        end else begin
                            pos <= pos + 1;      // miss at the root
                            st  <= S_IDLE;
                        end
                    end
                    S_DWAIT: st <= S_DENSE;
                    S_DENSE: begin
                        state_q <= d_dense;
                        a_oidx  <= d_dense;
                        pos     <= pos + 1;
                        st      <= S_OWAIT;
                    end
                    S_OWAIT:  st <= S_OWAIT2;
                    S_OWAIT2: st <= S_OIDX;
                    S_OIDX: begin
                        if (d_oidx_q[63:32] != 32'd0) begin
                            emit_off  <= d_oidx_q[31:0];
                            emit_left <= d_oidx_q[63:32];
                            emit_end  <= pos;
                            a_oflat   <= d_oidx_q[31:0];
                            st        <= S_FWAIT;
                        end else st <= S_IDLE;
                    end
                    S_FWAIT: st <= S_EMIT;
                    S_EMIT: if (out_cap != 32'd0 && out_count >= out_cap) begin
                        // R47 overflow: report what fits and flag it, rather
                        // than overrunning the wrapper's reply store.  SR3 is
                        // unaffected -- an overflowed reply is still a set of
                        // real nominations, and the host re-verifies anyway.
                        ovf_f <= 1'b1;
                        st    <= S_IDLE;
                    end else begin
                        res_wr         <= 1'b1;
                        res_pattern_id <= d_oflat;
                        res_start      <= 64'd0;  // host derives from end
                        res_end        <= emit_end;
                        res_flags      <= {epoch[15:0], 16'h0001};
                        out_count      <= out_count + 1;
                        emit_off       <= emit_off + 1;
                        a_oflat        <= emit_off + 1;
                        emit_left      <= emit_left - 1;
                        // Each further output is another registered read.
                        st <= (emit_left == 32'd1) ? S_IDLE : S_FWAIT;
                    end
                    default: st <= S_IDLE;
                endcase

                // Request complete: the byte carrying in_last has been
                // consumed (req_done) AND the pipeline has drained back to
                // the root (st == S_IDLE).  Both halves are needed -- ending
                // on req_done alone would report DONE while matches were
                // still being emitted, and the wrapper would sample
                // out_count too early.
                if (busy && req_done && st == S_IDLE) begin
                    busy   <= 1'b0;
                    done_f <= 1'b1;
                end
            end
        end
    end

endmodule

`default_nettype wire
