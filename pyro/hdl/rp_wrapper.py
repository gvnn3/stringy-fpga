"""Pattern RP-child wrapper generator (spec §10.2, R78-R82/R87/R88; Phase 2b).

A per-pattern PYRO circuit becomes a **PARTIAL bitstream** for the
reconfigurable partition ``pyro_rp`` (R72/R82).  The DFX flow
(``script/build_pr.tcl``) does
``synth_design -top pyro_rp`` and links the result into the ``pyro_rp`` cell of
the locked static DCP (R80/R82b), so a pattern child MUST be a module named
**``pyro_rp``** with the **exact R80 boundary ports** — a drop-in
replacement for
the default ID-stub child (``pyro_id_stub.sv``).

This module emits that wrapper.  It reuses the ID stub's structural conventions
(``pyro_id_stub.sv``): byte ``k`` of a frame lives in ``tdata[8*k +: 8]``
(least-significant lane = first wire byte, the OpenNIC/Xilinx AXIS convention);
replies MAC-swap and echo ``slot``/``seq`` (R78);
``tuser = {dst,src,size}`` with
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

**v2.2.4 blessings (design choices ratified by the spec — cited, not
invented):**
  1. **R78.5a** — ``rp_child_id`` is the **low 32 bits of the R47a pattern hash,
     forced non-zero** (``0x00000001`` if the low 32 bits are 0);
     overridable but
     always non-zero.  It is host-verifiable (the host holds the same hash).
     Injected as the ``RP_CHILD_ID`` parameter.
  2. **R78.5b** — the wire ``harness_version`` (``ID_REPLY``) carries the **R45
     resident-harness contract version** (``0x00010000``, matching the ID stub),
     a DISTINCT namespace from the ``PYROART1`` artifact ``HARNESS_VERSION``
     (``0x00020100`` as of spec v2.3.0).  The child reports the wire/R45 value.
  3. **R87** — the single-tenant pattern occupies **slot 1**; children
     default to
     ``SLOT = 1``.  Multi-slot (``slot ≥ 2``) is deferred; such requests get
     ``PYRO_E_NOT_RESIDENT``.

Note (S11): a pattern child reports ``BUILD16 = 0`` in its ``ID_REPLY``
by default
(the host probe only checks ``SPEC16``, R81/R83; ``BUILD16`` is a build-injected
discriminator and is not required to equal any fixed value).  A build
MAY inject a
real ``BUILD16`` via the ``PYRO_BUILD16`` define / ``BUILD16`` parameter.
"""

from __future__ import annotations

from typing import Optional

# The engine module name the L2 generator (pyro.hdl.generator) always emits.
ENGINE_MODULE = "pyro_circuit"

# PYRO MAC contract: the P1 digest program's engine module and its static
# RTL sources under hw/rtl (hand-maintained, not generated).  Every
# build-input assembly that emits a ``mac_program`` child MUST read all
# three files alongside the engine RTL and ``pyro_rp.sv`` — the wrapper
# only *instantiates* the module; the sources ride separately, exactly
# like the overlay/ROM engines.
MAC_ENGINE_MODULE = "pyro_mac_engine_top"
MAC_ENGINE_FILES = (
    "pyro_mac_engine_top.v",
    "pyro_mac_engine.v",
    "pyro_siphash.v",
)


def mac_engine_sources(repo_root: Optional[str] = None) -> "list[str]":
    """Absolute paths of the P1 MAC engine RTL sources (hw/rtl).

    Central so the synth toolchain and the shell build drivers agree on
    the one list; a missing file is the CALLER's error to raise (the
    engine is a separate deliverable and may not exist yet in a fresh
    checkout).
    """
    import os
    if repo_root is None:
        repo_root = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
    return [os.path.join(repo_root, "hw", "rtl", n)
            for n in MAC_ENGINE_FILES]

# Wire-side constants mirrored from pyro_id_stub.sv (R78/R81/R78.5b).
DEFAULT_SPEC16 = 0x0202
# R45/R78.5b resident-harness wire version
DEFAULT_WIRE_HARNESS_VERSION = 0x0001_0000
DEFAULT_SLOT = 1                            # single-tenant resident slot (R87)

# Buffer sizing: a full PYRO frame is ≤ 1518 B (R78.9); round up to 24×64 beats.
MAX_FRAME_BYTES = 1536
# R78.9a (v2.6.0/P2c): buffer sizing on a MAX_PKT_LEN=9600 jumbo shell —
# 150×64 B beats.  Emitted only when generate_rp_child is asked for it; the
# 1536 emission is byte-identical to pre-P2c.
JUMBO_FRAME_BYTES = 9600
# R78.7 max pyro_match entries / reply
MAX_ENTRIES = 61

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
    (``0x00000001`` if the low 32 bits are 0) so it can never collide
    with the ID
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
    assert text.count(old) == 1, (
        f"wrapper template anchor missing/dup: {old[:60]!r}")
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
    # 4 since A5 §3 added the TABLE_STATUS_REPLY composer.
    t = _replace_n(t, "txw_sel = 5'd0;", "txw_sel = 8'd0;", 4)
    t = _replace_n(t, "txw_sel = compose_idx[10:6];",
                   "txw_sel = compose_idx[13:6];", 2)
    # word-selects of the corpus feed: one in the ST_FEED code (v2 byte feed
    # or v3 wide feed) + one in the rx_words declaration comment
    # 3 since A5 §3 added ST_TBL_FEED, which streams table chunks through
    # the same addressing as the corpus feed.
    t = _replace_n(t, "rx_words[feed_addr[10:6]]",
                   "rx_words[feed_addr[13:6]]", 3)
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
        "        // ---- stream corpus one byte/cycle into the engine"
        " (R48) --------\n"
        "        ST_FEED: begin\n"
        "          eng_in_valid <= 1'b1;\n"
        "          // corpus base = frame offset 40 (R78), or 0 for a"
        " raw wire\n"
        "          // frame (OQ-2: the whole frame is the subject). "
        " Word select\n"
        "          // + 64:1 byte mux.\n"
        "          // feed_addr <= rx_len (W5 clamp), so this never reads"
        " a lane\n"
        "          // that was not on the wire.\n"
        "          feed_addr    = (wire_frame ? 16'd0 : 16'd40)"
        " + feed_idx;\n"
        "          eng_in_data  <=\n"
        "              rx_words[feed_addr[10:6]]"
        "[ {feed_addr[5:0], 3'b000} +: 8 ];\n"
        "          eng_in_last  <= (feed_idx == (corpus_len - 16'd1));\n"
        "          if (feed_idx == (corpus_len - 16'd1))\n"
        "            state <= ST_DRAIN;\n"
        "          feed_idx <= feed_idx + 16'd1;\n"
        "        end",
        f"        // ---- stream corpus {n} bytes/cycle into the engine"
        f" (P2b v3) ----\n"
        f"        ST_FEED: begin\n"
        f"          eng_in_valid <= 1'b1;\n"
        f"          // corpus base = frame offset 40, or 0 for a raw wire\n"
        f"          // frame (OQ-2); both are {n}-aligned, and each"
        f" {n}-byte group\n"
        f"          // lives inside one 512-bit word, so this is one"
        f" word read + a\n"
        f"          // {n*8}-bit aligned part select.  feed_addr <="
        f" rx_len (W5 clamp).\n"
        f"          feed_addr    = (wire_frame ? 16'd0 : 16'd40)"
        f" + feed_idx;\n"
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
        "  // ---- combinational AXIS master output (B1, S10)"
        " ------------------------",
        f"  // kept-lane mask for the final (short) corpus beat (P2b v3).\n"
        f"  function automatic [{n-1}:0] kmask(input [15:0] r);\n"
        f"    integer km;\n"
        f"    begin\n"
        f"      kmask = {{{n}{{1'b0}}}};\n"
        f"      for (km = 0; km < {n}; km = km + 1)\n"
        f"        if (km[15:0] < r) kmask[km] = 1'b1;\n"
        f"    end\n"
        f"  endfunction\n\n"
        f"  // ---- combinational AXIS master output (B1, S10)"
        f" ------------------------")
    return t


def _engine_backpressure(t: str) -> str:
    """Honor the engine's ``in_ready`` in ST_FEED (SNORT-PF SR7 group engine).

    The single-pattern engine consumes a byte every cycle unconditionally, so
    the v2/v3 feed never had to look at anything.  A GROUP engine cannot: N
    slots can accept on one byte and the R47 result ring writes one entry per
    cycle, so the engine stalls the feed (``in_ready`` low) while it drains
    pending accepts.

    **The hazard this rewrite exists for.**  The ungated ST_FEED asserts
    ``eng_in_valid`` and advances ``feed_idx`` in the SAME cycle, so by the
    time ``in_ready`` can be observed the next beat is already committed and
    ``feed_idx`` has moved past it.  Simply gating here would therefore DROP
    that in-flight beat — a missed byte, hence a missed match, with nothing on
    the wire to show for it.  Gating is only safe because the group engine
    accepts and HOLDS that beat in a 1-deep skid buffer (see HAZARD (a) in
    ``pyro.hdl.generator._emit_rtl_group``); this side is the *second* half of
    that contract, and the two must stay together.  A wrapper emitted without
    this rewrite MUST NOT be paired with a group engine.

    Emission is unchanged (byte-identical) unless asked for, so the flashed
    single-pattern child and every cached artifact are untouched (R47b).
    """
    t = _replace1(
        t,
        "  wire        eng_res_wr;",
        "  wire        eng_in_ready;   // SR7 group engine backpressure\n"
        "  wire        eng_res_wr;")
    t = _replace1(
        t,
        "    .in_last        (eng_in_last),",
        "    .in_last        (eng_in_last),\n"
        "    .in_ready       (eng_in_ready),")
    head = "        ST_FEED: begin\n"
    tail = "\n        end\n\n        // ---- wait for DONE"
    assert t.count(head) == 1 and t.count(tail) == 1, (
        "wrapper template ST_FEED anchors drifted; backpressure rewrite unsafe")
    i0 = t.index(head) + len(head)
    i1 = t.index(tail)
    body = "\n".join(("  " + ln) if ln.strip() else ln
                     for ln in t[i0:i1].split("\n"))
    gated = (
        "          // SR7: the group engine stalls the feed while its result\n"
        "          // ring drains.  Freezing the WHOLE body (valid,"
        " keep, last,\n"
        "          // feed_idx and the ST_DRAIN hand-off) is what keeps the\n"
        "          // clamp and the last-byte detection consistent; the beat\n"
        "          // already in flight when in_ready fell is held by the\n"
        "          // engine's skid buffer, not re-sent from here.\n"
        "          if (eng_in_ready) begin\n" + body + "\n          end")
    return t[:i0] + gated + t[i1:]


#: The wire the ``engine_backpressure`` rewrite introduces.
_WRAPPER_IN_READY = "eng_in_ready"


def _declares_in_ready(engine_rtl: str) -> bool:
    """Does the engine RTL declare an ``in_ready`` output port?

    Plain scanning, no ``re``: this module is imported lazily from the synth
    path and a module-level ``re.compile`` here would capture the *patched*
    compiler while ``pyro.install()`` is active (spurious R66 reuse-counter
    ticks — the same reasoning as :mod:`pyro.hdl.identity`).
    """
    for line in (engine_rtl or "").splitlines():
        code = line.split("//", 1)[0].strip()
        if code.startswith("output") and "in_ready" in code:
            return True
    return False


def _decl_width_bits(rtl: str, signal: str, starts) -> Optional[int]:
    """Declared width in bits of ``signal``, or ``None`` if not declared.

    Plain scanning, no ``re``, for :func:`_declares_in_ready`'s reason.
    A bare declaration (``input wire in_last``) is 1 bit; ``[63:0]`` is 64.
    """
    for line in (rtl or "").splitlines():
        code = line.split("//", 1)[0].strip()
        if not code.startswith(starts):
            continue
        toks = code.rstrip(",;").replace("[", " [").replace("]", "] ").split()
        if not toks or toks[-1] != signal:
            continue
        for tok in toks:
            if tok.startswith("[") and tok.endswith("]") and ":" in tok:
                hi, lo = tok[1:-1].split(":", 1)
                try:
                    return int(hi) - int(lo) + 1
                except ValueError:
                    return None
        return 1
    return None


def _engine_feed_shape(engine_rtl: str):
    """``(in_data bits, has in_keep)`` of a generated engine, or ``None``."""
    bits = _decl_width_bits(engine_rtl, "in_data", ("input",))
    if bits is None:
        return None
    return bits, _decl_width_bits(engine_rtl, "in_keep", ("input",)) is not None


def _wrapper_feed_shape(wrapper_rtl: str):
    """``(eng_in_data bits, has eng_in_keep)`` of a ``pyro_rp`` wrapper."""
    bits = _decl_width_bits(wrapper_rtl, "eng_in_data",
                            ("reg", "wire", "logic"))
    if bits is None:
        return None
    return bits, _decl_width_bits(
        wrapper_rtl, "eng_in_keep", ("reg", "wire", "logic")) is not None


