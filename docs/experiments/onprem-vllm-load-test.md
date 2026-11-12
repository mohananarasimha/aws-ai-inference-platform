# On-prem vLLM load test

Status: **tooling ready, experiment not yet run.** Every `_to fill_` below is waiting for measured data; nothing in this file is a result. Earlier on-prem experiments are not evidenced in this repository (see [`../known-limitations.md`](../known-limitations.md)); this run is meant to replace them with reproducible evidence.

## Environment

Facts supplied for the setup. Fill the rest in from the machines on the day of the run.

| Item | Value |
|---|---|
| Hardware | 2 x RTX 4090 (24 GB each) |
| Model | `Qwen/Qwen2.5-7B-Instruct` |
| vLLM | v0.33.0 |
| Replicas | GPU0 on `10.100.0.3:18080`, GPU1 on `10.100.0.3:18081` |
| Load balancer | Nginx on `10.100.0.3:18082`, `least_conn` |
| Load generator | AWS router EC2 (WireGuard), instance type: _to fill_ |
| vLLM launch flags (both replicas) | _to fill: `--max-model-len`, `--gpu-memory-utilization`, `--max-num-seqs`, anything else_ |
| Nginx upstream and timeout settings | _to fill: paste the upstream block, `proxy_read_timeout`, `keepalive`_ |
| Date, operator, tooling commit | _to fill_ |
| Clock sync, router and GPU host | _to fill: output of `chronyc tracking` or `timedatectl` on both_ |

## Architecture

```text
load generator (AWS router EC2)
  -> WireGuard
  -> Nginx least_conn  10.100.0.3:18082
       -> vLLM replica GPU0  10.100.0.3:18080
       -> vLLM replica GPU1  10.100.0.3:18081
```

The test hits the Nginx endpoint directly. API Gateway, Cognito and Lambda are deliberately not in the path: this measures inference-serving capacity, not the end-to-end route (which was tested separately). The WireGuard hop and the router's own capacity are part of the path and are checked as possible limits (see Methodology).

## Workload

- `POST /v1/chat/completions`, non-streaming, `temperature: 0`, `max_tokens: 128`.
- One fixed-size infrastructure prompt (about 250 words of filler). A unique request ID is the first thing in every prompt, so prefix caching cannot make requests artificially cheap.
- Prompt size is constant; the measured mean prompt and completion tokens per stage are in `load-stage-summary.csv`.
- Closed loop: a stage with concurrency N keeps N requests in flight.
- Default stages: 1, 10, 25, 50, 75, 100, 125, 150. Warm-up 20 s (discarded), measurement 60 s, then drain.

## Methodology

Two scripts, run on two machines, correlated by UTC timestamps.

1. `scripts/onprem_metrics.py capture` on the GPU host samples both vLLM `/metrics` endpoints and `nvidia-smi` about once per second.
2. `scripts/run_onprem_load_test.py` on the router runs the stages one after another and records each stage's UTC measurement window.
3. `scripts/onprem_metrics.py summarize` joins the two, offline.

Definitions used by the load generator:

- A request counts in a stage if it **completes** inside the measurement window. `attempted = successful + failed`.
- `requests_per_sec` is successful requests divided by the window length; failures are not throughput.
- p50/p95/p99 are client-side, over successful requests, from sending to reading the full response. At low concurrency there are few samples and p99 approaches the maximum; read it with the `successful` count.
- Requests still in flight at the end of the window are drained and excluded (`unfinished_at_end`).
- A stage whose error rate reaches `--stop-error-rate` (default 0.5) ends the run instead of pushing higher.

Checks that separate "the target is saturated" from "the test rig is the limit":

- `generator_cpu_pct` in `load-stage-summary.csv` (the generator warns at 80%). Also watch `top` on the router during the run.
- Nginx errors and timeouts (`504`, `502`) in the per-stage error counts in `load-stage-windows.json`.
- New Relic is used only as a visual cross-check during the run. No evidence in this document depends on it.

