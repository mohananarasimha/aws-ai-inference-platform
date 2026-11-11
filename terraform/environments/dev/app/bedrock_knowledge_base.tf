resource "aws_bedrockagent_knowledge_base" "rag" {
  name     = "${local.name}-rag"
  role_arn = aws_iam_role.knowledge_base.arn

  knowledge_base_configuration {
    type = "VECTOR"

    vector_knowledge_base_configuration {
      embedding_model_arn = local.embedding_model_arn

      embedding_model_configuration {
        bedrock_embedding_model_configuration {
          dimensions          = aws_s3vectors_index.rag.dimension
          embedding_data_type = "FLOAT32"
        }
      }
    }
  }

  storage_configuration {
    type = "S3_VECTORS"

    s3_vectors_configuration {
      index_arn = aws_s3vectors_index.rag.index_arn
    }
  }

  depends_on = [aws_iam_role_policy.knowledge_base]
}

resource "aws_bedrockagent_data_source" "rag_source" {
  knowledge_base_id = aws_bedrockagent_knowledge_base.rag.id
  name              = "${local.name}-rag-source"

  data_source_configuration {
    type = "S3"

    s3_configuration {
      bucket_arn = aws_s3_bucket.rag_source.arn
    }
  }
}
