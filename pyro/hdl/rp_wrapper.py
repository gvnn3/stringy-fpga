"""Pattern RP-child wrapper generator (spec §10.2, R78-R82/R87/R88; Phase 2b).

A per-pattern PYRO circuit becomes a **PARTIAL bitstream** for the reconfigurable
partition ``pyro_rp`` (R72/R82).  The DFX flow (``script/build_pr.tcl``) does
``synth_design -top pyro_rp`` and links the result into the ``pyro_rp`` cell of
the locked static DCP (R80/R82b), so a pattern child MUST be a module named
**``pyro_rp``** with the **exact R80 boundary ports** — a drop-in replacement for
the default ID-stub child (``pyro_id_stub.sv``).

This module emits that wrapper.  It reuses the ID stub's structural conventions
(``pyro_id_stub.sv``): byte ``k`` of a frame lives in ``tdata[8*k +: 8]``
(least-significant lane = first wire byte, the OpenNIC/Xilinx AXIS convention);
replies MAC-swap and echo ``slot``/``seq`` (R78); ``tuser = {dst,src,size}`` with
``size`` = the C2H frame byte length (R78.9); every reply is zero-padded to the
60-byte L2 minimum (R78.9).  Unlike the single-beat ID stub, a pattern child
handles a **multi-beat** ``MATCH_REQUEST`` (corpus up to 1474 B, R78.6): it
buffers the request frame, drives the buffered corpus into the generated engine
(``pyro_circuit``, one byte/cycle per its harness contract, R48/§7.4), captures
the engine's result-ring writes as R47 ``pyro_match`` entries, and emits a
**multi-beat** ``MATCH_REPLY`` (R78.7) via a proper AXIS master (combinational
data/keep/last/valid, registered handshake advance).

Wire behavior of a pattern child vs. the default stub (R78.5/R78.8):
  * ``ID_REQUEST`` → ``ID_REPLY`` with ``rp_child_id != 0`` (a non-zero value
    identifies a *loaded pattern circuit*, R78.5/R78.5a; the ID stub reports 0).
  * ``MATCH_REQUEST`` addressed to this child's ``SLOT`` (slot 1, R87) →
    ``MATCH_REPLY`` with the engine's ``pyro_match`` windows and the OVF status
    bit when the engine truncated the set (R78.7).
  * ``MATCH_REQUEST`` for any other slot (0, or ≥ 2) → ``STATUS``/``ERROR``
    ``PYRO_E_NOT_RESIDENT`` (R78.8/R87).

**v2.2.4 blessings (design choices ratified by the spec — cited, not invented):**
  1. **R78.5a** — ``rp_child_id`` is the **low 32 bits of the R47a pattern hash,
     forced non-zero** (``0x00000001`` if the low 32 bits are 0); overridable but
     always non-zero.  It is host-verifiable (the host holds the same hash).
     Injected as the ``RP_CHILD_ID`` parameter.
  2. **R78.5b** — the wire ``harness_version`` (``ID_REPLY``) carries the **R45
     resident-harness contract version** (``0x00010000``, matching the ID stub),
     a DISTINCT namespace from the ``PYROART1`` artifact ``HARNESS_VERSION``
     (``0x00020000``).  The child reports the wire/R45 value.
  3. **R87** — the single-tenant pattern occupies **slot 1**; children default to
     ``SLOT = 1``.  Multi-slot (``slot ≥ 2``) is deferred; such requests get
     ``PYRO_E_NOT_RESIDENT``.

Note (S11): a pattern child reports ``BUILD16 = 0`` in its ``ID_REPLY`` by default
(the host probe only checks ``SPEC16``, R81/R83; ``BUILD16`` is a build-injected
discriminator and is not required to equal any fixed value).  A build MAY inject a
real ``BUILD16`` via the ``PYRO_BUILD16`` define / ``BUILD16`` parameter.
"""

from __future__ import annotations

from typing import Optional

# The engine module name the L2 generator (pyro.hdl.generator) always emits.
ENGINE_MODULE = "pyro_circuit"

# Wire-side constants mirrored from pyro_id_stub.sv (R78/R81/R78.5b).
DEFAULT_SPEC16 = 0x0202
DEFAULT_WIRE_HARNESS_VERSION = 0x0001_0000  # R45/R78.5b resident-harness wire version
DEFAULT_SLOT = 1                            # single-tenant resident slot (R87)