def check_engine_pairing(engine_rtl: str, wrapper_rtl: str) -> None:
    """Fail loud when an engine is paired with a wrapper that mis-drives it.

    Neither side can detect a bad pairing alone (the engine cannot see the
    wrapper, the wrapper cannot see the engine), so the check lives where both
    texts exist: the build path that writes ``design.v`` and ``pyro_rp.sv``
    into one Vivado workdir.  Two axes, both of which fail SILENTLY in the
    dangerous direction — a clean build, a well-formed reply, zero matches:

    **1. Backpressure.**  An unconnected ``.in_ready`` output is perfectly
    legal Verilog: a group engine wired into an ungated ``pyro_rp``
    elaborates, synthesizes, meets timing, and then drops a byte on every
    stall.

    **2. Datapath width.**  ``generate_rp_child(datapath_bytes=)`` and the
    engine's width are two independent inputs (the synth path takes the
    wrapper's from ``SynthJob.datapath_bytes`` while the engine's is baked
    into ``SynthJob.rtl``), and Verilog will happily connect an 8-bit
    ``eng_in_data`` to a 64-bit ``in_data`` — zero-extending — and leave the
    engine's ``in_keep`` **unconnected, tied low**.  Measured under xsim
    (``tests/hw/xsim_diff.py``, dpb=8 group engine + dpb=1 wrapper): xelab and
    xsim complete without a warning that matters, every ``MATCH_REPLY`` comes
    back ``count=0`` and ``PERF`` reports ``BYTES=0``, because ``fed_keep==0``
    makes every ``accv[j]`` zero and ``byte_index`` never advances.  That is
    the maximal false negative for a completeness-absolute circuit (SR3).
    The opposite mismatch (narrow engine, wide wrapper) fails loud at
    elaboration (``VRFC 10-3180: cannot find port in_keep``), so the axis is
    checked here precisely because the harmful direction is the quiet one.

    Raises :class:`ValueError` on either dangerous pairing; silent otherwise.
    The single-pattern engine declares no ``in_ready``, so the historical
    pairing is unaffected (and stays byte-identical, R47b).
    """
    # -- axis 2: datapath width (applies to EVERY engine, group or not) -----
    eng = _engine_feed_shape(engine_rtl)
    wrap = _wrapper_feed_shape(wrapper_rtl)
    if eng is not None and wrap is not None:
        eng_bits, eng_keep = eng
        wrap_bits, wrap_keep = wrap
        if eng_bits != wrap_bits or eng_keep != wrap_keep:
            raise ValueError(
                "engine/wrapper datapath width mismatch: engine in_data is "
                "%d bits (in_keep %s) but the pyro_rp wrapper drives %d bits "
                "(eng_in_keep %s) — generate_rp_child(..., datapath_bytes=%d) "
                "is REQUIRED; a wide engine on a narrow wrapper elaborates "
                "with in_keep tied low and matches NOTHING, silently"
                % (eng_bits, "present" if eng_keep else "absent",
                   wrap_bits, "present" if wrap_keep else "absent",
                   max(1, eng_bits // 8)))

    # -- axis 1: backpressure ---------------------------------------------
    if not _declares_in_ready(engine_rtl):
        return                                   # single-pattern engine (v2/v3)
    if _WRAPPER_IN_READY not in (wrapper_rtl or ""):
        raise ValueError(
            "engine declares in_ready (SR7 group engine) but the pyro_rp "
            "wrapper ignores it — generate_rp_child(..., "
            "engine_backpressure=True) is REQUIRED for a group child; an "
            "ungated wrapper drops bytes on every stall, silently")


def generate_rp_child(pattern_hash_hex: str, *, slot: int = DEFAULT_SLOT,
                      rp_child_id: Optional[int] = None, build16: int = 0,
                      spec16: int = DEFAULT_SPEC16,
                      wire_harness_version: int = DEFAULT_WIRE_HARNESS_VERSION,
                      datapath_bytes: int = 1,
                      max_frame_bytes: int = MAX_FRAME_BYTES,
                      cores: int = 1,
                      engine_backpressure: bool = False,
                      mac_program: bool = False,
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

    ``engine_backpressure`` (SNORT-PF SR7): connect and honor the engine's
    ``in_ready``.  REQUIRED for a group engine
    (:func:`pyro.hdl.generator.generate_group`, whose result ring can need
    several cycles per byte) and MUST stay off for a single-pattern engine,
    which has no such port — see :func:`_engine_backpressure`.  Default off, so
    the emission stays byte-identical for every existing artifact.

    ``mac_program`` (PYRO MAC contract): emit a MULTI-PROGRAM child — the
    proven single-engine child becomes program P0 (module renamed
    ``pyro_rp_core``, text otherwise untouched, composing with every knob
    above) and a new P1 SipHash-2-4 MAC digest program plus a scheduling
    dispatcher top are appended (``_MAC_PROG_TOP``).  The P1 engine
    (``pyro_mac_engine_top``) is a SEPARATE source — build assemblies must
    read :data:`MAC_ENGINE_FILES` alongside.  Default off: every existing
    invocation stays byte-identical.  Mutually exclusive with ``cores > 1``
    (the v4 top and the program dispatcher both claim the boundary).
    """
    if mac_program and cores != 1:
        raise ValueError(
            f"mac_program requires cores == 1 (the program dispatcher and "
            f"the v4 multi-core top both claim the R80 boundary), got "
            f"cores={cores!r}")
    if max_frame_bytes not in (MAX_FRAME_BYTES, JUMBO_FRAME_BYTES):
        raise ValueError(
            f"max_frame_bytes must be {MAX_FRAME_BYTES} or "
            f"{JUMBO_FRAME_BYTES}, "
            f"got {max_frame_bytes!r} (R78.9a)")
    if rp_child_id is None:
        rp_child_id = rp_child_id_from_hash(pattern_hash_hex)
    rp_child_id &= 0xFFFFFFFF
    if rp_child_id == 0:
        # R78.5a: a loaded pattern MUST report non-zero
        rp_child_id = 0x00000001
    slot &= 0xFFFF
    build16 &= 0xFFFF
    spec16 &= 0xFFFF
    wire_harness_version &= 0xFFFFFFFF

    template = (_TEMPLATE if datapath_bytes == 1
                else _widen_template(datapath_bytes))
    if max_frame_bytes != MAX_FRAME_BYTES:
        template = _jumbo_slices(template)
    if engine_backpressure:
        template = _engine_backpressure(template)
    child = (template
             .replace("@ENGINE@", ENGINE_MODULE)
             .replace("@MAXFRAME@", str(max_frame_bytes))
             .replace("@MAXENT@", str(MAX_ENTRIES))
             .replace("@SPEC16@", f"16'h{spec16:04X}")
             .replace("@BUILD16@", f"16'h{build16:04X}")
             .replace("@HARNESSVER@", f"32'h{wire_harness_version:08X}")
             .replace("@SLOT@", f"16'd{slot}")
             .replace("@RPCHILDID@", f"32'h{rp_child_id:08X}"))
    if mac_program:
        # PYRO MAC: same structural-composition discipline as v4 — the
        # proven child is RENAMED, never edited; all new logic lives in
        # the appended top.  The P1 program's slices are emitted
        # jumbo-safe from the start, so it needs no _jumbo_slices pass.
        core_text = _replace1(child, "module pyro_rp #(",
                              "module pyro_rp_core #(")
        core_text = _replace1(core_text, "endmodule : pyro_rp",
                              "endmodule : pyro_rp_core")
        mac_top = (_MAC_PROG_TOP
                   .replace("@MAXFRAME@", str(max_frame_bytes))
                   .replace("@SPEC16@", f"16'h{spec16:04X}")
                   .replace("@HARNESSVER@",
                            f"32'h{wire_harness_version:08X}")
                   .replace("@SLOT@", f"16'd{slot}"))
        return core_text + mac_top
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
_TEMPLATE = r"""// *************************************************************
// PYRO Phase 2b - per-pattern reconfigurable-partition child `pyro_rp`.
//
// AUTO-GENERATED by pyro.hdl.rp_wrapper - do not edit.
//
// Drop-in replacement for the default ID-stub child with the SAME frozen R80
// boundary ports so it links against the same locked static DCP (R82b).  Parses
// PYRO control frames in-RP (R79), answers ID_REQUEST with a non-zero
// rp_child_id
// (R78.5a, "loaded pattern"), serves MATCH_REQUEST for its SLOT by streaming
// the
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
  // R81 spec 2.2 => 0x0202
  parameter [15:0] SPEC16          = @SPEC16@,
  // R81 build discriminator (default 0, S11)
  parameter [15:0] BUILD16         = `PYRO_BUILD16,
  // R78.5b wire/R45 harness version
  parameter [31:0] HARNESS_VERSION = @HARNESSVER@,
  // R87 this child's slot (1)
  parameter [15:0] SLOT            = @SLOT@,
  // R78.5a non-zero = loaded
  parameter [31:0] RP_CHILD_ID     = `PYRO_RP_CHILD_ID
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
  // bytes buffered (>= 1518, R78.9)
  localparam integer MAX_FRAME = @MAXFRAME@;
  // 512-bit beats (24 = MAX_FRAME/64)
  localparam integer NWORDS    = @MAXFRAME@/64;
  // max pyro_match entries / reply (R78.7)
  localparam integer MAXENT    = @MAXENT@;

  localparam [7:0]  KIND_ID_REQ      = 8'h01;
  localparam [7:0]  KIND_ID_REPLY    = 8'h02;
  localparam [7:0]  KIND_MATCH_REQ   = 8'h03;
  localparam [7:0]  KIND_MATCH_REPLY = 8'h04;
  localparam [7:0]  KIND_STATUS_ERR  = 8'h05;
  localparam [7:0]  KIND_PERF_REQ    = 8'h06;  // R45a read-out (R78.11, v2.4.0)
  localparam [7:0]  KIND_PERF_REPLY  = 8'h07;
  // A5 §3: overlay table load.  Additive kinds; a device predating the
  // amendment drops them as unknown (R78.4), which is the correct behaviour.
  localparam [7:0]  KIND_TBL_BEGIN    = 8'h08;
  localparam [7:0]  KIND_TBL_DATA     = 8'h09;
  localparam [7:0]  KIND_TBL_COMMIT   = 8'h0A;
  localparam [7:0]  KIND_TBL_STAT_REQ = 8'h0B;
  localparam [7:0]  KIND_TBL_STAT_REP = 8'h0C;
  localparam [7:0]  KIND_TBL_ABORT    = 8'h0D;
  localparam [7:0]  PYRO_MAGIC       = 8'h50;  // 'P'
  localparam [7:0]  PYRO_VER         = 8'h01;  // protocol version 1
  localparam [7:0]  ETH_HI           = 8'h88;  // EtherType 0x88B5 big-endian
  localparam [7:0]  ETH_LO           = 8'hB5;
  // PYRO_E_NOT_RESIDENT (R38/R78.8)
  localparam [31:0] E_NOT_RESIDENT   = 32'd7;

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

  // A5 §3.1 overlay table CSRs.  NOTE a divergence from the amendment as
  // written: §3.1 lists 0x007C as TABLE_BYTES_HI, but the engine implements
  // TBL_CTRL there and carries a single 32-bit byte counter at 0x0078.  A
  // 32-bit counter is sufficient (the largest table is ~2.1 MB) and the load
  // needs a control register, so the engine's map is the useful one; §3.1
  // should be amended to match rather than the RTL changed to match it.
  localparam [15:0] CSR_TBL_ACTIVE = 16'h0068;
  localparam [15:0] CSR_TBL_SHADOW = 16'h006C;
  localparam [15:0] CSR_TBL_EPOCH  = 16'h0070;
  localparam [15:0] CSR_TBL_STATUS = 16'h0074;
  localparam [15:0] CSR_TBL_BYTES  = 16'h0078;
  localparam [15:0] CSR_TBL_CTRL   = 16'h007C;
  localparam [15:0] CSR_TBL_CAPS   = 16'h0080;
  localparam [15:0] CSR_TBL_EXPECT = 16'h0084;   // host CRC (A5 §5)
  // TBL_CTRL bits: 0 LOAD, 1 COMMIT, 2 ABORT.  BEGIN writes LOAD|ABORT in one
  // go: the engine applies the abort (clearing wr_addr/bytes_rcvd/CRC seed)
  // and latches load_mode in the same cycle, which is exactly "open a fresh
  // transfer".
  localparam [31:0] TBL_OPEN    = 32'h0000_0005;   // LOAD | ABORT
  localparam [31:0] TBL_COMMIT  = 32'h0000_0002;
  localparam [31:0] TBL_ABORT   = 32'h0000_0004;
  localparam [31:0] E_TBL_SEQ   = 32'd8;   // chunk offset != engine bytes_rcvd

  // ---- frame buffers (RAM-inferable; a PYRO frame is <= NWORDS 512-bit
  // beats) --
  // RX: ONE full-word write per accepted beat (single write port).  We store
  // the
  // whole s_axis_tdata including lanes beyond tkeep; those don't-care bytes are
  // never read because every read is either a header field in beat 0 (always a
  // real >=60-byte L2-min region, R78.9) or a corpus feed bounded by the W5
  // clamp
  // (corpus_len <= rx_len-40, and rx_len counts only accepted/tkeep bytes),
  // so no
  // read can reach a lane that was not on the wire.  => 24-deep 512-bit distr
  // RAM.
  //
  // rx_words is read from exactly ONE place at run time: the variable-index
  // corpus
  // feed in ST_FEED (rx_words[feed_addr[10:6]]).  All *header-field* reads
  // (all at
  // the constant index beat 0) go through `hdr` below, a plain 512-bit
  // snapshot of
  // beat 0 latched at RX.  Keeping constant-index taps off rx_words is what
  // lets it
  // infer as a clean single-write/single-read distributed RAM: empirically,
  // once the
  // match store also became an inferred RAM, the tool would otherwise reject
  // rx_words
  // ("incorrect usage") for its mixed constant+variable indexing and drop it
  // back to
  // 12288 flops -- a marginal-inference coupling that this snapshot removes
  // so ALL of
  // rx_words/tx_words/match_mem stay RAM together.  hdr == rx_words[0] byte-
  // for-byte
  // (both take beat 0's s_axis_tdata), so every wire byte is UNCHANGED.
  (* ram_style = "distributed" *) reg [511:0] rx_words [0:NWORDS-1];
  // beat-0 header snapshot (== rx_words[0]) for header reads
  reg  [511:0] hdr;
  // TX: composed word-at-a-time into word_acc (a flat 512-bit register -> cheap
  // indexed byte inserts, NOT a memory) and flushed ONE word at a time to
  // tx_words
  // (single write port).  The B1 combinational AXIS master reads exactly one
  // word
  // per beat (tx_words[tx_beat]) -> single async read.  => 24-deep 512-bit
  // distr RAM.
  (* ram_style = "distributed" *) reg [511:0] tx_words [0:NWORDS-1];
  reg  [511:0] word_acc;      // TX compose accumulator (flat reg)
  reg  [15:0] rx_len;
  reg  [15:0] tx_len;
  reg  [15:0] rx_beat;

  // ---- result capture (R47 pyro_match entries) ---------------------------
  // Captured entries live in an inferable BLOCK RAM, NOT a wall of parallel
  // flops.
  // The prior MAXENT(61) x 192-bit m_start/m_end/m_pid/m_flg arrays cost
  // ~11712 FFs
  // held in parallel; at that footprint the wrapper (~13709 FFs) congested
  // the DFX
  // routing-contained pblock (two clock regions, LOCKED static DCP that
  // cannot be
  // resized) and route_design never closed.  This one 61-deep x 192-bit entry
  // store
  // holds the SAME 192-bit R47 payload with 0 flops.
  //
  // ram_style = "block" (NOT distributed) is deliberate: the frame buffers
  // rx_words/
  // tx_words are already distributed (LUTRAM) RAMs, and empirically a
  // *distributed*
  // match store competes with them for SLICEM/LUTRAM inference -- Vivado then
  // evicts
  // rx_words back to 12288 flops, leaving the total FF count unchanged (a
  // net-zero
  // swap).  Placing the match store in BRAM columns (RAMB) keeps it off the
  // SLICEM
  // pool entirely, so rx_words/tx_words stay distributed RAM AND the 11712
  // capture
  // flops disappear -- both wins at once.  A block child costs only a few
  // RAMB18,
  // which the pblock has spare.  (Spec sanctions this: R78.7 storage is an
  // implementation choice; the wire bytes are fixed by R47/R78.7, not the
  // memory.)
  //
  // Block RAM read is REGISTERED (1-cycle latency) unlike the old async FF
  // array, so
  // ST_BENT drives the read address a cycle ahead via ent_warm (see below); the
  // MATCH_REPLY wire bytes are byte-for-byte identical, only composed one
  // cycle later
  // per entry.  ONE write port (res_wr capture pulse) + ONE registered read
  // port
  // (ent_val_r) => a simple-dual-port BRAM.  Entry bit layout is the EXACT R47
  // little-endian-on-wire packing the parallel arrays fed into ST_BENT:
  // {flags[31:0], pattern_id[31:0], end[63:0], start[63:0]} (bits [63:0]=start,
  // [127:64]=end, [159:128]=pattern_id, [191:160]=flags).
  (* ram_style = "block" *) reg [191:0] match_mem [0:MAXENT-1];
  reg  [191:0] match_rd;     // registered BRAM read data (match_mem[build_idx])
  // 1 = BRAM read latency in flight; skip compose 1 cycle
  reg          ent_warm;
  reg  [15:0]  match_count;  // # captured entries AND the BRAM write pointer
  reg          ovf;

  // ---- parsed request state (valid after RX completes) -------------------
  reg  [15:0] corpus_len;    // clamped corpus length (R78.6, W5)
  reg  [15:0] reply_cap;     // min(out_cap, MAXENT)
  reg  [15:0] feed_idx;
  // TX entry index being serialized (0..match_count-1)
  reg  [15:0] build_idx;
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
                   // drive reply beats (combinational out)
                   ST_TX        = 4'd10,
                   // R78.11: read R45a counters from CSRs
                   ST_PERF      = 4'd11,
                   ST_TBL_OPEN  = 4'd12,  // A5 §3: TBL_CTRL <- LOAD|ABORT
                   // A5 §3: check chunk offset vs bytes_rcvd
                   ST_TBL_SEQ   = 4'd13,
                   // A5 §3: stream chunk bytes into the engine
                   ST_TBL_FEED  = 4'd14,
                   ST_TBL_STAT  = 4'd15;  // A5 §3: read the six table CSRs
  reg [3:0]  state;
  reg [2:0]  reply_kind;   // 0 = ID_REPLY, 1 = STATUS/ERROR, 2 = MATCH_REPLY,
                           // 3 = PERF_REPLY, 4 = TABLE_STATUS_REPLY
  // A5 §3 table-load working set.
  reg [7:0]  tbl_kind;     // which table op is in flight
  reg [3:0]  tbl_idx;      // CSR read-out sequencer (2 cycles per register)
  reg [31:0] tbl_active, tbl_shadow, tbl_epoch, tbl_status, tbl_bytes, tbl_caps;
  reg [31:0] tbl_err;      // 0 = ok, else an error the wrapper itself raised
  reg [15:0] tx_beat;

  // OQ-2 wire-scan (docs/studies/wire-rate-spike.md).  A frame whose
  // tuser src has bit 6 set (0x0040 = CMAC-0 via the 250 MHz adapter)
  // is raw wire traffic, not R78: scan its bytes against the active
  // table and reply (MATCH_REPLY, payload status bit2 = wire) ONLY
  // when it nominates.  Inert on the tied-off shell: QDMA H2C carries
  // src 0x0001, so wire_frame never sets.
  reg        wire_frame;   // current frame is wire-tagged (beat-0 latch)
  reg        load_open_w;  // a table load is open; wire scans must drop
  reg        boot_stat;    // one silent ST_TBL_STAT sweep out of reset
  reg [31:0] wire_seq;     // seq stamped into wire MATCH_REPLYs
  reg [31:0] wire_seen, wire_scanned, wire_drops, wire_noms;

  // R78.11 PERF_REPLY scratch: the four R45a counter halves, latched in
  // ST_PERF.
  reg [31:0] perf_cyc_lo, perf_cyc_hi, perf_byt_lo, perf_byt_hi;
  reg [3:0]  perf_idx;

  integer k;

  // W5 clamp scratch (ST_CLASSIFY, one cycle).
  reg [15:0] raw_corpus, avail_corpus, cl_tmp;

  // Blocking scratch for the RX feed read and TX compose (assigned-before-use
  // each
  // cycle; the single tx_words write port is the lone `tx_words[...] <=`
  // below).
  reg [15:0]  feed_addr;      // 40 + feed_idx (corpus byte offset in the frame)
  // current entry byte being composed (from match_rd)
  reg [7:0]   ent_byte;
  reg [511:0] wacc;           // word_acc + this cycle's byte insert
  reg         txw_en;         // pulse: flush a completed/last word this cycle
  reg [4:0]   txw_sel;        // tx_words index to flush
  reg [511:0] txw_data;       // word data to flush

  // Accept new frames only while receiving; back-pressure otherwise (R80).
  assign s_axis_tready = (state == ST_RX);

  // Header field views of the buffered request (big-endian, R78.3).  Byte b
  // of the
  // frame is hdr[8*b +: 8] (hdr == beat 0 == rx_words[0]); all header fields
  // live in
  // beat 0 (offset < 64).  Reading the snapshot `hdr` (not rx_words[0]) keeps
  // constant-index taps off the rx_words RAM so it infers cleanly (see decl
  // comment).
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
  // the presented beat is a pure function of tx_beat/tx_len/tx_words.  The
  // reworked
  // buffering (RAM-inferable) means the whole beat is a SINGLE async word read
  // (tx_words[tx_beat]); tx_words[w] already carries R78.9 zero-pad in bytes
  // beyond
  // tx_len (word_acc is cleared per word), so no per-byte "< tx_len ? : 0" is
  // needed.
  reg  [15:0]  eff_len, remaining, this_bytes;
  reg  [511:0] tx_word_r;
  reg  [63:0]  tx_k;
  integer tk;
  always @(*) begin
    // 60-byte L2 min (R78.9)
    eff_len    = (tx_len < 16'd60) ? 16'd60 : tx_len;
    remaining  = eff_len - (tx_beat * 16'd64);
    this_bytes = (remaining < 16'd64) ? remaining : 16'd64;
    // single async word read
    tx_word_r  = tx_words[tx_beat[4:0]];
    tx_k = 64'b0;
    for (tk = 0; tk < 64; tk = tk + 1)
      // valid-byte / pad mask
      if (tk[15:0] < this_bytes) tx_k[tk] = 1'b1;
  end
  assign m_axis_tvalid = (state == ST_TX);
  assign m_axis_tdata  = tx_word_r;
  assign m_axis_tkeep  = tx_k;
  assign m_axis_tlast  = (state == ST_TX) && (remaining <= 16'd64);
  // tuser {dst,src,size}: size = full C2H frame byte length (R78.9); src/dst
  // are
  // set/overridden by the static glue on egress (see the ID stub).
  assign m_axis_tuser  = {16'h0000, 16'h0000, eff_len};

  always @(posedge clk) begin
    if (!rstn) begin
      // Boot with one SILENT table-CSR sweep (ST_TBL_STAT with boot_stat
      // set) before accepting frames.  For a loadable engine every CSR
      // reads 0 out of reset, so this changes nothing.  For a ROM child
      // (S4) the table identity is BAKED and valid from configuration —
      // without this sweep the wrapper's tbl_epoch stays at its reset 0
      // until some host sends a TABLE_* frame, and until then the wire
      // gate below drops every frame and MATCH_REPLY attributes epoch 0.
      // Found by inspection while integrating pyro_ac_rom_engine, before
      // it could be found on silicon.
      state         <= ST_TBL_STAT;
      boot_stat     <= 1'b1;
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
      reply_kind    <= 3'd0;
      corpus_len    <= 16'd0;
      reply_cap     <= 16'd0;
      perf_cyc_lo   <= 32'd0;
      perf_cyc_hi   <= 32'd0;
      perf_byt_lo   <= 32'd0;
      perf_byt_hi   <= 32'd0;
      perf_idx      <= 4'd0;
      // A5 §3: epoch 0 is the "no valid table" encoding, matching the
      // engine's own post-reset state -- never stale content reading as good.
      tbl_kind      <= 8'd0;
      tbl_idx       <= 4'd0;
      tbl_active    <= 32'd0;
      tbl_shadow    <= 32'd0;
      tbl_epoch     <= 32'd0;
      tbl_status    <= 32'd0;
      tbl_bytes     <= 32'd0;
      tbl_caps      <= 32'd0;
      tbl_err       <= 32'd0;
      wire_frame    <= 1'b0;
      load_open_w   <= 1'b0;
      wire_seq      <= 32'd0;
      wire_seen     <= 32'd0;
      wire_scanned  <= 32'd0;
      wire_drops    <= 32'd0;
      wire_noms     <= 32'd0;
      eng_csr_write <= 1'b0;
      eng_csr_addr  <= CSR_STATUS;
      eng_csr_wdata <= 32'b0;
      eng_in_valid  <= 1'b0;
      eng_in_data   <= 8'b0;
      eng_in_last   <= 1'b0;
    end else begin
      // defaults each cycle (pulsed controls; default CSR read = STATUS so
      // BUSY/
      // DONE/OVF polling is always valid between writes).
      eng_csr_write <= 1'b0;
      eng_csr_addr  <= CSR_STATUS;
      eng_in_valid  <= 1'b0;
      eng_in_last   <= 1'b0;
      // no tx_words flush unless a case arm requests one
      txw_en         = 1'b0;

      // Capture result-ring writes whenever the engine pulses res_wr (R47).
      // The
      // engine self-limits at OUT_CAP (== reply_cap) and reports OVF in its
      // status (bit3), which ST_DRAIN latches (B2); this bound keeps
      // match_count
      // within reply_cap so the RAM write pointer never overflows MAXENT.
      // SINGLE write port to match_mem: assemble the 192-bit R47 entry in the
      // exact
      // {flags, pattern_id, end, start} order the parallel arrays used to feed
      // ST_BENT (unchanged wire bytes) and write it at the current pointer,
      // then
      // increment.  match_count is both the count and the write index (B2
      // count/OVF
      // logic is otherwise untouched).
      if (eng_res_wr && (match_count < reply_cap)) begin
        match_mem[match_count[5:0]] <=
          {eng_res_flags, eng_res_pid, eng_res_end, eng_res_start};
        match_count <= match_count + 16'd1;
      end

      // Registered read port of the match_mem BRAM: the entry addressed by the
      // current build_idx is latched into match_rd, ready one cycle later.
      // build_idx
      // holds steady for the 24 bytes of an entry, so match_rd is stable
      // across the
      // whole serialization of that entry; ent_warm (below) absorbs the
      // 1-cycle read
      // latency at each entry boundary.  Unconditional read + gated write =>
      // the tool
      // infers a simple-dual-port block RAM (no read/write address hazard:
      // capture
      // writes happen in ST_FEED/ST_DRAIN, entry reads in ST_BENT -- disjoint).
      match_rd <= match_mem[build_idx[5:0]];

      case (state)
        // ---- receive + buffer the request frame ------------------------
        ST_RX: begin
          if (s_axis_tvalid && s_axis_tready) begin
            // ONE full-word write per beat (single write port).  Lanes beyond
            // tkeep are stored but never read (header uses beat 0 real bytes;
            // the
            // corpus feed is bounded by the W5 rx_len clamp).  rx_len still
            // counts
            // only accepted (tkeep) bytes so the clamp stays sound.
            if (rx_beat < NWORDS[15:0])
              rx_words[rx_beat[4:0]] <= s_axis_tdata;
            if (rx_beat == 16'd0) begin
              // snapshot beat 0 for header reads
              hdr <= s_axis_tdata;
              // OQ-2: wire-origin latch (tuser src bit 6, adapter RX)
              wire_frame <= s_axis_tuser[22];
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
          if (wire_frame) begin
            // ---- OQ-2 wire-scan --------------------------------------
            // Raw wire frame: scan ALL its bytes (L2 headers included --
            // over-nomination is benign, SR5) against the active table.
            // Drop, counted, when no committed table exists or a load
            // is open (feeding the engine then would corrupt the
            // shadow).  Reply only on nominations: at line rate the
            // reply path carries signal, never per-frame chatter.
            wire_seen <= wire_seen + 32'd1;
            if (load_open_w || tbl_epoch == 32'd0
                || rx_len == 16'd0) begin
              wire_drops <= wire_drops + 32'd1;
              state   <= ST_RX;
              rx_beat <= 16'd0;
            end else begin
              wire_scanned <= wire_scanned + 32'd1;
              reply_kind   <= 3'd2;          // MATCH_REPLY (wire-flagged)
              cl_tmp = rx_len;
              if (cl_tmp > MAX_FRAME) cl_tmp = MAX_FRAME;
              corpus_len <= cl_tmp;
              reply_cap  <= MAXENT[15:0];
              state <= ST_RESET_ENG;
            end
          end else if (!is_pyro) begin
            state   <= ST_RX;                // not a PYRO frame -> drop (R78.1)
            rx_beat <= 16'd0;
          end else if (h_kind == KIND_ID_REQ) begin
            reply_kind <= 3'd0;              // ID_REPLY
            state <= ST_BHDR;
          end else if (h_kind == KIND_MATCH_REQ) begin
            if ({hdr[8*18 +: 8], hdr[8*19 +: 8]} != SLOT) begin
              // STATUS/ERROR NOT_RESIDENT (R78.8/R87)
              reply_kind <= 3'd1;
              state <= ST_BHDR;
            end else begin
              reply_kind <= 3'd2;            // MATCH_REPLY (R78.7)
              // W5: clamp corpus_len = min(length-12, rx_len-40, MAX_FRAME-40),
              // guarding rx_len < 40, so a host-crafted `length` can never make
              // the feed read past the bytes actually buffered.
              raw_corpus   = ({hdr[8*24 +: 8], hdr[8*25 +: 8]} >= 16'd12)
                  ? ({hdr[8*24 +: 8], hdr[8*25 +: 8]} - 16'd12)
                  : 16'd0;
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
              // STATUS/ERROR NOT_RESIDENT (R78.11)
              reply_kind <= 3'd1;
              state <= ST_BHDR;
            end else begin
              reply_kind <= 3'd3;            // PERF_REPLY (R78.11)
              perf_idx   <= 4'd0;
              state <= ST_PERF;
            end
          end else if (h_kind == KIND_TBL_BEGIN || h_kind == KIND_TBL_DATA ||
                       h_kind == KIND_TBL_COMMIT || h_kind == KIND_TBL_ABORT ||
                       h_kind == KIND_TBL_STAT_REQ) begin
            // ---- A5 §3 overlay table load ------------------------------
            if ({hdr[8*18 +: 8], hdr[8*19 +: 8]} != SLOT) begin
              reply_kind <= 3'd1;            // STATUS/ERROR NOT_RESIDENT
              state <= ST_BHDR;
            end else begin
              // Every table op answers with TABLE_STATUS_REPLY.  That is the
              // whole observability surface (§3), so the host never has to
              // infer what happened -- it reads active/shadow/epoch/bytes
              // back after each step and can compare against what it sent.
              reply_kind <= 3'd4;
              tbl_idx    <= 4'd0;
              tbl_err    <= 32'd0;
              // TABLE_DATA payload = u64 offset, u32 length, bytes[] at 40.
              // Same clamp discipline as the corpus feed (W5): a host-crafted
              // length can never make the feed read past the wire bytes.
              raw_corpus   = ({hdr[8*24 +: 8], hdr[8*25 +: 8]} >= 16'd12)
                  ? ({hdr[8*24 +: 8], hdr[8*25 +: 8]} - 16'd12)
                  : 16'd0;
              avail_corpus = (rx_len >= 16'd40) ? (rx_len - 16'd40) : 16'd0;
              cl_tmp = raw_corpus;
              if (cl_tmp > avail_corpus)     cl_tmp = avail_corpus;
              if (cl_tmp > (MAX_FRAME - 40)) cl_tmp = MAX_FRAME - 40;
              corpus_len <= cl_tmp;
              tbl_kind   <= h_kind;
              if (h_kind == KIND_TBL_BEGIN) begin
                state <= ST_TBL_OPEN;
              end else if (h_kind == KIND_TBL_DATA) begin
                // Read bytes_rcvd so the chunk's offset can be checked
                // against it before a single byte is written.
                eng_csr_addr <= CSR_TBL_BYTES;
                state <= ST_TBL_SEQ;
              end else begin
                state <= ST_TBL_OPEN;        // COMMIT / ABORT / STATUS_REQ
              end
            end
          end else begin
            state   <= ST_RX;                // unknown kind -> drop (R78.4)
            rx_beat <= 16'd0;
          end
        end

        // ---- A5 §3: one CSR write, then report status ------------------
        // BEGIN/COMMIT/ABORT are each a single TBL_CTRL write; STATUS_REQ is
        // no write at all.  All four then fall through to ST_TBL_STAT, so the
        // reply always describes the state *after* the operation.
        // eng_csr_addr must be driven EXACTLY once here.  Driving it a second
        // time to pre-start the status read would silently retarget the WRITE
        // (non-blocking: last assignment wins), sending TBL_OPEN to the
        // read-only TABLE_ACTIVE register instead of TBL_CTRL -- load_mode
        // would never assert and every subsequent byte would be dropped.
        // ST_TBL_STAT issues its own first address instead.
        ST_TBL_OPEN: begin
          // TABLE_BEGIN takes TWO cycles here: the host's declared CRC must
          // reach the engine BEFORE the load opens, or the commit gate has
          // nothing to compare against.  Staged on tbl_idx rather than a new
          // FSM state because `state` is exactly 4 bits wide and full.
          //
          // TABLE_BEGIN payload (A5 §3) starts at frame offset 28:
          //   version 28  engine_id 32  total_bytes 36  expected_crc 44
          if (tbl_kind == KIND_TBL_BEGIN && tbl_idx == 4'd0) begin
            eng_csr_write <= 1'b1;
            eng_csr_addr  <= CSR_TBL_EXPECT;
            eng_csr_wdata <= {hdr[8*44 +: 8], hdr[8*45 +: 8],
                              hdr[8*46 +: 8], hdr[8*47 +: 8]};
            tbl_idx <= 4'd1;
          end else begin
            if (tbl_kind != KIND_TBL_STAT_REQ) begin
              eng_csr_write <= 1'b1;
              eng_csr_addr  <= CSR_TBL_CTRL;
              eng_csr_wdata <= (tbl_kind == KIND_TBL_BEGIN)  ? TBL_OPEN
                             : (tbl_kind == KIND_TBL_COMMIT) ? TBL_COMMIT
                                                             : TBL_ABORT;
              // OQ-2: wire scans are refused while a load is open --
              // ST_FEED bytes would land in the shadow table.
              load_open_w <= (tbl_kind == KIND_TBL_BEGIN);
            end
            tbl_idx <= 4'd0;
            state <= ST_TBL_STAT;
          end
        end

        // ---- A5 §3: sequential-delivery check --------------------------
        // The engine accumulates CRC-32C over bytes AS THEY ARRIVE and
        // appends at its own wr_addr; it does not honour the chunk offset.
        // So out-of-order or duplicated delivery would corrupt the shadow
        // silently.  Rather than let that happen, reject any chunk whose
        // offset is not exactly the bytes the engine has already taken.
        // The transfer stays fail-closed: a rejected chunk leaves the
        // shadow untouched and the host restarts from TABLE_BEGIN.
        //
        // This is a deliberate narrowing of §3, which advertises
        // offset-addressed idempotent writes.  Honouring that needs the CRC
        // computed over the assembled image at commit instead of streamed,
        // which is a later change; until then the device must not pretend
        // to a property it does not have.
        // Two spacer cycles before the compare.  The overlay engine REGISTERS
        // csr_rdata, so the value for an address driven in cycle N appears in
        // N+2, not N+1.  (ST_PERF above assumes N+1, which is right for the
        // generated engine's combinational read but wrong for this one -- see
        // the note on ST_PERF.)  Reading a cycle early here would compare the
        // chunk offset against a stale register and reject valid chunks.
        ST_TBL_SEQ: begin
          eng_csr_addr <= CSR_TBL_BYTES;
          if (tbl_idx < 4'd2) begin
            tbl_idx <= tbl_idx + 4'd1;
          end else begin
            tbl_idx <= 4'd0;
            if ({hdr[8*32 +: 8], hdr[8*33 +: 8], hdr[8*34 +: 8], hdr[8*35 +: 8]}
                != eng_csr_rdata) begin
              tbl_err <= E_TBL_SEQ;
              state <= ST_TBL_STAT;
            end else if (corpus_len == 16'd0) begin
              state <= ST_TBL_STAT;
            end else begin
              feed_idx <= 16'd0;
              state    <= ST_TBL_FEED;
            end
          end
        end

        // ---- A5 §3: stream the chunk into the engine -------------------
        // Byte-per-cycle, same addressing as ST_FEED.  load_mode is already
        // set (TABLE_BEGIN did it), so in_ready is unconditionally high and
        // the engine consumes every beat.  in_last is NOT asserted: a chunk
        // is a fragment of one image, not a complete unit, and in_last would
        // be meaningless during a load.
        ST_TBL_FEED: begin
          eng_in_valid <= 1'b1;
          feed_addr    = 16'd40 + feed_idx;
          eng_in_data  <=
              rx_words[feed_addr[10:6]][ {feed_addr[5:0], 3'b000} +: 8 ];
          eng_in_last  <= 1'b0;
          if (feed_idx == (corpus_len - 16'd1)) begin
            tbl_idx <= 4'd0;
            state <= ST_TBL_STAT;
          end
          feed_idx <= feed_idx + 16'd1;
        end

        // ---- A5 §3: read the six table CSRs for the reply --------------
        // TWO cycles per register, because this engine registers csr_rdata:
        // an address driven in cycle N is sampled by the engine during N+1
        // and its data is readable in N+2.  The odd indices are the spacers
        // that make that latency explicit rather than assumed.  The per-cycle
        // default (eng_csr_addr <= CSR_STATUS) reclaims the bus on the spacer,
        // which is harmless -- the engine already sampled the address it
        // needed on the cycle before.
        ST_TBL_STAT: begin
          eng_in_valid <= 1'b0;
          case (tbl_idx)
            4'd0:  eng_csr_addr <= CSR_TBL_ACTIVE;
            4'd2:  begin tbl_active <= eng_csr_rdata;
              eng_csr_addr <= CSR_TBL_SHADOW;
              end
            4'd4:  begin tbl_shadow <= eng_csr_rdata;
              eng_csr_addr <= CSR_TBL_EPOCH;
              end
            4'd6:  begin tbl_epoch  <= eng_csr_rdata;
              eng_csr_addr <= CSR_TBL_STATUS;
              end
            4'd8:  begin tbl_status <= eng_csr_rdata;
              eng_csr_addr <= CSR_TBL_BYTES;
              end
            4'd10: begin tbl_bytes  <= eng_csr_rdata;
              eng_csr_addr <= CSR_TBL_CAPS;
              end
            4'd12: begin tbl_caps   <= eng_csr_rdata;
              // Boot sweep ends silently; a frame-triggered sweep
              // builds the TABLE_STATUS_REPLY as before.
              state     <= boot_stat ? ST_RX : ST_BHDR;
              boot_stat <= 1'b0;
              end
            default: ;   // spacer: let the registered read land
          endcase
          tbl_idx <= tbl_idx + 4'd1;
        end

        // ---- R78.11: read the R45a counters from the engine CSR block
        // --------
        // eng_csr_addr is a REGISTERED output and the engine's csr_rdata is a
        // combinational mux on it, so the value for the address driven in cycle
        // N is on eng_csr_rdata in cycle N+1: each cycle drives the next
        // address
        // and latches the previous read.  Served only between scans (this
        // responder is single-threaded), so the engine is idle and the counters
        // are post-DONE stable — a coherent, non-destructive read
        // (R45a/R78.11).
        // NOTE: the per-cycle default eng_csr_addr <= CSR_STATUS is overridden
        // by the explicit drives below for exactly the cycles that matter.
        // TWO cycles per counter, address HELD across both (2026-07-31).
        // The generated engines answer a CSR address combinationally (data
        // one cycle after the address registers); the A5 overlay engine
        // REGISTERS csr_rdata, so its data lands one cycle later still.
        // Holding the address for both cycles and latching on the second
        // is correct for either: a combinational engine repeats the same
        // stable value, a registered one has just delivered it.  The old
        // one-cycle sequence read the overlay child's counters a cycle
        // early -- constant garbage that decoded plausibly, found by the
        // telemetry audit rather than by any testbench.
        ST_PERF: begin
          case (perf_idx)
            4'd0: eng_csr_addr <= CSR_CYCLES_LO;
            4'd1: eng_csr_addr <= CSR_CYCLES_LO;   // hold: data lands
            4'd2: begin eng_csr_addr <= CSR_CYCLES_HI;
              perf_cyc_lo <= eng_csr_rdata;
              end
            4'd3: eng_csr_addr <= CSR_CYCLES_HI;
            4'd4: begin eng_csr_addr <= CSR_BYTES_LO;
              perf_cyc_hi <= eng_csr_rdata;
              end
            4'd5: eng_csr_addr <= CSR_BYTES_LO;
            4'd6: begin eng_csr_addr <= CSR_BYTES_HI;
              perf_byt_lo <= eng_csr_rdata;
              end
            4'd7: eng_csr_addr <= CSR_BYTES_HI;
            default: begin perf_byt_hi <= eng_csr_rdata; state <= ST_BHDR; end
          endcase
          perf_idx <= perf_idx + 4'd1;
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
          // corpus base = frame offset 40 (R78), or 0 for a raw wire
          // frame (OQ-2: the whole frame is the subject).  Word select
          // + 64:1 byte mux.
          // feed_addr <= rx_len (W5 clamp), so this never reads a lane
          // that was not on the wire.
          feed_addr    = (wire_frame ? 16'd0 : 16'd40) + feed_idx;
          eng_in_data  <=
              rx_words[feed_addr[10:6]][ {feed_addr[5:0], 3'b000} +: 8 ];
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
          if (eng_csr_rdata[1]) begin          // engine DONE (R48 status bit1)
            if (wire_frame && match_count == 16'd0) begin
              // OQ-2: a clean wire frame produces NO reply -- only
              // nominations travel to the host.
              state   <= ST_RX;
              rx_beat <= 16'd0;
            end else begin
              if (wire_frame)
                wire_noms <= wire_noms + {16'd0, match_count};
              state <= ST_BHDR;
            end
          end
        end

        // ---- build the reply header + payload framing ------------------
        // Compose reply word 0 (frame bytes 0..63) into wacc, a FLAT 512-bit
        // reg:
        // every insert is a constant part-select on one register (cheap
        // flops), not
        // a memory write.  Single-word replies (ID/STATUS) flush word 0 to
        // tx_words
        // here; MATCH_REPLY keeps word 0 in word_acc so ST_BENT appends
        // entries from
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
          // PYRO control header (R78.3): magic ver kind flags slot seq length
          // resv
          wacc[8*14 +: 8] = PYRO_MAGIC;
          wacc[8*15 +: 8] = PYRO_VER;
          wacc[8*16 +: 8] = (reply_kind == 3'd0) ? KIND_ID_REPLY :
                            (reply_kind == 3'd1) ? KIND_STATUS_ERR :
                            (reply_kind == 3'd3) ? KIND_PERF_REPLY :
                            (reply_kind == 3'd4) ? KIND_TBL_STAT_REP :
                                                   KIND_MATCH_REPLY;
          // OQ-2: a wire-origin MATCH_REPLY cannot echo the raw
          // frame's bytes -- slot is this child's own, seq is the
          // wire-scan counter, and the payload status word bit2
          // marks wire origin.  Header flags stay 0: R78.3 forbids
          // nonzero flags in version 1 and the host decoder rejects
          // them.
          wacc[8*17 +: 8] = 8'h00;                       // flags
          wacc[8*18 +: 8] = wire_frame ? SLOT[15:8]      // slot (BE)
                                       : hdr[8*18 +: 8];
          wacc[8*19 +: 8] = wire_frame ? SLOT[7:0]
                                       : hdr[8*19 +: 8];
          wacc[8*20 +: 8] = wire_frame ? wire_seq[31:24] // seq (BE)
                                       : hdr[8*20 +: 8];
          wacc[8*21 +: 8] = wire_frame ? wire_seq[23:16]
                                       : hdr[8*21 +: 8];
          wacc[8*22 +: 8] = wire_frame ? wire_seq[15:8]
                                       : hdr[8*22 +: 8];
          wacc[8*23 +: 8] = wire_frame ? wire_seq[7:0]
                                       : hdr[8*23 +: 8];
          if (wire_frame)
            wire_seq <= wire_seq + 32'd1;
          wacc[8*26 +: 8] = 8'h00;                       // reserved
          wacc[8*27 +: 8] = 8'h00;
          if (reply_kind == 3'd4) begin
            // TABLE_STATUS_REPLY payload (A5 §3), 40 B, all big-endian:
            //   active_id shadow_id epoch status_flags
            //   bytes_received(as u64) engine_caps error
            // The error word is the wrapper's own (sequence violation), 0 when
            // the wrapper is happy; engine-side refusals show up as the
            // status flags plus an unchanged active_id, which is what a
            // fail-closed commit looks like from outside.
            wacc[8*24 +: 8] = 8'h00; wacc[8*25 +: 8] = 8'h20;   // length = 32
            wacc[8*28 +: 8] = tbl_active[31:24];
              wacc[8*29 +: 8] = tbl_active[23:16];
            wacc[8*30 +: 8] = tbl_active[15:8];
              wacc[8*31 +: 8] = tbl_active[7:0];
            wacc[8*32 +: 8] = tbl_shadow[31:24];
              wacc[8*33 +: 8] = tbl_shadow[23:16];
            wacc[8*34 +: 8] = tbl_shadow[15:8];
              wacc[8*35 +: 8] = tbl_shadow[7:0];
            wacc[8*36 +: 8] = tbl_epoch[31:24];
              wacc[8*37 +: 8] = tbl_epoch[23:16];
            wacc[8*38 +: 8] = tbl_epoch[15:8];
              wacc[8*39 +: 8] = tbl_epoch[7:0];
            wacc[8*40 +: 8] = tbl_status[31:24];
              wacc[8*41 +: 8] = tbl_status[23:16];
            wacc[8*42 +: 8] = tbl_status[15:8];
              wacc[8*43 +: 8] = tbl_status[7:0];
            // bytes hi (u64)
            wacc[8*44 +: 8] = 8'h00; wacc[8*45 +: 8] = 8'h00;
            wacc[8*46 +: 8] = 8'h00; wacc[8*47 +: 8] = 8'h00;
            wacc[8*48 +: 8] = tbl_bytes[31:24];
              wacc[8*49 +: 8] = tbl_bytes[23:16];
            wacc[8*50 +: 8] = tbl_bytes[15:8];
              wacc[8*51 +: 8] = tbl_bytes[7:0];
            wacc[8*52 +: 8] = tbl_caps[31:24];
              wacc[8*53 +: 8] = tbl_caps[23:16];
            wacc[8*54 +: 8] = tbl_caps[15:8];
              wacc[8*55 +: 8] = tbl_caps[7:0];
            wacc[8*56 +: 8] = tbl_err[31:24];
              wacc[8*57 +: 8] = tbl_err[23:16];
            wacc[8*58 +: 8] = tbl_err[15:8];     wacc[8*59 +: 8] = tbl_err[7:0];
            word_acc <= wacc;
            txw_en = 1'b1; txw_sel = 5'd0; txw_data = wacc;      // flush word 0
            tx_len  <= 16'd60;   // 14 eth + 14 pyro + 32 payload
            tx_beat <= 16'd0;
            state   <= ST_TX;
          end else if (reply_kind == 3'd0) begin
            // ID_REPLY payload (R78.5/R78.5a/R78.5b): static_shell_id |
            // harness_version | rp_child_id
            wacc[8*24 +: 8] = 8'h00; wacc[8*25 +: 8] = 8'h0C;   // length = 12
            wacc[8*28 +: 8] = SPEC16[15:8];  wacc[8*29 +: 8] = SPEC16[7:0];
            wacc[8*30 +: 8] = BUILD16[15:8]; wacc[8*31 +: 8] = BUILD16[7:0];
            wacc[8*32 +: 8] = HARNESS_VERSION[31:24];
              wacc[8*33 +: 8] = HARNESS_VERSION[23:16];
            wacc[8*34 +: 8] = HARNESS_VERSION[15:8];
              wacc[8*35 +: 8] = HARNESS_VERSION[7:0];
            wacc[8*36 +: 8] = RP_CHILD_ID[31:24];
              wacc[8*37 +: 8] = RP_CHILD_ID[23:16];
            wacc[8*38 +: 8] = RP_CHILD_ID[15:8];
              wacc[8*39 +: 8] = RP_CHILD_ID[7:0];
            word_acc <= wacc;
            txw_en = 1'b1; txw_sel = 5'd0; txw_data = wacc;      // flush word 0
            tx_len  <= 16'd40;
            tx_beat <= 16'd0;
            state   <= ST_TX;
          end else if (reply_kind == 2'd1) begin
            // STATUS/ERROR payload (R78.8): code (4B BE) = PYRO_E_NOT_RESIDENT
            wacc[8*24 +: 8] = 8'h00; wacc[8*25 +: 8] = 8'h04;   // length = 4
            wacc[8*28 +: 8] = E_NOT_RESIDENT[31:24];
              wacc[8*29 +: 8] = E_NOT_RESIDENT[23:16];
            wacc[8*30 +: 8] = E_NOT_RESIDENT[15:8];
              wacc[8*31 +: 8] = E_NOT_RESIDENT[7:0];
            word_acc <= wacc;
            txw_en = 1'b1; txw_sel = 5'd0; txw_data = wacc;      // flush word 0
            tx_len  <= 16'd32;
            tx_beat <= 16'd0;
            state   <= ST_TX;
          end else if (reply_kind == 2'd3) begin
            // PERF_REPLY payload (R78.11): cycles(8 BE) | bytes(8 BE), the R45a
            // counter halves latched in ST_PERF ({HI,LO} = the 64-bit value).
            wacc[8*24 +: 8] = 8'h00; wacc[8*25 +: 8] = 8'h10;   // length = 16
            wacc[8*28 +: 8] = perf_cyc_hi[31:24];
              wacc[8*29 +: 8] = perf_cyc_hi[23:16];
            wacc[8*30 +: 8] = perf_cyc_hi[15:8];
              wacc[8*31 +: 8] = perf_cyc_hi[7:0];
            wacc[8*32 +: 8] = perf_cyc_lo[31:24];
              wacc[8*33 +: 8] = perf_cyc_lo[23:16];
            wacc[8*34 +: 8] = perf_cyc_lo[15:8];
              wacc[8*35 +: 8] = perf_cyc_lo[7:0];
            wacc[8*36 +: 8] = perf_byt_hi[31:24];
              wacc[8*37 +: 8] = perf_byt_hi[23:16];
            wacc[8*38 +: 8] = perf_byt_hi[15:8];
              wacc[8*39 +: 8] = perf_byt_hi[7:0];
            wacc[8*40 +: 8] = perf_byt_lo[31:24];
              wacc[8*41 +: 8] = perf_byt_lo[23:16];
            wacc[8*42 +: 8] = perf_byt_lo[15:8];
              wacc[8*43 +: 8] = perf_byt_lo[7:0];
            // OQ-2 additive extension (payload bytes 16-31): the wire
            // counters seen/scanned/drops/noms, each u32 BE.  Length
            // 16 -> 32; pre-wire hosts read the first 16 bytes only.
            wacc[8*25 +: 8] = 8'h20;             // length = 32
            wacc[8*44 +: 8] = wire_seen[31:24];
              wacc[8*45 +: 8] = wire_seen[23:16];
            wacc[8*46 +: 8] = wire_seen[15:8];
              wacc[8*47 +: 8] = wire_seen[7:0];
            wacc[8*48 +: 8] = wire_scanned[31:24];
              wacc[8*49 +: 8] = wire_scanned[23:16];
            wacc[8*50 +: 8] = wire_scanned[15:8];
              wacc[8*51 +: 8] = wire_scanned[7:0];
            wacc[8*52 +: 8] = wire_drops[31:24];
              wacc[8*53 +: 8] = wire_drops[23:16];
            wacc[8*54 +: 8] = wire_drops[15:8];
              wacc[8*55 +: 8] = wire_drops[7:0];
            wacc[8*56 +: 8] = wire_noms[31:24];
              wacc[8*57 +: 8] = wire_noms[23:16];
            wacc[8*58 +: 8] = wire_noms[15:8];
              wacc[8*59 +: 8] = wire_noms[7:0];
            word_acc <= wacc;
            txw_en = 1'b1; txw_sel = 5'd0; txw_data = wacc;      // flush word 0
            tx_len  <= 16'd60;
            tx_beat <= 16'd0;
            state   <= ST_TX;
          end else begin
            // MATCH_REPLY payload (R78.7): count(2 BE) status(2 BE) resv(4)
            // entries
            // length hi
            wacc[8*24 +: 8] = (16'd8 + (match_count * 16'd24)) >> 8;
            // length lo
            wacc[8*25 +: 8] = (16'd8 + (match_count * 16'd24)) & 16'hFF;
            wacc[8*28 +: 8] = match_count[15:8];
              wacc[8*29 +: 8] = match_count[7:0];
            // status bit0 OVF, bit2 wire-origin (OQ-2, additive).
            // NOT bit1: R78.7 defines bit1 as ERR, so a wire marker
            // there would read as an errored reply to a compliant
            // host (the same trap as the header-flags draft).
            wacc[8*30 +: 8] = 8'h00;
            wacc[8*31 +: 8] = {5'd0, wire_frame, 1'b0, ovf};
            // A5 §3: payload bytes 4-7 (frame 32-35) were reserved and now
            // carry the EPOCH of the table that produced these matches --
            // the SR14' attribution gate.  Additive: a pre-A5 device wrote
            // zero here, which the host reads as "no epoch, overlay unused",
            // and that is also what this reports before the first table load
            // because tbl_epoch resets to 0.
            //
            // tbl_epoch is latched in ST_TBL_STAT rather than re-read per
            // scan, which is exact rather than approximate: the epoch changes
            // only on a TBL_CTRL commit, every commit goes through
            // ST_TBL_STAT, and this responder handles one frame at a time --
            // so no commit can slip between the latch and a scan.  Re-reading
            // it here would need a spare CSR cycle on the match path and
            // would return the same value.
            wacc[8*32 +: 8] = tbl_epoch[31:24];
              wacc[8*33 +: 8] = tbl_epoch[23:16];
            wacc[8*34 +: 8] = tbl_epoch[15:8];
              wacc[8*35 +: 8] = tbl_epoch[7:0];
            // word 0 header; entries append @36
            word_acc    <= wacc;
            build_idx   <= 16'd0;
            // first entry byte lands at offset 36
            compose_idx <= 16'd36;
            byte_in_ent <= 5'd0;
            // warm the BRAM read for entry 0
            ent_warm    <= 1'b1;
            state <= ST_BENT;
          end
        end

        // ---- serialize pyro_match entries, one BYTE per cycle (R47, LE) -----
        // Accumulate-and-flush: insert the current entry byte into word_acc
        // (via
        // wacc, a flat-reg part-select) at its intra-word position; when a
        // 64-byte
        // word fills, flush it to tx_words (single write port) and clear
        // word_acc
        // for the next word (so bytes above the composed length stay 0 => R78.9
        // pad).  On completion, flush the final partial word once.  build_idx
        // is the
        // entry index, byte_in_ent the 0..23 byte within it (each field LE,
        // R47:
        // match_rd = {flags, pattern_id, end, start}, so byte j =
        // match_rd[8*j +: 8]).
        //
        // BRAM read latency: match_rd is match_mem[build_idx] delayed one
        // cycle.
        // ent_warm is set when we ENTER ST_BENT (from ST_BHDR) and each time
        // build_idx
        // advances; while set, we spend one cycle letting match_rd catch up
        // to the new
        // address and compose NOTHING.  The composed bytes are therefore
        // identical to
        // the old async-read array, just produced one cycle later per entry
        // (ST_TX
        // reads only the finished tx_words, so the wire MATCH_REPLY is byte-
        // for-byte
        // unchanged).
        ST_BENT: begin
          if (ent_warm) begin
            // match_rd now valid for build_idx; compose next cyc
            ent_warm <= 1'b0;
          end else if (build_idx >= match_count) begin
            // composition done; flush the final partial word if one is pending
            // (compose_idx not word-aligned). match_count==0 flushes word 0
            // (header).
            if (compose_idx[5:0] != 6'd0) begin
              txw_en = 1'b1; txw_sel = compose_idx[10:6]; txw_data = word_acc;
            end
            tx_len  <= 16'd36 + (match_count * 16'd24);
            tx_beat <= 16'd0;
            state   <= ST_TX;
          end else begin
            // SINGLE registered read port of the match_mem BRAM: match_rd
            // already
            // holds match_mem[build_idx] = {flags,pid,end,start} (latched
            // last cycle).
            // byte_in_ent*8
            ent_byte = match_rd[ {byte_in_ent, 3'b000} +: 8 ];
            wacc = word_acc;
            // insert @ intra-word pos
            wacc[ {compose_idx[5:0], 3'b000} +: 8 ] = ent_byte;
            if (compose_idx[5:0] == 6'd63) begin
              // flush full word
              txw_en = 1'b1; txw_sel = compose_idx[10:6]; txw_data = wacc;
              // next word starts cleared
              word_acc <= 512'b0;
            end else begin
              word_acc <= wacc;
            end
            if (byte_in_ent == 5'd23) begin
              byte_in_ent <= 5'd0;
              build_idx   <= build_idx + 16'd1;
              // advance BRAM read addr; absorb its 1-cyc latency
              ent_warm    <= 1'b1;
            end else begin
              byte_in_ent <= byte_in_ent + 5'd1;
            end
            compose_idx <= compose_idx + 16'd1;
          end
        end

        // ---- drive reply beats (combinational outputs; register handshake) --
        // m_axis_tvalid is high throughout ST_TX; the current beat is presented
        // combinationally from tx_beat.  Advance on each accepted beat; drop
        // out
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
      // Every reply path routes its flush through txw_en/txw_sel/txw_data
      // above, so
      // synthesis infers one write port and one async read port (distributed
      // RAM).
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


# ---------------------------------------------------------------------------
# PYRO MAC multi-program top: P0 (pyro_rp_core, the proven child renamed) +
# P1 (pyro_rp_mac_prog, SipHash-2-4 digest program) behind a wire-frame
# scheduler.  Pure structural composition — the core template is UNMODIFIED.
# Substitution via @SENTINELS@ only (same .replace discipline; SV {}
# concatenations abound).  Slices in P1 are emitted jumbo-safe ([13:6] word
# selects, [7:0] beat indices), so the SAME text serves 1536 and 9600.
# ---------------------------------------------------------------------------
_MAC_PROG_TOP = r"""

// ***************************************************************************
// pyro_rp_mac_prog — P1: SipHash-2-4 per-packet MAC digest program.
//
// Engine: pyro_mac_engine_top (pyro_circuit ABI; SEPARATE source files —
// hw/rtl/pyro_mac_engine_top.v + pyro_mac_engine.v + pyro_siphash.v).
// One res_wr pulse per DIGESTED frame: res_start = digest[63:0],
// res_end[15:0] = pkt_len, res_pattern_id = record flags, res_flags =
// key_id.  Skipped frames (non-IP, no committed key) pulse nothing; the
// engine owns the SEEN/DIGESTED/SKIP_* counters and their zero-slack
// invariant.
//
// Structure: THREE decoupled processes around a 64-record FIFO.
//   * frame FSM (lockstep, one frame at a time, like every R78 responder):
//     feeds scheduled wire frames into the engine byte-serially and
//     services the three host MAC kinds (KEY_LOAD/SCHED_SET/STAT_REQ),
//     each answered by a single-beat reply;
//   * record FIFO + flush timer: res_wr capture; on overflow the RECORD
//     is dropped and counted (records_lost) — digesting continues, the
//     wire path is NEVER stalled by C2H backpressure (contract);
//   * report FSM: drains the FIFO into unsolicited MAC_REPORT frames
//     (flush at 64 pending or on the 2^18-cycle timer with >= 1 pending;
//     a key commit also forces a flush, so no report's records span two
//     keys and the head key_id names every record's key — MR15).
//
// CSR timing: the MAC engine REGISTERS csr_rdata (N+2, the overlay
// engine's convention), so every read here holds its address TWO cycles
// and latches on the second — the ST_PERF discipline (2026-07-31).
// ***************************************************************************
module pyro_rp_mac_prog #(
  parameter [15:0] SLOT = @SLOT@
) (
  input                clk,
  input                rstn,

  // demux-fed slave: P1-scheduled wire frames + host MAC control frames
  input                s_axis_tvalid,
  input        [511:0] s_axis_tdata,
  input         [63:0] s_axis_tkeep,
  input                s_axis_tlast,
  input         [47:0] s_axis_tuser,
  output               s_axis_tready,

  // master: MAC_REPORT frames + single-beat codec replies
  output               m_axis_tvalid,
  output       [511:0] m_axis_tdata,
  output        [63:0] m_axis_tkeep,
  output               m_axis_tlast,
  output        [47:0] m_axis_tuser,
  input                m_axis_tready,

  // committed schedule, consumed by the top-level wire dispatcher
  output reg     [1:0] sched_mode,
  output reg    [15:0] sched_quantum
);

  localparam integer MAX_FRAME = @MAXFRAME@;
  localparam integer NWORDS    = @MAXFRAME@/64;

  localparam [7:0]  KIND_MAC_REPORT   = 8'h0E;
  localparam [7:0]  KIND_MAC_KEY_LOAD = 8'h0F;
  localparam [7:0]  KIND_MAC_KEY_ACK  = 8'h10;
  localparam [7:0]  KIND_SCHED_SET    = 8'h11;
  localparam [7:0]  KIND_SCHED_ACK    = 8'h12;
  localparam [7:0]  KIND_MAC_STAT_REQ = 8'h13;
  localparam [7:0]  KIND_MAC_STAT_REP = 8'h14;
  localparam [7:0]  PYRO_MAGIC        = 8'h50;
  localparam [7:0]  PYRO_VER          = 8'h01;
  localparam [7:0]  ETH_HI            = 8'h88;
  localparam [7:0]  ETH_LO            = 8'hB5;

  // Engine CSRs: R45 base map handshake + the MAC block at 0x0090+.
  localparam [15:0] CSR_CTRL       = 16'h0010;  // bit0 START, bit1 RESET
  localparam [15:0] CSR_STATUS     = 16'h0014;  // bit0 BUSY, bit1 DONE
  localparam [15:0] CSR_KEY0       = 16'h0090;  // key bytes 0-3, LE word
  localparam [15:0] CSR_KEY1       = 16'h0094;
  localparam [15:0] CSR_KEY2       = 16'h0098;
  localparam [15:0] CSR_KEY3       = 16'h009C;
  localparam [15:0] CSR_KEYCTRL    = 16'h00A0;  // w: bit0 commit, bit1 clr
  localparam [15:0] CSR_KEY_ID     = 16'h00A4;
  localparam [15:0] CSR_KEYCHK_LO  = 16'h00A8;
  localparam [15:0] CSR_KEYCHK_HI  = 16'h00AC;
  localparam [15:0] CSR_MACSTAT    = 16'h00B0;  // bit0 valid, bit1 busy
  localparam [15:0] CSR_SEEN       = 16'h00B4;
  localparam [15:0] CSR_DIGESTED   = 16'h00B8;
  localparam [15:0] CSR_SKIP_NONIP = 16'h00BC;
  localparam [15:0] CSR_SKIP_NOKEY = 16'h00C0;
  localparam [31:0] CTRL_START     = 32'h0000_0001;
  localparam [31:0] CTRL_RESET     = 32'h0000_0002;

  // ---- frame buffer (same shape as the core's RX store) ------------------
  (* ram_style = "distributed" *) reg [511:0] rx_words [0:NWORDS-1];
  reg [511:0] hdr;             // beat-0 snapshot for header reads
  reg  [15:0] rx_len, rx_beat, corpus_len, feed_idx;
  reg         wire_fr;         // beat-0 tuser src bit 6 latch (0x0040)

  // ---- engine (pyro_mac_engine_top) interface ----------------------------
  reg         mac_csr_write;
  reg  [15:0] mac_csr_addr;
  reg  [31:0] mac_csr_wdata;
  wire [31:0] mac_csr_rdata;
  reg         mac_in_valid;
  reg  [7:0]  mac_in_data;
  reg         mac_in_last;
  wire        mac_in_ready;
  wire        mac_res_wr;
  wire [63:0] mac_res_start;
  wire [63:0] mac_res_end;
  wire [31:0] mac_res_pid;
  wire [31:0] mac_res_flags;

  pyro_mac_engine_top mac_engine_inst (
    .clk            (clk),
    .rst_n          (rstn),
    .csr_addr       (mac_csr_addr),
    .csr_wdata      (mac_csr_wdata),
    .csr_write      (mac_csr_write),
    .csr_rdata      (mac_csr_rdata),
    .in_valid       (mac_in_valid),
    .in_data        (mac_in_data),
    .in_last        (mac_in_last),
    .in_ready       (mac_in_ready),
    .res_wr         (mac_res_wr),
    .res_start      (mac_res_start),
    .res_end        (mac_res_end),
    .res_pattern_id (mac_res_pid),
    .res_flags      (mac_res_flags)
  );

  // ---- frame FSM ---------------------------------------------------------
  localparam [3:0] M_RX      = 4'd0,   // buffering one frame
                   M_CLS     = 4'd1,   // decode: wire feed or host kind
                   M_RESET   = 4'd2,   // CTRL.RESET (per-frame state only)
                   M_START   = 4'd3,   // CTRL.START
                   M_WAITB   = 4'd4,   // wait engine BUSY
                   M_FEED    = 4'd5,   // byte-serial feed, in_ready hold
                   M_DRAIN   = 4'd6,   // wait engine DONE
                   M_KEY     = 4'd7,   // KEY0..3, KEY_ID, KEYCTRL commit
                   M_KEYWAIT = 4'd8,   // poll MACSTAT valid && !busy
                   M_KEYCHK  = 4'd9,   // read KEYCHK_LO/HI (2 cyc each)
                   M_SCHED   = 4'd10,  // validate + apply SCHED_SET
                   M_STAT    = 4'd11,  // read the four engine counters
                   M_REPLY   = 4'd12,  // compose the single-beat reply
                   M_REPLYW  = 4'd13;  // hold until the reply left
  reg  [3:0]  mstate;
  reg  [3:0]  k_idx;           // M_KEY/M_KEYWAIT/M_KEYCHK sequencer
  reg  [3:0]  st_idx;          // M_STAT sequencer
  reg  [31:0] wire_seq;        // autonomous; one per P1-scheduled frame,
                               // so seq GAPS reveal skipped frames
  reg  [31:0] cur_seq;         // wire_seq of the frame now in the engine
  reg  [31:0] chk_lo, chk_hi;  // keycheck halves latched in M_KEYCHK
  reg  [31:0] sc_status;       // SCHED_ACK status (0 ok, 1 refused)
  reg  [31:0] seen_c, dig_c, nonip_c, nokey_c;
  reg  [1:0]  reply_sel;       // 0 KEY_ACK, 1 SCHED_ACK, 2 STAT_REPLY
  reg         reply_v;
  reg [511:0] reply_word;
  reg         key_commit_p;    // 1-cycle: KEYCTRL commit just issued
  // blocking scratch (frame FSM only; assigned-before-use each cycle)
  reg [511:0] racc;
  reg  [15:0] cl_t, fa;
  integer ka;

  // ---- report FSM declarations (the frame FSM reads reports_sent /
  // records_lost / rep_active, so everything cross-process is declared
  // here, before any of the three always blocks) ---------------------------
  localparam [2:0] R_IDLE = 3'd0,   // wait for a flush condition
                   R_HDR  = 3'd1,   // compose the 36-byte header
                   R_REC  = 3'd2,   // records, one byte per cycle
                   R_PEND = 3'd3,   // wait out a pending codec reply
                   R_TX   = 3'd4;   // drive the report beats
  // 17 x 512b = the largest report (28 + 8 + 64*16 = 1060 B)
  (* ram_style = "distributed" *) reg [511:0] rep_words [0:16];
  reg  [2:0]  rstate;
  reg  [5:0]  rd_ptr;
  reg  [6:0]  rep_cnt, rec_i;
  reg  [3:0]  rec_byte;
  reg         rep_loss;
  reg  [15:0] rep_len, r_compose, rtx_beat;
  reg  [31:0] reports_sent;
  reg [511:0] r_acc;
  // blocking scratch (report FSM only)
  reg [511:0] r_wacc;
  reg [127:0] cur_rec;
  reg  [15:0] len16, cnt16;
  reg         rtxw_en;
  reg  [4:0]  rtxw_sel;
  reg [511:0] rtxw_data;
  reg  [7:0]  r_byte;
  integer kb;

  // report-beat presentation scratch (driven only by the combinational
  // block at the bottom; R_TX reads r_rem, hence declared here)
  reg  [15:0]  r_eff, r_rem, r_this;
  reg  [511:0] r_word;
  reg  [63:0]  r_keep;
  integer rk;

  wire rep_active = (rstate == R_TX);
  wire fifo_pop   = (rstate == R_REC) && (rec_i < rep_cnt) &&
                    (rec_byte == 4'd15);

  // ---- 64-record FIFO between digest and report (contract batcher) -------
  // Record bit layout IS the wire "<IHHQ" little-endian layout: byte j of
  // a record is rfifo[.][8*j +: 8] = {digest, flags, pkt_len, wire_seq}.
  (* ram_style = "distributed" *) reg [127:0] rfifo [0:63];
  reg  [5:0]  wr_ptr;
  reg  [6:0]  fifo_cnt;
  reg  [31:0] records_lost;
  reg         loss_pending;    // a record was lost since the last report
  reg  [47:0] rep_dst, rep_src;  // L2 identity of the first pending record
  reg  [31:0] key_id_rep;
  reg         key_flush_pend;  // rekey: flush the pending records so no
                               // report's records span two keys (MR15)
  reg  [17:0] flush_ft;

  // Records still owed to the report being composed.  While every
  // pending record belongs to the in-flight report, the NEXT record
  // written is the first of the NEXT report, which is when the head
  // latch below must fire (fifo_cnt == 0 alone misses this whenever
  // sustained traffic keeps the FIFO from ever draining empty).
  wire [6:0] cur_left = ((rstate == R_HDR) || (rstate == R_REC))
                        ? (rep_cnt - rec_i) : 7'd0;

  wire flush_tick = (flush_ft == 18'h3FFFF);   // 2^18-cycle flush period
  wire rep_begin  = (rstate == R_IDLE) &&
                    ((fifo_cnt >= 7'd64) ||
                     ((flush_tick || key_flush_pend) &&
                      (fifo_cnt != 7'd0)));
  wire fifo_wr_ok = mac_res_wr && ((fifo_cnt < 7'd64) || fifo_pop);

  wire [7:0] h_kind = hdr[8*16 +: 8];

  function automatic [6:0] mac_keep_bytes(input [63:0] keep);
    integer j;
    begin
      mac_keep_bytes = 7'd0;
      for (j = 0; j < 64; j = j + 1)
        mac_keep_bytes = mac_keep_bytes + {6'd0, keep[j]};
    end
  endfunction

  assign s_axis_tready = (mstate == M_RX);

  always @(posedge clk) begin
    if (!rstn) begin
      mstate        <= M_RX;
      hdr           <= 512'b0;
      rx_len        <= 16'd0;
      rx_beat       <= 16'd0;
      corpus_len    <= 16'd0;
      feed_idx      <= 16'd0;
      wire_fr       <= 1'b0;
      k_idx         <= 4'd0;
      st_idx        <= 4'd0;
      wire_seq      <= 32'd0;
      cur_seq       <= 32'd0;
      chk_lo        <= 32'd0;
      chk_hi        <= 32'd0;
      sc_status     <= 32'd0;
      seen_c        <= 32'd0;
      dig_c         <= 32'd0;
      nonip_c       <= 32'd0;
      nokey_c       <= 32'd0;
      reply_sel     <= 2'd0;
      reply_v       <= 1'b0;
      reply_word    <= 512'b0;
      key_commit_p  <= 1'b0;
      // contract reset default: round-robin, one frame per turn
      sched_mode    <= 2'd2;
      sched_quantum <= 16'd1;
      mac_csr_write <= 1'b0;
      mac_csr_addr  <= CSR_STATUS;
      mac_csr_wdata <= 32'b0;
      mac_in_valid  <= 1'b0;
      mac_in_data   <= 8'b0;
      mac_in_last   <= 1'b0;
    end else begin
      // pulsed controls each cycle; default read = STATUS so BUSY/DONE
      // polling is always valid between explicit drives.
      mac_csr_write <= 1'b0;
      mac_csr_addr  <= CSR_STATUS;
      mac_in_valid  <= 1'b0;
      mac_in_last   <= 1'b0;
      key_commit_p  <= 1'b0;

      case (mstate)
        // ---- receive + buffer one frame --------------------------------
        M_RX: begin
          if (s_axis_tvalid) begin
            if (rx_beat < NWORDS[15:0])
              rx_words[rx_beat[7:0]] <= s_axis_tdata;
            if (rx_beat == 16'd0) begin
              hdr     <= s_axis_tdata;
              wire_fr <= s_axis_tuser[22];
            end
            rx_len  <= rx_beat*64 + {9'd0, mac_keep_bytes(s_axis_tkeep)};
            rx_beat <= rx_beat + 16'd1;
            if (s_axis_tlast) mstate <= M_CLS;
          end
        end

        // ---- classify: wire frame -> digest, host kind -> codec --------
        M_CLS: begin
          rx_beat  <= 16'd0;
          feed_idx <= 16'd0;
          k_idx    <= 4'd0;
          st_idx   <= 4'd0;
          if (wire_fr) begin
            // Feed the WHOLE frame; the engine decides digest vs skip
            // (non-IP / no committed key) and owns those counters.
            // Every P1-scheduled frame consumes one wire_seq.
            cur_seq  <= wire_seq;
            wire_seq <= wire_seq + 32'd1;
            cl_t = rx_len;
            if (cl_t > MAX_FRAME[15:0]) cl_t = MAX_FRAME[15:0];
            corpus_len <= cl_t;
            mstate <= (rx_len == 16'd0) ? M_RX : M_RESET;
          end else if (h_kind == KIND_MAC_KEY_LOAD) begin
            mstate <= M_KEY;
          end else if (h_kind == KIND_SCHED_SET) begin
            mstate <= M_SCHED;
          end else if (h_kind == KIND_MAC_STAT_REQ) begin
            mstate <= M_STAT;
          end else begin
            mstate <= M_RX;      // defensive: the demux sends no more
          end
        end

        // ---- wire-frame digest: RESET -> START -> feed -> DONE ---------
        // CTRL.RESET clears per-frame scan state ONLY.  The key store and
        // the cumulative SEEN..SKIP_NOKEY counters survive it — KEYCTRL
        // bit1 is the one and only key invalidation (contract).
        M_RESET: begin
          mac_csr_write <= 1'b1;
          mac_csr_addr  <= CSR_CTRL;
          mac_csr_wdata <= CTRL_RESET;
          mstate <= M_START;
        end
        M_START: begin
          mac_csr_write <= 1'b1;
          mac_csr_addr  <= CSR_CTRL;
          mac_csr_wdata <= CTRL_START;
          mstate <= M_WAITB;
        end
        M_WAITB: begin
          if (mac_csr_rdata[0]) mstate <= M_FEED;    // BUSY
        end
        // Byte-serial feed from frame byte 0.  The engine's in_ready is
        // a CREDIT ("room for one more"), NOT plain AXI-Stream: a beat
        // registered here lands one cycle past the ready it observed and
        // the engine's 2-deep queue absorbs it (the SR7 contract shape).
        // So the feed PULSES valid — one beat per observed credit —
        // and never holds: holding valid across a stall would re-queue
        // the same byte every cycle and starve the credit forever.
        M_FEED: begin
          if (mac_in_ready) begin
            mac_in_valid <= 1'b1;
            fa = feed_idx;
            mac_in_data <=
                rx_words[fa[13:6]][ {fa[5:0], 3'b000} +: 8 ];
            mac_in_last <= (feed_idx == (corpus_len - 16'd1));
            if (feed_idx == (corpus_len - 16'd1))
              mstate <= M_DRAIN;
            feed_idx <= feed_idx + 16'd1;
          end
        end
        M_DRAIN: begin
          if (mac_csr_rdata[1]) mstate <= M_RX;      // DONE
        end

        // ---- MAC_KEY_LOAD: KEY0..3, KEY_ID, then commit ---------------
        // Payload (frame offsets): key_id u32 BE @28, key bytes @32..47.
        // KEYn is little-endian word order: key byte 0 -> KEY0[7:0]
        // (SipHash reference convention: bytes 0-7 = k0 LE, 8-15 = k1).
        M_KEY: begin
          mac_csr_write <= 1'b1;
          case (k_idx)
            4'd0: begin
              mac_csr_addr  <= CSR_KEY0;
              mac_csr_wdata <= {hdr[8*35 +: 8], hdr[8*34 +: 8],
                                hdr[8*33 +: 8], hdr[8*32 +: 8]};
            end
            4'd1: begin
              mac_csr_addr  <= CSR_KEY1;
              mac_csr_wdata <= {hdr[8*39 +: 8], hdr[8*38 +: 8],
                                hdr[8*37 +: 8], hdr[8*36 +: 8]};
            end
            4'd2: begin
              mac_csr_addr  <= CSR_KEY2;
              mac_csr_wdata <= {hdr[8*43 +: 8], hdr[8*42 +: 8],
                                hdr[8*41 +: 8], hdr[8*40 +: 8]};
            end
            4'd3: begin
              mac_csr_addr  <= CSR_KEY3;
              mac_csr_wdata <= {hdr[8*47 +: 8], hdr[8*46 +: 8],
                                hdr[8*45 +: 8], hdr[8*44 +: 8]};
            end
            4'd4: begin
              mac_csr_addr  <= CSR_KEY_ID;
              mac_csr_wdata <= {hdr[8*28 +: 8], hdr[8*29 +: 8],
                                hdr[8*30 +: 8], hdr[8*31 +: 8]};
            end
            default: begin
              mac_csr_addr  <= CSR_KEYCTRL;
              mac_csr_wdata <= 32'h0000_0001;        // commit
              key_commit_p  <= 1'b1;   // report FIFO: flush old-key recs
              k_idx  <= 4'd0;
              mstate <= M_KEYWAIT;
            end
          endcase
          if (k_idx < 4'd5) k_idx <= k_idx + 4'd1;
        end

        // ---- wait for the keycheck: MACSTAT bit0 && !bit1 --------------
        // The keycheck is itself a SipHash pass over "PYROMACKEYCHECK1",
        // so "valid after commit" is a POLL, not an assumption.  And it
        // must poll for COMPLETION, not validity: bit0 (key_valid)
        // stays HIGH across a RE-key, so bit0 alone would read the
        // PREVIOUS key's keycheck ~25 cycles early.  bit1 is the
        // commit-in-progress flag the engine exposes for exactly this.
        // Two spacer cycles absorb the registered-read (N+2) latency
        // before the first sample is believed.
        M_KEYWAIT: begin
          mac_csr_addr <= CSR_MACSTAT;
          if (k_idx < 4'd2) begin
            k_idx <= k_idx + 4'd1;
          end else if (mac_csr_rdata[0] && !mac_csr_rdata[1]) begin
            k_idx  <= 4'd0;
            mstate <= M_KEYCHK;
          end
        end

        // ---- read KEYCHK_LO/HI, address held two cycles each -----------
        M_KEYCHK: begin
          case (k_idx)
            4'd0: mac_csr_addr <= CSR_KEYCHK_LO;
            4'd1: mac_csr_addr <= CSR_KEYCHK_LO;     // hold: data lands
            4'd2: begin
              mac_csr_addr <= CSR_KEYCHK_HI;
              chk_lo       <= mac_csr_rdata;
            end
            4'd3: mac_csr_addr <= CSR_KEYCHK_HI;
            default: begin
              chk_hi    <= mac_csr_rdata;
              reply_sel <= 2'd0;
              mstate    <= M_REPLY;
            end
          endcase
          k_idx <= k_idx + 4'd1;
        end

        // ---- SCHED_SET: validate, apply-or-refuse, always ACK ----------
        // mode u8 @28 (0 P0-only, 1 P1-only, 2 RR), quantum u16 BE
        // @30-31 (>= 1).  A refusal leaves the schedule UNCHANGED and
        // reports status 1 (contract).
        M_SCHED: begin
          if ((hdr[8*28 +: 8] <= 8'd2) &&
              ({hdr[8*30 +: 8], hdr[8*31 +: 8]} != 16'd0)) begin
            sched_mode    <= hdr[8*28 +: 2];
            sched_quantum <= {hdr[8*30 +: 8], hdr[8*31 +: 8]};
            sc_status     <= 32'd0;
          end else begin
            sc_status     <= 32'd1;
          end
          reply_sel <= 2'd1;
          mstate    <= M_REPLY;
        end

        // ---- MAC_STAT: four engine counters, two held cycles each ------
        // Served only between frames (this responder is lockstep), so the
        // engine is idle and the zero-slack set is coherent.
        M_STAT: begin
          case (st_idx)
            4'd0: mac_csr_addr <= CSR_SEEN;
            4'd1: mac_csr_addr <= CSR_SEEN;
            4'd2: begin
              mac_csr_addr <= CSR_DIGESTED;
              seen_c       <= mac_csr_rdata;
            end
            4'd3: mac_csr_addr <= CSR_DIGESTED;
            4'd4: begin
              mac_csr_addr <= CSR_SKIP_NONIP;
              dig_c        <= mac_csr_rdata;
            end
            4'd5: mac_csr_addr <= CSR_SKIP_NONIP;
            4'd6: begin
              mac_csr_addr <= CSR_SKIP_NOKEY;
              nonip_c      <= mac_csr_rdata;
            end
            4'd7: mac_csr_addr <= CSR_SKIP_NOKEY;
            default: begin
              nokey_c   <= mac_csr_rdata;
              reply_sel <= 2'd2;
              mstate    <= M_REPLY;
            end
          endcase
          st_idx <= st_idx + 4'd1;
        end

        // ---- compose the single-beat reply (all three fit one beat) ----
        M_REPLY: begin
          racc = 512'b0;
          for (ka = 0; ka < 6; ka = ka + 1) begin
            racc[8*ka +: 8]       = hdr[8*(6 + ka) +: 8];  // MAC swap
            racc[8*(6 + ka) +: 8] = hdr[8*ka +: 8];
          end
          racc[8*12 +: 8] = ETH_HI;
          racc[8*13 +: 8] = ETH_LO;
          racc[8*14 +: 8] = PYRO_MAGIC;
          racc[8*15 +: 8] = PYRO_VER;
          racc[8*16 +: 8] = (reply_sel == 2'd0) ? KIND_MAC_KEY_ACK :
                            (reply_sel == 2'd1) ? KIND_SCHED_ACK
                                                : KIND_MAC_STAT_REP;
          racc[8*17 +: 8] = 8'h00;                   // flags (R78.3)
          racc[8*18 +: 8] = hdr[8*18 +: 8];          // slot echo
          racc[8*19 +: 8] = hdr[8*19 +: 8];
          racc[8*20 +: 8] = hdr[8*20 +: 8];          // seq echo
          racc[8*21 +: 8] = hdr[8*21 +: 8];
          racc[8*22 +: 8] = hdr[8*22 +: 8];
          racc[8*23 +: 8] = hdr[8*23 +: 8];
          racc[8*26 +: 8] = 8'h00;
          racc[8*27 +: 8] = 8'h00;
          if (reply_sel == 2'd0) begin
            // MAC_KEY_ACK: key_id echo + keycheck u64 BE ({HI, LO})
            racc[8*24 +: 8] = 8'h00; racc[8*25 +: 8] = 8'h0C;
            racc[8*28 +: 8] = hdr[8*28 +: 8];
            racc[8*29 +: 8] = hdr[8*29 +: 8];
            racc[8*30 +: 8] = hdr[8*30 +: 8];
            racc[8*31 +: 8] = hdr[8*31 +: 8];
            racc[8*32 +: 8] = chk_hi[31:24];
            racc[8*33 +: 8] = chk_hi[23:16];
            racc[8*34 +: 8] = chk_hi[15:8];
            racc[8*35 +: 8] = chk_hi[7:0];
            racc[8*36 +: 8] = chk_lo[31:24];
            racc[8*37 +: 8] = chk_lo[23:16];
            racc[8*38 +: 8] = chk_lo[15:8];
            racc[8*39 +: 8] = chk_lo[7:0];
          end else if (reply_sel == 2'd1) begin
            // SCHED_ACK: post-op mode/quantum + status
            racc[8*24 +: 8] = 8'h00; racc[8*25 +: 8] = 8'h08;
            racc[8*28 +: 8] = {6'd0, sched_mode};
            racc[8*29 +: 8] = 8'h00;
            racc[8*30 +: 8] = sched_quantum[15:8];
            racc[8*31 +: 8] = sched_quantum[7:0];
            racc[8*32 +: 8] = sc_status[31:24];
            racc[8*33 +: 8] = sc_status[23:16];
            racc[8*34 +: 8] = sc_status[15:8];
            racc[8*35 +: 8] = sc_status[7:0];
          end else begin
            // MAC_STAT_REPLY: 6 x u32 BE — the engine's zero-slack set
            // plus the two wrapper-owned report counters
            racc[8*24 +: 8] = 8'h00; racc[8*25 +: 8] = 8'h18;
            racc[8*28 +: 8] = seen_c[31:24];
            racc[8*29 +: 8] = seen_c[23:16];
            racc[8*30 +: 8] = seen_c[15:8];
            racc[8*31 +: 8] = seen_c[7:0];
            racc[8*32 +: 8] = dig_c[31:24];
            racc[8*33 +: 8] = dig_c[23:16];
            racc[8*34 +: 8] = dig_c[15:8];
            racc[8*35 +: 8] = dig_c[7:0];
            racc[8*36 +: 8] = nonip_c[31:24];
            racc[8*37 +: 8] = nonip_c[23:16];
            racc[8*38 +: 8] = nonip_c[15:8];
            racc[8*39 +: 8] = nonip_c[7:0];
            racc[8*40 +: 8] = nokey_c[31:24];
            racc[8*41 +: 8] = nokey_c[23:16];
            racc[8*42 +: 8] = nokey_c[15:8];
            racc[8*43 +: 8] = nokey_c[7:0];
            racc[8*44 +: 8] = reports_sent[31:24];
            racc[8*45 +: 8] = reports_sent[23:16];
            racc[8*46 +: 8] = reports_sent[15:8];
            racc[8*47 +: 8] = reports_sent[7:0];
            racc[8*48 +: 8] = records_lost[31:24];
            racc[8*49 +: 8] = records_lost[23:16];
            racc[8*50 +: 8] = records_lost[15:8];
            racc[8*51 +: 8] = records_lost[7:0];
          end
          reply_word <= racc;
          reply_v    <= 1'b1;
          mstate     <= M_REPLYW;
        end

        // ---- hold until the reply left (lockstep, like every R78 op) ---
        M_REPLYW: begin
          if (!rep_active && m_axis_tready) begin
            reply_v <= 1'b0;
            mstate  <= M_RX;
          end
        end

        default: mstate <= M_RX;
      endcase
    end
  end

  // ---- 64-record FIFO writer (res_wr capture + flush timer) --------------
  always @(posedge clk) begin
    if (!rstn) begin
      wr_ptr       <= 6'd0;
      fifo_cnt     <= 7'd0;
      records_lost <= 32'd0;
      loss_pending <= 1'b0;
      rep_dst      <= 48'd0;
      rep_src      <= 48'd0;
      key_id_rep   <= 32'd0;
      key_flush_pend <= 1'b0;
      flush_ft     <= 18'd0;
    end else begin
      flush_ft <= flush_ft + 18'd1;
      if (key_commit_p)
        key_flush_pend <= 1'b1;    // set wins over the clears below
      else if (rep_begin || (fifo_cnt == 7'd0))
        key_flush_pend <= 1'b0;
      if (rep_begin)
        loss_pending <= 1'b0;      // the set below wins (set-dominant)
      if (fifo_wr_ok) begin
        rfifo[wr_ptr] <= {mac_res_start, mac_res_pid[15:0],
                          mac_res_end[15:0], cur_seq};
        wr_ptr <= wr_ptr + 6'd1;
        if ((fifo_cnt == cur_left) || rep_begin) begin
          // FIRST record of the NEXT report: it answers toward this
          // record's sender, and its key_id is the one the engine
          // reported for this record.  "First" means every pending
          // record already belongs to the in-flight report (cur_left,
          // 0 when idle) or is being claimed by rep_begin this cycle.
          rep_dst    <= hdr[95:48];
          rep_src    <= hdr[47:0];
          key_id_rep <= mac_res_flags;
        end
      end else if (mac_res_wr) begin
        // C2H backpressure filled the buffer: drop the RECORD, count it,
        // keep digesting — the wire path is never stalled (contract).
        records_lost <= records_lost + 32'd1;
        loss_pending <= 1'b1;
      end
      fifo_cnt <= fifo_cnt + {6'd0, fifo_wr_ok} - {6'd0, fifo_pop};
    end
  end

  // ---- report FSM: drain the FIFO into one MAC_REPORT frame --------------
  always @(posedge clk) begin
    if (!rstn) begin
      rstate       <= R_IDLE;
      rd_ptr       <= 6'd0;
      rep_cnt      <= 7'd0;
      rec_i        <= 7'd0;
      rec_byte     <= 4'd0;
      rep_loss     <= 1'b0;
      rep_len      <= 16'd0;
      r_compose    <= 16'd0;
      rtx_beat     <= 16'd0;
      reports_sent <= 32'd0;
      r_acc        <= 512'b0;
    end else begin
      rtxw_en = 1'b0;
      case (rstate)
        R_IDLE: begin
          if (rep_begin) begin
            rep_cnt  <= fifo_cnt[6:0];   // <= 64 by FIFO capacity
            rep_loss <= loss_pending;
            rec_i    <= 7'd0;
            rec_byte <= 4'd0;
            rstate   <= R_HDR;
          end
        end

        // ---- header: 36 bytes into the word-0 accumulator --------------
        R_HDR: begin
          cur_rec = rfifo[rd_ptr];           // first record (async read)
          len16   = 16'd8 + {5'd0, rep_cnt, 4'b0000};
          cnt16   = {9'd0, rep_cnt};
          r_wacc  = 512'b0;
          for (kb = 0; kb < 6; kb = kb + 1) begin
            r_wacc[8*kb +: 8]       = rep_dst[8*kb +: 8];
            r_wacc[8*(6 + kb) +: 8] = rep_src[8*kb +: 8];
          end
          r_wacc[8*12 +: 8] = ETH_HI;
          r_wacc[8*13 +: 8] = ETH_LO;
          r_wacc[8*14 +: 8] = PYRO_MAGIC;
          r_wacc[8*15 +: 8] = PYRO_VER;
          r_wacc[8*16 +: 8] = KIND_MAC_REPORT;
          r_wacc[8*17 +: 8] = 8'h00;         // flags stay 0 (R78.3)
          r_wacc[8*18 +: 8] = SLOT[15:8];    // own slot, not echoed
          r_wacc[8*19 +: 8] = SLOT[7:0];
          r_wacc[8*20 +: 8] = cur_rec[31:24];  // seq = first wire_seq
          r_wacc[8*21 +: 8] = cur_rec[23:16];
          r_wacc[8*22 +: 8] = cur_rec[15:8];
          r_wacc[8*23 +: 8] = cur_rec[7:0];
          r_wacc[8*24 +: 8] = len16[15:8];
          r_wacc[8*25 +: 8] = len16[7:0];
          r_wacc[8*26 +: 8] = 8'h00;
          r_wacc[8*27 +: 8] = 8'h00;
          r_wacc[8*28 +: 8] = cnt16[15:8];
          r_wacc[8*29 +: 8] = cnt16[7:0];
          // status: bit2 wire-origin ALWAYS set, bit0 loss since last
          r_wacc[8*30 +: 8] = 8'h00;
          r_wacc[8*31 +: 8] = {5'd0, 1'b1, 1'b0, rep_loss};
          r_wacc[8*32 +: 8] = key_id_rep[31:24];
          r_wacc[8*33 +: 8] = key_id_rep[23:16];
          r_wacc[8*34 +: 8] = key_id_rep[15:8];
          r_wacc[8*35 +: 8] = key_id_rep[7:0];
          r_acc     <= r_wacc;
          r_compose <= 16'd36;
          rstate    <= R_REC;
        end

        // ---- records: one byte/cycle, ST_BENT-style accumulate/flush ---
        // Records are 16 B but start at offset 36, so they straddle
        // 64-byte words; the byte-serial insert + full-word flush is the
        // core's proven pattern.  ~1060 cycles for a full report — three
        // orders of magnitude inside the 2^18-cycle flush period.
        R_REC: begin
          if (rec_i >= rep_cnt) begin
            if (r_compose[5:0] != 6'd0) begin
              rtxw_en   = 1'b1;
              rtxw_sel  = r_compose[10:6];
              rtxw_data = r_acc;
            end
            rep_len  <= r_compose;
            rtx_beat <= 16'd0;
            rstate   <= R_PEND;
          end else begin
            cur_rec = rfifo[rd_ptr];
            r_byte  = cur_rec[ {rec_byte, 3'b000} +: 8 ];
            r_wacc  = r_acc;
            r_wacc[ {r_compose[5:0], 3'b000} +: 8 ] = r_byte;
            if (r_compose[5:0] == 6'd63) begin
              rtxw_en   = 1'b1;
              rtxw_sel  = r_compose[10:6];
              rtxw_data = r_wacc;
              r_acc     <= 512'b0;
            end else begin
              r_acc <= r_wacc;
            end
            if (rec_byte == 4'd15) begin
              rec_byte <= 4'd0;
              rd_ptr   <= rd_ptr + 6'd1;
              rec_i    <= rec_i + 7'd1;
            end else begin
              rec_byte <= rec_byte + 4'd1;
            end
            r_compose <= r_compose + 16'd1;
          end
        end

        // ---- wait out a pending single-beat reply ----------------------
        // The output mux switches sources only BETWEEN packets; entering
        // R_TX while a composed reply is being presented would retarget
        // tdata under a high tvalid.  Waiting here is safe: capture keeps
        // running and an overflow is a counted record loss, not a stall.
        R_PEND: begin
          if (!reply_v) rstate <= R_TX;
        end

        // ---- drive the report beats; count it when the last one goes ---
        R_TX: begin
          if (m_axis_tready) begin
            if (r_rem <= 16'd64) begin
              reports_sent <= reports_sent + 32'd1;
              rstate       <= R_IDLE;
            end else begin
              rtx_beat <= rtx_beat + 16'd1;
            end
          end
        end

        default: rstate <= R_IDLE;
      endcase

      // SINGLE rep_words write port (same discipline as tx_words)
      if (rtxw_en)
        rep_words[rtxw_sel] <= rtxw_data;
    end
  end

  // ---- P1 AXIS master: report beats or the single-beat codec reply -------
  always @(*) begin
    r_eff  = (rep_len < 16'd60) ? 16'd60 : rep_len;   // R78.9 L2 min
    r_rem  = r_eff - (rtx_beat * 16'd64);
    r_this = (r_rem < 16'd64) ? r_rem : 16'd64;
    r_word = rep_words[rtx_beat[4:0]];
    r_keep = 64'b0;
    for (rk = 0; rk < 64; rk = rk + 1)
      if (rk[15:0] < r_this) r_keep[rk] = 1'b1;
  end

  // every codec reply is <= 52 B of content, presented as one 60 B beat
  // (rep_active is declared with the cross-process signals above)
  localparam [63:0] KEEP60 = 64'h0FFF_FFFF_FFFF_FFFF;

  assign m_axis_tvalid = rep_active | reply_v;
  assign m_axis_tdata  = rep_active ? r_word : reply_word;
  assign m_axis_tkeep  = rep_active ? r_keep : KEEP60;
  assign m_axis_tlast  = rep_active ? (r_rem <= 16'd64) : 1'b1;
  assign m_axis_tuser  = {16'h0000, 16'h0000,
                          rep_active ? r_eff : 16'd60};

endmodule : pyro_rp_mac_prog


// ***************************************************************************
// pyro_rp — multi-program top (PYRO MAC contract): P0 + P1 co-resident,
// switched at RUN TIME, never by reconfiguration.
//
//   * P0 = pyro_rp_core: the existing child UNMODIFIED (module renamed);
//     every R78 kind it served, it still serves — including the unknown-
//     kind drop (R78.4) — so host behavior is unchanged unless a frame
//     carries one of the three new MAC kinds.
//   * P1 = pyro_rp_mac_prog above.
//   * Both AXIS boundaries pass through TWO-STAGE SKID BUFFERS so tready
//     is REGISTERED — the v4 top's discipline, adopted after its
//     combinational tdata->tready peek cost WNS -0.821 ns.
//   * RX dispatch, decided once per packet on the REGISTERED first beat:
//     WIRE frames (tuser src bit 6 = 0x0040) follow the schedule —
//     mode 0 = P0 only, mode 1 = P1 only, mode 2 = round-robin with
//     `quantum` frames per program per turn (reset default: mode 2,
//     quantum 1).  Packet-atomic: a locked target holds to tlast, so a
//     switch can never happen mid-frame.  HOST frames NEVER enter the
//     schedule: the three MAC kinds go to P1's codec, everything else
//     to P0 (R78 request/reply lockstep preserved per program).
//   * TX: packet-atomic round-robin between P0 and P1 output.
// ***************************************************************************
module pyro_rp #(
  parameter [15:0] SPEC16          = @SPEC16@,
  parameter [15:0] BUILD16         = `PYRO_BUILD16,
  parameter [31:0] HARNESS_VERSION = @HARNESSVER@,
  parameter [15:0] SLOT            = @SLOT@,
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

  localparam [7:0] KIND_MAC_KEY_LOAD = 8'h0F;
  localparam [7:0] KIND_SCHED_SET    = 8'h11;
  localparam [7:0] KIND_MAC_STAT_REQ = 8'h13;

  // ---- slave-side two-stage skid: registered tready, registered beats ----
  reg          si_v0, si_v1;
  reg  [511:0] si_d0, si_d1;
  reg   [63:0] si_k0, si_k1;
  reg          si_l0, si_l1;
  reg   [47:0] si_u0, si_u1;

  wire d_tready;                              // dispatcher consumes stage 0
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

  // ---- per-program AXIS wiring -------------------------------------------
  wire         p0_s_tvalid, p1_s_tvalid;
  wire         p0_s_tready, p1_s_tready;
  wire         p0_m_tvalid, p1_m_tvalid;
  wire [511:0] p0_m_tdata,  p1_m_tdata;
  wire  [63:0] p0_m_tkeep,  p1_m_tkeep;
  wire         p0_m_tlast,  p1_m_tlast;
  wire  [47:0] p0_m_tuser,  p1_m_tuser;
  wire         p0_m_tready, p1_m_tready;

  wire  [1:0]  sched_mode;
  wire [15:0]  sched_quantum;

  // ---- RX dispatch (registered stage-0 beat, packet-atomic) --------------
  reg          rx_inpkt;    // mid-packet: target locked in rx_tgt_q
  reg          rx_tgt_q;
  reg          cur_prog;    // whose turn in round-robin mode
  reg  [15:0]  turn_cnt;    // wire frames granted in the current turn
  reg   [1:0]  mode_p;      // schedule-change detect (previous values)
  reg  [15:0]  quant_p;

  wire        wire_tag = si_u0[22];           // tuser src bit 6 = 0x0040
  wire [7:0]  rx_kind  = si_d0[135:128];      // frame byte 16 (beat 0)
  wire        is_pyro  =
      (si_d0[103:96]  == 8'h88) && (si_d0[111:104] == 8'hB5) &&
      (si_d0[119:112] == 8'h50) && (si_d0[127:120] == 8'h01);
  wire        mac_host = is_pyro &&
      ((rx_kind == KIND_MAC_KEY_LOAD) || (rx_kind == KIND_SCHED_SET) ||
       (rx_kind == KIND_MAC_STAT_REQ));

  // Wire frames follow the schedule; host frames NEVER do (contract:
  // src 0x0001 control traffic always reaches its codec).
  wire tgt_new = wire_tag
      ? ((sched_mode == 2'd0) ? 1'b0 :
         (sched_mode == 2'd1) ? 1'b1 : cur_prog)
      : mac_host;
  wire rx_tgt  = rx_inpkt ? rx_tgt_q : tgt_new;
  assign d_tready = rx_tgt ? p1_s_tready : p0_s_tready;

  assign p0_s_tvalid = si_v0 && d_tready && !rx_tgt;
  assign p1_s_tvalid = si_v0 && d_tready &&  rx_tgt;

  always @(posedge clk) begin
    if (!rstn) begin
      rx_inpkt <= 1'b0;
      rx_tgt_q <= 1'b0;
      cur_prog <= 1'b0;
      turn_cnt <= 16'd0;
      mode_p   <= 2'd2;      // matches the P1 reset defaults
      quant_p  <= 16'd1;
    end else begin
      mode_p  <= sched_mode;
      quant_p <= sched_quantum;
      if ((mode_p != sched_mode) || (quant_p != sched_quantum)) begin
        // schedule changed: restart the rotation deterministically
        cur_prog <= 1'b0;
        turn_cnt <= 16'd0;
      end else if (d_fire && !rx_inpkt && wire_tag &&
                   (sched_mode == 2'd2)) begin
        // a wire frame was GRANTED to cur_prog: advance the turn
        if ((turn_cnt + 16'd1) >= sched_quantum) begin
          turn_cnt <= 16'd0;
          cur_prog <= ~cur_prog;
        end else begin
          turn_cnt <= turn_cnt + 16'd1;
        end
      end
      if (d_fire) begin
        if (!rx_inpkt) rx_tgt_q <= rx_tgt;
        rx_inpkt <= !si_l0;
      end
    end
  end

  // ---- TX mux: packet-atomic round-robin between P0 and P1 ---------------
  reg  tx_lock, tx_sel_q, rr_tx;

  wire tx_pick    = rr_tx ? (p1_m_tvalid ? 1'b1 : 1'b0)
                          : (p0_m_tvalid ? 1'b0 : 1'b1);
  wire tx_pick_ok = p0_m_tvalid | p1_m_tvalid;
  wire tx_sel     = tx_lock ? tx_sel_q : tx_pick;
  wire tx_active  = tx_lock | tx_pick_ok;

  wire         mx_tvalid = tx_active && (tx_sel ? p1_m_tvalid
                                               : p0_m_tvalid);
  wire [511:0] mx_tdata  = tx_sel ? p1_m_tdata : p0_m_tdata;
  wire  [63:0] mx_tkeep  = tx_sel ? p1_m_tkeep : p0_m_tkeep;
  wire         mx_tlast  = tx_sel ? p1_m_tlast : p0_m_tlast;
  wire  [47:0] mx_tuser  = tx_sel ? p1_m_tuser : p0_m_tuser;
  wire         mx_tready;                     // from the master-side skid

  assign p0_m_tready = mx_tready && tx_active && !tx_sel;
  assign p1_m_tready = mx_tready && tx_active &&  tx_sel;

  always @(posedge clk) begin
    if (!rstn) begin
      tx_lock  <= 1'b0;
      tx_sel_q <= 1'b0;
      rr_tx    <= 1'b0;
    end else if (mx_tvalid && mx_tready) begin
      if (!tx_lock) begin
        tx_sel_q <= tx_sel;
        rr_tx    <= ~tx_sel;
      end
      tx_lock <= !mx_tlast;
    end
  end

  // ---- master-side two-stage skid: registered tready toward programs -----
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

  // ---- programs -----------------------------------------------------------
  pyro_rp_core #(
    .SPEC16          (SPEC16),
    .BUILD16         (BUILD16),
    .HARNESS_VERSION (HARNESS_VERSION),
    .SLOT            (SLOT),
    .RP_CHILD_ID     (RP_CHILD_ID)
  ) u_prog0 (
    .clk           (clk),
    .rstn          (rstn),
    .s_axis_tvalid (p0_s_tvalid),
    .s_axis_tdata  (si_d0),
    .s_axis_tkeep  (si_k0),
    .s_axis_tlast  (si_l0),
    .s_axis_tuser  (si_u0),
    .s_axis_tready (p0_s_tready),
    .m_axis_tvalid (p0_m_tvalid),
    .m_axis_tdata  (p0_m_tdata),
    .m_axis_tkeep  (p0_m_tkeep),
    .m_axis_tlast  (p0_m_tlast),
    .m_axis_tuser  (p0_m_tuser),
    .m_axis_tready (p0_m_tready)
  );

  pyro_rp_mac_prog #(
    .SLOT (SLOT)
  ) u_prog1 (
    .clk           (clk),
    .rstn          (rstn),
    .s_axis_tvalid (p1_s_tvalid),
    .s_axis_tdata  (si_d0),
    .s_axis_tkeep  (si_k0),
    .s_axis_tlast  (si_l0),
    .s_axis_tuser  (si_u0),
    .s_axis_tready (p1_s_tready),
    .m_axis_tvalid (p1_m_tvalid),
    .m_axis_tdata  (p1_m_tdata),
    .m_axis_tkeep  (p1_m_tkeep),
    .m_axis_tlast  (p1_m_tlast),
    .m_axis_tuser  (p1_m_tuser),
    .m_axis_tready (p1_m_tready),
    .sched_mode    (sched_mode),
    .sched_quantum (sched_quantum)
  );

endmodule : pyro_rp
"""