Known limits of the method:

- Non-streaming requests: time to first token comes only from vLLM's server-side histogram, not from the client.
- Histogram percentiles (TTFT, TPOT, ITL, e2e) are interpolated from vLLM's bucket edges, so they are coarse.
- Server metrics use cumulative counters and 1 s sampling; very short events can fall between samples.

## Metrics captured

vLLM names, as defined in `vllm/v1/metrics/loggers.py` at v0.33.0 (counters are exposed with a `_total` suffix; the script accepts both spellings). Confirm against the live endpoint using the `<output>.metric-names.txt` file written by `capture`.

| Signal | vLLM metric |
|---|---|
| Running requests | `vllm:num_requests_running` |
| Waiting requests | `vllm:num_requests_waiting` |
| KV cache utilization | `vllm:kv_cache_usage_perc` (1 = 100%) |
| Preemptions | `vllm:num_preemptions` |
| Prompt / generation tokens | `vllm:prompt_tokens`, `vllm:generation_tokens` |
| TTFT | `vllm:time_to_first_token_seconds` |
| Server-side e2e latency | `vllm:e2e_request_latency_seconds` |
| Time per output token | `vllm:request_time_per_output_token_seconds` |
| Inter-token latency | `vllm:inter_token_latency_seconds` |

Each replica is recorded separately. GPU metrics for GPU0 and GPU1 from `nvidia-smi`: utilization %, VRAM used and total (MiB), temperature (C), power draw (W).

Metrics missing on the live endpoint: _to fill from `metric-names.txt`, or "none"._

## Results

Per-stage summary from `onprem-load-summary.csv`. _To fill after the run._

| Concurrency | req/s | p50 ms | p95 ms | p99 ms | error rate | GPU0 util avg/max | GPU1 util avg/max | max KV | max running | max waiting | preemptions | gen tok/s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| _to fill_ | | | | | | | | | | | | |

## Saturation analysis

Paste the evidence block printed by `summarize`, then interpret it. The script reports what the data shows and does not declare a saturation point.

| Question | Evidence from the run |
|---|---|
| Highest healthy concurrency | _to fill_ |
| First concurrency with waiting requests | _to fill_ |
| First concurrency with preemptions | _to fill_ |
| First concurrency with high KV cache pressure | _to fill_ |
| Peak throughput stage | _to fill_ |
| Throughput stops scaling while p95 rises | _to fill_ |
| Peak GPU utilization | _to fill_ |

Thresholds used by `summarize` (`--kv-high`, `--scale-gain-min`, `--p95-rise-min`): _to fill_. "Healthy" is defined here as: _to fill (for example, error rate below X% and p95 below Y ms)_.

## Observations

_To fill: load balance between the two replicas, the generator-limited check, anything unexpected, anything that could not be explained._

## Conclusions

_To fill, limited to what the evidence above supports._

## Evidence artifacts

Copy these into `docs/experiments/results/` after the run (suggested date suffix, for example `2026-11-xx`):

| File | Produced by | Machine |
|---|---|---|
| [`results/load-stage-summary.csv`](results/load-stage-summary.csv) | `run_onprem_load_test.py` | router |
| [`results/load-stage-windows.json`](results/load-stage-windows.json) | `run_onprem_load_test.py` | router |
| [`results/onprem-load-timeseries.csv`](results/onprem-load-timeseries.csv) | `onprem_metrics.py capture` | GPU host |
| [`results/onprem-load-timeseries.metric-names.txt`](results/onprem-load-timeseries.metric-names.txt) | `onprem_metrics.py capture` | GPU host |
| [`results/onprem-load-summary.csv`](results/onprem-load-summary.csv) | `onprem_metrics.py summarize` | anywhere |

Not committed by default: the optional per-request JSONL from `--per-request-jsonl` (large).

These links resolve only once the files are added.
