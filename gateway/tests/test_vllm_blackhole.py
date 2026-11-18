"""
The vLLM call must end within its timeout however the network fails.

Reproduces the node-failure outage: the internal NLB name resolves to one
address per subnet (five in this lab) and, with no healthy target, connects
to it are never answered. urllib gives EVERY resolved address the full
timeout, so three dead addresses take three timeouts and the Lambda is killed
at 30 s before the handler can answer.

Real sockets and real time, kept short. TEST-NET addresses (192.0.2.0/24)
stand in for the NLB; their connects are made to hang like an unanswered SYN.
"""

import json
import socket
import threading
import time

import pytest

import deadline as deadline_module
import handler
import vllm

NLB_HOST = "vllm-nlb.test"
DEAD = ("192.0.2.1", "192.0.2.2", "192.0.2.3")
SLACK = 0.3  # scheduling noise allowed on top of a limit


class FixedDeadline:
    """A deadline that always allows `seconds` for the call."""

    step = 1

    def __init__(self, seconds):
        self.seconds = seconds

    def timeout(self, maximum, component):
        return min(maximum, self.seconds)


@pytest.fixture
def blackholed_nlb(monkeypatch):
    """NLB_HOST resolves to three addresses whose connects are never answered.
    Returns the per-attempt socket timeouts that were applied."""
    attempts = []
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect

    def getaddrinfo(host, port, *args, **kwargs):
        if host == NLB_HOST:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, int(port))) for ip in DEAD]
        return real_getaddrinfo(host, port, *args, **kwargs)

    def connect(self, address):
        if address[0] in DEAD:
            attempts.append(self.gettimeout())
            time.sleep(self.gettimeout())
            raise TimeoutError("timed out")
        return real_connect(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setenv("VLLM_BASE_URL", f"http://{NLB_HOST}:8000")
    return attempts


def timed_request(limit):
    start = time.monotonic()
    with pytest.raises(vllm.VLLMError) as err:
        vllm.request_chat({"model": "m", "messages": []}, FixedDeadline(limit))
    return time.monotonic() - start, err.value


def test_unanswered_connects_to_every_nlb_address_stay_within_the_timeout(blackholed_nlb):
    elapsed, error = timed_request(0.4)

    assert error.code == "vllm_timeout"
    assert elapsed < 0.4 + SLACK, f"took {elapsed:.2f}s for a 0.4s timeout"
    # no single attempt was given more than the call's budget, and together they fit in it
    assert all(t <= 0.4 for t in blackholed_nlb)
    assert sum(blackholed_nlb) <= 0.4 + 0.01


def test_handler_returns_request_deadline_exceeded_not_a_lambda_kill(monkeypatch, blackholed_nlb, fake_mcp):
    monkeypatch.setattr(deadline_module, "REQUEST_TIMEOUT_SECONDS", 1.5)
    event = {"headers": {"authorization": "Bearer x"},
             "body": json.dumps({"provider": "vllm", "prompt": "hello"})}

    start = time.monotonic()
    response = handler.lambda_handler(event, None)
    elapsed = time.monotonic() - start
    body = json.loads(response["body"])

    assert elapsed < 1.5 + SLACK, f"handler took {elapsed:.2f}s for a 1.5s budget"
    assert response["statusCode"] == 504
    assert body["error"] == "request_deadline_exceeded"
    assert body["component"] == "vllm"


def test_dns_lookup_that_hangs_is_bounded(monkeypatch):
    real_getaddrinfo = socket.getaddrinfo

    def slow(host, *args, **kwargs):
        if host == "slow-dns.test":
            time.sleep(2)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    monkeypatch.setenv("VLLM_BASE_URL", "http://slow-dns.test:8000")

    elapsed, error = timed_request(0.4)

    assert error.code == "vllm_timeout"
    assert elapsed < 0.4 + SLACK


def test_response_that_trickles_is_bounded_in_total(monkeypatch):
    """Each read gets a byte before the per-read timeout, so only a total limit stops it."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    stop = threading.Event()

    def serve():
        conn, _ = server.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n")
        for _ in range(30):  # at most 3 s, so the unfixed code still finishes
            if stop.wait(0.1):
                break
            try:
                conn.sendall(b" ")
            except OSError:
                break
        conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setenv("VLLM_BASE_URL", f"http://127.0.0.1:{server.getsockname()[1]}")

    try:
        elapsed, error = timed_request(0.5)
    finally:
        stop.set()
        thread.join(5)
        server.close()

    assert error.code == "vllm_timeout"
    assert elapsed < 0.5 + SLACK