# Buffer sizing: a full PYRO frame is ≤ 1518 B (R78.9); round up to 24×64 beats.
MAX_FRAME_BYTES = 1536
MAX_ENTRIES = 61                            # R78.7 max pyro_match entries / reply


def rp_child_id_from_hash(pattern_hash_hex: str) -> int:
    """Derive a non-zero ``RP_CHILD_ID`` (R78.5a) from the pattern hash.

    R78.5a: the low 32 bits of the 16-byte R47a pattern hash, forced non-zero
    (``0x00000001`` if the low 32 bits are 0) so it can never collide with the ID
    stub's ``rp_child_id == 0`` ("no pattern resident").  Deterministic and
    host-verifiable (the host holds the same pattern hash).
    """
    try:
        raw = bytes.fromhex(pattern_hash_hex)
    except ValueError:
        raw = b""
    word = int.from_bytes(raw[:4], "little") if len(raw) >= 4 else 0
    return word if word != 0 else 0x00000001


def generate_rp_child(pattern_hash_hex: str, *, slot: int = DEFAULT_SLOT,
                      rp_child_id: Optional[int] = None, build16: int = 0,
                      spec16: int = DEFAULT_SPEC16,
                      wire_harness_version: int = DEFAULT_WIRE_HARNESS_VERSION,
                      ) -> str:
    """Emit the ``pyro_rp`` pattern-child wrapper SystemVerilog for one engine.

    The returned text is a module ``pyro_rp`` (the R80 reconfigurable cell) that
    instantiates the generated engine ``pyro_circuit``.  The engine RTL
    (``SynthJob.rtl`` / ``GeneratedCircuit.rtl``) is a **separate** source file;
    the DFX flow reads both.  Deterministic: same inputs ⇒ byte-identical text.
    """
    if rp_child_id is None:
        rp_child_id = rp_child_id_from_hash(pattern_hash_hex)
    rp_child_id &= 0xFFFFFFFF
    if rp_child_id == 0:
        rp_child_id = 0x00000001  # R78.5a: a loaded pattern MUST report non-zero
    slot &= 0xFFFF
    build16 &= 0xFFFF
    spec16 &= 0xFFFF
    wire_harness_version &= 0xFFFFFFFF

    return (_TEMPLATE
            .replace("@ENGINE@", ENGINE_MODULE)
            .replace("@MAXFRAME@", str(MAX_FRAME_BYTES))
            .replace("@MAXENT@", str(MAX_ENTRIES))
            .replace("@SPEC16@", f"16'h{spec16:04X}")
            .replace("@BUILD16@", f"16'h{build16:04X}")
            .replace("@HARNESSVER@", f"32'h{wire_harness_version:08X}")
            .replace("@SLOT@", f"16'd{slot}")
            .replace("@RPCHILDID@", f"32'h{rp_child_id:08X}"))


