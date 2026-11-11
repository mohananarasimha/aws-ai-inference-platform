import json
import time

import boto3
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "enterprise-ai-readonly",
    host="0.0.0.0",
    stateless_http=True,
)


def get_lambda_client():
    return boto3.client("lambda", region_name="us-east-1")


def get_logs_client():
    return boto3.client("logs", region_name="us-east-1")


def log_aws_call(tool, aws_api, start, error=None):
    entry = {
        "tool": tool,
        "aws_api": aws_api,
        "aws_api_latency_ms": int((time.time() - start) * 1000),
        "status": "success" if error is None else "error",
    }

    if error is not None:
        entry["error_type"] = type(error).__name__

    print(json.dumps(entry), flush=True)


@mcp.tool()
def get_lambda_configuration(function_name: str) -> dict:
    """Return basic configuration for a Lambda function."""

    lambda_client = get_lambda_client()

    start = time.time()

    try:
        response = lambda_client.get_function_configuration(
            FunctionName=function_name
        )
    except Exception as exc:
        log_aws_call(
            "get_lambda_configuration",
            "lambda.get_function_configuration",
            start,
            exc,
        )
        raise

    log_aws_call(
        "get_lambda_configuration",
        "lambda.get_function_configuration",
        start,
    )

    return {
        "function_name": response.get("FunctionName"),
        "runtime": response.get("Runtime"),
        "memory_mb": response.get("MemorySize"),
        "timeout_seconds": response.get("Timeout"),
        "state": response.get("State"),
        "last_update_status": response.get("LastUpdateStatus"),
    }


@mcp.tool()
def get_recent_lambda_logs(
    function_name: str,
    limit: int = 20,
) -> list[str]:
    """Return recent CloudWatch log messages for a Lambda function."""

    logs_client = get_logs_client()

    start = time.time()

    try:
        response = logs_client.filter_log_events(
            logGroupName=f"/aws/lambda/{function_name}",
            limit=limit,
        )
    except Exception as exc:
        log_aws_call(
            "get_recent_lambda_logs",
            "logs.filter_log_events",
            start,
            exc,
        )
        raise

    log_aws_call(
        "get_recent_lambda_logs",
        "logs.filter_log_events",
        start,
    )

    return [
        event["message"]
        for event in response.get("events", [])
    ]


@mcp.tool()
def get_lambda_errors(
    function_name: str,
    limit: int = 20,
) -> list[str]:
    """Return recent error-like log messages for a Lambda function."""

    logs_client = get_logs_client()

    start = time.time()

    try:
        response = logs_client.filter_log_events(
            logGroupName=f"/aws/lambda/{function_name}",
            filterPattern="?ERROR ?Error ?Exception ?error",
            limit=limit,
        )
    except Exception as exc:
        log_aws_call(
            "get_lambda_errors",
            "logs.filter_log_events",
            start,
            exc,
        )
        raise

    log_aws_call(
        "get_lambda_errors",
        "logs.filter_log_events",
        start,
    )

    return [
        event["message"]
        for event in response.get("events", [])
    ]


if __name__ == "__main__":
    mcp.run(transport="streamable-http")