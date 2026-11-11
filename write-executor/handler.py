import json
import os
import time

import boto3


lambda_client = boto3.client("lambda")

TARGET_FUNCTION_NAME = os.environ["TARGET_FUNCTION_NAME"]

MIN_MEMORY_MB = 128
MAX_MEMORY_MB = 10240

ALLOWED_ARGUMENTS = {"function_name", "memory_size"}

UPDATE_POLL_ATTEMPTS = 30
UPDATE_POLL_SECONDS = 2


class ValidationError(Exception):
    pass


class VerificationError(Exception):
    pass


def validate_action(event):
    action = event.get("action") if isinstance(event, dict) else None

    if not isinstance(action, dict):
        raise ValidationError("action is missing")

    if action.get("tool") != "update_lambda_memory":
        raise ValidationError("unsupported tool")

    arguments = action.get("arguments")

    if not isinstance(arguments, dict):
        raise ValidationError("arguments are missing")

    if set(arguments) != ALLOWED_ARGUMENTS:
        raise ValidationError("arguments must be exactly function_name and memory_size")

    if arguments["function_name"] != TARGET_FUNCTION_NAME:
        raise ValidationError("function_name is not the allowed target")

    memory_size = arguments["memory_size"]

    if isinstance(memory_size, bool) or not isinstance(memory_size, int):
        raise ValidationError("memory_size must be an integer")

    if not MIN_MEMORY_MB <= memory_size <= MAX_MEMORY_MB:
        raise ValidationError(
            f"memory_size must be between {MIN_MEMORY_MB} and {MAX_MEMORY_MB}"
        )

    return memory_size


def wait_for_update(function_name):
    for _ in range(UPDATE_POLL_ATTEMPTS):
        config = lambda_client.get_function_configuration(
            FunctionName=function_name
        )

        status = config.get("LastUpdateStatus")

        if status == "Successful":
            return config

        if status == "Failed":
            raise VerificationError(
                f"Lambda update failed: {config.get('LastUpdateStatusReason')}"
            )

        time.sleep(UPDATE_POLL_SECONDS)

    raise VerificationError("Lambda update did not finish in time")


def lambda_handler(event, context):
    request_id = event.get("request_id") if isinstance(event, dict) else None

    try:
        memory_size = validate_action(event)

        previous = lambda_client.get_function_configuration(
            FunctionName=TARGET_FUNCTION_NAME
        )["MemorySize"]

        lambda_client.update_function_configuration(
            FunctionName=TARGET_FUNCTION_NAME,
            MemorySize=memory_size,
        )

        verified = wait_for_update(TARGET_FUNCTION_NAME)["MemorySize"]

        if verified != memory_size:
            raise VerificationError(
                f"requested {memory_size} MB but function reports {verified} MB"
            )

    except Exception as exc:
        print(json.dumps({
            "request_id": request_id,
            "tool": "update_lambda_memory",
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }))
        raise

    result = {
        "status": "success",
        "tool": "update_lambda_memory",
        "function_name": TARGET_FUNCTION_NAME,
        "previous_memory_size": previous,
        "requested_memory_size": memory_size,
        "verified_memory_size": verified,
    }

    print(json.dumps({"request_id": request_id, **result}))

    return result
