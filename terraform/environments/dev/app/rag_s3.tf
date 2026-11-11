resource "aws_s3_bucket" "rag_source" {
  bucket = "${local.name}-rag-source-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "rag_source" {
  bucket = aws_s3_bucket.rag_source.id

  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "rag_source" {
  bucket = aws_s3_bucket.rag_source.id

  rule {
    bucket_key_enabled = true

    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_object" "rag_documents" {
  for_each = fileset("${path.module}/../../../../data", "**")

  bucket       = aws_s3_bucket.rag_source.id
  key          = each.value
  source       = "${path.module}/../../../../data/${each.value}"
  etag         = filemd5("${path.module}/../../../../data/${each.value}")
  content_type = "text/markdown"
}

resource "aws_s3vectors_vector_bucket" "rag" {
  vector_bucket_name = "${local.name}-rag-vectors"
}

resource "aws_s3vectors_index" "rag" {
  vector_bucket_name = aws_s3vectors_vector_bucket.rag.vector_bucket_name
  index_name         = "${local.name}-rag-index"
  data_type          = "float32"
  dimension          = 1024
  distance_metric    = "cosine"

  metadata_configuration {
    non_filterable_metadata_keys = ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]
  }
}
