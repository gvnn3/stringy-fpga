#!/usr/bin/env python3
"""Hand-rolled pcaps for SNORT-PF S1 (sid 1927): established FTP session,
client->server data segment carrying (or not) 'authorized_keys'."""
import struct
import sys

CLI_MAC = bytes.fromhex("020000000001")
SRV_MAC = bytes.fromhex("020000000002")
CLI_IP = bytes([192, 168, 50, 10])   # $EXTERNAL_NET=any by default config
SRV_IP = bytes([10, 0, 0, 21])
CLI_PORT, SRV_PORT = 40222, 21


def csum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    s = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def tcp_pkt(src_ip, dst_ip, sport, dport, seq, ack, flags, payload=b""):
    tcp = struct.pack("!HHIIBBHHH", sport, dport, seq, ack,
                      (5 << 4), flags, 65535, 0, 0) + payload
    pseudo = src_ip + dst_ip + struct.pack("!BBH", 0, 6, len(tcp))
    tcp = tcp[:16] + struct.pack("!H", csum(pseudo + tcp)) + tcp[18:]
    ip = struct.pack("!BBHHHBBH", 0x45, 0, 20 + len(tcp), 0x1234, 0,
                     64, 6, 0) + src_ip + dst_ip
    ip = ip[:10] + struct.pack("!H", csum(ip)) + ip[12:]
    if src_ip == CLI_IP:
        eth = SRV_MAC + CLI_MAC + b"\x08\x00"
    else:
        eth = CLI_MAC + SRV_MAC + b"\x08\x00"
    return eth + ip + tcp


def session(client_payload: bytes):
    seq_c, seq_s = 1000, 5000
    pkts = [
        tcp_pkt(CLI_IP, SRV_IP, CLI_PORT, SRV_PORT, seq_c, 0, 0x02),          # SYN
        tcp_pkt(SRV_IP, CLI_IP, SRV_PORT, CLI_PORT, seq_s, seq_c + 1, 0x12),  # SYN+ACK
        tcp_pkt(CLI_IP, SRV_IP, CLI_PORT, SRV_PORT, seq_c + 1, seq_s + 1, 0x10),  # ACK
    ]
    banner = b"220 ftp ready\r\n"
    pkts.append(tcp_pkt(SRV_IP, CLI_IP, SRV_PORT, CLI_PORT, seq_s + 1,
                        seq_c + 1, 0x18, banner))
    pkts.append(tcp_pkt(CLI_IP, SRV_IP, CLI_PORT, SRV_PORT, seq_c + 1,
                        seq_s + 1 + len(banner), 0x18, client_payload))
    pkts.append(tcp_pkt(SRV_IP, CLI_IP, SRV_PORT, CLI_PORT,
                        seq_s + 1 + len(banner),
                        seq_c + 1 + len(client_payload), 0x10))
    return pkts


def write_pcap(path, pkts):
    with open(path, "wb") as f:
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        ts = 1700000000
        for i, p in enumerate(pkts):
            f.write(struct.pack("<IIII", ts, i * 1000, len(p), len(p)))
            f.write(p)


write_pcap(sys.argv[1], session(b"RETR AuthoRized_Keys\r\n"))   # mixed case: nocase must carry it
write_pcap(sys.argv[2], session(b"RETR notes_from_today.txt\r\n"))
print("wrote", sys.argv[1], sys.argv[2])
