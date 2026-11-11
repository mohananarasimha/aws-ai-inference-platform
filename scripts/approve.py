#!/usr/bin/env python3
"""
Terminal approval for the Step Functions approval workflow.

Reads ONE approval request from the approval SQS queue, shows the exact
proposed action, asks yes/no, then answers the waiting Step Functions
execution:

    yes -> SendTaskSuccess {"decision": "approved"}   (executor runs)
    no  -> SendTaskSuccess {"decision": "rejected"}   (execution ends Rejected)

The action is shown and forwarded exactly as it arrived in the message.
Nothing here regenerates or edits it.

Queue URL lookup order:
    1. --queue-url
    2. APPROVAL_QUEUE_URL environment variable
    3. `terraform output -raw approval_queue_url` in terraform/environments/dev/app

AWS credentials come from the normal boto3 chain (e.g. AWS_PROFILE=personal).
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


TERRAFORM_DIR = (
    Path(__file__).resolve().parent.parent
    / "terraform" / "environments" / "dev" / "app"
)

# Hold the message while a human decides, so it is not redelivered.
VISIBILITY_TIMEOUT_SECONDS = 900

# The task can no longer be answered (already answered or timed out).
STALE_TASK_ERRORS = {"TaskTimedOut", "TaskDoesNotExist", "InvalidToken"}


def resolve_queue_url(cli_value):
    if cli_value:
        return cli_value

    if os.environ.get("APPROVAL_QUEUE_URL"):
        return os.environ["APPROVAL_QUEUE_URL"]

    try:
        result = subprocess.run(
            ["terraform", "output", "-raw", "approval_queue_url"],
            cwd=TERRAFORM_DIR,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        sys.exit(
            "Could not determine the approval queue URL.\n"
            "Pass --queue-url, set APPROVAL_QUEUE_URL, or run terraform apply.\n"
            f"({exc})"
        )

    return result.stdout.strip()


def region_from_queue_url(queue_url):
    match = re.match(r"https://sqs\.([a-z0-9-]+)\.amazonaws\.com", queue_url)
    return match.group(1) if match else None


def receive_one(sqs, queue_url):
    print("Waiting for an approval request... (Ctrl-C to quit)")

    while True:
        response = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            VisibilityTimeout=VISIBILITY_TIMEOUT_SECONDS,
        )

        messages = response.get("Messages")

        if messages:
            return messages[0]


def parse_request(message):
    request = json.loads(message["Body"])

    for key in ("request_id", "action", "reason", "task_token"):
        if key not in request:
            raise ValueError(f"approval message is missing '{key}'")

    return request


def show_request(request):
    action = request["action"]

    print()
    print("=" * 60)
    print(f"Request ID: {request['request_id']}")
    print(f"Tool:       {action.get('tool')}")
    print("Arguments:")
    print(json.dumps(action.get("arguments"), indent=2))
    print(f"Reason:     {request['reason']}")
    print("=" * 60)


def ask_approval():
    while True:
        answer = input("Approve? yes/no: ").strip().lower()

        if answer == "yes":
            return True

        if answer == "no":
            return False

        print("Please type 'yes' or 'no'.")


def caller_arn():
    try:
        return boto3.client("sts").get_caller_identity()["Arn"]
    except Exception:
        return "unknown"


def answer_workflow(sfn, request, approved):
    sfn.send_task_success(
        taskToken=request["task_token"],
        output=json.dumps({
            "decision": "approved" if approved else "rejected",
            "approver": caller_arn(),
        }),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--queue-url", help="approval SQS queue URL")
    args = parser.parse_args(argv)

    queue_url = resolve_queue_url(args.queue_url)
    region = region_from_queue_url(queue_url)

    sqs = boto3.client("sqs", region_name=region)
    sfn = boto3.client("stepfunctions", region_name=region)

    message = receive_one(sqs, queue_url)

    try:
        request = parse_request(message)
    except (ValueError, KeyError, TypeError) as exc:
        print(f"Skipping malformed approval message: {exc}", file=sys.stderr)
        print("The message was left on the queue.", file=sys.stderr)
        return 1

    show_request(request)

    approved = ask_approval()

    try:
        answer_workflow(sfn, request, approved)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]

        if code not in STALE_TASK_ERRORS:
            raise

        print(
            f"This request can no longer be answered ({code}); "
            "it was probably answered already or timed out."
        )
    else:
        print("Approved: workflow resumed." if approved
              else "Rejected: workflow resumed.")

    sqs.delete_message(
        QueueUrl=queue_url,
        ReceiptHandle=message["ReceiptHandle"],
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
