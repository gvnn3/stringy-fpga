// PYRO MAC engine — per-packet SipHash-2-4 digest over the
// transit-invariant bytes of a wire frame (multi-program MAC contract,
// P1 program; RFC 4302 mutable-field model).
//
// One frame arrives as a byte stream (in_valid/in_data/in_last, byte 0 =
// first byte of the Ethernet frame).  The parser skips L2 entirely (MACs,
// any stack of 0x8100/0x88A8 VLAN tags, the final EtherType), then feeds
// the SipHash core every byte from the start of the IP header to the end
// of the frame, substituting 0x00 for the mutable bytes IN PLACE — masking
// rather than elision, so offsets stay stable and two frames that differ
// only in TTL/DSCP/checksums produce the SAME digest:
//
//   IPv4  zeroed: byte 1 (DSCP/ECN), 6-7 (flags+frag), 8 (TTL), 10-11
//         (header checksum), and ALL options bytes when IHL > 5 (record
//         flag bit5).  Covered: ver/IHL, total length, id, proto, addrs.
//   IPv6  zeroed: byte 0 low nibble (TC high; version nibble kept), bytes
//         1-3 (TC low + flow label), byte 7 (hop limit).
//   L4    checksum zeroed: TCP 16-17, UDP 6-7, ICMP/ICMPv6 2-3 (offsets
//         from the L4 header).  Unknown L4 proto: cover ALL of it (flag
//         bit4).  IPv6 extension headers are NOT walked (v1): the
//         next-header byte is treated as the L4 proto.
//
// Frames that are not IPv4/IPv6 are consumed with no digest and counted
// skip_nonip; frames that arrive before a key is committed are counted
// skip_nokey (fail-closed — the contract forbids digesting under no key).
// A frame is flagged truncated (bit6) when it ends before its header
// regions complete (IP header + the L4 checksum window, in_last early)
// OR when it delivers fewer L3 bytes than the IPv4 total-length /
// IPv6 payload-length field claims (snap-length and wire-truncated
// captures) — matching pyro.macwire flag-for-flag.  Either way the
// digest covers exactly the bytes absorbed; if digesting never
// started the frame degrades to skip_nonip.
//
// The classification is exhaustive and exclusive by construction — every
// frame ends in exactly one evt_kind — which is what makes the top's
// zero-slack counter invariant (seen == digested + skip_nonip +
// skip_nokey) hold with EXACT equality.
//
// Input handshake: in_ready is a CREDIT ("room for one more"), NOT plain
// AXI-Stream — the same 2-deep queue discipline as pyro_overlay_engine,
// because rp_wrapper's ST_FEED pulses in_valid once per byte and may
// commit a beat one cycle past the ready it observed.  The queue absorbs
// that beat while the SipHash core is mid-compression.
//
// Byte-serial absorption is 8 bytes per ~10 cycles at axis_aclk 250 MHz
// (~200 MB/s), far above the ~30.5 MB/s wire ingest; the queue plus
// credit handshake covers the compression stalls.

`timescale 1ns/1ps
`default_nettype none

