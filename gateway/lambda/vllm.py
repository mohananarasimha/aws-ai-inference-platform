import http.client
import io
import json
import os
import socket
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

from config import (
    AWS_CONNECT_TIMEOUT_SECONDS,
    DISCOVERY_MAX_TIMEOUT_SECONDS,
    REGION,
    VLLM_MAX_TIMEOUT_SECONDS,
    VLLM_NLB_TAG_KEY,
    VLLM_NLB_TAG_VALUE,
)
from deadline import call_timeout


# Only the /v1/models diagnostic uses this; inference calls take
# min(VLLM_MAX_TIMEOUT_SECONDS, remaining request budget).
DIAGNOSTIC_TIMEOUT_SECONDS = 5
MAX_BODY_BYTES = 65536
MAX_BODY_CHARS = 2000

# Error codes that are not an upstream failure, with the HTTP status to return.
# Everything else from this module maps to 502.
ERROR_STATUS = {
    "vllm_endpoint_not_found": 503,
    "vllm_endpoint_ambiguous": 500,
    "vllm_invalid_target": 400,
    "vllm_onprem_not_configured": 503,
    "vllm_timeout": 504
}

# Logical inference targets a request may name. The caller never supplies a
# URL: "eks" is found by NLB discovery, "onprem" comes from the
# ONPREM_VLLM_BASE_URL environment variable (set by Terraform).
TARGET_EKS = "eks"
TARGET_ONPREM = "onprem"
ALLOWED_TARGETS = (TARGET_EKS, TARGET_ONPREM)

_discovered_base_url = None


# ---- HTTP with a total time limit -----------------------------------------------
# urllib's timeout is per socket operation: create_connection gives EVERY address
# a host resolves to the full timeout, getaddrinfo has no timeout at all, and each
# read restarts the clock. The vLLM NLB name resolves to one address per subnet,
# so an outage where connects go unanswered took several full timeouts and ran
# into the Lambda's 30 s kill. Here the timeout is the total for DNS, connecting
# (all addresses together), sending and reading the whole response.

_dns = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vllm-dns")


def _left(expires):
    seconds = expires - time.monotonic()

    if seconds <= 0:
        raise TimeoutError("timed out")

    return seconds


class _DeadlineReader(io.RawIOBase):
    """Reads from the socket with whatever is left of the total, never more."""

    def __init__(self, sock, expires):
        self._sock = sock
        self._raw = sock.makefile("rb", buffering=0)
        self._expires = expires

    def readable(self):
        return True

    def readinto(self, buffer):
        self._sock.settimeout(_left(self._expires))
        return self._raw.readinto(buffer)

    def close(self):
        self._raw.close()
        super().close()


class _DeadlineSocket:
    def __init__(self, sock, expires):
        self._sock = sock
        self._expires = expires

    def sendall(self, data):
        self._sock.settimeout(_left(self._expires))
        self._sock.sendall(data)

    def makefile(self, mode="rb", *args, **kwargs):
        return io.BufferedReader(_DeadlineReader(self._sock, self._expires))

    def __getattr__(self, name):
        return getattr(self._sock, name)


class _DeadlineHTTPConnection(http.client.HTTPConnection):

    def connect(self):
        expires = time.monotonic() + self.timeout
        addresses = _dns.submit(
            socket.getaddrinfo, self.host, self.port, 0, socket.SOCK_STREAM
        ).result(timeout=_left(expires))
        error = OSError("no address for vLLM host")

        for family, kind, proto, _, address in addresses:
            seconds = _left(expires)
            sock = socket.socket(family, kind, proto)

            try:
                sock.settimeout(seconds)
                sock.connect(address)
            except OSError as exc:
                sock.close()
                error = exc
                continue

            self.sock = _DeadlineSocket(sock, expires)
            return

        raise error


class _DeadlineHTTPHandler(urllib.request.HTTPHandler):

    def http_open(self, req):
        return self.do_open(_DeadlineHTTPConnection, req)


