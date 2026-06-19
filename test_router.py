import socket
import msgpack
import time
import select

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect("/var/run/arduino-router.sock")

print("Sending register commands...")
req1 = msgpack.packb([0, 1, "$/register", ["sensor_point"]])
req2 = msgpack.packb([0, 2, "$/register", ["anomaly_trigger"]])
sock.sendall(req1 + req2)

unpacker = msgpack.Unpacker()
while True:
    ready, _, _ = select.select([sock], [], [], 1.0)
    if not ready:
        print("Timeout, waiting...")
        continue
    
    data = sock.recv(4096)
    if not data:
        print("Connection closed")
        break
    unpacker.feed(data)
    for msg in unpacker:
        print("Received:", msg)
