"""
Parsing and summarising for scripts/onprem_metrics.py, on synthetic data only:
no GPU, vLLM or network needed.
"""

import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import onprem_metrics as om  # noqa: E402

EXPOSITION = '''\
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="Qwen/Qwen2.5-7B-Instruct"} 12.0
vllm:num_requests_waiting{engine="0",model_name="Qwen/Qwen2.5-7B-Instruct"} 3.0
vllm:kv_cache_usage_perc{engine="0",model_name="Qwen/Qwen2.5-7B-Instruct"} 0.42
vllm:num_preemptions_total{engine="0",model_name="Qwen/Qwen2.5-7B-Instruct"} 5.0
vllm:num_preemptions_created{engine="0"} 1.7e9
vllm:prompt_tokens_total{engine="0",model_name="a,b {x}"} 1000.0
vllm:generation_tokens_total{engine="0",model_name="Qwen/Qwen2.5-7B-Instruct"} 2000.0
vllm:time_to_first_token_seconds_bucket{engine="0",le="0.1"} 10.0
vllm:time_to_first_token_seconds_bucket{engine="0",le="1.0"} 90.0
vllm:time_to_first_token_seconds_bucket{engine="0",le="+Inf"} 100.0
vllm:time_to_first_token_seconds_sum{engine="0"} 55.0
vllm:time_to_first_token_seconds_count{engine="0"} 100.0
python_gc_objects_collected_total{generation="0"} 4.0
'''


def test_parse_prometheus_handles_labels_comments_and_foreign_metrics():
    samples = om.parse_prometheus(EXPOSITION)
    names = [n for n, _, _ in samples]
    assert "vllm:num_requests_running" in names
    assert "python_gc_objects_collected_total" in names
    assert not any(n.startswith("#") for n in names)
    odd = [labels for n, labels, _ in samples if n == "vllm:prompt_tokens_total"][0]
    assert odd["model_name"] == "a,b {x}"


def test_find_scalar_prefers_first_candidate_and_falls_back():
    samples = om.parse_prometheus(EXPOSITION)
    assert om.find_scalar(samples, ["vllm:num_preemptions_total", "vllm:num_preemptions"], "sum") == (
        5.0, "vllm:num_preemptions_total")
    legacy = om.parse_prometheus("vllm:num_preemptions 7\n")
    assert om.find_scalar(legacy, ["vllm:num_preemptions_total", "vllm:num_preemptions"], "sum") == (
        7.0, "vllm:num_preemptions")
    assert om.find_scalar(samples, ["vllm:nope"], "sum") == (None, None)


def test_find_scalar_ignores_created_series():
    samples = om.parse_prometheus(EXPOSITION)
    value, _ = om.find_scalar(samples, ["vllm:num_preemptions_total"], "sum")
    assert value == 5.0


def test_histogram_roundtrip_and_quantile():
    samples = om.parse_prometheus(EXPOSITION)
    buckets, total_sum, total_count = om.find_histogram(samples, "vllm:time_to_first_token_seconds")
    assert (total_sum, total_count) == (55.0, 100.0)
    decoded = om.decode_buckets(om.encode_buckets(buckets))
    assert decoded == buckets
    # rank 95 falls in the +Inf bucket, so the last finite edge is returned
    assert om.histogram_quantile(0.95, buckets) == 1.0
    # rank 50 is in (0.1, 1.0]: 0.1 + 0.9 * (50-10)/(90-10)
    assert om.histogram_quantile(0.5, buckets) == pytest.approx(0.55)
    assert om.histogram_quantile(0.5, {}) is None


def test_expected_metric_report_lists_missing():
    samples = om.parse_prometheus(EXPOSITION)
    missing = om.expected_metric_report(samples)
    assert "vllm:e2e_request_latency_seconds" in missing
    assert not any("kv_cache_usage_perc" in m for m in missing)


def test_metric_inventory_collapses_histogram_series():
    inv = om.metric_inventory(om.parse_prometheus(EXPOSITION))
    assert inv["vllm:time_to_first_token_seconds"] == "histogram"
    assert "vllm:time_to_first_token_seconds_bucket" not in inv
    assert "python_gc_objects_collected_total" not in inv


def test_parse_nvidia_csv_handles_na_and_garbage():
    text = "0, 87, 20113, 24564, 71, 312.45\n1, 0, 0, 24564, 33, [N/A]\nnot a row\n"
    gpus = om.parse_nvidia_csv(text)
    assert gpus[0] == {"util_pct": 87.0, "mem_used_mib": 20113.0, "mem_total_mib": 24564.0,
                       "temp_c": 71.0, "power_w": 312.45}
    assert gpus[1]["power_w"] is None