# NB: the SV body contains many ``{...}`` concatenations, so substitution uses
# .replace on @SENTINELS@ (never str.format) — same discipline as the toolchain.
_TEMPLATE = r"""// *************************************************************************
// PYRO Phase 2b - per-pattern reconfigurable-partition child `pyro_rp`.
//
// AUTO-GENERATED by pyro.hdl.rp_wrapper - do not edit.
//
// Drop-in replacement for the default ID-stub child with the SAME frozen R80
// boundary ports so it links against the same locked static DCP (R82b).  Parses
// PYRO control frames in-RP (R79), answers ID_REQUEST with a non-zero rp_child_id
// (R78.5a, "loaded pattern"), serves MATCH_REQUEST for its SLOT by streaming the
// corpus through the generated engine and replying with R47 pyro_match entries
// (R78.7), and returns PYRO_E_NOT_RESIDENT for a MATCH_REQUEST to any other slot
// (R78.8/R87).  Byte k of a frame occupies tdata[8*k +: 8].
// *************************************************************************
`timescale 1ns/1ps

`ifndef PYRO_BUILD16
  `define PYRO_BUILD16 @BUILD16@
`endif
`ifndef PYRO_RP_CHILD_ID
  `define PYRO_RP_CHILD_ID @RPCHILDID@
`endif

module pyro_rp #(
  parameter [15:0] SPEC16          = @SPEC16@,          // R81 spec 2.2 => 0x0202
  parameter [15:0] BUILD16         = `PYRO_BUILD16,     // R81 build discriminator (default 0, S11)
  parameter [31:0] HARNESS_VERSION = @HARNESSVER@,      // R78.5b wire/R45 harness version
  parameter [15:0] SLOT            = @SLOT@,            // R87 this child's slot (1)
  parameter [31:0] RP_CHILD_ID     = `PYRO_RP_CHILD_ID  // R78.5a non-zero = loaded
) (
  input                clk,
  input                rstn,

  // AXI-Stream slave: host -> device (QDMA H2C) frames into the RP (R80)
  input                s_axis_tvalid,
  input        [511:0] s_axis_tdata,
  input         [63:0] s_axis_tkeep,
  input                s_axis_tlast,
  input         [47:0] s_axis_tuser,   // {dst[15:0], src[15:0], size[15:0]}
  output               s_axis_tready,

  // AXI-Stream master: device -> host (QDMA C2H) frames out of the RP (R80).
  // Combinational AXIS master (B1): data/keep/last/valid are driven from the
  // CURRENT tx_beat; only the handshake advance is registered.  This keeps the
  // presented beat phase-aligned with tx_beat so no beat is duplicated/dropped.
  output               m_axis_tvalid,
  output       [511:0] m_axis_tdata,
  output        [63:0] m_axis_tkeep,
  output               m_axis_tlast,
  output        [47:0] m_axis_tuser,
  input                m_axis_tready
);

  // ---- sizing / R78 constants -------------------------------------------
  localparam integer MAX_FRAME = @MAXFRAME@;   // bytes buffered (>= 1518, R78.9)
  localparam integer MAXENT    = @MAXENT@;     // max pyro_match entries / reply (R78.7)

  localparam [7:0]  KIND_ID_REQ      = 8'h01;
  localparam [7:0]  KIND_ID_REPLY    = 8'h02;
  localparam [7:0]  KIND_MATCH_REQ   = 8'h03;
  localparam [7:0]  KIND_MATCH_REPLY = 8'h04;
  localparam [7:0]  KIND_STATUS_ERR  = 8'h05;
  localparam [7:0]  PYRO_MAGIC       = 8'h50;  // 'P'
  localparam [7:0]  PYRO_VER         = 8'h01;  // protocol version 1
  localparam [7:0]  ETH_HI           = 8'h88;  // EtherType 0x88B5 big-endian
  localparam [7:0]  ETH_LO           = 8'hB5;
  localparam [31:0] E_NOT_RESIDENT   = 32'd7;  // PYRO_E_NOT_RESIDENT (R38/R78.8)

  // Engine CSR offsets (pyro.hdl.generator §7.4 R45 harness contract).
  localparam [15:0] CSR_CTRL    = 16'h0010;    // bit0 START, bit1 RESET
  localparam [15:0] CSR_STATUS  = 16'h0014;    // bit0 BUSY, bit1 DONE, bit3 OVF
  localparam [15:0] CSR_OUT_CAP = 16'h0048;
  localparam [31:0] CTRL_START  = 32'h0000_0001;
  localparam [31:0] CTRL_RESET  = 32'h0000_0002;

  // ---- frame buffers -----------------------------------------------------
  reg  [7:0]  rx_buf [0:MAX_FRAME-1];
  reg  [7:0]  tx_buf [0:MAX_FRAME-1];
  reg  [15:0] rx_len;
  reg  [15:0] tx_len;
  reg  [15:0] rx_beat;

  // ---- result capture (R47 pyro_match entries) ---------------------------
  reg  [63:0] m_start [0:MAXENT-1];
  reg  [63:0] m_end   [0:MAXENT-1];
  reg  [31:0] m_pid   [0:MAXENT-1];
  reg  [31:0] m_flg   [0:MAXENT-1];
  reg  [15:0] match_count;
  reg         ovf;

  // ---- parsed request state (valid after RX completes) -------------------
  reg  [15:0] corpus_len;    // clamped corpus length (R78.6, W5)
  reg  [15:0] reply_cap;     // min(out_cap, MAXENT)
  reg  [15:0] feed_idx;
  reg  [15:0] build_idx;

  // ---- engine (pyro_circuit) interface ----------------------------------
  reg         eng_csr_write;
  reg  [15:0] eng_csr_addr;
  reg  [31:0] eng_csr_wdata;
  wire [31:0] eng_csr_rdata;
  reg         eng_in_valid;
  reg  [7:0]  eng_in_data;
  reg         eng_in_last;
  wire        eng_res_wr;
  wire [63:0] eng_res_start;
  wire [63:0] eng_res_end;
  wire [31:0] eng_res_pid;
  wire [31:0] eng_res_flags;

  @ENGINE@ engine_inst (
    .clk            (clk),
    .rst_n          (rstn),
    .csr_addr       (eng_csr_addr),
    .csr_wdata      (eng_csr_wdata),
    .csr_write      (eng_csr_write),
    .csr_rdata      (eng_csr_rdata),
    .in_valid       (eng_in_valid),
    .in_data        (eng_in_data),
    .in_last        (eng_in_last),
    .res_wr         (eng_res_wr),
    .res_start      (eng_res_start),
    .res_end        (eng_res_end),
    .res_pattern_id (eng_res_pid),
    .res_flags      (eng_res_flags)
  );

  // ---- responder FSM -----------------------------------------------------
  localparam [3:0] ST_RX        = 4'd0,   // buffering the request frame
                   ST_CLASSIFY  = 4'd1,   // decode header, choose reply kind
                   ST_RESET_ENG = 4'd2,   // W4: reset the engine before a scan
                   ST_CAP       = 4'd3,   // write engine OUT_CAP CSR
                   ST_START     = 4'd4,   // write engine CTRL.START
                   ST_WAIT_BUSY = 4'd5,   // wait for engine BUSY
                   ST_FEED      = 4'd6,   // stream corpus one byte/cycle
                   ST_DRAIN     = 4'd7,   // wait for DONE, latch OVF (B2)
                   ST_BHDR      = 4'd8,   // build reply header + framing
                   ST_BENT      = 4'd9,   // serialize pyro_match entries
                   ST_TX        = 4'd10;  // drive reply beats (combinational out)
  reg [3:0]  state;
  reg [1:0]  reply_kind;   // 0 = ID_REPLY, 1 = STATUS/ERROR, 2 = MATCH_REPLY
  reg [15:0] tx_beat;

  integer k;

  // W5 clamp scratch (ST_CLASSIFY, one cycle).
  reg [15:0] raw_corpus, avail_corpus, cl_tmp;

  // Accept new frames only while receiving; back-pressure otherwise (R80).
  assign s_axis_tready = (state == ST_RX);

  // Header field views of the buffered request (big-endian, R78.3).
  wire [7:0] h_eth_hi = rx_buf[12];
  wire [7:0] h_eth_lo = rx_buf[13];
  wire [7:0] h_magic  = rx_buf[14];
  wire [7:0] h_ver    = rx_buf[15];
  wire [7:0] h_kind   = rx_buf[16];
  wire       is_pyro  = (h_eth_hi == ETH_HI) && (h_eth_lo == ETH_LO) &&
                        (h_magic == PYRO_MAGIC) && (h_ver == PYRO_VER);

  // popcount of tkeep (partial last beat) -> valid byte count this beat.
  function automatic [6:0] keep_bytes(input [63:0] keep);
    integer j;
    begin
      keep_bytes = 7'd0;
      for (j = 0; j < 64; j = j + 1)
        keep_bytes = keep_bytes + {6'd0, keep[j]};
    end
  endfunction

  // ---- combinational AXIS master output (B1, S10) ------------------------
  // Segregated scratch: these regs are driven ONLY here (no clocked driver), so
  // the presented beat is a pure function of tx_beat/tx_len/tx_buf.
  reg  [15:0]  eff_len, remaining, this_bytes, tx_ii;
  reg  [511:0] tx_d;
  reg  [63:0]  tx_k;
  integer tk;
  always @(*) begin
    eff_len    = (tx_len < 16'd60) ? 16'd60 : tx_len;     // 60-byte L2 min (R78.9)
    remaining  = eff_len - (tx_beat * 16'd64);
    this_bytes = (remaining < 16'd64) ? remaining : 16'd64;
    tx_d = 512'b0;
    tx_k = 64'b0;
    for (tk = 0; tk < 64; tk = tk + 1) begin
      tx_ii = tx_beat * 16'd64 + tk[15:0];
      if (tx_ii < tx_len) tx_d[8*tk +: 8] = tx_buf[tx_ii];  // real bytes
      // else 0 => R78.9 zero-pad region (tx_len..eff_len)
      if (tk[15:0] < this_bytes) tx_k[tk] = 1'b1;
    end
  end
  assign m_axis_tvalid = (state == ST_TX);
  assign m_axis_tdata  = tx_d;
  assign m_axis_tkeep  = tx_k;
  assign m_axis_tlast  = (state == ST_TX) && (remaining <= 16'd64);
  // tuser {dst,src,size}: size = full C2H frame byte length (R78.9); src/dst are
  // set/overridden by the static glue on egress (see the ID stub).
  assign m_axis_tuser  = {16'h0000, 16'h0000, eff_len};

  always @(posedge clk) begin
    if (!rstn) begin
      state         <= ST_RX;
      rx_len        <= 16'd0;
      rx_beat       <= 16'd0;
      tx_len        <= 16'd0;
      tx_beat       <= 16'd0;
      match_count   <= 16'd0;
      ovf           <= 1'b0;
      feed_idx      <= 16'd0;
      build_idx     <= 16'd0;
      reply_kind    <= 2'd0;
      corpus_len    <= 16'd0;
      reply_cap     <= 16'd0;
      eng_csr_write <= 1'b0;
      eng_csr_addr  <= CSR_STATUS;
      eng_csr_wdata <= 32'b0;
      eng_in_valid  <= 1'b0;
      eng_in_data   <= 8'b0;
      eng_in_last   <= 1'b0;
    end else begin
      // defaults each cycle (pulsed controls; default CSR read = STATUS so BUSY/
      // DONE/OVF polling is always valid between writes).
      eng_csr_write <= 1'b0;
      eng_csr_addr  <= CSR_STATUS;
      eng_in_valid  <= 1'b0;
      eng_in_last   <= 1'b0;

      // Capture result-ring writes whenever the engine pulses res_wr (R47).  The
      // engine self-limits at OUT_CAP (== reply_cap) and reports OVF in its
      // status (bit3), which ST_DRAIN latches (B2); this bound keeps match_count
      // within reply_cap so the entry arrays never overflow.
      if (eng_res_wr && (match_count < reply_cap)) begin
        m_start[match_count[5:0]] <= eng_res_start;
        m_end  [match_count[5:0]] <= eng_res_end;
        m_pid  [match_count[5:0]] <= eng_res_pid;
        m_flg  [match_count[5:0]] <= eng_res_flags;
        match_count <= match_count + 16'd1;
      end

      case (state)
        // ---- receive + buffer the request frame ------------------------
        ST_RX: begin
          if (s_axis_tvalid && s_axis_tready) begin
            for (k = 0; k < 64; k = k + 1) begin
              if (s_axis_tkeep[k] && ((rx_beat*64 + k) < MAX_FRAME))
                rx_buf[rx_beat*64 + k] <= s_axis_tdata[8*k +: 8];
            end
            rx_len  <= rx_beat*64 + {9'd0, keep_bytes(s_axis_tkeep)};
            rx_beat <= rx_beat + 16'd1;
            if (s_axis_tlast) state <= ST_CLASSIFY;
          end
        end

        // ---- decode header, pick the reply kind ------------------------
        ST_CLASSIFY: begin
          match_count <= 16'd0;
          ovf         <= 1'b0;
          feed_idx    <= 16'd0;
          build_idx   <= 16'd0;
          if (!is_pyro) begin
            state   <= ST_RX;                // not a PYRO frame -> drop (R78.1)
            rx_beat <= 16'd0;
          end else if (h_kind == KIND_ID_REQ) begin
            reply_kind <= 2'd0;              // ID_REPLY
            state <= ST_BHDR;
          end else if (h_kind == KIND_MATCH_REQ) begin
            if ({rx_buf[18], rx_buf[19]} != SLOT) begin
              reply_kind <= 2'd1;            // STATUS/ERROR NOT_RESIDENT (R78.8/R87)
              state <= ST_BHDR;
            end else begin
              reply_kind <= 2'd2;            // MATCH_REPLY (R78.7)
              // W5: clamp corpus_len = min(length-12, rx_len-40, MAX_FRAME-40),
              // guarding rx_len < 40, so a host-crafted `length` can never make
              // the feed read past the bytes actually buffered.
              raw_corpus   = ({rx_buf[24], rx_buf[25]} >= 16'd12)
                             ? ({rx_buf[24], rx_buf[25]} - 16'd12) : 16'd0;
              avail_corpus = (rx_len >= 16'd40) ? (rx_len - 16'd40) : 16'd0;
              cl_tmp = raw_corpus;
              if (cl_tmp > avail_corpus)     cl_tmp = avail_corpus;
              if (cl_tmp > (MAX_FRAME - 40)) cl_tmp = MAX_FRAME - 40;
              corpus_len <= cl_tmp;
              reply_cap  <= ({rx_buf[36], rx_buf[37]} < MAXENT[15:0])
                            ? {rx_buf[36], rx_buf[37]} : MAXENT[15:0];
              state <= ST_RESET_ENG;
            end
          end else begin
            state   <= ST_RX;                // unknown kind -> drop (R78.4)
            rx_beat <= 16'd0;
          end
        end

        // ---- W4: explicitly RESET the engine before each scan ----------
        // Clears residual running/state/out_count/status from a prior scan so
        // scan #2+ re-seeds cleanly rather than relying on ctrl_start staying
        // high (which would leave the engine running with stale state).
        ST_RESET_ENG: begin
          if (corpus_len == 16'd0) begin
            state <= ST_BHDR;                // empty corpus -> no matches
          end else begin
            eng_csr_write <= 1'b1;
            eng_csr_addr  <= CSR_CTRL;
            eng_csr_wdata <= CTRL_RESET;     // de-assert START, clear engine
            state <= ST_CAP;
          end
        end
        // ---- drive the engine: OUT_CAP then CTRL.START (R48) -----------
        ST_CAP: begin
          eng_csr_write <= 1'b1;
          eng_csr_addr  <= CSR_OUT_CAP;
          eng_csr_wdata <= {16'd0, reply_cap};
          state <= ST_START;
        end
        ST_START: begin
          eng_csr_write <= 1'b1;
          eng_csr_addr  <= CSR_CTRL;
          eng_csr_wdata <= CTRL_START;       // START (R48 CTRL bit0)
          state <= ST_WAIT_BUSY;
        end
        ST_WAIT_BUSY: begin
          if (eng_csr_rdata[0]) begin         // engine BUSY -> begin streaming
            feed_idx <= 16'd0;
            state    <= ST_FEED;
          end
        end

        // ---- stream corpus one byte/cycle into the engine (R48) --------
        ST_FEED: begin
          eng_in_valid <= 1'b1;
          eng_in_data  <= rx_buf[16'd40 + feed_idx];   // corpus base = frame off 40
          eng_in_last  <= (feed_idx == (corpus_len - 16'd1));
          if (feed_idx == (corpus_len - 16'd1))
            state <= ST_DRAIN;
          feed_idx <= feed_idx + 16'd1;
        end

        // ---- wait for DONE; latch OVF from the engine status (B2) ------
        // The engine (OUT_CAP == reply_cap) truncates at reply_cap and sets its
        // own status OVF bit3 when more matches existed than returned; OR it in
        // so the MATCH_REPLY status carries OVF and the host resumes (R41/R47).
        // DONE (bit1) is a level, not a pulse, so polling is safe (W4).
        ST_DRAIN: begin
          ovf <= ovf | eng_csr_rdata[3];      // latch engine OVF each cycle
          if (eng_csr_rdata[1])                // engine DONE (R48 status bit1)
            state <= ST_BHDR;
        end

        // ---- build the reply header + payload framing ------------------
        ST_BHDR: begin
          // Ethernet: MAC swap (dst <- req src, src <- req dst), EtherType.
          for (k = 0; k < 6; k = k + 1) begin
            tx_buf[k]     <= rx_buf[6 + k];
            tx_buf[6 + k] <= rx_buf[k];
          end
          tx_buf[12] <= ETH_HI;
          tx_buf[13] <= ETH_LO;
          // PYRO control header (R78.3): magic ver kind flags slot seq length resv
          tx_buf[14] <= PYRO_MAGIC;
          tx_buf[15] <= PYRO_VER;
          tx_buf[16] <= (reply_kind == 2'd0) ? KIND_ID_REPLY :
                        (reply_kind == 2'd1) ? KIND_STATUS_ERR : KIND_MATCH_REPLY;
          tx_buf[17] <= 8'h00;                 // flags
          tx_buf[18] <= rx_buf[18];            // slot echo (BE)
          tx_buf[19] <= rx_buf[19];
          tx_buf[20] <= rx_buf[20];            // seq echo (BE)
          tx_buf[21] <= rx_buf[21];
          tx_buf[22] <= rx_buf[22];
          tx_buf[23] <= rx_buf[23];
          tx_buf[26] <= 8'h00;                 // reserved
          tx_buf[27] <= 8'h00;
          if (reply_kind == 2'd0) begin
            // ID_REPLY payload (R78.5/R78.5a/R78.5b): static_shell_id | harness_version | rp_child_id
            tx_buf[24] <= 8'h00; tx_buf[25] <= 8'h0C;   // length = 12
            tx_buf[28] <= SPEC16[15:8];  tx_buf[29] <= SPEC16[7:0];
            tx_buf[30] <= BUILD16[15:8]; tx_buf[31] <= BUILD16[7:0];
            tx_buf[32] <= HARNESS_VERSION[31:24]; tx_buf[33] <= HARNESS_VERSION[23:16];
            tx_buf[34] <= HARNESS_VERSION[15:8];  tx_buf[35] <= HARNESS_VERSION[7:0];
            tx_buf[36] <= RP_CHILD_ID[31:24]; tx_buf[37] <= RP_CHILD_ID[23:16];
            tx_buf[38] <= RP_CHILD_ID[15:8];  tx_buf[39] <= RP_CHILD_ID[7:0];
            tx_len  <= 16'd40;
            tx_beat <= 16'd0;
            state   <= ST_TX;
          end else if (reply_kind == 2'd1) begin
            // STATUS/ERROR payload (R78.8): code (4B BE) = PYRO_E_NOT_RESIDENT
            tx_buf[24] <= 8'h00; tx_buf[25] <= 8'h04;   // length = 4
            tx_buf[28] <= E_NOT_RESIDENT[31:24]; tx_buf[29] <= E_NOT_RESIDENT[23:16];
            tx_buf[30] <= E_NOT_RESIDENT[15:8];  tx_buf[31] <= E_NOT_RESIDENT[7:0];
            tx_len  <= 16'd32;
            tx_beat <= 16'd0;
            state   <= ST_TX;
          end else begin
            // MATCH_REPLY payload (R78.7): count(2 BE) status(2 BE) resv(4) entries
            tx_buf[24] <= (16'd8 + (match_count * 16'd24)) >> 8;    // length hi
            tx_buf[25] <= (16'd8 + (match_count * 16'd24)) & 16'hFF; // length lo
            tx_buf[28] <= match_count[15:8]; tx_buf[29] <= match_count[7:0];
            tx_buf[30] <= 8'h00;             tx_buf[31] <= {7'd0, ovf}; // status bit0 OVF
            tx_buf[32] <= 8'h00; tx_buf[33] <= 8'h00;
            tx_buf[34] <= 8'h00; tx_buf[35] <= 8'h00;
            build_idx <= 16'd0;
            state <= ST_BENT;
          end
        end

        // ---- serialize pyro_match entries, one per cycle (R47, LE) -----
        ST_BENT: begin
          if (build_idx >= match_count) begin
            tx_len  <= 16'd36 + (match_count * 16'd24);
            tx_beat <= 16'd0;
            state   <= ST_TX;
          end else begin
            // entry base = 36 + 24*build_idx; each field little-endian (R47)
            for (k = 0; k < 8; k = k + 1)
              tx_buf[16'd36 + (build_idx * 16'd24) + k]      <= m_start[build_idx[5:0]][8*k +: 8];
            for (k = 0; k < 8; k = k + 1)
              tx_buf[16'd36 + (build_idx * 16'd24) + 8 + k]  <= m_end[build_idx[5:0]][8*k +: 8];
            for (k = 0; k < 4; k = k + 1)
              tx_buf[16'd36 + (build_idx * 16'd24) + 16 + k] <= m_pid[build_idx[5:0]][8*k +: 8];
            for (k = 0; k < 4; k = k + 1)
              tx_buf[16'd36 + (build_idx * 16'd24) + 20 + k] <= m_flg[build_idx[5:0]][8*k +: 8];
            build_idx <= build_idx + 16'd1;
          end
        end

        // ---- drive reply beats (combinational outputs; register handshake) --
        // m_axis_tvalid is high throughout ST_TX; the current beat is presented
        // combinationally from tx_beat.  Advance on each accepted beat; drop out
        // of ST_TX (tvalid low) after the final beat is accepted (B1).
        ST_TX: begin
          if (m_axis_tready) begin
            if (remaining <= 16'd64) begin       // final beat just accepted
              rx_beat <= 16'd0;
              state   <= ST_RX;
            end else begin
              tx_beat <= tx_beat + 16'd1;
            end
          end
        end

        default: state <= ST_RX;
      endcase
    end
  end

endmodule : pyro_rp
"""
