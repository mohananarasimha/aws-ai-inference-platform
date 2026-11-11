# Decision: CloudWatch-only observability for the gateway, vLLM and EKS

Status: **implemented 2026-11-09 (code, Terraform and tests; nothing applied).** The vLLM, GPU and Kubernetes dashboard queries are drafts until the first live run.

## What

- **Lambda agent:** CloudWatch Embedded Metric Format, one record per finished request, emitted by the handler wrapper (`gateway/lambda/metrics.py`). Namespace `EnterpriseAIPlatform/Gateway`. Metrics: `RequestCount`, `RequestLatency` (Provider, Status), `ModelLatency` (Provider), `TimeoutCount` (Provider, Component, Type), `ErrorCount` (Provider, ErrorType). 4xx responses and the `vllm_test` diagnostic are not counted. RAG and MCP timings stay in the structured logs.
- **vLLM, GPU, Kubernetes:** the `amazon-cloudwatch-observability` EKS add-on (`eks-addons/cloudwatch_observability.tf`), OTel Container Insights only. The add-on finds vLLM itself (any container whose image name contains `vllm`, declared port 8000, Running pods) and manages the DCGM exporter. Metrics go to CloudWatch over OTLP and are read with PromQL.
- **Dashboard:** one, in `app` (`dashboard.tf`), 10 widgets: 4 agent/Lambda (metric widgets), 4 vLLM, 2 GPU/cluster (PromQL `chart` widgets).

## Why this shape

- No Prometheus, Grafana or custom DCGM stack: the add-on already covers vLLM scraping and GPU metrics.
- OTel-only: the legacy Container Insights pipeline is billed per observation and writes performance logs; running both would pay twice for the same node and pod data. Logs, Application Signals and Fluent Bit are off.
- Dashboard in `app`: it outlives EKS teardown and references only metric names, so there is no lifecycle link to `eks`/`eks-addons`.
- vLLM image pinned to `v0.33.0` so metric names do not change on a pull.
- IAM: AWS-managed `CloudWatchAgentServerPolicy` on a dedicated Pod Identity role for the `cloudwatch-agent` service account. Broader than needed (X-Ray, `logs:*` creation); narrowing is deferred.

## Known limits

- The v7.0.1 add-on schema rejects `otelFilters`, so metric volume cannot be filtered per name; only components can be switched off.
- Metric names (`vllm:*_total` suffixes) and PromQL label names in CloudWatch are unconfirmed. Widgets are titled `[draft query]` until checked in Query Studio.
- The add-on adds about 650m CPU and 384Mi to the single t3.medium system node (about 1.6 of 1.9 allocatable CPU with Karpenter and the load balancer controller).

## Not done yet

Alarms, tracing, autoscaling on these metrics.
