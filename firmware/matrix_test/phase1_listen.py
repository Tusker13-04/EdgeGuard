#!/usr/bin/env python3
"""Phase 1 test: Listen for accelerometer data from the MCU via RouterBridge.
Uses MessagePack protocol with $/register as the actual bridge_receiver does."""
import socket
import select
import time
import sys

try:
    import msgpack
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "msgpack"])
    import msgpack

SOCK_PATH = "/var/run/arduino-router.sock"

def main():
    print("Connecting to arduino-router...")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    sock.connect(SOCK_PATH)
    print("Connected!")

    # Register for the notifications our test sketch sends
    # Format: [type=0 (request), id, method, params]
    reg1 = msgpack.packb([0, 1, "$/register", ["accel"]])
    reg2 = msgpack.packb([0, 2, "$/register", ["status"]])
    sock.sendall(reg1 + reg2)
    print("Registered for 'accel' and 'status' notifications.")

    unpacker = msgpack.Unpacker(raw=False)
    start = time.time()
    duration = 20  # listen for 20 seconds
    msg_count = 0

    print(f"Listening for {duration}s...")
    while time.time() - start < duration:
        ready, _, _ = select.select([sock], [], [], 0.5)
        if not ready:
            continue

        chunk = sock.recv(4096)
        if not chunk:
            print("Connection closed by router.")
            break

        unpacker.feed(chunk)
        for msg in unpacker:
            msg_count += 1
            elapsed = time.time() - start
            if isinstance(msg, list) and len(msg) >= 3:
                msg_type, method, params = msg[0], msg[1], msg[2]
                print(f"[{elapsed:.1f}s] type={msg_type} method={method} params={params}")
            else:
                print(f"[{elapsed:.1f}s] RAW: {msg}")

    sock.close()
    print(f"\nDone. Received {msg_count} messages in {duration}s.")

if __name__ == "__main__":
    main()
