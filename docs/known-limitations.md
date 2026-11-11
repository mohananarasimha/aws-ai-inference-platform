# Known limitations and open issues

Only what is still true. Evidence and numbers are in [experiments/vllm-failure-recovery.md](experiments/vllm-failure-recovery.md) and [vllm-cold-start.md](vllm-cold-start.md).

## GPU quota

The account quota for Running On-Demand G and VT instances is **4 vCPUs**; one `g5.xlarge` uses all 4.

- Only one GPU node can run, so a real `1 -> 2` GPU autoscaling test cannot be done. Autoscaling is deferred for this reason ([decision](decisions/vllm-cold-start-and-autoscaling.md)).
- **It also slowed recovery from a GPU node failure.** Karpenter's replacement NodeClaims first failed with `VcpuLimitExceeded` (operator-reported events), and about 8 min 13 s passed between the replacement pod and the NodeClaim that launched. Quota headroom is a failure-recovery concern, not only an autoscaling one. How much of the 8 min 13 s was the quota is not measured.

## Timeout gap

Request deadline propagation is implemented and unit tested (24 s budget under a 30 s Lambda timeout). A real inference-backend outage exposed at least one path that still reached Lambda's 30 s hard timeout: five invocations ended with `Duration: 30000 ms, Status: timeout`, and none logged a request or emitted a metric. A pod-failure run also shows three requests of about 31 s that may be the same thing (unverified).

**Root cause is not known.** What is established:

- The plain "vLLM never answers" path is bounded. `scripts/repro/vllm_hang_probe.py` and `scripts/repro/agent_hang_repro.py` show it ends at the vLLM call limit (about 20 s) with a controlled exception.
- `urllib`'s timeout is per socket operation, so a server that trickles data can exceed it. A dead NLB target is not expected to do that.
- The vLLM call is synchronous inside the async agent loop; unwinding after a failure (MCP session close, NAT-routed calls to AgentCore) is the unreproduced part.

Until it is found, the 24 s deadline must not be read as a guarantee. A first step would be stage-boundary log lines plus one controlled outage; a watchdog that returns a 504 shortly before the Lambda limit is a possible safety net (a design change, not started). The timeouts that reach 30 s emit no `TimeoutCount` metric.

## Observability

- Dashboard Terraform exists (11 widgets). The four agent/Lambda widgets use CloudWatch metrics the Lambda emits.
- The vLLM, GPU and cluster widgets are PromQL written from the vLLM and DCGM documentation and are still titled `[draft query]`. No record in this repository shows them checked against live series. Treat them as unverified.
- The NLB healthy/unhealthy widget searches `AWS/NetworkELB`; that it was confirmed against live series is not recorded in the repository either.
- No alarms, no tracing. Proposed alerts are listed in the failure doc and not implemented.
- The CloudWatch agent uses the AWS-managed `CloudWatchAgentServerPolicy`, broader than needed.

## Single replica

One vLLM replica on one GPU node. Health checks detect a failure but there is nothing to fail over to, so every pod or node failure is a total outage until the replacement is Ready (369 s for a pod failure, over 20 minutes for the measured node failure). The NLB health check does not change that. Adding replicas needs more GPU quota.

## Cold start and fresh-node recovery

A fresh GPU node takes about 701 s to a Ready pod: the image (about 9.7 GB) takes about 299 s to pull, and the model download, load and warm-up take about 347 s. The Hugging Face cache is an `emptyDir`, so the weights are downloaded again after any pod or node replacement. Ways to shorten this (a pre-pulled image, a persistent model store) are listed in [vllm-cold-start.md](vllm-cold-start.md) and not implemented.

## Not validated

- `1 -> 2` GPU autoscaling (quota).
- Readiness-failure behaviour of a running but hung vLLM (a test was designed and deliberately not run; the readiness threshold has since changed from 60 to 3).
- On-prem inference experiments: see [experiments/onprem-vllm-load-test.md](experiments/onprem-vllm-load-test.md) if present, otherwise nothing about them is evidenced in this repository.
- The write/approval path, RAG and tool calling through vLLM have unit tests against fakes; no live end-to-end run is recorded in this repository.

## Operations

- **Terraform state is local**, one file per stack and not in Git. Losing it orphans the resources.
- **The Lambda's outbound traffic goes through one self-managed NAT/router instance** that this repo does not create; if it stops, every Bedrock, RAG and MCP call hangs.
- **Destroying the app stack can fail on the Lambda security group** for up to about 40 minutes after the function is deleted, because AWS releases the Lambda network interfaces asynchronously. `scripts/down.sh` waits and retries.
- `scripts/measure_vllm_recovery.py` aborts if the Kubernetes API has a transient error after a failure is injected, which cost the node run its start time. It should retry instead.
