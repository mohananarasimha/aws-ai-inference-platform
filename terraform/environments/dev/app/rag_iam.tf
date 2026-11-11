locals {
  embedding_model_arn = "arn:aws:bedrock:${var.aws_region}::foundation-model/${var.embedding_model_id}"
}

data "aws_iam_policy_document" "knowledge_base_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:knowledge-base/*"]
    }
  }
}

resource "aws_iam_role" "knowledge_base" {
  name               = "${local.name}-knowledge-base"
  assume_role_policy = data.aws_iam_policy_document.knowledge_base_assume.json
}

data "aws_iam_policy_document" "knowledge_base" {
  statement {
    sid       = "ReadSourceBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.rag_source.arn]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  statement {
    sid       = "ReadSourceObjects"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.rag_source.arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  statement {
    sid       = "InvokeEmbeddingModel"
    actions   = ["bedrock:InvokeModel"]
    resources = [local.embedding_model_arn]
  }

  statement {
    sid = "UseVectorIndex"
    actions = [
      "s3vectors:GetIndex",
      "s3vectors:QueryVectors",
      "s3vectors:PutVectors",
      "s3vectors:GetVectors",
      "s3vectors:DeleteVectors",
    ]
    resources = [aws_s3vectors_index.rag.index_arn]
  }
}

resource "aws_iam_role_policy" "knowledge_base" {
  name   = "knowledge_base_permissions"
  role   = aws_iam_role.knowledge_base.id
  policy = data.aws_iam_policy_document.knowledge_base.json
}
