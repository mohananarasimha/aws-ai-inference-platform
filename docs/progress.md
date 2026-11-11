# Build log: managed-model phases (1 to 3 and 6)

Historical learning notes from the first phases, kept because they hold the only recorded numbers for the Bedrock, RAG and MCP work (Nova Micro 864 ms vs Nova Lite 1540 ms; MCP session reuse from about 3.5 to 5 s down to about 1.2 s). They are narrative, not raw results. Phases 4 and 5 (MCP tools, approval) were not written up here, and nothing after phase 6 is recorded in this file.

**Current project status is in the README ("Project status"), not here.** For the vLLM, EKS and failure work see `docs/experiments/` and `docs/vllm-cold-start.md`.

---

Phase 1 — AI Gateway + Managed Model

Built:
- API Gateway HTTP API
- Lambda AI gateway
- Bedrock Nova Micro
- request validation
- request ID
- latency tracking
- token usage
- CloudWatch structured logging
- Cognito M2M authentication
- JWT authorizer

What I learned:
- API Gateway is the HTTP entry point
- Lambda contains gateway logic
- Cognito authenticates machine clients
- JWT scope is used for authorization
- Lambda IAM role authorizes Bedrock access
- Bedrock is managed inference

Issue encountered:
- Third-party model access failed due to Marketplace permissions
- switched to Nova Micro for simpler testing

Next:
- API Gateway throttling
- Terraform reproduction
- multi-model routing


## Phase 2 — Multi-model Gateway

Built:
- Added model abstraction using model_class.
- fast routes to Nova Micro.
- quality routes to Nova Lite.
- Client does not need Bedrock model IDs.
- Updated Lambda IAM permissions for both models.

Experiment:
- Sent identical troubleshooting prompt to both models.
- Nova Micro latency: 864 ms
- Nova Lite latency: 1540 ms
- Both produced useful responses.
- Lite was slightly more detailed, but improvement was not large enough
  to justify claiming it is always better.

Architecture decision:
- Use Micro as the default fast/cheap class.
- Use Lite as the higher-capability class.
- Do not implement sophisticated automatic routing yet.

Lesson:
Model selection is a tradeoff between capability, latency, and cost.
The gateway provides an abstraction where routing policy can change
without applications depending directly on Bedrock model IDs.

## Phase 3 — RAG

Built:
- S3 source documents
- Bedrock Knowledge Base
- Titan embeddings
- S3 Vectors
- Knowledge Base Retrieve API
- Lambda RAG orchestration
- top_k = 3
- augmented prompt
- RAG and non-RAG request paths

Validated:
- relevant document ranked first for test queries
- RAG answer matched internal policy
- unsupported query returned "context is insufficient"
- non-RAG answer produced generic model guidance instead

Observation:
RAG increased input token count because retrieved context is added,
but grounded the response in organization-specific information.

Architecture lesson:
RAG improves grounding and access to private/current knowledge.
It does not inherently make the base model more capable.




## Phase 6 — Observability

Built:
- total request latency tracking
- RAG retrieval latency tracking
- Bedrock per-call and total latency tracking
- MCP tool-call latency tracking
- MCP connection, initialize, and tools-list timing
- structured request metrics

Validated:
- full request latency can now be broken down by component
- Bedrock was not the main bottleneck
- MCP tool execution itself was relatively small
- repeated requests still showed high MCP initialization latency

Observation:
MCP session initialization took roughly 3.5–4.5 seconds on repeated requests and became the dominant contributor to total latency.

Optimization:
Investigating reuse of the MCP session and cached tool schemas across warm Lambda invocations instead of initializing a new MCP session for every request.

Architecture lesson:
Measure each boundary before optimizing. The apparent AI/model latency problem was actually recurring MCP session setup overhead.

Optimization:
- found recurring MCP session initialization was the main latency bottleneck
- warm requests were repeatedly paying ~3.5–5s for MCP initialization
- changed Lambda MCP client to reuse the AgentCore session ID and cached tool definitions across warm invocations
- kept HTTP/MCP transport short-lived per request
- added one reconnect attempt if the cached session becomes stale

Validated:
- cold MCP path still pays full initialization cost
- warm MCP path skips initialize and tools/list
- direct tests reduced MCP warm-path latency from several seconds to roughly ~1.2s

Architecture lesson:
Reuse cheap metadata/session identifiers across warm Lambda invocations, but avoid caching async transport objects tied to a closed event loop.