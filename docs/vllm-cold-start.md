# vLLM cold start on EKS: how it is measured and what it showed

Why this was measured and why autoscaling was deferred: [`decisions/vllm-cold-start-and-autoscaling.md`](decisions/vllm-cold-start-and-autoscaling.md). This page is the working reference: method, results, how to rerun it, and how to read the numbers.

## The question

With no GPU node running and the vLLM Deployment at one replica, how long until new GPU capacity can serve a request, and where does the time go?

```text
vLLM pod Pending -> Karpenter NodeClaim -> g5.xlarge launches -> node Ready
  -> NVIDIA device plugin advertises the GPU -> pod bound -> image pulled
  -> container starts -> model loads -> readiness passes -> first inference
```

## Measurement

`scripts/measure_vllm_cold_start.py` is read-only: it never applies, scales or deletes anything. Every milestone except `/health` and the inference request is a timestamp already stored on a Kubernetes object, so the result does not depend on when the script looks. It also works after the fact while the pod, NodeClaim and node still exist (it then says so, because `/health` and the inference time are no longer first-success times).

| | Milestone | Source |
|---|---|---|
| T0 | vLLM pod Pending | pod `creationTimestamp`. The pod is the newest one with label `app=vllm` owned by a ReplicaSet, so a hand-made pod with the same label is ignored. |
| T1 | NodeClaim created | NodeClaim `creationTimestamp`, matched to the pod through its node (before binding: Karpenter's `Nominated` event). |
| T2 | node Ready | node condition `Ready=True`, `lastTransitionTime`. |
| T2b | `nvidia.com/gpu` available | NodeClaim `Initialized` transition (Karpenter waits for extended resources). Informational; T2 stays node Ready. The device plugin pod's Ready time is shown as well. |
| T3 | container started | `containerStatuses[vllm].state.running.startedAt`. |
| T4 | vLLM ready | the later of pod `Ready` (T4a) and T4b, the first successful `/health`. T4b is observed live through a port-forward polled every 3 s. After the fact it comes from the vLLM log line `Application startup complete` (sub-second, from the container runtime's timestamps), and the report says which one was used. |
| T5 | first inference | one request through API Gateway with `--test-inference`. |

**Measuring T4b.** T4b is the first successful `/health`, and Kubernetes does not timestamp that. Two ways to get it:
- *Live:* start the script before the pod becomes Ready. It polls `/health` through `kubectl port-forward` once the container is running; the first 200 is T4b, with the poll interval (3 s) as its resolution. This has to be running at the time, and it restarts the port-forward if kubectl exits while vLLM is still loading.
- *After the fact:* the log. vLLM prints `Application startup complete` when it can serve, and later logs every `/health` request with a timestamp. The first `GET /health 200` from the node's own IP is the kubelet's readiness probe, which is what makes the pod Ready (it matched the pod's `Ready` time exactly). The gap between the two is the readiness-probe lag. The script reads the beginning of the log (the end of a long-running pod's log is only probe lines).

**Why T4 is the later of two.** The readiness probe runs every 10 s, so pod Ready can lag the application by up to 10 s, while `/health` is the application's own answer. Traffic needs both. After Ready, the load balancer also needs its own health check to pass (10 s interval, 2 successes), which the script reports separately as "NLB target healthy".

**Durations**

```text
Karpenter reaction       T1 - T0
Node provisioning        T2 - T1
Container scheduling     T3 - T2     (includes the image pull)
Model initialization     T4 - T3
First request            T5 - T4
GPU capacity ready       T2 - T0
Model cold start         T4 - T3
Total usable cold start  T5 - T0
```

The report also has an informational breakdown (EC2 launch, node registration, GPU available, pod bound, image pull, container start, readiness probe lag) and the timings vLLM itself printed while starting. That log parsing is best effort: the wording depends on the vLLM version, and nothing is invented when a line does not match.

**Limits**
- Kubernetes timestamps have 1 s resolution, so every duration is plus or minus 1 s.
- If the NodeClaim is older than the pod, the script warns that a GPU node already existed (a warm start, not a cold start).
- If the container restarted, T3 is its latest start and the model initialization number is not a clean cold start.
- The script's own timeouts (pod appears 300 s, NodeClaim 180 s, ready 2400 s, load balancer target 300 s) only stop the measurement. They are unrelated to the application's request deadline.

## First observed run (2026-11-11, no inference request)

Original script output, with the invalid figures marked: [`experiments/results/cold-start-2026-11-11.txt`](experiments/results/cold-start-2026-11-11.txt).

Observed on a cold start that was already in progress; one `g5.xlarge`, us-east-1d, vLLM `v0.33.0`, model `Qwen/Qwen2.5-7B-Instruct`.

| | Time | Share of 701 s |
|---|---|---|
| Karpenter reaction | 2 s | |
| Node provisioning | 39 s | |
| GPU capacity ready (T2 - T0) | **41 s** | 6% |
| Container scheduling | **313 s** | 45% |
| of which image pull | 299 s | 43% |
| Model initialization | **347 s** | 49% |
| Pod ready (T4 - T0) | **701 s (11 min 41 s)** | |

Inside the 41 s: the EC2 instance launched 3 s after the NodeClaim, the node registered 25 s later and was Ready 11 s after that; the GPU was advertised about 9 s after Ready.

What vLLM reported during the 347 s: model loading 216 s in total (of which 105 s loading weights), torch.compile 17.6 s, graph capture 7 s. The model cache is an `emptyDir`, so the weights are very likely downloaded again on every cold start, which would explain the gap between the 216 s and the 105 s; this was not confirmed from the logs.

**Reading it.** Getting the GPU is not the slow part. About 93% of the time is spent on the image pull and on the model download, load and warm-up. The image (`vllm/vllm-openai:v0.33.0`) is about 9.7 GB. The Hugging Face cache is an `emptyDir`, so it does not survive a pod or node replacement. vLLM reported about 105 s loading weights, 216 s model loading in total, 17.6 s torch.compile and 7 s CUDA graph capture. The `nvidia.com/gpu` resource was available about 9 s after the node was Ready.

The pod-creation to Ready figure (701 s) is the reliable baseline. The `T5` / 1870 s numbers from the late-started script run are not a cold-start duration (see below).

## Second run: same pod, measured after the fact, with one inference (2026-11-11)

The script was started with `--test-inference` about 19 minutes after the pod became Ready, so it worked from the objects that still existed.

**The Kubernetes-recorded milestones came out identical to the first run** (T0 to T4, every duration, the breakdown, vLLM's own timings). That shows they are stable and can be read back later, as long as the pod, NodeClaim and node still exist.

**The inference path worked end to end.** The script fetched a Cognito token from the Terraform outputs, confirmed the pod was a healthy load balancer target, and sent one request to API Gateway with `provider: vllm`. It succeeded, and the request took **11 s**.

| | Value | How to read it |
|---|---|---|
| T5 (first inference) | 05:26:41Z | when the successful response arrived |
| Inference request latency | **11 s** | the one number from this run that describes a request. It covers API Gateway, Lambda, the load balancer and vLLM together, so it includes whatever the Lambda does before calling vLLM (it may have been a cold Lambda: finding the load balancer through the AWS API, opening the MCP session). It is an upper bound on what vLLM adds, not a vLLM-only figure; vLLM's own TTFT and end-to-end latency are on the dashboard. |

**Numbers from that first output that must not be used.** The original output printed `First request 1169s`, `Total usable cold start 1870s` and `NLB target healthy 1158s after T4`. They are artefacts of starting the script late: T5 minus T4 is the 19 minutes between the pod becoming Ready and the script running, and the load balancer check only recorded the moment the script first looked. The script's note said so, but the headline numbers still looked like results. The script was fixed afterwards: when the pod is already Ready it prints `n/a` for First request and Total usable cold start, leaves out the load balancer wait, and says why. The 701 s above is the valid total, and it is a pod-Ready figure.

## T4b for this run (from the log)

Reading the log of the same pod after the fact:

| | Time (UTC) |
|---|---|
| vLLM `Application startup complete` (T4b) | 05:07:07.5 |
| First `GET /health 200`, from the node's IP, i.e. the kubelet probe | 05:07:12.3 |
| Pod `Ready` (T4a) | 05:07:12 |

So the application could serve about **4.5 s** before Kubernetes marked the pod Ready. That is the readiness probe's lag (it runs every 10 s, so it can be up to 10 s). Pod Ready, 701 s, is the number that matters for traffic; the application itself was ready 4.5 s earlier. After Ready, the load balancer's own health checks started hitting the pod about 3 s later (several checks from different load balancer addresses, in the log).

**Still not measured, and needing a live run from the start** (see "Running it"):
- A live T4b observation. The log-derived value above is exact, but the live polling path (including the port-forward fix) has not been verified.
- A true "first request after Ready" time, with the load balancer registration measured as it happens.

## Configuration that shapes the result

**Probes.** The vLLM Deployment has a readiness probe only: `GET /health` on port 8000, every 10 s, 3 s timeout, `failureThreshold: 3`, first check after 30 s. During model start the probe failed repeatedly with connection refused, the restart count stayed 0 and the pod became Ready. That is correct for this workload: the Service routes only to Ready pods, readiness failures never restart the container, and there is no liveness or startup probe that could kill a slow start. A startup probe is not needed to support the slow start, and would not shorten it. (A comment in `vllm-deploy.yaml` still mentions 60 x 10 s; it is stale.)

**Karpenter GPU pool.** Instance type `g5.xlarge` only, on-demand, amd64, taint `nvidia.com/gpu=true:NoSchedule`, label `workload=gpu`. Limits: 4 CPU and 1 GPU. It consolidates only empty nodes, after 30 s, and nodes expire after 24 h. The node class uses the AL2023 AMI alias and a 100 GiB encrypted gp3 root volume. In the run above the sequence was Unschedulable, then Nominated, then a `g5.xlarge` NodeClaim within 2 s.

**Quota.** Running On-Demand G and VT instances: 4 vCPUs (adjustable). One `g5.xlarge` is 4 vCPUs and fits; two need 8 and do not. This is why a real `1 -> 2` scale-out cannot be validated today.

**Cost.** `g5.xlarge` on-demand in us-east-1 is $1.006/hour. One experiment (about 12 minutes to ready, one request, then teardown) is roughly $0.25 to $0.30 for the GPU node. The EKS control plane and the system node bill while the platform is up; a GPU node left running costs about $24 a day.

## Running it

The platform must be up (`scripts/up.sh`), with no GPU node and no vLLM Deployment, and `AWS_PROFILE` set. From the repo root:

```bash
kubectl delete deploy vllm --ignore-not-found     # then wait for the GPU node to disappear
kubectl get nodes -l workload=gpu                 # expect none
SUBNETS=$(aws eks describe-cluster --name ai-inference-lab --query 'cluster.resourcesVpcConfig.subnetIds' --output text | tr '\t' ',')
sed "s|\${VLLM_NLB_SUBNETS}|$SUBNETS|" terraform/environments/dev/eks-addons/manifests/vllm-service.yaml \
  | kubectl apply -f -                             # load balancer only, no GPU
scripts/measure_vllm_cold_start.py --test-inference > cold-start.txt &
sleep 5
kubectl apply -f terraform/environments/dev/eks-addons/manifests/vllm-deploy.yaml
wait                                              # about 12-15 minutes
kubectl delete deploy vllm                        # stop paying; Karpenter removes the node after about 30 s
```

Options: `--json` for machine-readable output, `--pod NAME` to measure a specific pod, `--timeout SECONDS`. Run it without `--test-inference` to measure only up to Ready; it can also be started after the pod is already running.

Its tests need no cluster: `gateway/.venv/bin/python -m pytest scripts/tests -q`.

## Where the time could be reduced (not implemented)

These follow from the numbers above and are listed only so they are not forgotten:

- **Image pull (about 43%).** Pre-pull the image onto the node, bake it into a custom AMI, or use a smaller or regionally cached image.
- **Model download and load (about 49%).** Keep the weights somewhere that survives a new node (a pre-populated volume, S3, or a baked image) instead of an `emptyDir`.
- **GPU capacity (about 6%).** Already small; keeping a warm node trades cost for latency and is a separate decision.

## Dashboard signals for a cold start

The CloudWatch dashboard's cluster widget shows GPU count, vLLM replicas desired and available, and restarts; the vLLM widgets show running and waiting requests, KV-cache usage, token throughput, TTFT and end-to-end latency. Desired versus available replicas is what shows the Pending phase, because the GPU count only appears once the GPU exporter is running on the new node. The vLLM, GPU and cluster queries are still drafts until checked against live series.

## A second data point: node replacement after a failure (2026-11-11)

When the GPU instance was terminated, the replacement went through the same path on a fresh node in another AZ: NodeClaim to node Ready 37 s, image pull 5 min 9 s (not cached), container start to Ready 5 min 28 s. Pod created to Ready took 20 min 02 s because the replacement NodeClaim was delayed by the 4 vCPU quota. See [`experiments/vllm-failure-recovery.md`](experiments/vllm-failure-recovery.md).
