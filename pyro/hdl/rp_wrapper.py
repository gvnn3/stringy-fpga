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
  * ``PERF_REQUEST`` for this child's ``SLOT`` → ``PERF_REPLY`` carrying the
    R45a ``CYCLES``/``BYTES`` counters of the most recent scan (R78.11, v2.4.0);
    any other slot → ``STATUS``/``ERROR`` ``PYRO_E_NOT_RESIDENT``.

**v2.2.4 blessings (design choices ratified by the spec — cited, not invented):**
  1. **R78.5a** — ``rp_child_id`` is the **low 32 bits of the R47a pattern hash,
     forced non-zero** (``0x00000001`` if the low 32 bits are 0); overridable but
     always non-zero.  It is host-verifiable (the host holds the same hash).
     Injected as the ``RP_CHILD_ID`` parameter.
  2. **R78.5b** — the wire ``harness_version`` (``ID_REPLY``) carries the **R45
     resident-harness contract version** (``0x00010000``, matching the ID stub),
     a DISTINCT namespace from the ``PYROART1`` artifact ``HARNESS_VERSION``
     (``0x00020100`` as of spec v2.3.0).  The child reports the wire/R45 value.
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
# R78.9a (v2.6.0/P2c): buffer sizing on a MAX_PKT_LEN=9600 jumbo shell —
# 150×64 B beats.  Emitted only when generate_rp_child is asked for it; the
# 1536 emission is byte-identical to pre-P2c.
JUMBO_FRAME_BYTES = 9600
MAX_ENTRIES = 61                            # R78.7 max pyro_match entries / reply

# The template's address decompositions (word-select [10:6], word index
# [4:0], byte-in-word [5:0]) are exact only for NWORDS <= 32 / offsets < 2048;
# the jumbo emission widens them to [13:6]/[7:0] (NWORDS <= 256, offsets <
# 16384) via _jumbo_slices.  Guard the DEFAULT here so an unconsidered bump
# fails loud instead of silently aliasing addresses in silicon.
assert MAX_FRAME_BYTES % 64 == 0 and MAX_FRAME_BYTES <= 2048, (
    "rp_wrapper address slices assume NWORDS <= 32; widen the generated "
    "slices before raising MAX_FRAME_BYTES")
assert JUMBO_FRAME_BYTES % 64 == 0 and JUMBO_FRAME_BYTES <= 16384, (
    "jumbo slices assume NWORDS <= 256 / offsets < 16384")


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


def _replace1(text: str, old: str, new: str) -> str:
    """One exact-match replacement; loud failure if the anchor drifted."""
    assert text.count(old) == 1, f"wrapper template anchor missing/dup: {old[:60]!r}"
    return text.replace(old, new)


def _replace_n(text: str, old: str, new: str, n: int) -> str:
    """Exactly-``n`` replacement; loud failure if the anchor count drifted."""
    assert text.count(old) == n, (
        f"wrapper template anchor count {text.count(old)} != {n}: {old[:60]!r}")
    return text.replace(old, new)


def _jumbo_slices(t: str) -> str:
    """Widen the template's word-index address slices for JUMBO_FRAME_BYTES
    (R78.9a): NWORDS goes 24 -> 150, so every 5-bit word index becomes 8 bits
    and every [10:6] word-select becomes [13:6].  Anchored, count-asserted —
    the same discipline as _widen_template.  Applies to both the v2 and v3
    (wide-feed) template variants; the wide ST_FEED's word-select is widened
    by the generic [10:6] rewrite below."""
    t = _replace1(t, "rx_words[rx_beat[4:0]]", "rx_words[rx_beat[7:0]]")
    t = _replace1(t, "tx_words[tx_beat[4:0]]", "tx_words[tx_beat[7:0]]")
    t = _replace1(t, "reg [4:0]   txw_sel;", "reg [7:0]   txw_sel;")
    t = _replace_n(t, "txw_sel = 5'd0;", "txw_sel = 8'd0;", 3)
    t = _replace_n(t, "txw_sel = compose_idx[10:6];",
                   "txw_sel = compose_idx[13:6];", 2)
    # word-selects of the corpus feed: one in the ST_FEED code (v2 byte feed
    # or v3 wide feed) + one in the rx_words declaration comment
    t = _replace_n(t, "rx_words[feed_addr[10:6]]", "rx_words[feed_addr[13:6]]", 2)
    assert "feed_addr[10:6]" not in t
    return t


def _widen_template(n: int) -> str:
    """Rewrite the v2 wrapper template for an ``n``-byte/cycle engine (P2b v3).

    Only the engine-facing feed changes: ``eng_in_data`` widens to ``8*n`` bits
    with an ``eng_in_keep`` lane mask, and ST_FEED streams ``n`` bytes/cycle.
    The R78 wire format, reply composition, and every other state are
    untouched.  Constraint: corpus base offset 40 must be n-aligned and each
    n-byte group must not straddle a 512-bit word — true for n in {2, 4, 8}
    (40 % 8 == 0); n == 16 would straddle, hence the guard.
    """
    assert n in (2, 4, 8), f"harness v3 feed supports n in (2,4,8), got {n}"
    lo = n.bit_length() - 1                       # log2(n)
    t = _TEMPLATE
    t = _replace1(
        t,
        "  reg         eng_in_valid;\n"
        "  reg  [7:0]  eng_in_data;\n"
        "  reg         eng_in_last;",
        f"  reg         eng_in_valid;\n"
        f"  reg  [{8*n-1}:0] eng_in_data;   // P2b v3: {n} bytes/cycle\n"
        f"  reg  [{n-1}:0]  eng_in_keep;\n"
        f"  reg         eng_in_last;")
    t = _replace1(
        t,
        "    .in_valid       (eng_in_valid),\n"
        "    .in_data        (eng_in_data),\n"
        "    .in_last        (eng_in_last),",
        "    .in_valid       (eng_in_valid),\n"
        "    .in_data        (eng_in_data),\n"
        "    .in_keep        (eng_in_keep),\n"
        "    .in_last        (eng_in_last),")
    t = _replace1(
        t,
        "      eng_in_valid  <= 1'b0;\n"
        "      eng_in_data   <= 8'b0;\n"
        "      eng_in_last   <= 1'b0;",
        f"      eng_in_valid  <= 1'b0;\n"
        f"      eng_in_data   <= {{{8*n}{{1'b0}}}};\n"
        f"      eng_in_keep   <= {{{n}{{1'b0}}}};\n"
        f"      eng_in_last   <= 1'b0;")
    t = _replace1(
        t,
        "        // ---- stream corpus one byte/cycle into the engine (R48) --------\n"
        "        ST_FEED: begin\n"
        "          eng_in_valid <= 1'b1;\n"
        "          // corpus base = frame offset 40; word select + 64:1 byte mux.  feed_addr\n"
        "          // <= rx_len (W5 clamp), so this never reads a lane that was not on the wire.\n"
        "          feed_addr    = 16'd40 + feed_idx;\n"
        "          eng_in_data  <= rx_words[feed_addr[10:6]][ {feed_addr[5:0], 3'b000} +: 8 ];\n"
        "          eng_in_last  <= (feed_idx == (corpus_len - 16'd1));\n"
        "          if (feed_idx == (corpus_len - 16'd1))\n"
        "            state <= ST_DRAIN;\n"
        "          feed_idx <= feed_idx + 16'd1;\n"
        "        end",
        f"        // ---- stream corpus {n} bytes/cycle into the engine (P2b v3) ----\n"
        f"        ST_FEED: begin\n"
        f"          eng_in_valid <= 1'b1;\n"
        f"          // corpus base = frame offset 40 ({n}-aligned); each {n}-byte group\n"
        f"          // lives inside one 512-bit word, so this is one word read + a\n"
        f"          // {n*8}-bit aligned part select.  feed_addr <= rx_len (W5 clamp).\n"
        f"          feed_addr    = 16'd40 + feed_idx;\n"
        f"          eng_in_data  <= rx_words[feed_addr[10:6]]"
        f"[ {{feed_addr[5:{lo}], {lo+3}'b0}} +: {8*n} ];\n"
        f"          if ((corpus_len - feed_idx) <= 16'd{n}) begin\n"
        f"            eng_in_keep <= kmask(corpus_len - feed_idx);\n"
        f"            eng_in_last <= 1'b1;\n"
        f"            state <= ST_DRAIN;\n"
        f"          end else begin\n"
        f"            eng_in_keep <= {{{n}{{1'b1}}}};\n"
        f"          end\n"
        f"          feed_idx <= feed_idx + 16'd{n};\n"
        f"        end")
    t = _replace1(
        t,
        "  // ---- combinational AXIS master output (B1, S10) ------------------------",
        f"  // kept-lane mask for the final (short) corpus beat (P2b v3).\n"
        f"  function automatic [{n-1}:0] kmask(input [15:0] r);\n"
        f"    integer km;\n"
        f"    begin\n"
        f"      kmask = {{{n}{{1'b0}}}};\n"
        f"      for (km = 0; km < {n}; km = km + 1)\n"
        f"        if (km[15:0] < r) kmask[km] = 1'b1;\n"
        f"    end\n"
        f"  endfunction\n\n"
        f"  // ---- combinational AXIS master output (B1, S10) ------------------------")
    return t