_opener = urllib.request.build_opener(_DeadlineHTTPHandler)


def _urlopen(request, timeout):
    """urllib.request.urlopen, but `timeout` bounds the whole exchange."""

    return _opener.open(request, timeout=timeout)


class VLLMError(Exception):
    """A failed vLLM call. The message is a short code and never holds prompt text."""

    def __init__(self, code, http_status=None):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def discover_base_url(timeout=DISCOVERY_MAX_TIMEOUT_SECONDS):
    """
    Find the internal NLB the load balancer controller created for the vllm
    Service (tagged service.k8s.aws/stack = default/vllm) and build its URL.
    The lookup goes through the router (NAT): each of its two AWS calls
    fails after `timeout` seconds at most, no retries.
    """

    discovery_config = Config(
        connect_timeout=min(AWS_CONNECT_TIMEOUT_SECONDS, timeout / 3),
        read_timeout=timeout / 2,
        retries={"total_max_attempts": 1}
    )

    try:

        tagging = boto3.client(
            "resourcegroupstaggingapi",
            region_name=REGION,
            config=discovery_config
        )

        mappings = tagging.get_resources(
            ResourceTypeFilters=["elasticloadbalancing:loadbalancer"],
            TagFilters=[{
                "Key": VLLM_NLB_TAG_KEY,
                "Values": [VLLM_NLB_TAG_VALUE]
            }]
        )["ResourceTagMappingList"]

        if len(mappings) > 1:
            raise VLLMError("vllm_endpoint_ambiguous")

        if not mappings:
            raise VLLMError("vllm_endpoint_not_found")

        balancers = boto3.client(
            "elbv2",
            region_name=REGION,
            config=discovery_config
        ).describe_load_balancers(
            LoadBalancerArns=[mappings[0]["ResourceARN"]]
        )["LoadBalancers"]

    except VLLMError:
        raise

    except Exception as exc:
        raise VLLMError("vllm_discovery_failed") from exc

    if not balancers or balancers[0]["State"]["Code"] != "active":
        raise VLLMError("vllm_endpoint_not_found")

    port = os.environ.get("VLLM_PORT", "8000")

    return f"http://{balancers[0]['DNSName']}:{port}"


def resolve_base_url(deadline=None, target=TARGET_EKS):
    """
    Where this request goes, in order:

    1. VLLM_BASE_URL (global override for debugging and tests) wins.
    2. target "onprem": ONPREM_VLLM_BASE_URL. Missing is a configuration
       error; it never falls back to EKS.
    3. target "eks" (the default): NLB discovery. A discovered URL is kept
       for the life of the container and dropped when a request cannot
       connect to it, so a recreated NLB is found again.
    4. Anything else is an error, never a silent fall back to EKS.
    """

    global _discovered_base_url

    override = os.environ.get("VLLM_BASE_URL")

    if override:
        return override

    if target not in ALLOWED_TARGETS:
        raise VLLMError("vllm_invalid_target")

    if target == TARGET_ONPREM:
        onprem_url = os.environ.get("ONPREM_VLLM_BASE_URL")

        if not onprem_url:
            raise VLLMError("vllm_onprem_not_configured")

        return onprem_url

    if _discovered_base_url is None:
        _discovered_base_url = discover_base_url(
            call_timeout(deadline, DISCOVERY_MAX_TIMEOUT_SECONDS, "vllm_discovery")
        )

    return _discovered_base_url


def forget_discovered_base_url():
    global _discovered_base_url

    _discovered_base_url = None


