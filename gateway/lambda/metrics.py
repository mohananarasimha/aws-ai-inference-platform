import json
import re
import time

from config import METRICS_NAMESPACE


# Dimension values come from fixed code sets; anything else is folded to
# "other" so a stray value can never create new metric series.
_SAFE_LABEL = re.compile(r"^[a-z0-9_]{1,64}$")


def _label(value):
    return value if isinstance(value, str) and _SAFE_LABEL.match(value) else "other"


def build_request_metrics(provider, status_code, body, latency_ms, model_latency_ms=None):
    """
    One CloudWatch Embedded Metric Format record for a finished request, or
    None when nothing should be counted (client errors, requests that never
    reached a provider).

    Metrics: RequestCount and RequestLatency by Provider and Status,
    ModelLatency by Provider, TimeoutCount by Provider/Component/Type and
    ErrorCount by Provider/ErrorType. Request IDs and anything user-supplied
    stay in the structured logs, not in dimensions.
    """

    if provider not in ("bedrock", "vllm"):
        return None

    if status_code == 200:
        status = "success"
    elif status_code == 202:
        status = "approval_required"
    elif status_code == 504:
        status = "timeout"
    elif status_code >= 500:
        status = "error"
    else:
        return None

    body = body if isinstance(body, dict) else {}
    error = body.get("error")

    record = {
        "event": "request_metrics",
        "request_id": body.get("request_id"),
        "Provider": provider,
        "Status": status,
        "RequestCount": 1,
        "RequestLatency": latency_ms
    }

    directives = [{
        "Namespace": METRICS_NAMESPACE,
        "Dimensions": [["Provider", "Status"]],
        "Metrics": [
            {"Name": "RequestCount", "Unit": "Count"},
            {"Name": "RequestLatency", "Unit": "Milliseconds"}
        ]
    }]

    if model_latency_ms is not None:
        record["ModelLatency"] = model_latency_ms

        directives.append({
            "Namespace": METRICS_NAMESPACE,
            "Dimensions": [["Provider"]],
            "Metrics": [{"Name": "ModelLatency", "Unit": "Milliseconds"}]
        })

    if status == "timeout":
        component = body.get("component")

        if component is None and isinstance(error, str) and error.endswith("_timeout"):
            component = error[:-len("_timeout")]

        record["Component"] = _label(component)
        record["Type"] = "deadline" if error == "request_deadline_exceeded" else "component"
        record["TimeoutCount"] = 1

        directives.append({
            "Namespace": METRICS_NAMESPACE,
            "Dimensions": [["Provider", "Component", "Type"]],
            "Metrics": [{"Name": "TimeoutCount", "Unit": "Count"}]
        })

    if status == "error":
        record["ErrorType"] = _label(error)
        record["ErrorCount"] = 1

        directives.append({
            "Namespace": METRICS_NAMESPACE,
            "Dimensions": [["Provider", "ErrorType"]],
            "Metrics": [{"Name": "ErrorCount", "Unit": "Count"}]
        })

    record["_aws"] = {
        "Timestamp": int(time.time() * 1000),
        "CloudWatchMetrics": directives
    }

    return record


def emit_request_metrics(outcome, response, latency_ms):
    """Print the EMF record. Metrics must never break a request."""

    try:

        record = build_request_metrics(
            outcome.get("provider"),
            response["statusCode"],
            json.loads(response["body"]),
            latency_ms,
            outcome.get("model_latency_ms")
        )

        if record is not None:
            print(json.dumps(record))

    except Exception:
        pass
