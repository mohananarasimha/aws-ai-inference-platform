import json
import os
import re

from clients import aws_client
from config import APPROVAL_MAX_TIMEOUT_SECONDS
from deadline import call_timeout

# Tools that are never executed by the agent. Nova may propose them;
# the proposal is sent to the human approval workflow instead.
WRITE_TOOL_NAMES = {"update_lambda_memory"}


def write_tool_specs():
    """
    Bedrock toolSpecs for approval-required write tools.

    These tools are not exposed by the MCP server (it is read-only), so
    Nova only learns about them from here.
    """

    function_name_schema = {
        "type": "string",
        "description": "Name of the Lambda function to change"
    }

    own_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")

    if own_name:
        function_name_schema["enum"] = [own_name]

    return [{
        "toolSpec": {
            "name": "update_lambda_memory",
            "description": (
                "Change the memory size of the AI gateway Lambda function. "
                "This does not run immediately: the change is sent to a "
                "human for approval first."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "function_name": function_name_schema,
                        "memory_size": {
                            "type": "integer",
                            "description": "New memory size in MB (128-10240)"
                        }
                    },
                    "required": ["function_name", "memory_size"]
                }
            }
        }
    }]


def proposal_reason(model_message, tool_name):
    """
    Short reason for the approver: the model's own text around the
    tool request if there is any, otherwise a fixed fallback.
    """

    text = " ".join(
        block["text"]
        for block in model_message["content"]
        if "text" in block
    )

    text = re.sub(r"</?thinking>", " ", text)
    text = " ".join(text.split())

    if not text:
        return f"Agent proposed {tool_name}"

    return text[:300]


def start_approval(request_id, tool_name, tool_input, reason, deadline=None):
    """
    Start the approval workflow with the exact proposed action.
    Does not wait for the human. Returns the execution ARN.
    """

    stepfunctions = aws_client(
        "stepfunctions",
        call_timeout(deadline, APPROVAL_MAX_TIMEOUT_SECONDS, "approval")
    )

    response = stepfunctions.start_execution(
        stateMachineArn=os.environ["APPROVAL_STATE_MACHINE_ARN"],
        name=request_id,
        input=json.dumps({
            "request_id": request_id,
            "action": {
                "tool": tool_name,
                "arguments": tool_input
            },
            "reason": reason
        })
    )

    return response["executionArn"]