def connectivity_test(request_id):
    """
    Temporary check that this Lambda can reach the self-hosted vLLM
    server: GET {base URL}/v1/models. One attempt, no retries,
    no fallback. Returns (response_body, http_status_code).
    """

    try:
        base_url = resolve_base_url()
    except VLLMError as exc:
        print(json.dumps({
            "event": "vllm_connectivity_test",
            "request_id": request_id,
            "status": "error",
            "error": exc.code
        }))

        return {
            "provider": "vllm_test",
            "status": "error",
            "error": exc.code
        }, ERROR_STATUS.get(exc.code, 502)

    url = base_url.rstrip("/") + "/v1/models"

    http_status = None
    body_text = None
    error_type = None
    error = None

    start_time = time.time()

    try:

        with _urlopen(
            urllib.request.Request(url, method="GET"),
            timeout=DIAGNOSTIC_TIMEOUT_SECONDS
        ) as response:

            http_status = response.status

            body_text = response.read(
                MAX_BODY_BYTES
            ).decode("utf-8", errors="replace")

    except urllib.error.HTTPError as exc:

        http_status = exc.code
        error_type = type(exc).__name__
        error = str(exc)[:200]

        body_text = exc.read(
            MAX_BODY_BYTES
        ).decode("utf-8", errors="replace")

    except Exception as exc:

        error_type = type(exc).__name__
        error = str(exc)[:200]

    latency_ms = int((time.time() - start_time) * 1000)

    succeeded = (
        error_type is None
        and http_status is not None
        and 200 <= http_status < 300
    )

    result = {
        "provider": "vllm_test",
        "status": "ok" if succeeded else "error",
        "url": url,
        "http_status": http_status,
        "latency_ms": latency_ms
    }

    models = None

    if body_text:

        try:

            parsed = json.loads(body_text)

            if isinstance(parsed, dict) and isinstance(parsed.get("data"), list):
                models = [
                    model.get("id")
                    for model in parsed["data"]
                    if isinstance(model, dict)
                ]

        except ValueError:
            pass

    if models is not None:
        result["models"] = models
    elif body_text:
        result["body"] = body_text[:MAX_BODY_CHARS]

    if error_type is not None:
        result["error_type"] = error_type
        result["error"] = error

    log = {
        "event": "vllm_connectivity_test",
        "request_id": request_id,
        "status": result["status"],
        "url": url,
        "http_status": http_status,
        "latency_ms": latency_ms
    }

    if models is not None:
        log["models_count"] = len(models)

    if error_type is not None:
        log["error_type"] = error_type
        log["error"] = error

    print(json.dumps(log))

    return result, 200 if succeeded else 502


def is_timeout(exc):
    if isinstance(exc, urllib.error.URLError):
        exc = exc.reason

    return isinstance(exc, TimeoutError)


def request_chat(payload, deadline=None, target=TARGET_EKS):
    """
    POST one OpenAI-style chat completion request to vLLM and return the
    parsed JSON body. One attempt, no retries, no fallback. Used by the
    model adapter (model.py). `target` is a logical name (see
    resolve_base_url), never a URL.
    """

    base_url = resolve_base_url(deadline, target)

    # Taken after discovery, so the call only gets what discovery left.
    timeout = call_timeout(deadline, VLLM_MAX_TIMEOUT_SECONDS, "vllm")

    http_request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "accept": "application/json"
        },
        method="POST"
    )

    try:

        with _urlopen(
            http_request,
            timeout=timeout
        ) as response:

            body_text = response.read(
                MAX_BODY_BYTES * 16
            ).decode("utf-8", errors="replace")

    except urllib.error.HTTPError as exc:

        raise VLLMError(
            "vllm_request_failed"
            if 400 <= exc.code < 500
            else "vllm_unavailable",
            http_status=exc.code
        ) from exc

    except Exception as exc:

        # Only the discovered EKS address is cached; an on-prem failure must
        # not drop it.
        if target == TARGET_EKS:
            forget_discovered_base_url()

        raise VLLMError(
            "vllm_timeout" if is_timeout(exc) else "vllm_unavailable"
        ) from exc

    try:

        return json.loads(body_text)

    except ValueError as exc:

        raise VLLMError("vllm_invalid_response") from exc
