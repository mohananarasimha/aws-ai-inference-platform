locals {
  metrics_namespace = "EnterpriseAIPlatform/Gateway"

  # vLLM, GPU and Kubernetes arrive through the CloudWatch Observability
  # add-on over OTLP and are read with PromQL. These queries are drafts: the
  # metric and label names are taken from the vLLM v0.33.0 docs and the DCGM
  # field list, and are confirmed against the live series after the first run.
  promql = {
    vllm_requests  = ["vllm:num_requests_running", "vllm:num_requests_waiting"]
    vllm_kv_cache  = ["vllm:kv_cache_usage_perc * 100"]
    vllm_tokens    = ["sum(rate(vllm:prompt_tokens_total[5m]))", "sum(rate(vllm:generation_tokens_total[5m]))"]
    vllm_latency   = ["histogram_quantile(0.95, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket[5m])))", "histogram_quantile(0.95, sum by (le) (rate(vllm:e2e_request_latency_seconds_bucket[5m])))"]
    gpu            = ["avg(DCGM_FI_DEV_GPU_UTIL)", "avg(DCGM_FI_DEV_FB_USED_PERCENT)", "avg(DCGM_FI_DEV_GPU_TEMP)", "avg(DCGM_FI_DEV_POWER_USAGE)"]
    cluster_health = ["count(DCGM_FI_DEV_GPU_UTIL)", "sum(kube_deployment_spec_replicas{deployment=\"vllm\"})", "sum(kube_deployment_status_replicas_available{deployment=\"vllm\"})", "sum(kube_pod_container_status_restarts_total{container=\"vllm\"})"]
  }

  promql_labels = {
    vllm_requests  = ["running", "waiting"]
    vllm_kv_cache  = ["KV cache %"]
    vllm_tokens    = ["prompt tokens/s", "generated tokens/s"]
    vllm_latency   = ["TTFT p95 (s)", "end-to-end p95 (s)"]
    gpu            = ["GPU util %", "VRAM %", "temperature C", "power W"]
    cluster_health = ["GPU count", "vLLM replicas desired", "vLLM replicas available", "vLLM restarts"]
  }

  agent_metric_defaults = {
    region = var.aws_region
    view   = "timeSeries"
  }
}

locals {
  dashboard_widgets = concat(
    [
      {
        type = "metric", x = 0, y = 0, width = 6, height = 6
        properties = merge(local.agent_metric_defaults, {
          title   = "Agent: requests by provider and status"
          stacked = true
          period  = 300
          metrics = [[{
            expression = "SEARCH('{${local.metrics_namespace},Provider,Status} MetricName=\"RequestCount\"', 'Sum', 300)"
            id         = "requests"
          }]]
        })
      },
      {
        type = "metric", x = 6, y = 0, width = 6, height = 6
        properties = merge(local.agent_metric_defaults, {
          title  = "Agent: latency p95, request vs model (ms)"
          period = 300
          stat   = "p95"
          metrics = [
            [local.metrics_namespace, "RequestLatency", "Provider", "bedrock", "Status", "success", { label = "bedrock request" }],
            [local.metrics_namespace, "RequestLatency", "Provider", "vllm", "Status", "success", { label = "vllm request" }],
            [local.metrics_namespace, "ModelLatency", "Provider", "bedrock", { label = "bedrock model" }],
            [local.metrics_namespace, "ModelLatency", "Provider", "vllm", { label = "vllm model" }],
          ]
        })
      },
      {
        type = "metric", x = 12, y = 0, width = 6, height = 6
        properties = merge(local.agent_metric_defaults, {
          title  = "Agent: timeouts by component, errors by type"
          period = 300
          metrics = [
            [{
              expression = "SEARCH('{${local.metrics_namespace},Provider,Component,Type} MetricName=\"TimeoutCount\"', 'Sum', 300)"
              id         = "timeouts"
            }],
            [{
              expression = "SEARCH('{${local.metrics_namespace},Provider,ErrorType} MetricName=\"ErrorCount\"', 'Sum', 300)"
              id         = "errors"
            }],
          ]
        })
      },
      {
        type = "metric", x = 18, y = 0, width = 6, height = 6
        properties = merge(local.agent_metric_defaults, {
          title  = "Lambda: duration p95 and errors"
          period = 300
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", local.gateway_function_name, { stat = "p95", label = "duration p95 (ms)" }],
            ["AWS/Lambda", "Errors", "FunctionName", local.gateway_function_name, { stat = "Sum", yAxis = "right", label = "errors" }],
          ]
        })
      },
    ],
    [
      for index, widget in [
        { key = "vllm_requests", title = "vLLM: running vs waiting requests [draft query]", x = 0, y = 6, width = 6 },
        { key = "vllm_kv_cache", title = "vLLM: KV-cache usage % [draft query]", x = 6, y = 6, width = 6 },
        { key = "vllm_tokens", title = "vLLM: token throughput [draft query]", x = 12, y = 6, width = 6 },
        { key = "vllm_latency", title = "vLLM: TTFT and end-to-end p95 [draft query]", x = 18, y = 6, width = 6 },
        { key = "gpu", title = "GPU: utilization, VRAM, temperature, power [draft query]", x = 0, y = 12, width = 12 },
        { key = "cluster_health", title = "Cluster: GPU count, vLLM replicas desired/available, restarts [draft query]", x = 12, y = 12, width = 12 },
        ] : {
        type = "chart", x = widget.x, y = widget.y, width = widget.width, height = 6
        properties = {
          view   = "line"
          title  = widget.title
          region = var.aws_region
          data = {
            queries = [
              for position, query in local.promql[widget.key] : {
                id       = "${widget.key}_${position}"
                type     = "cloudwatch-metrics"
                language = "PromQL"
                query    = query
                label    = local.promql_labels[widget.key][position]
              }
            ]
          }
        }
      }
    ],
    [
      {
        type = "metric", x = 0, y = 18, width = 12, height = 6
        properties = merge(local.agent_metric_defaults, {
          title  = "Inference NLB: healthy and unhealthy targets"
          period = 60
          metrics = [
            [{
              expression = "SEARCH('{AWS/NetworkELB,LoadBalancer,TargetGroup} MetricName=\"HealthyHostCount\" vllm', 'Minimum', 60)"
              id         = "nlb_healthy"
            }],
            [{
              expression = "SEARCH('{AWS/NetworkELB,LoadBalancer,TargetGroup} MetricName=\"UnHealthyHostCount\" vllm', 'Maximum', 60)"
              id         = "nlb_unhealthy"
            }],
          ]
        })
      },
    ]
  )
}

resource "aws_cloudwatch_dashboard" "platform" {
  dashboard_name = "${local.name}-platform"
  dashboard_body = jsonencode({ widgets = local.dashboard_widgets })
}
