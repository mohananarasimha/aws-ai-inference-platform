import os
import time

from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError

from clients import aws_client
from config import RAG_MAX_TIMEOUT_SECONDS
from deadline import ComponentTimeout, call_timeout


RAG_PROMPT = """Answer the question using only the context below.
If the context does not contain enough information, say that the context is insufficient.

You may use available tools when live system information is needed.

Context:
{context}

Question:
{question}"""


def retrieve(prompt, deadline=None):
    """
    Query the Knowledge Base and return the raw Retrieve response.
    Raises on failure; the caller decides how to report it.
    """

    client = aws_client(
        "bedrock-agent-runtime",
        call_timeout(deadline, RAG_MAX_TIMEOUT_SECONDS, "rag")
    )

    try:

        return client.retrieve(
            knowledgeBaseId=os.environ[
                "KNOWLEDGE_BASE_ID"
            ],
            retrievalQuery={
                "text": prompt
            },
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": 3
                }
            }
        )

    except (ReadTimeoutError, ConnectTimeoutError) as exc:

        raise ComponentTimeout("rag") from exc


def source_of(result):
    """
    Where a retrieved chunk came from, as returned by the Knowledge Base:
    the document's file name (never the bucket) and the relevance score.
    None for anything the Retrieve response did not include.
    """

    uri = ((result.get("location") or {}).get("s3Location") or {}).get("uri")
    score = result.get("score")

    return {
        "document": uri.rsplit("/", 1)[-1] if uri else None,
        "score": round(score, 4) if isinstance(score, (int, float)) else None
    }


def build_rag_context(prompt, retrieval, retrieval_start):
    """
    Turn a Retrieve response into the augmented model prompt
    and the RAG metrics.
    """

    used = [
        result
        for result in retrieval[
            "retrievalResults"
        ]
        if result["content"].get("text")
    ]

    chunks = [result["content"]["text"] for result in used]

    model_prompt = RAG_PROMPT.format(
        context="\n\n".join(chunks),
        question=prompt
    )

    rag_metrics = {
        "rag_enabled": True,
        "retrieved_chunks": len(chunks),
        "rag_sources": [source_of(result) for result in used],
        "retrieval_latency_ms": int(
            (
                time.time()
                - retrieval_start
            )
            * 1000
        )
    }

    return model_prompt, rag_metrics
