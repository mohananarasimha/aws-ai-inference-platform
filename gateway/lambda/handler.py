import asyncio
import json
import time
import uuid

from agent import run_agent
from config import MAX_QUEUED_SECONDS, MODEL_MAP, VLLM_MODEL
from deadline import ComponentTimeout, DeadlineExceeded, request_budget
from metrics import emit_request_metrics
from rag import build_rag_context, retrieve
from vllm import ALLOWED_TARGETS, ERROR_STATUS, TARGET_EKS, VLLMError, connectivity_test


def api_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "content-type": "application/json"
        },
        "body": json.dumps(body, default=str)
    }


def find_error(exc, kind):
    """
    The MCP session runs inside an anyio task group, so an error raised in
    the agent loop can arrive wrapped in (nested) ExceptionGroups.
    """

    if isinstance(exc, kind):
        return exc

    for inner in getattr(exc, "exceptions", ()):
        found = find_error(inner, kind)

        if found is not None:
            return found

    return None


def find_vllm_error(exc):
    return find_error(exc, VLLMError)


def find_timeout_error(exc):
    return (
        find_error(exc, DeadlineExceeded)
        or find_error(exc, ComponentTimeout)
    )


def queued_seconds(event):
    """
    Seconds API Gateway had the request before this handler started (cold
    start, mostly). API Gateway's own limit counts from its receive time.
    """

    received_ms = (event.get("requestContext") or {}).get("timeEpoch")

    if not isinstance(received_ms, (int, float)):
        return 0.0

    return min(max(time.time() - received_ms / 1000, 0.0), MAX_QUEUED_SECONDS)


def timeout_response(request_id, error, deadline):
    """
    504 for a spent request budget or a component timeout. Only fixed
    strings and numbers: no prompt, URL, credential or stack trace.
    """

    step = getattr(error, "step", None) or deadline.step

    # A component that timed out because the budget ran out is a deadline error.
    exhausted = isinstance(error, DeadlineExceeded) or deadline.expired()

    code = (
        "request_deadline_exceeded"
        if exhausted
        else f"{error.component}_timeout"
    )

    details = {
        "component": error.component,
        "agent_step": step,
        "elapsed_ms": int(deadline.elapsed() * 1000),
        "budget_ms": int(deadline.budget * 1000)
    }

    print(json.dumps({
        "event": "request_timeout",
        "request_id": request_id,
        "status": "error",
        "error": code,
        **details,
        "remaining_ms": int(max(deadline.remaining(), 0) * 1000)
    }))

    return api_response(
        504,
        {
            "request_id": request_id,
            "error": code,
            **details
        }
    )


def vllm_error_response(request_id, error, latency_ms):
    cause = error.__cause__

    if error.code in ERROR_STATUS:
        error_type = "ConfigurationError"
    elif error.code == "vllm_invalid_response":
        error_type = "InvalidResponse"
    else:
        error_type = type(cause).__name__ if cause else "VLLMError"

    print(json.dumps({
        "event": "vllm_inference",
        "request_id": request_id,
        "status": "error",
        "model": VLLM_MODEL,
        "http_status": error.http_status,
        "latency_ms": latency_ms,
        "error_type": error_type
    }))

    return api_response(
        ERROR_STATUS.get(error.code, 502),
        {
            "request_id": request_id,
            "provider": "vllm",
            "model": VLLM_MODEL,
            "error": error.code,
            "error_type": error_type,
            "http_status": error.http_status,
            "latency_ms": latency_ms
        }
    )


def lambda_handler(event, context):
    started = time.time()
    outcome = {}

    response = _handle(event, context, outcome)

    emit_request_metrics(
        outcome,
        response,
        int((time.time() - started) * 1000)
    )

    return response


