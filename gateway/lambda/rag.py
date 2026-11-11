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


def build_rag_context(prompt, retrieval, retrieval_start):
    """
    Turn a Retrieve response into the augmented model prompt
    and the RAG metrics.
    """

    chunks = [
        result["content"]["text"]
        for result in retrieval[
            "retrievalResults"
        ]
        if result["content"].get("text")
    ]

    model_prompt = RAG_PROMPT.format(
        context="\n\n".join(chunks),
        question=prompt
    )

    rag_metrics = {
        "rag_enabled": True,
        "retrieved_chunks": len(chunks),
        "retrieval_latency_ms": int(
            (
                time.time()
                - retrieval_start
            )
            * 1000
        )
    }

    return model_prompt, rag_metrics
