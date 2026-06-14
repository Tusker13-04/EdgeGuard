import pytest
import struct
import socket
import threading
import time
from unittest.mock import MagicMock, patch
from src.udp_receiver import PacketParser, UDPReceiver, parse_payload, PACKET_FORMAT

def make_packet(ts=1000, seq=1, ax=1.0, ay=2.0, az=3.0, temp=25.0):
    return struct.pack(PACKET_FORMAT, ts, seq, ax, ay, az, temp)

def test_packet_parser_bad_length():
    parser = PacketParser()
    assert parser.parse(b"123") is None

def test_packet_parser_implausible_accel():
    parser = PacketParser()
    pkt = make_packet(ax=500.0)
    assert parser.parse(pkt) is None

def test_packet_parser_implausible_temp():
    parser = PacketParser()
    pkt = make_packet(temp=200.0)
    result = parser.parse(pkt)
    assert result[2][3] == 25.0 # default last known

    parser._last_temp_c = 30.0
    result = parser.parse(pkt)
    assert result[2][3] == 30.0 # uses last known

def test_packet_parser_jitter_and_drop():
    parser = PacketParser()
    pkt1 = make_packet(seq=1)
    parser.parse(pkt1)
    
    pkt2 = make_packet(seq=5) # 3 dropped
    ts, seq, features, jitter, dropped = parser.parse(pkt2)
    assert dropped == 3
    assert parser.total_dropped == 3

def test_packet_parser_implausible_gap():
    parser = PacketParser()
    parser.parse(make_packet(seq=1))
    
    # massive gap
    ts, seq, features, jitter, dropped = parser.parse(make_packet(seq=100000))
    assert dropped == 0 # treated as reboot

def test_packet_parser_properties():
    parser = PacketParser()
    assert parser.drop_rate_pct == 0.0
    
    parser.parse(make_packet(seq=1, temp=40.0))
    assert parser.last_temp_c == 40.0
    
    parser.parse(make_packet(seq=3)) # 1 dropped
    # 2 received, 1 dropped => drop rate = 1/3 = 33.3%
    assert 33.0 < parser.drop_rate_pct < 34.0

def test_parse_payload():
    pkt = make_packet(ts=10, seq=2, ax=1, ay=2, az=3, temp=25)
    ts, seq, features = parse_payload(pkt)
    assert ts == 10
    assert seq == 2
    assert features.shape == (4,)
    
    with pytest.raises(ValueError):
        parse_payload(b"bad")

@patch("socket.socket")
def test_udp_receiver_run(mock_socket_cls):
    mock_socket = MagicMock()
    mock_socket_cls.return_value = mock_socket
    
    # Simulate recvfrom returning a valid packet, then a bad packet, then a timeout, then an error, then stop
    pkt = make_packet()
    
    def side_effect(*args):
        if side_effect.count == 0:
            side_effect.count += 1
            return pkt, ("127.0.0.1", 1234)
        elif side_effect.count == 1:
            side_effect.count += 1
            return b"bad", ("127.0.0.1", 1234)
        elif side_effect.count == 2:
            side_effect.count += 1
            raise socket.timeout()
        elif side_effect.count == 3:
            side_effect.count += 1
            raise OSError("test error")
        else:
            stop_event.set()
            raise OSError("stop")
            
    side_effect.count = 0
    mock_socket.recvfrom.side_effect = side_effect
    
    receiver = UDPReceiver()
    buf = MagicMock()
    stop_event = threading.Event()
    
    receiver.run(buf, stop_event)
    
    # buf.add_row should be called once for the valid packet
    assert buf.add_row.call_count == 1
    assert receiver.last_temp_c == 25.0
    assert receiver.drop_rate_pct == 0.0