def test_capture_header_has_timestamp_and_per_replica_gpu_columns():
    header = om.capture_header(["gpu0", "gpu1"], [0, 1])
    assert header[0] == "timestamp_utc"
    for name in ("vllm_gpu1_kv_cache_usage", "vllm_gpu0_ttft_buckets", "nvsmi_gpu1_power_w"):
        assert name in header


def test_cli_parsing():
    args = om.parse_args(["capture", "--replica", "a=http://x:1/metrics", "--duration", "5"])
    assert args.mode == "capture" and args.replica == ["a=http://x:1/metrics"] and args.duration == 5
    args = om.parse_args(["summarize", "--kv-high", "0.8"])
    assert args.mode == "summarize" and args.kv_high == 0.8
    with pytest.raises(SystemExit):
        om.parse_args(["capture", "--interval", "0"])
    with pytest.raises(SystemExit):
        om.parse_args([])


def test_capture_rejects_bad_replica_spec(tmp_path):
    args = om.parse_args(["capture", "--replica", "not-a-spec", "--output", str(tmp_path / "t.csv")])
    with pytest.raises(SystemExit):
        om.run_capture(args)


# ---------------------------------------------------------------- summarize, end to end

T0 = 1_800_000_000.0


def iso(t):
    return om.utc_iso(T0 + t)


def build_timeseries(path, seconds=130):
    """gpu0/gpu1 over `seconds` one-second samples. Stage 1 = t 10..40, stage 2 = t 50..80,
    stage 3 = t 90..120. Load grows, waiting appears in stage 2, preemptions and
    KV >= 0.9 in stage 3."""
    header = om.capture_header(["gpu0", "gpu1"], [0, 1])
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header, restval="")
        w.writeheader()
        pre = {"gpu0": 0, "gpu1": 0}
        tokens = {"gpu0": 0, "gpu1": 0}
        for t in range(seconds):
            stage = 1 if 10 <= t <= 40 else 2 if 50 <= t <= 80 else 3 if 90 <= t <= 120 else 0
            row = {"timestamp_utc": iso(t)}
            for r in ("gpu0", "gpu1"):
                tokens[r] += {0: 0, 1: 100, 2: 300, 3: 320}[stage]
                if stage == 3 and t >= 100:
                    pre[r] += 1
                row.update({
                    f"vllm_{r}_scrape_ok": 1,
                    f"vllm_{r}_running": {0: 0, 1: 1, 2: 20, 3: 30}[stage],
                    f"vllm_{r}_waiting": 2 if stage == 2 and t == 60 else 5 if stage == 3 else 0,
                    f"vllm_{r}_kv_cache_usage": {0: 0, 1: 0.1, 2: 0.5, 3: 0.95}[stage],
                    f"vllm_{r}_preemptions_total": pre[r],
                    f"vllm_{r}_generation_tokens_total": tokens[r],
                    f"vllm_{r}_prompt_tokens_total": tokens[r] * 2,
                })
            for g in (0, 1):
                util = {0: 0, 1: 20, 2: 85, 3: 99}[stage]
                row.update({f"nvsmi_gpu{g}_util_pct": util, f"nvsmi_gpu{g}_mem_used_mib": 18000 + stage * 1000,
                            f"nvsmi_gpu{g}_mem_total_mib": 24564, f"nvsmi_gpu{g}_temp_c": 60 + stage,
                            f"nvsmi_gpu{g}_power_w": 200 + stage * 50})
            w.writerow(row)


def write_load_files(tmp_path):
    stage_csv = tmp_path / "load-stage-summary.csv"
    with open(stage_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["stage", "concurrency", "requests_attempted", "requests_successful",
                                           "requests_failed", "requests_per_sec", "p50_ms", "p95_ms", "p99_ms",
                                           "error_rate"])
        w.writeheader()
        # stage 3: throughput barely grows (10 -> 10.4) while p95 rises 2.1 s -> 6.0 s
        w.writerow(dict(stage=1, concurrency=1, requests_attempted=20, requests_successful=20, requests_failed=0,
                        requests_per_sec=0.5, p50_ms=2000, p95_ms=2100, p99_ms=2200, error_rate=0))
        w.writerow(dict(stage=2, concurrency=50, requests_attempted=300, requests_successful=300, requests_failed=0,
                        requests_per_sec=10.0, p50_ms=2500, p95_ms=2900, p99_ms=3100, error_rate=0))
        w.writerow(dict(stage=3, concurrency=100, requests_attempted=312, requests_successful=312, requests_failed=0,
                        requests_per_sec=10.4, p50_ms=5000, p95_ms=6000, p99_ms=6500, error_rate=0))
    windows = tmp_path / "load-stage-windows.json"
    windows.write_text(json.dumps({"status": "completed", "stages": [
        {"stage": 1, "concurrency": 1, "measurement_start_utc": iso(10), "measurement_end_utc": iso(40)},
        {"stage": 2, "concurrency": 50, "measurement_start_utc": iso(50), "measurement_end_utc": iso(80)},
        {"stage": 3, "concurrency": 100, "measurement_start_utc": iso(90), "measurement_end_utc": iso(120)},
    ]}))
    series = tmp_path / "onprem-load-timeseries.csv"
    build_timeseries(series)
    return stage_csv, windows, series