def generate_rp_child(pattern_hash_hex: str, *, slot: int = DEFAULT_SLOT,
                      rp_child_id: Optional[int] = None, build16: int = 0,
                      spec16: int = DEFAULT_SPEC16,
                      wire_harness_version: int = DEFAULT_WIRE_HARNESS_VERSION,
                      datapath_bytes: int = 1,
                      max_frame_bytes: int = MAX_FRAME_BYTES,
                      cores: int = 1,
                      ) -> str:
    """Emit the ``pyro_rp`` pattern-child wrapper SystemVerilog for one engine.

    The returned text is a module ``pyro_rp`` (the R80 reconfigurable cell) that
    instantiates the generated engine ``pyro_circuit``.  The engine RTL
    (``SynthJob.rtl`` / ``GeneratedCircuit.rtl``) is a **separate** source file;
    the DFX flow reads both.  Deterministic: same inputs ⇒ byte-identical text.

    ``datapath_bytes`` MUST match the engine's (P2b): 1 emits the original v2
    wrapper byte-identically; 2/4/8 emit the v3 wide feed.  The R78 wire
    format is identical across widths.

    ``max_frame_bytes`` (R78.9a, P2c): MAX_FRAME_BYTES (1536) for the 1518
    shell — byte-identical emission — or JUMBO_FRAME_BYTES (9600) for a
    MAX_PKT_LEN=9600 shell (widened address slices via _jumbo_slices).  A
    1536 child on a jumbo shell is safe (the W5 clamp truncates long
    corpora); a 9600 child on a 1518 shell simply never sees long frames.
    """
    if max_frame_bytes not in (MAX_FRAME_BYTES, JUMBO_FRAME_BYTES):
        raise ValueError(
            f"max_frame_bytes must be {MAX_FRAME_BYTES} or {JUMBO_FRAME_BYTES}, "
            f"got {max_frame_bytes!r} (R78.9a)")
    if rp_child_id is None:
        rp_child_id = rp_child_id_from_hash(pattern_hash_hex)
    rp_child_id &= 0xFFFFFFFF
    if rp_child_id == 0:
        rp_child_id = 0x00000001  # R78.5a: a loaded pattern MUST report non-zero
    slot &= 0xFFFF
    build16 &= 0xFFFF
    spec16 &= 0xFFFF
    wire_harness_version &= 0xFFFFFFFF

    template = _TEMPLATE if datapath_bytes == 1 else _widen_template(datapath_bytes)
    if max_frame_bytes != MAX_FRAME_BYTES:
        template = _jumbo_slices(template)
    child = (template
             .replace("@ENGINE@", ENGINE_MODULE)
             .replace("@MAXFRAME@", str(max_frame_bytes))
             .replace("@MAXENT@", str(MAX_ENTRIES))
             .replace("@SPEC16@", f"16'h{spec16:04X}")
             .replace("@BUILD16@", f"16'h{build16:04X}")
             .replace("@HARNESSVER@", f"32'h{wire_harness_version:08X}")
             .replace("@SLOT@", f"16'd{slot}")
             .replace("@RPCHILDID@", f"32'h{rp_child_id:08X}"))
    if cores == 1:
        return child                       # byte-identical pre-v4 emission
    # v4 (P2e): frame-parallel multi-core — CORES unmodified children behind
    # a kind-aware packet demux and a packet-atomic reply mux.  The NFA state
    # recurrence caps a single engine's width (the N=8 cascade closed with
    # +0.002 ns at jumbo), so aggregate B/cyc scales by core count instead:
    # the demux round-robins MATCH frames across ready cores (RX of frame
    # k+1 proceeds while frame k scans — the overlap the serialized v3 FSM
    # never had) and routes ID/PERF to the core that most recently completed
    # a MATCH, preserving the R78.11 "most recent scan" counter semantics.
    if cores not in (2, 3, 4):
        raise ValueError(f"cores must be 1..4, got {cores!r} (P2e)")
    core_text = _replace1(child, "module pyro_rp #(", "module pyro_rp_core #(")
    core_text = _replace1(core_text, "endmodule : pyro_rp",
                          "endmodule : pyro_rp_core")
    return core_text + _MULTI_TOP.replace("@CORES@", str(cores))


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
// (R78.7), returns PYRO_E_NOT_RESIDENT for a MATCH_REQUEST to any other slot
// (R78.8/R87), and serves PERF_REQUEST with the R45a CYCLES/BYTES counters of
// the most recent scan (R78.11, v2.4.0).  Byte k of a frame occupies
// tdata[8*k +: 8].
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
  localparam integer NWORDS    = @MAXFRAME@/64; // 512-bit beats (24 = MAX_FRAME/64)
  localparam integer MAXENT    = @MAXENT@;     // max pyro_match entries / reply (R78.7)

  localparam [7:0]  KIND_ID_REQ      = 8'h01;
  localparam [7:0]  KIND_ID_REPLY    = 8'h02;
  localparam [7:0]  KIND_MATCH_REQ   = 8'h03;
  localparam [7:0]  KIND_MATCH_REPLY = 8'h04;
  localparam [7:0]  KIND_STATUS_ERR  = 8'h05;
  localparam [7:0]  KIND_PERF_REQ    = 8'h06;  // R45a read-out (R78.11, v2.4.0)
  localparam [7:0]  KIND_PERF_REPLY  = 8'h07;
  localparam [7:0]  PYRO_MAGIC       = 8'h50;  // 'P'
  localparam [7:0]  PYRO_VER         = 8'h01;  // protocol version 1
  localparam [7:0]  ETH_HI           = 8'h88;  // EtherType 0x88B5 big-endian
  localparam [7:0]  ETH_LO           = 8'hB5;
  localparam [31:0] E_NOT_RESIDENT   = 32'd7;  // PYRO_E_NOT_RESIDENT (R38/R78.8)

  // Engine CSR offsets (pyro.hdl.generator §7.4 R45 harness contract).
  localparam [15:0] CSR_CTRL    = 16'h0010;    // bit0 START, bit1 RESET
  localparam [15:0] CSR_STATUS  = 16'h0014;    // bit0 BUSY, bit1 DONE, bit3 OVF
  localparam [15:0] CSR_OUT_CAP = 16'h0048;
  localparam [15:0] CSR_CYCLES_LO = 16'h0058;  // R45a perf counters (v2.3.0)
  localparam [15:0] CSR_CYCLES_HI = 16'h005C;
  localparam [15:0] CSR_BYTES_LO  = 16'h0060;
  localparam [15:0] CSR_BYTES_HI  = 16'h0064;
  localparam [31:0] CTRL_START  = 32'h0000_0001;
  localparam [31:0] CTRL_RESET  = 32'h0000_0002;

  // ---- frame buffers (RAM-inferable; a PYRO frame is <= NWORDS 512-bit beats) --
  // RX: ONE full-word write per accepted beat (single write port).  We store the
  // whole s_axis_tdata including lanes beyond tkeep; those don't-care bytes are
  // never read because every read is either a header field in beat 0 (always a
  // real >=60-byte L2-min region, R78.9) or a corpus feed bounded by the W5 clamp
  // (corpus_len <= rx_len-40, and rx_len counts only accepted/tkeep bytes), so no
  // read can reach a lane that was not on the wire.  => 24-deep 512-bit distr RAM.
  //
  // rx_words is read from exactly ONE place at run time: the variable-index corpus
  // feed in ST_FEED (rx_words[feed_addr[10:6]]).  All *header-field* reads (all at
  // the constant index beat 0) go through `hdr` below, a plain 512-bit snapshot of
  // beat 0 latched at RX.  Keeping constant-index taps off rx_words is what lets it
  // infer as a clean single-write/single-read distributed RAM: empirically, once the
  // match store also became an inferred RAM, the tool would otherwise reject rx_words
  // ("incorrect usage") for its mixed constant+variable indexing and drop it back to
  // 12288 flops -- a marginal-inference coupling that this snapshot removes so ALL of
  // rx_words/tx_words/match_mem stay RAM together.  hdr == rx_words[0] byte-for-byte
  // (both take beat 0's s_axis_tdata), so every wire byte is UNCHANGED.
  (* ram_style = "distributed" *) reg [511:0] rx_words [0:NWORDS-1];
  reg  [511:0] hdr;           // beat-0 header snapshot (== rx_words[0]) for header reads
  // TX: composed word-at-a-time into word_acc (a flat 512-bit register -> cheap
  // indexed byte inserts, NOT a memory) and flushed ONE word at a time to tx_words
  // (single write port).  The B1 combinational AXIS master reads exactly one word
  // per beat (tx_words[tx_beat]) -> single async read.  => 24-deep 512-bit distr RAM.
  (* ram_style = "distributed" *) reg [511:0] tx_words [0:NWORDS-1];
  reg  [511:0] word_acc;      // TX compose accumulator (flat reg)
  reg  [15:0] rx_len;
  reg  [15:0] tx_len;
  reg  [15:0] rx_beat;

  // ---- result capture (R47 pyro_match entries) ---------------------------
  // Captured entries live in an inferable BLOCK RAM, NOT a wall of parallel flops.
  // The prior MAXENT(61) x 192-bit m_start/m_end/m_pid/m_flg arrays cost ~11712 FFs
  // held in parallel; at that footprint the wrapper (~13709 FFs) congested the DFX
  // routing-contained pblock (two clock regions, LOCKED static DCP that cannot be
  // resized) and route_design never closed.  This one 61-deep x 192-bit entry store
  // holds the SAME 192-bit R47 payload with 0 flops.
  //
  // ram_style = "block" (NOT distributed) is deliberate: the frame buffers rx_words/
  // tx_words are already distributed (LUTRAM) RAMs, and empirically a *distributed*
  // match store competes with them for SLICEM/LUTRAM inference -- Vivado then evicts
  // rx_words back to 12288 flops, leaving the total FF count unchanged (a net-zero
  // swap).  Placing the match store in BRAM columns (RAMB) keeps it off the SLICEM
  // pool entirely, so rx_words/tx_words stay distributed RAM AND the 11712 capture
  // flops disappear -- both wins at once.  A block child costs only a few RAMB18,
  // which the pblock has spare.  (Spec sanctions this: R78.7 storage is an
  // implementation choice; the wire bytes are fixed by R47/R78.7, not the memory.)
  //
  // Block RAM read is REGISTERED (1-cycle latency) unlike the old async FF array, so
  // ST_BENT drives the read address a cycle ahead via ent_warm (see below); the
  // MATCH_REPLY wire bytes are byte-for-byte identical, only composed one cycle later
  // per entry.  ONE write port (res_wr capture pulse) + ONE registered read port
  // (ent_val_r) => a simple-dual-port BRAM.  Entry bit layout is the EXACT R47
  // little-endian-on-wire packing the parallel arrays fed into ST_BENT:
  // {flags[31:0], pattern_id[31:0], end[63:0], start[63:0]} (bits [63:0]=start,
  // [127:64]=end, [159:128]=pattern_id, [191:160]=flags).
  (* ram_style = "block" *) reg [191:0] match_mem [0:MAXENT-1];
  reg  [191:0] match_rd;     // registered BRAM read data (match_mem[build_idx])
  reg          ent_warm;     // 1 = BRAM read latency in flight; skip compose 1 cycle
  reg  [15:0]  match_count;  // # captured entries AND the BRAM write pointer
  reg          ovf;

  // ---- parsed request state (valid after RX completes) -------------------
  reg  [15:0] corpus_len;    // clamped corpus length (R78.6, W5)
  reg  [15:0] reply_cap;     // min(out_cap, MAXENT)
  reg  [15:0] feed_idx;
  reg  [15:0] build_idx;     // TX entry index being serialized (0..match_count-1)
  reg  [15:0] compose_idx;   // TX byte compose offset (frame byte position)
  reg  [4:0]  byte_in_ent;   // 0..23: byte within the current pyro_match entry

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
                   ST_TX        = 4'd10,  // drive reply beats (combinational out)
                   ST_PERF      = 4'd11;  // R78.11: read R45a counters from CSRs
  reg [3:0]  state;
  reg [1:0]  reply_kind;   // 0 = ID_REPLY, 1 = STATUS/ERROR, 2 = MATCH_REPLY, 3 = PERF_REPLY
  reg [15:0] tx_beat;

  // R78.11 PERF_REPLY scratch: the four R45a counter halves, latched in ST_PERF.
  reg [31:0] perf_cyc_lo, perf_cyc_hi, perf_byt_lo, perf_byt_hi;
  reg [2:0]  perf_idx;

  integer k;

  // W5 clamp scratch (ST_CLASSIFY, one cycle).
  reg [15:0] raw_corpus, avail_corpus, cl_tmp;

  // Blocking scratch for the RX feed read and TX compose (assigned-before-use each
  // cycle; the single tx_words write port is the lone `tx_words[...] <=` below).
  reg [15:0]  feed_addr;      // 40 + feed_idx (corpus byte offset in the frame)
  reg [7:0]   ent_byte;       // current entry byte being composed (from match_rd)
  reg [511:0] wacc;           // word_acc + this cycle's byte insert
  reg         txw_en;         // pulse: flush a completed/last word this cycle
  reg [4:0]   txw_sel;        // tx_words index to flush
  reg [511:0] txw_data;       // word data to flush

  // Accept new frames only while receiving; back-pressure otherwise (R80).
  assign s_axis_tready = (state == ST_RX);

  // Header field views of the buffered request (big-endian, R78.3).  Byte b of the
  // frame is hdr[8*b +: 8] (hdr == beat 0 == rx_words[0]); all header fields live in
  // beat 0 (offset < 64).  Reading the snapshot `hdr` (not rx_words[0]) keeps
  // constant-index taps off the rx_words RAM so it infers cleanly (see decl comment).
  wire [7:0] h_eth_hi = hdr[8*12 +: 8];
  wire [7:0] h_eth_lo = hdr[8*13 +: 8];
  wire [7:0] h_magic  = hdr[8*14 +: 8];
  wire [7:0] h_ver    = hdr[8*15 +: 8];
  wire [7:0] h_kind   = hdr[8*16 +: 8];
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
  // the presented beat is a pure function of tx_beat/tx_len/tx_words.  The reworked
  // buffering (RAM-inferable) means the whole beat is a SINGLE async word read
  // (tx_words[tx_beat]); tx_words[w] already carries R78.9 zero-pad in bytes beyond
  // tx_len (word_acc is cleared per word), so no per-byte "< tx_len ? : 0" is needed.
  reg  [15:0]  eff_len, remaining, this_bytes;
  reg  [511:0] tx_word_r;
  reg  [63:0]  tx_k;
  integer tk;
  always @(*) begin
    eff_len    = (tx_len < 16'd60) ? 16'd60 : tx_len;     // 60-byte L2 min (R78.9)
    remaining  = eff_len - (tx_beat * 16'd64);
    this_bytes = (remaining < 16'd64) ? remaining : 16'd64;
    tx_word_r  = tx_words[tx_beat[4:0]];                   // single async word read
    tx_k = 64'b0;
    for (tk = 0; tk < 64; tk = tk + 1)
      if (tk[15:0] < this_bytes) tx_k[tk] = 1'b1;          // valid-byte / pad mask
  end
  assign m_axis_tvalid = (state == ST_TX);
  assign m_axis_tdata  = tx_word_r;
  assign m_axis_tkeep  = tx_k;
  assign m_axis_tlast  = (state == ST_TX) && (remaining <= 16'd64);
  // tuser {dst,src,size}: size = full C2H frame byte length (R78.9); src/dst are
  // set/overridden by the static glue on egress (see the ID stub).
  assign m_axis_tuser  = {16'h0000, 16'h0000, eff_len};

  always @(posedge clk) begin
    if (!rstn) begin
      state         <= ST_RX;
      hdr           <= 512'b0;
      rx_len        <= 16'd0;
      rx_beat       <= 16'd0;
      tx_len        <= 16'd0;
      tx_beat       <= 16'd0;
      match_count   <= 16'd0;
      ovf           <= 1'b0;
      ent_warm      <= 1'b0;
      feed_idx      <= 16'd0;
      build_idx     <= 16'd0;
      compose_idx   <= 16'd0;
      byte_in_ent   <= 5'd0;
      word_acc      <= 512'b0;
      reply_kind    <= 2'd0;
      corpus_len    <= 16'd0;
      reply_cap     <= 16'd0;
      perf_cyc_lo   <= 32'd0;
      perf_cyc_hi   <= 32'd0;
      perf_byt_lo   <= 32'd0;
      perf_byt_hi   <= 32'd0;
      perf_idx      <= 3'd0;
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
      txw_en         = 1'b0;   // no tx_words flush unless a case arm requests one

      // Capture result-ring writes whenever the engine pulses res_wr (R47).  The
      // engine self-limits at OUT_CAP (== reply_cap) and reports OVF in its
      // status (bit3), which ST_DRAIN latches (B2); this bound keeps match_count
      // within reply_cap so the RAM write pointer never overflows MAXENT.
      // SINGLE write port to match_mem: assemble the 192-bit R47 entry in the exact
      // {flags, pattern_id, end, start} order the parallel arrays used to feed
      // ST_BENT (unchanged wire bytes) and write it at the current pointer, then
      // increment.  match_count is both the count and the write index (B2 count/OVF
      // logic is otherwise untouched).
      if (eng_res_wr && (match_count < reply_cap)) begin
        match_mem[match_count[5:0]] <=
          {eng_res_flags, eng_res_pid, eng_res_end, eng_res_start};
        match_count <= match_count + 16'd1;
      end

      // Registered read port of the match_mem BRAM: the entry addressed by the
      // current build_idx is latched into match_rd, ready one cycle later.  build_idx
      // holds steady for the 24 bytes of an entry, so match_rd is stable across the
      // whole serialization of that entry; ent_warm (below) absorbs the 1-cycle read
      // latency at each entry boundary.  Unconditional read + gated write => the tool
      // infers a simple-dual-port block RAM (no read/write address hazard: capture
      // writes happen in ST_FEED/ST_DRAIN, entry reads in ST_BENT -- disjoint).
      match_rd <= match_mem[build_idx[5:0]];

      case (state)
        // ---- receive + buffer the request frame ------------------------
        ST_RX: begin
          if (s_axis_tvalid && s_axis_tready) begin
            // ONE full-word write per beat (single write port).  Lanes beyond
            // tkeep are stored but never read (header uses beat 0 real bytes; the
            // corpus feed is bounded by the W5 rx_len clamp).  rx_len still counts
            // only accepted (tkeep) bytes so the clamp stays sound.
            if (rx_beat < NWORDS[15:0])
              rx_words[rx_beat[4:0]] <= s_axis_tdata;
            if (rx_beat == 16'd0)
              hdr <= s_axis_tdata;               // snapshot beat 0 for header reads
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
            if ({hdr[8*18 +: 8], hdr[8*19 +: 8]} != SLOT) begin
              reply_kind <= 2'd1;            // STATUS/ERROR NOT_RESIDENT (R78.8/R87)
              state <= ST_BHDR;
            end else begin
              reply_kind <= 2'd2;            // MATCH_REPLY (R78.7)
              // W5: clamp corpus_len = min(length-12, rx_len-40, MAX_FRAME-40),
              // guarding rx_len < 40, so a host-crafted `length` can never make
              // the feed read past the bytes actually buffered.
              raw_corpus   = ({hdr[8*24 +: 8], hdr[8*25 +: 8]} >= 16'd12)
                             ? ({hdr[8*24 +: 8], hdr[8*25 +: 8]} - 16'd12) : 16'd0;
              avail_corpus = (rx_len >= 16'd40) ? (rx_len - 16'd40) : 16'd0;
              cl_tmp = raw_corpus;
              if (cl_tmp > avail_corpus)     cl_tmp = avail_corpus;
              if (cl_tmp > (MAX_FRAME - 40)) cl_tmp = MAX_FRAME - 40;
              corpus_len <= cl_tmp;
              reply_cap  <= ({hdr[8*36 +: 8], hdr[8*37 +: 8]} < MAXENT[15:0])
                            ? {hdr[8*36 +: 8], hdr[8*37 +: 8]} : MAXENT[15:0];
              state <= ST_RESET_ENG;
            end
          end else if (h_kind == KIND_PERF_REQ) begin
            if ({hdr[8*18 +: 8], hdr[8*19 +: 8]} != SLOT) begin
              reply_kind <= 2'd1;            // STATUS/ERROR NOT_RESIDENT (R78.11)
              state <= ST_BHDR;
            end else begin
              reply_kind <= 2'd3;            // PERF_REPLY (R78.11)
              perf_idx   <= 3'd0;
              state <= ST_PERF;
            end
          end else begin
            state   <= ST_RX;                // unknown kind -> drop (R78.4)
            rx_beat <= 16'd0;
          end
        end

        // ---- R78.11: read the R45a counters from the engine CSR block --------
        // eng_csr_addr is a REGISTERED output and the engine's csr_rdata is a
        // combinational mux on it, so the value for the address driven in cycle
        // N is on eng_csr_rdata in cycle N+1: each cycle drives the next address
        // and latches the previous read.  Served only between scans (this
        // responder is single-threaded), so the engine is idle and the counters
        // are post-DONE stable — a coherent, non-destructive read (R45a/R78.11).
        // NOTE: the per-cycle default eng_csr_addr <= CSR_STATUS is overridden
        // by the explicit drives below for exactly the cycles that matter.
        ST_PERF: begin
          case (perf_idx)
            3'd0: eng_csr_addr <= CSR_CYCLES_LO;
            3'd1: begin eng_csr_addr <= CSR_CYCLES_HI; perf_cyc_lo <= eng_csr_rdata; end
            3'd2: begin eng_csr_addr <= CSR_BYTES_LO;  perf_cyc_hi <= eng_csr_rdata; end
            3'd3: begin eng_csr_addr <= CSR_BYTES_HI;  perf_byt_lo <= eng_csr_rdata; end
            default: begin perf_byt_hi <= eng_csr_rdata; state <= ST_BHDR; end
          endcase
          perf_idx <= perf_idx + 3'd1;
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
          // corpus base = frame offset 40; word select + 64:1 byte mux.  feed_addr
          // <= rx_len (W5 clamp), so this never reads a lane that was not on the wire.
          feed_addr    = 16'd40 + feed_idx;
          eng_in_data  <= rx_words[feed_addr[10:6]][ {feed_addr[5:0], 3'b000} +: 8 ];
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
        // Compose reply word 0 (frame bytes 0..63) into wacc, a FLAT 512-bit reg:
        // every insert is a constant part-select on one register (cheap flops), not
        // a memory write.  Single-word replies (ID/STATUS) flush word 0 to tx_words
        // here; MATCH_REPLY keeps word 0 in word_acc so ST_BENT appends entries from
        // frame offset 36 onward.  Bytes not written stay 0 => R78.9 zero-pad.
        ST_BHDR: begin
          wacc = 512'b0;
          // Ethernet: MAC swap (dst <- req src, src <- req dst), EtherType.
          for (k = 0; k < 6; k = k + 1) begin
            wacc[8*k +: 8]       = hdr[8*(6 + k) +: 8];
            wacc[8*(6 + k) +: 8] = hdr[8*k +: 8];
          end
          wacc[8*12 +: 8] = ETH_HI;
          wacc[8*13 +: 8] = ETH_LO;
          // PYRO control header (R78.3): magic ver kind flags slot seq length resv
          wacc[8*14 +: 8] = PYRO_MAGIC;
          wacc[8*15 +: 8] = PYRO_VER;
          wacc[8*16 +: 8] = (reply_kind == 2'd0) ? KIND_ID_REPLY :
                            (reply_kind == 2'd1) ? KIND_STATUS_ERR :
                            (reply_kind == 2'd3) ? KIND_PERF_REPLY : KIND_MATCH_REPLY;
          wacc[8*17 +: 8] = 8'h00;                       // flags
          wacc[8*18 +: 8] = hdr[8*18 +: 8];      // slot echo (BE)
          wacc[8*19 +: 8] = hdr[8*19 +: 8];
          wacc[8*20 +: 8] = hdr[8*20 +: 8];      // seq echo (BE)
          wacc[8*21 +: 8] = hdr[8*21 +: 8];
          wacc[8*22 +: 8] = hdr[8*22 +: 8];
          wacc[8*23 +: 8] = hdr[8*23 +: 8];
          wacc[8*26 +: 8] = 8'h00;                       // reserved
          wacc[8*27 +: 8] = 8'h00;
          if (reply_kind == 2'd0) begin
            // ID_REPLY payload (R78.5/R78.5a/R78.5b): static_shell_id | harness_version | rp_child_id
            wacc[8*24 +: 8] = 8'h00; wacc[8*25 +: 8] = 8'h0C;   // length = 12
            wacc[8*28 +: 8] = SPEC16[15:8];  wacc[8*29 +: 8] = SPEC16[7:0];
            wacc[8*30 +: 8] = BUILD16[15:8]; wacc[8*31 +: 8] = BUILD16[7:0];
            wacc[8*32 +: 8] = HARNESS_VERSION[31:24]; wacc[8*33 +: 8] = HARNESS_VERSION[23:16];
            wacc[8*34 +: 8] = HARNESS_VERSION[15:8];  wacc[8*35 +: 8] = HARNESS_VERSION[7:0];
            wacc[8*36 +: 8] = RP_CHILD_ID[31:24]; wacc[8*37 +: 8] = RP_CHILD_ID[23:16];
            wacc[8*38 +: 8] = RP_CHILD_ID[15:8];  wacc[8*39 +: 8] = RP_CHILD_ID[7:0];
            word_acc <= wacc;
            txw_en = 1'b1; txw_sel = 5'd0; txw_data = wacc;      // flush word 0
            tx_len  <= 16'd40;
            tx_beat <= 16'd0;
            state   <= ST_TX;
          end else if (reply_kind == 2'd1) begin
            // STATUS/ERROR payload (R78.8): code (4B BE) = PYRO_E_NOT_RESIDENT
            wacc[8*24 +: 8] = 8'h00; wacc[8*25 +: 8] = 8'h04;   // length = 4
            wacc[8*28 +: 8] = E_NOT_RESIDENT[31:24]; wacc[8*29 +: 8] = E_NOT_RESIDENT[23:16];
            wacc[8*30 +: 8] = E_NOT_RESIDENT[15:8];  wacc[8*31 +: 8] = E_NOT_RESIDENT[7:0];
            word_acc <= wacc;
            txw_en = 1'b1; txw_sel = 5'd0; txw_data = wacc;      // flush word 0
            tx_len  <= 16'd32;
            tx_beat <= 16'd0;
            state   <= ST_TX;
          end else if (reply_kind == 2'd3) begin
            // PERF_REPLY payload (R78.11): cycles(8 BE) | bytes(8 BE), the R45a
            // counter halves latched in ST_PERF ({HI,LO} = the 64-bit value).
            wacc[8*24 +: 8] = 8'h00; wacc[8*25 +: 8] = 8'h10;   // length = 16
            wacc[8*28 +: 8] = perf_cyc_hi[31:24]; wacc[8*29 +: 8] = perf_cyc_hi[23:16];
            wacc[8*30 +: 8] = perf_cyc_hi[15:8];  wacc[8*31 +: 8] = perf_cyc_hi[7:0];
            wacc[8*32 +: 8] = perf_cyc_lo[31:24]; wacc[8*33 +: 8] = perf_cyc_lo[23:16];
            wacc[8*34 +: 8] = perf_cyc_lo[15:8];  wacc[8*35 +: 8] = perf_cyc_lo[7:0];
            wacc[8*36 +: 8] = perf_byt_hi[31:24]; wacc[8*37 +: 8] = perf_byt_hi[23:16];
            wacc[8*38 +: 8] = perf_byt_hi[15:8];  wacc[8*39 +: 8] = perf_byt_hi[7:0];
            wacc[8*40 +: 8] = perf_byt_lo[31:24]; wacc[8*41 +: 8] = perf_byt_lo[23:16];
            wacc[8*42 +: 8] = perf_byt_lo[15:8];  wacc[8*43 +: 8] = perf_byt_lo[7:0];
            word_acc <= wacc;
            txw_en = 1'b1; txw_sel = 5'd0; txw_data = wacc;      // flush word 0
            tx_len  <= 16'd44;
            tx_beat <= 16'd0;
            state   <= ST_TX;
          end else begin
            // MATCH_REPLY payload (R78.7): count(2 BE) status(2 BE) resv(4) entries
            wacc[8*24 +: 8] = (16'd8 + (match_count * 16'd24)) >> 8;    // length hi
            wacc[8*25 +: 8] = (16'd8 + (match_count * 16'd24)) & 16'hFF; // length lo
            wacc[8*28 +: 8] = match_count[15:8]; wacc[8*29 +: 8] = match_count[7:0];
            wacc[8*30 +: 8] = 8'h00;             wacc[8*31 +: 8] = {7'd0, ovf}; // status bit0 OVF
            wacc[8*32 +: 8] = 8'h00; wacc[8*33 +: 8] = 8'h00;
            wacc[8*34 +: 8] = 8'h00; wacc[8*35 +: 8] = 8'h00;
            word_acc    <= wacc;                 // word 0 header; entries append @36
            build_idx   <= 16'd0;
            compose_idx <= 16'd36;               // first entry byte lands at offset 36
            byte_in_ent <= 5'd0;
            ent_warm    <= 1'b1;                  // warm the BRAM read for entry 0
            state <= ST_BENT;
          end
        end

        // ---- serialize pyro_match entries, one BYTE per cycle (R47, LE) -----
        // Accumulate-and-flush: insert the current entry byte into word_acc (via
        // wacc, a flat-reg part-select) at its intra-word position; when a 64-byte
        // word fills, flush it to tx_words (single write port) and clear word_acc
        // for the next word (so bytes above the composed length stay 0 => R78.9
        // pad).  On completion, flush the final partial word once.  build_idx is the
        // entry index, byte_in_ent the 0..23 byte within it (each field LE, R47:
        // match_rd = {flags, pattern_id, end, start}, so byte j = match_rd[8*j +: 8]).
        //
        // BRAM read latency: match_rd is match_mem[build_idx] delayed one cycle.
        // ent_warm is set when we ENTER ST_BENT (from ST_BHDR) and each time build_idx
        // advances; while set, we spend one cycle letting match_rd catch up to the new
        // address and compose NOTHING.  The composed bytes are therefore identical to
        // the old async-read array, just produced one cycle later per entry (ST_TX
        // reads only the finished tx_words, so the wire MATCH_REPLY is byte-for-byte
        // unchanged).
        ST_BENT: begin
          if (ent_warm) begin
            ent_warm <= 1'b0;      // match_rd now valid for build_idx; compose next cyc
          end else if (build_idx >= match_count) begin
            // composition done; flush the final partial word if one is pending
            // (compose_idx not word-aligned). match_count==0 flushes word 0 (header).
            if (compose_idx[5:0] != 6'd0) begin
              txw_en = 1'b1; txw_sel = compose_idx[10:6]; txw_data = word_acc;
            end
            tx_len  <= 16'd36 + (match_count * 16'd24);
            tx_beat <= 16'd0;
            state   <= ST_TX;
          end else begin
            // SINGLE registered read port of the match_mem BRAM: match_rd already
            // holds match_mem[build_idx] = {flags,pid,end,start} (latched last cycle).
            ent_byte = match_rd[ {byte_in_ent, 3'b000} +: 8 ];    // byte_in_ent*8
            wacc = word_acc;
            wacc[ {compose_idx[5:0], 3'b000} +: 8 ] = ent_byte;    // insert @ intra-word pos
            if (compose_idx[5:0] == 6'd63) begin
              txw_en = 1'b1; txw_sel = compose_idx[10:6]; txw_data = wacc;  // flush full word
              word_acc <= 512'b0;                                 // next word starts cleared
            end else begin
              word_acc <= wacc;
            end
            if (byte_in_ent == 5'd23) begin
              byte_in_ent <= 5'd0;
              build_idx   <= build_idx + 16'd1;
              ent_warm    <= 1'b1;    // advance BRAM read addr; absorb its 1-cyc latency
            end else begin
              byte_in_ent <= byte_in_ent + 5'd1;
            end
            compose_idx <= compose_idx + 16'd1;
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

      // SINGLE tx_words write port: the lone clocked write to the tx_words RAM.
      // Every reply path routes its flush through txw_en/txw_sel/txw_data above, so
      // synthesis infers one write port and one async read port (distributed RAM).
      if (txw_en)
        tx_words[txw_sel] <= txw_data;
    end
  end

endmodule : pyro_rp
"""


# ---------------------------------------------------------------------------
# v4 (P2e) frame-parallel top: CORES x pyro_rp_core behind a packet demux/mux.
# Pure structural composition — the proven core template is UNMODIFIED (its
# module is renamed pyro_rp_core); all new logic lives here.  Substitution
# via @CORES@ only (same .replace discipline; SV {} concatenations abound).
# ---------------------------------------------------------------------------
_MULTI_TOP = r"""

// ***************************************************************************
// pyro_rp — v4 frame-parallel top (P2e): @CORES@ x pyro_rp_core.
//
//   * Both AXIS boundaries pass through TWO-STAGE SKID BUFFERS so tready is
//     a REGISTERED signal: the first spin of this top peeked the R78 kind
//     combinationally from s_axis_tdata to compute s_axis_tready — a
//     tdata->tready arc that routed static->RP->static across two SLR
//     crossings in one cycle (RP-scoped WNS -0.821 ns).  All demux/mux
//     logic now operates on registered beats.
//   * RX demux: one AXIS packet goes to exactly one core.  The R78 kind is
//     peeked from frame byte 16 of the REGISTERED first beat:
//     MATCH_REQUEST round-robins over cores whose slave side is ready
//     (skip-busy), everything else (ID/PERF/unknown) routes to the core
//     that most recently completed a MATCH ('last_done', reset 0) so the
//     R78.11 PERF read-out keeps its "most recent scan" semantics.
//   * TX mux: packet-atomic round-robin over cores with pending replies;
//     'last_done' updates when a MATCH_REPLY's first beat is granted.
//   * While one core scans, the demux keeps accepting frames into other
//     cores — the RX/scan overlap the serialized single-core FSM never had.
// ***************************************************************************
module pyro_rp #(
  parameter [15:0] SPEC16          = 16'h0202,
  parameter [15:0] BUILD16         = `PYRO_BUILD16,
  parameter [31:0] HARNESS_VERSION = 32'h00010000,
  parameter [15:0] SLOT            = 16'd1,
  parameter [31:0] RP_CHILD_ID     = `PYRO_RP_CHILD_ID
) (
  input                clk,
  input                rstn,

  input                s_axis_tvalid,
  input        [511:0] s_axis_tdata,
  input         [63:0] s_axis_tkeep,
  input                s_axis_tlast,
  input         [47:0] s_axis_tuser,
  output               s_axis_tready,

  output               m_axis_tvalid,
  output       [511:0] m_axis_tdata,
  output        [63:0] m_axis_tkeep,
  output               m_axis_tlast,
  output        [47:0] m_axis_tuser,
  input                m_axis_tready
);

  localparam integer CORES = @CORES@;
  localparam [2:0]  C3    = CORES;        // width-safe copies for compares
  localparam [1:0]  LASTC = CORES - 1;
  localparam [7:0]  KIND_MATCH_REQ   = 8'h03;
  localparam [7:0]  KIND_MATCH_REPLY = 8'h04;

  // ---- slave-side two-stage skid: registered tready, registered beats ----
  reg          si_v0, si_v1;
  reg  [511:0] si_d0, si_d1;
  reg   [63:0] si_k0, si_k1;
  reg          si_l0, si_l1;
  reg   [47:0] si_u0, si_u1;

  wire d_tready;                              // demux consumes stage 0
  wire in_fire  = s_axis_tvalid && !si_v1;
  wire d_fire   = si_v0 && d_tready;

  assign s_axis_tready = !si_v1;              // registered occupancy only

  always @(posedge clk) begin
    if (!rstn) begin
      si_v0 <= 1'b0;
      si_v1 <= 1'b0;
    end else begin
      if (in_fire) begin
        if (si_v0 && !d_fire) begin           // stage 0 busy: park in skid
          si_v1 <= 1'b1;
          si_d1 <= s_axis_tdata;  si_k1 <= s_axis_tkeep;
          si_l1 <= s_axis_tlast;  si_u1 <= s_axis_tuser;
        end else begin                        // straight into stage 0
          si_v0 <= 1'b1;
          si_d0 <= s_axis_tdata;  si_k0 <= s_axis_tkeep;
          si_l0 <= s_axis_tlast;  si_u0 <= s_axis_tuser;
        end
      end
      if (d_fire) begin
        if (si_v1) begin                      // refill stage 0 from skid
          si_v1 <= 1'b0;
          si_d0 <= si_d1;  si_k0 <= si_k1;
          si_l0 <= si_l1;  si_u0 <= si_u1;
        end else if (!in_fire) begin
          si_v0 <= 1'b0;
        end
      end
    end
  end

  // ---- per-core AXIS wiring (flat vectors: variable +: slices) -----------
  wire [CORES-1:0]        c_s_tready;
  wire [CORES-1:0]        c_m_tvalid;
  wire [CORES*512-1:0]    c_m_tdata;
  wire [CORES*64-1:0]     c_m_tkeep;
  wire [CORES-1:0]        c_m_tlast;
  wire [CORES*48-1:0]     c_m_tuser;
  wire [CORES-1:0]        c_s_tvalid;
  wire [CORES-1:0]        c_m_tready;

  // ---- RX demux (operates on registered stage-0 beat) --------------------
  reg          rx_inpkt;    // mid-packet: target locked in rx_tgt_q
  reg  [1:0]   rx_tgt_q;
  reg  [1:0]   rr;          // next MATCH round-robin start
  reg  [1:0]   last_done;   // core of the most recent MATCH_REPLY

  wire [7:0]   rx_kind  = si_d0[135:128];    // frame byte 16 (beat 0)
  wire         is_match = (rx_kind == KIND_MATCH_REQ);

  // first ready core at/after rr (skip-busy round-robin)
  reg  [1:0]   pick_c;
  reg          pick_ok;
  integer pk;
  always @* begin
    pick_c  = 2'd0;
    pick_ok = 1'b0;
    for (pk = CORES - 1; pk >= 0; pk = pk - 1) begin : rr_pick
      reg [2:0] cand;
      cand = {1'b0, rr} + pk[2:0];
      if (cand >= C3)
        cand = cand - C3;
      if (c_s_tready[cand[1:0]]) begin
        pick_c  = cand[1:0];
        pick_ok = 1'b1;
      end
    end
  end

  wire [1:0] tgt_new  = is_match ? pick_c : last_done;
  wire       new_ok   = is_match ? pick_ok : c_s_tready[last_done];
  wire [1:0] rx_tgt   = rx_inpkt ? rx_tgt_q : tgt_new;
  assign d_tready     = rx_inpkt ? c_s_tready[rx_tgt_q] : new_ok;

  genvar gd;
  generate
    for (gd = 0; gd < CORES; gd = gd + 1) begin : g_demux
      assign c_s_tvalid[gd] = si_v0 && d_tready && (rx_tgt == gd[1:0]);
    end
  endgenerate

  always @(posedge clk) begin
    if (!rstn) begin
      rx_inpkt <= 1'b0;
      rx_tgt_q <= 2'd0;
      rr       <= 2'd0;
    end else if (d_fire) begin
      if (!rx_inpkt) begin
        rx_tgt_q <= rx_tgt;
        if (is_match)
          rr <= (rx_tgt == LASTC) ? 2'd0 : rx_tgt + 2'd1;
      end
      rx_inpkt <= !si_l0;
    end
  end

  // ---- TX mux (packet-atomic round-robin) into the master-side skid ------
  reg          tx_lock;
  reg  [1:0]   tx_sel_q;
  reg  [1:0]   rr_tx;

  reg  [1:0]   tx_pick;
  reg          tx_pick_ok;
  integer tp;
  always @* begin
    tx_pick    = 2'd0;
    tx_pick_ok = 1'b0;
    for (tp = CORES - 1; tp >= 0; tp = tp - 1) begin : tx_rr_pick
      reg [2:0] cand;
      cand = {1'b0, rr_tx} + tp[2:0];
      if (cand >= C3)
        cand = cand - C3;
      if (c_m_tvalid[cand[1:0]]) begin
        tx_pick    = cand[1:0];
        tx_pick_ok = 1'b1;
      end
    end
  end

  wire [1:0] tx_sel    = tx_lock ? tx_sel_q : tx_pick;
  wire       tx_active = tx_lock | tx_pick_ok;

  wire         mx_tvalid = tx_active && c_m_tvalid[tx_sel];
  wire [511:0] mx_tdata  = c_m_tdata[tx_sel*512 +: 512];
  wire  [63:0] mx_tkeep  = c_m_tkeep[tx_sel*64 +: 64];
  wire         mx_tlast  = c_m_tlast[tx_sel];
  wire  [47:0] mx_tuser  = c_m_tuser[tx_sel*48 +: 48];
  wire         mx_tready;                     // from the master-side skid

  genvar gm;
  generate
    for (gm = 0; gm < CORES; gm = gm + 1) begin : g_mux
      assign c_m_tready[gm] = mx_tready && tx_active && (tx_sel == gm[1:0]);
    end
  endgenerate

  always @(posedge clk) begin
    if (!rstn) begin
      tx_lock   <= 1'b0;
      tx_sel_q  <= 2'd0;
      rr_tx     <= 2'd0;
      last_done <= 2'd0;
    end else if (mx_tvalid && mx_tready) begin
      if (!tx_lock) begin
        tx_sel_q <= tx_sel;
        rr_tx    <= (tx_sel == LASTC) ? 2'd0 : tx_sel + 2'd1;
        if (mx_tdata[135:128] == KIND_MATCH_REPLY)
          last_done <= tx_sel;
      end
      tx_lock <= !mx_tlast;
    end
  end

  // ---- master-side two-stage skid: registered tready toward the cores ----
  reg          so_v0, so_v1;
  reg  [511:0] so_d0, so_d1;
  reg   [63:0] so_k0, so_k1;
  reg          so_l0, so_l1;
  reg   [47:0] so_u0, so_u1;

  wire mo_fire = so_v0 && m_axis_tready;
  wire mi_fire = mx_tvalid && !so_v1;

  assign mx_tready     = !so_v1;              // registered occupancy only
  assign m_axis_tvalid = so_v0;
  assign m_axis_tdata  = so_d0;
  assign m_axis_tkeep  = so_k0;
  assign m_axis_tlast  = so_l0;
  assign m_axis_tuser  = so_u0;

  always @(posedge clk) begin
    if (!rstn) begin
      so_v0 <= 1'b0;
      so_v1 <= 1'b0;
    end else begin
      if (mi_fire) begin
        if (so_v0 && !mo_fire) begin
          so_v1 <= 1'b1;
          so_d1 <= mx_tdata;  so_k1 <= mx_tkeep;
          so_l1 <= mx_tlast;  so_u1 <= mx_tuser;
        end else begin
          so_v0 <= 1'b1;
          so_d0 <= mx_tdata;  so_k0 <= mx_tkeep;
          so_l0 <= mx_tlast;  so_u0 <= mx_tuser;
        end
      end
      if (mo_fire) begin
        if (so_v1) begin
          so_v1 <= 1'b0;
          so_d0 <= so_d1;  so_k0 <= so_k1;
          so_l0 <= so_l1;  so_u0 <= so_u1;
        end else if (!mi_fire) begin
          so_v0 <= 1'b0;
        end
      end
    end
  end

  // ---- cores --------------------------------------------------------------
  genvar gc;
  generate
    for (gc = 0; gc < CORES; gc = gc + 1) begin : g_core
      pyro_rp_core #(
        .SPEC16          (SPEC16),
        .BUILD16         (BUILD16),
        .HARNESS_VERSION (HARNESS_VERSION),
        .SLOT            (SLOT),
        .RP_CHILD_ID     (RP_CHILD_ID)
      ) u_core (
        .clk           (clk),
        .rstn          (rstn),
        .s_axis_tvalid (c_s_tvalid[gc]),
        .s_axis_tdata  (si_d0),
        .s_axis_tkeep  (si_k0),
        .s_axis_tlast  (si_l0),
        .s_axis_tuser  (si_u0),
        .s_axis_tready (c_s_tready[gc]),
        .m_axis_tvalid (c_m_tvalid[gc]),
        .m_axis_tdata  (c_m_tdata[gc*512 +: 512]),
        .m_axis_tkeep  (c_m_tkeep[gc*64 +: 64]),
        .m_axis_tlast  (c_m_tlast[gc]),
        .m_axis_tuser  (c_m_tuser[gc*48 +: 48]),
        .m_axis_tready (c_m_tready[gc])
      );
    end
  endgenerate

endmodule : pyro_rp
"""