def _handle(event, context, outcome):
    start_time = time.time()
    request_id = str(uuid.uuid4())

    deadline = request_budget(context, queued_seconds(event))

    try:
        # --------------------------------
        # PARSE REQUEST
        # --------------------------------

        body = json.loads(
            event.get("body") or "{}"
        )

        headers = (
            event.get("headers")
            or {}
        )

        authorization_header = (
            headers.get("authorization")
            or headers.get("Authorization")
        )

        if not authorization_header:
            return api_response(
                401,
                {
                    "request_id": request_id,
                    "error": "authorization header missing"
                }
            )

        if body.get("provider") == "vllm_test":
            result, status_code = connectivity_test(request_id)

            return api_response(
                status_code,
                {
                    "request_id": request_id,
                    **result
                }
            )

        provider = body.get("provider") or "bedrock"

        if provider not in ("bedrock", "vllm"):
            return api_response(
                400,
                {
                    "request_id": request_id,
                    "error": "invalid_provider",
                    "allowed_providers": ["bedrock", "vllm"]
                }
            )

        outcome["provider"] = provider

        # Logical inference target for provider=vllm; never a URL.
        target = body.get("target") or TARGET_EKS

        if target not in ALLOWED_TARGETS:
            return api_response(
                400,
                {
                    "request_id": request_id,
                    "error": "invalid_target",
                    "allowed_targets": list(ALLOWED_TARGETS)
                }
            )

        prompt = body.get("prompt")

        model_class = body.get(
            "model_class",
            "fast"
        )

        if not prompt:
            return api_response(
                400,
                {
                    "request_id": request_id,
                    "error": "prompt is required"
                }
            )

        if provider == "vllm":
            model_id = VLLM_MODEL
        else:

            if model_class not in MODEL_MAP:
                return api_response(
                    400,
                    {
                        "request_id": request_id,
                        "error": "invalid_model_class",
                        "allowed_model_classes": list(
                            MODEL_MAP.keys()
                        )
                    }
                )

            model_id = MODEL_MAP[
                model_class
            ]

        identity = {"provider": provider}

        if provider == "bedrock":
            identity["model_class"] = model_class

        identity["model"] = model_id

        # --------------------------------
        # OPTIONAL RAG
        # --------------------------------

        rag = body.get("rag") is True

        model_prompt = prompt

        rag_metrics = {
            "rag_enabled": False
        }

        if rag:

            retrieval_start = time.time()

            try:

                retrieval = retrieve(prompt, deadline)

            except (DeadlineExceeded, ComponentTimeout) as exc:

                return timeout_response(request_id, exc, deadline)

            except Exception as exc:

                print(json.dumps({
                    "request_id": request_id,
                    "rag_enabled": True,
                    "status": "error",
                    "error": repr(exc)
                }))

                return api_response(
                    500,
                    {
                        "request_id": request_id,
                        "error": "retrieval_failed"
                    }
                )

            model_prompt, rag_metrics = build_rag_context(
                prompt,
                retrieval,
                retrieval_start
            )

        # --------------------------------
        # RUN AGENT
        # --------------------------------

        try:

            agent_result = asyncio.run(
                run_agent(
                    model_prompt=model_prompt,
                    model_id=model_id,
                    authorization_header=authorization_header,
                    request_id=request_id,
                    provider=provider,
                    deadline=deadline,
                    target=target
                )
            )

        except Exception as exc:

            timeout_error = find_timeout_error(exc)

            if timeout_error is not None:
                return timeout_response(request_id, timeout_error, deadline)

            vllm_error = find_vllm_error(exc)

            if vllm_error is None:
                raise

            if vllm_error.code == "vllm_timeout" and deadline.expired():
                return timeout_response(
                    request_id,
                    DeadlineExceeded("vllm", deadline.step),
                    deadline
                )

            return vllm_error_response(
                request_id,
                vllm_error,
                int((time.time() - start_time) * 1000)
            )

        outcome["model_latency_ms"] = agent_result["total_model_latency_ms"]

        latency_ms = int(
            (
                time.time()
                - start_time
            )
            * 1000
        )

        # --------------------------------
        # STRUCTURED LOG
        # --------------------------------

        print(json.dumps({
            "request_id": request_id,
            **identity,
            "agent_status": agent_result[
                "status"
            ],
            "agent_steps": agent_result[
                "agent_steps"
            ],
            "latency_ms": latency_ms,
            "total_latency_ms": latency_ms,
            "request_budget_ms": int(deadline.budget * 1000),
            "remaining_budget_ms": int(max(deadline.remaining(), 0) * 1000),
            "input_tokens": agent_result[
                "input_tokens"
            ],
            "output_tokens": agent_result[
                "output_tokens"
            ],
            "model_calls_ms": agent_result[
                "model_calls_ms"
            ],
            "total_model_latency_ms": agent_result[
                "total_model_latency_ms"
            ],
            "tool_calls": agent_result[
                "tool_calls"
            ],
            "status": {
                "approval_required": "approval_required",
                "approval_failed": "error"
            }.get(agent_result["status"], "success"),
            **agent_result["mcp_metrics"],
            **rag_metrics
        }))

        if agent_result["status"] == "approval_required":
            return api_response(
                202,
                {
                    "request_id": request_id,
                    "status": "approval_required",
                    "action": agent_result["approval"]["action"],
                    "approval_execution_arn": agent_result[
                        "approval"
                    ]["execution_arn"]
                }
            )

        if agent_result["status"] == "approval_failed":
            return api_response(
                500,
                {
                    "request_id": request_id,
                    "error": "approval_start_failed"
                }
            )

        # --------------------------------
        # API RESPONSE
        # --------------------------------

        return api_response(
            200,
            {
                "request_id": request_id,
                **identity,
                "response": agent_result[
                    "response"
                ],
                "agent_status": agent_result[
                    "status"
                ],
                "agent_steps": agent_result[
                    "agent_steps"
                ],
                "latency_ms": latency_ms,
                "input_tokens": agent_result[
                    "input_tokens"
                ],
                "output_tokens": agent_result[
                    "output_tokens"
                ],
                **rag_metrics
            }
        )

    except Exception as exc:

        latency_ms = int(
            (
                time.time()
                - start_time
            )
            * 1000
        )

        print(json.dumps({
            "request_id": request_id,
            "latency_ms": latency_ms,
            "total_latency_ms": latency_ms,
            "status": "error",
            "error": repr(exc)
        }))

        return api_response(
            500,
            {
                "request_id": request_id,
                "error": "agent_invocation_failed"
            }
        )
