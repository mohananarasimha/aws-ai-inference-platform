# vLLM failure and recovery experiments

Status (2026-11-11): **two of three experiments were run: pod failure and GPU node failure. The readiness-failure test was not run, by decision.** Everything marked "measured" comes from the result files in [`results/`](results/); everything marked "hypothesis" was never tested.

Evidence files (each one says how original it is):

| File | What it is |
|---|---|
| [`results/cold-start-2026-11-11.txt`](results/cold-start-2026-11-11.txt) | original script output, with the invalid figures marked |
| [`results/pod-failure-2026-11-11.txt`](results/pod-failure-2026-11-11.txt) | the script's own saved output, sanitized |
| [`results/node-failure-2026-11-11.txt`](results/node-failure-2026-11-11.txt) | original Lambda log lines; the Kubernetes timeline and the quota events are operator-reported, because the measuring script aborted |

| Item | Status |
|---|---|
| Cold-start behavior | COMPLETE, see [`../vllm-cold-start.md`](../vllm-cold-start.md) |
| GPU node failure and recovery (Experiment 3) | COMPLETE, with caveats (see "Measurement gaps") |
| Pod-only failure (Experiment 1) | COMPLETE: 369 s, see below |
| Readiness-failure test (Experiment 2) | NOT REQUIRED, not run |
| Timeout gap (requests reaching Lambda's 30 s) | FOUND, narrowed, not yet located (see "Reliability gap") |

No chaos or failure-injection platform was added, and none will be.

## The path under test

```text
Client -> API Gateway -> Lambda -> internal NLB -> Service -> vLLM pod -> g5.xlarge (Karpenter)
```

One vLLM replica (Deployment strategy `Recreate`), one GPU, one NLB target, one GPU node. The AWS quota allows a single `g5.xlarge` (4 vCPUs), so there is no redundancy: every failure is a total outage until recovery.

## Experiment 3 (measured): GPU node failure

### What was done

The Karpenter GPU instance was terminated with `aws ec2 terminate-instances`. AWS later confirmed `State = terminated`.

| Object | Old | Replacement |
|---|---|---|
| Pod | (the original vLLM pod) | `vllm-687b7b9c45-lfvl5` |
| NodeClaim | `gpu-7cf6r` | `gpu-dc9j4` |
| Node | `<gpu-node>` | `<gpu-node-2>` |
| EC2 instance | `i-xxxxxxxx` (g5.xlarge) | a new g5.xlarge |
| Availability zone | us-east-1d | **us-east-1b** |

### Timestamps (UTC, 2026-11-11)

| Moment | Time | Source |
|---|---|---|
| Last successful vLLM request before the failure | 06:23:16 | Lambda log: success, E2E 7042 ms, model 1480 ms |
| Replacement pod created | 06:25:12 | pod `creationTimestamp` |
| Replacement NodeClaim `gpu-dc9j4` created | 06:33:25 | NodeClaim `creationTimestamp` |
| Replacement node Ready | 06:34:02 | node condition |
| vLLM container started | 06:39:46 | container status |
| Pod Ready | 06:45:14 | pod condition |
| First confirmed successful end-to-end request | 06:46:18 | Lambda log: provider vllm, status success, 1 agent step, E2E 8443 ms, model 2428 ms, 537 input / 24 output tokens, RAG false |

### Durations

| Interval | Duration |
|---|---|
| Replacement pod created -> successful NodeClaim | 8 min 13 s |
| Successful NodeClaim -> node Ready | 37 s |
| Node Ready -> container started | 5 min 44 s |
| Container started -> pod Ready | 5 min 28 s |
| **Pod created -> pod Ready** | **20 min 02 s** |
| Pod Ready -> first successful end-to-end request | 1 min 04 s |
| **Replacement pod created -> successful inference** | **21 min 06 s** |

**This is not the exact failure MTTR.** The script that was measuring the run aborted after the termination, so the exact failure-injection moment (N0) was not preserved. The 21 min 06 s figure starts at the replacement pod's creation, not at the failure. The real outage is somewhat longer. Do not quote 21 min 06 s as "MTTR".

### What the run proved

```text
GPU node terminated
-> workload lost its GPU
-> replacement pod needed a GPU
-> Karpenter tried to launch replacement capacity
-> a new NodeClaim was created and a new g5.xlarge launched
-> node Ready, NVIDIA GPU became available
-> pod scheduled, image pulled
-> vLLM initialized, pod Ready
-> inference succeeded through the whole path
```

| Check | Result |
|---|---|
| Karpenter replacement works | yes |
| Replacement in a different AZ (1d -> 1b) | yes: recovery did not depend on the original AZ |
| New GPU node joins EKS, GPU usable | yes |
| Replacement vLLM starts, readiness gates traffic | yes |
| Full path (API Gateway -> Lambda -> NLB -> Service -> vLLM -> GPU) works again | yes, first confirmed at 06:46:18 |

### Where the 21 minutes went

Karpenter's EC2 provisioning was not the problem: once a NodeClaim could launch, **NodeClaim -> node Ready took 37 s**. Three other things dominate:

1. **No quota headroom: about 8 min 13 s** between the replacement pod and the successful NodeClaim (see the next section).
2. **Image pull: 5 min 9.438 s** for `vllm/vllm-openai:v0.33.0` (about 9.7 GB) on a fresh node, as recorded by Kubernetes. The node was new, so nothing was cached.
3. **Model initialization: 5 min 28 s** from container start to Ready: model download and load, weight loading, torch.compile, CUDA graph capture. The Hugging Face cache is an `emptyDir`, so it does not survive a pod or node replacement.

## Finding 1: quota equal to steady state delays recovery

Quota: Running On-Demand G and VT instances = **4 vCPUs**. One `g5.xlarge` needs 4. A running GPU instance therefore uses the whole quota.

During the replacement, Karpenter tried to launch capacity and failed. Examples of failed NodeClaims:

```text
gpu-8lq29
gpu-jt8fq
  event: VcpuLimitExceeded
  "You have requested more vCPU capacity than your current vCPU limit of 4 allows"
```

`gpu-dc9j4` was eventually created and launched. This explains much of the 8 min 13 s between the replacement pod and the successful NodeClaim. (The exact split between the old instance still counting against the quota and other delays was not measured.)

**Conclusion.** AWS quota headroom is not only an autoscaling concern, it is a failure-recovery concern. With quota exactly equal to steady-state consumption, a GPU failure can leave replacement capacity temporarily blocked, which delays recovery. For anything beyond a lab, leave headroom for replacement capacity instead of treating "quota = steady state" as sufficient.

## Finding 2 (reliability gap): requests reached Lambda's 30 s hard timeout

The Lambda has an application-level request deadline of about 24 s and a 30 s Lambda timeout, so requests should fail in a controlled way before 30 s. During the outage they did not.

Five invocations ended with `Duration: 30000.00 ms, Status: timeout`:

| Invocation start (UTC) | Time from START to `mcp_tools_discovered` | Result |
|---|---|---|
| 06:25:38 | 0.1 s (cached tools) | killed at 30 s |
| 06:27:52 | about 6.0 s | killed at 30 s |
| 06:29:55 | about 6.6 s | killed at 30 s |
| 06:32:10 | about 6.0 s | killed at 30 s |
| 06:34:10 | about 5.5 s | killed at 30 s |

(Times are from the CloudWatch log timestamps of each invocation's START line, rounded to the second. The five `REPORT` lines are the 06:26:08 to 06:34:38 entries in the original report.)

### What the logs show

Every one of the five invocations logged only `START`, one `mcp_tools_discovered` line, `END` and `REPORT`. **None emitted a request log or a metric** (no `TimeoutCount`, no `ErrorCount`, no `RequestLatency`). The handler therefore never reached its error handling or `emit_request_metrics`. The request was stuck somewhere after the MCP tools were discovered, and no code that would have reported the failure ever ran.

The first invocation had cached MCP state; the other four had to initialize a new MCP session first, consistent with the Lambda execution environment being recycled after each timeout.

### What was ruled out (local reproduction, no AWS, no cluster)

| Check | Result |
|---|---|
| `request_chat` against a server that accepts the connection and never answers (what a dead NLB target looks like) | Raised `vllm_timeout` at the limit (3.0 s for a 3 s limit) |
| The full agent loop (`run_agent`) with a real local MCP server and a vLLM socket that never answers, budget 24 s | Ended after 20.0 s (the vLLM call limit) with a controlled exception; nothing hung afterwards |

So the plain "vLLM never answers" path is bounded and unwinds cleanly. That path should have failed in about 20 s, so it does not explain the 30 s.

### What is still open

Which call was active, and where the extra time went, is **not established**. Findings so far:

1. **One path is not bounded.** `urllib`'s timeout is per socket operation, not total. A server that sends data slowly (one byte a second) kept `request_chat` running for 30.1 s against a 3 s limit (it ended as `vllm_unavailable`). A dead NLB target is not expected to behave this way, so this is a possible cause, not a likely one. DNS resolution inside `urllib` is not covered by the timeout either.
2. **The vLLM call blocks the event loop.** `request_chat` is synchronous `urllib` called from inside the async agent loop, while the MCP session (and its background reader) lives on the same loop. Nothing else, including MCP's own timeouts, can run while it is blocked. This did not cause a hang in the local reproduction, but the production MCP connection goes through a NAT instance to AgentCore, which was not reproduced.
3. **Unwinding after the failure.** In four of the five invocations the vLLM call is bounded by the remaining budget, so it should end near 24 s, leaving about 6 s for unwinding. In the first, the vLLM limit (20 s) ends near 20 s, leaving about 10 s. All five were still stuck at 30 s. Something after the vLLM failure (closing the MCP session, shutting down the event loop) or something before it (NLB discovery after a recreated NLB, through the NAT) took the rest. The local reproduction unwound immediately.
4. **Not blamed on anything else.** There is no evidence either way for the NAT path, DNS, or the discovery call. The logs cannot separate these because nothing was logged between `mcp_tools_discovered` and the kill.

**Status: a discovered gap, not diagnosed.** The timeout implementation was not changed.

### Reproduction scripts

The local reproductions are preserved in [`scripts/repro/`](../../scripts/repro/): `vllm_hang_probe.py` (the per-socket timeout versus a hung or trickling server) and `agent_hang_repro.py` with `local_mcp_server.py` (the full agent loop against a hung vLLM). They run locally with no AWS. They show the simple hang path is bounded; they do **not** reproduce the 30 s outage behaviour.

### Further indication from the pod-failure run

The pod-failure result file also shows three requests of about 31 s during the outage (see Experiment 1). If those are Lambda timeouts, the gap reproduced in a second, independent outage. Not verified.

### Proposed next step (not done)

Add log lines at the stage boundaries inside the request (discovery start/end, vLLM call start/end, session close start/end) and repeat one controlled outage, or reproduce the stuck path from a Lambda-like environment. That would show in one run which stage runs past the deadline. The deadline cannot be enforced by timeouts on blocking calls alone; a watchdog that returns a controlled 504 shortly before the Lambda timeout would be the safety net, but that is a design change and was not started.

## Finding 3: measurement-script robustness

After the instance was terminated, `scripts/measure_vllm_recovery.py` failed:

```text
kubectl get nodes failed:
Unable to connect to the server: net/http: TLS handshake timeout
```

This was a **measurement-tool failure, not a platform failure**: Karpenter and the cluster kept recovering. But it aborted the measurement after the destructive action had already happened, so the N0 timestamp and the NLB-healthy moment were not preserved.

**Issue to fix later:** transient Kubernetes API polling errors should be retried (with a limit) instead of aborting an experiment after the failure has been injected. The destructive experiment was not rerun, and the script was not changed in this update.

## Measurement gaps (do not fill by estimating)

| Milestone | Status |
|---|---|
| N0, exact failure-injection time | not preserved (script aborted) |
| N8, NLB target becomes healthy | **not captured exactly.** Known only: Pod Ready 06:45:14, first successful API request 06:46:18. The NLB was necessarily serving the recovered target by the second one; no first-healthy time was recorded and none is estimated. |
| Failure start -> successful inference (true MTTR) | not measurable; only "replacement pod created -> successful inference" (21 min 06 s) is |
| Which fraction of the 8 min 13 s was quota vs other causes | not measured |
| What callers saw during the outage | partly: 5 requests ended at 30 s as Lambda timeouts (node run, from Lambda logs); 3 requests of about 31 s in the pod run, cause unverified; the status codes API Gateway returned were not recorded in the node run |

## Experiment 1 (measured): pod failure on a surviving GPU node

Evidence: [`results/pod-failure-2026-11-11.txt`](results/pod-failure-2026-11-11.txt) (the script's saved output). The Deployment, replica count and node were untouched; only the pod was deleted (`kubectl delete pod ... --wait=false`).

| Moment | Time (UTC) | Source |
|---|---|---|
| P0 pod deletion requested | 06:17:08.6 | script clock at the delete command |
| P1 replacement pod created | 06:17:10 | Kubernetes |
| P2 replacement bound to a node | 06:17:11 | Kubernetes |
| P3 vLLM container started | 06:17:12 | Kubernetes |
| P4b vLLM serving (its log line) | 06:22:19.4 | vLLM log |
| P4a pod Ready | 06:22:24 | Kubernetes |
| P4c NLB target healthy | 06:22:53.7 | polled (resolution about 3 to 5 s) |
| P5 first successful request | 06:23:17.5 | controlled request |

| Interval | Duration |
|---|---|
| container start to Ready (model startup) | 312 s |
| pod Ready after the failure | 315 s |
| NLB recovery after Ready | 30 s |
| **Pod failure to first successful request** | **369 s** (6 min 9 s) |

Recorded by the script: the image was already cached on the node, the replacement landed on the **same node**, and vLLM downloaded the weights again (about 106 s reported), because the model cache is an `emptyDir`.

What the caller saw: three controlled requests during the outage failed after about 31 s each (the script labelled them "HTTP 503 endpoint not found"), then the fourth, at +358 s, succeeded in 11 s. **Caveat:** that label is the script's own mapping of status 503. A 31 s duration matches Lambda's 30 s hard timeout, and API Gateway HTTP APIs also return 503 for a Lambda timeout, so these may be the same 30 s timeouts seen in the node experiment rather than the gateway's own endpoint-not-found error. No Lambda logs were preserved for this window, so this is unverified (see Finding 2).

### Pod failure versus node failure

| | Pod failure | Node failure |
|---|---|---|
| New EC2 capacity | none (node survived) | required |
| Quota delay | none | yes, `VcpuLimitExceeded` (Finding 1) |
| Image pull | none (cached) | 5 min 9 s (fresh node) |
| Model download and load | about 312 s | about 328 s |
| Time to a successful request | 369 s from the failure | 21 min 06 s from the replacement pod (the failure start was not captured) |

The two runs differ in more than the node: they were measured differently (the pod run has an exact start, the node run does not), so compare the durations of the stages, not the headline totals. What the data supports: with the node alive, recovery is dominated by the model reload; a lost node adds the quota delay and the image pull on top of the same model reload.

## Experiment 2 (readiness failure): not required

Not run, by decision. Its hypotheses about steady-state probe behavior (how long Kubernetes would keep a hung vLLM in the endpoints, and whether the NLB would fail open with a single target) are untested. Note that these were written for a readiness probe with a failure threshold of 60; the manifest now has `failureThreshold: 3`, so the old numbers (about 600 s) no longer apply. The script can run it (`readiness --inject`, which needs a deliberate confirmation) but there is no plan to.

## Readiness behavior (observed)

The vLLM probe, as observed in the later experiment: `GET /health` every 10 s, timeout 3 s, `failureThreshold: 3`. There is a readiness probe, **no liveness and no startup probe**.

During model start the readiness probe failed repeatedly with connection refused, the **restart count stayed 0**, and the pod became `Ready=True`. So the readiness probe gates traffic correctly while the long model start continues. A startup probe is not required just to support the slow start, because nothing (no liveness probe) kills the container. Caveat: the comment above the probe in `vllm-deploy.yaml` still says "60 x 10 s of failures are tolerated"; it is stale next to `failureThreshold: 3`.

## Production-relevant weaknesses found

1. No GPU quota headroom delays recovery (about 8 min in this run).
2. A large vLLM image pull adds about 5 min on a fresh node.
3. Model initialization adds about 5.5 min more (the cache is an `emptyDir`).
4. Requests during a backend outage can still reach Lambda's 30 s hard timeout despite the 24 s application deadline. Not diagnosed yet.

Items 1 to 3 explain the long recovery. Item 4 is a separate request-failure-handling issue.

## How it was measured

`scripts/measure_vllm_recovery.py` reuses the cold-start helpers. Kubernetes timestamps are used wherever they exist (pod, NodeClaim, node and pod conditions). The load balancer target state and Service endpoints have no timestamps and were meant to be observed by polling. It injects only with `--inject` after a typed confirmation, and refuses unless its safety checks pass. The node-failure experiment was run by the user. See "Finding 3" for the polling gap.

## Signals to read in CloudWatch

| Question | Where |
|---|---|
| Did callers fail, and how? | Agent widgets: requests by status, timeouts by component, errors by type. Note that the 30 s Lambda timeouts emitted no metric (Finding 2), so they are visible only as Lambda duration/timeouts. |
| What does Kubernetes think? | Cluster widget: vLLM replicas desired and available, restarts, GPU count |
| What does the load balancer think? | Inference NLB healthy and unhealthy targets (widget 11; the dashboard change was planned at 0 add / 1 change / 0 destroy; check `terraform plan` before assuming it was applied) |
| Is vLLM busy or stuck? | vLLM widgets: running and waiting requests, KV cache, TTFT, end-to-end latency |
| Is the GPU alive? | GPU widget: utilization, VRAM, temperature, power |

The vLLM, GPU and cluster PromQL queries are still drafts until checked against live series.

## What would alert an operator (proposed, not implemented)

- NLB `UnHealthyHostCount >= 1` for 1 to 2 minutes.
- vLLM replicas available below desired for more than 2 minutes.
- Lambda timeouts (the platform's own `TimeoutCount` is not emitted for the 30 s case, so a Lambda `Duration` near the limit or the Lambda `Errors`/timeout signal is needed).
- NLB `HealthyHostCount == 0`.
- GPU vCPU quota usage at 100% of the limit (the condition that blocked the replacement).

## Next

After the timeout-gap investigation: one controlled load / cost-performance experiment using the observability already built.

Tests need no cluster: `gateway/.venv/bin/python -m pytest scripts/tests -q` (adjust the interpreter to your venv).
