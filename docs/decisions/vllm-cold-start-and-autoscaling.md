# Decision: measure vLLM cold start first; defer autoscaling

Status: **cold start measured 2026-11-11; autoscaling deliberately not implemented.**

## The question

How long does new GPU inference capacity take to become usable, and where is that time spent? Scale-from-zero is already proven (Karpenter launches one `g5.xlarge` for a Pending vLLM pod); this phase puts numbers on it.

## How it is measured

`scripts/measure_vllm_cold_start.py` is read-only. Every milestone except `/health` and the inference check is a timestamp already stored on a Kubernetes object, so the result does not depend on when the script looks, and it also works after the fact while the pod, NodeClaim and node still exist.

| | Milestone | Source |
|---|---|---|
| T0 | vLLM pod Pending | pod `creationTimestamp` (the Deployment's ReplicaSet-owned pod, label `app=vllm`) |
| T1 | NodeClaim created | NodeClaim `creationTimestamp`, matched through the pod's node (before binding: the `Nominated` event) |
| T2 | node Ready | node condition `Ready=True` `lastTransitionTime` |
| T2b | `nvidia.com/gpu` available | NodeClaim `Initialized` transition (Karpenter waits for extended resources), informational |
| T3 | container started | `containerStatuses[vllm].state.running.startedAt` |
| T4 | vLLM ready | later of pod `Ready` (T4a) and the first successful `/health` through a port-forward (T4b) |
| T5 | first inference | one request through API Gateway (`--test-inference`) |

Pod Ready is driven by a probe that runs every 10 s, so it can lag the application by up to 10 s; `/health` is the application's own answer. Traffic needs both, so T4 is the later one. Kubernetes timestamps have 1 s resolution, so every duration is plus or minus 1 s. The script also reports image pull time from pod events and the timings vLLM prints while starting (best effort; the wording depends on the vLLM version).

## Probes

The vLLM Deployment has a readiness probe only (`/health`, every 10 s, `failureThreshold: 3`). That is correct: the Service routes only to Ready pods, the load balancer runs its own `/health` check before sending traffic, and readiness failures never restart the container. There is no liveness or startup probe, so a slow start cannot get the pod killed. A startup probe would not shorten the cold start, so none was added.

## Why autoscaling is deferred

Real autoscaling would add `1 -> 2` vLLM replicas under load, which makes a second GPU pod Pending, which makes Karpenter add a second GPU node:

```text
demand / queue pressure -> pod autoscaler -> vLLM replicas 1 -> 2
  -> second GPU pod Pending -> Karpenter -> GPU nodes 1 -> 2
```

Useful signals are waiting requests, TTFT, request rate, GPU utilization and KV-cache pressure. It is deferred because:

1. The account's quota for Running On-Demand G and VT instances is 4 vCPUs: one `g5.xlarge` (4 vCPUs). A second needs 8, so a real two-GPU validation is not possible.
2. Karpenter scale-from-zero is already proven.
3. Adding KEDA or a custom metrics adapter for an experiment that cannot be validated would be unnecessary complexity.

If the quota is raised, the smallest next step is a pod autoscaler on one vLLM signal (for example waiting requests), plus raising the NodePool CPU and GPU limits from 4 and 1 to 8 and 2.

Method, results and how to rerun it: [`../vllm-cold-start.md`](../vllm-cold-start.md).

## First observed run (2026-11-11, no inference request)

Pod Pending to Karpenter NodeClaim 2 s, to node Ready 41 s, container started after 313 s (299 s of it pulling the vLLM image), model ready after another 347 s (about 216 s loading weights, the rest compiling and warming up). Pod Ready 701 s after the pod was created. The model cache is an `emptyDir`, so the weights are downloaded again on every cold start. A second run on the same pod with `--test-inference` confirmed the Kubernetes-recorded numbers and showed one request through the whole path taking 11 s; the first-request and total figures from that run are not valid cold-start numbers (the script was started 19 minutes after Ready) and the script now reports `n/a` for them.


## Kserve
The platform currently deploys vLLM using native Kubernetes Deployment and Service resources rather than KServe. The current workload consists of a single serving runtime and model, so KServe would add control-plane complexity without materially improving reliability or scalability. KServe becomes valuable when the platform needs standardized multi-model lifecycle management, reusable serving abstractions, advanced rollout policies, or centralized autoscaling configuration.