module pyro_mac_engine (
    input  wire        clk,
    input  wire        rst_n,
    // key plumbing, driven by the top-level CSR block; k0/k1 latch into
    // the SipHash core on key_set, so a commit during an in-flight frame
    // cannot tear that frame's digest (the running v-state is untouched).
    input  wire [63:0] k0,
    input  wire [63:0] k1,
    input  wire        key_set,
    input  wire        key_valid,
    // frame byte stream (credit handshake, see header)
    input  wire        in_valid,
    input  wire [7:0]  in_data,
    input  wire        in_last,
    output wire        in_ready,
    // one event pulse per frame, exactly one kind each
    output reg         evt_done,
    output reg  [1:0]  evt_kind,      // EV_* below
    output reg  [63:0] evt_digest,    // valid when EV_DIGESTED
    output reg  [15:0] evt_len,       // total frame bytes consumed
    output reg  [6:0]  evt_flags,     // record flags per contract
    // one pulse when the FIRST byte of a frame is accepted (queued);
    // the top's DIG_CYCLES counter starts here.  Pure observation —
    // nothing in the digest datapath depends on it.
    output reg         evt_start
);

    // Event kinds.  The top decodes these; keep in step with
    // pyro_mac_engine_top.
    localparam [1:0] EV_DIGESTED = 2'd0,
                     EV_NONIP    = 2'd1,
                     EV_NOKEY    = 2'd2;

    // Record flag bits (MAC_REPORT record.flags, contract kind 0x0E).
    localparam integer FB_IPV4  = 0;
    localparam integer FB_IPV6  = 1;
    localparam integer FB_TCP   = 2;
    localparam integer FB_UDP   = 3;
    localparam integer FB_OTHER = 4;
    localparam integer FB_OPT   = 5;
    localparam integer FB_TRUNC = 6;

    localparam [3:0] S_L2    = 4'd0,   // 12 MAC bytes
                     S_ET0   = 4'd1,   // EtherType high byte
                     S_ET1   = 4'd2,   // EtherType low byte; classify
                     S_TCI0  = 4'd3,   // VLAN TCI high (skipped)
                     S_TCI1  = 4'd4,   // VLAN TCI low  (skipped)
                     S_START = 4'd5,   // 1 dead cycle: pulse msg_start
                     S_IP4   = 4'd6,   // IPv4 header incl. options
                     S_IP6   = 4'd7,   // IPv6 fixed 40-byte header
                     S_L4    = 4'd8,   // L4 header up to checksum end
                     S_BODY  = 4'd9,   // everything else, fed as-is
                     S_SKIP  = 4'd10,  // consume-only (non-IP / no key)
                     S_LAST  = 4'd11,  // wait ready, pulse msg_end
                     S_FIN   = 4'd12;  // wait dig_valid, emit event
    reg [3:0]  st;

    reg [5:0]  idx;          // byte index within the current region
    reg [15:0] byte_cnt;     // whole-frame length counter
    reg [7:0]  et_hi;        // EtherType high byte
    reg        ip_is6;
    reg [3:0]  ihl;          // IPv4 IHL nibble, captured at IP byte 0
    reg [7:0]  proto;        // L4 proto / v6 next-header
    reg [4:0]  ck_lo, ck_hi; // L4 checksum byte window
    reg [6:0]  flags;
    reg        frame_nokey;  // latched at frame byte 0: no committed key
    reg        in_frame;     // a frame is open: first byte queued, no
                             // event yet (evt_start bookkeeping only)
    reg [15:0] ip_tlen;      // IPv4 total length / IPv6 payload length
    reg        tlen_ok;      // both length-field bytes were captured
    reg [15:0] l3_cnt;       // L3 bytes actually delivered

    // Garbage guard: an IHL below 5 claims a header shorter than its own
    // fixed fields; clamp to 5 so the FSM cannot wedge.  The frame is
    // nonsense either way and the digest simply covers what arrived.
    wire [3:0] eff_ihl  = (ihl < 4'd5) ? 4'd5 : ihl;
    wire [5:0] v4_last  = {eff_ihl, 2'b00} - 6'd1;

    // Length-claim truncation (record flag bit6): fewer L3 bytes
    // arrived than the IP header claims.  The IPv6 payload length
    // excludes its 40-byte fixed header, so the +40 needs 17 bits.
    wire [16:0] len_claim = ip_is6 ? ({1'b0, ip_tlen} + 17'd40)
                                   : {1'b0, ip_tlen};
    wire        len_short = tlen_ok && ({1'b0, l3_cnt} < len_claim);

    // ---------------- 2-deep input queue (credit handshake) -------------
    reg [7:0] sk0_d, sk1_d;
    reg       sk0_l, sk1_l;
    reg [1:0] sk_n;

    wire       have_byte = (sk_n != 2'd0);
    wire [7:0] byte_in   = sk0_d;
    wire       byte_last = sk0_l;
    wire       push      = in_valid && (sk_n < 2'd2);
    assign in_ready = (sk_n == 2'd0);

    // ---------------- SipHash core ---------------------------------------
    wire        sip_ready;
    wire        sip_dvld;
    wire [63:0] sip_dig;
    reg         sip_start;   // 1-cycle pulse, message begins
    reg         sip_end;     // 1-cycle pulse, message finalizes

    wire feed_st = (st == S_IP4) || (st == S_IP6)
                || (st == S_L4)  || (st == S_BODY);
    wire consume = have_byte
                && ((st == S_L2)  || (st == S_ET0)  || (st == S_ET1)
                 || (st == S_TCI0) || (st == S_TCI1) || (st == S_SKIP)
                 || (feed_st && sip_ready));
    wire pop = consume;

    // Mask mux: substitute 0x00 for mutable bytes as they are fed.  The
    // IPv6 byte 0 case is the one PARTIAL mask (version nibble kept).
    reg [7:0] fed_byte;
    always @* begin
        fed_byte = byte_in;
        case (st)
        S_IP4: if (idx == 6'd1  || idx == 6'd6  || idx == 6'd7
                || idx == 6'd8  || idx == 6'd10 || idx == 6'd11
                || idx >= 6'd20)
                   fed_byte = 8'h00;
        S_IP6: if (idx == 6'd0)
                   fed_byte = byte_in & 8'hF0;
               else if (idx <= 6'd3 || idx == 6'd7)
                   fed_byte = 8'h00;
        S_L4:  if (idx >= {1'b0, ck_lo} && idx <= {1'b0, ck_hi})
                   fed_byte = 8'h00;
        default: ;
        endcase
    end

    pyro_siphash sip (
        .clk(clk), .rst_n(rst_n),
        .k0(k0), .k1(k1), .key_set(key_set),
        .msg_start(sip_start),
        .msg_valid(feed_st && consume),
        .msg_byte(fed_byte),
        .msg_ready(sip_ready),
        .msg_end(sip_end),
        .dig_valid(sip_dvld),
        .dig(sip_dig)
    );

    // A frame whose FIRST byte arrives with no committed key is skip_nokey
    // for its whole life, even if a commit lands mid-frame.
    wire nokey_eff = frame_nokey
                  || ((st == S_L2) && (idx == 6'd0) && !key_valid);

    // Per-frame reset, used by both event sites.
    task frame_reset;
    begin
        st          <= S_L2;
        idx         <= 6'd0;
        byte_cnt    <= 16'd0;
        flags       <= 7'd0;
        frame_nokey <= 1'b0;
        in_frame    <= 1'b0;
        ihl         <= 4'd0;
        proto       <= 8'd0;
        tlen_ok     <= 1'b0;
        l3_cnt      <= 16'd0;
    end
    endtask

    // Frame ended before any byte was absorbed: skip event, one pulse.
    task end_skip;
    begin
        evt_done <= 1'b1;
        evt_kind <= nokey_eff ? EV_NOKEY : EV_NONIP;
        evt_len  <= byte_cnt + 16'd1;
        evt_flags <= 7'd0;
        frame_reset;
    end
    endtask

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            st <= S_L2; idx <= 6'd0; byte_cnt <= 16'd0;
            et_hi <= 8'd0; ip_is6 <= 1'b0; ihl <= 4'd0; proto <= 8'd0;
            ck_lo <= 5'd0; ck_hi <= 5'd0; flags <= 7'd0;
            frame_nokey <= 1'b0;
            ip_tlen <= 16'd0; tlen_ok <= 1'b0; l3_cnt <= 16'd0;
            sk0_d <= 8'd0; sk1_d <= 8'd0; sk0_l <= 1'b0; sk1_l <= 1'b0;
            sk_n <= 2'd0;
            sip_start <= 1'b0; sip_end <= 1'b0;
            evt_done <= 1'b0; evt_kind <= 2'd0; evt_digest <= 64'd0;
            evt_len <= 16'd0; evt_flags <= 7'd0;
            evt_start <= 1'b0; in_frame <= 1'b0;
        end else begin
            evt_done  <= 1'b0;
            evt_start <= 1'b0;
            sip_start <= 1'b0;
            sip_end   <= 1'b0;

            // ---- queue ----  push && pop only happens at sk_n == 1
            // (push needs room, pop needs a byte), so the combined case
            // is a straight head replacement.
            if (push && pop) begin
                sk0_d <= in_data; sk0_l <= in_last;
            end else if (push) begin
                if (sk_n == 2'd0) begin
                    sk0_d <= in_data; sk0_l <= in_last;
                end else begin
                    sk1_d <= in_data; sk1_l <= in_last;
                end
                sk_n <= sk_n + 2'd1;
            end else if (pop) begin
                sk0_d <= sk1_d; sk0_l <= sk1_l;
                sk_n <= sk_n - 2'd1;
            end

            // ---- parser ----
            if (consume) begin
                byte_cnt <= byte_cnt + 16'd1;
                if (feed_st)
                    l3_cnt <= l3_cnt + 16'd1;
                case (st)
                S_L2: begin
                    if (byte_last)
                        end_skip;
                    else if ((idx == 6'd0) && !key_valid) begin
                        frame_nokey <= 1'b1;
                        st <= S_SKIP;
                    end else if (idx == 6'd11) begin
                        st <= S_ET0; idx <= 6'd0;
                    end else
                        idx <= idx + 6'd1;
                end
                S_ET0: begin
                    et_hi <= byte_in;
                    if (byte_last) end_skip;
                    else           st <= S_ET1;
                end
                S_ET1: begin
                    if (byte_last)
                        end_skip;   // EtherType known, zero L3 bytes
                    else if ({et_hi, byte_in} == 16'h8100
                          || {et_hi, byte_in} == 16'h88A8)
                        st <= S_TCI0;
                    else if ({et_hi, byte_in} == 16'h0800) begin
                        ip_is6 <= 1'b0; flags[FB_IPV4] <= 1'b1;
                        sip_start <= 1'b1; st <= S_START;
                    end else if ({et_hi, byte_in} == 16'h86DD) begin
                        ip_is6 <= 1'b1; flags[FB_IPV6] <= 1'b1;
                        sip_start <= 1'b1; st <= S_START;
                    end else
                        st <= S_SKIP;
                end
                S_TCI0: begin
                    if (byte_last) end_skip;
                    else           st <= S_TCI1;
                end
                S_TCI1: begin
                    if (byte_last) end_skip;
                    else           st <= S_ET0;
                end
                S_IP4: begin
                    if (idx == 6'd0) ihl   <= byte_in[3:0];
                    if (idx == 6'd2) ip_tlen[15:8] <= byte_in;
                    if (idx == 6'd3) begin
                        ip_tlen[7:0] <= byte_in;
                        tlen_ok      <= 1'b1;
                    end
                    if (idx == 6'd9) proto <= byte_in;
                    if (idx >= 6'd20) flags[FB_OPT] <= 1'b1;
                    if (byte_last) begin
                        // Complete only if this byte closed the header
                        // AND the (unknown) L4 covers-all, i.e. the
                        // frame legitimately has zero L4 bytes — a
                        // known L4 proto still owes its checksum
                        // window.  The proto flag is classification,
                        // set whenever the proto byte is trustworthy
                        // (header complete), truncated or not.
                        if (!((idx == v4_last) && (proto != 8'd6)
                              && (proto != 8'd17) && (proto != 8'd1)))
                            flags[FB_TRUNC] <= 1'b1;
                        if (idx == v4_last) begin
                            case (proto)
                            8'd6:    flags[FB_TCP] <= 1'b1;
                            8'd17:   flags[FB_UDP] <= 1'b1;
                            8'd1:    ;           // ICMP: no flag
                            default: flags[FB_OTHER] <= 1'b1;
                            endcase
                        end
                        st <= S_LAST;
                    end else if (idx == v4_last) begin
                        idx <= 6'd0;
                        case (proto)
                        8'd6: begin
                            flags[FB_TCP] <= 1'b1;
                            ck_lo <= 5'd16; ck_hi <= 5'd17; st <= S_L4;
                        end
                        8'd17: begin
                            flags[FB_UDP] <= 1'b1;
                            ck_lo <= 5'd6; ck_hi <= 5'd7; st <= S_L4;
                        end
                        8'd1: begin      // ICMP: checksum masked, no flag
                            ck_lo <= 5'd2; ck_hi <= 5'd3; st <= S_L4;
                        end
                        default: begin
                            flags[FB_OTHER] <= 1'b1; st <= S_BODY;
                        end
                        endcase
                    end else
                        idx <= idx + 6'd1;
                end
                S_IP6: begin
                    if (idx == 6'd4) ip_tlen[15:8] <= byte_in;
                    if (idx == 6'd5) begin
                        ip_tlen[7:0] <= byte_in;
                        tlen_ok      <= 1'b1;
                    end
                    if (idx == 6'd6) proto <= byte_in;
                    if (byte_last) begin
                        if (!((idx == 6'd39) && (proto != 8'd6)
                              && (proto != 8'd17) && (proto != 8'd58)))
                            flags[FB_TRUNC] <= 1'b1;
                        if (idx == 6'd39) begin
                            case (proto)
                            8'd6:    flags[FB_TCP] <= 1'b1;
                            8'd17:   flags[FB_UDP] <= 1'b1;
                            8'd58:   ;           // ICMPv6: no flag
                            default: flags[FB_OTHER] <= 1'b1;
                            endcase
                        end
                        st <= S_LAST;
                    end else if (idx == 6'd39) begin
                        idx <= 6'd0;
                        case (proto)
                        8'd6: begin
                            flags[FB_TCP] <= 1'b1;
                            ck_lo <= 5'd16; ck_hi <= 5'd17; st <= S_L4;
                        end
                        8'd17: begin
                            flags[FB_UDP] <= 1'b1;
                            ck_lo <= 5'd6; ck_hi <= 5'd7; st <= S_L4;
                        end
                        8'd58: begin     // ICMPv6: checksum masked
                            ck_lo <= 5'd2; ck_hi <= 5'd3; st <= S_L4;
                        end
                        default: begin
                            flags[FB_OTHER] <= 1'b1; st <= S_BODY;
                        end
                        endcase
                    end else
                        idx <= idx + 6'd1;
                end
                S_L4: begin
                    if (byte_last) begin
                        if (idx != {1'b0, ck_hi})
                            flags[FB_TRUNC] <= 1'b1;
                        st <= S_LAST;
                    end else if (idx == {1'b0, ck_hi})
                        st <= S_BODY;
                    else
                        idx <= idx + 6'd1;
                end
                S_BODY: begin
                    if (byte_last) st <= S_LAST;
                end
                S_SKIP: begin
                    if (byte_last) end_skip;
                end
                default: ;
                endcase
            end

            // ---- non-consuming states ----
            case (st)
            S_START: begin
                // sip_start pulsed on entry; core initializes this
                // cycle, first IP byte can be absorbed the next.
                st  <= ip_is6 ? S_IP6 : S_IP4;
                idx <= 6'd0;
            end
            S_LAST: begin
                // The last byte may have launched a word compression;
                // finalize only once the core is back to accepting.
                if (sip_ready) begin
                    sip_end <= 1'b1;
                    st <= S_FIN;
                end
            end
            S_FIN: begin
                if (sip_dvld) begin
                    evt_done   <= 1'b1;
                    evt_kind   <= EV_DIGESTED;
                    evt_digest <= sip_dig;
                    evt_len    <= byte_cnt;
                    // bit6 also covers the length claim (see header)
                    evt_flags  <= len_short ? (flags | 7'b100_0000)
                                            : flags;
                    frame_reset;
                end
            end
            default: ;
            endcase

            // ---- frame-start observation (evt_start) ----
            // The first byte of a frame ACCEPTED (queued) opens it;
            // the event sites close it via frame_reset.  The lockstep
            // wrapper never pushes a new frame's byte on an event
            // cycle, so open/close can never collide in practice.
            if (push && !in_frame) begin
                in_frame  <= 1'b1;
                evt_start <= 1'b1;
            end
        end
    end

endmodule
`default_nettype wire
