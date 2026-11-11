"""
Does vllm.request_chat honour its timeout against a hung or slow server?

    python3 scripts/repro/vllm_hang_probe.py

Local only. Two cases, each with a 3 s limit:
  1. the server accepts the connection and never answers (what a dead NLB
     target looks like): expected vllm_timeout after about 3 s;
  2. the server trickles one byte per second: urllib's timeout is per socket
     operation, not total, so this runs far past the limit (about 30 s).
Result recorded in docs/experiments/vllm-failure-recovery.md.
"""
import os
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gateway" / "lambda"))
import vllm  # noqa: E402


class FixedDeadline:
    step = 1

    def timeout(self, maximum, component):
        return min(maximum, 3)


def listener():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    return sock


# 1) accepts, never answers
hung = listener()
held = []
threading.Thread(target=lambda: [held.append(hung.accept()) for _ in range(5)], daemon=True).start()
os.environ["VLLM_BASE_URL"] = f"http://127.0.0.1:{hung.getsockname()[1]}"
start = time.time()
try:
    vllm.request_chat({"model": "x", "messages": []}, FixedDeadline())
except vllm.VLLMError as exc:
    print("accept-no-reply:", exc.code, round(time.time() - start, 1), "s (limit 3)")

# 2) trickle
slow = listener()


def trickle():
    conn, _ = slow.accept()
    conn.recv(65536)
    conn.sendall(b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: 100000\r\n\r\n")
    for _ in range(30):
        time.sleep(1)
        try:
            conn.sendall(b" ")
        except OSError:
            break


threading.Thread(target=trickle, daemon=True).start()
os.environ["VLLM_BASE_URL"] = f"http://127.0.0.1:{slow.getsockname()[1]}"
start = time.time()
try:
    vllm.request_chat({"model": "x", "messages": []}, FixedDeadline())
except vllm.VLLMError as exc:
    print("trickle:", exc.code, round(time.time() - start, 1), "s (limit 3)")
