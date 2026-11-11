resource "aws_sqs_queue" "approval" {
  name                       = "${local.name}-approval"
  message_retention_seconds  = 86400
  visibility_timeout_seconds = 60
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = true
}

data "aws_iam_policy_document" "approval_workflow_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "approval_workflow" {
  name               = "${local.name}-approval-workflow"
  assume_role_policy = data.aws_iam_policy_document.approval_workflow_assume.json
}

data "aws_iam_policy_document" "approval_workflow" {
  statement {
    sid       = "SendApprovalRequests"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.approval.arn]
  }

  statement {
    sid     = "InvokeWriteExecutor"
    actions = ["lambda:InvokeFunction"]
    resources = [
      aws_lambda_function.write_executor.arn,
      "${aws_lambda_function.write_executor.arn}:*",
    ]
  }
}

resource "aws_iam_role_policy" "approval_workflow" {
  name   = "approval_workflow_permissions"
  role   = aws_iam_role.approval_workflow.id
  policy = data.aws_iam_policy_document.approval_workflow.json
}

resource "aws_sfn_state_machine" "approval" {
  name     = "${local.name}-approval"
  role_arn = aws_iam_role.approval_workflow.arn
  type     = "STANDARD"

  definition = jsonencode({
    Comment = "Human approval gate: waits for a task-token callback from the approval queue"
    StartAt = "RequestApproval"
    States = {
      RequestApproval = {
        Type           = "Task"
        Resource       = "arn:aws:states:::sqs:sendMessage.waitForTaskToken"
        TimeoutSeconds = 86400
        Parameters = {
          QueueUrl = aws_sqs_queue.approval.url
          MessageBody = {
            "request_id.$" = "$.request_id"
            "action.$"     = "$.action"
            "reason.$"     = "$.reason"
            "task_token.$" = "$$.Task.Token"
          }
        }
        ResultPath = "$.approval"
        Catch = [
          {
            ErrorEquals = ["States.Timeout"]
            ResultPath  = "$.error"
            Next        = "Expired"
          }
        ]
        Next = "CheckDecision"
      }

      CheckDecision = {
        Type = "Choice"
        Choices = [
          {
            Variable  = "$.approval.decision"
            IsPresent = false
            Next      = "InvalidDecision"
          },
          {
            Variable     = "$.approval.decision"
            StringEquals = "approved"
            Next         = "InvokeWriteExecutor"
          },
          {
            Variable     = "$.approval.decision"
            StringEquals = "rejected"
            Next         = "Rejected"
          }
        ]
        Default = "InvalidDecision"
      }

      InvokeWriteExecutor = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.write_executor.arn
          Payload = {
            "request_id.$" = "$.request_id"
            "action.$"     = "$.action"
          }
        }
        ResultSelector = {
          "result.$" = "$.Payload"
        }
        ResultPath = "$.execution"
        Retry = [
          {
            ErrorEquals = [
              "Lambda.ServiceException",
              "Lambda.AWSLambdaException",
              "Lambda.SdkClientException",
              "Lambda.TooManyRequestsException",
            ]
            IntervalSeconds = 2
            MaxAttempts     = 3
            BackoffRate     = 2
          }
        ]
        Next = "Completed"
      }

      Completed = {
        Type = "Pass"
        Parameters = {
          "decision"          = "approved"
          "request_id.$"      = "$.request_id"
          "action.$"          = "$.action"
          "reason.$"          = "$.reason"
          "approval.$"        = "$.approval"
          "executor_result.$" = "$.execution.result"
        }
        End = true
      }

      Rejected = {
        Type = "Pass"
        Parameters = {
          "decision"     = "rejected"
          "request_id.$" = "$.request_id"
          "action.$"     = "$.action"
          "reason.$"     = "$.reason"
          "approval.$"   = "$.approval"
        }
        End = true
      }

      InvalidDecision = {
        Type  = "Fail"
        Error = "InvalidApprovalDecision"
        Cause = "The approval callback did not contain a valid decision"
      }

      Expired = {
        Type  = "Fail"
        Error = "ApprovalExpired"
        Cause = "No approval decision was received within 24 hours"
      }
    }
  })
}