def run_summarize(tmp_path, extra=()):
    stage_csv, windows, series = write_load_files(tmp_path)
    out = tmp_path / "onprem-load-summary.csv"
    args = om.parse_args(["summarize", "--stage-summary", str(stage_csv), "--windows", str(windows),
                          "--timeseries", str(series), "--output", str(out), *extra])
    assert om.run_summarize(args) == 0
    with open(out, newline="") as fh:
        return list(csv.DictReader(fh))


def test_summarize_correlates_windows_and_computes_stage_metrics(tmp_path):
    rows = run_summarize(tmp_path)
    assert [r["concurrency"] for r in rows] == ["1", "50", "100"]
    s1, s2, s3 = rows
    assert float(s2["gpu0_util_avg_pct"]) == 85 and float(s3["gpu1_util_max_pct"]) == 99
    assert float(s3["gpu0_vram_max_mib"]) == 21000
    assert float(s3["kv_cache_max"]) == 0.95
    assert float(s2["running_max_total"]) == 40 and float(s3["waiting_max_total"]) == 10
    assert float(s1["waiting_max_total"]) == 0 and float(s2["waiting_max_total"]) == 4
    # +1 per replica per second for t=100..120 inclusive = 21 each, 42 in total
    assert float(s2["preemptions_delta"]) == 0 and float(s3["preemptions_delta"]) == 42
    # 2 replicas x 300 tokens/s
    assert float(s2["generation_tokens_per_sec"]) == pytest.approx(600)
    assert float(s2["prompt_tokens_per_sec"]) == pytest.approx(1200)
    assert float(s1["p95_ms"]) == 2100 and float(s2["requests_per_sec"]) == 10.0
    assert s1["ttft_p95_ms"] == ""  # histograms were not in the synthetic capture


def test_summarize_prints_evidence(tmp_path, capsys):
    run_summarize(tmp_path)
    out = capsys.readouterr().out
    assert "first concurrency with waiting_requests > 0: conc 50" in out
    assert "first concurrency with preemptions: conc 100" in out
    assert "KV cache >= 90%: conc 100" in out
    assert "peak throughput: 10.4 req/s at conc 100" in out
    assert "stops scaling while p95 rises: conc 100" in out
    assert "peak GPU utilization: 99% on gpu0 at conc 100" in out


def test_summarize_reports_not_observed_instead_of_inventing(tmp_path, capsys):
    run_summarize(tmp_path, ["--kv-high", "0.99", "--scale-gain-min", "0.001"])
    out = capsys.readouterr().out
    assert "KV cache >= 99%: not observed; highest seen 95.0% at conc 100" in out
    assert "throughput stall with rising p95: not observed" in out


def test_summarize_clock_offset_moves_the_window(tmp_path):
    rows = run_summarize(tmp_path, ["--metrics-clock-offset-seconds", "-45"])
    # metric timestamps shifted 45 s earlier: stage 2 (t 50..80) now sees what was t 95..125
    assert float(rows[1]["kv_cache_max"]) == 0.95


def test_counter_rate_handles_restart():
    rows = [{"_ts": float(t), "c": str(v)} for t, v in enumerate([10, 12, 15, 2, 5])]
    delta, seconds = om.counter_rate(None, rows, "c")
    assert (delta, seconds) == (2 + 3 + 2 + 3, 4.0)  # the drop to 2 counts as +2
    assert om.counter_rate(None, rows[:1], "c") == (None, None)


def test_summarize_rejects_concurrency_mismatch(tmp_path):
    stage_csv, windows, series = write_load_files(tmp_path)
    data = json.loads(windows.read_text())
    data["stages"][1]["concurrency"] = 999
    windows.write_text(json.dumps(data))
    args = om.parse_args(["summarize", "--stage-summary", str(stage_csv), "--windows", str(windows),
                          "--timeseries", str(series), "--output", str(tmp_path / "o.csv")])
    with pytest.raises(SystemExit):
        om.run_summarize(args)
